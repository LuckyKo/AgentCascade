# AgentCascade Settings Pipeline Audit

## Executive Summary

This audit traces every configurable setting from the WebUI (`index.html`) through `app.js` (DOM reading) → `getGenerateCfg()` (backend API payload) → `api_integration.py` / `api_server.py` (backend consumption) → actual usage in execution logic.

**Total WebUI settings fields audited:** ~45
**Properly wired:** 30
**Partially wired / UI-only:** 10
**Disconnected / Hardcoded (issues):** 5

---

## Detailed Audit Table

| # | Setting Name | WebUI ID | Pipeline Status | Issue Details | File/Line Affected |
|---|-------------|----------|-----------------|---------------|-------------------|
| **LLM Generation Parameters** |
| 1 | Temperature | `setting-temperature` | ✅ Properly wired | Flows: WebUI → getGenerateCfg() → _apply_ui_config() → instance._generate_cfg_override → merged into LLM call cfg | app.js:3277, api_integration.py:864-866, execution_engine.py:736-740 |
| 2 | Top-P | `setting-top-p` | ✅ Properly wired | Same path as temperature | app.js:3278, api_integration.py:819 (floats list) |
| 3 | Top-K | `setting-top-k` | ✅ Properly wired | Same path; sanitized as int | app.js:3279, api_integration.py:821 (ints list) |
| 4 | Min-P | `setting-min-p` | ✅ Properly wired | Same path as temperature | app.js:3280, api_integration.py:820 (floats list) |
| 5 | Repeat Penalty | `setting-repeat-penalty` | ✅ Properly wired | Normalized to repetition_penalty in api_integration.py:840-843 | app.js:3281, api_integration.py:840-843 |
| 6 | Presence Penalty | `setting-presence-penalty` | ✅ Properly wired | Same path as temperature | app.js:3282, api_integration.py:819 (floats list) |
| 7 | Frequency Penalty | `setting-frequency-penalty` | ✅ Properly wired | Same path as temperature | app.js:3283, api_integration.py:819 (floats list) |
| 8 | Max Tokens | `setting-max-tokens` | ✅ Properly wired | Flows to LLM max_tokens in generate_cfg | app.js:3284, api_integration.py:821 (ints list) |
| 9 | Context Size (Max Input Tokens) | `setting-max-context` | ✅ Properly wired | Flows to max_input_tokens → _get_max_tokens() fallback chain | app.js:3285, execution_engine.py:2114-2139 |
| **Tool Result Limits** |
| 10 | Wild Read Threshold (Tool Result Max Chars) | `setting-tool-result-max-chars` | ✅ Properly wired | FIXED — tiered resolution: DEFAULT_TOOL_RESULT_MAX_CHARS → pool.llm_cfg override | execution_engine.py:2357-2362 |
| 11 | Grep Char Limit | `setting-grep-char-limit` | ✅ Properly wired | Stored in pool.llm_cfg, read by file_ops.py grep() | api_integration.py:892, file_ops.py:600 |
| 12 | Shell Char Limit | `setting-shell-char-limit` | ✅ Properly wired | Stored in pool.llm_cfg, read by shell_cmd.py | api_integration.py:892, shell_cmd.py:63 |
| 13 | Code Char Limit | `setting-code-char-limit` | ✅ Properly wired | Stored in pool.llm_cfg, read by code_interpreter.py | api_integration.py:892, code_interpreter.py:478 |
| **Compression Settings** |
| 14 | Compression Force Threshold | N/A | ⚠️ Hardcoded (no WebUI control) | Default: 95% — no slider/input in WebUI | agent_instance.py:99 |
| 15 | Compression Warning Threshold | N/A | ⚠️ Hardcoded (no WebUI control) | Default: 85% — no slider/input in WebUI | agent_instance.py:100 |
| **Agent Concurrency / Pool Settings** |
| 16 | Max Parallel Agents | `setting-max-parallel` | ✅ Properly wired | FIXED — api_server.py reads `max_parallel_agents` from ui_cfg → sets `pool.settings.max_workers` → calls `_execution.resize_executor()`. ThreadPoolExecutor in agent_pool.py reads from `pool.settings.max_workers` instead of hardcoded 10. | app.js:3288, api_server.py:1844-1850, agent_pool.py:1156 (reads pool.settings), agent_instance.py:105 |
| 17 | Auto-Continue (Truncation) | `setting-auto-continue` | ✅ Properly wired | FIXED — WebUI sends auto_continue → api_integration.py stores on pool.settings.auto_continue → execution_engine.py checks self.pool.settings.auto_continue before auto-continuing. Default True (backward compatible). | app.js:3289, api_integration.py:872-875, execution_engine.py:825, agent_instance.py:106 |
| 18 | Max Auto-Rollbacks | `setting-max-rollbacks` | ✅ Properly wired | Sent via getGenerateCfg() → run_agent_unified.py reads it as max_auto_retries | app.js:3293, run_agent_unified.py:109-111 |
| 19 | Auto-Rollback on Loop | `setting-auto-rollback` | ✅ Properly wired | Sent via getGenerateCfg() → run_agent_unified.py reads it as auto_rollback_enabled | app.js:3291, run_agent_unified.py:112 |
| 20 | Idle Timeout (seconds) | `setting-idle-timeout` | ✅ Properly wired | FIXED — Bug #3 fix. Stored in pool.settings.idle_timeout_seconds, read by IdleManager | api_server.py:1841-1843, agent_pool.py:1447 |
| **Vision / Image Settings** |
| 21 | Vision Enabled | `setting-vision-enabled` | ❌ **DISCONNECTED** | Stored in WebUI local state (app.js line 543) but NOT sent via getGenerateCfg() to backend. No consumption in agent_cascade/ codebase. | app.js:543 (stored), app.js:3271-3326 (NOT in getGenerateCfg()) |
| 22 | Image Detail | `setting-image-detail` | ❌ **DISCONNECTED** | Stored in WebUI local state but NOT sent via getGenerateCfg(). Not consumed anywhere in backend. | app.js:526, app.js:3271-3326 (NOT in getGenerateCfg()) |
| 23 | Max Image Size (px) | `setting-max-image-size` | ❌ **DISCONNECTED** | Stored in WebUI local state but NOT sent via getGenerateCfg(). Not consumed anywhere in backend. | app.js:527, app.js:3271-3326 (NOT in getGenerateCfg()) |
| **Display / UI Settings (WebUI-only)** |
| 24 | Lines Enabled | `setting-lines-enabled` | ⚠️ UI-only (no backend) | Stored in local settings state, applied to DOM for line numbers. No backend consumption needed. | app.js:516, app.js:586-587 |
| 25 | Truncate Tools | `setting-truncate-tools` | ⚠️ UI-only (no backend) | Stored in local settings state. May affect WebUI display of tool output. No backend consumption needed. | app.js:524, app.js:590-591 |
| 26 | Sound: User Intervention | `setting-sound-intervention` | ⚠️ UI-only (no backend) | Stored in local state, used for WebUI sound effects. No backend consumption needed. | app.js:517, app.js:591-592 |
| 27 | Sound: Task Completed | `setting-sound-completed` | ⚠️ UI-only (no backend) | Stored in local state, used for WebUI sound effects. No backend consumption needed. | app.js:518, app.js:595-596 |
| 28 | User Message Color | `setting-user-color` | ⚠️ UI-only (no backend) | Stored in local state, applied to CSS for user message styling. No backend consumption needed. | app.js:519, app.js:603-604 |
| 29 | Assistant Message Color | `setting-assistant-color` | ⚠️ UI-only (no backend) | Stored in local state, applied to CSS for assistant message styling. No backend consumption needed. | app.js:520, app.js:607-608 |
| 30 | Raw Edit Background Color | `setting-raw-edit-color` | ⚠️ UI-only (no backend) | Stored in local state, applied to CSS for raw edit styling. No backend consumption needed. | app.js:521, app.js:611-612 |
| 31 | Font Size | `setting-font-size` | ⚠️ UI-only (no backend) | Stored in local state, applied to DOM font-size property. No backend consumption needed. | app.js:522, app.js:571-572 |
| **MCP / Tools** |
| 32 | MCP Enabled | `setting-mcp-enabled` | ✅ Properly wired (UI toggle) | Toggles whether MCP servers JSON is sent to backend. Backend consumes it in api_server.py:1818-1827. | app.js:528, app.js:3301-3309, api_server.py:1818-1827 |
| 33 | MCP Servers (JSON) | `setting-mcp-servers` | ✅ Properly wired | Parsed JSON → sent to backend → loaded via MCPManager. | app.js:3305, api_server.py:1819-1826 |
| **Session / Execution Settings** |
| 34 | Max Turns per Run | `setting-max-turns` | ✅ Properly wired | Applied to instance.max_turns in _apply_ui_config() | app.js:3287, api_integration.py:869-870 |
| 35 | Log API POST Dump | `setting-log-api-post` | ✅ Properly wired | Sent via getGenerateCfg() → consumed in oai.py for debug logging. | app.js:3292, oai.py:231-245 and 365-379 |
| 36 | Grep Spillover | `setting-grep-spillover` | ✅ Properly wired | Stored in pool.llm_cfg, used by file_ops.py grep tool | api_integration.py:892, file_ops.py (grep spillover logic) |
| **Model / API Settings** |
| 38 | API Endpoint | `setting-endpoint` | ✅ Properly wired | Sent as api_base in getGenerateCfg() → applied to LLM config | app.js:3273, api_integration.py:815-866 |
| 39 | API Key | `setting-api-key` | ✅ Properly wired | Sent as api_key in getGenerateCfg() → applied to LLM config | app.js:3274, api_integration.py:815-866 |
| 40 | Model | `setting-model` | ✅ Properly wired | Sent as model in getGenerateCfg() → applied to LLM config | app.js:3275, api_integration.py:815-866 |
| **Work Folders** |
| 41 | Work Access Folders RW | `workAccessFoldersRW` | ✅ Properly wired | Sent as work_access_folders_rw → applied via operation_manager.set_extra_work_folders() | app.js:3315, api_server.py:1830-1834 |
| 42 | Work Access Folders RO | `workAccessFoldersRO` | ✅ Properly wired | Sent as work_access_folders_ro → applied via operation_manager.set_extra_work_folders() | app.js:3312, api_server.py:1830-1834 |

