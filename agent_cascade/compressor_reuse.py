"""Process-level claim registry for Compressor warm instance reuse (BUG_0330).

Mirrors agent_cascade/security_reuse.py for the Compressor rollout: a single *warm*
``Compressor_warm`` instance (and one warm ``CompressorReviewer_warm`` reviewer) is reused
across L1 compressions / L2 consolidations / compaction reviews so the stable
conversation[0] prefix stays byte-identical and hits the server-side KV cache, instead of
spawning a fresh ``Compressor_<n>`` / ``CompressionReviewer_<n>`` per op.

The one safety property that makes reuse correct is **exactly one owner at a time**:
* :func:`try_claim` — atomically take ownership of an instance name for one op. Returns
  ``False`` if another op already owns it (the caller then falls back to a fresh spawn).
* :func:`release_claim` — give ownership back. Only the owning rid can release (a wrong
  rid is a no-op, so a stale finally cannot free a claim that moved on).
* :func:`claim_holder` — debug/telemetry read of who currently owns a name.

Deliberately a SEPARATE registry from security_reuse (plan §8 item 4, RESOLVED): decouples
the Compressor rollout from Security, keeps Security's pinned tests untouched, and lets the
warm-session helpers live with their owner. The two registries never see the same name.

Design notes (inherited from the Security pattern):
  * Module-level state + RLock, NOT stashed on the pool — the pool can be rebuilt/reloaded
    mid-session and must never own this liveness signal.
  * The claim is taken by :meth:`ExecutionEngine._acquire_reusable_system_agent` (BEFORE any
    mutation of the warm instance) and released in the caller's ``finally`` (AFTER cleanup),
    so it covers the entire run including the LLM call.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

from agent_cascade.log import logger
from agent_cascade.settings import (
    COMPRESSOR_REUSE_ENABLED,
    COMPRESSOR_REUSE_MAX_REUSES,
    COMPRESSOR_REVIEWER_WARM_NAME,
    COMPRESSOR_WARM_NAME,
)

# Guards _CLAIMS. An RLock so a nested claim/release on the same name (e.g. a test helper)
# cannot self-deadlock; matches security_reuse.py's ownership style.
CLAIM_LOCK = threading.RLock()

# instance_name -> rid of the current owner. Process-global and tiny (at most two entries:
# the warm Compressor + the warm reviewer names). Never persisted.
_CLAIMS: Dict[str, str] = {}


def try_claim(name: str, rid: str) -> bool:
    """Attempt to claim ``name`` for op ``rid``.

    Returns ``True`` if this call now owns the name (it was free), ``False`` if another op
    already holds it. This is the single atomic gate that makes reuse safe — a caller MUST
    treat ``False`` as "fall back to a fresh spawn" and never mutate the warm instance in
    that case.

    Re-claiming an already-owned name (same or different rid) returns ``False``; claims are
    not reentrant by design because two concurrent ops must never share one instance — this
    is why the §3f re-interrogation continuation reuses the claim HELD by the session owner
    instead of re-acquiring per turn.
    """
    if not name:
        return False
    with CLAIM_LOCK:
        if name in _CLAIMS:
            return False
        _CLAIMS[name] = rid
        return True


def release_claim(name: str, rid: str) -> None:
    """Release ``name`` if (and only if) the current owner is ``rid``.

    A mismatched or absent rid is a no-op — this prevents a stale ``finally`` from freeing a
    claim that has already been re-taken by a later op (cross-release). Never raises.
    """
    if not name:
        return
    with CLAIM_LOCK:
        if _CLAIMS.get(name) == rid:
            del _CLAIMS[name]


def claim_holder(name: str) -> Optional[str]:
    """Return the rid currently owning ``name``, or ``None`` if it is free.

    Read-only; for logging/telemetry and tests (and the no-reset continuation guard). Not a
    gate — always use :func:`try_claim` to actually take ownership.
    """
    if not name:
        return None
    with CLAIM_LOCK:
        return _CLAIMS.get(name)


# instance_name -> warm reuses served since the last (re-)seed. Cleared on seed and on
# retirement. Only read/written while CLAIM_LOCK is held.
_REUSE_COUNTS: Dict[str, int] = {}


def _note_warm_reuse(engine: Any, name: str) -> None:
    """BUG_0330 Phase 3 re-mint: count one completed warm reuse. After
    COMPRESSOR_REUSE_MAX_REUSES consecutive reuses the warm instance is retired (dropped
    from pool.instances), so the next acquire re-seeds a brand-new one under the same
    fixed name — a bounded cap on prefix staleness / conversation drift.

    0 (the default) disables re-minting entirely. Retirement only drops the pool slot:
    the in-flight caller keeps its reference and still releases the claim in its own
    finally (rid-guarded), so a live claim is never freed here."""
    if COMPRESSOR_REUSE_MAX_REUSES <= 0:
        return
    with CLAIM_LOCK:
        n = _REUSE_COUNTS.get(name, 0) + 1
        if n < COMPRESSOR_REUSE_MAX_REUSES:
            _REUSE_COUNTS[name] = n
            return
        _REUSE_COUNTS.pop(name, None)
    try:
        instances = getattr(getattr(engine, 'pool', None), 'instances', None)
        if instances is not None:
            instances.pop(name, None)
        logger.info('[COMPRESSOR_REUSE] retired %s after %d reuses '
                    '(COMPRESSOR_REUSE_MAX_REUSES=%d) — next op re-seeds',
                    name, n, COMPRESSOR_REUSE_MAX_REUSES)
    except Exception as e:  # noqa: BLE001 — retirement bookkeeping is never fatal
        logger.warning('[COMPRESSOR_REUSE] retirement bookkeeping for %s failed: %s', name, e)


def _clear_all() -> None:
    """Test-only helper: drop every claim and reuse counter. Never called from production code."""
    with CLAIM_LOCK:
        _CLAIMS.clear()
        _REUSE_COUNTS.clear()


def acquire_compressor_agent(engine, agent_class: str, reuse_name: Optional[str],
                             fallback_name: str, task: str, caller: str,
                             rid: str) -> Tuple[Any, bool, str]:
    """Two-branch Compressor-instance acquire shared by the three compression call sites.

    Tries to warm-reuse a fixed-name instance via ``engine._acquire_reusable_system_agent``
    and falls back to a fresh per-op spawn via ``engine._create_system_agent`` when that
    misses. Returns ``(instance, was_reused, actual_name)`` where ``actual_name`` is the REAL
    instance name (the fixed warm name on a reuse/seed hit, the per-op fallback name on a
    miss) so the caller's finally targets the right object.

    This exists so the compression/consolidation/reviewer call sites do NOT each carry their
    own copy of the branch logic (single source of truth, mirrors acquire_security_agent).
    Reuse is best-effort: any exception from the warm path falls back to a fresh spawn.

    The shape guard accepts only the real engine's 3-tuple; anything else (incl. an auto-spec
    MagicMock from tests that mock ExecutionEngine without stubbing this method) is truthy but
    NOT our tuple → no reuse, fall back to a fresh spawn.
    """
    if reuse_name:
        try:
            got = engine._acquire_reusable_system_agent(
                agent_class=agent_class, instance_name=reuse_name, task=task, caller=caller, rid=rid)
        except Exception as _e:  # noqa: BLE001 — reuse is best-effort; never break the caller
            from agent_cascade.log import logger
            logger.warning('[COMPRESSOR_REUSE] acquire raised %s — falling back to fresh spawn', _e)
            got = None

        # Shape guard. Only the real 3-tuple is a hit (see docstring).
        if isinstance(got, tuple) and len(got) == 3 and got[0] is not None:
            inst, reused, actual = got[0], bool(got[1]), (got[2] or reuse_name)
            if reused:
                _note_warm_reuse(engine, actual)
            else:
                # A fresh seed under the fixed name starts the re-mint counter over.
                with CLAIM_LOCK:
                    _REUSE_COUNTS.pop(actual, None)
            return inst, reused, actual

    instance = engine._create_system_agent(
        agent_class=agent_class, instance_name=fallback_name, task=task, caller=caller)
    return instance, False, fallback_name


class WarmCompressorSession:
    """Owns one warm Compressor claim across a consolidation + all its review-retry correction
    turns (BUG_0330 §3f). The claim is held for the whole session lifetime — refine() never
    re-acquires (claims are non-reentrant; a per-turn re-acquire would wedge every correction
    into a fresh respawn that re-drops preserve_items). refine() is APPEND-ONLY so the instance
    carries system prefix + original task + the summary under review into every correction.
    """

    __slots__ = ('instance', 'actual_name', 'rid', 'engine', 'agent_pool', 'caller_name', '_released')

    def __init__(self, instance, actual_name: str, rid: str, engine, agent_pool, caller_name: str):
        self.instance = instance
        self.actual_name = actual_name
        self.rid = rid
        self.engine = engine
        self.agent_pool = agent_pool
        self.caller_name = caller_name
        self._released = False

    def refine(self, preserve_items):
        """Ask the same warm instance to re-issue the consolidated summary retaining
        ``preserve_items``. Returns ``(summary, caption)`` or None — None means the session is
        no longer usable (claim lost / instance not IDLE / execution error); the caller degrades
        to the byte-identical fresh-respawn path for this and any remaining retries."""
        if self._released:
            return None
        try:
            from agent_cascade.compression.agent_invoker import (
                _execute_compressor_and_extract_summary, _preserve_items_instruction,
                _reset_terminated_compressor_for_retry)
            from agent_cascade.log import logger
            from agent_cascade.prompts.dna import END_MARKER_INSTRUCTION

            # Correction task reuses the exact single-source preserve wording so the fresh-respawn
            # and warm-correction prompts carry the same instruction, plus the end-marker contract
            # so the output parser applies unchanged (§3f F-3).
            correction_task = ('Your previous consolidated summary DROPPED required items. Re-issue '
                               'the consolidated summary now, correcting that loss.'
                               + _preserve_items_instruction(preserve_items) + ' ' + END_MARKER_INSTRUCTION)
            # A run that ended on max_turns leaves TERMINATED; engine.run() would trip the L1 race
            # guard. No-op unless TERMINATED (same helper as the compression retry path).
            _reset_terminated_compressor_for_retry(self.agent_pool, self.instance, self.actual_name)
            if self.engine._continue_reusable_system_agent_turn(
                    self.instance, correction_task, self.caller_name, rid=self.rid) is None:
                logger.info('[COMPRESSOR_REUSE] warm refine unavailable for %s — degrading to fresh respawn',
                            self.actual_name)
                return None
            return _execute_compressor_and_extract_summary(
                self.agent_pool, self.engine, self.instance, self.actual_name, self.caller_name,
                timeout_label='Consolidation')
        except Exception as e:  # noqa: BLE001 — warm path is best-effort; degrade, never escalate
            from agent_cascade.log import logger
            logger.warning('[COMPRESSOR_REUSE] warm refine failed for %s: %s — degrading to fresh respawn',
                           self.actual_name, e)
            return None

    def release(self) -> None:
        """Give the warm claim back. Idempotent — safe to call unconditionally in a finally."""
        if not self._released:
            self._released = True
            release_claim(self.actual_name, self.rid)


def acquire_warm_consolidation(agent_pool, marker_summaries, caller_name: str,
                               marker_time_ranges=None):
    """Open a warm consolidation session (BUG_0330 §3f), or None to signal "run today's path".

    Returns None when reuse is OFF (claim registry never touched), when the warm acquire misses
    (claim held / not seeded / ineligible), or on any error — in every case the caller's
    byte-identical fresh-spawn path proceeds. Unlike acquire_compressor_agent there is NO fresh
    spawn fallback here: an un-ran orphan fresh instance would leak. On success the returned
    session already holds the claim and the consolidation task is already appended as the warm
    instance's conversation task message, so the first run is a plain engine.run() underneath.
    """
    if not COMPRESSOR_REUSE_ENABLED:
        return None
    rid = ''
    try:
        import uuid
        from agent_cascade.execution_engine import ExecutionEngine
        from agent_cascade.compression.agent_invoker import _build_consolidation_prompt
        rid = uuid.uuid4().hex
        prompt = _build_consolidation_prompt(marker_summaries, marker_time_ranges)
        engine = ExecutionEngine(agent_pool)
        got = engine._acquire_reusable_system_agent(
            agent_class='Compressor', instance_name=COMPRESSOR_WARM_NAME,
            task=prompt, caller=caller_name, rid=rid)
        if not (isinstance(got, tuple) and len(got) == 3 and got[0] is not None):
            return None
        inst, _reused, actual_name = got
        if _reused:
            _note_warm_reuse(engine, actual_name)
        return WarmCompressorSession(inst, actual_name, rid, engine, agent_pool, caller_name)
    except Exception as e:  # noqa: BLE001 — warm path is best-effort
        # If the engine acquired (claim HELD) and a later step threw, the claim would
        # otherwise wedge the warm name for the process lifetime — no session object
        # exists, so no release() will ever fire. rid-guarded no-op if never acquired.
        release_claim(COMPRESSOR_WARM_NAME, rid)
        logger.warning('[COMPRESSOR_REUSE] warm consolidation session unavailable: %s', e)
        return None
