# Streaming Backlog Fix Plan — Independent Review

**Reviewer:** Senior QA & Review Specialist  
**Date:** 2026-08-31  
**Input:** `N:\work\WD\AgentCascade\reports\streaming_backlog_FIXPLAN.md` + live probe logs  

---

## Executive Verdict: NEEDS WORK

The plan correctly identifies the **core mechanism** (consumer stall → queue pin → FIFO drops), but several proposed fixes are either incomplete, risky, or misaligned with production realities. **Fix C is fundamentally flawed as currently described and must be rejected or radically redesigned.** Fix A has serious async correctness hazards. Fix B requires thread-safety proof. Fix D is a band-aid that masks rather than cures.

**Minimal viable fix set for production:** **A (with caution) + B (with safeguards)**. C is not needed if A and B work; if added, it must be redesigned as a safe coalescing layer. D can be a minor tuning parameter only after A+B are proven effective.

---

## 1. Root Cause Verification — What Holds, What’s Wrong

### ✅ Correctly Diagnosed
| Claim | Evidence | File:Line |
|-------|----------|-----------|
| Single bounded queue `maxsize=128` | Probe logs show qsize pinned at 127 for minutes | `api_server.py:420` + `logs/stream_probe_backend.log` |
| Single consumer `_sender_loop` | Created once on startup, no other drainers | `api_server.py:700`, `729-746` |
| Producer silently drops on full | `put_nowait()` catches `QueueFull` → `pass` | `streaming.py:127-129` |
| Producer is real-time (`yield_to_enqueue_ms≈0`) | Probe logs show near-zero delays even when qsize=127 | `logs/stream_probe_backend.log` (many lines) |
| Settings toggle injects heavy frame on same saturated queue | `handle_update_config` → `await self._broadcast()` → `build_state()` | `ws_handlers.py:683`, `102-109`, `api_server.py:461` |
| `broadcast()` is serial and unbounded | Loop over `snapshot` with `await conn.send_text(text)` no timeout | `api_server.py:610-627` |

### ❌ Questionable Assumptions

#### 1. “build_state() is a heavy CPU burst on the event loop”
**Status:** **TRUE BUT INCOMPLETE.** `build_state()` calls `_serialize_all_instances()` which serializes all agent instances, messages, telemetry. It is indeed O(N) and can easily take tens to hundreds of ms. However, the plan says it runs *on the event loop*. Is that correct?

Let’s trace:
- `handle_update_config` (line 683) is an async handler running on the **WebSocket server loop**.
- It calls `await self._broadcast()` which awaits `self.broadcast_fn(...)`.
- `broadcast_fn` is `api_server.broadcast` (registered as WS handler).
- That calls `build_state()` **synchronously inside the same async context** before `put_nowait`.

So yes: `build_state()` runs on the event loop, blocking it. **However**, the plan doesn’t address the fact that `_sender_loop` and `handle_update_config` are both awaiting on the *same* loop. When `build_state()` blocks, `_sender_loop` cannot run. That’s the pinning mechanism.

#### 2. “The consumer (`_sender_loop`/`broadcast`) stalls”
**Status:** **TRUE.** But the plan doesn’t distinguish two stall types:
- **CPU-bound stall:** `build_state()` occupies the loop, `_sender_loop` waits for CPU.
- **I/O-bound stall:** `send_text` to a slow client blocks on network I/O.

Both cause qsize to pin, but they require different fixes. The plan conflates them under “consumer stalls” and proposes A for I/O and B for CPU. That’s correct in principle, but the implementation details matter.

#### 3. “Single-consumer assumption — truly only one `_sender_loop`?”
**Status:** **TRUE.** Only one task created at startup (`api_server.py:700`). No other drainers. The probe logs confirm qsize behaves as a single-queue metric.

#### 4. Is there a deadlock/lock-hold in `broadcast` or `build_state`?
**Status:** **NOT EVIDENCED.** The plan assumes no lock, but `build_state()` accesses `pool.instances` which may have locks. Let’s check:

