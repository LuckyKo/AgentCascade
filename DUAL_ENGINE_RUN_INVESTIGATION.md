# Dual `engine.run()` Investigation: Root Cause Analysis

## Executive Summary

Investigated the bug where TWO `engine.run()` calls for the same agent (Maine) appear to execute concurrently during a synchronous `call_agent` child execution. Found that while **no obvious code path starts two concurrent threads**, there is a **critical race condition** at the WebSocket message handler that allows it under specific timing conditions.

---

## 1. Code Flow Analysis: SYNC Path in `_handle_call_agent`

### Normal Execution Sequence

```
Thread A (Maine's first run):
  engine.run(Maine) ENTRY                          # Line ~396
  Phase 1: _setup_turn → local [messages, llm_messages, response]
  ...turn loop iterations...
  LLM returns call_agent(TestAgent) tool_call
  Phase 4: _process_response:
    └── _execute_tool(instance, 'call_agent', ...)
         └── _handle_call_agent (SYNC path)
              ├── Line 2274: self._release_slot(Maine)     ← Slot released!
              ├── Line 2286: _create_and_run_agent(TestAgent)
              │    └── engine.run(TestAgent)                ← BLOCKS here
              │         └── TestAgent executes turns...
              │         └── TestAgent completes
              │    └── Returns result string
              └── Returns to _process_response

  Back in _process_response (still Thread A):
    ├── Line 1725-1729: Append fn_msg (tool result) to messages, llm_messages, response, instance.conversation
    ├── Line 1785-1789: Post-tool drain → append queued user messages AFTER fn_msg
    └── Return True (tool was used)

  Back in turn loop:
    ├── yield response   ← Broadcasts to frontend
    └── Loop back to Phase 2...
```

### Key Observation

During the block at `_create_and_run_agent(TestAgent)` (line 2286), **Maine's generator is suspended** — it cannot process any yields or resume. The only way a second `engine.run()` can start is if a **new thread** calls `run_agent_in_pool` for Maine independently.

---

## 2. ROOT CAUSE: Missing Lock on `session['generating']` Check

### The Vulnerable Code (api_server.py, line ~1272)

```python
if session['generating']:
    # Async injection while agent is running — route to target agent
    if agent_pool:
        target = data.get('target_agent') or session.get('session_name', 'Maine')
        agent_pool.enqueue_message(target, text)
    continue
```

**Problem:** This check is NOT protected by `session_lock`. The `session` dict is shared across all WebSocket connections and threads, but the read of `session['generating']` has no synchronization.

### Race Condition Scenario

```
Time T0: WebSocket msg 1 arrives (connection A)
Time T1: WebSocket msg 2 arrives (connection B) — nearly simultaneous!

Thread A (from connection A):
  Read session['generating'] → False  ← Check without lock!
  Proceeds to start run_agent_thread()
  Sets session['generating'] = True  (at line 1379, inside session_lock)

Thread B (from connection B):
  Read session['generating'] → False  ← Still False! Thread A hasn't set it yet
  Proceeds to start ANOTHER run_agent_thread()
```

**Result:** TWO threads call `run_agent_in_pool_with_recovery(pool, "Maine", ...)` which each call `engine.run(Maine_instance)`. Both operate on the SAME `AgentInstance` object in the pool.

### Why This Is Plausible for Maine

1. The WebSocket handler serves multiple connections
2. If two UI tabs/clients send messages nearly simultaneously (one before sync child starts, one during), both could pass the unprotected check
3. The first thread enters `engine.run()`, releases its slot, blocks on TestAgent
4. The second thread starts while TestAgent is still running — **this is the bug**

---

## 3. What Happens When Two `engine.run()` Run Concurrently

### Shared State (Both Threads Operate On Same `AgentInstance`)

```python
# Both Thread A and Thread B share:
pool.instances["Maine"] → AgentInstance with .conversation list

# Each thread creates its own LOCAL copies in _setup_turn:
conv_a = list(instance.conversation)  # Thread A's snapshot
conv_b = list(instance.conversation)  # Thread B's snapshot (may see updates Thread A made)
```

### Critical Race Window

```
Thread A (blocked on TestAgent):
  Slot released at line 2274
  Blocked inside _create_and_run_agent(TestAgent)

During this block, user sends message → enqueued in message_queues["Maine"]

Thread B starts (via race condition above):
  engine.run(Maine) ENTRY
  Phase 1: _setup_turn → reads instance.conversation (may or may not see Thread A's tool result yet)
  Phase 2 [LINE 915]: _pre_llm_checks → drain_queue() → injects user messages
  
Thread A resumes (TestAgent completed):
  Line 1725-1729: Append fn_msg (tool result) to instance.conversation
  Line 1785-1789: Post-tool drain (queue already drained by Thread B!)

Result: User messages may appear BEFORE tool result in Thread B's working lists!
```

### Why Message Ordering Breaks

The issue is that **Thread B's Phase 2 drain (line 915) runs BEFORE Thread A's post-tool drain (line 1785)**, because Thread B starts while Thread A is still blocked. Even though Thread A appends the tool result to `instance.conversation`, Thread B's local `messages` list was created from a snapshot that may not include the tool result yet.

**However**, if Thread B starts AFTER Thread A has already appended the tool result (which is likely since `_process_response` completes synchronously), then Thread B would see the correct order: `[tool_result, user_msgs]`.

The actual ordering depends on **exact timing**:
- If Thread B starts BEFORE Thread A appends fn_msg → WRONG ORDER
- If Thread B starts AFTER Thread A appends fn_msg → CORRECT ORDER

---

## 4. Verification Steps

