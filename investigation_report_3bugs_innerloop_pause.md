# Bug Investigation Report — Inner-Loop Detection, Compression Summary, UI Pause Streaming

**Date**: 2026-08-06
**Investigator**: bug_investigator_1
**Codebase**: `N:\work\WD\AgentCascade`
**Scope**: todo.md lines 45, 46, 49

---

## Executive Summary

| Bug | Root Cause Location | Type | Severity |
|-----|--------------------|------|----------|
| **BUG 1** (line 45) wrong summary on compressor failure | `compression/agent_invoker.py:352-374` + missing endpoint rotation on non-loop/empty failures | Backend | High |
| **BUG 2** (line 46) inner-loop false positives | Already migrated to two-phase semantic detector; FP control lives in `two_phase_loop_detect.py` + settings | Backend / testing | Medium |
| **BUG 3** (line 49) UI streaming stops on pause | `web_ui/app.js:1777-1778` (frontend drops `stream_update` on halt) | Frontend | High |

---

## BUG 1 — Wrong summary pushed from inner-loop detector when compressor fails

### Symptom
When the compressor's LLM call fails and gets stuck on a loop producing `[SYSTEM ERROR: Empty LLM response]`, the wrong summary gets pushed and the compression keeps retrying the same endpoint instead of rotating.

### Root cause part A — the bad summary gets committed
1. `agent_cascade/compression/agent_invoker.py:352-374` extracts the summary by scanning `comp_instance.conversation` for the **last assistant message** (lines 353-360) and validates only `if not summary.strip(): raise` (line 371).
2. When the LLM call returns nothing usable, `execution_engine.py:2946` yields an assistant message with content exactly `"[SYSTEM ERROR: Empty LLM response]"`.
3. `extract_text_from_message()` (`utils/utils.py:782-846`) returns that string verbatim (`text = msg.content`, line 829). `strip_thinking_blocks()` does not remove it. So `summary` = `"[SYSTEM ERROR: Empty LLM response]"`, which is **non-empty** → passes the `.strip()` validation → returned as a valid summary (line 374).
4. That garbage string is then committed as the compression summary: `compression/handler.py:596` sets `instance.compression_summary = result.summary_text`, and core.py uses it as the "EXISTING SUMMARY" for the next compounding pass (core.py:226-230, 288-295). Once the marker is written, the corruption cascades on every subsequent compression.

**Fix (part A):** In `invoke_compression_agent` (agent_invoker.py), before accepting `summary`, reject known error/failure strings — e.g. `if summary.strip().startswith('[SYSTEM ERROR') or 'Empty LLM response' in summary: raise RuntimeError(...)`. Treat the compression as failed and fall through to the endpoint-rotation path rather than committing a garbage marker.

### Root cause part B — endpoint rotation gap on compressor failure
- The two retry loops negotiate poorly. `call_with_fallback` (`api_router.py`) builds a **fresh endpoint chain each call** and only remembers the *last successful* endpoint (`_last_successful_endpoint_cfg`, api_router.py:1025), never failed ones.
- `_execute_llm_call_with_retry` (`execution_engine.py:2549`) advances the per-instance endpoint cursor only on `CharacterRunDetected` (character-run), `MaxTokenExceeded`, `ContextWindowExceeded` (`_handle_inner_loop_detection`, lines 2504-2524; retry path 2889-2891). **An empty-response / non-loop failure retries the same endpoint**.
- There is no cross-recall failed-endpoint set, so even if one endpoint returns `[SYSTEM ERROR]` every time, the same endpoint is retried (matching prior findings in `inner_loop_endpoint_switch_analysis.md`).

**Fix (B):**
- Track failed endpoints per retry window and pass a `skip`/offset so subsequent `call_with_fallback` calls don't repeat them (candidate: carry failed `api_base`s from the outer retry loop into `get_endpoint_chain`).
- Also treat empty-LMM/garbage responses as an endpoint-failure signal and call `advance_instance_endpoint(inst_name)` before retrying, like the char-run path does.

---

## BUG 2 — Inner-loop detector false positives; `char run` the only reliable mode

### Current state
- The detector `agent_cascade/inner_loop_detect.py` **no longer uses** the old scoring modes (sentence/ngram/block/entropy). Per settings.py:160-197 and inner_loop_detect.py:69-121, active modes are:
  1. **Max char guard** (hard limit ~40KB).
  2. **Character run** (limit 129 identical chars) — the reliable one.
  3. **Two-phase semantic** detector (`two_phase_loop_detect.py`) — suspicion (ngram frequency) → exact-match confirmation (≥3) → cooldown on failure.
- The two-phase detector is gated by `loop_two_phase_enabled`, **default `False`** (settings.py:193; agent_instance.py:653). So with stock settings, the FP-prone semantic modes are already effectively disabled, and only char_run + max_chars run — which explains "`char run` is the only good mode".
- The newer two-phase semantic detector mentioned in todo.md line 38 **exists** and replaces all scoring modes (`two_phase_loop_detect.py`, with 62 passing tests in `tests/test_two_phase_loop_detect.py`).

