# Race Condition Root Cause Report: User Message Order During Synchronous call_agent

## Executive Summary

**Bug:** When a user sends a message to the parent agent while a child agent is executing via synchronous `call_agent`, the user's message appears BEFORE the child agent's tool result in the conversation.

**Status:** Static analysis shows the code **should** produce correct ordering (tool result → user messages). The bug likely manifests through one of three mechanisms:
1. Frontend WebSocket timing issue
2. A specific edge case not caught by static analysis
3. Interaction between multiple drain points across turns

---

## Complete Drain Point Inventory

All 8 drain points in `execution_engine.py`:

| # | Line | Context | Drain Type | Target |
|---|------|---------|-----------|--------|
| 1 | L495-499 | SLEEPING guard | items mode | Async results |
| 2 | L502-506 | SLEEPING guard | drain_fn | User messages (AFTER async) |
| 3 | L550-554 | SLEEPING timeout | items mode | Final async results |
| 4 | L580-583 | SLEEPING stable-state | drain_fn | Async results (while loop) |
| 5 | L588-592 | SLEEPING safety | drain_fn | Async results |
| 6 | **L915-919** | **PRE-LLM checks** | **drain_fn** | **User messages from queue** |
| 7 | **L1785-1789** | **POST-TOOL drain** | **drain_fn** | **User messages from queue** |
| 8 | L1866-1870 | POST-TURN safety | drain_fn | Async results |

---

## Synchronous call_agent Flow Analysis

### Phase-by-Phase Execution

```
Parent Turn N:
├── Phase 2 [L915]: _pre_llm_checks → drain queue
│   └── Queue is empty (no messages yet) → returns False
│
├── Phase 3 [L645]: LLM call
│   └── Returns: tool_call for 'call_agent'
│
└── Phase 4 [L1785-1789]: _process_response
    ├── Detects 'call_agent' tool in turn_output
    │
    ├── _execute_tool(instance, 'call_agent', ...) → _handle_call_agent(...)
    │   └── SYNC PATH (caller holds slot):
    │       ├── Releases caller's slot [L2274]
    │       ├── _create_and_run_agent(agent_class, instance_name, args, ...) [L286]
    │       │   └── engine.run(child) → child turn loop
    │       │       └── During child execution:
    │       │           └── User sends message → send_message() → message_queues[parent].append(text)
    │       │
    │       ├── Child completes → returns (inst, conv)
    │       ├── extract_instance_output(conv, instance_name) → result_str
    │       └── Returns: f"[Agent '{instance_name}' Completed]:\n{result}"
    │
    ├── _execute_tool() returns tool_result string [L1651]
    │
    ├── Build fn_msg = Message(role=FUNCTION, content=tool_result) [L1716-1724]
    │
    ├── Append to all working lists [L1725-1731]:
    │   ├── messages.append(fn_msg)          ← TOOL RESULT #1
    │   ├── llm_messages.append(fn_msg)      ← TOOL RESULT #2
    │   ├── response.append(fn_msg)          ← TOOL RESULT #3
    │   └── instance.conversation.append(fn_msg) ← TOOL RESULT #4
    │
    └── Post-tool drain [L1785-1789]:
        └── _drain_and_inject(... drain_fn=self.pool.drain_queue ...):
            ├── raw_data = self.pool.drain_queue(inst_name)  → gets user message(s)
            └── For each user message:
                ├── messages.append(user_msg)          ← USER MSG #1 (AFTER tool result)
                ├── llm_messages.append(user_msg)      ← USER MSG #2 (AFTER tool result)
                ├── response.append(user_msg)          ← USER MSG #3 (AFTER tool result)
                └── pool.add_message(inst_name, user_msg) ← USER MSG #4 (AFTER tool result)
```

### Expected Final Order in All Lists:

```
[0]: system message
[1]: assistant message with tool_call for 'call_agent'
[2]: FUNCTION result from 'call_agent' (child's output)     ← TOOL RESULT
[3]: user message that arrived during child execution       ← USER MSG
```

**This order is CORRECT.** The tool result at index [2] comes BEFORE the user message at index [3].

---

## Potential Race Condition Scenarios

### Scenario A: Multi-Turn Interaction (Most Likely)

If `_process_response` returns True (tool was used), the loop continues:

```
Turn N, Phase 4 → _process_response returns True (tool used)
├── yield response at L691
│   └── Frontend receives: [..., assistant_tool_call, fn_msg(tool_result), user_msg]
└── Loop continues to Turn N+1, Phase 2

Turn N+1, Phase 2 [L915]: _pre_llm_checks → drain queue
├── Queue may be empty (already drained at L1785)
└── OR: New messages arrived between yield and next Phase 2 check
```