- `pool.instances` is a dict protected by `pool._instances_lock`? Actually, in `agent_pool.py`, I see `self.instances` is a plain dict. Access is typically guarded by `self._execution._state_lock` or other locks during mutation, but reads are **unprotected**. If `_sender_loop` runs while an instance is being mutated (e.g., during generation), there could be race conditions, but not deadlocks.

- `broadcast()` uses a snapshot (`frozenset(ws_connections)`) to avoid RuntimeError. That’s good. But if a connection is closed mid-send, the exception is caught and the conn discarded. No deadlock.

**Root cause verdict:** The diagnosis is **solid enough for planning**, but missing nuance: **two distinct stall modes (CPU vs I/O)** need separate fixes. The plan treats them as one problem with one solution set. That’s why C is being considered—it tries to make the system tolerate drops, but that doesn’t fix either stall.

---

## 2. Fix A — Concurrent Gather with Per-Send Timeout

### What the Plan Proposes
- Replace serial `for conn in snapshot: await conn.send_text(text)` with concurrent `asyncio.gather(*[send_to(conn) for conn in snapshot])`
- Add per-send timeout (e.g., `asyncio.wait_for(conn.send_text(text), timeout=5.0)`)
- Reap dead connections on error

### 🔴 Critical Issues

#### 2.1 Starlette’s `send_text` Does Not Support Clean Per-Call Timeout
Starlette’s `WebSocket.send_text()` is a simple wrapper around the transport layer. It does **not** have a timeout parameter. To add one, you’d need:

```python
await asyncio.wait_for(websocket.send_text(text), timeout=5.0)
```

But this **cancels the entire send operation**, leaving the WebSocket in an undefined state. The connection may be half-closed. You must then call `websocket.close()` to clean up. If the send is mid-protocol, you risk protocol errors.

**Risk:** A cancelled send could leave `ws_connections` in a weird state, causing subsequent sends to fail or leak resources.

#### 2.2 Snapshot Race Condition
The plan uses `snapshot = frozenset(ws_connections)`. But if a connection is closed *after* snapshot but *before* its `send_text`, you get an exception. The current code discards it. With `gather`, exceptions from any task propagate and may cancel other sends prematurely unless carefully handled with `return_exceptions=True`.

**Recommendation:** Must use `asyncio.gather(..., return_exceptions=True)` and then filter out dead conns *after* the gather, not during. But even then, a connection that errors mid-send might still be in `ws_connections` for a brief window, causing duplicate errors.

#### 2.3 Ordering Guarantees
**Critical:** The current design sends **all frames to all clients in FIFO order per client**. With concurrent gather across *clients*, the order of delivery **between clients** is nondeterministic (which is fine), but **per-client order is preserved** because each client gets one send task.

However, if a single client has multiple pending frames in the queue, the _sender_loop processes them serially: Frame1 → broadcast → Frame2 → broadcast. With concurrent gather, Frame1 and Frame2 are sent to clients in overlapping windows. Could this cause interleaving? No, because each frame is a separate `broadcast` call; the queue ensures frame ordering at the source.

But there’s a subtlety: if `send_text` to Client A for Frame1 is slow, and we use `gather`, we might wait for all clients to receive Frame1 before moving to Frame2. That’s actually **worse** than serial if one client is very slow—the whole gather is blocked by the slowest client unless we give each send its own timeout and let it fail early.

#### 2.4 Exception Handling Complexity
Current code catches per-send exceptions and discards the conn. With `gather`, you must handle multiple exceptions: which ones are retryable? Which indicate a dead connection? The logic gets messy quickly.

### 🟠 Recommendation for Fix A
**KEEP but MODIFY heavily:**
- Do **not** use `asyncio.wait_for` with cancellation. Instead, wrap each send in a `try/except` with a **separate asyncio shielded task** that runs the send with a timeout *using a separate cancel scope* (like `anyio` or custom). But plain asyncio doesn’t support partial cancellation cleanly.
- Better approach: Use `asyncio.wait_for` **but ensure you close the connection on error** and use `return_exceptions=True`. Then after gather, iterate over results and discard any connections that had errors.

