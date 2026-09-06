# Streaming "choppy / stuck for a few turns" — REVISED Fix Plan (v2)

Supersedes `streaming_backlog_FIXPLAN.md`. Incorporates the independent review
(`streaming_backlog_PLAN_REVIEW.md`) plus two corrections I verified against code.

## Confirmed Root Cause (live-reproduced)
- Single bounded queue `asyncio.Queue(maxsize=128)` (`api_server.py:417-420`), drained by ONE `_sender_loop()` (`api_server.py:700,729-746`).
- Producer enqueues via `put_nowait()`, silently swallowing `QueueFull` → **drops the NEWEST frame** when full (`streaming.py:30-32`).
- Live repro (user-triggered settings toggle during streaming), `logs/stream_probe_backend.log` 09:42:
  - `qsize` slammed to **127 and pinned for ~10s** (5 heartbeats) while producer kept generating (n 15600→15800) → frames dropped the whole window.
  - `yield_to_enqueue_ms ≈ 0` throughout → **producer real-time; consumer (`_sender_loop`/`broadcast`) stalled.**
- Two distinct stall modes (both must be fixed):
  - **I/O stall:** `broadcast()` (`api_server.py:610-627`) sends to clients **serially with no per-send timeout** → one slow/wedged client (e.g. backgrounded tab, full TCP buffer) blocks the whole drain.
  - **CPU stall:** a settings toggle → `handle_update_config` → `_broadcast()` (`ws_handlers.py:102-109`) runs `build_state()` (O(N) full serialization) **on the event loop**, blocking `_sender_loop` for that window and injecting a big frame into the saturated queue.

## Corrections to the review (verified against code)
1. **Fix B does NOT need a new `take_snapshot()` API.** `build_state_from_pool` already locks per-instance: it takes `instance_snapshot = dict(pool.instances)` (`state_builder.py:279`) and reads each instance's `conversation`/`_streaming_responses` **under `inst._compression_lock`** (`state_builder.py:84,167,832-834`). The only unprotected read is the top-level shallow `dict(pool.instances)` copy — a GIL-atomic reference copy; instances are appended, not mutated in place. So offloading the existing function to an executor is already thread-safe. **No AgentPool API change required.**
2. **Fix C (LWW coalescing) is REJECTED** (agreed with reviewer): it breaks the delta-based streaming protocol and risks dropping critical frames (`len_changed`, `force_full_refresh`, tool events, dismissal). Also note current behavior is already "drop-newest-on-full," not drop-oldest — so LWW's premise was off.

## Scope Decision
**Implement A (modified) + B (simplified) as two independently-reviewable commits. Reject C. Defer D and the priority-queue hardening until A+B are proven against the live repro.**

---

## Fix A (MODIFIED) — Robust, non-blocking broadcast fan-out
**File:** `api_server.py` `broadcast()` (~610-627).

Goal: one slow/wedged client must not stall the shared drain.

Design:
- Keep the existing `snapshot = frozenset(ws_connections)` (prevents set-mutation RuntimeError).
- Send to each client **concurrently** via `asyncio.gather(..., return_exceptions=True)`.
- Wrap each send with a per-send timeout using `asyncio.wait_for(conn.send_text(text), timeout=WS_SEND_TIMEOUT)` where `WS_SEND_TIMEOUT` is a module constant (e.g. 5.0s).
- On `TimeoutError` or any exception for a conn: **best-effort `await conn.close()`** (guarded in try/except), then discard it from `ws_connections` *after* the gather (iterate results, not during).
- Preserve per-client FIFO ordering: each client gets exactly one send task per frame; frames are still processed serially by `_sender_loop`, so per-client order is preserved. Concurrency is only *across* clients.

Reviewer-flagged hazards and how they're handled:
- **Cancelled-send leaves conn in bad state** → we `close()` it on timeout/error and discard; a half-open conn is not reused.
- **Snapshot race (conn closed after snapshot)** → exception caught per-task, discarded post-gather.
- **Slowest-client blocks gather** → per-send timeout bounds each task; `return_exceptions=True` prevents one failure from cancelling the rest.
- **Exception filtering** → only discard conns whose task raised; successful sends untouched.

Acceptance: with a simulated slow client (or a backgrounded tab), qsize no longer pins at 127; other clients keep receiving in real time.

## Fix B (SIMPLIFIED) — Offload build_state() off the event loop
**Files:** `ws_handlers.py` `_broadcast()` (~102-109) and/or `api_server.py` `_broadcast_state` (~442).

Goal: a settings toggle (or any full-state broadcast) must not CPU-block the event loop / `_sender_loop`.

Design:
- In `_broadcast()`, run `self.build_state_fn(generating=generating)` in a thread executor:
  `state = await asyncio.get_running_loop().run_in_executor(None, lambda: self.build_state_fn(generating=generating))`
  then `await self.broadcast_fn({'type': ws_type, **state})`.
- Rationale for safety (see Correction #1): `build_state_from_pool` already locks per-instance internally; the only shared read is a GIL-atomic shallow dict copy. No new locking or AgentPool API needed.
- Keep `broadcast_fn` (the enqueue+fan-out) on the loop — it's cheap (json.dumps + queue put / gather).

Acceptance: toggling a tool/skill during streaming no longer produces a ~10s qsize pin; `build_state()` cost moves off the loop so `_sender_loop` keeps draining.

---

## Out of Scope (deferred)
- **Fix C** — REJECTED (delta protocol incompatible). If queue pressure persists after A+B, revisit via a **priority queue** for critical frames (separate design doc), NOT LWW.
- **Fix D** (raise maxsize / backpressure) — band-aid; only tune after A+B are measured against the live repro.
- **Priority queue + ping/pong health checks** — Phase 2 hardening, separate commit, only if needed.

## Verification Plan (evidence over assumptions)
1. Unit/integration: simulate a slow client (artificially delayed `send_text`) → assert qsize does not pin and other clients keep receiving; assert dead/slow conn is reaped.
2. Reproduce the user's trigger: toggle a tool during streaming with the backend probe on → confirm `qsize` no longer pins at 127 (compare to the 09:42 baseline).
3. Regression: normal streaming (no toggles) still smooth; state broadcasts after config change still delivered correctly and completely; dismissal/tool events still arrive in order.

## Commit Plan
- **Commit 1:** Fix A (robust broadcast fan-out). Reviewed independently.
- **Commit 2:** Fix B (off-loop build_state). Reviewed independently.
- Each commit: implement → independent review → fix findings → clean PASS → commit.
