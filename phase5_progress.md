# Phase 5 Integration Progress - AgentCascade Unified Architecture

## Completed Changes to api_server.py

### Step 1: Simplified run_agent_thread (DONE)
- `run_agent_thread()` delegates to `run_agent_thread_unified()` from our new module
- Signature preserved for backward compatibility with all 3 call sites in WebSocket handler
- Removed the dead legacy fallback code (~200 lines of commented-out truncated code)
- `run_agent_thread_unified` handles everything internally: pool init, instance creation, UI config injection, loop detection, recovery, final state broadcast

### Step 2: Simplified build_state (DONE)
- Delegates to `build_state_from_pool()` — single unified path
- Removed the dual-path feature detection and `_build_state_legacy()` (~85 lines of dead code)
- Returns minimal empty state if instance not yet created
- Adds `agent_index` field for frontend compatibility

### Step 3: Simplified build_stream_update (DONE)
- Delegates to `build_stream_update_from_pool()` — already was clean
- Falls back to minimal state dict if unified build fails
- Legacy parameters accepted but ignored (backward compatible signature)

### Step 4: Added Compatibility Shims to New AgentPool (DONE)
Added 14 compatibility shims to `agent_cascade/agent_pool.py`:
1. `is_halted(name)` → alias for `is_instance_halted(name)`
2. `list_agents()` → returns `list(self.templates.keys())`
3. `reset()` → clears halted instances, active stack, etc.
4. `active_stack` property → delegates to `_execution.active_stack`
5. `last_tool_args` attribute → dict initialized in __init__
6. `rollback_to_snapshots(snapshots)` → truncates instance conversations
7. `capture_snapshots()` → returns `{name: len(inst.conversation)}`
8. `load_session_from_log(path, target_instance)` → full JSONL session restore
9. `refresh_agents()` → re-scans agents directory
10. `instance_classes` property → derived from instances dict
11. `instance_loggers` property → delegates to LoggerManager
12. `instance_summaries` attribute → mutable dict
13. `_ws_loop` attribute → None by default, set at runtime
14. `agents` property → alias for `self.templates`

Also created `_InstanceConversationMapping` class that provides bidirectional sync between `pool.instance_conversations[name]` and `pool.instances[name].conversation`.

### Step 5: Switched Pool Import (DONE)
- Changed `from agent_pool import AgentPool` → `from agent_cascade.agent_pool import AgentPool`
- Updated constructor call to match new signature (no more idle_timeout_seconds/idle_check_interval as args, now set via PoolSettings)
- Instance creation: `agent_pool.create_instance('Maine', 'orchestrator')` + `agent_pool.get_agent('orchestrator')`

### Step 6: Updated WebSocket Handler — User Message Path (DONE)
- Main message handler (msg_type == 'message') now uses unified path:
  - Creates main agent instance if needed via `create_main_agent_instance()`
  - Adds user message via `agent_pool.add_message()` instead of `session['history'].append()`
  - No more `copy.deepcopy(session['history'])` — run_agent_thread_unified reads from pool directly

### Step 7: Updated WebSocket Handler — Retry/Resume Paths (DONE)
- Retry path after rollback now uses unified pool methods
- Fallback branches that used `session['history'].append()` replaced with pool-based approach

## What's Left to Do

### Remaining session['history'] References (Lower Priority)
Several fallback paths still reference `session['history']`. These are safety nets during transition:

| Location | Usage | Risk Level | Action |
|----------|-------|------------|--------|
| Line 498-500 | Dual-read wrapper fallback in `get_session_history()` | Low — only used if unified store empty | Keep for now, remove when fully confident |
| Line 557-559 | Session load: `_load_session_history()` → `session['history']` → sync to pool | Medium — initial session loading | Update in Phase 6 |
| Line 1053 | `/api/reset`: `session['history'] = []` | Low — clears legacy state alongside pool | Remove when session['history'] eliminated |
| Lines 1544-1557 | Rollback command fallback paths | Low — only if pool unavailable | Clean up in Phase 6 |
| Line 1623 | Reset handler: `session['history'] = []` | Low — parallel cleanup | Remove when session['history'] eliminated |
| Lines 2089, 2156 | REST endpoint fallbacks for history reads | Low — only if pool unavailable | Clean up in Phase 6 |

### Next Steps (Phase 6)
1. **Eliminate `session['history']` entirely** from api_server.py — the remaining references are transition artifacts
2. **Replace `_load_session_history()` with pool-based session loading** via `pool.load_session_from_log()`
3. **Remove dual-read wrapper `get_session_history()`** — only unified path needed
4. **Clean up agent_orchestrator.py** — remove `_stream_sub_agent_call()`, replace with ExecutionEngine path
5. **Test end-to-end**: Start server, send message, verify sub-agent calls work, compression works, loop detection works

## Files Modified
- `api_server.py` — cleaned up run_agent_thread, build_state, build_stream_update (~288 lines removed); switched pool import; updated WebSocket handler for unified path
- `agent_cascade/agent_pool.py` — added 14 compatibility shims + `_InstanceConversationMapping` class

## Files NOT Modified (as instructed)
- `agent_orchestrator.py` — Phase 6
- Compression files (core.py, helpers.py) — untouched
- Frontend files — Phase 7