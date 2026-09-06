# Investigation Report: Backend Streaming "Buffering / Huge Chunks" on All Agents

**Date:** 2026-08-23 | **Investigator:** streaming-buffer-investigation | **Mode:** Investigation only (no files modified)
**Repo:** N:\work\WD\AgentCascade @ HEAD = 7bf12c2

---

## Executive Summary

The choppy, chunky streaming is **not** caused by the three recent router commits (8f4103c / b7e4bbd / 824c4e3) — those touch only circuit-breaker/failover logic and were exonerated by diff review. The root cause is the **stream_update broadcast pipeline**: every WebSocket frame carries the **entire conversation history** (full re-serialization of the active instance plus cached-but-still-sent message arrays for every other instance), and this O(N)-per-frame build runs **once per LLM SSE chunk** because `is_streaming_tick=True` bypasses the 100ms throttle and because token-stat caches are keyed in a way that guarantees a miss on every chunk.

Because build+serialize cost grows linearly with conversation length, the engine's consume loop falls progressively further behind: UI updates arrive as ever-larger batches of accumulated chunks. That exactly reproduces the symptom: **chunk size scales with message count; fresh instances look fine; all agents are affected** (every agent's stream_update includes *all* instances' data).

---

## (a) Most likely root cause — with file:line evidence

### The pipeline (verified end-to-end)

```
oai.py:_chat_stream            → yields accumulated snapshot per SSE event   (1 yield/chunk)
llm/base.py wrappers           → 1:1 passthrough, no buffering
engine/llm_call.py:369,548     → consumes each chunk, yields None tick       (1 tick/chunk)
engine/core.py:667             → yields (response+turn_output+partials, True) per tick
run_agent_unified.py:202       → broadcast_stream_update() per yield
api_integration_pkg/streaming.py:91-98 → throttle check (bypassed when is_streaming_tick)
api_integration_pkg/state_builder.py   → builds FULL payload
api_server.py queue (maxsize=128)      → WS send
web_ui/app.js:1962+            → merge + throttled render (~200ms)
```

No stage buffers deliberately — but four compounding factors make the *effective* chunk size scale with history length:

### Factor 1 — Every frame carries the full conversation (primary)
- `state_builder.py:824-827`: *"Always send all messages — no tail optimization"* — `start_idx = 0`; serializes ALL messages of the active instance.
- `state_builder.py:197`: active instance (`name == instance_name`) is force-reserialized on **every** broadcast regardless of version.
- `state_builder.py:544-546`: the frame embeds `'instances': all_instances` — including other instances' **cached but complete** message arrays (`_serialize_instances_incremental` reuses cache only to avoid *rebuilding*, not *resending*).
- A proportional tail optimization (>50 msgs → ~10% tail) existed until commit **d2ba7a3 (2026-06-20)** removed it ("remove streaming tail optimization — always send all messages to UI"). It was carried verbatim through the Aug-19 package split (863531e).

### Factor 2 — Per-chunk broadcasts bypass the throttle
- `run_agent_unified.py:202-211` passes `is_streaming_tick=is_streaming_tick or has_tool_event`.
- `engine/core.py:645-668` yields `(…, True)` for every engine-loop iteration; `engine/llm_call.py:548` yields once per consumed LLM chunk.
- `streaming.py:91-95`: `should_broadcast = is_streaming_tick or len_changed or (now - last_send > 0.1)` — an OR means `is_streaming_tick=True` short-circuits the 100ms throttle. Result: one full-payload build+send attempt **per SSE chunk** (often 50–150/sec), not 10/sec.

### Factor 3 — Token stats recomputed every chunk (scales with history)
- `state_builder.py:441-446` and `:873-878`: cache/version keys include raw `stream_content_len`, which changes on **every** chunk ⇒ cache miss every chunk ⇒ `_calc_stream_token_stats` (`streaming.py:157-163`) runs `pool.slice_history_for_llm(combined)` + `get_history_stats()` over the **whole conversation** per chunk. This matches the never-fixed finding in `.agent_lessons/activity_feed_investigation.md` ("cached token stats are NEVER reused during active streaming").

### Factor 4 — Engine loop coupling (why it looks like buffering)
- `llm_call.py:369` (`for output in gen:`) pulls the next LLM chunk only after finishing the previous tick's work. As Factors 1–3 make each tick slower, more SSE events accumulate inside httpx/openai buffers between yields. The next yield therefore contains proportionally more new text — observed by the user as "buffering and sending huge chunks."

**Quantified intuition:** at ~10 ticks/sec with a 32k-token history, the server JSON-serializes and ships roughly **~125 KB × 10/sec ≈ 1.25 MB/s** even if the model emits only a few bytes between ticks — vs. a few KB/s if only deltas/tails were sent. On long conversations the serialize+queue step alone can exceed the 100ms budget by orders of magnitude.

---

## (b) Did commits 8f4103c / b7e4bbd / 824c4e3 cause it? — **NO (definitive)**

Diff review of `git diff 8f4103c~1..HEAD`:

