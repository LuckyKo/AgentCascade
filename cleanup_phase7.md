# Aggressive Cleanup — Phase 7 Summary

## Date: 2026-05-30
## Goal: Remove backwards compat cruft, dead code, unused imports, and investigation artifacts.

---

## Files Deleted

### web_ui/
- `audit_chat_tab_fixes.md` — Investigation report
- `settings_tool_use_fix_report.md` — Investigation report

### Root
- `sub_agent_path_analysis.md` — Investigation analysis (was supposed to be deleted in previous cleanup)

---

## Code Edits

### agent_cascade/api_server.py
1. **Removed `agent_index` legacy field** from `build_state()` response dict (line ~836). Frontend no longer needs this.
2. **Removed `work_access_folders` backward compat path** in WebSocket handler — the old parameter name that was treated as RW fallback is gone. Only `work_access_folders_ro` and `work_access_folders_rw` are accepted now.
3. **Cleaned comments:** Removed "backward compatibility" phrasing from `build_stream_update()`, `run_agent_thread()`, and `_get_main_history()` docstrings.
4. **Removed `instance_conversations` fallback** in `_get_main_history()` — when agent_pool exists but instance doesn't, return empty list instead of falling back to the old mapping.

### agent_cascade/agent_pool.py
1. **Renamed comment block** from "Backward-compatible accessors for compression module" to "Compression module compatibility layer" — more accurate since these are actively used by core.py and execution_engine.py.
2. **Fixed contradictory comment** on `add_message()` — previously said "no separate instance_conversations dict needed anymore" but the property exists right below it. Updated to clarify that instance_conversations is a convenience view for other components.

### agent_cascade/api_integration.py
1. **Removed "(matches old build_state output)"** from frontend compatibility comment (line ~361). The extra fields are for current display needs, not old format matching.
2. **Updated comment** on `build_stream_update_from_pool()` — removed "format of the old build_stream_update()" phrasing.
3. **Cleaned up `serialize_message()` docstring** — removed "backward compatibility" label from dict handling.

### agent_cascade/soul_loader.py
1. **Rephrased YAML dict-handling comment** (line ~87) — changed "legacy/unquoted" to "YAML may parse multi-line strings as dicts" which describes the actual behavior without implying it's obsolete.
2. **Removed verbose compatibility comment** on line ~198 about role_name/AgentPool tracking.

### agent_cascade/tools/_agent_instance_proxy.py
1. **Removed dead `.call()` method** from `_AgentInstanceFunctionProxy` class. The proxy is schema-only — ExecutionEngine intercepts `call_agent` before it reaches this class. The old `.call()` method just returned a placeholder string and was never invoked at runtime.

---

## Not Removed (Functional Fallbacks)

The following "legacy" / "backward compat" code paths were **kept** because they handle real-world edge cases:

- **`code_interpreter.py` lines 248, 266, 497:** Fallbacks for LLMs that send non-JSON input, markdown-wrapped content in JSON, or standalone config. These are defensive measures against imperfect LLM output.
- **`file_ops.py` lines 76, 402, 460, 489:** Parameter name normalization (`start_line`→`offset`, `old_string`→`old_content`) and markdown wrapper stripping. These handle LLMs trained on old tool schemas or imperfect output formats.
- **`instance_state` in agent_pool.py:** Still heavily used by api_server.py for WebUI state tracking. Not a legacy shim — it's an active parallel state mechanism.
- **`delta_stream` in llm/base.py and subclasses:** Feature flag that exists across many LLM implementations. Removing it would require changes to 10+ files.

---

## Verification

All edited files pass Python syntax validation (AST parsing).