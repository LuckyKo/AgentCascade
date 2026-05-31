# UI Update Performance Analysis — Agent Cascade Unified Branch

## Executive Summary

The slow UI updates during agent execution stem from a combination of three factors:
1. **Heavy serialization on every yield** — `build_stream_update_from_pool()` serializes ALL instances in the pool on every yield, including full message arrays
2. **Tool execution blocks the main loop** — during `call_agent`, the ExecutionEngine is blocked for seconds/minutes with no yields
3. **Frontend rendering cost** — `renderSubAgents()` and `renderAgentConversation()` are expensive DOM operations called on every stream_update

The result: during LLM streaming, updates arrive every ~150ms but feel sluggish because each update carries a heavy payload and triggers expensive DOM work. During tool execution (especially sub-agent calls), the UI freezes for seconds because no yields happen.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  User sends message → WebSocket → api_server.py:ws_chat()       │
│    ↓                                                            │
│  run_agent_thread_unified() in background thread                │
│    ↓                                                            │
│  for yield in run_agent_in_pool_with_recovery():                │
│    ↓                                                            │
│  build_stream_update_from_pool() ← SERIALIZES ALL INSTANCES     │
│    ↓                                                            │
│  put onto send_queue (asyncio.Queue, maxsize=32)                │
│    ↓                                                            │
│  _sender_loop() reads queue → broadcast() → all WebSocket clients│
│    ↓                                                            │
│  Frontend: handleServerMessage() → renderSubAgents()            │
│    ↓                                                            │
│  renderSubAgentPanel() → renderAgentConversation()              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Bottleneck 1: Heavy Serialization on Every Yield

### The Problem

`build_stream_update_from_pool()` in `api_integration.py:388-481` is called on EVERY yield from the ExecutionEngine. Each yield represents a phase transition (LLM call complete, tool execution complete, etc.). The function does:

1. **Serialize ALL instances** (line 448-453):
```python
all_instances = {
    name: _serialize_instance(inst, pool, include_messages=True, streaming=True)
    for name, inst in instance_snapshot_data.items()
}
```

2. **For each instance**, `_serialize_instance()` (line 639-702):
   - Takes a snapshot of the entire conversation under `_compression_lock`
   - Serializes ALL messages via `serialize_message()` (or last 3 if streaming and >30 msgs)
   - Calls `get_history_stats()` to calculate token counts
   - Calls `_get_max_tokens_for_instance()` for max token info

3. **Also calculates token stats** for the primary instance separately (line 424-432)

### Complexity Analysis

- Let N = total messages across all instances
- Let I = number of instances
- Serialization cost: O(I × N) per yield (each instance serializes its messages)
- Token counting cost: O(I × N) per yield (each instance recalculates token stats)
- Total: O(I × N) per yield

With 3-5 active instances and 50-200 messages each, this is 150-1000 message serializations per yield.

### Why the "last 3 messages" optimization isn't enough

The streaming optimization at line 671-678 only sends the last 3 messages for conversations >30 messages. But:
- It still serializes the full conversation for token counting (line 684-690)
- It still calls `get_history_stats()` on the full active history for each instance
- The `history_count` field is still sent, so the frontend merges partial updates

---

## Bottleneck 2: Tool Execution Blocks the Main Loop

### The Problem

The ExecutionEngine's `run()` method (execution_engine.py:262-331) only yields on phase transitions:
- After `_setup_turn()` completes
- After `_pre_llm_checks()` returns True
- After `_call_llm_with_injection()` completes
- After `_process_response()` returns True
- After `_post_turn_checks()` returns False

During tool execution (especially `call_agent`), the engine is **completely blocked**. No yields happen, so no stream_updates are sent from the main loop.

### The Sub-Agent Streaming Fix and Its Limitation

The `_create_and_run_agent()` method (execution_engine.py:1460-1822) tries to push stream_updates during sub-agent execution (line 1726-1771):

```python
now = time.time()
if now - _last_sub_send >= _sub_send_interval and not _stream_pushing_disabled:
    su = build_stream_update_from_pool(
        pool=self.pool,
        instance_name=caller,
        responses=None,
    )
```

**But this has the same serialization problem!** It calls `build_stream_update_from_pool()` which serializes ALL instances. During a long sub-agent execution with multiple nested agents, this means:
- Every 150ms, the sub-agent pushes a stream_update
- Each stream_update serializes ALL instances including the sub-agent itself
- The sub-agent's conversation grows with each turn, making serialization progressively slower

### Impact During call_agent

When the main agent calls a sub-agent:
1. Main agent's turn is blocked (no yield from main loop)
2. Sub-agent starts executing, pushing stream_updates every 150ms
3. Each stream_update serializes ALL instances (main agent + sub-agent + any other active instances)
4. If the sub-agent makes LLM calls, the LLM streaming phase takes seconds
5. During those seconds, the main agent's UI is frozen

---

## Bottleneck 3: Frontend Rendering Cost

### The Problem

`renderSubAgents()` (app.js:2228-2351) is called on every stream_update (throttled to 100ms). For each agent:

