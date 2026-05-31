# Call Agent Debug Logging Summary

## Purpose
Added comprehensive debug logging to trace the full execution path of nested agent calls (`call_agent` tool) in the Agent Cascade unified branch. All logs use `[CALL_AGENT_DEBUG]` prefix and `logger.debug()` level (except warnings/errors which use appropriate levels) so they can be controlled via log level configuration.

## Bug #1 Status (Bare Return)
**VERIFIED FIXED.** `_create_and_run_agent` always returns `(inst, conv)` at line ~1974. No bare `return` found in the code path. However, added runtime checks in both `_execute_agent_sync` and `task_wrapper` to detect if it ever returns None anyway — these will emit `[CALL_AGENT_DEBUG] BUG DETECTED` messages.

## Files Modified
1. **agent_cascade/execution_engine.py** (42 debug log points added)
2. **agent_cascade/agent_pool.py** (18 debug log points added)

## Complete Call Chain Coverage

### 1. Tool Dispatch (`_execute_tool`)
- Entry: instance name, tool_args type and preview
- After `_resolve_placeholders`: resolved type and preview
- Warning if resolved is None (JSON parse failure)
- After `_handle_call_agent`: result type and preview

### 2. Args Resolution (`_resolve_placeholders`)
- JSON parse failure path: instance, tool name, args preview
- Non-dict result path: instance, tool name, parsed type
- Unexpected type path: instance, tool name, type

### 3. Main Handler (`_handle_call_agent`)
- **Entry**: caller name, args type, args preview (first 300 chars)
- **Early exit — args is None**: reason logged
- **Early exit — missing instance_name/agent_class**: values logged
- **Recursive self-call detection**: original and cloned names
- **Class mismatch**: existing vs requested class
- **Nesting depth check**: caller_depth, child_depth, max_depth
- **Early exit — nesting depth exceeded**: depths logged
- **Concurrency limits**: is_parallel_allowed, effective_concurrency, parallel_launch_arg
- **Parallel path dispatch**: target, class, depth + result preview on exit
- **Sync path dispatch**: target, class, depth + result preview on exit

### 4. Engine Run (`run`)
- **Entry**: instance name, class, nest_depth
- **Early exit** (empty conversation): reason logged at WARNING level
- **Exception handler**: error_type and error message
- **Finally cleanup**: is_active=False confirmation

### 5. Setup Turn (`_setup_turn`)
- **Empty conversation early exit**: instance name logged at WARNING level

### 6. Create and Run Agent (`_create_and_run_agent`)
- **Entry**: target, class, caller, nest_depth
- **Instance registration**: confirmation in pool
- **Template not found**: class and caller logged at ERROR level
- **Before engine.run()**: starting execution for target
- **Exit**: inst type, conv length, final_resp length

### 7. Sync Execution (`_execute_agent_sync`)
- **Entry**: target, class, caller, nest_depth
- **Template not found early exit**: class logged at ERROR level
- **Endpoint slot acquired**: confirmation
- **Before _create_and_run_agent call**: confirmation
- **Bug #1 check**: if inst or conv is None — BUG DETECTED message
- **After _create_and_run_agent return**: inst type, conv length
- **After extract_instance_output**: result preview
- **Exception handler**: error_type and error logged at ERROR level
- **Endpoint slot released**: confirmation

### 8. Parallel Submission (`submit_parallel`)
- **Entry**: target, class, caller, nest_depth
- **Exit**: result preview

### 9. Parallel Task Submission (`submit_task`)
- **Entry**: target, class, caller, nest_depth
- **No executor early exit**: logged at ERROR level
- **Endpoint slot acquired**: confirmation
- **Slot acquisition failure**: logged at ERROR level
- **task_wrapper START**: target, class, caller, depth
- **New ExecutionEngine created**: engine_id (object ID) for tracking
- **Before _create_and_run_agent call**: confirmation
- **Bug #1 check in task_wrapper**: if inst or conv is None — BUG DETECTED message
- **After _create_and_run_agent return**: inst type, conv length
- **After extract_instance_output**: result preview
- **Sending completion message to caller**: confirmation
- **Exception handler**: error_type and error logged at ERROR level
- **Endpoint slot released**: confirmation
- **task_wrapper EXIT**: target name
- **submit_task EXIT (success)**: future_id

## Usage
To enable the debug logs, set the log level to DEBUG for the agent_cascade logger:

```python
import logging
logging.getLogger('agent_cascade').setLevel(logging.DEBUG)
```

Or in a config file:
```ini
[loggers]
keys = agent_cascade

[logger_agent_cascade]
level = DEBUG
```

To filter specifically for call_agent debug messages, grep logs for `[CALL_AGENT_DEBUG]`:
```bash
grep "CALL_AGENT_DEBUG" your_log_file.log
```

## Silent Failure Points Identified and Now Logged
1. `_resolve_placeholders` returning None (3 paths: JSON parse failure, non-dict result, unexpected type)
2. `_setup_turn` returning empty conversation → early exit from `run()`
3. `_handle_call_agent` early exits (args=None, missing fields, class mismatch, nesting depth)
4. Exception caught in `run()` and yielded as error message instead of propagated
5. Exception caught in `_execute_agent_sync` and returned as string
6. Exception caught in `task_wrapper` and sent via message queue
7. Bug #1 reoccurrence: `_create_and_run_agent` returning None (runtime check added)