"""Supervisor that runs the Telegram bridge as an in-process daemon thread.

Phase 3 of the AC Telegram bridge integration. The bridge (PTB long-polling
app) now runs inside AC's own process as a **daemon thread** instead of a
separate supervised child process. This eliminates the duplicate-bridge-on-
restart bug by construction: when AC restarts via ``os._exit(0)``, the daemon
thread dies with the process — there is nothing to orphan.

Lifecycle (driven by the UI toggle, ``PoolSettings.telegram_bridge_enabled``):

- ``start()`` / ``set_enabled(True)`` spawns one daemon thread (``tg-bridge``).
  The thread body builds a fresh PTB Application + ACClient per attempt and
  runs ``app.run_polling(stop_signals=None)``.
- ``stop()`` / ``set_enabled(False)`` schedules ``app.stop()`` onto the bridge
  loop via ``asyncio.run_coroutine_threadsafe`` (NOT ``app.stop_running()``,
  which raises RuntimeError off-loop), then joins the thread with a bounded
  timeout. If the thread is still alive after the timeout we log a warning and
  move on — it's daemon, so it dies at interpreter exit anyway.
- Crash containment: exceptions from ``run_polling`` are contained in the
  thread; the body applies a **bounded auto-restart with exponential backoff**
  (max ``max_restart_attempts`` consecutive failures). A config-class failure
  (``validate_config`` problems — missing token / empty allowlist) is a
  PERMANENT stop: no retry until the toggle is re-enabled. A clean return from
  ``run_polling`` means "someone stopped it" -> no restart either; the restart
  debt resets on any clean run.

The bot token is NEVER passed via env or argv: config loading reads it itself
from ``config/secrets.json`` (the thread's CWD is the repo root).

All state is guarded by a lock: the bridge thread and event-loop callers
(config handler, startup/shutdown hooks) both touch it.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Optional

from agent_cascade.log import logger


# Bounded-restart policy for the in-thread restart loop. The backoff is
# exponential with a ceiling; after MAX_RESTART_ATTEMPTS total consecutive
# failed starts (no clean run in between) the supervisor gives up and requires
# the user to toggle off->on to retry. This is what prevents an infinite restart
# loop when, e.g., the token is missing or the AC port is wrong.
DEFAULT_BACKOFF_BASE = 1.0      # seconds; delay for attempt n = base * 2**(n-1)
DEFAULT_BACKOFF_CAP = 30.0      # seconds; ceiling for any single backoff delay
DEFAULT_MAX_RESTART_ATTEMPTS = 5   # TOTAL attempts (initial + retries)
DEFAULT_STOP_JOIN_TIMEOUT_SEC = 5.0   # bounded join window after scheduling PTB stop


def _build_app(cfg, ac):
    """Module-level seam around ``bot.build_application`` (tests patch this)."""
    from .bot import build_application
    return build_application(cfg, ac)


class TelegramBridgeSupervisor:
    """Run/own the Telegram bridge in-process (daemon thread) on behalf of AC.

    Attached to ``agent_pool`` as ``agent_pool.telegram_supervisor`` by
    ``create_app()`` (single source of truth — every launcher gets it). The AC
    base URL can be given explicitly, or left empty and resolved lazily at
    start time from ``agent_pool.server_info`` (the ACTUAL bound port, set by
    the launcher right before ``server.run()``) when attached before uvicorn
    binds.

    Base URL resolution precedence (see ``_resolve_base_url``):
      1. ``ac_base_url`` if non-empty (trailing slash stripped) — used as-is.
      2. Otherwise ``agent_pool.server_info`` (host ignored; the bridge is
         always local, so the host is forced to 127.0.0.1 and only the port
         is taken).
      3. Otherwise the ``AGENT_CASCADE_PORT`` env var.
      4. Otherwise the default ``http://127.0.0.1:8765``.
    """

    def __init__(self,
                 ac_base_url: str = '',
                 agent_pool=None,
                 project_root: Optional[Path] = None,
                 workspace_dir: Optional[str] = None,
                 allowed_users: str = '',
                 target_agent: str = 'Maine',
                 backoff_base: float = DEFAULT_BACKOFF_BASE,
                 backoff_cap: float = DEFAULT_BACKOFF_CAP,
                 max_restart_attempts: int = DEFAULT_MAX_RESTART_ATTEMPTS,
                 stop_join_timeout_sec: float = DEFAULT_STOP_JOIN_TIMEOUT_SEC):
        self.ac_base_url = (ac_base_url or '').rstrip('/')
        # Lazily-resolved base URL source. When the supervisor is attached from
        # create_app() (before uvicorn binds), ac_base_url is empty and we resolve
        # the ACTUAL bound port from agent_pool.server_info at start time (set by
        # the launcher right before server.run()).
        self._agent_pool = agent_pool
        # CWD for config/secrets.json resolution (kept for parity with the old
        # child process, whose CWD was the repo root).
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent.parent
        ws = Path(workspace_dir) if workspace_dir else self.project_root / 'AgentWorkspace'
        self._log_dir = ws / 'logs'
        self.allowed_users = allowed_users or ''
        self.target_agent = target_agent or 'Maine'

        # Bounded-restart knobs (exposed so tests can shrink them).
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.max_restart_attempts = int(max_restart_attempts)
        self.stop_join_timeout_sec = float(stop_join_timeout_sec)

        # Runtime state (all accessed under self._lock).
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._app = None                     # current PTB Application (stop target)
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # bridge thread's loop
        self._enabled = False          # toggle intent (drives restart decisions)
        self._stopping = False         # True while an explicit stop() is in flight
        self._error: str = ''

    # ── Public API ────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """True if a live bridge thread is currently managed."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        """Snapshot of supervisor state (for status reporting / debugging)."""
        with self._lock:
            return {
                'enabled': self._enabled,
                'running': self.is_running(),
                'error': self._error,
            }

    def set_enabled(self, enabled: bool) -> None:
        """Idempotent start/stop entry point driven by the UI toggle.

        Because the UI sends a full settings snapshot on every save, this fires
        on every save — not only on flips. So it is a strict no-op when already
        in the requested state (no double-start on repeated "on"; safe no-op on
        "off" when nothing is running).

        NOTE: the stop path must NOT hold self._lock while _stop_thread joins,
        because the thread body needs that lock on its exit path. We therefore
        release the lock before calling _stop_thread (which manages its own).
        """
        enabled = bool(enabled)
        with self._lock:
            if enabled and not self._enabled:
                self._enabled = True
                self._error = ''
                self._stopping = False       # clear any stale stop flag from a prior stop()
                self._spawn_thread()          # safe under lock (no join)
                return
            elif not enabled and self._enabled:
                self._enabled = False
                self._stopping = True
            else:
                # Already in the requested state — no-op.
                return
        # Stop path: _stop_thread handles its own locking (must not hold lock during join).
        self._stop_thread()

    def start(self) -> None:
        """Start the bridge (idempotent — no double-start if already running).

        If a previous thread is still shutting down (slow stop), this does a
        bounded wait-then-spawn so we never have two concurrent bridge threads
        holding the bot token. The join here is done WITHOUT holding self._lock
        (same deadlock constraint as _stop_thread).
        """
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            # Either already running, or a slow stop in flight — wait it out
            # (bounded) so we never double-start while the old loop still holds
            # the bot token. Join WITHOUT the lock (thread body needs it on exit).
            deadline = time.monotonic() + self.stop_join_timeout_sec
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(timeout=0.1)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.warning(
                    '[TelegramBridge] start() skipped: previous bridge thread still '
                    'alive after %.1fs (refusing to double-start)',
                    self.stop_join_timeout_sec,
                )
                return
            self._enabled = True
            self._error = ''
            self._stopping = False
            self._spawn_thread()

    def stop(self) -> None:
        """Stop the bridge (idempotent — safe no-op when nothing is running)."""
        with self._lock:
            self._enabled = False
            self._stopping = True
        self._stop_thread()   # manages its own locking; must not hold lock during join

    # ── Thread spawn / stop internals (caller holds self._lock) ───────────

    def _resolve_base_url(self) -> str:
        """Return the base URL to hand the in-process client, preferring an explicit ac_base_url.

        When constructed without one (create_app attach path), resolve from
        agent_pool.server_info (set by the launcher before server.run()); fall back to
        AGENT_CASCADE_PORT env, then default 8765. Mirrors system_info.py resolution.
        """
        if self.ac_base_url:
            return self.ac_base_url
        # The bridge client is ALWAYS local to AC, so the URL must always use
        # 127.0.0.1 — never the bind host from server_info (a launcher may bind to
        # 0.0.0.0 for LAN access; 0.0.0.0 is a bind address, not a valid connect target).
        si = getattr(self._agent_pool, 'server_info', None) if self._agent_pool else None
        port = 8765
        if isinstance(si, (tuple, list)) and len(si) == 2 and si[0] and si[1]:
            try:
                port = int(si[1])
            except (ValueError, TypeError):
                port = 8765
        else:
            env_port = os.getenv('AGENT_CASCADE_PORT')
            try:
                port = int(env_port) if env_port is not None else 8765
            except (ValueError, TypeError):
                port = 8765
        return f'http://127.0.0.1:{port}'

    def _spawn_thread(self) -> None:
        """Spawn the daemon bridge thread. Caller must hold self._lock."""
        if self._thread is not None and self._thread.is_alive():
            logger.debug('[TelegramBridge] start() no-op: already running')
            return
        thread = threading.Thread(target=self._run_loop, name='tg-bridge', daemon=True)
        # Tag with owner so test fixtures can find the supervisor for cleanup.
        thread._owner_supervisor = self
        self._thread = thread
        thread.start()
        logger.info('[TelegramBridge] Started bridge thread base_url=%s', self._resolve_base_url())

    def _stop_thread(self) -> None:
        """Schedule PTB stop on the bridge loop and join (bounded).

        IMPORTANT: this method must NOT hold self._lock while joining. The thread
        body takes self._lock on its exit path; if we held it during join(), the
        thread would deadlock waiting for us to release — and the join would time
        out every time. So we snapshot what we need under the lock, release it,
        then schedule + join outside.

        The crux of the in-process design: ``app.stop_running()`` calls
        ``asyncio.get_running_loop().stop()`` and raises RuntimeError when called
        off-loop, so we must schedule ``app.stop()`` onto the bridge loop instead.
        Awaiting it resolves the coroutine ``run_forever`` is waiting on; PTB then
        runs its full graceful teardown (updater stopped -> post_shutdown cancels
        waiters + closes the AC client) and ``run_polling`` returns.
        """
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                # Nothing alive to stop — clear state and return (idempotent no-op).
                self._thread = None
                self._app = None
                self._loop = None
                return
            app, loop = self._app, self._loop

        if app is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(app.stop(), loop)
                logger.debug('[TelegramBridge] PTB stop scheduled on bridge loop')
            except Exception as e:  # pragma: no cover - defensive
                logger.warning('[TelegramBridge] scheduling PTB stop failed: %s', e)

        try:
            thread.join(timeout=self.stop_join_timeout_sec)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning('[TelegramBridge] join error: %s', e)

        with self._lock:
            if thread.is_alive():
                logger.warning(
                    '[TelegramBridge] bridge thread still alive after %.1fs; leaving it to '
                    'interpreter exit (daemon)', self.stop_join_timeout_sec,
                )
                self._error = f'bridge stop timed out after {self.stop_join_timeout_sec:.1f}s'
            else:
                logger.info('[TelegramBridge] Stopped bridge thread')
                # Clear state once the thread is gone.
                if self._thread is thread:
                    self._thread = None
                    self._app = None
                    self._loop = None

    # ── Thread body / restart policy ──────────────────────────────────────

    def _run_loop(self) -> None:
        """Daemon-thread body: run the bridge with a bounded auto-restart loop.

        All failures are contained here — nothing in this thread can take down AC.
        Restart policy (per plan §2.3):
          - config-class failure (validate_config problems) -> PERMANENT stop, no retry
          - clean return from run_polling -> no restart (someone stopped it); resets debt
          - other exception -> bounded restart with exponential backoff; after
            max_restart_attempts consecutive failures, give up with a clear _error.
        """
        # Make config/secrets.json resolvable regardless of how AC was launched.
        try:
            os.chdir(str(self.project_root))
        except Exception as e:  # pragma: no cover - CWD edge cases
            logger.debug('[TelegramBridge] Failed to chdir to %s (continuing): %s',
                         self.project_root, e)

        attempts = 0
        while True:
            # Import inside the loop so tests can patch these at their source.
            from .ac_client import ACClient
            from .config import load_config, validate_config

            cfg = None
            app = None
            try:
                # The in-process client reads its config from the SAME process env +
                # config/secrets.json (CWD = repo root). TG_BRIDGE_ENABLED is forced on
                # because the toggle IS the master switch here.
                os.environ['TG_BRIDGE_ENABLED'] = 'true'
                cfg = load_config()
                problems = validate_config(cfg)
                if problems:
                    # Config-class failure (missing token / empty ALLOWED_USERS):
                    # permanent stop — surface the problem, no retry until re-toggled.
                    self._set_error(
                        'Telegram bridge config problem: ' + '; '.join(problems) +
                        ' Fix it, then re-enable the toggle to retry.'
                    )
                    logger.error('[TelegramBridge] %s', self._error)
                    break

                # Fresh client per attempt. Do NOT pre-open: _request auto-opens on
                # the PTB loop for the first real request, and bot.py's post_shutdown
                # closes it on the same loop — no cross-loop fds.
                ac = ACClient(base_url=self._resolve_base_url(), target_agent=self.target_agent)
                app = _build_app(cfg, ac)

                # Publish the app under lock BEFORE run_polling so a concurrent
                # stop() can find it to schedule app.stop(). The loop is created by
                # run_polling itself; we capture it via a post_init hook (runs inside
                # PTB's loop, right after initialize) so stop() always has a valid
                # target. For the brief window before post_init fires there is nothing
                # to stop — run_polling returns immediately on failure.
                with self._lock:
                    self._app = app
                    self._loop = None

                async def _publish_loop(_app=app):
                    # Runs on the bridge loop (PTB post_init). Publish the loop so a
                    # concurrent stop() can schedule app.stop() onto it.
                    with self._lock:
                        if self._app is _app:
                            self._loop = asyncio.get_running_loop()

                try:
                    # PTB Application.post_init is a settable property (v22.x) — assign,
                    # don't call. Fakes that model it as a method are handled below.
                    app.post_init = _publish_loop
                except AttributeError:  # pragma: no cover - defensive (non-PTB fakes)
                    try:
                        app.post_init(_publish_loop)
                    except Exception as e:
                        logger.debug('[TelegramBridge] post_init hook not supported: %s', e)

                logger.info('[TelegramBridge] Bridge starting (attempt %d)', attempts + 1)
                app.run_polling(
                    allowed_updates=['message'],
                    drop_pending_updates=False,
                    stop_signals=None,   # portable across Windows/POSIX; AC drives shutdown
                )
                # Clean return -> someone stopped it (or PTB exited cleanly). No restart.
                logger.info('[TelegramBridge] Bridge stopped cleanly')
                break

            except Exception as e:
                with self._lock:
                    stopping = self._stopping
                    enabled = self._enabled

                if stopping or not enabled:
                    # Explicit stop raced the failure — do not restart.
                    logger.info('[TelegramBridge] Bridge run ended (stop in progress); not restarting')
                    break

                # max_restart_attempts = TOTAL number of attempts (initial + retries).
                # Once we've exhausted the budget, give up with a clear error.
                if attempts + 1 >= self.max_restart_attempts:
                    self._set_error(
                        f'Telegram bridge failed to start {attempts + 1} times in a row '
                        f'(last error: {e}); giving up to avoid a restart loop. '
                        'Check AC reachability / logs, then re-enable the toggle to retry.'
                    )
                    logger.error('[TelegramBridge] %s', self._error)
                    break

                attempts += 1
                delay = min(self.backoff_cap, self.backoff_base * (2 ** (attempts - 1)))
                logger.warning(
                    '[TelegramBridge] Bridge run failed; retrying (attempt %d/%d): %s in %.1fs',
                    attempts, self.max_restart_attempts, e, delay,
                )
                if delay > 0:
                    time.sleep(delay)
            finally:
                # Drop the published app reference once this attempt is over.
                with self._lock:
                    if self._app is app:
                        self._app = None
                        self._loop = None

        # Thread exiting: clear any stale references (the thread object itself is
        # only cleared by _stop_thread when it observes the join complete).
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._app = None
                self._loop = None

    def _set_error(self, msg: str) -> None:
        """Record an error message (caller may or may not hold the lock)."""
        with self._lock:
            self._error = msg