Pseudo-code:

```python
async def send_with_timeout(conn, text, timeout=5.0):
    try:
        await asyncio.wait_for(conn.send_text(text), timeout=timeout)
        return conn, None
    except (asyncio.TimeoutError, Exception) as e:
        try: await conn.close()
        except: pass
        return conn, e

tasks = [send_with_timeout(conn, text) for conn in snapshot]
results = await asyncio.gather(*tasks, return_exceptions=True)
for conn, err in results:
    if err is not None:
        ws_connections.discard(conn)
```

**Risk:** Still medium. The connection close may fail if already closed. Must be robust.

**Verdict:** **MODIFY** — Implement with careful error handling and timeout semantics. Do not ship unmodified concurrent gather.

---

## 3. Fix B — Off-Loop `build_state()` + Coalescing

### What the Plan Proposes
- Run `build_state()` in a thread pool (executor) so it doesn’t block the event loop.
- Optionally coalesce state broadcasts during streaming (only send on config change, not every tick).

### 🟠 Thread Safety of `pool.instances` Reads
`build_state_from_pool` reads `pool.instances`, `instance.conversation`, etc. Are these thread-safe?

Let’s examine `agent_pool.py` and `instance.py`:
- `self.instances` is a plain dict. Mutations happen via `self.instances[name] = instance` inside `self._instances_lock`. But reads are **unprotected**.
- `instance.conversation` is a list. It is modified during generation (via `self._lock` or `self._execution._state_lock`). Reads in `build_state_from_pool` are **not atomic** with respect to writes.

**Risk:** A `build_state()` running in a thread could read a partially updated conversation list, leading to inconsistencies or even crashes if the list is being resized. However, Python’s GIL provides some protection for simple reads, but not for complex data structures.

**Mitigation:**
- Take a snapshot of `pool.instances` under its lock **before** releasing to the thread. That’s what `build_state_from_pool` already does: `instance_snapshot = dict(pool.instances)` — but this is still unprotected if called from the loop. If we move it to a thread, the read happens under no lock.
- Better: Provide a thread-safe snapshot method on AgentPool that acquires the lock and returns a copy.

**Verdict:** **MODIFY** — Do not run raw `build_state()` in executor without explicit snapshotting under lock. Either:
1. Add `pool.take_snapshot()` that returns a deep copy of instances under lock, then have `build_state_from_pool` accept a pre-snapshotted dict.
2. Or ensure `build_state_from_pool` uses the existing snapshot pattern (it already does `dict(pool.instances)` but that’s not thread-safe).

### Coalescing State Broadcasts
The plan says: “Make settings-toggle state broadcast cheap/non-blocking during streaming.” If we run `build_state()` off-loop, it still generates a big frame and enqueues it. That doesn’t solve the queue saturation issue; it only removes the CPU block. The big frame still competes with stream updates for queue space.

**Recommendation:** Coalesce state broadcasts with stream updates, but **not by dropping them**. Instead, use a separate high-priority channel for config changes, or ensure that when a config change occurs during streaming, we send a minimal delta rather than full state. But the current `build_state()` always returns full state.

**Verdict:** **MODIFY** — Off-loop execution is necessary but not sufficient. Need to also reduce the size of state broadcasts during streaming (e.g., send only changed fields). However, that’s a larger refactor beyond the current plan.

---

## 4. Fix C — Last-Writer-Wins Coalescing (Hardest Scrutiny)

### What the Plan Proposes
When queue is near full, drop intermediate stream frames and keep only the latest per instance (LWW). Instead of FIFO drops that lose the newest, we keep the newest.

**User’s uncertainty:** “Does last-writer-wins risk losing important frames (len_changed / force-full-refresh / tool events)? Is it safe given the client relies on cumulative state? Could it cause flicker or lost messages?”

### 🔴 Critical Flaws in Fix C

