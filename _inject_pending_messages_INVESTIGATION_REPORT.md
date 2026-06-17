# Investigation Report: `_inject_pending_messages` Method

## Summary

The `_inject_pending_messages` method **was removed in uncommitted changes** to `agent_cascade/execution_engine.py`. The method exists in the last committed version (HEAD) but has been replaced with inline code in the current working tree.

## Key Findings

### 1. Method History
- **Added**: Commit `ec8fb49` - "refactor: Phase 3 recovery - code quality improvements from lost work" (Thu Jun 11 03:08:50 2026)
- **Last Exists In**: HEAD commit `b8f5627` - "fix(compression): read conversation after engine.run() completes to capture assistant message"
- **Removed In**: Uncommitted working tree changes

### 2. Current Status
```bash
# Method exists in HEAD (last committed version)
git show HEAD:agent_cascade/execution_engine.py | findstr "_inject_pending_messages"
# Returns 5 matches (4 calls + 1 definition)

# Method does NOT exist in current working tree
grep "def _inject_pending_messages" agent_cascade/execution_engine.py
# Returns 0 matches
```

### 3. Git Status
The file shows as modified with uncommitted changes:
```bash
git status
# Modified: agent_cascade/execution_engine.py
```

## The Original Method Definition

From commit HEAD (lines 1764-1818):

```python
def _inject_pending_messages(
    self, instance: AgentInstance, messages: List[Message], llm_messages: List[Message], 
    response: List[Message], inst_name: str, log_level: str = "info", used_any_tool: bool = False
) -> tuple[bool, bool]:
    """Inject pending user messages from the queue into all message lists.
    
    This is a shared helper used at multiple drain points for both code paths
    (no-real-content and real-content). It handles the common pattern of: draining the queue,
    creating USER messages, appending to all 4 message sets, and invalidating the token cache.
    
    Args:
        instance: The agent instance receiving the messages.
        messages: Full working set of messages.
        llm_messages: Messages for LLM API context.
        response: Response accumulator for streaming/frontend display.
        inst_name: Instance name for logging.
        log_level: Either "info" or "debug" for appropriate logging level.
        used_any_tool: Value to return as second element of tuple if messages were injected.
        
    Returns:
        Tuple of (injected: bool, used_any_tool: bool) where injected indicates if 
        messages were injected, and used_any_tool is the value passed in if injected.
    """
        
    pending = self.pool.drain_queue(inst_name)
    if not pending:
        return (False, False)  # Return tuple: (injected=False, used_any_tool=False)
    
    if log_level == "info":
        logger.info(f"Draining {len(pending)} queued messages for {inst_name} after turn completion.")
    else:
        logger.debug(f"Draining {len(pending)} queued messages for {inst_name}.")
        
    for async_msg_text in pending:
        if not async_msg_text.strip():
            continue  # Skip empty messages
        async_msg = Message(role=USER, content=async_msg_text)
        messages.append(async_msg)
        llm_messages.append(async_msg)
        response.append(async_msg)
        with instance._compression_lock:
            instance.conversation.append(async_msg)
        
        # FIX LogAppendFixer: Log injected message immediately to ensure it's persisted
        # even if no subsequent LLM call triggers sync in _process_response()
        try:
            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
            log_inst.log_message(async_msg)
        except Exception as e:
            logger.debug(f"Logging injected message to file failed for {inst_name} (non-critical): {e}")
    
    # Invalidate token cache after injecting all messages (once, not per-message)
    _invalidate_token_cache(instance)
    
    return (True, used_any_tool)  # Return tuple: (injected, used_any_tool_value)
```

## Method Usage in HEAD

The method was called at **4 locations** in the code:

1. **Line ~383**: In SLEEPING state wakeup (after draining async results)
2. **Line ~400**: In SLEEPING state waiting loop (for new user messages)
3. **Line ~704**: Async message injection during normal operation
4. **Line ~1432**: Post-tool urgent injection

## Current Implementation (Working Tree)

The method has been replaced with **inline code** at these locations. Instead of calling `self._inject_pending_messages(...)`, the code now directly:
1. Drains the queue: `pending = self.pool.drain_queue(inst_name)`
2. Loops through messages and appends them to all 4 lists
3. Logs each message immediately
4. Calls `_invalidate_token_cache(instance)` once at the end

This can be seen in the current file around **lines 1678-1736** (Drain Point 2).

## Backup Files

The complete version with the method is saved at:
- `N:\work\WD\AgentCascade_unified\logs\spillover\execution_engine_with_method.txt` (205,799 bytes)

This file contains execution_engine.py as it exists in HEAD commit.

## Recommendations

1. **If you want to restore the method**: Use `git checkout HEAD -- agent_cascade/execution_engine.py` to restore the committed version
2. **If the inline version is preferred**: The uncommitted changes should be reviewed and committed
3. **Review the diff**: Run `git diff agent_cascade/execution_engine.py` to see all changes

## Files Created During Investigation

1. `find_inject_method.py` - Python script used to search git history
2. `logs/spillover/execution_engine_with_method.txt` - Backup of execution_engine.py from HEAD
3. `_inject_pending_messages_INVESTIGATION_REPORT.md` - This report

---
*Report generated: 2026-06-15*  
*Investigation conducted by: coder_worker1*