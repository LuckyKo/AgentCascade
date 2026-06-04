# Compression Context Tool Desync Analysis

## Executive Summary

This document traces the complete code path when a sub-agent calls `compress_context` as a tool from its `_run()` loop, and analyzes potential desynchronization between the sub-agent's local messages and the pool.

---

## 1. Code Path: Sub-Agent Calls compress_context Tool

### Step-by-Step Trace

**1.1 Orchestrator invokes sub-agent (`_stream_sub_agent_call`, line 2081)**
```python
working_history = self.agent_pool.slice_history_for_llm(conv)  # conv = pool ref
for resp in agent.run(working_history, agent_instance_name=instance_name):
```

**1.2 Sub-agent's `_run()` loop (fncall_agent.py, line 74)**
```python
def _run(self, messages: List[Message], ...) -> Iterator[List[Message]]:
    messages = copy.deepcopy(messages)  # LOCAL COPY of working_history
```

**Key Point:** The sub-agent works on a `deepcopy` of the pool's conversation. The local `messages` variable is independent of the pool at this point.

**1.3 Sub-agent calls compress_context tool (fncall_agent.py, line 102)**
```python
tool_result = self._call_tool(tool_name, tool_args, messages=messages, **kwargs)
```

The `messages` kwarg passed here is the sub-agent's LOCAL COPY (not the pool reference).

**1.4 compression_tools.py call() method (lines 87-97)**
```python
result = compress_context(
    agent_pool=self.agent_pool,        # Pool reference from tool init
    target_agent_name=agent_name,
    fraction=fraction,
    ...
)
```

**1.5 core.py compress_context() — Pool Mutation (line 274)**
```python
new_history = history[:active_start_idx] + [marker_message] + history[insert_pos:]
agent_pool.instance_conversations[target_agent_name] = new_history  # POOL IS MODIFIED
```

**Critical:** The pool is directly modified here. `conv` in `_stream_sub_agent_call` (which is a reference to the pool) now contains compressed data.

**1.6 compression_tools.py — Post-Compression Sync (lines 99-102)**
```python
if result.success:
    # Rebuild caller's working set from pool (single source of truth)
    if 'messages' in kwargs and not dry_run:
        rebuild_working_set(kwargs['messages'], self.agent_pool, agent_name)
```

**1.7 helpers.py rebuild_working_set() (lines 79-85)**
```python
compressed = agent_pool.get_conversation(agent_name)  # Get compressed pool data
if not compressed:
    return
messages_list.clear()
messages_list.extend(copy.deepcopy(compressed))  # Sync local copy to pool state
```

**Result:** After compression succeeds, the sub-agent's local `messages` is synced back from the pool. **NO DESYNC after successful compression.**

---

## 2. The conv Reference in _stream_sub_agent_call

### Line 1939: Conv = Pool Reference
```python
conv = self.agent_pool.get_conversation(instance_name)
# Returns: instance_conversations[instance_name] (direct reference, NOT a copy)
```

### Line 2076: Working History Extraction
```python
working_history = self.agent_pool.slice_history_for_llm(conv)
```

`conv` is the pool reference. After compression modifies the pool (line 274 of core.py), `conv` automatically reflects the compressed state since it's a direct reference.

### Line 2105: State Sync for UI
```python
state['messages'] = list(conv) + list(resp)
```

This uses `conv` (pool reference) + the sub-agent's current response. If compression happened during the sub-agent's run, `conv` already contains compressed data. **This is correct.**

---

## 3. Desync Analysis Summary

### After Successful Compression: NO DESYNC

| Component | State After Compression |
|-----------|------------------------|
| Pool (`instance_conversations[name]`) | Compressed (new_history) |
| `conv` (pool reference) | Points to compressed pool |
| Sub-agent's local `messages` | Rebuilt from pool via rebuild_working_set() |
| `_stream_sub_agent_call` state | Uses `conv` + resp, both in sync |

### Why There's No Desync After Success

1. Pool is modified directly (core.py line 274)
2. `conv` is a reference to the pool — automatically sees changes
3. `rebuild_working_set()` copies from pool back to sub-agent's local messages
4. All three sources (pool, conv, local messages) end up with identical data

---

## 4. Forced Compression Failure Path

### Lines 804-814 of agent_orchestrator.py

```python
else:
    logger.error(f"Forced compression failed for {instance_name}: {result.error}")
    notification = (
        f"[SYSTEM NOTIFICATION: Context exceeded {usage_pct:.1f}%, "
        f"but automatic compression failed ({result.error}). "
        f"The upcoming API call will likely fail due to length.]"
    )
    self._append_system_notification(messages, "[SYSTEM NOTIFICATION: Context exceeded", notification)
    # No compression happened — write notification-containing messages to pool
    self.agent_pool.instance_conversations[instance_name] = copy.deepcopy(messages)
    rebuild_working_set(messages, self.agent_pool, instance_name)
```

### What Happens After Forced Compression Fails

