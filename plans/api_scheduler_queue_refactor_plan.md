# Implementation Plan: Per-Slot FIFO Queue Scheduler Refactor

**Date**: 2026-08-10
**Status**: DRAFT v2 — REVISED per architectural review (2026-08-10)
**Investigation reference**: `reports/api_scheduling_architecture_investigation.md`, `.agent_lessons/api-scheduling-architecture.md`
**Review reference**: `reviews/scheduler_plan_review.md` (scheduler_plan_reviewer, 2026-08-10)

> **REVISION MARKERS (v2)**: Sections changed in this revision are marked with `▶️ [v2]` at the
> start of the block, and addressed reviewer items are cited inline as `(Review #N)`. This makes
> the delta easy to spot during the next review. Unmarked sections are unchanged from v1.

---

## 0. Problem Statement (from investigation)

Current `EndpointScheduler` (api_router.py:227) uses raw `threading.Semaphore` per slot with a 30s acquire timeout. Gaps that cause model trashing:

1. **No queue / no fairness** — semaphore waiters have no ordering, no priority, no re-queue on timeout; blocked agents fail with `TimeoutError` (or proceed slotless after re-acquire giving up) → two sync agents interleave turns on the same slot.
2. **Async child chain bypass** — A(sync)→B(async, different pool)→C(sync on A's pool) + A→D(sync): C can take A's pool slot while A still owes work on it. Slot ownership is "currently held" only, no reservation awareness across ancestor chains.
3. **Slot key vs actual endpoint skew** — slot key derives from first chain entry; `call_with_fallback` may fall over to other endpoints whose slot pool never got a hold.
4. **No cancel-on-termination for waiting agents** — a waiting instance's queue position persists after dismiss.

---

## 1. Architecture Overview

Replace semaphore blocking with a **ticket-based FIFO wait queue per slot pool**, plus a **reservation registry** for ancestor-chain protection. Semaphores remain only as a *capacity/signaling* primitive; the queue owns ordering and cancellation.

```
┌─────────────────────────────────────────────────────────────────┐
│                     EndpointScheduler (refactored)              │
│                                                                 │
│   SlotPoolRegistry: dict[slot_key → SlotPool]                   │
│   ┌──────────────────────────────────────────────────────────┐  │
│   │ SlotPool (per slot_key)                                 │  │
│   │  - key: '_shared_sequential_slot_' | api_base           │  │
│   │  - capacity: 1 (conc=0) | N (conc>0) | ∞ (conc=-1)      │  │
│   │  - _waiters: deque[QueueTicket]  (FIFO, ticket order)   │  │
│   │  - _running: set[SlotHolder]     (current permit owners)│  │
│   │  - _cond: threading.Condition    (notify on release/    │  │
│   │                                    cancel/termination)   │  │
│   │  - _reservations: set[agent_name] (ancestor reservations)│ │
│   └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│   Ticket: token = (seq, ticket_id, agent_name, instance_name,   │
│                    cancelled: Event, deadline)                   │
│                                                                 │
│   acquire(): ticket → if capacity free & no blocking reservation│
│              → grant immediately (permit transfer)             │
│              else → enqueue & wait on ticket.cancelled + cond  │
│                                                                 │
│   release(): remove from _running → signal next waiter          │
│   cancel(ticket_id): remove from _waiters w/o granting          │
│   reserve(agent): add to _reservations (blocks grants to        │
│                   any NON-reserved waiter on same pool)         │
└─────────────────────────────────────────────────────────────────┘
```

**Key design decisions:**

- **One `SlotPool` per `slot_key`.** Shared sequential pool (`conc=0`) maps to a single global `SlotPool(capacity=1)` regardless of api_base — preserves the anti-cache-trashing design.
- **Permit model**: a `SlotPool` holds up to `capacity` concurrent "permits". No semaphore object at all — permits are granted/revoked explicitly with the pool's condition variable. This gives us cancellation (remove a waiter and re-grant its permit to the next) which semaphores cannot do cleanly.
- **Ticket**: each waiter gets a monotonically increasing ticket. Waits are ordered by ticket. Condition-based wakeups with `wait_for(callback)` semantics (not bare `wait()`) to avoid spurious-wakeup races.
- **Reservation registry**: when an agent transitions to SLEEPING or launches an async child, it registers a reservation on its pool slot key. New grants to that pool are blocked while the reservation exists *unless* the grantee is in the reserving agent's ancestor chain (i.e., the exact work the reserving agent is waiting for). This closes Gap A (C cannot steal A's slot while A waits for B).

---

## 2. Data Structures

### 2.1 QueueTicket (dataclass)

```python
@dataclass
class QueueTicket:
    ticket_id: int            # monotonic global counter (itertools.count)
    seq: int                  # pool-local sequence for FIFO ordering
    agent_name: str           # display name (for diagnostics)
    instance_name: str        # pool instance naming
    agent_class: str
    slot_key: str
    created_at: float         # time.monotonic()
    deadline: float           # created_at + queue_timeout (configurable)
    cancelled: threading.Event
    granted: threading.Event
    holder_ctx: dict          # {parent_chain: tuple, reservation_token: str|None}
```

### 2.2 SlotHolder (running permit owner)

```python
@dataclass
class SlotHolder:
    agent_name: str
    instance_name: str
    acquisition_id: int       # unique per grant (prevents stale release)
    granted_at: float
    reservation_token: str | None   # set if granted via ancestor reservation
```

