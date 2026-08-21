# FIX PLAN — BUG-11 (OBS): Guaranteed DEBUG tracing of slot queue lifecycle

Part of [fix_plan_endpoint_slot_deadlock.md](../fix_plan_endpoint_slot_deadlock.md). Observability-only — zero behavior changes; independent of all other plans in this folder.

## Problem recap: what's logged today vs missing

All facts verified against `agent_cascade/slot_queue.py` and `agent_cascade/api_router_pkg/scheduler.py`:

| Event | Today | Gap |
|---|---|---|
| Timeout | WARNING `_log_acquire_timeout` (slot_queue.py:342–350): pool key, ticket id, agent, wait_time, running/waiters counts | **Complete — nothing to add** |
| Grant | INFO `[EndpointScheduler] Agent 'X' acquired slot …` (scheduler.py:135–136): pool key, active/capacity counts | No wait duration, no ticket id, fast-path vs queued-grant indistinguishable |
| Release | INFO `[EndpointScheduler] Agent 'X' released slot …` (scheduler.py:147–150): pool key, active count | **No held duration** |
| Enqueue (enter FIFO wait) | Nothing | **Missing entirely** |
| Cancel / terminate-for-agent | Nothing (`SlotCancelled` raised silently at slot_queue.py:196/207; `cancel()`/`terminate_for_agent()` L234–272 return silently) | **Missing entirely** |

The 2026-08-21 incident was reconstructable only because the timeout line happened to carry holder info. Enqueue/grant ordering and holder durations had to be inferred from timestamps across three loggers. This plan makes the full lifecycle one-grep traceable under DEBUG.

## Change description (pseudo-code + exact insertion points)

Logger: reuse the module logger (`logger = logging.getLogger(__name__)` → `agent_cascade.slot_queue`). Prefix all lines `[SLOTPOOL]` to match the existing `_log_acquire_timeout` style.

### 1. Enqueue log — `SlotPool.acquire` slow path

Insert immediately AFTER `self._waiters[ticket.ticket_id] = ticket` (slot_queue.py:174), i.e., once per enqueue, OUTSIDE/BEFORE the while loop:

```python
logger.debug(
    f"[SLOTPOOL] Queued on '{self.key}': agent={instance_name} ({agent_class}) "
    f"ticket={ticket.ticket_id} position={len(self._waiters)} "
    f"waiters={len(self._waiters)} holders={[h.instance_name for h in self._running.values()]} "
    f"timeout={timeout:.0f}s"
)
```

Covers every user-required field: instance, class, pool key, ticket id + queue position, waiter count, current holders, timestamp (added by logging framework).

### 2. Grant log — `_grant()` helper (slot_queue.py:310–320)

`_grant` is the single choke point for BOTH the fast path (acquire L160) and queued grant (L200). Add optional wait metadata so both paths log through one line:

```python
def _grant(pool, instance_name, agent_class, ticket=None) -> SlotHolder:
    ...
    pool._running[instance_name] = holder
    waited = (time.monotonic() - ticket.created_at) if ticket else 0.0
    logger.debug(
        f"[SLOTPOOL] Granted on '{pool.key}': agent={instance_name} ({agent_class}) "
        f"acquisition={holder.acquisition_id}"
        + (f" ticket={ticket.ticket_id} waited={waited:.1f}s" if ticket else " (fast-path)")
    )
    return holder
```

Call sites: fast path passes no ticket (`_grant(self, instance_name, agent_class)`); queued grant passes it (`_grant(self, instance_name, agent_class, ticket)`). Covers: who, pool key, elapsed wait since enqueue, acquisition id. The existing scheduler INFO grant line stays untouched — it carries endpoint-level info (api_base, active/capacity) at INFO; ours adds ticket/waited at DEBUG. Complementary, not duplicate.

### 3. Release log — `SlotPool.release` (slot_queue.py:209–217)

Insert after `del self._running[...]`, before `notify_all()`:

