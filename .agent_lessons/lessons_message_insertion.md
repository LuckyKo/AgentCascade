# Unified Message Insertion Pattern

## Overview
All message insertion in `agent_orchestrator.py` now uses a single unified method `_insert_message()` that writes to the agent pool (Stack 1). The finalize block in `api_server.py` propagates this to session history (Stack 2) and log file (Stack 3) via `update_history()`.

## The Problem
Previously, the code used dual-write patterns:
```python
logger_inst.log_message(msg)  # Write to log file
pool_conv.append(msg)         # Write to pool
```

This caused duplicates because the finalize block also syncs pool → session → log via `update_history()`.

## The Solution
Created `_insert_message()` method that provides a clean unilateral path:

```python
def _insert_message(self, msg: Message, messages: List[Message] = None, 
                    llm_messages: List[Message] = None, response: List[Message] = None):
    """Unified message insertion - writes to pool only, finalize propagates to all stacks."""
    # Append to working sets if provided
    if messages is not None:
        messages.append(msg)
    if llm_messages is not None:
        llm_messages.append(msg)
    if response is not None:
        response.append(msg)
    
    # Sync to pool conversation (Stack 1); finalize block handles Stacks 2 & 3
    try:
        pool_conv = self.agent_pool.get_conversation(self.session_name)
        pool_conv.append(msg)
    except Exception as e:
        logger.warning(f"Pool conversation sync failed for message in {self.session_name}: {e}")
```

### Important Implementation Notes

1. **LLM Output Messages**: Use `extend()` not `_insert_message()` - the working sets are updated via extend before truncation detection:
   ```python
   response.extend(output)
   messages.extend(output)
   llm_messages.extend(history_output)
   # Then only detect truncation, don't call _insert_message again
   ```

2. **Pool Sync Failures**: Logged as warnings for visibility (not silently swallowed)

3. **No Redundant Pool Appends**: After calling `_insert_message()`, don't manually append to pool again

## Replaced Patterns

### 1. LLM Output Messages (line ~1486)
**Before:**
```python
for msg in output:
    logger_inst.log_message(msg)
    try:
        pool_conv = self.agent_pool.get_conversation(self.session_name)
        pool_conv.append(msg)
    except Exception:
        pass
```

**After:**
```python
for msg in output:
    self._insert_message(msg, llm_messages=llm_messages)
```

### 2. Continuation Messages (line ~1502)
**Before:**
```python
cont_msg = Message(role=USER, content="[SYSTEM]: ...")
messages.append(cont_msg)
response.append(cont_msg)
llm_messages.append(cont_msg)
logger_inst.log_message(cont_msg)
try:
    pool_conv.append(cont_msg)
except Exception:
    pass
```

**After:**
```python
cont_msg = Message(role=USER, content="[SYSTEM]: ...")
self._insert_message(cont_msg, messages, llm_messages, response)
```

### 3. Function Result Messages (lines ~1554, ~1864)
**Before:**
```python
messages.append(fn_msg)
llm_messages.append(fn_msg)
response.append(fn_msg)
logger_inst.log_message(fn_msg)
try:
    pool_conv.append(fn_msg)
except Exception:
    pass
```

**After:**
```python
self._insert_message(fn_msg, messages, llm_messages, response)
```

### 4. Async/Urgent Messages (lines ~1280, ~1564, ~1873)
**Before:**
```python
async_msg = Message(role=USER, content=async_msg_text)
messages.append(async_msg)
llm_messages.append(async_msg)
response.append(async_msg)
logger_inst.log_message(async_msg)
try:
    pool_conv.append(async_msg)
except Exception as e:
    logger.debug(f"Pool conversation sync skipped for async message: {e}")
```

**After:**
```python
async_msg = Message(role=USER, content=async_msg_text)
self._insert_message(async_msg, messages, llm_messages, response)
```

### 5. Status Messages (line ~1169)
**Before:**
```python
status_msg = Message(role=ASSISTANT, content=f"Generating context summary...")
messages.append(status_msg)
llm_messages.append(status_msg)
response.append(status_msg)
logger_inst.log_message(status_msg)
try:
    pool_conv.append(status_msg)
except Exception as e:
    logger.debug(f"Pool conversation sync skipped for status message: {e}")
```

**After:**
```python
status_msg = Message(role=ASSISTANT, content=f"Generating context summary...")
self._insert_message(status_msg, messages, llm_messages, response)
```

## Simplified _append_feedback_and_return
The `_append_feedback_and_return` helper was simplified to use `_insert_message`:

**Before:**
```python
def _append_feedback_and_return(self, feedback_content: str, messages, llm_messages, response):
    feedback_msg = Message(role=ASSISTANT, content=feedback_content)
    messages.append(feedback_msg)
    llm_messages.append(feedback_msg)
    response.append(feedback_msg)
    try:
        pool_conv = self.agent_pool.get_conversation(self.session_name)
        pool_conv.append(feedback_msg)
    except Exception as e:
        logger.debug(f"Pool conversation sync skipped for feedback message: {e}")
    self.turn_final_messages = messages
    yield [feedback_msg]
    return None
```

**After:**
```python
def _append_feedback_and_return(self, feedback_content: str, messages, llm_messages, response):
    feedback_msg = Message(role=ASSISTANT, content=feedback_content)
    self._insert_message(feedback_msg, messages, llm_messages, response)
    self.turn_final_messages = messages
    yield [feedback_msg]
    return None
```

## Sub-Agent Hook Patterns
In the sub-agent `_call_llm` hook, messages are appended to the working `messages` list which gets synced via `agent.run()` → finalize:

**Before:**
```python
async_msg = Message(role=USER, content=async_msg_text)
messages.append(async_msg)
logger_inst.log_message(async_msg)
```

**After:**
```python
async_msg = Message(role=USER, content=async_msg_text)
messages.append(async_msg)  # Pool write via agent.run() → finalize propagates to log
```

## Key Benefits
1. **Single Source of Truth**: Pool is the authoritative source
2. **No Duplicates**: Eliminated dual-write patterns that caused message duplication
3. **Simpler Code**: Reduced boilerplate from ~8 lines to 1 line per message insertion
4. **Consistent Pattern**: All message insertions now follow the same pattern
5. **Easier Maintenance**: Changes to sync logic only need to happen in one place

## Migration Notes
- No direct `logger_inst.log_message()` calls remain in `agent_orchestrator.py`
- All logging goes through: pool → finalize block → `update_history()` → session + log file
- Pool sync failures are silently handled (same as before)
- Working set lists (messages/llm_messages/response) are optional parameters

## Files Modified
- `N:\work\WD\AgentCascade\agent_orchestrator.py`