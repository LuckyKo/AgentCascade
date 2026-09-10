---
name: scheduler-architecture
description: Architecture and implementation details of the per-slot FIFO queue scheduler system, including reservation registry, lifecycle hooks, and known edge cases. Use when working on endpoint scheduling, slot management, agent concurrency, or debugging model trashing issues.
triggers:
  - scheduler architecture
  - endpoint scheduling
  - slot management
  - model trashing
  - concurrent agents
  - FIFO queue
  - reservation registry
  - agent lifecycle hooks
  - Rule 4 inheritance
  - sweep thread
---

# Scheduler Architecture & Implementation Lessons

## When to Use This Skill
- Working on endpoint scheduling, slot management, or agent concurrency
- Debugging model trashing, interleaved turns, or slot contention issues
- Adding new scheduling features (priority queues, fair sharing, etc.)
- Modifying agent lifecycle hooks (sleep/wake, terminate, async spawn)

## Architecture Overview

### Core Problem Solved
The original semaphore-based `EndpointScheduler` allowed two sync agents to interleave turns on the same endpoint slot, causing model trashing. Specifically: A(sync,pool P)→B(async,pool Q); A→D(sync,P); B→C(sync,P). When A sleeps awaiting B, C could acquire pool P before D, causing parallel sends.

### Solution: Ticket-Based FIFO Queue System
- **Module**: `agent_cascade/slot_queue.py` — new core scheduling module
- **Key classes**: `QueueTicket`, `SlotHolder`, `Reservation`, `SlotPool`
- **EndpointScheduler** is now a thin façade delegating to `SlotPool`

### Critical Design Decisions

#### 1. OrderedDict for FIFO Queue (O(1) Removal)
```python
self._waiters: OrderedDict[int, QueueTicket] = OrderedDict()
```
- Head waiter accessed via `next(iter(_waiters))` — O(1)
- Cancellation removes ticket in O(1) via `pop(ticket_id)`
- Verified under 8-thread contention with mass cancellation of 100 waiters completing in <50ms

#### 2. Reservation Registry Prevents Slot Stealing
When parent A sleeps awaiting async child B:
- A reserves pool P before releasing its slot
- Only agents whose ancestor chain intersects with reservation's chain can be granted (self-exemption)
- C (unrelated to A) is blocked until A unreserves on wake
- **Self-exemption logic**: `_blocked_by_reservation` checks `any(name in res.ancestor_chain for name in grantee_ancestor_chain)` — if ANY overlap, not blocked

#### 3. Ancestor Chain Built Once, Reused Everywhere
Shared utility: `build_ancestor_chain(current_instance_name, get_agent_by_name)` in `slot_queue.py`
- Used by: agent_pool (async spawn reservation), execution_engine (sleep/wake), tool_dispatcher (re-acquire)
- Chain format: tuple of instance names from current agent up through parents to root

#### 4. Timeout Configuration Priority
```python
effective_timeout = timeout if timeout is not None else (QUEUE_WAIT_TIMEOUT or ENDPOINT_SLOT_ACQUIRE_TIMEOUT)
```
- `QUEUE_WAIT_TIMEOUT` (300s default) takes precedence — configurable via `QWEN_AGENT_SLOT_QUEUE_TIMEOUT`
- Old `ENDPOINT_SLOT_ACQUIRE_TIMEOUT` (30s) is fallback for backward compat only

### Lifecycle Hooks Locations

| Event | File | Method | Action |
|-------|------|--------|--------|
| Parent sleeps awaiting child | execution_engine.py | _transition_to_sleeping() | Reserve pool, release slot |
| Async child spawned on different pool | agent_pool.py | _run_child_async() | Reserve parent's pool |
| Agent wakes from sleep | execution_engine.py | _handle_sleeping_state() | Unreserve, re-acquire with 30s timeout |
| Agent terminated | agent_instance.py | terminate() | Cancel tickets + unreserve reservations |
| Session stopped | agent_pool.py | stop_session() | Cancel all tickets + clear reservations |

### Rule 4 Inheritance (tool_dispatcher.py)
Children with no own endpoints run SYNC inline on parent's thread:
```python
if caller_holds_slot and child_slot_info and not child_slot_info.get('has_own_endpoints'):
    # Run sync inline — borrow permit implicitly, NO acquire needed
```
**Critical**: This check MUST use `elif` before collision detection block to prevent override.

### Background Sweeper Thread
- **Location**: `api_router.py` — `_sweeper_loop()` in EndpointScheduler
- **Interval**: 60 seconds
- **Purpose**: Cleans stale reservations from agents that died without calling terminate()
- **Daemon thread** — doesn't block process exit
- Uses `detect_stale_reservations(RESERVATION_TIMEOUT)` per pool

### Known Issues & Edge Cases

1. **EndpointScheduler uses RLock not Lock** — `_start_sweeper()` is called from within `_get_or_create_pool()` while holding the lock. Deadlock found during integration testing.

2. **Re-acquire self-exemption via ancestor_chain** — When parent re-acquires after sync child, passes full ancestor chain to `pool.acquire()` so reservation system recognizes it as exempt. Uses 30s timeout with queue-aware acquire (not immediate try).

3. **Flaky tests under xdist** — Some scheduler tests can be timing-sensitive when run in parallel. The integration test suite uses FIFOWaiterSetup helpers with gate-based synchronization to minimize flakiness, but occasional failures may still occur under heavy load.

### Testing Patterns (tests/test_scheduler_integration.py)
- **FIFOWaiterSetup**: Reusable helper for deterministic FIFO ordering tests via handshake gates
- **SchedulerFIFOWaiterSetup**: Same pattern but acquires through EndpointScheduler API
- **Procedural generation**: Random agent call graphs with configurable seeds verify no violations under unpredictable patterns
- **ViolationTracker**: Monitors per-slot running count to detect >capacity violations

### Configuration Constants (slot_queue.py)
```python
QUEUE_WAIT_TIMEOUT = 300      # Default timeout for queue wait (QWEN_AGENT_SLOT_QUEUE_TIMEOUT env var)
RESERVATION_TIMEOUT = 600     # Max age before sweeper cleans reservation
REACQUIRE_TIMEOUT = 30        # Timeout when parent re-acquires after child
QUEUE_WAIT_TICK = 1           # Tick interval in acquire wait loop (for interruptibility)
```

### Files Modified
- `agent_cascade/slot_queue.py` — NEW core module
- `agent_cascade/api_router.py` — EndpointScheduler refactored + sweeper thread
- `agent_cascade/tool_dispatcher.py` — Rule 4 inheritance, re-acquire logic
- `agent_cascade/execution_engine.py` — Reservation hooks in sleep/wake paths
- `agent_cascade/agent_pool.py` — Async spawn reservation, stop_session cleanup
- `agent_cascade/agent_instance.py` — Termination cleanup

### Plan Reference
Full implementation plan: `plans/api_scheduler_queue_refactor_plan.md`
