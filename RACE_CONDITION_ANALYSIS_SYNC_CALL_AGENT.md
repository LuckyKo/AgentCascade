# Race Condition Analysis: User Message Order During Synchronous call_agent

## Summary

**Bug:** When a user sends a message to the parent agent WHILE a child agent is running via synchronous `call_agent`, the user's message appears BEFORE the child agent's tool result in the conversation history.

**Severity:** Critical — breaks the LLM's understanding of conversation order, causing incorrect responses.

---

## Root Cause Analysis

### The Code Flow for Synchronous call_agent

The synchronous path is taken when the caller holds a concurrency slot (lines 2214-2337 in `execution_engine.py`):

```
Phase 4: _process_response()
  └── _execute_tool(instance, 'call_agent', ...)     # L2045
       └── _handle_call_agent(resolved, messages, instance, function_id)  # L2117
            └── SYNC PATH (caller holds slot):      # L2214
                 ├── Release caller's slot           # L2274
                 ├── _create_and_run_agent(...)      # L286
                      └── engine.run(child)          # L3033
                           └── Child turn loop:
                                ├── Phase 2: pre_llm_checks → drain queue (child's queue)
                                ├── Phase 3: LLM call + streaming
                                ├── Phase 4: process_response → tool exec → post-tool drain
                                └── ... repeats until child completes
                 ├── extract_instance_output(conv, instance_name)  # L2319
                 └── return f"[Agent '{instance_name}' Completed]:\n{result}"

         └── Back in _process_response:
              ├── tool_result = "[Agent 'X' Completed]:\n..."   # L1651 (return from _execute_tool)
              ├── fn_msg = Message(role=FUNCTION, content=tool_result)  # L1716-1724
              ├── messages.append(fn_msg)                       # L1725 ← TOOL RESULT APPENDED
              ├── llm_messages.append(fn_msg)                   # L1726
              ├── response.append(fn_msg)                       # L1727
              └── instance.conversation.append(fn_msg)          # L1729

              └── Post-tool drain (L1785-1789):
                   _drain_and_inject(... drain_fn=self.pool.drain_queue ...)
                    └── For each queued user message:
                         messages.append(user_msg)               # AFTER tool result
                         llm_messages.append(user_msg)           # AFTER tool result
                         response.append(user_msg)               # AFTER tool result
                         pool.add_message(inst_name, user_msg)   # AFTER tool result
```

### Expected Order (Current Code Behavior):

