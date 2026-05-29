# Unified Branch Cleanup Notes — 2026-05-29

## Files Deleted (Scratch/Investigation Artifacts)
### Root Directory
- `error.txt` — old debug log output
- `lessons_duplication_bug.md` — investigation report
- `lessons_orchestrator_agent_removal.md` — post-fix analysis
- `lessons_webui.md` — web UI lessons scratch file
- `sub_agent_path_analysis.md` — investigation analysis
- `system_message_flow_analysis.md` — flow analysis
- `web_ui_unification_plan.md` — planning doc (duplicate exists elsewhere)
- `test_orchestrator_call.py` — test for removed orchestrator stub
- `diagnose_api.py` — debug diagnostic script
- `diagnose_soul.py` — debug diagnostic script
- `check_quotes.py` — one-off analysis script

### web_ui/
- `msg_duplication_bug_investigation.md` — investigation report
- `msg_duplication_bug_investigation_v2.md` — investigation report v2
- `lessons_msgdup.md` — lessons scratch file
- `lessons_xss_sanitization.md` — lessons scratch file
- `result.txt` — debug output

### review/ and temp/ directories (entire dirs deleted)
- `review/compression_review_report.md`
- `review/false_failure_root_cause_analysis.md`
- `review/phase6_reviewer_fixes.md`
- `temp/deep_trace_analysis.py`
- `temp/forced_compression_fix_summary.md`
- `temp/phase6_progress.md`
- `temp/pool_logger_desync_comprehensive_report.md`
- `temp/pool_logger_desync_trace.py`

## Code Edits

### agent_cascade/agent_factory.py
1. **Removed top-level `from agent_cascade.agents import Assistant`** — only used in fallback path (line 214) where it's already imported locally
2. **Cleaned docstring**: "Legacy fallback parameter" → "LLM config used when APIRouter is not active"
3. **Renamed comment block**: "Backward-compatible aliases" → "Convenience wrappers"
4. **Updated error message**: Removed reference to `load_orchestrator_agent()/load_agent_template()` in favor of just `load_agent()`

### agent_cascade/tools/_agent_instance_proxy.py
1. **Rewrote docstring** for `_AgentInstanceFunctionProxy` — clarified it's a schema-only proxy needed for LLM function discovery, not dead code

### agent_cascade/agent_pool.py
1. **Removed "compatibility shim — remove in Phase 6"** comment from `_InstanceConversationMapping` class
2. **Cleaned init attribute comments**: Removed "Compatibility shim attributes (Phase 5 bridge)" and "Backward compatibility shim for agent_invoker.py" labels, replaced with cleaner descriptions
3. **Renamed method section comment**: "Compatibility shims (Phase 5 bridge to old api_server calls)" → "API bridge methods for api_server.py"
4. **Cleaned `_state_lock` docstring**: Removed "backward compatibility" language, updated to reflect current usage by agent_invoker.py
5. **Cleaned `instance_conversations` docstring**: "for compression module compat" → "required by compression module and api_server.py"
6. **Removed dead `soft` parameter from `rollback_to_snapshots()`** — was never used in method body
7. **Removed dead `soft` parameter from `surgical_rollback()`** — was never used in method body

### agent_cascade/api_router.py
1. **Removed dead `get_concurrency_limit()` method** — never called anywhere, just delegated to `get_effective_concurrency()`

### agent_cascade/api_server.py
1. **Removed TODO(Phase 7) legacy fallback comments** and simplified the retry rollback code by removing unnecessary `if agent_pool:` guards (agent_pool always exists in this code path)
2. **Removed dead `soft` parameter from `rollback_to_snapshots()` call** (line ~1712)
3. **Removed dead `soft` parameter from `surgical_rollback()` call** (line ~1430)

### agent_cascade/compression/core.py
1. **Removed unused `orchestrator` parameter from `compress_context()`** — never used in function body, no callers pass it
2. **Updated docstring** to remove orchestrator parameter reference
3. **Removed `orchestrator=orchestrator` from `invoke_compression_agent()` call**

### agent_cascade/compression/agent_invoker.py
1. **Removed unused `orchestrator` parameter from `invoke_compression_agent()`** — never used in function body

## What Was Kept (Intentionally)
- `_AgentInstanceFunctionProxy` class and `CALL_AGENT_SCHEMA` — needed for LLM function discovery
- `load_orchestrator_agent()` / `load_agent_template()` wrappers — actively called by start_api_server.py, start_multi_agent.py, agent_pool.py, __init__.py
- `agent_pool.instance_conversations`, `instance_state`, `last_tool_args`, `_state_lock` — required bridge attributes used by api_server.py and agent_invoker.py
- `hasattr()` checks in execution_engine.py — defensive checks for edge cases (lazy initialization, optional config attributes)
- Legacy `work_access_folders` config support in api_server.py — still actively used as migration path
- Old agent classes in `agent_cascade/agents/` (ArticleAgent, GroupChat, etc.) — not actively used but exported for potential external use; removing would require deleting their .py files

## Remaining Cleanup Opportunities (Not Done)
1. **Dead agent classes** in `agent_cascade/agents/`: DialogueRetrievalAgent, GroupChatCreator, ReActChat, Router, TIRMathAgent, VirtualMemoryAgent — never imported anywhere
2. **`__init__.py` exports** for those dead agent classes
3. **More aggressive hasattr simplification** in execution_engine.py (e.g., lines 1389/1400/1414/1418 check `hasattr(caller_template, 'llm')` which should always be true)
4. **web_ui/lessons_*.md files**: `lessons_gpu_perf.md`, `lessons_tab_unification.md` — these are actual lessons for future work, not scratch files