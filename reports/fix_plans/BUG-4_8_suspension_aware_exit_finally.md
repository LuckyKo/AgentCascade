# FIX PLAN — BUG-4 + BUG-8: Suspension-aware exit finally (keep wakeups; SLEEPING over IDLE when work outstanding)

Part of [fix_plan_endpoint_slot_deadlock.md](../fix_plan_endpoint_slot_deadlock.md).

## Problem recap

`run()`'s generic exit finally (`engine/core.py:820–897`) is unconditional:

- **BUG-8:** `_async_registry.clear_pending(inst_name)` (core.py:825–829) and `pool.drain_queue(inst_name)` (core.py:831–835) run on EVERY exit. If the turn loop breaks out because of a suspension-related race, pending async-child registrations are cancelled and queued messages (wakeups!) are silently discarded.
- **BUG-4:** any of {RUNNING, SLEEPING, COMPLETING} → IDLE at core.py:886–893, with no compression-suspension awareness. An agent that still has outstanding work exits as IDLE.

Why IDLE-instead-of-SLEEPING matters (and why it doesn't matter more): the idle checker skips SLEEPING agents (`pool/idle_manager.py:123–124`) but will auto-dismiss IDLE ones after their timeout (1600s regular / 60s system agents). Message-response behavior is otherwise identical — per user clarification (2026-08-21): *agents wake from any message in the queue whether IDLE or SLEEPING; SLEEPING exists to prevent idle-agent dismissal*. So this fix adds NO new wakeup machinery. Its job is narrow:

1. Don't destroy queued wakeups / pending async registrations on a suspension-driven exit.
2. Prefer SLEEPING over IDLE when exiting with outstanding work, so the waiter isn't dismissed while its work is still pending.

Trigger paths for a suspension-driven break (from investigation §5 BUG-4): post-resume loop completion via non-suspension branches (`_post_turn_checks` returning False through pure-thinking detection etc.), or generator abandonment via `gen.close()` unwinding into the same finally. With BUG-1 fixed these become rare — this plan is defense-in-depth.

## Change description (pseudo-code level)

### Marker: record that this run saw compression-halt

**`AgentInstance` dataclass** (next to slot fields, `agent_instance.py:~284`):

```python
_compression_suspended_at: float = 0.0   # monotonic timestamp of last compression-halt wait entry
```

Set it inside `_wait_for_compression_to_clear` on entry (same method BUG-1 edits — one extra line), reset at `run()` entry next to the existing per-run init block (core.py:448–449):

```python
# in _wait_for_compression_to_clear, before the release:
instance._compression_suspended_at = time.monotonic()

# in run(), alongside "instance._slot_release = None":
instance._compression_suspended_at = 0.0
```

### Exit finally: suspension-aware preservation + state choice

Restructure core.py:824–835 and 886–893 minimally:

```python
finally:
    inst_name = instance.instance_name
    suspended_this_run = getattr(instance, '_compression_suspended_at', 0.0) > 0.0
    terminated = self._is_terminal_stop(inst_name)
    outstanding = self.pool.has_pending(inst_name) or self.pool.has_messages(inst_name)

    # BUG-8 FIX: preserve wakeups ONLY when a suspension-driven exit left real
    # work behind. Normal completions and terminal stops behave exactly as before.
    preserve = suspended_this_run and not terminated and outstanding

    if not preserve:
        # existing cleanup, unchanged
        if hasattr(self.pool, '_async_registry'):
            try: self.pool._async_registry.clear_pending(inst_name)
            except Exception: pass
        if hasattr(self.pool, 'drain_queue'):
            try: self.pool.drain_queue(inst_name)
            except Exception: pass

    ... (unchanged continue_saved_msg cleanup, _release_slot, final log sync) ...

    with instance._state_lock:
        current_state = instance.state
        if current_state in (AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING):
            self.pool._mark_activity(inst_name)
            # BUG-4 FIX: exiting with preserved outstanding work → SLEEPING
            # (idle-checker protected). Everything else → IDLE as before.
            target = AgentState.SLEEPING if preserve else AgentState.IDLE
            instance._transition(target)
            instance.sleeping_since = time.monotonic() if preserve else instance.sleeping_since
            logger.debug("EXIT - %s %s→%s%s", inst_name, current_state.name,
                         target.name, " [suspension-preserved]" if preserve else "")
```

How preserved state gets consumed afterward (existing machinery only, per user constraint):
- Root/main agent: any new user message restarts generation (`ws_handlers.handle_message` starts a thread when not generating); the Resume button likewise (`ws_handlers.py ~460`). Queued messages survive because we skipped the drain.
- Sub-agents: async children complete via `AsyncToolRegistry._execute` → `enqueue_message(parent, ...)` (`async_tools.py:216–228`) — results land in the intact queue instead of vanishing.
- The SLEEPING label additionally keeps `IdleManager` from dismissing the waiter while that work sits pending.

## Edge cases considered

| Case | Handling |
|---|---|
| Normal completion after having been suspended earlier in the run | `outstanding` is False once work drained → `preserve=False` → identical to today's behavior (clear+drain+IDLE) |
| Terminal stop during suspension wait (`_wait_for_compression_to_clear` returns False) | `terminated=True` → `preserve=False`; termination path owns cleanup; no zombie SLEEPING instances |
| Instance already TERMINATED at exit | Falls into the existing TERMINATED branch (core.py:894–895), untouched |
| Pending exists but nothing was ever suspended | `preserve=False` → today's semantics (this is the pre-existing early-exit path, investigation BUG-4c; deliberately out of scope) |
| Queue holds stale junk from long-dead flows | Unchanged risk profile: drain still happens on all normal/terminal exits; only suspension-driven exits keep items — which are by definition recent wakeups |
| Dismissal later removes an agent with preserved queue entries | Same as today's dismissal of any agent with queued messages (`message_queues` keyed by name; terminate path cleans up tickets/pending) |
| `gen.close()` abandonment mid-suspension (invoker timeout path) | Unwinds into the same finally → same logic applies; compressor rarely has pending work, so mostly a no-op |
| State-lock discipline | Decision reads pool state outside `_state_lock`, transitions under it exactly like current code; `has_pending`/`has_messages` are lock-protected internally |
| Double transition guard | `_transition` validates from-state as before; RUNNING/SLEEPING/COMPLETING gate unchanged |

## Why minimal / safe

- Two booleans and one ternary at the exit site; one timestamp field; one marker line inside the method BUG-1 already touches. No new threads, events, conditions, or re-drive loops — explicitly none, per user clarification.
- Default behavior (no suspension seen) is byte-for-byte identical to today: the `not preserve` branch IS the current code.
- Worst-case failure of the new branch: an agent sits SLEEPING with intact queues instead of being dismissed — strictly better than losing wakeups, and recoverable through existing user-message/Resume flows.

## Files touched

- `agent_cascade/engine/core.py` — exit finally (~820–897) + marker reset at run entry (~449) + marker set in `_wait_for_compression_to_clear`
- `agent_cascade/agent_instance.py` — one dataclass field

## Tests

1. **Unit — preserve on suspension-driven exit:** drive engine.run with mocked LLM; halt the instance, force a break with pending async registration + queued message; assert registry entry still present, queue NOT drained, final state SLEEPING, log contains `[suspension-preserved]`.
2. **Unit — normal exit unchanged:** no halt; assert clear_pending + drain called, state IDLE (regression guard).
3. **Unit — terminal stop during suspension:** assert clear/drain happen, state TERMINATED, no SLEEPING.
4. **Unit — suspended but everything completed:** outstanding=False → IDLE, cleanup ran.
5. **Unit — idle checker interaction:** preserved-SLEEPING agent is skipped by `_is_idle` (idle_manager.py:123).
6. **Integration:** index §"Shared verification" — after resume-from-compression with an outstanding async child, child's result remains enqueued and a follow-up generation consumes it.
