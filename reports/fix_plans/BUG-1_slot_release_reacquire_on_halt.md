# FIX PLAN — BUG-1 (CRITICAL, root cause): Endpoint slot held across compression-halt suspension

Part of [fix_plan_endpoint_slot_deadlock.md](../fix_plan_endpoint_slot_deadlock.md). Also fixes the BUG-2 enabling condition as a side effect.

## Problem recap

`halt_all_instances()` / `resume_all_instances()` (`agent_cascade/pool/lifecycle.py:187–212`) are flag-only — they never touch `instance._slot_release`. A slot-holding agent halted mid-run blocks in `_wait_for_compression_to_clear` **while keeping the permit**. On the shared conc=0 pool (`_shared_sequential_slot_`, capacity 1) this starves everything else — including the Compressor whose completion is required to clear the halt ⇒ guaranteed circular wait whenever the compression target differs from the slot holder (the 2026-08-21 incident: Security held the slot ~4m52s while suspended; Compressor_4 timed out after exactly 300s).

Suspension wait sites (all funnel through one method):
- `engine/core.py:709–716` — after LLM stream ends
- `engine/core.py:1929–1934` — `_post_turn_checks`
- `engine/tool_execution.py:101–106` — tool-execution loop

## Change description (pseudo-code level)

**Single surgical change site: `engine/core.py::_wait_for_compression_to_clear` (~1158–1172).** All three call sites get the fix automatically; no call-site edits needed. Reuses exactly two existing idioms: `_release_slot` (idempotent capture-nullify-release) and `reacquire_for` (public bounded-FIFO re-acquire helper at core.py:1994–2072).

```python
def _wait_for_compression_to_clear(self, inst_name: str) -> bool:
    instance = self.pool.get_instance(inst_name)

    # BUG-1 FIX: yield the endpoint slot before blocking so other agents
    # (incl. the Compressor required to clear this halt) can acquire it.
    if instance is not None:
        from agent_cascade.state_ops import save_instance_state
        save_instance_state(instance)                      # KV persistence while others share pool
        self._release_slot(instance, inst_name, "compression_halt")

    try:
        while self._is_suspended_by_compression(inst_name):
            if self._is_terminal_stop(inst_name):
                return False                               # slot stays released; exit finally cleans up
            time.sleep(_COMPRESSION_WAIT_TIMEOUT)          # BUG-6 FIX (see BUG-6 plan)
    finally:
        if not self._is_terminal_stop(inst_name):
            # Same re-acquire idiom as tool_dispatcher sync-child path.
            ok = self.reacquire_for(instance, inst_name, context="after_compression_resume")
            if ok and instance is not None:
                from agent_cascade.state_ops import restore_instance_state
                restore_instance_state(instance)           # matches tool_dispatcher.py:564–573 ordering
            # on failure: reacquire_for already logged + cleared slot state (degrade-to-slotless)
    return True
```

Supporting details:
- **KV save/restore ordering** mirrors `_transition_to_sleeping` (core.py:2092–2108 saves before release) and the sync-child path (restores only AFTER successful re-acquire, to avoid evicting another agent's model mid-run — `tool_dispatcher.py:564–573`). Not optional decoration: during suspension other agents run on the same conc=0 endpoint and would otherwise thrash this agent's KV cache.
- `reacquire_for` handles the no-slot cases itself (unlimited endpoint → clears stale state, returns True; bounded `REACQUIRE_TIMEOUT=30s` FIFO wait; catches `SlotQueueTimeout`/`SlotCancelled`; degrades cleanly).
- New log line via existing patterns: `[SLOT_YIELD] compression-halt` debug in release, plus `[SLOT_REACQUIRED] after_compression_resume` from `reacquire_for`.

## Edge cases considered

| Case | Handling |
|---|---|
| Agent holds no slot when suspended (unlimited endpoint, or previously degraded) | `_release_slot` is a no-op (None check); `reacquire_for` returns True fast without touching scheduler |
| Terminal stop arrives during wait | Return False with slot still released — run()'s exit finally releases again idempotently; no leak |
| Instance dismissed/terminated during wait | Caught by `_is_terminal_stop` tick inside loop |
| Terminated *while blocked in* `reacquire_for` FIFO queue | `scheduler.acquire` raises `AgentTerminatedError` (docstring scheduler.py:103); propagates out of `_wait_for_compression_to_clear` into run()'s `except AgentTerminatedError` (core.py:804) — clean abort, slot never acquired |
| Re-acquire times out after resume | Degrade-to-slotless per existing idiom + loud `[SLOT_REACQUIRE_FAILED]` warning (open question #1 in index: degrade vs abort) |
| SLEEPING agents during halt | Already hold NO slot (released at sleep transition, core.py:2088–2108); sleep loop polls halt flags (core.py:2161 comment) — nothing to do here |
| Suspension "mid-stream" | Does not exist as an interruption point today: stream termination checks use `_is_terminal_stop` only (core.py:1236–1244); halts are noticed after stream end at Site 1. Documented invariant: any future mid-stream halt check MUST route through this same helper |
| Lock safety | No engine lock is held at any call site (verified); `_release_slot` takes `_state_lock` briefly; `reacquire_for` blocks in the FIFO **outside** `_state_lock`, taking it only momentarily to store the callback — no new lock nesting |
| Double suspension (re-halt while waiting) | Loop simply keeps spinning on the flag; release is idempotent |
| Second halt while agent is queued in re-acquire | Slot granted → helper returns True → next loop iteration reaches the next checkpoint which observes flags normally (or proceeds one segment advisory-halted — pre-existing behavior, accepted; index risk #5) |

## Why minimal / safe

- One method touched (+ zero call-site changes): the exact choke point all three suspension paths already share.
- Only pre-existing helpers are used (`_release_slot`, `reacquire_for`, `save_instance_state`, `restore_instance_state`) — same trio the sleep-transition and sync-child flows use today.
- FIFO fairness preserved by design (constraint #1/#2): the resumed agent re-acquires like anyone else — fresh ticket, tail position behind current waiters. The Compressor wins because the holder no longer owns a phantom permit, not because of priority.
- Failure modes degrade to today's known weaknesses (slotless degradation), never to new deadlock shapes: releasing can only shrink the circular-wait cycle.

## Files touched

- `agent_cascade/engine/core.py` — body of `_wait_for_compression_to_clear` (~+20 lines incl. imports)

## Tests

1. **Unit — release on suspend:** fake pool/scheduler; agent acquires slot, engine halts it, drive Site-1 path; assert `scheduler.get_status()[pool]['running_count'] == 0` while suspended and holder is None.
2. **Unit — re-acquire on resume:** clear halt; assert holder == agent again and `_slot_release` restored; assert `restore_instance_state` called after (mock).
3. **Unit — reacquire timeout:** second holder keeps the slot past `REACQUIRE_TIMEOUT`; assert degrade warning logged, `_slot_release is None`, no exception escapes, function returns True.
4. **Unit — termination during wait/reacquire:** terminate mid-wait → returns False / raises clean abort; assert no slot leak.
5. **Unit — no-slot agent:** unlimited endpoint → both release and re-acquire are fast no-ops.
6. Integration scenario: see index §"Shared verification strategy" steps 1–4.
