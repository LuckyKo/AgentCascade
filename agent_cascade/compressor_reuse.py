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


def _clear_all() -> None:
    """Test-only helper: drop every claim. Never called from production code."""
    with CLAIM_LOCK:
        _CLAIMS.clear()


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
            return got[0], bool(got[1]), (got[2] or reuse_name)

    instance = engine._create_system_agent(
        agent_class=agent_class, instance_name=fallback_name, task=task, caller=caller)
    return instance, False, fallback_name
