"""Why a warm system-agent reuse acquire missed — and whether it is worth waiting on.

``ExecutionEngine._acquire_reusable_system_agent`` runs a sequence of eligibility gates and
returns ``None`` on every miss. A single ``None`` cannot distinguish "wait a moment and retry
the warm instance" (the instance is mid-run / still draining / a lock is busy) from "give up
and fresh-spawn now" (halted / terminated / unfrozen prompt / wrong class). This module gives
the shared gate a machine-readable reason so callers (the Compressor bounded claim-wait in
``compressor_reuse.acquire_warm_consolidation``) can retry the transient misses instead of
silently losing the warm KV-cache win, and stop early on the permanent ones.

Reasons map 1:1 to the gate that rejected (the gate key appears in the gate's debug log).
The shared gate is also used by the Security reuse path, whose signature/return type must not
change — the reason is delivered through an optional out-parameter sink, so Security callers
that pass nothing are unaffected.
"""

from __future__ import annotations

from enum import Enum


class ReuseRejectReason(Enum):
    # Transient — clear on their own; bounded-wait and retry.
    CLAIM_HELD = 'claim_held'                     # gate 1: another op owns the name
    STATE_NOT_IDLE = 'state_not_idle'             # gate 3: a run is finishing
    SLOT_PERMIT_LEAKED = 'slot_permit_leaked'     # gate 4: discard of a leaked permit failed
    LIVE_LLM_STREAM = 'live_llm_stream'           # gate 5: last stream's reader still in flight
    LLM_RECENT_ACTIVITY = 'llm_recent_activity'   # gate 5b: below the quiescence epsilon
    QUEUED_MESSAGES = 'queued_messages'           # gate 9: pending messages still draining
    ON_ACTIVE_STACK = 'on_active_stack'           # gate 11: instance on the execution stack
    COMPRESSION_LOCK_BUSY = 'compression_lock_busy'  # gate 10: compressor/rollback holds the lock
    # Permanent — only a fresh seed can fix them; fall back to fresh spawn now.
    CLASS_MISMATCH = 'class_mismatch'             # gate 2: a different class owns the name
    HALTED = 'halted'                             # gate 6
    TERMINATED_POOL = 'terminated_pool'           # gate 7: pool-level termination set
    TERMINATED_FLAG = 'terminated_flag'           # gate 7b: instance flag is_terminated
    BAD_CONVERSATION_HEAD = 'bad_conversation_head'  # gate 8: empty or head not SYSTEM
    PROMPT_NOT_FROZEN = 'prompt_not_frozen'       # gate 8b: needs a (re-)seed to freeze
    SEED_FAILED = 'seed_failed'                   # the seed-on-miss create raised
    ACQUIRE_EXCEPTION = 'acquire_exception'       # an unexpected error in the predicate


# Exactly the reasons worth bounded-waiting on. Membership (not "not permanent") is the test,
# so a NEW reason added later is fail-closed (NOT transient) until deliberately added here —
# an accidental omission can only cost a fresh spawn, never wedge a caller in a pointless wait.
TRANSIENT_REASONS = frozenset({
    ReuseRejectReason.CLAIM_HELD,
    ReuseRejectReason.STATE_NOT_IDLE,
    ReuseRejectReason.SLOT_PERMIT_LEAKED,
    ReuseRejectReason.LIVE_LLM_STREAM,
    ReuseRejectReason.LLM_RECENT_ACTIVITY,
    ReuseRejectReason.QUEUED_MESSAGES,
    ReuseRejectReason.ON_ACTIVE_STACK,
    ReuseRejectReason.COMPRESSION_LOCK_BUSY,
})


def is_transient(reason) -> bool:
    """True only for a known-transient ``ReuseRejectReason``.

    Unknown values (``None`` when no reason was recorded, a permanent reason, or anything
    not in ``TRANSIENT_REASONS``) are False — fail-closed so callers never wait on a
    permanent condition or on an unset sink.
    """
    return reason in TRANSIENT_REASONS