1. Notification is appended to `messages` (the caller's working set)
2. Pool is replaced with a deepcopy of messages (which now includes the notification)
3. `rebuild_working_set()` syncs messages back from pool (redundant since they were already synced)
4. Returns `True` — halts current turn

### Repeated Failure Scenario

When `_inject_compression_warning_for_agent` is called via `hooked_call_llm`:

1. Token count > 95% → enters forced compression block
2. `compress_context()` fails (e.g., "Already optimally compressed")
3. Notification appended to pool, flag set to True
4. Finally block resets flag to False
5. Returns True → `hooked_call_llm` returns early (no LLM call)
6. Sub-agent's `_run()` loop sees no output → breaks out of while loop

**On next iteration:** The flag was reset in the finally block, so the guard at line 770 doesn't trigger. Forced compression is attempted again → fails again → same cycle repeats.

### Potential Issue: Agent Gets Stuck

If forced compression keeps failing (e.g., agent has <3 messages but still >95% tokens), the sub-agent will be stuck in a loop where:
- `hooked_call_llm` always returns early (no LLM call)
- Sub-agent's `_run()` breaks after each iteration with empty response
- Control returns to orchestrator, which calls `hooked_call_llm` again
- Cycle repeats indefinitely

**This is not a crash — it's an infinite halt.** The agent never makes progress because the LLM call is blocked.

---

## 5. rebuild_working_set() — Failure Analysis

### Function (helpers.py lines 61-86)

```python
def rebuild_working_set(
    messages_list: list[Any],
    agent_pool: Any,
    agent_name: str,
) -> None:
    compressed = agent_pool.get_conversation(agent_name)
    if not compressed:
        return
    messages_list.clear()
    messages_list.extend(copy.deepcopy(compressed))
```

### Can It Fail Catastrophically?

**No.** The function has these safeguards:
1. Returns early if `compressed` is None/empty
2. Uses `copy.deepcopy()` to avoid mutating pool through reference
3. Only clears and extends the caller's list — no complex operations

**Potential edge case:** If `agent_pool.get_conversation(agent_name)` returns a different type than expected, `messages_list.clear()` / `.extend()` could fail. But this is unlikely given the consistent usage pattern.

---

## 6. agent_pool.reset() — When Is It Called?

### Only from Explicit User Commands (api_server.py)

```python
# Line 1554: POST /api/reset endpoint
@app.post("/api/reset")
async def api_reset():
    ...
    if agent_pool:
        agent_pool.reset()

# Line 1829: /rollback command handler
agent_pool.reset()

# Line 2092: WebSocket 'reset' message handler
elif msg_type == 'reset':
    ...
    agent_pool.reset()
```

### NOT Called from Compression Failure Code

There is **no code path** that calls `agent_pool.reset()` as a result of compression failure. The pool is only cleared/reset when a user explicitly sends a reset or rollback command.

---

## 7. Key Findings

### Finding 1: Successful Sub-Agent Compression Is Safe
When a sub-agent calls compress_context and succeeds, the desync concern does NOT materialize because `rebuild_working_set()` syncs the local messages back from the pool immediately after compression.

### Finding 2: Forced Compression Failure Can Stuck Agents
If forced compression fails repeatedly (e.g., "Already optimally compressed" at >95%), the sub-agent can get stuck in an infinite halt loop where:
- `_inject_compression_warning_for_agent` always returns True
- `hooked_call_llm` always returns early without making an LLM call
- The agent never makes progress

This is a **hang**, not a crash. The pool is NOT reset.

### Finding 3: Pool Reset Is User-Initiated Only
`agent_pool.reset()` is only called from explicit user commands (`/reset`, `/rollback`, WebSocket reset). Compression failures do not trigger a pool reset.

### Finding 4: No Catastrophic rebuild_working_set() Failure
The function is simple and safe — it just copies data from the pool to the caller's list.

---

## 8. Diagram: Full Code Path

```
Orchestrator._stream_sub_agent_call()
    │
    ├─ conv = get_conversation(instance_name)   ← pool reference
    │
    ├─ working_history = slice_history_for_llm(conv)
    │
    └─ agent.run(working_history, ...)           ← FnCallAgent._run()
            │
            ├─ messages = deepcopy(messages)     ← LOCAL COPY
            │
            └─ _call_tool('compress_context', ...)
                    │
                    ├─ compress_context()        ← core.py
                    │   ├─ modifies pool directly (line 274)
                    │   └─ conv sees changes (it's a reference)
                    │
                    └─ rebuild_working_set(kwargs['messages'], ...)
                            ├─ gets compressed data from pool
                            └─ syncs local messages to match
            │
            └─ Tool result → function message appended to:
                - local messages (fncall_agent.py line 112)
                - conv / pool (via _stream_sub_agent_call line 1940-1941)
                - state['messages'] (line 2105)
```

---

## 9. Potential Improvement Areas

### Area 1: Forced Compression Failure Loop Protection
Currently, if forced compression fails repeatedly for a sub-agent, the agent can get stuck indefinitely. A counter or maximum retry limit could prevent this.

### Area 2: Token Percentage Stale After Failed Compression
When forced compression fails and appends a notification, `_get_history_tokens()` counts from `messages` which was synced to pool. But if the notification is very short (e.g., <50 tokens), it won't meaningfully reduce the token count. The system keeps trying without making progress.

### Area 3: No Pool Corruption Path Found
The investigation did not find any code path where compression failure causes pool corruption that would necessitate `agent_pool.reset()`. User-reported pool resets appear to be triggered by explicit user commands, not automatic compression failure recovery.