#### 4.1 Frame Types That MUST NOT Be Coalesced Away
The current streaming protocol sends `type='stream_update'` with fields like:
- `instance_name`, `message_index`, `content`, `is_delta`
- `len_changed`: boolean indicating new committed messages
- `force_full_refresh`: triggered every 100 ticks to resync

If we apply LWW per instance, we risk:
1. **Dropping a `len_changed` frame** that signals a new message was added. The client may miss the fact that a message arrived, breaking UI consistency.
2. **Dropping a `force_full_refresh` frame**. The client will not resync and may drift further.
3. **Dropping tool event frames** (e.g., `tool_call`, `approval_request`). These are critical and must be delivered in order.
4. **Dropping dismissal signals** (`type='dismissal'`). If a user dismisses an agent during streaming, that signal must be delivered immediately to update UI.

**The plan doesn’t differentiate frame types.** It says “keep only the latest per instance.” That’s too blunt.

#### 4.2 Cumulative State Assumption
The frontend assumes **cumulative, append-only messages**. If we drop intermediate stream updates, the client may miss partial content. For example:
- Frame1: “Hello” (partial)
- Frame2: “World” (partial)
If we drop Frame1 and only send Frame2, the client sees “World” without “Hello” → broken sentence.

**LWW only makes sense if each frame is self-contained and replaces previous state.** But stream updates are deltas that **build on prior frames**. The client renders by appending content. Dropping earlier deltas loses text.

#### 4.3 Queue Full Behavior Is Already Drop-Stale
The current `_put_stream_update` drops the event silently when full. That’s already “drop stale” — but it may drop **older** frames, not newer ones. The plan says LWW would drop intermediate and keep newest. But under FIFO queue with `put_nowait`, if the queue is full, the **newest** event gets dropped (because you can’t enqueue it). So the current behavior is actually **drop-newest-on-full**, not drop-oldest.

Wait: Let’s re-check. `asyncio.Queue` with `put_nowait`:
- If queue is full, `put_nowait` raises `QueueFull`.
- The code catches it and does nothing. So the **newest** frame is lost.
- The queue still contains older frames.

So the plan’s statement “FIFO drops that lose the newest” is **misleading**. Actually, FIFO queue with `put_nowait` drops the **newest** when full. If we wanted drop-oldest, we’d need to manually `get()` before `put()`. But they propose “keep newest, drop intermediate” — which would require a different data structure (e.g., a ring buffer or per-instance latest slot).

**So what is Fix C really proposing?** It seems to propose: when queue is near full, instead of dropping the new event, we **replace** an existing older event for the same instance with the new one. That would require scanning the queue for old events from that instance and removing them before enqueueing the new one. But `asyncio.Queue` doesn’t support removal of arbitrary elements. You’d need a custom queue.

That’s a **massive refactor** and introduces concurrency issues.

#### 4.4 Implementation Complexity
To implement LWW properly:
- Need a custom queue that tracks per-instance latest frame.
- On enqueue, check if there’s an older frame for same instance; if so, remove it.
- Must handle edge cases: what about frames from different instances? What about `len_changed` vs content updates?
- This is non-trivial and error-prone.

### 🟠 Alternative: Prioritize Critical Frames
Instead of LWW coalescing, a safer approach:
- **Mark critical frames** (dismissal, tool events, force-full-refresh) as high-priority.
- Use **two queues**: one for streaming updates, one for critical messages. `_sender_loop` drains critical first, then streaming.
- When streaming queue is full, drop **only low-priority** stream updates, never critical ones.

This preserves correctness while preventing starvation.

### Verdict on Fix C
**REJECT as described.** The LWW coalescing proposal is fundamentally incompatible with the delta-based streaming protocol and would cause data loss and UI corruption. If the team still wants to reduce queue pressure, implement **priority queuing** instead.

---

## 5. Fix D — Raise Queue Bounds + Backpressure

### What the Plan Proposes
Increase `maxsize` from 128 to maybe 512 or 1024, and/or make producer await with a short timeout to backpressure.