```python
held = time.monotonic() - holder.granted_at
logger.debug(
    f"[SLOTPOOL] Released '{self.key}': agent={holder.instance_name} "
    f"held={held:.1f}s running={len(self._running)}/{self.capacity}"
)
```

(`held_duration` is impossible at the scheduler layer — its release wrapper only holds the closure callback, not the SlotHolder — which is exactly why this belongs in SlotPool.)

### 4. Cancel logs — three silent paths

- Pre-raise in `acquire()` when a queued ticket is found cancelled (slot_queue.py:194–196 and the fall-through at 206–207 — one helper or two inline lines):
  ```python
  logger.debug(f"[SLOTPOOL] Cancelled while waiting on '{self.key}': agent={ticket.instance_name} ticket={ticket.ticket_id}")
  ```
- In `cancel()` (L234–256): when `cancelled_ids` non-empty, one DEBUG with agent_name + `len(cancelled_ids)` + ticket ids.
- In `terminate_for_agent()` (L258–272): same shape — agent_name, cancelled count, ids.

### Explicitly NOT added

- Any log inside the 1s poll loop (`while not ticket.cancelled...`) — see edge cases.
- Timeout events — already covered by `_log_acquire_timeout`.
- scheduler.py changes — none required; its existing INFO lines stay as-is.
- New config knobs/env vars — DEBUG level control already exists via standard logging config.

## Edge cases considered

| Case | Handling |
|---|---|
| Log volume | All four events are once-per-slot-lifecycle (enqueue/grant/release/cancel), not per-tick — negligible even with hundreds of acquires. DEBUG-gated anyway. |
| Per-tick spam in wait loop | Structurally prevented: enqueue logs once before the loop; grant logs once inside `_grant`; nothing logs between `cond.wait_for` iterations. Test asserts record count stays flat across N poll ticks. |
| Logging under `_cond` lock | Lines are single f-string evaluations, no blocking calls — matches existing practice (`_log_acquire_timeout` also logs under `_cond`). |
| Fast-path grants (no queueing) | Logged via same `_grant` line tagged `(fast-path)` with `waited=0` semantics omitted — distinguishes contention-free from contended acquisitions. |
| `release()` early-return on stale/idempotent release (existing/acquisition_id mismatch, L212–214) | Early return BEFORE the new log — stale releases stay silent, only real releases log. |
| Performance | Four f-strings per acquire/release cycle; string formatting only executes when DEBUG enabled (guard is implicit via logger level check on call). |

## Why minimal / safe

Pure additive `logger.debug` calls plus one optional-parameter change to an internal helper (`_grant` is called only from within this module — grep-verified two call sites). No state, no locking, no behavioral branches. If DEBUG is off (production default), cost is four boolean checks per lifecycle event.

## Files touched

- `agent_cascade/slot_queue.py` — 4 insertion points (+1 optional param on `_grant`)
- `agent_cascade/api_router_pkg/scheduler.py` — NO changes

## Tests

1. **caplog — enqueue:** fill capacity with holder A; start `acquire("B")` in thread; assert one `agent_cascade.slot_queue` DEBUG record matching `[SLOTPOOL] Queued` containing B's name, ticket id, `position=1`, holders list including A.
2. **caplog — grant (queued):** release A's slot; assert `[SLOTPOOL] Granted` record for B with `ticket=` and `waited=` fields; assert B's acquire returns.
3. **caplog — grant (fast path):** uncontended acquire logs Granted with `(fast-path)`, no `ticket=`.
4. **caplog — release:** call release callback; assert Released record with `held=<duration>` ≥ test sleep delta.
5. **caplog — cancel:** enqueue B then terminate B (or cancel by ticket); assert Cancelled record and that `SlotCancelled` propagates.
6. **No-spam guard:** let a queued waiter tick through ≥3 poll iterations (hold capacity shut, short manual waits); assert total Queued records == 1 for that agent.
7. **Timeout unchanged:** assert existing `_log_acquire_timeout` WARNING still fires alongside the new Cancelled-style coverage (regression guard).
