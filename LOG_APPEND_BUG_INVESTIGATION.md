# Log Append Bug Investigation Report

## Executive Summary

**Bug**: Only the FIRST user message gets appended to agent log files (JSONL format) after recent fixes.

**Evidence**: The log file `orchestrator_Maine_20260615_094306.jsonl` contains exactly 5 entries:
1. metadata
2. system message 
3. user message ("hi") — the ONLY user message
4. assistant response (Turn 1)
5. assistant response (Turn 2 — likely auto-continue or second turn)

Despite evidence of multiple assistant responses and an expected multi-turn session, only one "user" role message appears in the log.

---

## Codebase Analysis

### Key Files Involved

| File | Lines | Purpose |
|------|-------|---------|
| `agent_cascade/logger/agent_instance_logger.py` | Full file (478 lines) | JSONL logging — `log_message()`, `update_history()` |
| `agent_cascade/execution_engine.py` | 1490-1547, 842-873, 1740-1786 | Main logging sync logic and message injection |
| `agent_cascade/api_server.py` | 1267-1396 | WebSocket handler — routes user messages to queue or conversation |
| `agent_cascade/agent_pool.py` | 1071-1088, 1208-1229 | Message queuing and `add_message()` |
| `agent_cascade/api_integration.py` | 707-759 | `execute_agent_turn()` — adds user messages to conversation |

### The Logging Architecture

The logging system has **two paths**:

#### Path 1: Direct User Messages (when agent is NOT generating)
```
WebSocket → add_message() → instance.conversation.append() → engine.run() → log_message()
```
Location: `api_server.py:1376` → `agent_pool.py:1082` → `execution_engine.py:1500-1512`

#### Path 2: Queued Messages (when agent IS generating)
```
WebSocket → enqueue_message() → message_queues[] → SLEEPING guard → _inject_pending_messages() → instance.conversation.append()
→ next iteration sync → log_message()
```
Location: `api_server.py:1276` → `_inject_pending_messages()` (execution_engine.py:1740-1786)

---

## Root Cause Analysis

### Primary Finding: Queue Drain Timing Issue

**The bug is in the timing of when user messages added via `_inject_pending_messages()` get logged.**

#### Detailed Flow Analysis

1. **User sends "hi" (agent idle)**
   - WebSocket handler: `add_message("Maine", user_msg)` → conversation = [system, user("hi")]
   - Thread starts → `engine.run(instance)`
   - `_setup_turn()`: conv snapshot = [system, user("hi")]
   - `_process_response()` sync (line 1496-1512): logs both messages ✓
   - LLM call → assistant response logged at line 1545 ✓

2. **User sends "how are you?" (agent still generating from Turn 1)**
   - WebSocket handler sees `session['generating'] == True` (line 1272)
   - Message is ENQUEUED via `enqueue_message()` (line 1276), NOT added to conversation
   - Handler skips the rest (line 1277: `continue`)

3. **Turn 1 completes, agent enters SLEEPING state**
   - `_post_turn_checks()` finds queued messages (line 1851-1853) → returns True
   - Loop continues to top of while loop
   - **SLEEPING guard** (line 395-444): detects user messages → calls `_inject_pending_messages()`
   - `_inject_pending_messages()`: drains queue, creates `Message(role=USER, content="how are you?")`, appends to `instance.conversation`

4. **Critical Issue — The Sync Gap**
   
   After injection at step 3, the loop continues:
   - Goes back to top of while loop (line 392)
   - `_pre_llm_checks()` called → drains queue AGAIN (already drained, no-op)
   - LLM call processes both user messages → turn output
   - `_process_response()` calls sync at lines 1496-1512
   
   **BUT**: The initial sync reads `conv = instance.conversation` and checks `already_logged_count`.
   
   If the injected user message was added AFTER a prior sync in this same `engine.run()` call, it should be caught by the next iteration's sync. However, there is a **critical edge case**:

   ### Edge Case: Single-Iteration Execution After Injection

   If `_inject_pending_messages()` injects messages and then the loop processes them in ONE iteration WITHOUT reaching `_process_response`'s initial sync before the message was "ready":

   - The SLEEPING guard injection happens at line ~421
   - Loop continues, calls LLM, gets response
   - `_process_response()` runs sync at lines 1496-1512
   
   In this flow, the injected message SHOULD be caught. But if there's a **race condition** where:
   
   - Two `engine.run()` calls operate on the same instance concurrently
   - Both read `already_logged_count` before either writes
   - One thread's writes are "overwritten" by the other

### Secondary Finding: `_format_message` Timestamp Collision Risk

In `agent_instance_logger.py`, the `_format_message()` method generates a **NEW timestamp** every time it's called (line 90-95, 102-104, 114-116):

```python
def _format_message(self, message: Union[Dict, Any]) -> Dict:
    # ...
    if 'timestamp' not in msg_copy:
        msg_copy['timestamp'] = datetime.datetime.now().isoformat()  # NEW timestamp each call!
```

This means when `update_history()` reformats already-logged messages, their timestamps change. The dedup logic falls back to content-based comparison (lines 270-278), which should work — **UNLESS** two user messages have identical content (unlikely) or there's a timing issue with millisecond-level timestamp collisions.

### Tertiary Finding: `update_history` Deduplication May Skip Messages

In `update_history()` (lines 223-336):

1. The method iterates over ALL messages in the input history
2. For each message, it searches for a match in `self.data["history"]`
3. If found → treated as UPDATE to existing slot
4. If NOT found → added to buffer for appending