### 🟡 Analysis
- **Increasing maxsize** simply delays the drop. If the consumer is stalled, the queue will eventually fill again, just later. It doesn’t fix the stall; it just increases buffer latency.
- **Backpressure** (making producer `await queue.put()` with timeout) would slow down the agent thread if the queue is full. That could actually **worsen** the stall by making the producer wait for the consumer, but if the consumer is blocked (e.g., on I/O), the backpressure will cause the agent thread to sleep, reducing CPU load. However, it doesn’t prevent the queue from filling; it just shifts the bottleneck.

**Risk:** Larger buffers mean more memory and potentially staler UI (more frames buffered while consumer is stalled). Not a cure.

### Verdict
**REJECT as a standalone fix.** Can be used as a minor tuning parameter after A+B are in place, but not a solution.

---

## 6. Ordering & Completeness — Is A+B Enough?

### What A+B Address
- **A:** Prevents one slow client from blocking the drain → solves I/O stall.
- **B (off-loop build_state):** Prevents CPU-bound blocking during settings toggle → solves CPU stall.

Together, they should eliminate the observed qsize pinning in both scenarios.

### Missing Pieces
1. **What about a completely dead client that never closes?** The `broadcast` loop will keep trying to send to it, potentially blocking indefinitely even with timeout if the TCP stack is stuck. A+B don’t fully solve this; you need connection health checks and aggressive reaping.
2. **What about multiple slow clients?** Concurrent gather could still be blocked by the slowest if timeouts aren’t set properly. But with `return_exceptions=True`, it’s manageable.
3. **What about the size of state broadcasts?** A large full-state frame can saturate the queue for a long time even if sent off-loop. The plan doesn’t address compression or delta encoding for state updates.

### Root-Cause Correction
The true root cause is **not just “consumer stalls”** but **“the single drain loop is the sole point of failure for both CPU and I/O”**. A better architecture would be:
- **Multiple sender tasks** per client group (sharding).
- **Per-client send buffers** to isolate slow clients.
- **Priority queue** for critical messages.

But that’s out of scope for this fix plan.

---

## 7. Recommended Minimal Implementation Order

### Phase 1 — Core Stability (Must Ship)
1. **Fix A (modified):** Implement concurrent broadcast with per-call timeout and robust error handling. This is the single most impactful change.
2. **Fix B (modified):** Move `build_state()` off the event loop using a thread executor, but ensure thread-safe snapshots (add `pool.take_snapshot()` or protect reads).

**Testing:** Reproduce the settings-toggle stall with both fixes in place. Verify qsize no longer pins at 127. Measure `yield_to_enqueue_ms` during toggle.

### Phase 2 — Hardening (Should Ship)
3. **Add priority queue** for critical messages (dismissal, tool events). This prevents them from being dropped under pressure.
4. **Add connection health checks** (ping/pong) to detect and drop dead clients proactively.

### Phase 3 — Optimization (Nice to Have)
5. **Reduce state broadcast size** (send only changed fields or compressed diffs).
6. **Tune queue maxsize** based on metrics after A+B are proven.

### Skip / Reject
- **Fix C as described:** Do not implement LWW coalescing. If needed, implement priority queuing instead.
- **Fix D alone:** Not a solution.

---

## 8. Summary of Verdicts

| Fix | Verdict | Reason |
|-----|---------|--------|
| **A** | **MODIFY** | Concurrent gather is sound in principle but requires careful timeout handling, snapshot race management, and exception filtering. Do not ship unmodified. |
| **B** | **MODIFY** | Off-loop execution is necessary but must be paired with thread-safe snapshots. Also consider reducing frame size. |
| **C** | **REJECT** | LWW coalescing breaks delta-based streaming, risks losing critical frames, and is complex to implement correctly. Use priority queuing instead. |
| **D** | **REJECT** (standalone) | Only masks the symptom; doesn’t fix stall. Can be used as tuning after A+B. |

---