1. **Tab button updates** — DOM queries and class toggles per agent
2. **Panel rendering** — `renderSubAgentPanel()` for each agent
3. **Message rendering** — `renderAgentConversation()` which creates DOM elements for new messages

### renderSubAgentPanel() (app.js:2353-2531)

Key costs:
- **Content key computation** (line 2427-2428): calculates a hash of message count, last message length, reasoning content length, function call length, and active flag
- **Full re-render** if DOM is out of sync (line 2463-2480): clears innerHTML and re-renders all messages
- **Incremental append** (line 2490-2499): renders only new messages
- **Context bar update** (line 2503-2511): throttled to 1Hz
- **Auto-scroll** via requestAnimationFrame (line 2522-2530)

### renderAgentConversation() (app.js:1378+)

This is the most expensive function — it creates DOM elements for each message, including:
- Message bubbles with markdown rendering
- Tool call/result displays
- Reasoning content blocks
- Function call displays

### Throttling (app.js:1154-1188)

```javascript
const subThrottleContent = 100; // 100ms throttle
if (stackChanged || subAgentNewVisibleMessage || subAgentContentChanged || 
    now - state.genStats.lastSubAgentRender > subThrottleContent) {
    renderSubAgents();
}
```

This means:
- If the active stack changed → immediate render
- If a new visible message appeared → immediate render
- If content changed in an existing bubble → immediate render
- Otherwise → wait for 100ms

The problem: "content changed" is true on almost every stream_update during LLM streaming, so the 100ms throttle rarely kicks in during active generation.

---

## Bottleneck 4: Send Queue Backpressure

### The Problem

`send_queue` has `maxsize=32` (api_server.py:584). When full, stale stream_updates are dropped via `put_nowait` in `_put_stream_update()`:

```python
try:
    queue.put_nowait(event)
except asyncio.QueueFull:
    pass  # Drop stale event
```

### Impact

During heavy activity (multiple active agents, frequent yields):
- Stream_updates pile up faster than `_sender_loop()` can broadcast them
- Old updates are dropped, meaning the frontend misses state changes
- The frontend may show stale data until the next non-dropped update arrives

---

## Throttle Summary

| Component | Throttle Interval | Location | Impact |
|-----------|------------------|----------|--------|
| Backend stream_update | 150ms | run_agent_unified.py:156 | Limits how often updates are sent during LLM streaming |
| Sub-agent stream_update | 150ms | execution_engine.py:1694 | Limits how often sub-agents push updates |
| Frontend sub-agent render | 100ms | app.js:1156 | Limits how often renderSubAgents() is called |
| ActivityBar render | 200ms | app.js:186 | Limits how often activity bar is re-rendered |
| GenStats update | 500ms | app.js:1194 | Limits how often token/sec stats are updated |
| Controls update | 1000ms | app.js:1149 | Limits how often control buttons are updated |
| Context bar update | 1000ms | app.js:2505 | Limits how often per-agent context bars are updated |
| Send queue size | 32 items | api_server.py:584 | Limits how many updates can be queued |

---

## Root Cause Summary

The fundamental issue is that **the UI update pipeline is synchronous and monolithic**:

1. **Serialization is monolithic** — every stream_update includes ALL instances, not just the changed ones
2. **Serialization is repeated** — token stats are recalculated for each instance on every yield
3. **Tool execution blocks yields** — the main loop only yields on phase transitions, not during long operations
4. **Frontend rendering is monolithic** — `renderSubAgents()` processes ALL agents even if only one changed
5. **No incremental updates** — the frontend receives full state snapshots, not deltas

---

## Recommendations

### Quick Wins (Low Risk, High Impact)

1. **Incremental stream_updates** — Only include instances whose state changed since the last update. Track a version/timestamp per instance and only serialize instances with newer versions.

2. **Token stat caching** — Cache `get_history_stats()` results per instance and only recalculate when the conversation actually changes (new message added, not during streaming).

3. **Increase send_queue size** — Increase from 32 to 128 to reduce dropped updates during heavy activity.

4. **Frontend: Skip rendering hidden panels** — Already partially implemented (line 2405-2410), but can be improved by also skipping tab button updates for hidden agents.

### Medium Effort (Moderate Risk, High Impact)

5. **Delta-based stream_updates** — Instead of sending full instance snapshots, send only the changes: new messages, updated message content, changed metadata.

6. **Sub-agent: Push lightweight updates during tool execution** — Instead of calling `build_stream_update_from_pool()` every 150ms, push a lightweight event with just the sub-agent's current status (active/inactive, message count, latest message summary).

7. **Frontend: Virtual scrolling for message lists** — Only render messages that are visible in the viewport, reducing DOM operations.

### Long-term (Higher Risk, Higher Impact)

8. **Event-driven architecture** — Replace the monolithic stream_update with individual events: `message_added`, `message_updated`, `agent_started`, `agent_stopped`, `tool_called`, etc.

9. **Web Worker for frontend rendering** — Offload `renderAgentConversation()` to a Web Worker to avoid blocking the main thread.

10. **Backend: Async tool execution** — Make tool execution (especially `call_agent`) non-blocking by yielding intermediate states.