### FP control points (when two-phase is enabled)
- `ngram_window_size=64`, `suspicion_threshold=7` (env `QWEN_AGENT_LOOP_SUSPICION_THRESHOLD`, two_phase_loop_detect.py:47-49)
- `confirmed_matches_required=3` (line 61-63) — exact byte match before abort
- `cooldown_duration=50` feeds with mandatory state reset (lines 66-70, 236-247)
- Consistency gate ≥60% single dominant gap before suspicion is reliable (`_estimate_interval`, lines 135-142)

### Why FPs still happen / appear "unusable"
- `save_loop_sample` captures every trigger, including legitimate content that merely crosses the confirmation threshold.
- The suspicion threshold can fire on repetitive-but-not-looping technical prose when enabled; cooldown helps but the default `False` guards against it being on at all.
- There is **no telemetry count of false positives vs real loops** to measure improvement (todo line 85 also wants loop counts in telemetry).

### Existing tests (tests/)
- `test_inner_loop_fp_simulation.py` — 30-message sample, 20-char chunks, asserts FP <3%.
- `test_inner_loop_live_data.py` — replays real assistant messages from logs, checks FP and true positives.
- `test_two_phase_loop_detect.py` — 7 scenarios: gating, suspicion→confirm flow, non-loop prose, cooldown, reset, edges, discrimination.
- `tests/loop_test_utils.py` — log discovery + `feed_streaming()` + `extract_assistant_texts()`.

### Proposed test plan (use real logs to measure FP rates)
Use `logs/` real data (agent JSONL conversation logs, and `workspace/logs/loop_samples/` for known true-positives) to build a **streaming replay harness** that feeds each committed assistant message through a fresh detector **the way production feeds it** (delta streaming with accumulating `_total_text`, sliced by prev length — see execution_engine.py:2688-2724):

1. **Log-replay FP harness** (`tests/test_inner_loop_fp_log_replay.py`): load real assistant texts from `logs/`, feed with small tokens-like chunk sizes, and assert:
   - char_run-only FP rate (expect ~0%).
   - two_phase-enabled FP rate < some threshold (propose <3%).
   - two_phase true-positive on known real loops from `inner_loop_phase0_baseline.md` samples (e.g. the repeated-call/ngram loops listed there).
2. **Per-mode FP** replicating the automated pipeline's own 10 detections from phase0 doc** so the audit table maps to tests, not just synthetic prose.
3. **Warmup cooldown test**: feed unique technical prose (like `make_unique_filler`) then a loop, ensure no FP after cooldown.
4. Regression for line 54: enable two_phase default `False` guard to confirm default off.

---

	## BUG 3 — UI streaming stops on `pause`

### Symptom
`ws_handlers.py:373` (`handle_pause`) sets `pool._paused.clear()` — a global event; `handle_resume_all` (ws_handlers.py:387) clears it. Backend `_is_stopped()` (execution_engine.py:1811-1831) **excludes** pause, and pause gates tool execution only (execution_engine.py:3404-3407 `if pool.is_paused(): wait_if_paused()`). So the backend correctly keeps streaming the LLM during pause.

### Root cause — frontend
`web_ui/app.js` in the `stream_update` handler:
```js
case 'stream_update': {
  const activeName = getActiveAgentName();
  if (state.subAgents[activeName]?.is_halted) break;   // line 1778
```
When the user presses Pause, `createPauseButton` (app.js:4510-4520) sets local `is_halted=true` on active agents. The backend also reports `is_halted = is_paused() || in _halted_instances` (agent_pool.py:2518-2522; api_integration.py:1516). Every stream update for the active agent is therefore dropped → streaming appears frozen even though the backend is still producing tokens.

### Fix
- Do **not** gate `stream_update` on `is_halted`. Pause should only suppress the tool/approval-adjacent UI, not the streaming display.
- Options: (a) remove the `if (state.subAgents[activeName]?.is_halted) break;` line; or (b) narrow it so only tool-related UI is suppressed on halt while message content continues to stream. Since backend keeps streaming on pause, removing the front-end block restores expected behavior ("pause should ONLY stop the tool response logic").

---

### Confidence levels
- BUG 1: **High confidence** (code trace verified across agent_invoker → execution_engine → handler/core).
- BUG 2: **High confidence** on the mode-default facts; FP rate numbers from existing tests are empirical and small-sample.
- BUG 3: **High confidence** (backend clearly keeps streaming; frontend line 1778 is the only gate).

### Open questions / unknowns
- Whether an actual produced `[SYSTEM ERROR...]` marker is present in a live log (grep did not confirm a real instance, only the code path).
- The recommended FP target values (<3%) need validation against a large log sample.

### Suggested next actions
1. Implement BUG 3 fix first (single line in app.js) — smallest, highest UX impact.
2. Implement BUG 1 summary validation + endpoint-rotation on empty-response in `_handle_inner_loop_detection`.
3. Build the log-replay FP harness (BUG 2) to set evidence-based thresholds and enable two-phase safely.
4. Run `pytest tests/test_inner_loop_* tests/test_two_phase_loop_detect.py` after changes to confirm no regressions.