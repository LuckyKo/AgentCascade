# Bug Fix: list_agents Tool - Undefined Variable 'inst'

## Issue Summary
**File**: `agent_cascade/tools/custom/manager_ops.py`  
**Method**: `list_instances()` (lines 368-405)  
**Bug Type**: Undefined variable reference  

## Problem Description
In the `list_instances` method, a for loop was defined with variable name `inst_name`:
```python
for inst_name in all_instances:
```

However, within the loop body, four references incorrectly used `inst` instead of `inst_name`, causing a NameError when the method was executed.

## Affected Lines (Before Fix)
- **Line 376**: `msgs = self.agent_pool.get_conversation(inst)` 
- **Line 382**: `logger_inst = self.agent_pool.instance_loggers.get(inst)`
- **Line 395**: `summary = self.agent_pool.instance_summaries.get(inst, "None")`
- **Line 399**: `lines.append(f"### {status_emoji} Instance: `{inst}`")`

## Fix Applied
Changed all four occurrences of `inst` to `inst_name` within the for loop block (lines 368-405):

```python
# Line 376
msgs = self.agent_pool.get_conversation(inst_name)

# Line 382  
logger_inst = self.agent_pool.instance_loggers.get(inst_name)

# Line 395
summary = self.agent_pool.instance_summaries.get(inst_name, "None")

# Line 399
lines.append(f"### {status_emoji} Instance: `{inst_name}`")
```

## Verification
1. ✅ Syntax check passed - no Python syntax errors
2. ✅ All four references now correctly use `inst_name`
3. ✅ Other methods in the file that legitimately use `inst` as a variable name remain unchanged (e.g., dismiss_agent method at line 250)

## Notes
- The `dismiss_agent` method (around line 250) also has a for loop but correctly uses `inst` as its loop variable, so no changes were needed there.
- Backup files created: 
  - `logs/backups/coder/manager_ops.py.1781253140.bak`
  - `logs/backups/coder/manager_ops.py.1781253147.bak`

## Testing Recommendations
Test the `list_agents` tool by:
1. Creating at least one agent instance using `call_agent`
2. Running `list_agents` to verify it displays instance information without errors
3. Check that conversation metrics, log paths, and summaries are displayed correctly