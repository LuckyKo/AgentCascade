# Async Shell Truncation Investigation Report (CORRECTED)

## Executive Summary

**Finding:** One truncation gap identified. The `__wait` control command path in `shell_cmd.py` returns raw untruncated shell output because `_truncate_shell_message()` is a no-op, and the outer tool-result truncation layer it references has been removed from the codebase.

All OTHER async shell output paths (heartbeats, final output, status checks, early completion) correctly use MID truncation via `truncate_with_spillover(operation_mode='mid')`.

---

## 1. Truncation Points Mapping

### async_shell.py (Async Shell Tracker) — ALL CORRECT

| Line | Function | Truncation Type | Limit Value | Applies To |
|------|----------|-----------------|-------------|------------|
| 1122 | `_send_heartbeat()` | MID ✅ | `shell_char_limit` (2048) | Heartbeat output |
| 1171 | `_get_remaining_output_text()` | MID ✅ | `shell_char_limit * 3` (6144) | Final/remaining output on completion |
| 1462 | `get_status()` | MID ✅ | `shell_char_limit * 2` (4096) | Status check output |

### shell_cmd.py (Async Shell Command Tool)

| Line | Function | Truncation Type | Limit Value | Applies To |
|------|----------|-----------------|-------------|------------|
| 293 | `_launch_async()` early completion | MID ✅ | `shell_char_limit` (2048) | Early completion output |
| 330 | `_launch_async()` launched message | MID ✅ | `shell_char_limit` (2048) | Initial output in launch message |
| **455** | **`_handle_control_command()` __wait path** | **NONE ❌** | **No limit** | **__wait output — BUG** |

### operation_manager/shell.py (Sync Shell Command) — CORRECT

| Line | Function | Truncation Type | Limit Value | Applies To |
|------|----------|-----------------|-------------|------------|
| 509 | `execute_shell_command()` | MID ✅ | `char_limit` (2048) | Sync shell output |

---

## 2. The Bug: `__wait` Path Has No Truncation

### Root Cause Chain

1. **`shell_cmd.py:455`** — `__wait` handler calls `ShellCmd._truncate_shell_message(output_text, agent_name, self.agent_pool)`
2. **`shell_cmd.py:67-74`** — `_truncate_shell_message()` is a NO-OP:
   ```python
   @staticmethod
   def _truncate_shell_message(text: str, agent_name: str, agent_pool) -> str:
       """Return async message content as-is.
       
       Heartbeat/completion messages are already truncated by async_shell.py before
       being queued. __wait must not re-truncate them; the outer tool-result path
       will apply any necessary truncation once, consistently with other shell_cmd responses.
       """
       return text or ""
   ```
3. **The docstring's assumption is WRONG** — it claims "the outer tool-result path will apply any necessary truncation" but:
   - `tool_dispatcher.py:696-697`: `truncate_tool_result` was REMOVED ("tools already handle their own truncation for wild reads")
   - No other outer truncation layer exists in the execution engine for tool results
4. **The output is NOT pre-truncated** — unlike heartbeats (which go through `_send_heartbeat` → `truncate_with_spillover`), the `__wait` path reads raw lines directly:
   ```python
   # shell_cmd.py:446-454
   new_stdout = list(task.stdout_lines[last_stdout_len:])
   new_stderr = list(task.stderr_lines[last_stderr_len:])
   lines = new_stdout + new_stderr
   output_text = '\n'.join(line.rstrip('\r\n') for line in lines)
   ```
   These lines have NEVER been through `truncate_with_spillover`.

### Impact
- A long-running command with heavy output can return arbitrarily large `__wait` responses
- This bloats the agent's context window with unbounded tool results
- Inconsistent with all other shell output paths which are bounded

---

## 3. Why User Observed "Tail Instead of Mid"

The user's observation likely stems from:
1. **`__wait` returning only recent lines** — since `last_stdout_len` tracks what was already seen, `__wait` only returns NEW lines since the last check. This makes it appear as if only the "tail" (most recent) output is being shown, when actually it's just an incremental window with no truncation applied.
2. **Possible historical behavior** — `_truncate_shell_message` may have previously done tail-based slicing before being changed to a no-op.

The fix should make `__wait` use proper MID truncation like all other paths, so that if the new output since last check exceeds the limit, it's properly mid-truncated with spillover.

---

## 4. Fix Plan

### Change 1: Make `_truncate_shell_message` actually truncate (shell_cmd.py)

Replace the no-op with proper mid-truncation using `truncate_with_spillover`:

```python
@staticmethod
def _truncate_shell_message(text: str, agent_name: str, agent_pool) -> str:
    """Truncate __wait output using mid-truncation with spillover.
    
    The __wait path reads raw lines directly from the task (not pre-truncated
    like heartbeats), so it needs its own truncation pass. Uses the same
    shell_char_limit as other async shell output paths for consistency.
    """
    if not text:
        return ""
    try:
        llm_cfg = getattr(agent_pool, 'llm_cfg', {}) if agent_pool else {}
        char_limit = llm_cfg.get('shell_char_limit', 2048) if isinstance(llm_cfg, dict) else 2048
        base_dir = agent_pool.operation_manager.base_dir if agent_pool and hasattr(agent_pool, 'operation_manager') else None
        if base_dir and char_limit > 0:
            return truncate_with_spillover(
                text, char_limit,
                instance_name=agent_name,
                tool_name='shell_cmd_async',
                base_dir=base_dir,
                operation_mode='mid',
            )
    except Exception as e:
        logger.debug(f"[shell_cmd] _truncate_shell_message failed for {agent_name}: {e}")
    return text
```

### Verification
- All 6 truncation points will use `operation_mode='mid'` consistently
- No output path skips truncation
- Spillover file preserves full output for reference

---

## 5. Confidence Level

**Confidence: HIGH** — verified by direct code reading of all truncation call sites, confirmation that no outer truncation layer exists, and tracing the data flow from raw task lines through `__wait` to the LLM.
