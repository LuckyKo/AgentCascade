"""BUG_0029 Phase 2 — the single owner of UI-settable runtime state.

One import gets everything:
    from agent_cascade.runtime_state import state

Every attribute below is the LIVE value. Readers MUST access it as ``state.<name>`` at the
point of use. Never ``from agent_cascade.runtime_state import <name>`` for a field — that
rebinds a snapshot and reintroduces the exact bug class this module exists to kill.

Import-cycle note: this module imports only ``threading``, ``time`` and immutable constants
from ``agent_cascade.settings``. ``ResettableRLock`` (moved here verbatim from
security_handler.py) is constructed at import time; its ``force_reset()`` logs via a local
import so no cycle can form.
"""

import threading
import time

from agent_cascade.settings import (COMPRESSION_DEFAULT_FRACTION, COMPRESSION_MAX_FRACTION,
                                    COMPRESSION_MIN_FRACTION)


class ResettableRLock:
    """An RLock wrapper that can recover from a leaked lock.

    The plain ``threading.RLock`` used for the security execution lock is acquired
    by a daemon thread (``_run_check_worker``). If that thread is killed before it
    reaches ``exec_lock.release()`` — e.g. session stop, agent dismissal, or an
    unhandled crash that skips the ``finally`` block — the RLock is leaked forever
    and every subsequent security check times out on ``acquire(timeout=10s)``.

    Python's RLock cannot be force-released from another thread, so this wrapper
    tracks the owning thread and, when a new acquirer detects that the previous
    holder is DEAD (no longer alive), it replaces the internal RLock with a fresh
    one. This is safe because:
      - We only reset when ``acquire()`` timed out, i.e. the current thread is NOT
        the owner, so no live thread holds the lock we are about to discard.
      - A LIVE holder (another check genuinely running) is never reset — its thread
        is still alive, so normal timeout semantics apply and the caller raises.

    Reentrancy is preserved: the internal ``threading.RLock`` handles same-thread
    nested acquisition exactly as before.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._owner_thread = None  # threading.Thread of the current holder (None if free)
        self._acquired_at = 0.0  # time.monotonic() when acquired (for staleness logging)

    def acquire(self, timeout=None):
        """Acquire with optional timeout. Returns True on success, False on timeout.

        ``timeout=None`` means block until acquired (no timeout), matching the
        native RLock's no-arg behavior. We branch explicitly because passing
        ``timeout=None`` to the underlying RLock.acquire() raises TypeError.
        """
        if timeout is None:
            acquired = self._lock.acquire()
        else:
            acquired = self._lock.acquire(timeout=timeout)
        if acquired:
            self._owner_thread = threading.current_thread()
            self._acquired_at = time.monotonic()
        return acquired

    def release(self):
        """Release the lock (normal path — called from the finally block)."""
        try:
            self._lock.release()
        finally:
            # Clear ownership tracking even if release raised, so a later
            # force_reset decision is not based on stale owner info.
            self._owner_thread = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError('ResettableRLock: timed out acquiring lock')
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    @property
    def owner_is_alive(self) -> bool:
        """True if the current holder thread is still running.

        A live holder means another check is genuinely in progress — we must NOT
        steal its lock. A dead holder (or none) means the lock may be leaked.

        Note: reading ``_owner_thread`` and calling ``is_alive()`` is not a single
        atomic step, but this is safe by construction — a thread that is alive when
        read can only transition to *dead* afterwards, never the reverse. So the only
        "wrong" outcome we could observe is treating a just-died holder as alive (we
        then block/timeout normally and recover on the *next* attempt), which is
        strictly safer than the opposite error of stealing a live lock.
        """
        owner = self._owner_thread
        return owner is not None and owner.is_alive()

    def force_reset(self, reason: str = '') -> bool:
        """Force-release a leaked lock by swapping in a fresh RLock.

        DANGEROUS — only call when the previous holder is known to be dead.
        Returns True if the reset actually swapped the lock (i.e. it was held),
        False if the lock was already free (nothing to reset).
        """
        from agent_cascade.log import logger
        was_held = self._owner_thread is not None
        # Swap internals: any future acquire() targets the fresh RLock. The old
        # RLock object becomes garbage once no live thread references it — which
        # is guaranteed because we only call this after an acquire timeout (the
        # current thread was never granted it).
        self._lock = threading.RLock()
        self._owner_thread = None
        self._acquired_at = 0.0
        if was_held:
            logger.warning(f"[SECURITY] Execution lock force-reset (leaked by dead holder): {reason}")
        return was_held


def clamp_compression_fraction(value) -> float:
    """Clamp a compression fraction into the [MIN, MAX] bounds.

    The single clamp for the process-global ``state.compression_fraction`` — the
    duplicated clamps in config_persist.py / config_handlers.py are deleted (D-2c).
    """
    return min(COMPRESSION_MAX_FRACTION, max(COMPRESSION_MIN_FRACTION, float(value)))


class RuntimeState:
    """Process-level singleton holding every UI-settable runtime value.

    Fields, grouped by owner-of-record (persistence still lives in the pool):

    - ``auto_security`` (bool) — single writer: :meth:`set_auto_security` (the toggle).
      The assignment channel is :meth:`_assign_auto_security` (boot/load paths only).
    - ``compression_fraction`` (float) — single writer: :meth:`set_compression_fraction`.
    - ``afk_enabled`` / ``afk_message`` — single writer: :meth:`set_afk`.
    - ``enable_timeout`` / ``approval_timeout_seconds`` — single writers:
      :meth:`set_enable_timeout` / :meth:`set_approval_timeout`.
    - Security synchronization primitives (constructed once here; Step 4 moves the
      handlers' lazy creation onto these).
    """

    def __init__(self):
        # ── UI-settable settings ────────────────────────────────────────────
        self.auto_security: bool = True
        # Seeded from the env-var default in settings.py; runtime mutations happen
        # exclusively through set_compression_fraction() (Step 3 re-points readers here).
        self.compression_fraction: float = COMPRESSION_DEFAULT_FRACTION
        self.afk_enabled: bool = False
        self.afk_message: str = ''
        self.enable_timeout: bool = True
        self.approval_timeout_seconds: int = 300

        # ── Security handler synchronization primitives (BUG_0029 Phase 2) ──
        # RLock so a nested security check from the same thread re-enters safely.
        self.security_check_lock = threading.RLock()
        # ResettableRLock: recovers from a leaked lock left by a killed daemon thread.
        self.security_execution_lock = ResettableRLock()
        self._active_checks: set = set()
        self._active_checks_lock = threading.Lock()

    # ── Writers (the ONLY mutation paths) ───────────────────────────────────

    def _assign_auto_security(self, value: bool) -> None:
        """Assignment channel for auto_security — NO persistence.

        Used by the pool's ``__init__`` seeding and the config-persist load path,
        where a disk write would be wrong (the value just came from disk). The
        toggle API is :meth:`set_auto_security`, which also persists.
        """
        self.auto_security = bool(value)

    def set_auto_security(self, enabled: bool) -> None:
        """Toggle channel for auto_security (UI/WS/REST writers route here).

        Persistence is NOT done here — the pool's ``set_auto_security`` wraps this
        with change-detection + ``_save_pool_settings()`` so a disk write happens
        exactly once per real change.
        """
        self.auto_security = bool(enabled)

    def set_compression_fraction(self, pct: float) -> None:
        """Single writer for the compression fraction.

        Takes a **percentage** (the UI/JSON contract, e.g. ``70`` → ``0.7``), clamps
        via :func:`clamp_compression_fraction`, and does NOT persist — the pool's
        save path reads this value at save time.
        """
        self.compression_fraction = clamp_compression_fraction(float(pct) / 100.0)

    def set_afk(self, enabled: bool, message=None) -> None:
        """Set the server-backed AFK flag (shared by WebUI and Telegram bridge).

        ``message`` is optional; when absent the stored afk_message is left untouched
        so a toggle-only caller (e.g. Telegram /afk) cannot clobber a UI-set message.
        Unrelated to the time-based approval timeout (enable_timeout).
        """
        self.afk_enabled = bool(enabled)
        if message is not None:
            self.afk_message = str(message)

    def set_enable_timeout(self, enabled) -> None:
        """Enable or disable approval timeout."""
        self.enable_timeout = bool(enabled)

    def set_approval_timeout(self, seconds) -> None:
        """Set the approval timeout duration in seconds (clamped 10s–2h)."""
        self.approval_timeout_seconds = max(10, min(int(seconds), 7200))


# Process-level singleton — constructed at import time, before any pool/OM/app exists.
state = RuntimeState()
