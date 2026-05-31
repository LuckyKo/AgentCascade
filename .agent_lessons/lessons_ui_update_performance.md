# UI Update Performance — Key Findings

## The Three Bottlenecks

### 1. Heavy Serialization on Every Yield
- `build_stream_update_from_pool()` in `api_integration.py:388` serializes ALL instances on every yield
- For each instance: serializes all messages (or last 3 if >30), calculates token stats, gets max tokens
- Complexity: O(I × N) per yield where I = instances, N = total messages
- The "last 3 messages" optimization at line 671-678 only helps with message serialization, NOT token counting

### 2. Tool Execution Blocks the Main Loop
- ExecutionEngine.run() (execution_engine.py:262) only yields on phase transitions
- During `call_agent`, the engine is blocked for seconds/minutes with no yields
- Sub-agent streaming in `_create_and_run_agent()` (line 1726-1771) pushes updates every 150ms
- BUT it calls `build_stream_update_from_pool()` which has the same serialization problem

### 3. Frontend Rendering Cost
- `renderSubAgents()` (app.js:2228) processes ALL agents on every stream_update
- `renderAgentConversation()` creates DOM elements for each message including markdown rendering
- 100ms throttle (app.js:1156) rarely kicks in because "content changed" is true during streaming

## Key Code Locations

| File | Line | Function | Issue |
|------|------|----------|-------|
| api_integration.py | 388-481 | build_stream_update_from_pool() | Serializes ALL instances |
| api_integration.py | 639-702 | _serialize_instance() | Serializes all messages + token stats |
| execution_engine.py | 262-331 | ExecutionEngine.run() | Only yields on phase transitions |
| execution_engine.py | 1726-1771 | Sub-agent streaming loop | Calls heavy build_stream_update_from_pool |
| run_agent_unified.py | 156 | Throttle check | 150ms throttle |
| app.js | 1154-1156 | subThrottleContent | 100ms throttle |
| app.js | 2228-2351 | renderSubAgents() | Processes all agents |
| app.js | 2353-2531 | renderSubAgentPanel() | Content key + DOM operations |
| api_server.py | 584 | send_queue | maxsize=32, drops stale updates |

## Quick Wins

1. **Incremental serialization** — Only serialize instances whose state changed (track version/timestamp per instance)
2. **Token stat caching** — Cache get_history_stats() per instance, only recalculate on message add
3. **Increase send_queue** — From 32 to 128 to reduce dropped updates
4. **Lightweight sub-agent updates** — Push just status changes during tool execution, not full snapshots