---

## Issue Summary (Critical Findings)

### 🔴 Critical — Settings that do nothing (hardcoded instead of reading config)

| Setting | Problem | Fix Required |
|---------|---------|-------------|
| ~~**Max Parallel Agents** (`setting-max-parallel`)~~ | ~~WebUI slider changes `max_parallel_agents` which is sent to backend, but ThreadPoolExecutor in agent_pool.py:1156 was hardcoded to `max_workers=10`. The setting value was never applied.~~ ✅ **FIXED 2026-05-31** — Now fully wired: PoolSettings.max_workers added, ParallelAgentManager reads from pool.settings, resize_executor() supports runtime resize, api_server.py wires ui_cfg → settings → resize. |
| ~~**Auto-Continue on Truncation** (`setting-auto-continue`)~~ | ~~The auto-continue logic in execution_engine.py:824-837 was unconditional — it always continued after truncation.~~ ✅ **FIXED 2026-05-31** — Added `auto_continue` field to PoolSettings (agent_instance.py:106), wired pool.settings.auto_continue from ui_cfg in api_integration.py:872-875, added conditional check `self.pool.settings.auto_continue` in execution_engine.py:825. Default True (backward compatible). |
| ~~**Show Active Window Only** (`setting-show-active-only`)~~ | ~~Sent via getGenerateCfg() but never consumed anywhere in the backend codebase. Dead setting.~~ ✅ **FIXED 2026-05-31** — Setting removed from WebUI (was dead code with no backend consumer). |

