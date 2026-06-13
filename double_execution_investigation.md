# Double-Execution Bug Investigation - Final Findings

## Question 1: Does register_async_call() ALSO call _create_and_run_agent?

**Answer: NO.** `register_async_call()` does NOT directly call `_create_and_run_agent()`.

### What register_async_call() actually does (agent_pool.py lines 1257-1310):

```python
def register_async_call(self, instance_name, function_id, agent_class, child_instance_name, args, caller, nest_depth):
    def run_child_agent() -> str:
        # This CLOSURE creates _create_and_run_agent CALLS, but doesn't call it directly here
        endpoint_release = self._acquire_slot(agent_class, child_instance_name)  # Line 1284
        engine = ExecutionEngine(self)                                          # Line 1289
        inst, child_conv = engine._create_and_run_agent(agent_class, child_instance_name, args, caller, nest_depth)  # Line 1290
        result = extract_instance_output(child_conv, child_instance_name)       # Line 1298
        return f"[Parallel Agent '{child_instance_name}' Finished]:\n{result}"  # Line 1299
    
    self._async_registry.register(instance_name, run_child_agent, function_id=function_id)  # Line 1310
```

**Key insight:** `run_child_agent()` is a **closure** (a callable) that captures `agent_class`, `child_instance_name`, `args`, `caller`, and `nest_depth` from the surrounding scope. It is **NOT executed immediately** — it's just created and passed to `AsyncToolRegistry.register()`.

### What AsyncToolRegistry.register() does (async_tools.py lines 77-100):

```python
def register(self, instance_name, tool_call, function_id=None) -> BackgroundToolEntry:
    with self._lock:
        entry = BackgroundToolEntry(
            tool_call=tool_call,           # ← This is the run_child_agent() closure
            agent_instance_name=instance_name,  # "Maine"
            function_id=function_id
        )
        self._pending.setdefault(instance_name, []).append(entry)  # Added to _pending["Maine"]
        self._executor.submit(self._execute, entry)                 # ← SUBMITTED TO THREAD POOL (NOT executed yet)
    return entry
```

**Key insight:** The `tool_call` (the `run_child_agent()` closure) is **submitted to ThreadPoolExecutor**, not called directly. The actual `_create_and_run_agent()` call happens LATER, when a thread pool worker picks up the task and calls `_execute(entry)`, which then calls `entry.tool_call()` → `run_child_agent()`.

---

## Question 2: Where does _acquire_slot + _create_and_run_agent ENTRY come from?

**Answer: From within `run_child_agent()` in `register_async_call()`, executed by the ThreadPoolExecutor worker.**

### Timeline of what happens after "EXIT (async)":

```
T+0ms: _handle_call_agent returns "Agent 'X' launched asynchronously. Waiting for result."
       ← This is the "EXIT (async)" log line
      
T+0ms: register_async_call() created run_child_agent() closure and submitted it to ThreadPoolExecutor
       
T+Nms: A thread pool worker picks up the task and calls _execute(entry)
       |
       v  _execute(entry) calls entry.tool_call():
       |
       +→ run_child_agent() executes on THREAD POOL WORKER:
          ├─ self._acquire_slot(agent_class, child_instance_name)  
          │  → Logs "[CALL_AGENT_DEBUG] _acquire_slot — ..." 
          │  (This is the "_acquire_slot" log you see at same millisecond!)
          │
          ├─ engine = ExecutionEngine(self)  ← NEW engine for CHILD agent
          │
          +→ engine._create_and_run_agent(agent_class, child_instance_name, args, caller, nest_depth)
             → Logs "[CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target=child_instance_name ..."
             (This is the "_create_and_run_agent ENTRY" log you see at same millisecond!)
```

**Both log lines come from the SAME call chain:** `run_child_agent()` → `_acquire_slot()` + `_create_and_run_agent()`. They appear at the same millisecond because `run_child_agent()` executes them sequentially in the same thread pool worker, very close together.

---

## Question 3: What happens when _create_and_run_agent completes? Does it buffer results?

**Answer: NO — _create_and_run_agent does NOT put anything into the async result buffer directly.**

### What _create_and_run_agent returns (execution_engine.py line 2818):
```python
return inst, conv  # Returns tuple of (AgentInstance, conversation history)
```

### How results get buffered (indirectly through run_child_agent → _execute):

