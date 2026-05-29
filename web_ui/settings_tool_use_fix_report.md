# Settings & Tool Use Fix Report

**Date:** 2026-05-29  
**Branch:** tab-unification (unified)  

---

## Summary of Fixes Applied Today

### Phase 1: Chat Tab Audit & Fix (Previous Session)
- **XSS vulnerability** → DOMPurify + hardened config
- **isRoot rendering divergence** → Unified pipeline  
- **Message truncation race condition** → Preserve messages on reconnect
- **Dead code, event leaks, stale settings** → Cleaned up

### Phase 2: Tool Use Fix (execution_engine.py) — 10 fixes

| # | Issue | Severity | Fix |
|---|-------|----------|-----|
| 1 | Missing `function_id` in fn_msg extra dict | 🔴 Critical | Extract from assistant message's extra dict, pass to fn_msg. Without this, LLM API can't match tool results to tool calls. |
| 2 | Missing `tool_success` in fn_msg extra dict | 🔴 Critical | Track via `_tool_success` bool, pass to fn_msg. Frontend `isToolFailure()` reads this. |
| 3 | No post-execution error detection | 🟠 Major | Added check for error indicators (error:, failed:, invalid:, etc.) in tool result strings. |
| 4 | LoopDetectedError silently swallowed | 🔴 Critical | Re-raise via `isinstance(e, LoopDetectedError)` after capturing error state in `_tool_error`. |
| 5 | Telemetry completely absent | 🟠 Major | Added `record_tool_call_start` before execution, `record_tool_call_end` in finally block. |
| 6 | Truncation not tracked in telemetry | 🟠 Major | Added `_was_truncated` via pre/post length comparison, passed to telemetry. |
| 7 | response.append(fn_msg) missing | 🟠 Major | Tool results weren't being streamed to UI. Now appended to response list. |
| 8 | log_message(fn_msg) missing | 🟡 Moderate | Tool results weren't persisted to JSONL logs. Now logged with error guard. |
| 9 | response.append(async_msg) missing for urgent messages | 🟠 Major | Urgent injected messages weren't streamed to UI. |
| 10 | disabled_tools dict format mismatch | 🔴 Critical | Frontend sends `{"Maine": ["tool1"]}` but execution_engine read as flat list. Now handles both dict and list formats with type guards. |

**Result:** Full parity with main branch (agent_orchestrator.py lines 1430-1722). Verified via 14/14 checks passing.

### Phase 3: Settings Hook Fix (api_integration.py) — 2 fixes

| # | Issue | Severity | Fix |
|---|-------|----------|-----|
| 1 | `disabled_tools` filtered out by NON_LLM_KEYS, never re-applied | 🔴 Critical | After filtering, re-apply to `template.llm.generate_cfg['disabled_tools']` with type guard (list/dict only). |
| 2 | Tool-level settings (char limits) not written to agent_pool.llm_cfg | 🟠 Major | Write to `pool.llm_cfg[key]` under `_state_lock` RLock for thread safety. |

**Result:** All tools now read correct user-configured values from `agent_pool.llm_cfg`.

### Phase 4: Approval Window Fix — 3 fixes

| # | Issue | Severity | Fix |
|---|-------|----------|-----|
| 1 | Flex container squeezed approval bar away with many tabs | 🟠 Major | Added `min-height: 60px` + kept `flex-shrink: 0`. Raised `z-index` to 90. |
| 2 | scrollIntoView did nothing (nearest + smooth) | 🟡 Moderate | Changed to `scrollIntoView({ behavior: 'instant', block: 'start' })`. |
| 3 | renderApprovals() only called when approvals field present | 🟡 Moderate | Made unconditional in state/done and stream_update handlers. |

### Phase 5: Tab Overflow Fix — CSS-only

| # | Issue | Severity | Fix |
|---|-------|----------|-----|
| 1 | Tabs overflowed horizontally with hidden scrollbar, making tabs invisible | 🟠 Major | Added `flex-wrap: wrap` for multi-row layout. |
| 2 | Long tab names not truncating | 🟡 Moderate | Added `text-overflow: ellipsis` on labels, icons and close buttons always visible via `flex-shrink: 0`. |
| 3 | No limit on how many rows of tabs | 🟡 Moderate | Capped at ~2 rows (`max-height: 90px`) with vertical scrollbar for extreme cases. |

### Phase 5: Review Pass — All findings resolved

- ✅ LoopDetectedError comment added
- ✅ disabled_tools type guard (list/dict only)
- ✅ Thread-safe lock for pool.llm_cfg writes (RLock)
- ✅ else branch rejects non-list/dict values
- ✅ Removed unused Union import
- ✅ Docstring about deepcopy-preserved disabled_tools behavior
- ✅ LoopDetectedError string-match replaced with isinstance

---

## Files Modified

| File | Changes |
|------|---------|
| `web_ui/app.js` | Security + rendering unification fixes, approval window fixes |
| `web_ui/index.html` | DOMPurify CDN, CSS var fix |
| `web_ui/styles.css` | Approval bar + tab overflow fixes |
| `agent_cascade/execution_engine.py` | Tool use parity with main (10 fixes) |
| `agent_cascade/api_integration.py` | Settings hook parity with main (2 fixes + thread safety) |

---

## Todo Items Completed

| Item | Status |
|------|--------|
| fix tool use to match main | ✅ Fixed |
| make sure all settings are properly hooked into the backend | ✅ Fixed |
| sub-agent tab names shrink/wrap | ✅ Fixed |
| continue should not insert a new user message | ✅ Already fixed in unified branch |
| auto agent discard timer value in agent settings | ✅ Already exists |
| user injected messages cause context reprocessing | ✅ Investigated — no full reprocessing in normal flow |

---

## Verification

- ✅ Python syntax valid (ast.parse) on both modified files
- ✅ No TODO/FIXME/HACK markers in modified sections
- ✅ Tool use parity with main branch confirmed (14/14 checks)
- ✅ Settings flow verified end-to-end: UI → WebSocket → session storage → _apply_ui_config → template/pool
- ✅ Full reviewer PASS verdict on all changes