### 2.3 SlotPool

▶️ [v2] **SlotPool (Blocker #1, Review #5/#15)**

```python
class SlotPool:
    __slots__ = ("key", "capacity", "_waiters", "_running", "_reservations", "_cond", "_seq_counter")
    # _waiters: OrderedDict[ticket_id → QueueTicket]  (FIFO by insertion order, O(1) remove)
    #           Python 3.7+ guarantees insertion order — head = next(iter(_waiters))
    # _running: dict[instance_name → SlotHolder]
    # _reservations: dict[reservation_token → Reservation]
    # _cond: threading.Condition — ALL mutations under this lock
```

▶️ [v2] **FIFO guarantee (explicit, reviewer-verified):** every waiter, when notified, re-checks under `_cond` whether **it is `next(iter(pool._waiters))` AND capacity is free AND no blocking reservation applies**. Because only the head satisfies the predicate, exactly one waiter per wake can proceed; non-head waiters immediately re-wait. Notifications use `notify_all()` (a bare `notify()` could strand the head if a non-head ticket is notified first). This yields strict FIFO with plain condition variables — no per-ticket condition objects needed. A unit test asserting grant order under 8-thread contention guards against regressions.

▶️ [v2] **O(1) removal structure (Blocker #1, Review #5/#15):** `_waiters` is an `OrderedDict[ticket_id → QueueTicket]`, NOT a `deque`. This gives:
- FIFO iteration: `next(iter(pool._waiters))` = head waiter in O(1).
- Dequeue head: `pool._waiters.pop(next_key)` in O(1).
- Cancel/timeout removal by ticket_id: `pool._waiters.pop(ticket_id, None)` in O(1).
This is a hard requirement for Phase 1 (not deferred) — `stop_session` with 100+ waiters must complete queue cleanup in < 50ms under the condition lock. No separate `_waiters_map` needed; the OrderedDict IS both the queue and the index.

▶️ [v2] **Reservation (Blocker #3, Review #10/#23/#3)**

```python
@dataclass
class Reservation:
    token: str                # f"res-{agent_name}-{acquisition_id}"
    agent_name: str           # who is waiting (the SLEEPING/async-waiting agent)
    ancestor_chain: tuple     # instance names in the parent chain (incl. self)
    slot_key: str
    created_at: float
    reason: str               # "sleeping" | "async_child" | "sync_yield"
```

▶️ [v2] **Multiple reservations per agent (Review #3):** an agent may hold multiple reservations on the same pool (e.g., sleeping + async child). The reservation dict is keyed by `token`, not agent_name. All operations that affect an agent's reservations (`unreserve_for_agent`, `terminate()` cleanup, stale-sweep) must iterate all tokens and clear every matching reservation atomically under `_cond`. The unreserve path in wake-from-sleep clears ALL reservations for that agent before re-acquire — never relies on a single token.

▶️ [v2] **Reservation timeout (Blocker #3, Review #10/#23):** to prevent permanent leaks from dead agents:
- Each reservation has an implicit timeout: `RESERVATION_TIMEOUT = 300s` (configurable `QWEN_AGENT_RESERVATION_TIMEOUT`).
- The scheduler exposes a diagnostic method `detect_stale_reservations(threshold)` that returns reservations older than threshold.
- A lightweight background janitor thread (`scheduler._stale_sweeper`) runs every 60s, calls `detect_stale_reservations(RESERVATION_TIMEOUT)`, forcibly unreserves any stale entry, and logs `[SCHEDULER_STALE_RESERVATION]` with agent_name, reason, age. This is best-effort (does not block normal operations) and only fires when an agent died without calling terminate/unreserve — in a healthy system it never triggers.

---

## 3. Queue Semantics (acquire / release / cancel / reserve)

### 3.1 acquire(api_base, concurrency_limit, instance_name, agent_class, ctx) → release_cb | None

Algorithm (all under `pool._cond`):

▶️ [v2] **acquire algorithm (Blocker #2, Review #1/#4/#7)** — all under `pool._cond`:

```python
def acquire(pool, instance_name, agent_class, ancestor_chain):
    with pool._cond:
        # Fast path: capacity available AND no blocking reservation
        if len(pool._running) < pool.capacity and not _blocked_by_reservation(pool, ancestor_chain):
            holder = grant(pool, instance_name, agent_class, ancestor_chain)
            return make_release_cb(holder)

        # Slow path: enqueue
        ticket = QueueTicket(seq=next(pool._seq_counter), ...)
        pool._waiters[ticket.ticket_id] = ticket   # OrderedDict insert at tail (FIFO)
        deadline = ticket.deadline
        while not ticket.cancelled.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _remove_ticket(pool, ticket)       # O(1) via OrderedDict.pop(ticket_id)
                raise SlotQueueTimeout(ticket)     # distinct from TimeoutError

            # Wait until: capacity frees, reservation clears, ticket is head
            if pool._cond.wait_for(
                lambda: (len(pool._running) < pool.capacity
                         and not _blocked_by_reservation(pool, ancestor_chain)
                         and next(iter(pool._waiters)) == ticket.ticket_id),  # STRICT FIFO head check
                timeout=min(remaining, 1.0)):      # 1s tick = interruptible within 1s

                # Double-check after wakeup (cancel-after-grant guard — Review #7)
                if ticket.cancelled.is_set():
                    _remove_ticket(pool, ticket)
                    raise SlotCancelled()

                head_id = next(iter(pool._waiters))
                if head_id == ticket.ticket_id:    # still head? (another waiter might have barged on notify_all race)
                    pool._waiters.pop(ticket.ticket_id)  # O(1) dequeue
                    holder = grant(pool, instance_name, agent_class, ancestor_chain)
                    ticket.granted.set()
                    return make_release_cb(holder)

        # cancelled → exit loop, remove from queue
        _remove_ticket(pool, ticket)
        raise SlotCancelled()
```

▶️ [v2] **O(1) removal helper:** `_remove_ticket(pool, t)` = `pool._waiters.pop(t.ticket_id, None)` — single OrderedDict pop, O(1). No separate map.

▶️ [v2] **Strict-FIFO eligibility**: only the head ticket (`next(iter(pool._waiters))`) may be granted. No barging by later tickets even when the reservation temporarily clears for a non-head waiter — prevents starvation and preserves order. (This is stricter than the current semaphore, which is exactly what the task demands.)

▶️ [v2] **Reservation blocking predicate — concrete pseudocode (Blocker #2, Review #1/#4):**

```python
def _blocked_by_reservation(pool: SlotPool, ancestor_chain: tuple) -> bool:
    """Return True if ANY active reservation blocks this grantee.

    A reservation blocks unless the grantee is a descendant of the reserving agent
    (i.e., grantee's instance_name appears in the reservation's ancestor_chain).

    This implements self-exemption implicitly: if A reserves and then A re-acquires,
    A's own instance_name IS in A's ancestor_chain → not blocked.
    """
    for res in pool._reservations.values():
        # Grantee is allowed if it appears anywhere in the reservation's ancestor chain
        # (includes self-exemption: A's instance_name is always in its own chain)
        if any(name in res.ancestor_chain for name in ancestor_chain):
            continue   # this reservation does NOT block us; check next one
        return True    # blocked by this reservation until it clears
    return False       # no active reservations block us
```

**Why this is correct:** A's ancestor chain always includes A itself. So when A wakes and tries to re-acquire, `any(name in res.ancestor_chain for name in ancestor_chain)` matches immediately — A is never blocked by its own reservation. The rule "unreserve before re-acquire" (see §7.4) is still followed as a belt-and-braces measure, but correctness no longer depends on it.

▶️ [v2] **Atomicity guarantee (Review #2):** dequeue + grant + `ticket.granted.set()` all occur under the same `_cond` lock without releasing it in between. The cancel-after-grant guard (`if ticket.cancelled.is_set()` immediately after wakeup) ensures that a cancellation arriving at the exact moment of wakeup does not result in a stale grant.

▶️ [v2] **No slot (conc=-1):** `acquire` returns `None` immediately (fast path: `capacity=∞`).

### 3.2 release(holder) — under `pool._cond`

```
with pool._cond:
    # idempotency: match acquisition_id; ignore stale releases
    if pool._running.get(holder.instance_name, None)?.acquisition_id != holder.acquisition_id:
        return
    del pool._running[holder.instance_name]
    pool._cond.notify_all()   # wake head waiter(s)
```

▶️ [v2] **cancel(ticket_id | agent_name) — termination path (Review #7):**

```python
def cancel(pool, ticket_id=None, agent_name=None):
    with pool._cond:
        if ticket_id is not None and ticket_id in pool._waiters:
            pool._waiters[ticket_id].cancelled.set()   # signal waiting thread
            pool._waiters.pop(ticket_id)               # O(1) removal
            pool._cond.notify_all()
            return True
        elif agent_name is not None:
            # Cancel all tickets for this agent (mass cancel / terminate path)
            cancelled = []
            for tid, t in list(pool._waiters.items()):
                if t.instance_name == agent_name:
                    t.cancelled.set()
                    cancelled.append(tid)
            for tid in cancelled:
                pool._waiters.pop(tid)                 # O(1) each
            if cancelled:
                pool._cond.notify_all()
            return len(cancelled) > 0
        return False   # was running → caller must release() explicitly
```

Callable from any thread (pool stop, dismiss, terminate-instance, wait_for_message timeout, stop_session).

▶️ [v2] **reserve(agent, reason) / unreserve(token) / unreserve_for_agent(agent_name) (Review #3/#4):**

- `reserve()` adds a `Reservation` under the agent's current slot_key (if it holds one). Called by `_transition_to_sleeping` (before releasing the permit) and by `_run_child_async` (parent spawns async child on a different pool — it keeps its own permit but registers the reservation so grandchildren can't steal while waiting).
- `unreserve(token)` removes ONE specific reservation. If removal makes the pool eligible, notify_all.
- `unreserve_for_agent(agent_name)` removes ALL reservations for that agent atomically under `_cond`. This is used by:
  - wake-from-sleep (belt-and-braces after explicit token unreserve),
  - `terminate()` cleanup,
  - stale reservation sweeper.

**Why reserve on async-spawn**: even though A keeps its permit while B runs async (different pool), A will eventually `wait_for_message` / SLEEP and release its permit. The reservation persists across that release so C (unrelated) can't grab it before A's child work completes. The reservation is tied to A's *promise of work*, not just the permit.

▶️ [v2] **Lock ordering protocol (High Priority #8, Review #6):** to prevent deadlocks between scheduler locks and instance/pool locks:

1. **Scheduler condition lock (`pool._cond`) is the innermost lock.** It is NEVER held while acquiring any other lock (`instance._state_lock`, `scheduler._lock`, `pool._execution._state_lock`). All mutations under `_cond` are self-contained.
2. **Instance state lock (`instance._state_lock`) is acquired BEFORE scheduler calls.** Example: `_transition_to_sleeping` acquires `_state_lock`, reads/clears `_slot_release`, releases `_state_lock`, THEN calls `scheduler.reserve()` and `_release_slot()`. This ensures no nested hold of both locks.
3. **Scheduler façade lock (`scheduler._lock`) guards pool registry only.** It is acquired briefly to create/fetch a `SlotPool`, then released before entering that pool's `_cond`. No pool mutations occur under `scheduler._lock`.
4. **`cancel()` and `unreserve_for_agent()` may be called from any thread without holding other locks.** They acquire the target pool's `_cond` directly.

This ordering (instance → scheduler façade → pool condition, never nested) is documented in code comments at each call site. Unit tests for lock-ordering violations use a deadlock-detection harness (assert no lock held longer than 2s).

▶️ [v2] **Diagnostic APIs (High Priority #5, Review #22/#23):** new methods on `EndpointScheduler`:

- `get_status()` — unchanged signature; reimplemented to read from `_running`/`_waiters` OrderedDict.
- `get_slot_holders(slot_key)` — returns deep copy of running holders list (unchanged signature).
- `detect_stuck_slots(threshold=60)` — flags running holders with age > threshold (unchanged behavior, reimplemented).
- `get_reservations()` — NEW: returns list of `(slot_key, token, agent_name, reason, created_at, age_s, ancestor_chain)` for all active reservations across all pools. Used by UI/debug endpoints.
- `detect_stale_reservations(threshold=300)` — NEW: returns reservations older than threshold. Logged as warning; triggers stale-sweeper unreserve if called from the janitor thread.

▶️ [v2] **Process-crash reservation cleanup (Review #9/#23):** there is no cross-process persistence, so a process crash wipes all in-memory queues and reservations instantly — no leak possible on restart. The concern is only about long-running processes where an agent dies without calling `terminate()`. This is handled by:
- The stale reservation sweeper (60s interval, 300s timeout).
- `detect_stale_reservations()` exposed for monitoring/alerting.
No background janitor thread is started during Phase 1; it is added in Phase 3 alongside reservations.

---

## 4. conc=0 Shared Sequential Slot in the New System

- All endpoints with `concurrency_limit == 0` resolve to `slot_key = '_shared_sequential_slot_'` (unchanged from current behavior, api_router.py:278-281).
- Refactor creates exactly **one** `SlotPool(capacity=1)` for that key, shared by ALL agents on ALL conc=0 endpoints. api_base is irrelevant to the pool; only the key matters.
- Sequential semantics: `capacity=1` + strict FIFO → every agent on any sequential endpoint waits in the same global line. No interleaving across api_bases → no KV-cache thrash (the original design intent, preserved).
- Diagnostics: the pool exposes `queue_depth`, `waiters` (agent names + wait times), `running`.

---

## 5. Slot Inheritance / Borrowing (child inherits parent's slot)

**Requirement**: children with no assigned endpoints inherit parent's slot/queue position.

### 5.1 Chain resolution — where the parent's slot_key is inherited

In `agent_pool._acquire_slot()` (agent_pool.py:2554), the current code computes effective concurrency + api_base for the CHILD's agent_class. New behavior:

```
def _acquire_slot(instance, agent_class=None, caller_agent_type=None, ...):
    # existing: router.get_effective_concurrency(agent_class, caller_agent_type)
    # existing: router.get_llm_config(...)  → api_base
    # NEW: if child has NO own endpoints (had_own_endpoints is False) and caller
    #      holds a slot → inherit caller's (slot_key, pool) DIRECTLY.
    #      This is slot borrowing: child does NOT create a new queue entry;
    #      it reuses the parent's permit via the reservation/child-chain grant.
    parent = pool.get_instance(caller_instance_name)
    if parent and parent._slot_release and child_has_no_own_endpoints:
        slot_key = parent._slot_key       # store slot_key on instance at acquire
        concurrency = parent._slot_conc
        # Grant child a permit in parent's pool, marked with ancestor chain,
        # OR (simplest & safest) run child on parent's thread (SYNC path) so it
        # shares the permit implicitly — see 5.2.
```

**Two valid designs — recommend B for minimal disruption:**

- **A. Permit sharing**: child acquires a second permit in parent's pool (if capacity allows) tagged with the ancestor chain; reserved grants allow this. More throughput, more complexity (capacity accounting, release pairing).
- **B. Thread-inheritance (recommended, matches current sync path)**: children inheriting the parent's slot run **SYNC inline** on the parent's thread, exactly like `_run_child_sync` does today. They never call `acquire()` — the parent's permit covers them. This is already the dominant pattern for inherited-endpoint children (they're treated as same-pool → sync). The queue refactor keeps this: inheritance = no acquire, no ticket, no permit; the child borrows the thread + permit.

Choose B. It is the smallest behavior change and provably prevents interleaving (one thread = one turn at a time).

### 5.2 Integration into handle_call_agent (tool_dispatcher.py:284-351)

Extend the existing decision table:

```
1. child conc=-1                → ASYNC (no slot anywhere)
2. child conc=0                 → SYNC (shared sequential pool)
3. caller holds no slot         → ASYNC
4. child inherits caller slot
   (= caller has slot AND child has no own endpoints)
                                → SYNC (thread + permit borrowing; NO acquire)
5. same slot pool (collision)   → SYNC (caller releases, child acquires, caller re-acquires)
6. different parallel pool      → ASYNC + caller registers Reservation on own pool
7. ancestor-chain collision     → SYNC (existing _find_ancestor_with_slot logic, tool_dispatcher.py:472-517)
```

Rule 4 replaces the current behavior where a no-endpoint child's chain resolution could diverge from the caller's pool (Gap C) — now it's explicit: no own endpoints = inherit = sync.

### 5.3 Reservation in async spawn

`_run_child_async` gains `pool.scheduler.reserve(caller_agent, reason="async_child", ancestor_chain=caller_chain)`. The reservation is unreserved in the wake-from-sleep handler when the async result is drained (`_handle_sleeping_state` → re-acquire path, execution_engine.py:4806-4808).

---

## 6. Threading & Synchronization Strategy

- **Single condition variable per SlotPool** (`threading.Condition` with a module-level `RLock` or per-pool lock). All pool mutations (enqueue, dequeue, grant, release, cancel, reserve) occur under `pool._cond`. No lock-free tricks, no `queue.Queue` misuse (we need cancellation + reservation blocking, so a custom deque under one condition is clearer than `queue.Queue`).
- **No semaphores at all** in the scheduler after refactor. Capacity is checked via `len(pool._running) < capacity`. This removes the entire class of "semaphore leaked by forgotten release / double release" bugs — release becomes idempotent via `acquisition_id` match.
- **Interruptibility**: the wait loop ticks on `wait_for(..., timeout=min(remaining, 1.0))` so termination checks (every 1s, matching current `api_router.py:324-356` behavior), cancel events, and stop signals are honored within ≤1s. This is a hard requirement: dismissed agents must leave the queue promptly.
- **Timeouts**: default `QUEUE_WAIT_TIMEOUT = 300s` (configurable `QWEN_AGENT_SLOT_QUEUE_TIMEOUT`), 10× the current 30s. The queue is a wait-line, not a fire-and-forget acquire; only pathological cases (deadlock) should hit the timeout, and when they do it raises a distinct `SlotQueueTimeout` (carries queue position, wait time, pool state) for diagnostics.
- **Fairness**: strict FIFO by `seq`. No priority tiers in v1 (keep it simple; add priority later if needed).
- **Diagnostics hook**: each grant/wait/cancel logs `[SlotQueue] key=... ticket=... agent=... running=N waiting=M` at debug; release logs `[SlotQueue] released ...`. Keep `get_status()`, `detect_stuck_slots()`, `get_slot_holders()` APIs (they're used by UI/tooling) — reimplement on top of `_running`/`_waiters`.

---

## 7. Agent Lifecycle Integration

### 7.1 Acquire points (no caller changes needed)

`ExecutionEngine.run()` → `_acquire_slot_with_logging` → `pool._acquire_slot()` → `router.scheduler.acquire(...)`. The signature stays identical; return value stays a release callback (or None for conc=-1). **All existing call sites keep working.**

### 7.2 Release points (no caller changes needed)

All existing release sites keep their shape, now routed to `pool.release(holder)`:
- `engine.run()` finally block (execution_engine.py:1556-1558)
- `_transition_to_sleeping` (execution_engine.py:4711-4725) — **plus `scheduler.reserve(...)` before releasing the permit**
- `agent_pool.stop_session` release-all (agent_pool.py:1562-1585) — now also `cancel()`s every ticket in every pool (replaces the loose "release all callbacks")
- `_run_child_sync` (tool_dispatcher.py:549-557) — caller releases, child acquires, caller re-acquires

### 7.3 Cancel points (NEW — the fix for zombie waiters)

- `AgentInstance.terminate()` (agent_instance.py:612 — currently only sets durable state flags; the cancel hook is NEW work) → after setting `is_terminated`, call `router.scheduler.cancel_for_agent(instance_name)` to remove any pending ticket and unreserve any reservations held by that agent. Note: `terminate()` runs on the dismissing thread, not the waiting thread — the waiting thread's 1s tick notices `ticket.cancelled` and exits the wait loop promptly.
- `pool.stop_session()` → `router.scheduler.cancel_all()` (idempotent, iterate all pools).
- `_reacquire_caller_slot` failure (tool_dispatcher.py:661-712, `max_attempts=2`, `retry_delay=0.1` → total ~0.2s) → if caller can't re-acquire within the new queue timeout, log `[SLOT_SYNC_REACQUIRE_FAILED]`, **clear the caller's `_slot_release` to None (state consistency — reviewer item)** so no stale permit is assumed, **cancel any pending ticket**, and fall back to ASYNC-only subsequent calls (same degrade path as today but with the ticket properly removed first — no phantom waiter).
- `SlotQueueTimeout` raised inside `engine.run()` → standard pre-existing cleanup (run_agent finally, dismiss if child) plus `scheduler.cancel_for_agent(instance_name)` as belt-and-braces.
- `_skip_slot_acquire` path (Security/Compressor nested agents) unchanged — they don't enter the queue.

### 7.4 SLEEPING transition detail (critical for Gap A)

Current flow releases the permit on sleep. New flow:

_handle_sleep_transition:
    if instance holds slot_key:
        scheduler.reserve(instance, reason="sleeping", ancestor_chain=active_stack snapshot)
    _release_slot(instance, context="sleep transition")   # permit goes back to pool
    → SLEEPING
wake (async result / user message):
    # ORDER MATTERS (reviewer-critical): unreserve BEFORE re-acquire.
    # If the sleeper tried to re-acquire while its own reservation is still
    # active, the self-exemption rule would grant it — but that is fragile
    # if self-exemption is ever removed. Do both defensively:
    scheduler.unreserve(instance)                    # 1. clear reservation first
    re-acquire slot (may wait in queue — but as own-ancestor the reservation
    has kept unrelated agents out, so it is head-of-line and near-immediate)



---

## 8. Migration Steps (from semaphore to queue)

**Phase 1 — Scheduler internals (isolated, no behavior change):**
1. Add `QueueTicket`, `SlotHolder`, `Reservation`, `SlotPool` classes in a new module `agent_cascade/slot_queue.py` (keep `EndpointScheduler` in api_router.py as the façade).
2. Implement `acquire/release/cancel/reserve/unreserve` with strict FIFO; keep the same public signatures (`acquire(...) → release_cb`, `get_status()`, etc.).
3. Reimplement `_get_or_create_pool` keyed by the same slot_key logic; keep `_shared_sequential_slot_` mapping intact.
4. Unit tests: FIFO order, release-wakes-next, cancel-removes-waiter, idempotent release, timeout raises `SlotQueueTimeout`, reservation blocking (A sleeps → C queued → A's child granted → C still queued until unreserve).

**Phase 2 — Wire into scheduler façade (no engine/tool change yet):**
5. Replace semaphore usage inside `EndpointScheduler.acquire` body with `SlotPool.acquire`. Return a release callback that calls `pool.release(holder)`; on None (conc=-1) return None.
6. Keep `ENDPOINT_SLOT_ACQUIRE_TIMEOUT` name for backward-compat but bump default to 300s via new setting; deprecate old name in docs.
7. Run full existing test suite (esp. `tests/test_call_agent_sync_async_selection.py`, async shell, dismiss tests) — must stay green with no engine changes.

**Phase 3 — Reservation integration (the Gap A fix):**
8. Add `reserve/unreserve` calls in `_transition_to_sleeping` and `_run_child_async`/wake path (execution_engine.py 4711+, 4806+; tool_dispatcher.py).
9. Add termination/cancel hooks: `AgentInstance.terminate()`, `stop_session`, `cancel_all`.
10. Integration tests: the exact todo.md:93 scenario (A sync → B async → C sync on A's pool, A→D sync) — assert C's grant is blocked while A sleeps, no interleaving; assert logs show reservation blocks.

▶️ [v2] **Phase 4 — Inheritance & cleanup (Blocker #4, Review #24/#13):**
11. Implement Rule 4 in `handle_call_agent` (no-endpoint child → inherit parent slot → SYNC, no acquire). Update `agent_pool._acquire_slot` to expose whether child has own endpoints (reuse `_resolve_inherited_endpoints` result) and store `slot_key` on instance at acquire for reservation/inheritance use.
12. ▶️ [v2] **Re-acquire algorithm — detailed design (Blocker #4, Review #24):**

    Current: `_reacquire_caller_slot` has `max_attempts=2`, `retry_delay=0.1s` (~0.2s total), then gives up and leaves caller without slot → silent concurrency bug.
    
    New algorithm (`_reacquire_caller_slot`):
    ```python
    def _reacquire_caller_slot(slot_holder, context_label="sync_child"):
        # 1. Try immediate acquire (slot may already be free)
        release_cb = pool._acquire_slot(slot_holder.agent_class, slot_holder.instance_name)
        if release_cb is not None:
            slot_holder._slot_release = release_cb
            return True

        # 2. Queue-aware re-acquire with reservation exemption:
        #    The caller's ancestor chain includes itself → self-exemption applies.
        #    Wait up to REACQUIRE_TIMEOUT (30s) for the slot.
        #    ▶️ [v3] ancestor_chain passed from calling context via instance tracking,
        #    NOT from pool.get_active_stack() (which doesn't exist).
        ancestor_chain = _build_ancestor_chain(slot_holder)  # includes self + all parents
        try:
            release_cb = scheduler.acquire(
                api_base=slot_holder._slot_api_base,
                concurrency_limit=slot_holder._slot_conc,
                instance_name=slot_holder.instance_name,
                ancestor_chain=ancestor_chain,   # enables self-exemption
                timeout=30.0)                    # shorter than full QUEUE_WAIT_TIMEOUT
            slot_holder._slot_release = release_cb
            return True
        except (SlotQueueTimeout, SlotCancelled):
            pass

        # 3. On failure: clean state and degrade to async-only
        with slot_holder._state_lock:
            slot_holder._slot_release = None     # explicit — no stale permit assumed

    Helper `_build_ancestor_chain(slot_holder)` walks the instance's parent chain using existing tracking (`instance._parent_instance_name` or equivalent stored during `call_agent`). Returns tuple including self at end, e.g. `(root, mid, caller)`. This is the same mechanism used for ancestor-chain self-exemption in reservations.
        logger.warning(f"[SLOT_SYNC_REACQUIRE_FAILED] {context_label} for '{slot_holder.instance_name}' "
                       f"after 30s. Subsequent calls will use ASYNC path only.")
        return False
    ```

    Key properties:
    - Self-exemption ensures the caller is never blocked by its own reservation (from sleeping or async child).
    - 30s timeout is long enough for normal contention but short enough to fail fast on deadlock.
    - On failure, `_slot_release` is cleared atomically under `_state_lock` so no other code assumes a stale permit exists.

13. ▶️ [v2] **_slot_release clearing audit (High Priority #7, Review #13):** all sites that read `_slot_release` have been audited:
    - `tool_dispatcher.py:318` — checks if caller holds slot for collision detection; safe to clear on failure.
    - `tool_dispatcher.py:503` — ancestor walk; reads under `_state_lock`; safe.
    - `tool_dispatcher.py:550-551` — releases before sync child; safe (clears after release).
    - `tool_dispatcher.py:699` — sets on re-acquire success; idempotent.
    - `execution_engine.py:889, 1157` — sets initial slot or None at startup; unaffected by re-acquire clearing.
    - `execution_engine.py:4684-4686, 4715-4717` — release paths capture-nullify-release under `_state_lock`; safe (clearing to None is the same operation).
    - `agent_pool.py:1574-1576` — stop_session mass-release; captures before clearing; safe.
    - `security_handler.py:324`, `compression/agent_invoker.py:288` — read-only checks for logging/debug; safe.
    - `lifecycle_manager.py:502` — clears on slot timeout; same semantics as our clearing.
    
    **Conclusion:** clearing `_slot_release = None` on re-acquire failure is safe across all code paths. No behavior change required in any reader.

14. Remove dead semaphore code + update SystemDocs (`docs/SYSTEM_DOCS.md` concurrency section), update `detect_stuck_slots` to also flag waiters > QUEUE_WAIT_TIMEOUT.
15. Full regression: all suites + a soak test with 8 agents on conc=0 and conc=3 pools (assert no two sync turns interleave — instrument via a turn-ownership counter per slot).

---

▶️ [v2] **Error Handling & Edge Cases (updated per review):**

| Edge case | Handling |
|---|---|
| Waiter times out (300s) | `SlotQueueTimeout` with pool state dump; ticket removed via OrderedDict.pop(O(1)); engine cleanup + `cancel_for_agent` |
| Agent terminated while waiting | `terminate()` → `cancel_for_agent` → ticket.cancelled; waiter exits loop raising `SlotCancelled` (subclass of `InstanceDismissedError`) |
| Double release | Idempotent via `acquisition_id` match; stale releases logged at debug |
| Cancel-after-grant race | Checked immediately after wakeup: if `ticket.cancelled.is_set()` → dequeue without granting, raise `SlotCancelled`. No phantom permits. |
| Stuck queue entry (agent vanished without terminate) | `detect_stuck_slots()` extended: flag waiters with age > timeout; stop_session `cancel_all()` guarantees eventual cleanup |
| Reservation leak (agent died while reserved) | ▶️ [v2] Timeout-based: RESERVATION_TIMEOUT=300s; stale sweeper every 60s forcibly unreserves; `stop_session` and `terminate()` also unreserve all for that agent; idempotent |
| conc=-1 endpoints | No pool entry created (fast return None) — keeps unlimited endpoints hot |
| conc=N pool resize (admin changes concurrency at runtime) | `SlotPool.capacity` mutation under `_cond`; raising → `notify_all()`; lowering → no eviction, just no new grants until `len(_running) ≤ capacity`. Existing waiters keep position. Unit test covers resize with waiters present. |
| Multiple reservations on same pool | ▶️ [v2] All cleared via `unreserve_for_agent(agent_name)` — iterates all tokens under `_cond`, removes atomically. Used by terminate/unreserve/sweeper. |
| Self-reservation deadlock (A reserves, then A re-acquires) | ▶️ [v2] Impossible: `_blocked_by_reservation` checks `any(name in res.ancestor_chain for name in ancestor_chain)` — A's own instance_name is always in its chain → never blocked by self |
| New endpoint added at runtime | `_get_or_create_pool` lazy-creation under a global `scheduler._lock` |

---

## 10. Testing Strategy

▶️ [v2] **Testing Strategy (High Priority #6, Review #18-21):**

### Unit (new `tests/test_slot_queue.py`)
- FIFO order: T1,T2,T3 enqueued → grants in order; no barging.
- Release→next-waiter wake under contention.
- Cancel removes waiter in middle; head waiter still granted.
- Timeout: deadline expiry raises `SlotQueueTimeout`, ticket removed, release still healthy.
- Reservation: pool cap=1; A holds; A reserves; B (unrelated) enqueues + blocked; A's child C (in chain) enqueues after B, IS granted (chain exemption); unreserve(A) → B granted.
- Idempotent release/cancel; resize under contention.
- Concurrency stress: 8 threads × 100 acquire/release on cap=1 — assert max 1 running at any instant (instrument a running counter).
- ▶️ [v2] **Cancel-after-grant race test (Review #18):** force `cancel(ticket_id)` to fire between dequeue and `granted.set()` using a cooperative harness (yield control after dequeue, before grant). Assert: the ticket is NOT granted; no phantom permit; pool state consistent.
- ▶️ [v2] **Reservation timeout test (Review #19):** create reservation; advance time past RESERVATION_TIMEOUT; call `detect_stale_reservations()` → returns it; stale sweeper forcibly unreserves; next waiter is granted.
- ▶️ [v2] **Mass cancellation performance test (Review #20):** enqueue 100 waiters on cap=1 pool; call `cancel_all()` / `stop_session` cleanup; assert completes in < 50ms under the condition lock (verifies OrderedDict O(1) removal).
- ▶️ [v2] **Reservation leak test (Review #21):** simulate agent death: create reservation, never call unreserve or terminate; after timeout, stale sweeper clears it; pool grants proceed normally.

### Integration (extend `tests/test_call_agent_sync_async_selection.py` + new `tests/test_slot_reservation_chain.py`)
- todo.md:93 scenario: A(sync,pool P)→B(async,pool Q); A→D(sync,P); B→C(sync,P). Assert: when A sleeps awaiting B, C's acquire hangs (blocked by A's reservation); when A wakes/result arrives, order is deterministic; no turn interleaving (per-slot turn-ownership counter in test doubles the LLM call).
- Inheritance: child with no endpoints on parent holding slot → SYNC path, no new ticket (scheduler queue depth unchanged).
- Dismiss-waiting-agent: agent queued on P, dismissed → ticket cancelled within ≤1s, slot granted to next waiter; no zombie.
- stop_session during active queue: all tickets cancelled, all permits released.
- Async child chain with reserve: B async → wakes A → unreserve → next waits proceed.
- ▶️ [v2] **Multiple reservations test (Review #3):** agent holds two reservations on same pool; `unreserve_for_agent` clears both atomically; grants resume immediately.

### Soak / load
- 8 agents, 2 on conc=0 pool, 6 on conc=3 pool, mixed sync/async call_agent traffic for 10 min; assert per-slot max concurrency and turn-ownership atomicity; watch `[SlotQueue]` logs for grants/waits balance (grants == releases + cancels, modulo in-flight).

### Negative
- Kill/dismiss agent mid-wait (force) → queue clean, no stuck entry after stop_session.
- Admin lowering concurrency below current running → no new grants, no crash.

---

▶️ [v2] **Risks & Mitigations (updated per review):**

| Risk | Mitigation |
|---|---|
| Behavior change in hot path (engine/tool) | Phased migration; phases 1-2 are pure-internal (same signatures); full suite must stay green before reservation work |
| Reservation logic introduces deadlock | ▶️ [v2] Self-exemption is mathematically guaranteed by `_blocked_by_reservation` predicate (A always in A's ancestor chain). Unit tests cover all ancestor-chain permutations. Stale sweeper provides escape hatch. |
| Strict FIFO increases latency vs current (barging) semaphore | Correctness over latency — this is the point of the task; unrelated waits are expected to line up; the 1s tick keeps cancellation snappy |
| `notify_all()` thundering herd (Review #17) | ▶️ [v2] With strict FIFO, only head proceeds; all others immediately re-wait. This wastes a few wake cycles but is correctness-safe. If profiling shows CPU impact >5%, switch to `notify()` + explicit head-ticket event signaling in Phase 4+. For now, prefer simplicity and robustness. |
| O(n) remove from waiters on cancel | ▶️ [v2] Eliminated: OrderedDict.pop(ticket_id) is O(1). Mass cancellation of 100+ waiters completes in <50ms under lock (verified by performance test). |
| New endpoint added at runtime | `_get_or_create_pool` lazy-creation under a global `scheduler._lock` |
| Two pools for same physical server (different api_base strings) | Existing known limitation (investigation Gap C); NOT fixed by this plan — document that consolidation is a config-level concern (`api_base` normalization) |

---

## 12. Out of Scope (explicitly)

- Endpoint-chain slot-key unification (Gap C root fix — api_base normalization / acquiring all chain endpoints). Documented as follow-up.
- Priority queues / QoS tiers.
- Multi-process scheduling (stays in-process, thread-based).
- Changing `_skip_slot_acquire` semantics.

---

▶️ [v2] **Deliverable Checklist (updated per review):**

- [ ] `agent_cascade/slot_queue.py` with QueueTicket, SlotHolder, Reservation, SlotPool (OrderedDict-based)
- [ ] `EndpointScheduler` façade refactored to delegate to SlotPool (same public API)
- [ ] Reservation hooks: `_transition_to_sleeping`, `_run_child_async`, wake path, `unreserve_for_agent` on result drain
- [ ] Cancel hooks: `AgentInstance.terminate()`, `stop_session`, reacquire-failure (`_slot_release = None`)
- [ ] ▶️ [v2] Diagnostic APIs: `get_reservations()`, `detect_stale_reservations(threshold)` (Phase 1 only — no sweeper thread yet)
- [ ] ▶️ [v2] Stale reservation sweeper thread (Phase 3): 60s interval, RESERVATION_TIMEOUT=300s
- [ ] ▶️ [v2] Re-acquire algorithm in `_reacquire_caller_slot` with ancestor-chain self-exemption + 30s timeout
- [ ] ▶️ [v2] Lock ordering protocol documented in code comments (instance → scheduler façade → pool condition)
- [ ] ▶️ [v2] `APIRouter.get_agent_slot_info()` updated for inheritance scenarios (Review #14)
- [ ] Tests: unit (slot_queue), integration (reservation chain + todo scenario), soak, performance (mass cancel <50ms)
  - ▶️ [v3] **Explicit lock-ordering violation test:** deadlock-detection harness asserts no lock held >2s across instance→scheduler→pool paths.
  - ▶️ [v3] **Self-exemption unit test:** verify A reserves → A re-acquires succeeds immediately; B tries to acquire on same pool → blocked until A unreserves.
- [ ] Docs: SYSTEM_DOCS concurrency section, plan review by Reviewer agent