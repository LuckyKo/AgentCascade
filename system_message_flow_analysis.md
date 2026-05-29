# System Message Flow Comparison: Original vs Unified Branch

## Executive Summary

The unified branch has TWO critical issues after the soul fix:

### Issue 1: Session Load Failure / API Crash
When `_load_session_history()` fails (no log file, or malformed log), **NO pool instance is created**. The `build_state_from_pool()` returns None, and the minimal fallback state is missing critical fields like `'agents'` (tools list).

### Issue 2: Missing Tools in POST Message
The fallback state at lines 753-765 of api_server.py doesn't include `'agents'`, `'current_model'`, etc. — only happens when no instance exists.

## Root Cause Analysis

### Original Branch Flow (Works)
1. `_load_session_history('Maine')` returns `([], "")` on failure
2. `session['history'] = []` — always initialized, even if empty
3. `agent_pool.instance_conversations[default_session_name] = session['history']` — registered in pool
4. WebSocket connects → `build_state()` reads from `session['history']` (always exists)
5. First message → `agent_runner.run(history)` → base `Agent.run()` injects system_message

### Unified Branch Flow (Broken)
1. `_load_session_history('Maine')` — return value DISCARDED
2. If it fails, **NO instance is created** in the pool
3. WebSocket connects → `build_state_from_pool(pool, 'Maine')` → `pool.get_instance('Maine')` returns None
4. Returns minimal fallback state WITHOUT agents/tools
5. First message → creates instance via `create_main_agent_instance()` — but too late for initial state

### Key Differences

| Aspect | Original | Unified |
|--------|----------|---------|
| History storage | `session['history']` dict | `pool.instances[name].conversation` |
| Instance creation at startup | Implicit (session['history'] always exists) | Explicit (via load_session_from_log or create_main_agent_instance) |
| System message injection | `Agent.run()` prepends self.system_message | ExecutionEngine reads from conversation or template |
| State building source | session['history'] | pool.instances[name].conversation |
| Fallback state completeness | Full (includes agents/tools) | Minimal (no agents/tools) |

### All Occurrences of system_prompt/base_system_message/system_message

**Unified branch api_server.py:**
- Line 837-869: system_message_content extraction in run_agent_thread
- Line 1347-1362: base_system_message > system_message priority chain (our fix)
- Line 1633: system_message_content="" fallback
- Line 2417-2422: Another priority chain for main() entry point

**Unified branch execution_engine.py:**
- Line 151-152: template.system_message injection when no SYSTEM message at start
- Line 1168-1169: base_system_message > system_message for sub-agent creation

**Original branch api_server.py:**
- Line 1003: system_prompt in telemetry (just reads from history[0])

**Both branches agent.py:**
- Lines 43, 69, 116-128: system_message attribute used by Agent.run() to prepend system message

## Fix Applied ✅

### Fix 1: Startup Instance Creation (api_server.py lines 554-578)
After `_load_session_history` fails, create an empty instance with system message from the orchestrator agent. This mirrors how the original branch always had `session['history'] = []`. Includes operator warning on failure.

### Fix 2: build_state() Fallback Enriched (api_server.py lines 773-833)
The fallback state now includes ALL fields that `build_state_from_pool` normally returns:
- `'agents'` (tools list from `_build_agents_list`)
- `'current_model'` (from orchestrator llm.model)
- `'telemetry'`, `'default_workspace'`, `'is_waiting'`, `'api_router'`
- `'instances'`, `'instance_name'`, `'stopped'`, `'summary'`, `'has_queued_messages'`
- `'max_tokens'` resolved via API router (not hardcoded DEFAULT_MAX_INPUT_TOKENS)

### Fix 3: build_stream_update() Fallback Enriched (api_server.py lines 853-890)
Added missing fields to stream update fallback:
- `'instances'`, `'current_model'`, `'telemetry'`, `'stopped'`
- `approvals` now uses `_get_approvals` instead of empty list
- `'max_tokens'` resolved via API router (not hardcoded DEFAULT_MAX_INPUT_TOKENS)

### Code Quality Fixes (from review)
- Removed redundant lazy imports — moved `_build_agents_list` and `_get_approvals` to top-level import block at lines 395-402
- Removed redundant `from api_integration import create_main_agent_instance` inside startup try block (already imported at top level)

### Files Modified
- N:\work\WD\AgentCascade_unified\agent_cascade\api_server.py (7 edits total, all green from review)