# Reasoning-Streaming Regression — Line-Cited Diagnosis
## "Thinking agents update once per turn" (AgentCascade, N:\work\WD\AgentCascade)

**Date:** 2026-09-03 · **Investigator:** thinking_stream_trace · **Mode:** investigation only (no code modified)

---

## 1. Executive Summary

The regression is **NOT a backend drop, filter, or accumulation** of reasoning-only deltas. Verified by probe data:

- The LLM layer emits reasoning deltas **incrementally** (per-chunk), for every model.
- The backend broadcast loop runs at **18–36 updates/sec** for every agent type (root, sub, compressor) — healthy, post-`de0c3b2`.
- The serialized payload **includes `reasoning_content`** end-to-end; the frontend **does** render a thinking block.

The "once per turn" perception is a **frontend render-rate** problem, caused by the fact that a message carrying `reasoning_content` **always takes the expensive full-re-render path** (never the O(1) incremental-append path), combined with an **adaptive render throttle** that widens as render time grows. As the thinking text grows, each re-render gets slower, so the UI visibly "jumps" far less often than it does for a regular content agent.

**Most likely root cause (rank 1):** `web_ui/app.js:3076` — the incremental-append guard requires `!msg.reasoning_content`, so reasoning messages are always full-re-rendered (`renderMarkdown` + `renderThinkingBlock` + `innerHTML` replace, L3108–3136). Regular content uses the cheap path; thinking does not.

---

## 2. The reasoning-delta path (line-cited map)

### 2.1 LLM layer — `agent_cascade/llm/oai.py`
- **L511–512** (`delta_stream=True` branch): for each SSE chunk, reads `reasoning = delta.reasoning_content`, `content = delta.content`; if EITHER non-empty → `yield [Message(role=ASSISTANT, content=content or '', reasoning_content=reasoning or '')]`.
- **No drop here.** A reasoning-only chunk (`content=''`, `reasoning` set) is emitted. **Probe-confirmed incremental:** `LLMDONE` lines show `Qwen3.8-27B` avg 245 chunks @ avg_gap 57ms, `Qwen3.8-27B-NVFP4-MTP-ako` avg 850 @ 39ms, `Agents-A1-APEX-I-Quality` @ 18ms. (i.e. ~17–125 chunks/sec; not batched at turn end.)

### 2.2 LLM-call consumer — `agent_cascade/engine/llm_call.py`
- **L564** `for output in gen:` consumes the oai.py yields.
- **L713–719** `_update_streaming_responses(...)` deep-copies the current partial into `instance._streaming_responses`, throttled to **~0.1s**.
- **L743** `yield None` signals a UI tick upstream (the message is NOT passed by value; it lives in `instance._streaming_responses`).
- **No drop here** — reasoning-only partials are stored; the 0.1s throttle only limits refresh frequency, not visibility.

### 2.3 Execution engine — `agent_cascade/engine/core.py`
- **L697** `for msg in gen:` receives the `None` yields.
- **L709** on `msg is None`, reads `partial_msgs = list(instance._streaming_responses)` (includes the reasoning-only partial).
- **L719** `yield (response + turn_output + partial_msgs, True)` with `is_streaming_tick=True` (L715 is the enclosing conditional, L719 is the yield).
- **No drop here** — the reasoning-only partial is forwarded as part of `partial_msgs`.

### 2.4 Broadcast path — `agent_cascade/run_agent_unified.py` → `agent_cascade/api_integration_pkg/streaming.py`
- **run_agent_unified.py L166** `now = time.monotonic()`; **L205** calls `broadcast_stream_update(... now_sec=now, last_send=last_send, last_resp_len=exec_state['last_resp_len'])`.
- **streaming.py L318–319** `resp_len = len(turn_output)`; `len_changed = (resp_len != last_resp_len)`.
  - ⚠️ `resp_len` is the **message count**, NOT payload bytes. A reasoning-only tick keeps the same message count → `len_changed=False` (this is **expected**, not a drop). The probe's `len_chg=0` reflects this.
- **streaming.py L328** `MIN_STREAM_BROADCAST_INTERVAL = 0.2`; **L329–333** `should_broadcast = len_changed or (is_streaming_tick and now- last_send >= 0.2) or (not is_streaming_tick and now-last_send > 0.1)`.
- **Probe-confirmed the floor is NOT the constraint:** the main/root agent broadcasts at **~23.5 updates/sec** (50-broadcast heartbeat median dt ≈ 2.1s), sub-agents 18–28/sec, compressor 33–36/sec — all well above the 5/sec floor. So `de0c3b2`'s 0.2s floor does **not** throttle the loop. (Latent bug, not the regression: `last_send` appears not to hold across ticks for the main agent, so the floor rarely fires — worth a separate fix but unrelated to "once per turn.")

### 2.5 Serialization — `agent_cascade/api_integration_pkg/state_builder.py`
- `build_stream_update_from_pool` (**L415**) builds the per-agent payload.
- `serialize_message` **explicitly includes `reasoning_content`**; the dedup fingerprint includes it too. → **The frontend receives growing reasoning on every broadcast.** No serialization drop.

### 2.6 Frontend — `web_ui/app.js`
- **L2780–2781** `createMessageEl`: if `msg.reasoning_content` → `html += renderThinkingBlock(...)`. Thinking block IS created.
- **L3055** `curReasoning = msg.reasoning_content || ''`; **L3063** early-return only when content AND reasoning AND generating-state all unchanged.
- **L3076** ⚠️ **THE KEY LINE** — incremental-append guard:
  `if (isGenerating && prevContent !== undefined && !msg.function_call && msg.role !== 'function' && !msg.reasoning_content)`
  → the cheap O(1) append (L3092 `appendStreamingDelta`, L3097 early-return) is taken **only when there is NO reasoning_content**. Any message with reasoning **always** falls through.