### Add Debug Logging to Confirm Race Condition

```python
# In execution_engine.py, at the start of run():
logger.info(
    f"[DUAL_RUN_DEBUG] engine.run() ENTRY — instance={instance.instance_name}, "
    f"thread={threading.current_thread().name}, "
    f"stack_id={id(instance.conversation)}, "
    f"conv_len={len(instance.conversation)}"
)

# In execution_engine.py, at _pre_llm_checks drain (line 915):
logger.info(
    f"[DUAL_RUN_DEBUG] PRE-LLM DRAIN — instance={inst_name}, "
    f"thread={threading.current_thread().name}, "
    f"queue_size={len(self.pool.message_queues.get(inst_name, []))}"
)

# In execution_engine.py, at post-tool drain (line 1785):
logger.info(
    f"[DUAL_RUN_DEBUG] POST-TOOL DRAIN — instance={inst_name}, "
    f"thread={threading.current_thread().name}, "
    f"queue_size={len(self.pool.message_queues.get(inst_name, []))}"
)

# In execution_engine.py, after fn_msg append (line 1729):
logger.info(
    f"[DUAL_RUN_DEBUG] TOOL_RESULT_APPENDED — instance={inst_name}, "
    f"thread={threading.current_thread().name}, "
    f"conv_roles={[m.get('role') for m in instance.conversation[-3:]]}"
)

# In api_server.py, at WebSocket message handler (line 1272):
logger.info(
    f"[DUAL_RUN_DEBUG] WS_MSG — type={msg_type}, generating={session['generating']}, "
    f"thread={threading.current_thread().name}"
)
```

### Reproduce the Race Condition

1. Open TWO browser tabs connected to the same session
2. Send a message in Tab 1 that triggers `call_agent` (sync child)
3. IMMEDIATELY send another message in Tab 2 while the child is running
4. Check logs for duplicate `[DUAL_RUN_DEBUG] engine.run() ENTRY` for the same instance

---

## 5. Recommended Fix

### Fix 1: Protect `session['generating']` Check with Lock (CRITICAL)

**Location:** `api_server.py`, line ~1272

```python
# BEFORE (vulnerable):
if session['generating']:
    if agent_pool:
        target = data.get('target_agent') or session.get('session_name', 'Maine')
        agent_pool.enqueue_message(target, text)
    continue

# AFTER (fixed):
with session_lock:
    is_generating = session['generating']

if is_generating:
    if agent_pool:
        target = data.get('target_agent') or session.get('session_name', 'Maine')
        agent_pool.enqueue_message(target, text)
    continue
```

### Fix 2: Add Concurrency Guard in `run_agent_in_pool` (Defense-in-Depth)

**Location:** `api_integration.py`, line ~284-289

```python
def run_agent_in_pool(pool: AgentPool, instance_name: str) -> Iterator[List[Message]]:
    instance = pool.get_instance(instance_name)
    if instance is None:
        raise KeyError(f"Instance '{instance_name}' not found in pool")

    # Defense-in-Depth: Prevent concurrent engine.run() for same instance
    with pool._execution._state_lock:
        if instance.state == AgentState.RUNNING:
            logger.warning(
                f"[DUAL_RUN_PREVENTED] Instance '{instance_name}' already RUNNING. "
                f"Queuing request instead of starting concurrent run."
            )
            # Could either raise or queue for later processing
            return  # Stop iteration — no yields = immediate completion

    engine = ExecutionEngine(pool)
    yield from engine.run(instance)
```

### Fix 3: Use Generation ID to Prevent Stale Runs (Already Partially Implemented)

The `generation_id` mechanism at lines 1380-1382 is designed to prevent this, but it relies on the old thread checking the gen_id. Add a check at the start of `engine.run()`:

```python
# In run_agent_unified.py, at start of run_agent_thread_unified():
with session_lock:
    current_gen_id = session.get('generation_id', 0)

# Pass gen_id to engine or check it during execution
# If gen_id changes during execution, the thread should stop
```

---

## 6. Additional Investigation Points

### 6.1 Check for Multiple WebSocket Connections

The race condition is most likely when:
- Multiple browser tabs are open to the same session
- Two users/clients are connected simultaneously
- Messages arrive from different connections nearly simultaneously

**Action:** Check if the deployment typically has single or multiple WebSocket connections per session.

### 6.2 Verify `_pre_llm_checks` vs Post-Tool Drain Ordering

Even with the race condition fix, verify that:
1. Thread A's post-tool drain (line 1785) always runs before any new thread starts Phase 2
2. The `message_queues` are properly drained atomically

### 6.3 Check TestAgent's Behavior During Execution

If TestAgent itself triggers any callbacks or messages that could affect Maine's state, this could create additional race conditions. Review:
- `_create_and_run_agent` sub-agent streaming push (line 3072+)
- Any `pool.add_message()` calls made during TestAgent execution that target Maine

---

## 7. Conclusion

### Root Cause

The most likely root cause is the **missing lock on the `session['generating']` check** at line ~1272 of `api_server.py`. This allows two WebSocket messages arriving nearly simultaneously to both pass the check and start independent `run_agent_thread` calls for the same instance.

### Impact

When two `engine.run()` calls run concurrently for the same instance:
- Both operate on the same `AgentInstance.conversation` list
- Message ordering can be disrupted if Thread B's Phase 2 drain runs before Thread A's post-tool drain
- The LLM may receive messages in wrong order, breaking conversation coherence

### Priority

**HIGH** — This is a data integrity bug that can cause incorrect agent behavior. The fix (adding `session_lock` around the generating check) is minimal and low-risk.