1. Assistant message with `tool_call` for `call_agent`
2. **FUNCTION result** from `call_agent` (child's output)
3. User message(s) that arrived during child execution

This order is **correct** in the synchronous path as coded. The tool result at L1725-1729 is ALWAYS appended BEFORE the post-tool drain at L1785-1789.

### Where the Race Condition Actually Occurs

After extensive analysis of all 8 drain points in `execution_engine.py`, I identified that the race condition manifests through **multiple interaction points**:

#### Drain Point Analysis

| # | Location | Context | What It Drains | Order Impact |
|---|----------|---------|----------------|--------------|
| 1 | L495-499 | SLEEPING guard | Async results (items) | N/A - async only |
| 2 | L502-506 | SLEEPING guard | User messages AFTER async results | Correct ordering |
| 3 | L550-554 | SLEEPING timeout | Final async results | N/A - async only |
| 4 | L580-583 | SLEEPING stable-state | Async results (while loop) | N/A - async only |
| 5 | L588-592 | SLEEPING safety drain | Async results | N/A - async only |
| 6 | **L915-919** | **PRE-LLM checks** | **User messages from queue** | **Potential issue** |
| 7 | **L1785-1789** | **POST-TOOL drain** | **User messages from queue** | **Correct ordering** |
| 8 | L1866-1870 | POST-TURN safety | Async results | N/A - async only |

#### The Critical Timing Gap

The race condition occurs through a **multi-turn interaction** between Phase 2 pre-LLM drain and Phase 4 post-tool drain:

```
Turn N (parent):
  Phase 2 [L915]: _pre_llm_checks → drain queue → no messages (False)
  Phase 3: LLM call → returns tool_call for call_agent
  Phase 4 [L1785]: _process_response
    ├── _execute_tool → sync child runs
    │   └── During child execution: user sends message → queued in message_queues[parent]
    ├── tool result appended (L1725-1729) ← CORRECT ORDER
    └── post-tool drain (L1785) → injects user message AFTER tool result ← CORRECT

  If _process_response returns True (tool used):
    yield response  # Contains: [assistant_tool_call, fn_msg, user_msg]
    Loop back to Phase 2...

Turn N+1 (parent, if loop continues):
  Phase 2 [L915]: _pre_llm_checks → drain queue → might be empty already!
```

#### The Actual Race Condition Scenario

The race condition manifests when:

1. **User message arrives during child execution** → queued in `message_queues[parent]`
2. **Child completes, tool result appended** at L1725-1729
3. **Post-tool drain at L1785** drains queue and appends user messages AFTER tool result

This ordering is CORRECT within a single `_process_response` call. However, the race condition occurs because:

**The `_pre_llm_checks` drain (L915) runs BEFORE each LLM call, not after.** If there's a timing issue where:
- The queue was drained at a PREVIOUS point (e.g., during SLEEPING state recovery)
- OR the tool result append and drain happen in separate generator yields

The user message could end up being seen by the LLM BEFORE the tool result.

### Specific Race Condition Path

After careful analysis, I believe the actual race condition occurs through this specific path:

1. Parent's `_process_response` executes `call_agent` (SYNC)
2. Child runs → user sends message to parent → queued in `message_queues[parent]`
3. Child completes → tool result appended at L1725-1729
4. Post-tool drain at L1785 drains queue → user messages appended AFTER tool result
5. `_process_response` returns True (tool was used)
6. **`yield response`** at L691 → broadcasts to frontend via WebSocket
7. Loop continues back to Phase 2
8. **Phase 2 pre-LLM drain at L915** drains any NEW messages that arrived between steps

The critical insight: **the tool result IS appended before the user message in `_process_response`**, but there may be a separate code path or timing issue where:

- The child agent's output is streamed to the frontend independently (via WebSocket push at L3072+ in `_create_and_run_agent`)
- Meanwhile, the parent's conversation gets updated separately
- The frontend might receive these updates out of order

### Why This Is a Real Bug Despite Correct Intra-Call Ordering

Even though the tool result and user message are appended in correct order within `_process_response`, there are **three potential race conditions**:

#### Race Condition 1: Frontend Display Order
The child agent's output is pushed to WebSocket via `stream_update` events during `_create_and_run_agent` (L3072+). These updates may arrive at the frontend BEFORE the parent's tool result is appended. The frontend renders messages as they arrive, potentially showing the child's output (as a separate agent tab) before the parent's conversation shows the tool result.

#### Race Condition 2: LLM API Message Order
If `_pre_llm_checks` (L915) drains messages that were queued DURING the pre-LLM phase (before the LLM call), and then the LLM call returns a `call_agent` tool, the sequence would be correct. BUT if there's a scenario where:
- Messages are drained at L915 (pre-LLM)
- Then `_process_response` appends tool result
- Then drain at L1785

The order should still be correct. **Unless** there's a code path where `drain_queue` is called TWICE between the tool append and the next LLM call, injecting user messages before the tool result.

#### Race Condition 3: Multiple Tools in Same Turn
If the LLM returns MULTIPLE tool calls in one turn (including `call_agent`), each tool's result is appended sequentially. If a user message arrives during any of these tool executions, it gets queued and drained at L1785 AFTER all tools complete. This should be correct. **However**, if there's an early break from the tool loop (due to halt/stop at L1628), orphaned tools get placeholder results at L1743-1780, and then drain happens at L1785. The order should still be: all tool results → user messages.

### The Most Likely Root Cause

After thorough analysis, I believe the most likely root cause is related to **how `_drain_and_inject` handles multiple items**:

```python
# In _drain_and_inject (L345-367):
for item in raw_data:
    msg = factory(item)
    if not msg.content.strip():
        continue
    messages.append(msg)
    llm_messages.append(msg)
    response.append(msg)
    self.pool.add_message(inst_name, msg)  # Also appends to instance.conversation
```

Each user message is appended individually. If the tool result was already appended, and then user messages are appended one by one, the order should be: `[tool_result, user_msg_1, user_msg_2, ...]`. This is correct.

**However**, if `pool.add_message()` (L356) somehow triggers a side effect that modifies the conversation in a way that affects message ordering — for example, if it also writes to JSONL and there's a separate reader reading from JSONL rather than memory — then the displayed order could differ from the in-memory order.

## Verification Steps

To verify this race condition, add logging at these critical points:

```python
# In _process_response, after L1729 (tool result appended):
logger.debug(f"[RACE_DEBUG] TOOL RESULT APPENDED to {inst_name}: {tool_result[:100]}")
logger.debug(f"[RACE_DEBUG] Conversation state after tool append: {[m.get('role') for m in instance.conversation[-3:]]}")

# In _drain_and_inject, before L352 (user message appended):
logger.debug(f"[RACE_DEBUG] DRAIN STARTING for {inst_name}: {len(raw_data)} messages")
for item in raw_data:
    logger.debug(f"[RACE_DEBUG] DRAINING user message: {str(item)[:100]}")

# In _drain_and_inject, after L356 (user message appended):
logger.debug(f"[RACE_DEBUG] USER MESSAGE APPENDED to {inst_name}: {msg.content[:100]}")
logger.debug(f"[RACE_DEBUG] Conversation state after user msg: {[m.get('role') for m in instance.conversation[-3:]]}")
```

## Additional Investigation Needed

Despite thorough static analysis showing correct intra-call ordering, the user reports the bug occurs in practice. This suggests either:

1. **A timing condition I haven't identified** — possibly involving multiple concurrent threads or WebSocket event ordering
2. **A frontend rendering issue** — where messages appear out of order due to async WebSocket delivery timing
3. **A specific edge case** — such as a particular combination of settings, agent configurations, or message patterns

To definitively identify the root cause, add these debug logs:

```python
# In _process_response, after L1729 (tool result appended):
logger.info(
    f"[RACE_DEBUG] {inst_name}: TOOL RESULT APPENDED — "
    f"conversation_roles={[m.get('role') for m in instance.conversation[-4:] if isinstance(m, dict)]}"
)

# In _drain_and_inject, just before L352:
logger.info(
    f"[RACE_DEBUG] {inst_name}: DRAIN START — {len(raw_data)} messages to inject"
)

# In _drain_and_inject, after L367 (all messages injected):
logger.info(
    f"[RACE_DEBUG] {inst_name}: DRAIN COMPLETE — "
    f"conversation_roles={[m.get('role') for m in instance.conversation[-4:] if isinstance(m, dict)]}"
)
```

## Recommended Fix

The fix should ensure that user messages queued during sync `call_agent` execution are **not** drained until AFTER all tool results are appended AND the next LLM call begins. This could be achieved by:

1. **Option A**: Defer queue draining for agents executing sync children — set a flag `_skip_queue_drain` on the parent instance when entering sync child execution, and clear it after post-tool drain completes.

2. **Option B**: Use a per-message timestamp or sequence number to sort messages correctly regardless of insertion order.

3. **Option C**: Ensure that `pool.add_message()` and the working list appends happen atomically under a single lock, preventing any interleaving with other operations.

The root cause appears to be that `drain_queue` at L1785 drains user messages AFTER tool results are appended, which should be correct — but there may be an interaction with how the frontend or another component reads from the conversation that causes the perceived ordering issue.