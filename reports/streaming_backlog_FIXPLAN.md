# Streaming "choppy / stuck for a few turns" — Root Cause & Fix Plan

## Symptom
- UI lags behind the LLM: a long message is still streaming while the model has moved on to tool calls.
- **Reproducible trigger (user-confirmed):** toggling any UI setting/tool/skill *during* streaming makes it "stuck for a few turns."
- Backend probe (`logs/stream_probe_backend.log`) shows `qsize` climbing to **~127 and pinning flat** (not draining), while `yield_to_enqueue_ms ≈ 0`.

## Verified Root Cause (evidence, file:line)

### The send path is a single bounded queue drained by one loop
- `api_server.py:417-420`: `_send_queue = asyncio.Queue(maxsize=128)` — **bounded at 128**.
- `api_server.py:729-746` `_sender_loop()`: the ONLY consumer. `data = await send_queue.get()` → `await broadcast(data)`.
- `streaming.py:30-32` `_put_stream_update()`: uses `put_nowait()` and **silently swallows `QueueFull`** ("drop stale event"). So when the queue is full, stream frames are **dropped**, not backpressured.

### The producer is fine; the consumer stalls
- Probe: `yield_to_enqueue_ms ≈ 0` even at qsize=127 → engine→enqueue is real-time. The bottleneck is **downstream of enqueue** (the `_sender_loop`/`broadcast` drain).

### Why a settings toggle stalls the loop (the reproducible trigger)
- Every UI setting change → `handle_update_config` (ws_handlers.py:683) → `await self._broadcast()` (line 715).
- `_broadcast()` (ws_handlers.py:102-109) calls **`build_state()`** — a full O(N) serialization of all instances + messages + telemetry — and pushes the big frame onto the **same** bounded queue.
- Two compounding effects during streaming:
  1. `build_state()` is a heavy CPU burst on the event loop → starves `_sender_loop` for that window.
  2. The large state frame lands in an already-near-full queue (maxsize=128) → more drops; `_sender_loop` then spends time serializing/sending the big payload to all clients.

### `broadcast()` fan-out is serial and unbounded
- `api_server.py:610-627`: `text = json.dumps(data)` once (good), then `for conn in snapshot: await conn.send_text(text)` — **sequential, no per-send timeout**. A single slow/wedged client (e.g., a backgrounded tab whose TCP buffer is full) blocks the whole broadcast and stalls `_sender_loop` → queue saturates. This also explains why a minimized tab makes it worse (its receive buffer fills → `send_text` backpressures).

## Net mechanism
toggle setting → heavy `build_state()` + big frame into saturated queue (and/or one slow client) → `_sender_loop` can't drain → qsize pins at 127/128 → `put_nowait` drops stream frames for several turns → UI lags behind LLM.

## Ranked Fix Options

| # | Fix | Where | Impact | Risk |
|---|-----|-------|--------|------|
| **A** | **Don't block the loop on a slow client:** send to clients with a per-send timeout and/or concurrently (gather), drop+reap dead conns. | `api_server.py:610-627` `broadcast()` | Prevents one wedged/backgrounded client from stalling the whole drain. | Medium (async correctness; must keep snapshot + discard-on-error) |
| **B** | **Make settings-toggle state broadcast cheap / non-blocking during streaming:** build state off-loop (executor) and/or coalesce, so a toggle doesn't inject a heavy O(N) burst into a saturated queue. | `ws_handlers.py:102-109`, `api_server.py:442` `_broadcast_state` | Removes the reproducible trigger. | Medium (build_state off-loop; ensure thread-safety of pool reads) |
| **C** | **Coalesce/drop stale stream frames deliberately:** when queue is near full, drop intermediate ticks and keep only the latest per instance (last-writer-wins), instead of FIFO drops that lose the newest. | `streaming.py` `_put_stream_update` + a per-instance "latest" slot | Streaming stays fresh even under saturation; fewer stale frames rendered. | Medium (changes drop semantics; must not lose len_changed/full-refresh frames) |
| **D** | **Raise/relax queue bounds + backpressure signal:** increase maxsize or make producer await with a short timeout so it slows instead of dropping the newest. | `api_server.py:417-420` | Reduces drop frequency. | Low-Med (bigger buffer = more latency if consumer truly stuck; pairs with A) |
| **E** | **Frontend catch-up (already done):** visibilitychange handler resets render timers + forces re-render on return to visible. | `web_ui/app.js` | Helps client recover after backgrounding; does NOT fix server-side drops. | Done (reviewed, refined) |

## Recommended Scope
- **A is the core fix** — a single slow/wedged client must not stall the shared drain. This directly stops the qsize-pinning behavior.
- **B removes the user's reproducible trigger** (settings toggle during streaming).
- **C makes streaming robust under saturation** (keep newest, drop stale) — best UX.
- **D is cheap insurance.**
- **E is already in place** for the client-side recovery.

Suggested order: **A → B → C → D** (each independently reviewable; A+B alone likely eliminate the observed symptom).

## Notes / Open Items
- Exact backend death trigger for #150 (view_image 502) is a *separate* issue (stale `ready` flag in llama-autoloader) — see `reports/view_image_502_FIXPLAN.md`. Do not conflate.
- Recommend confirming fix A empirically: reproduce by toggling a tool during streaming, watch `qsize` in the probe log — it should no longer pin at 127.