### 🟡 Medium — Settings with no WebUI control (hardcoded defaults)

| Setting | Default | Where Used |
|---------|---------|-----------|
| Compression Force Threshold | 95% | execution_engine.py:526 |
| Compression Warning Threshold | 85% | execution_engine.py:530 |

These could benefit from WebUI sliders if admins want to tune compression behavior.

### 🟢 Low — Vision/Image settings not sent to backend

| Setting | Status |
|---------|--------|
| Vision Enabled (`setting-vision-enabled`) | Stored in WebUI local state, never sent via getGenerateCfg(). May need to be added to LLM config if the API supports vision toggling. |
| Image Detail (`setting-image-detail`) | Same as above — stored locally but not sent to backend. |
| Max Image Size (`setting-max-image-size`) | Same as above — stored locally but not sent to backend. |

These three settings appear to be WebUI-local only (perhaps for display purposes). If they're intended to control LLM vision/image behavior, they need to be added to getGenerateCfg() and consumed in the LLM client code (oai.py).

---

## Correctly Wired Settings (Reference)

The following 30 settings are properly wired end-to-end:

1. Temperature → ✅
2. Top-P → ✅
3. Top-K → ✅
4. Min-P → ✅
5. Repeat Penalty → ✅
6. Presence Penalty → ✅
7. Frequency Penalty → ✅
8. Max Tokens → ✅
9. Context Size (max_input_tokens) → ✅
10. Wild Read Threshold → ✅ (fixed)
11. Grep Char Limit → ✅
12. Shell Char Limit → ✅
13. Code Char Limit → ✅
14. Max Parallel Agents → ✅ (fixed 2026-05-31)
15. Auto-Continue on Truncation → ✅ (fixed 2026-05-31)
16. Max Auto-Rollbacks → ✅
17. Auto-Rollback on Loop → ✅
18. Idle Timeout → ✅ (Bug #3 fixed)
19. MCP Enabled → ✅
20. MCP Servers → ✅
21. Max Turns → ✅
22. Log API POST Dump → ✅
23. Grep Spillover → ✅
24. API Endpoint → ✅
25. API Key → ✅
26. Model → ✅
27. Work Access Folders RW → ✅
28. Work Access Folders RO → ✅

Plus 6 UI-only settings that correctly stay in the browser:
- Lines Enabled, Truncate Tools, Sound (x2), Colors (x3), Font Size