**Step 1:** `run_child_agent()` receives the return value from `_create_and_run_agent()`:
```python
inst, child_conv = engine._create_and_run_agent(agent_class, child_instance_name, args, caller, nest_depth)
# ↑ Returns (AgentInstance, conversation list) — NOT buffered here

result = extract_instance_output(child_conv, child_instance_name)  # Extract text output
return f"[Parallel Agent '{child_instance_name}' Finished]:\n{result}"  # Return string to _execute
```

**Step 2:** `_execute()` in AsyncToolRegistry receives the return value:
```python
def _execute(self, entry):
    try:
        entry.result = entry.tool_call()  # ← This is run_child_agent()'s return string
    except Exception as e:
        entry.error = str(e)
    finally:
        with self._lock:
            entry.completed = True
            if self.pool and hasattr(self.pool, '_async_results'):
                result_msg = f"[Background Tool Result]:\n{entry.result}"  # or error
                self.pool._async_results.put(entry.agent_instance_name, result_msg, function_id=entry.function_id)
                #     ^^^^^^^^ THIS is where results get buffered — for "Maine"
```

**The buffering chain:**
1. `_create_and_run_agent()` → returns `(inst, conv)` tuple
2. `run_child_agent()` → extracts text, returns string to `_execute`
3. `_execute()` → stores in `entry.result`, marks `completed=True`, buffers to `_async_results["Maine"]`

**The async result buffer is populated by `_execute()`, NOT by `_create_and_run_agent()` directly.**

---

## Question 4: What happens after _execute_tool returns "Agent X launched asynchronously"?

### Phase 4 tool processing flow (execution_engine.py lines 1348-1470):

```python
# Phase 4: Tool detection and execution
for out in turn_output:
    use_tool, tool_name, tool_args, _ = self._detect_tool(out)
    
    if tool_name == 'call_agent':
        # _execute_tool calls _handle_call_agent which:
        #   1. Calls register_async_call() → registers BackgroundToolEntry
        #   2. Returns immediately with "Agent 'X' launched asynchronously..."
        
        tool_result = self._execute_tool(instance, "call_agent", tool_args, llm_messages, function_id)
        # ↑ This is SYNCHRONOUS — returns immediately!
        # The actual child agent runs in background (thread pool worker)
    
    # Build FUNCTION result message with the tool result text
    fn_msg = Message(
        role=FUNCTION,
        name="call_agent",
        content=tool_result,  # "Agent 'X' launched asynchronously. Waiting for result."
        extra={'function_id': function_id, 'tool_success': True},
    )
    messages.append(fn_msg)      # Full working set
    llm_messages.append(fn_msg)  # LLM API context
    response.append(fn_msg)      # Streaming UI
    instance.conversation.append(fn_msg)
```

**Key insight:** The FUNCTION result message contains the TEXT "Agent 'X' launched asynchronously. Waiting for result." — this is NOT the child agent's actual output. It's just a placeholder acknowledgment that tells the LLM: "I've launched the child agent, wait for its result."

The actual child agent result will come later via the async result buffer (injected as a USER message when Maine wakes from SLEEPING).

---

## Question 5: IS THERE A DOUBLE-EXECUTION BUG?

### Answer: NO — There is no double execution.

### Evidence: Only ONE caller of _create_and_run_agent in the entire codebase

```
grep _create_and_run_agent( → Found 2 matches:
  1. agent_pool.py line 1290: Inside run_child_agent() ← THE ONLY CALLER
  2. execution_engine.py line 2314: Method definition itself
```

### Evidence: Only ONE place where BackgroundToolEntry is created

```
grep BackgroundToolEntry( → Found 1 match:
  async_tools.py line 92: Inside AsyncToolRegistry.register() ← THE ONLY CREATOR
```

### The complete execution path (single execution, no duplicates):