| File | Changes | Streaming-path impact |
|---|---|---|
| `router.py` | Breaker state machine (trip/skip/probe), D1 pre-loop fail-fast wait, consult-before-fire skip, busy-loading break-out, context-exceeded A1/A2 gate | All code runs **before/at call start**, not during generator iteration. Success path unchanged: first-chunk pull + `yield from` wrapper identical to before (router.py:996-1017). No accumulation, no delays added on the success path. |
| `oai.py` (+31 lines) | Only adds `_breaker_blocks_base()` gate to `_detect_context_window()` (skip `/models` GET when breaker open) | Zero effect on `_chat_stream`. |
| breaker_gate.py, normalization.py, settings.py, scheduler.py | Breaker helpers/constants | Not on the data path. |

The D1 fail-fast wait (router.py:943-966) fires only when **all** chain endpoints sit on breaker-open servers — an error/recovery scenario, not steady-state streaming, and it precedes the HTTP call entirely. The conditional break-out at router.py:1132 affects retry loops after a 503, not mid-stream cadence.

**However:** the timing correlation that made these commits suspect is explained by Factor 1's history — the tail-optimization removal (d2ba7a3, June 20) predates them, and its cost grows with conversation length, which is consistent with "it got worse recently" perception as conversations got longer. Additionally `17b77af` (Aug 4, frontend) stopped deleting `is_partial` in merges, making the client process partial updates continuously rather than treating them as complete — increasing render pressure but improving correctness.

---

## (c) Why it scales with message count

Three linear-in-N costs executed per chunk:
1. Full-history serialization of the active instance (`state_builder.py:197, 827`) — plus resending all instances' arrays (`:545`).
2. Whole-conversation token estimation per chunk due to self-invalidating cache keys (`:441-446, 873-878` → `streaming.py:158-162`). Note `get_history_stats`/`slice_history_for_llm` iterate the entire conversation.
3. Frame size ∝ total conversation characters ⇒ WS send time and frontend merge/parse (`app.js:2007-2019` splices the full array) grow with N.

Since the engine loop is synchronous with broadcasting, chunks-per-render ≈ (build+send+render latency)/(LLM inter-chunk interval). Latency grows ∝ N ⇒ perceived chunk size grows ∝ N. Fresh instances have small N ⇒ near-normal behavior. Exactly matches the reported symptom.

---

## (d) Minimal fix proposal (ranked)

1. **Restore the streaming tail** (biggest win, smallest change): in `_serialize_instance`, when `streaming=True` and `len(msgs) > threshold`, send only the last K messages (K = max(5, len//10)) with correct absolute indices via `history_count`. The frontend **already handles this** — the splice path at `app.js:2007-2019` computes `startIdx = hCount - sa.messages.length` precisely so partial tails can merge. Effectively revert d2ba7a3's scope for the `stream_update` path only (keep full sends on initial `state`).
   - Files: `agent_cascade/api_integration_pkg/state_builder.py:821-832`.
2. **Quantize streaming content length in cache keys** (kills per-chunk recompute): use `stream_content_len // 512` (or bucket) in the version tuples at `state_builder.py:441-446` and cache key at `:873-878`. Token stats then refresh at most every ~512 chars of growth instead of every chunk.
3. **Add a min-interval floor for streaming-tick broadcasts**: in `broadcast_stream_update` (`streaming.py:91-95`), treat `is_streaming_tick` as bypassing only a *long* idle gap, e.g. broadcast if `now_sec - last_send > 0.05` even when `is_streaming_tick` (or drop the OR-short-circuit entirely since `len_changed` already catches committed messages and content growth is covered by the periodic path reading `_streaming_responses`).

Items 1–2 together should reduce per-chunk work from O(total-history) to O(tail); item 3 caps broadcast frequency. Combined they restore smooth small-delta streaming independent of conversation length.

---

## Confidence & Hypothesis Ranking

| # | Hypothesis | Likelihood | Confidence |
|---|---|---|---|
| H1 | Full-payload stream_update frames (tail opt removed d2ba7a3) + per-chunk bypass of throttle | **Primary** | **High** (code-verified mechanism matches all symptoms) |
| H2 | Token-stats recompute per chunk over full history (self-invalidating cache key) | Strong amplifier | High (confirmed cache-key math) |
| H3 | Recent router commits (8f4103c/b7e4bbd/824c4e3) introduced buffering | **Ruled out** | High (diff evidence) |
| H4 | llama.cpp-side batched emission | Unlikely | Low-Medium (not tested directly; backend would buffer regardless due to sync loop) |

**Overall confidence: High** on root cause location (backend serialization/broadcast path); Medium-High that fixing items 1–2 eliminates the visible symptom. Remaining unknowns: exact per-frame timing profile under production load (would need instrumentation), and whether llama.cpp's own batching contributes additional coarseness at high context (testable by comparing raw `/v1/chat/completions` SSE cadence vs UI cadence at fixed history sizes).

## Suggested Next Actions
1. Implement fixes 1–3 (small, surgical; frontend already compatible).
2. Add a debug log/metric: per-broadcast serialized byte count + build duration, gated behind existing debug logging patterns — will confirm the diagnosis within minutes of running a long conversation.
3. Optional verification experiment: temporarily set `tick_num % 100 == 0` force-full off and cap messages sent during streaming to confirm immediate smoothing before committing the permanent fix.

---
*Evidence artifacts saved during investigation: tmp_router_diff.txt, tmp_tail_removal_diff.txt (N:\work\WD\AgentWorkspace). Memory saved: .agent_lessons/stream-update-full-payload-scales-with-history.md*