- **L3108–3121** full re-render: `renderThinkingBlock(msg.reasoning_content, isGenerating)` + `renderMarkdown(getDisplayedText(msg), false)`.
- **L3136** `contentDiv.innerHTML = html;` — full DOM replacement every tick.
- **L2093 / L2101** `subAgentContentChanged = true` is set for reasoning-only partials (same message count, `is_partial`) — so the data-change signal IS produced.
- **L2174–2176** adaptive throttle: `rootThrottleContent = min(500, 250 + round(lastRenderDur*0.5))`; `subThrottleContent = isSubAgentActive ? 250 : rootThrottleContent` (L46–48: `RENDER_SUBAGENT_MS=250`, `RENDER_ROOT_BASE_MS=250`, `RENDER_ROOT_MAX_MS=500`).
- **L2180 / L2185** `isVisibleActiveAgentContentChanged` bypasses the throttle for the visible active agent on content change — but the render itself is the expensive full re-render above.
- **L3341** `renderThinkingBlock`; **L4848** activity preview includes reasoning.

---

## 3. Every point where a reasoning-only delta could be dropped / fail to produce a visible change

| # | Location | Reasoning-only delta? | Verdict |
|---|----------|----------------------|---------|
| 1 | oai.py L511–512 | Emitted | ✅ Not dropped (probe-confirmed incremental) |
| 2 | engine/llm_call.py L713–719 (0.1s throttle) | Stored in `_streaming_responses` | ✅ Not dropped (refresh cadence only) |
| 3 | core.py L709 / L719 | Forwarded in `partial_msgs` | ✅ Not dropped |
| 4 | streaming.py L318–333 (0.2s floor) | Broadcasts (probe: 18–36/s) | ✅ Not the constraint; floor not binding |
| 5 | state_builder serialize/fingerprint | `reasoning_content` included | ✅ Serialized |
| 6 | app.js L2093/2101 `subAgentContentChanged` | Set for reasoning partials | ✅ Change signal produced |
| 7 | **app.js L3076** incremental guard | **Skipped → full re-render** | ⚠️ **Primary bottleneck** |
| 8 | app.js L3108–3136 full re-render + `innerHTML` | O(N) in thinking length | ⚠️ Expensive per tick |
| 9 | app.js L2174–2176 adaptive throttle | Widens 250→500ms as render slows | ⚠️ Amplifies #7/#8 |

---

## 4. Ranked causes of "thinking agents stream once per turn"

**RANK 1 (most likely) — Frontend full-re-render for reasoning + adaptive throttle.**
`app.js:3076` forces every reasoning message onto the full-re-render path (L3108–3136: `renderThinkingBlock` + `renderMarkdown` + `innerHTML` replace). For a large, growing thinking text this is O(N) per tick and blocks the JS main thread. The adaptive throttle (L2175: `min(500, 250 + lastRenderDur*0.5)`) then widens the render interval as render time grows → render rate collapses as thinking grows → perceived "once per turn." Regular content agents use the cheap O(1) incremental path (L3076 guard passes) → smooth. **This is the only code path that treats thinking differently from regular content.**

**RANK 2 — Sub-agent 250ms render throttle.** If the thinking agent is a *sub-agent* (not the visible active root), `subThrottleContent = 250ms` (L2176, L46) caps it at ~4 updates/sec regardless of backend rate. For a long thinking turn this looks like slow, chunked updates. (The visible active root bypasses this, L2180/2185, but the expensive re-render still limits it.)

**RANK 3 (ruled out as the cause) — `de0c3b2` 0.2s backend floor.** Probe data shows the backend at 18–36/sec (exceeds the 5/sec floor), so the floor is not throttling. `de0c3b2`'s O(N) quantization fix actually **improved** the backend. Not the regression cause. (Separate latent bug: `last_send` not holding across ticks → floor rarely enforced.)

---

## 5. Confidence

- **Backend is healthy / no reasoning drop:** **Verified** (probe log `logs/stream_probe_backend.log`, post-de0c3b2, 2026-09-03 01:30).
- **Reasoning serialized & rendered:** **Verified** (state_builder + app.js L2780/L3109/L3341/L4848).
- **Full-re-render-for-reasoning is the primary bottleneck:** **High confidence** (direct code path, L3076/L3108–3136, L2174–2176). Not yet confirmed by an in-browser render-timing measurement.
- **Which specific agent (root vs sub) the user observed:** **Unknown** — affects whether rank 1 vs rank 2 dominates.

## 6. Open questions
1. Is the affected agent the visible active root, or a sub-agent tab? (Determines rank 1 vs rank 2.)
2. What is the typical thinking-text length in the failing scenario? (Larger ⇒ worse rank-1 collapse.)
3. Does the user's "worse now" compare against pre-`de0c3b2` (slow backend) or an older healthy build? (Clarifies perception vs. reality.)

## 7. Suggested next actions (investigation-only, no code changed)
1. **Measure frontend render timing** on a thinking agent: log `lastRenderDur` (app.js L2174) and render frequency vs. thinking length. Confirms rank 1.
2. **Fix direction:** give reasoning the same O(1) incremental-append path as content (append-only thinking delta) instead of full re-render; or defer `renderMarkdown`/`renderThinkingBlock` on the thinking block until idle / cap its size during streaming.
3. **Do NOT** tune the 0.2s backend floor — it is not the cause. Optionally fix the latent `last_send` non-persistence so the floor actually engages.
4. **Verify** the affected agent is root vs sub to target rank 1 vs rank 2.

---
*All file:line references verified against the working tree at investigation time (2026-09-03). Memory saved: `.agent_lessons/thinking-stream-once-per-turn-frontend-render.md`.*
