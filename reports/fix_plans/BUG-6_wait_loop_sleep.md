# FIX PLAN — BUG-6: Busy-spin in `_wait_for_compression_to_clear`

Part of [fix_plan_endpoint_slot_deadlock.md](../fix_plan_endpoint_slot_deadlock.md).

## Problem recap

The suspension wait loop (`engine/core.py:1158–1172`) calls `self.pool.wait_if_paused(timeout=_COMPRESSION_WAIT_TIMEOUT)` each tick. But `wait_if_paused` (`pool/slots.py:144–146`) waits on the **global** `_paused` Event, while compression-halt is **per-instance** (`_halted_instances`, set by `halt_instance` at `pool/slots.py:158–160`, which never clears `_paused`). While the pool is globally resumed (the normal case), every `Event.wait(1.0)` returns immediately ⇒ tight hot loop (~100% of that thread's core) for the entire suspension. In the incident that was ~5 minutes × 2 agents of pure spin.

## Change description

One line in `_wait_for_compression_to_clear` (inside the method BUG-1 rewrites anyway):

```python
while self._is_suspended_by_compression(inst_name):
    if self._is_terminal_stop(inst_name):
        return False
    time.sleep(_COMPRESSION_WAIT_TIMEOUT)   # was: self.pool.wait_if_paused(timeout=_COMPRESSION_WAIT_TIMEOUT)
```

- `_COMPRESSION_WAIT_TIMEOUT = 1.0` stays as-is (`core.py:80`).
- `time` is already imported in `core.py`.
- The global-pause behavior is not lost: a user pause during suspension just means this loop keeps ticking until both the halt flag clears and the next checkpoint's own pause-waits engage; responsiveness remains bounded by the same 1s tick.
- Terminal-stop responsiveness unchanged: checked at the top of every 1s iteration.

## Edge cases considered

| Case | Handling |
|---|---|
| Pool globally paused while agent suspended | Loop keeps 1s-ticking on the per-instance flag (correct source of truth); no behavioral change vs today other than CPU |
| Halt flag cleared mid-sleep | Worst-case +1s resume latency — already the effective granularity today by design |
| Instance terminated during sleep tick | Caught by `_is_terminal_stop` next iteration (unchanged) |
| Future desire for instant wakeup | Optional follow-up: per-pool `threading.Condition` notified by `resume_instance()`/`halt_instance()`. Deliberately NOT in this fix — new machinery for a problem a sleep tick solves adequately |

## Why minimal / safe

Single-line replacement of an ineffective wait with a plain sleep; removes the only consumer whose semantics were broken by the global-vs-per-instance mismatch. No locks, no new primitives.

## Files touched

- `agent_cascade/engine/core.py` — one line inside `_wait_for_compression_to_clear`

## Tests

1. **Unit:** suspend an agent with mocked time; assert `time.sleep(1.0)` invoked repeatedly and NO `pool.wait_if_paused` call from this path.
2. **Unit:** assert loop exits promptly when flag clears and when terminal stop fires mid-wait.
3. Manual/CPU check: suspension of ~60s shows flat-idle thread instead of a spinning one (before: measurable busy core).
