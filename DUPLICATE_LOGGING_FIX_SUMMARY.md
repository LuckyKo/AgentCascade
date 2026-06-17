# Duplicate User Message Logging Fix Summary

## Problem Description

In `api_server.py` Drain Point 1 (around line 1388-1412), after calling `agent_pool.add_message()` to append drained/queued messages and the new user message, we were ALSO explicitly logging them via `log_inst.log_message()`. But then the execution engine's `_process_response()` method has its own "pre-existing messages sync" (lines 1445-1467) that logs any new messages in the conversation. This caused **double logging** — each user message appeared twice in the JSONL log file.

## Root Cause

The duplicate logging occurred because:
1. **Drain Point 1** in `api_server.py` explicitly logged messages after adding them to the conversation
2. **Execution Engine's `_process_response()`** method also logs all unlogged messages via its pre-existing sync mechanism (lines 1451-1467 in `execution_engine.py`)

The execution engine checks `already_logged_count` vs `len(conv)` and logs any new messages — this is the single source of truth for logging.

## Solution

Removed all explicit `log_inst.log_message()` calls from Drain Point 1 in `api_server.py`. Now only the execution engine's pre-existing sync mechanism handles all logging.

### Changes Made

**File: `agent_cascade/api_server.py`** (lines 1374-1392)

**Before:**
```python
# Get the instance's agent_class for proper logging (not 'User')
inst = agent_pool.get_instance(instance_name)
if not inst:
    logger.warning(
        f"Instance {instance_name} not found in pool at Drain Point 1, "
        f"defaulting to 'Orchestrator' for logging"
    )
agent_cls = inst.agent_class if inst else 'Orchestrator'

# Step 1: Drain user queue → append as USER message with JSONL logging
pending = agent_pool.drain_queue(instance_name)
if pending:
    logger.debug(f"Draining {len(pending)} queued user messages for {instance_name} at Drain Point 1 (IDLE).")
    for msg_text in pending:
        if msg_text.strip():
            msg = Message(role=USER, content=msg_text)
            agent_pool.add_message(instance_name, msg)
            # Log to JSONL immediately for persistence
            # CRITICAL: Use instance.agent_class for logging, not 'User'
            try:
                log_inst = agent_pool.get_logger(instance_name, agent_cls)
                log_inst.log_message(msg)
            except Exception as e:
                logger.debug(f"Logging queued user message to file failed for {instance_name} (non-critical): {e}")

# Step 2: Now add the current user message (instance guaranteed to exist) with JSONL logging
user_msg = Message(role=USER, content=parsed_content)
agent_pool.add_message(instance_name, user_msg)
try:
    # CRITICAL: Use instance.agent_class for logging, not 'User'
    log_inst = agent_pool.get_logger(instance_name, agent_cls)
    log_inst.log_message(user_msg)
except Exception as e:
    logger.debug(f"Logging user message to file failed for {instance_name} (non-critical): {e}")
```

**After:**
```python
# Step 1: Drain user queue → append as USER messages
# Logging is handled by execution_engine._process_response() pre-existing sync
pending = agent_pool.drain_queue(instance_name)
if pending:
    logger.debug(f"Draining {len(pending)} queued user messages for {instance_name} at Drain Point 1 (IDLE).")
    for msg_text in pending:
        if msg_text.strip():
            msg = Message(role=USER, content=msg_text)
            agent_pool.add_message(instance_name, msg)

# Step 2: Add the current user message
# Logging is handled by execution_engine._process_response() pre-existing sync
user_msg = Message(role=USER, content=parsed_content)
agent_pool.add_message(instance_name, user_msg)
```

### Verification

1. ✅ **Syntax check passed**: `python_compiler` validated `agent_cascade/api_server.py` - no syntax errors
2. ✅ **No remaining explicit log calls**: Verified no `log_message()` calls remain in `api_server.py`
3. ✅ **Agent pool add_message() doesn't log**: Confirmed via code review that `agent_pool.add_message()` only appends to memory (line 1087 comment)
4. ✅ **Execution engine handles logging**: The `_process_response()` method has proper sync mechanism (lines 1451-1467)

## Technical Details

### How Logging Now Works

1. **Drain Point 1** (`api_server.py`): Messages are added to `instance.conversation` via `agent_pool.add_message()`
2. **Execution Engine** (`execution_engine.py`, lines 1451-1467): Before processing each response, the engine:
   - Checks how many messages are already logged: `already_logged_count = len(log_inst.data.get("history", []))`
   - Compares with conversation length: `if already_logged_count < len(conv)`
   - Logs only unlogged messages: `for msg in conv[already_logged_count:]`

### Why This Approach Works

- **Single source of truth**: All logging happens in one place (execution engine)
- **Defensive check**: The `already_logged_count < len(conv)` comparison prevents redundant logging
- **Clean separation**: Message addition (`agent_pool.add_message()`) is separated from message persistence (`log_inst.log_message()`)

## Files Modified

- `agent_cascade/api_server.py` (lines 1374-1392): Removed explicit logging calls at Drain Point 1

## Testing Recommendations

1. Start a new agent session
2. Send multiple user messages in quick succession (to trigger queue draining)
3. Check the JSONL log file (`logs/agent_*.json`) for duplicate entries
4. Verify each user message appears exactly once in the log

## Related Code References

- **Drain Point 1**: `agent_cascade/api_server.py` lines 1374-1392
- **Pre-existing sync**: `agent_cascade/execution_engine.py` lines 1451-1467
- **Final sync**: `agent_cascade/execution_engine.py` lines 657-677
- **Agent pool add_message()**: `agent_cascade/agent_pool.py` lines 1081-1088

## Notes

- The `/continue` command path was also checked and doesn't have explicit logging calls
- Async results are drained at Drain Point 2 (execution_engine.py) to avoid double-append issues
- This fix simplifies the code and reduces the chance of future logging inconsistencies

## Known Limitations

### Pre-existing Retry Path Duplicate Logging

There is a **separate, pre-existing duplicate logging issue** in the retry path (`api_server.py` lines 1732-1780):

1. When a user retries, line 1736 pops the last user message from `inst.conversation` but NOT from the JSONL log
2. The message is re-inserted at lines 1767-1770 or 1780
3. The execution engine's sync sees `already_logged_count < len(conv)` as True (because the conversation was temporarily reduced)
4. **Result**: The same user message gets logged again on retry

This is outside the scope of the current fix but should be addressed in a future cleanup. For now, users might see duplicate messages specifically when using the retry feature.