```
1. LLM generates tool_use for call_agent
   ↓
2. Phase 3: _call_llm_with_injection() → returns assistant message with tool_call
   ↓
3. Phase 4: _detect_tool() → identifies "call_agent"
   ↓
4. Phase 4: _execute_tool(instance, "call_agent", args, function_id)
   ↓
5. _handle_call_agent(args, instance, function_id)
   ├─ Validates args (instance_name, agent_class)
   ├─ Checks nesting depth
   └─ register_async_call(instance_name="Maine", ...)
      ├─ Creates run_child_agent() CLOSURE (NOT executed yet)
      └─ _async_registry.register("Maine", run_child_agent, function_id)
         ├─ BackgroundToolEntry(tool_call=run_child_agent, agent_instance_name="Maine")
         ├─ Added to _pending["Maine"]
         └─ _executor.submit(_execute, entry)  ← SUBMITTED to thread pool
   ↓
6. _handle_call_agent returns "Agent 'X' launched asynchronously..." (IMMEDIATELY)
   ↓
7. Phase 4: Build FUNCTION result message with the acknowledgment text
   ↓
8. Phase 4: Return True → loop continues to next iteration
   ↓
9. Loop back to top: SLEEPING guard checks instance.state == RUNNING → SKIPPED
   ↓
10. Phase 3: _call_llm_with_injection() → LLM sees FUNCTION result, generates next turn
    ...
```

**Meanwhile, in the thread pool (concurrent):**

```
Thread pool worker picks up task:
  _execute(entry):
    entry.result = entry.tool_call()  # Calls run_child_agent():
      ├─ self._acquire_slot(agent_class, child_instance_name)
      ├─ engine = ExecutionEngine(self)  ← NEW engine for CHILD
      └─ engine._create_and_run_agent(...)  ← RUNS CHILD AGENT LOOP
         └─ engine.run(inst) through unified loop until COMPLETING/IDLE
    finally:
      entry.completed = True
      pool._async_results.put("Maine", result_msg, function_id=entry.function_id)
```

---

## Root Cause of the Actual Bug

Since there is NO double execution, the bug must be elsewhere. Here's what I believe is happening:

### Most Likely Scenario: Timing Gap Between Entry Completion and has_pending() Check

**The sequence:**

1. Maine calls 4 children async → 4 BackgroundToolEntry objects in `_pending["Maine"]`
2. Thread pool workers start executing all 4 entries concurrently
3. Entries 1 & 2 complete quickly → marked completed, results buffered to `_results["Maine"]`
4. **Phase 5 (_post_turn_checks) runs BEFORE entries 3 & 4 complete:**
   - `has_pending("Maine")` = True (entries 3 & 4 still running)
   - Transitions to SLEEPING
5. Loop back → SLEEPING guard:
   - `drain_async_results("Maine")` → catches results from entries 1 & 2 only
   - `has_pending("Maine")` → True (entries 3 & 4 may or may not be done)
6. **If entries 3 & 4 are DONE at this point:**
   - Cleanup removes `_pending["Maine"]`
   - Stable-state drain catches remaining results
   - Everything works correctly
7. **If entries 3 & 4 are NOT done yet (the BUG case):**
   - WAITING branch: `yield []`, continue
   - Thread pool workers finish entries 3 & 4
   - Loop back → SLEEPING guard drains ALL 4 results
   - Transitions to RUNNING

**The bug occurs when there's a gap between:**
- When the safety drain catches some results (2 of 4)
- When `has_pending("Maine")` is checked again
- When entries 3 & 4 actually complete and get cleaned up

### Why has_pending() Stays True After All Children Complete

If all children complete but `has_pending("Maine")` still returns True, the most likely cause is:

**Entries were NOT properly marked as completed.** This could happen if:
1. `_execute()` raised an exception before reaching the `finally` block that sets `entry.completed = True`
2. The thread pool worker crashed or was terminated before completing
3. There's a bug in how entries are tracked after completion

### Verification Needed

The fix should focus on ensuring that:
1. All 4 BackgroundToolEntry objects are properly created and registered
2. All 4 entries are marked as completed by `_execute()` when their children finish
3. The cleanup in `has_pending()` properly removes completed entries from `_pending["Maine"]`

## Summary

| Question | Answer |
|----------|--------|
| Does register_async_call call _create_and_run_agent directly? | NO — only creates a closure, submits to thread pool |
| Where do _acquire_slot + _create_and_run_agent ENTRY logs come from? | From run_child_agent() in ThreadPoolExecutor worker |
| Does _create_and_run_agent buffer results? | NO — buffers happen in AsyncToolRegistry._execute() |
| What happens after _execute_tool returns "Agent launched asynchronously"? | Phase 4 builds FUNCTION result message, loops back to Phase 3 |
| Is there a double-execution bug? | **NO** — only ONE caller of _create_and_run_agent, only ONE BackgroundToolEntry creator |

The actual bug is a **timing/race condition** between entry completion and `has_pending()` checks, NOT a double execution issue.