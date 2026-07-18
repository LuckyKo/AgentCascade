# Feedback Message Duplication Fix (2026-06-13)

## Bug Summary
The `/compress X` command was producing duplicate "Context compressed successfully." messages in the log file.

## Root Cause Analysis

### The Problem
In `agent_orchestrator.py`, the `_append_feedback_and_return()` method (lines 434-470) was logging feedback messages through TWO separate paths:

1. **Path A - Direct Logging** (Line 461):
   ```python
   logger_inst.log_message(feedback_msg)  # Writes directly to log file
   ```

2. **Path B - Pool Sync** (Lines 463-465):
   ```python
   pool_conv = self.agent_pool.get_conversation(self.session_name)
   pool_conv.append(feedback_msg)  # Appended to pool conversation
   ```

The complete sync chain works as follows:
1. `pool_conv.append()` adds the message to the agent pool conversation
2. At `api_server.py:1331`, the pool is synced to session state: `session['history'] = copy.deepcopy(agent_pool.instance_conversations[...])`
3. Then `_save_session_history()` in `api_server.py` (line 1373) calls:
   ```python
   logger_inst.update_history(history)  # Syncs session history back to log
   ```

This caused the feedback message to be logged TWICE:
- Once via Path A (direct logging)
- Once via Path B → pool→session sync → update_history() sync

### Why Dedup Failed (Unconfirmed)
The `Agent.run()` wrapper may modify `feedback_msg.name` between the two log calls, causing deduplication logic to sometimes fail and produce visible duplicates. This requires further investigation to confirm the exact mutation point.

**Note**: The primary issue was simply having TWO independent logging paths; even without name mutation, this would cause duplication in most cases.

## The Fix

**Location**: `agent_orchestrator.py`, `_append_feedback_and_return()` method (lines 457-465)

**Change**: Remove the direct `log_message()` call, keeping only the pool append which is sufficient:

```python
feedback_msg = Message(role=ASSISTANT, content=feedback_content)
messages.append(feedback_msg)
llm_messages.append(feedback_msg)
response.append(feedback_msg)
# Note: Direct log_message() removed to prevent duplicate logging.
# The feedback is appended to pool_conv below and synced via update_history() in _save_session_history().
try:
    pool_conv = self.agent_pool.get_conversation(self.session_name)
    pool_conv.append(feedback_msg)  # ← This path alone is sufficient
```

### What Was Removed
- Line 461: `logger_inst.log_message(feedback_msg)`

### What Was Kept
- `messages.append(feedback_msg)` - Working message list for this session
- `llm_messages.append(feedback_msg)` - Deep copy for LLM processing (separate concern)
- `response.append(feedback_msg)` - Response accumulator for this turn
- `pool_conv.append(feedback_msg)` - Pool sync that feeds into update_history()

## Verification

1. **Compilation**: File compiles successfully with `python_compiler`
2. **Logic**: The pool append + update_history() path handles all logging needs
3. **No Side Effects**: Removing direct log_message doesn't affect:
   - Message accumulation in working lists
   - LLM processing (uses llm_messages separately)
   - Session persistence (handled via pool → session → update_history sync)
4. **Backup**: Original file backed up at `N:\work\WD\AgentWorkspace\logs\backups\coder\agent_orchestrator.py.1781384213.bak` (final version with docstring update)

## Related Files
- `agent_orchestrator.py` - Main fix location
- `api_server.py` - Contains `_save_session_history()` that calls `update_history()`
- `agent_logger.py` - Contains `AgentInstanceLogger.update_history()` method

## Testing Recommendations
After deployment, test with:
```
/compress 30
```
Verify only ONE "Context compressed successfully." message appears in the log.

## Lessons Learned
1. **Single Source of Truth**: When logging through multiple paths, ensure deduplication is robust or consolidate to a single path
2. **Name Mutation Matters**: If objects are mutated between logging calls (like `feedback_msg.name`), dedup logic may fail
3. **Pool as Central Hub**: The pool conversation should be the primary sync point for all message persistence

## Related Issues
- See `lessons_duplicate_consecutive_messages_analysis.md` for broader duplication analysis
- See `compression_duplication_fixes.md` for related compression logging fixes