If new user messages arrive between the yield at L691 and the pre-LLM drain at L915 of Turn N+1, they would be injected BEFORE the next LLM call. But since the tool result from Turn N was already in the conversation, this should not cause ordering issues.

### Scenario B: Frontend WebSocket Timing Issue

The child agent pushes `stream_update` events to the frontend via WebSocket during `_create_and_run_agent` (L3072+). These updates may arrive at the frontend BEFORE or AFTER the parent's tool result is appended and yielded. If the frontend renders messages based on WebSocket arrival order rather than message sequence numbers, the display could show incorrect ordering.

### Scenario C: Edge Case - Exception Path

If `_execute_tool` raises an exception that is NOT caught by the inner `except Exception as e` (e.g., a non-Exception subclass), the tool result would never be appended to the lists. However, all exceptions in Python inherit from `BaseException`, and the LLM call path doesn't raise non-Exception types. The only way to skip the append is if `_execute_tool` returns without raising AND without returning a string — which it always does for `call_agent`.

---

## Recommended Debugging Approach

### Step 1: Add Detailed Logging

Add these logs to capture exact ordering:

```python
# In _process_response, AFTER L1729 (tool result appended to conversation):
logger.info(
    f"[RACE_DEBUG] {inst_name}: TOOL_RESULT_APPENDED - "
    f"last_5_roles={[m.get('role') for m in instance.conversation[-5:]]}"
)

# In _drain_and_inject, BEFORE the for loop (L344):
if drain_fn and raw_data:
    logger.info(
        f"[RACE_DEBUG] {inst_name}: DRAIN_START - "
        f"queue_size={len(raw_data)}, last_5_roles_before={[m.get('role') for m in instance.conversation[-5:]]}"
    )

# In _drain_and_inject, AFTER the for loop (L367):
if raw_data:
    logger.info(
        f"[RACE_DEBUG] {inst_name}: DRAIN_COMPLETE - "
        f"last_5_roles_after={[m.get('role') for m in instance.conversation[-5:]]}"
    )
```

### Step 2: Reproduce and Analyze

1. Start the agent system with detailed logging enabled
2. Trigger a `call_agent` tool call on the parent
3. While the child is running, send a message to the parent
4. Check logs for `[RACE_DEBUG]` entries to verify ordering

### Step 3: Verify Frontend Behavior

1. Monitor WebSocket messages in browser DevTools
2. Track the order of `stream_update` events received
3. Compare WebSocket arrival order with expected conversation order

---

## Recommended Fix (If Bug Confirmed)

If logging confirms that user messages appear before tool results:

### Option 1: Mark-and-Defer Drain (Recommended)

```python
# In _handle_call_agent, SYNC PATH, before running child:
instance._pending_tool_drain = True  # Mark: defer queue drain until after tool result

# In _process_response, post-tool drain at L1785:
if self._drain_and_inject(...):
    instance._pending_tool_drain = False  # Clear marker after drain

# In _pre_llm_checks at L915:
if getattr(instance, '_pending_tool_drain', False):
    # Skip drain - it will happen in post-tool drain instead
    pass
else:
    if self._drain_and_inject(...):
        return True
```

### Option 2: Single Atomic Append

Modify `_drain_and_inject` to batch all appends:

```python
def _drain_and_inject(self, ...):
    # ... existing code ...
    
    if not raw_data:
        return False
    
    # Collect all new messages first
    new_messages = [factory(item) for item in raw_data if factory(item).content.strip()]
    
    # Append ALL at once (atomic from the perspective of list ordering)
    for msg in new_messages:
        messages.append(msg)
        llm_messages.append(msg)
        response.append(msg)
        self.pool.add_message(inst_name, msg)
```

### Option 3: Fix Frontend WebSocket Ordering

Add sequence numbers to stream_update events and sort on the frontend:

```python
# In run_agent_thread_unified:
stream_update['_sequence'] = tick_num  # Add monotonic sequence number

# In frontend: sort messages by _sequence before rendering
```

---

## Files Modified/Created

1. `RACE_CONDITION_ANALYSIS_SYNC_CALL_AGENT.md` - Detailed technical analysis
2. `RACE_CONDITION_ROOT_CAUSE_REPORT.md` - This file (executive summary + actionable findings)

## Key Code Locations

- `execution_engine.py:L2214-2337`: SYNC path for call_agent
- `execution_engine.py:L2672-3182`: `_create_and_run_agent` (child execution)
- `execution_engine.py:L1501-1793`: `_process_response` (tool execution + post-tool drain)
- `execution_engine.py:L1785-1789`: Post-tool queue drain
- `execution_engine.py:L915-919`: Pre-LLM queue drain
- `agent_pool.py:L1204-1229`: Message queue operations