## 9. Required Changes Before Plan Approval

1. **Rewrite Fix A design** with explicit error handling, timeout semantics, and snapshot race mitigation.
2. **Define thread-safety guarantees** for Fix B: either protect `pool.instances` reads or provide a snapshot API.
3. **Replace Fix C** with a priority queue design document.
4. **Add performance tests** that simulate slow clients and settings toggles, measuring qsize and latency before/after.

**Final verdict:** The plan is **NEEDS WORK**. The core insight is correct, but the proposed fixes require significant refinement to be production-ready. Prioritize A+B with the modifications above.

---

*End of Review*

---

## v2 Final Verdict: PASS (with implementation cautions)

### A. Fix A — Concurrent gather + per-send timeout + close-on-error
**Status: SAFE TO SHIP**

The revised design addresses all my flagged hazards:

1. **`asyncio.wait_for` + `close()` pattern is acceptable.** Starlette’s `send_text` has no native timeout, and `wait_for` cancellation with subsequent `close()` is the standard asyncio idiom for bounded I/O. The risk of half-open connections is mitigated by the best-effort `close()` call and post-gather discarding.

2. **No remaining ordering/exception hazards** beyond those already handled:
   - `return_exceptions=True` ensures one slow client doesn’t cancel others.
   - Per-client FIFO preserved (one task per client).
   - Snapshot race handled by catching exceptions and discarding dead conns after gather.
   - Exception filtering works because `send_with_timeout` returns `(conn, err)` instead of raising.

**Must-verify before shipping:** Ensure `send_with_timeout` catches **all** relevant exceptions (e.g., `WebSocketException`, `ConnectionResetError`, `OSError`). Use a broad `except Exception` to be defensive.

### B. Fix B — Off-loop `build_state_from_pool` via executor
**Status: THREAD-SAFE AS VERIFIED — NO NEW API NEEDED**

My earlier concern about unprotected reads was based on incomplete code inspection. Upon verification:

1. **Top-level `dict(pool.instances)` (state_builder.py:279)** is a GIL-atomic shallow copy of references. Concurrent adds/removes may cause a snapshot to be slightly stale, but never torn or crashing. This is acceptable for UI state.

2. **Per-instance data access** under `inst._compression_lock` (lines 84, 167, 832-834) ensures conversation and streaming responses are read atomically.

3. **Other pool reads** (`pool.settings`, `pool.agents`, `pool._execution.active_stack`) are either stable after startup, protected by locks (`active_stack` uses `_stack_lock`), or accept stale values (settings). No race conditions that would crash the process.

4. **No need for `take_snapshot()` API**: The existing snapshot pattern is sufficient. Offloading the entire `build_state_from_pool` call to an executor is safe and achieves the goal of moving O(N) CPU work off the event loop.

### C. Both stall modes addressed?
**Yes.**

- **I/O stall (slow client)** → Fix A prevents a single client from blocking the drain.
- **CPU stall (settings toggle → `build_state()` on-loop)** → Fix B moves the heavy serialization to a thread executor, unblocking `_sender_loop`.

Together they eliminate the observed qsize pin at 127 during the user’s reproducible trigger.

### D. Verification plan sufficient?
**Yes.** The plan covers:
- Simulated slow client (stress test A)
- Live toggle during streaming (reproduce B)
- Regression checks (ensure correctness preserved)

If after implementation `yield_to_enqueue_ms` remains ~0 and qsize never pins, the fixes are successful.

---

### Implementation Order & Final Notes

1. **Commit 1:** Fix A — concurrent broadcast with timeout and error handling.
2. **Commit 2:** Fix B — offload `build_state_from_pool` to executor in `_broadcast()` / `_broadcast_state`.
3. **Test:** Run the user’s reproducible trigger (toggle any setting during streaming) and confirm qsize no longer pins at 127.

**Reject C, defer D.** No other changes needed for this plan.

**Verdict: PASS.** The v2 plan is now production-ready from a correctness and risk perspective.

---

*Review concludes here.*