The deduplication logic at lines 267-278 uses:
1. **Timestamp match** (primary) — fails when `_format_message` generates new timestamps
2. **Content-based fallback** (secondary) — should catch same-slot updates

But there's a subtle bug: if the input `history` contains messages that are NOT in `self.data["history"]` at all (e.g., newly injected messages), they go into the buffer. The buffer is only written to JSONL if `needs_rewrite == False` (line 332-333). If `needs_rewrite` becomes True, `reset_history(rewrite=True)` overwrites the ENTIRE file — but with the NEW history that includes all buffered messages.

This path should work correctly... unless there's an issue with `reset_history()`.

---

## Most Likely Root Cause

### The Queue Drain Timing Bug

The most probable cause is in how **concurrent WebSocket message handling** interacts with the **queue drain mechanism**:

1. User sends Message A while generating → enqueued
2. Agent finishes Turn 1, enters SLEEPING
3. SLEEPING guard drains queue, injects Message A into conversation
4. LLM processes Message A, generates response
5. `_post_turn_checks()` checks `has_messages()` — returns True (if another message was queued between steps 1-4)
6. Loop continues back to top...

But if between step 2 and step 3, the user sends Message B via WebSocket:
- `session['generating']` is still True (agent hasn't completed Turn 1 yet from the WebSocket handler's perspective)
- Message B gets ENQUEUED again
- When SLEEPING guard drains at step 3, BOTH Message A and Message B are in the queue

**The issue**: `_inject_pending_messages()` creates ONE `Message` object per queued text and appends it to `instance.conversation`. These injected messages are NOT individually logged via `log_message()`. They're only picked up by the initial sync at lines 1496-1512.

If the initial sync at lines 1496-1512 runs BEFORE `_inject_pending_messages()` adds these messages to `instance.conversation`, they won't be logged until the NEXT iteration's sync. If there IS no next iteration (loop exits), the injected messages are lost from the log file!

### Specific Bug Location

**File**: `agent_cascade/execution_engine.py`
**Lines**: 1490-1512 (initial sync in `_process_response()`)

The initial sync reads `instance.conversation` at line 1498:
```python
with instance._compression_lock:
    conv = instance.conversation
```

But the SLEEPING guard at lines 420-423 also uses `_compression_lock`:
```python
self._inject_pending_messages(instance, messages, llm_messages, response, inst_name, ...)
```

If there's a timing issue where:
1. Thread reads `instance.conversation` in the sync (before injection)
2. SLEEPING guard injects messages (after sync read, before sync write to file)
3. Sync writes only pre-injection messages

The injected messages would be missing from the log file.

---

## Recommended Fixes

### Fix 1: Log Injected Messages Immediately

In `_inject_pending_messages()` (execution_engine.py line 1780-1781), add logging after appending to conversation:

```python
with instance._compression_lock:
    instance.conversation.append(async_msg)

# NEW: Also log the injected message directly
try:
    log_inst = self.pool.get_logger(inst_name, instance.agent_class)
    log_inst.log_message(async_msg)
except Exception as e:
    logger.debug(f"Logging injected message failed for {inst_name}: {e}")
```

### Fix 2: Ensure Sync Always Runs After Injection

After `_inject_pending_messages()` returns True in the SLEEPING guard, force a sync of `instance.conversation` to the logger before continuing to LLM processing. This ensures injected messages are captured even if the loop exits unexpectedly.

### Fix 3: Add Message Count Verification

Add a verification step at the end of `engine.run()` that checks whether all messages in `instance.conversation` have been logged to JSONL, and performs a catch-up sync if needed:

```python
# At end of engine.run(), before returning:
log_inst = self.pool.get_logger(inst_name, instance.agent_class)
conv_len = len(instance.conversation)
logged_count = len(log_inst.data.get("history", []))
if conv_len > logged_count:
    # Catch-up sync for any missed messages
    log_inst.update_history(list(instance.conversation))
```

### Fix 4: Prevent Concurrent `engine.run()` Calls on Same Instance

Add a guard in the WebSocket handler to prevent multiple threads from starting `engine.run()` on the same instance simultaneously. Use a per-instance lock or check if generation is already in progress:

```python
# In api_server.py WebSocket handler, before starting thread:
if agent_pool and agent_pool.is_instance_running(instance_name):
    # Agent already running — message will be queued (current behavior)
    # But don't start another engine.run() call
    agent_pool.enqueue_message(instance_name, text)
    continue
```

---

## Files Requiring Modification

| File | Lines | Change Required |
|------|-------|-----------------|
| `agent_cascade/execution_engine.py` | 1780-1786 | Add immediate logging after injection (Fix 1) |
| `agent_cascade/execution_engine.py` | ~395-444 | Add post-injection sync (Fix 2) |
| `agent_cascade/api_server.py` | 1378-1396 | Add guard against concurrent runs (Fix 4) |

---

## Verification Steps

1. Start the agent server
2. Send "hi" to the main agent
3. Wait for response
4. Quickly send "how are you?" while agent is still processing
5. Check the log file — both user messages should appear

Expected: Log file should contain entries for BOTH user messages.
Actual (bug): Only the first user message appears.

---

## Additional Notes

- The log file timestamps show all messages from Turn 1 have the same timestamp (`2026-06-15T09:43:19.508841`), suggesting rapid processing
- There are TWO assistant responses in the log, indicating the agent did process multiple turns
- The fact that only one user message appears despite evidence of multi-turn interaction strongly supports the queue timing hypothesis