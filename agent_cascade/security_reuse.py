"""Process-level claim registry for Security agent instance reuse.

BUG_0029 Phase 3 (Security instance reuse): a single *warm* ``Security`` instance is
reused across shell_cmd / skill-advisor security checks so the LLM prompt prefix stays
byte-identical and hits the server-side KV cache, instead of spawning a fresh
``Security_<rid>`` per check.

The one safety property that makes reuse correct is **exactly one owner at a time**. If
two checks both tried to reset the same warm instance, they would corrupt each other's
conversation (or trip the engine L1 state guard). This module provides a tiny,
dependency-free claim registry mirroring the ``runtime_state.py`` single-owner pattern:

* :func:`try_claim` — atomically take ownership of an instance name for a check. Returns
  ``False`` if another check already owns it (the caller then falls back to a fresh spawn).
* :func:`release_claim` — give ownership back. Only the owning rid can release (a wrong
  rid is a no-op, so a stale finally cannot free a claim that moved on).
* :func:`claim_holder` — debug/telemetry read of who currently owns a name.

Design notes (plan §5.2):
  * Module-level state + RLock, NOT stashed on the pool — the pool can be rebuilt/reloaded
    mid-session and must never own this liveness signal.
  * The claim is taken by :meth:`ExecutionEngine._acquire_reusable_system_agent` (BEFORE any
    mutation of the warm instance) and released in the caller's ``finally`` (AFTER
    ``_cleanup()``), so it covers the entire run including the LLM call.
  * Two distinct names are used — one per path — because the skill-advisor path holds no
    lock (plan §3.3). Sharing one object across both paths would put a lock-free concurrent
    run behind only the L1 guard, which raises rather than blocks.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

# Guards _CLAIMS. An RLock so a nested claim/release on the same name (e.g. a test helper)
# cannot self-deadlock; matches the runtime_state.py ownership style.
CLAIM_LOCK = threading.RLock()

# instance_name -> rid of the current owner. The dict is process-global and tiny (at most
# two entries: the approval + skill-advisor warm names). Never persisted.
_CLAIMS: Dict[str, str] = {}


def try_claim(name: str, rid: str) -> bool:
    """Attempt to claim ``name`` for check ``rid``.

    Returns ``True`` if this call now owns the name (it was free), ``False`` if another
    check already holds it. This is the single atomic gate that makes reuse safe — a
    caller MUST treat ``False`` as "fall back to a fresh spawn" and never mutate the warm
    instance in that case.

    Re-claiming an already-owned name (same or different rid) returns ``False``; claims are
    not reentrant by design because two concurrent checks must never share one instance.
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

    A mismatched or absent rid is a no-op — this prevents a stale ``finally`` from freeing
    a claim that has already been re-taken by a later check (cross-release). Never raises.
    """
    if not name:
        return
    with CLAIM_LOCK:
        if _CLAIMS.get(name) == rid:
            del _CLAIMS[name]


def claim_holder(name: str) -> Optional[str]:
    """Return the rid currently owning ``name``, or ``None`` if it is free.

    Read-only; for logging/telemetry and tests. Not a gate — always use :func:`try_claim`
    to actually take ownership.
    """
    if not name:
        return None
    with CLAIM_LOCK:
        return _CLAIMS.get(name)


def _clear_all() -> None:
    """Test-only helper: drop every claim. Never called from production code."""
    with CLAIM_LOCK:
        _CLAIMS.clear()


def acquire_security_agent(engine, agent_class: str, reuse_name: Optional[str],
                           fallback_name: str, task: str, caller: str,
                           rid: str) -> Tuple[Any, bool, str]:
    """Two-branch Security-instance acquire shared by the approval + skill-advisor paths.

    Tries to warm-reuse a fixed-name instance via ``engine._acquire_reusable_system_agent`` and
    falls back to a fresh per-check spawn via ``engine._create_system_agent`` when that misses.
    Returns ``(instance, was_reused, actual_name)`` where ``actual_name`` is the REAL instance name
    (the fixed reuse name on a reuse/seed hit, the per-call fallback name on a miss).

    This exists so the two call sites (security_handler.py / advisor_runner.py) do NOT each carry
    their own copy of the branch logic — which had already drifted. It is dependency-light: it only
    calls engine methods and never touches the claim registry directly (the engine owns that).
    Reuse is best-effort: any exception from the acquire path falls back to a fresh spawn.

    The shape guard accepts only the real engine's 3-tuple; anything else (incl. an auto-spec
    MagicMock from tests that mock ExecutionEngine without stubbing this method) is truthy but NOT
    our tuple → no reuse, fall back to a fresh spawn:
      * ``len==3`` → real engine ``(instance, is_reuse, name)``; a falsy name falls back to reuse_name.
    """
    if reuse_name:
        try:
            got = engine._acquire_reusable_system_agent(
                agent_class=agent_class, instance_name=reuse_name, task=task, caller=caller, rid=rid)
        except Exception as _e:  # noqa: BLE001 — reuse is best-effort; never break the caller
            from agent_cascade.log import logger
            logger.warning('[SECURITY_REUSE] acquire raised %s — falling back to fresh spawn', _e)
            got = None

        # Shape guard. Only the real 3-tuple is a hit (see docstring).
        if isinstance(got, tuple) and len(got) == 3 and got[0] is not None:
            return got[0], bool(got[1]), (got[2] or reuse_name)

    instance = engine._create_system_agent(
        agent_class=agent_class, instance_name=fallback_name, task=task, caller=caller)
    return instance, False, fallback_name
