# FIX PLAN — BUG-7: Failed-compression retry storm (failure backoff + honest return values)

Part of [fix_plan_endpoint_slot_deadlock.md](../fix_plan_endpoint_slot_deadlock.md).

## Problem recap

`CompressionHandler.execute_force_compression` (`compression/handler.py:843–845`) catches any exception, logs, and `return True`; the success path falls off the end (returns `None`). Either way the turn loop continues and the next natural checkpoint (post-tool / async-drain / pre-LLM guard) instantly re-triggers — halting **all** other instances again (`handler.py:769`) and spawning a fresh Compressor. In the incident, attempt #2 started 59ms after attempt #1's 300s timeout expired, producing serial wasted 300s attempts while context stayed critical.

**Timeout note (design decision):** there is ONE shared slot-wait timeout for all agents — `QUEUE_WAIT_TIMEOUT=300s` (`agent_cascade/slot_queue.py:37`, applied in `api_router_pkg/scheduler.py:124–125`) — including Compressors. No per-agent overrides. The incident's 300s-vs-300s race mattered only because the slot holder was frozen mid-run while owning the permit (BUG-1); with BUG-1 fixed, holders release on suspension and either finish or free the slot, so a long FIFO wait is correct behavior. Failures surface when they actually happen; this plan's backoff gate prevents the retry storm that used to follow them.

## Change description (pseudo-code level)

### Failure backoff gate (`compression/handler.py::execute_force_compression`)

Gate FIRST, before `halt_all_instances` — failed attempts must not repeatedly freeze the pool:

```python
def execute_force_compression(self, instance, messages, llm_messages, usage_pct, response=None) -> bool:
    inst_name = instance.instance_name

    # BUG-7 FIX: back off after a FAILED attempt instead of re-halting everyone
    # immediately. Next retry = fresh attempt at a natural checkpoint; it queues
    # at FIFO tail like any other agent (no reservation — user constraint #2).
    streak = getattr(instance, '_force_compress_fail_streak', 0)
    if streak > 0:
        elapsed = time.monotonic() - getattr(instance, '_last_force_compress_fail_at', 0.0)
        backoff = min(60.0 * (2 ** (streak - 1)), 600.0)   # 60s → 600s cap
        if elapsed < backoff:
            logger.warning(f"[COMPRESSION_BACKOFF] {inst_name}: skipping forced compression "
                           f"(attempt failed {elapsed:.0f}s ago, streak={streak}, backoff={backoff:.0f}s)")
            self.engine._inject_compression_warning(llm_messages, usage_pct,
                current_tokens=..., max_tokens=...)        # reuse existing warning injection
            return False                                    # explicit: not compressed, keep going

    ...existing exempt/halt/try block...

    except Exception as e:
        logger.error(f"Forced compression raised exception for {inst_name}: {e}")
        with instance._compression_lock:
            instance._force_compress_fail_streak = streak + 1
            instance._last_force_compress_fail_at = time.monotonic()
        return False                                        # was: return True

    finally:
        self.pool.resume_all_instances()

    # success path (result.success True): reset streak under _compression_lock
    with instance._compression_lock:
        instance._force_compress_fail_streak = 0
```

Notes:
- **Current return-value behavior (verified at handler.py:838–848):** the `except` path returns **True** (handler.py:845) and the soft-failure branch (`result.success == False`, handler.py:838–841) falls off the end returning **None**. Both are dishonest — callers can't distinguish "compressed" from "failed". **Both must change to record the failure streak + `return False`.** The success path (falls off the end today, returning None) gets an explicit streak reset.
- New AgentInstance fields (next to `_force_compress_count`, `agent_instance.py:558–559`): `_force_compress_fail_streak: int = 0`, `_last_force_compress_fail_at: float = 0.0`.
- Deliberately independent of `check_cooldown`'s 2s cooldown, which the critical-threshold path **overrides** (`engine/compression_exec.py:162–169`). The backoff gate lives inside `execute_force_compression` so the override can't bypass it.
- Natural retry points are unchanged (post-tool hook `tool_execution.py:467`, async-drain `core.py:334`, pre-LLM guard `llm_call.py:109`) — each now just checks the gate first. Retry = fresh acquire at FIFO tail per constraint #2.
- Manual `/compress` command and consolidation paths don't go through this gate (different entry points), so operator-initiated retries stay immediate.
- **Rejected alternative (user decision 2026-08-21):** a scoped shorter slot-wait timeout for Compressor instances was considered to de-race the two ~300s timeouts and dropped — one shared `QUEUE_WAIT_TIMEOUT` for all slots is by design; no special-casing for system agents.

## Edge cases considered

| Case | Handling |
|---|---|
| Backoff while context critically full | Warning still injected each skip (model sees pressure); pre-LLM hard guard still raises `ContextWindowExceeded` after `compression_max_attempts` (100). Tradeoff documented as index risk #3 |
| Stale streak after restart/manual /compress success | Manual path doesn't touch streak; next successful forced compression resets it |
| Multiple agents failing simultaneously | Per-instance fields — no cross-talk |
| `halt_all_instances` skipped during backoff window | Correct — that's the point: no pool-wide freeze for an attempt we know will likely fail; other agents keep working |
| SlotCancelled during Compressor's queue wait (terminated mid-queue) | Existing handling unchanged (`SlotCancelled` propagates as clean abort through invoker) |
| Long FIFO wait under contention after BUG-1 fix | Correct by design: shared 300s timeout applies equally; holder releases on suspension, so waits terminate when real work finishes |

## Why minimal / safe

- One gate + two fields + honest return values in one method that already owns failure logging; ordering change (gate before halt) strictly reduces side effects.
- Zero new configuration or plumbing — the shared timeout model is untouched.
- No queue semantics touched (constraint #1/#2 intact): retries remain ordinary FIFO citizens.

## Files touched

- `agent_cascade/compression/handler.py` — gate + failure recording + explicit returns
- `agent_cascade/agent_instance.py` — two new dataclass fields

(`engine/core.py` changes belong to the BUG-1/BUG-6 plans, not this one.)

## Tests

1. **Unit — backoff gate:** stub `_compress` to raise; call `execute_force_compression`; assert streak=1, return False, halt flags NOT set on immediate second call (gate short-circuits before `halt_all_instances`).
2. **Unit — backoff expiry:** freeze/manipulate `_last_force_compress_fail_at`; assert gate passes after backoff window and proceeds to halt+compress.
3. **Unit — success resets:** successful compression zeroes the streak.
4. **Unit — soft-failure path:** `result.success=False` records streak and returns False.
5. **Integration:** index §"Shared verification" step 5 (failure injection: attempt fails when it fails naturally under the shared 300s timeout, no instant re-halt, later checkpoint succeeds).
