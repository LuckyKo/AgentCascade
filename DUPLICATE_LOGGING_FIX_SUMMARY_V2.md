# Duplicate Logging Bug Fix - Summary V2

## Problem Description
The pre-existing sync in `_process_response()` (execution_engine.py lines 1445-1467) was re-logging messages that were already logged by the helper methods and tool execution loop. This caused duplicate entries in the JSONL log files.

### Root Cause Analysis
- Helper methods (`_inject_pending_messages`, `_inject_async_results`) log atomically when they insert messages
- Tool execution loop logs FUNCTION messages atomically
- The pre-existing sync checked `already_logged_count` vs `len(conv)` and logged any "new" messages
- But messages logged by the helpers were already in the log, so the sync re-logged them as duplicates

## Solution Applied

### Step 1: Removed Pre-existing Sync from execution_engine.py
**File:** `agent_cascade/execution_engine.py` (lines 1445-1467)

**Before:** A 23-line block that synced pre-existing messages to JSONL before appending turn_output.

**After:** Simplified to just getting the logger once (needed for turn_output logging).

```python
# Get logger for this instance (needed for turn_output logging below)
log_inst = self.pool.get_logger(inst_name, instance.agent_class)
```

### Step 2: Added Atomic Logging to Drain Point 1 in api_server.py
**File:** `agent_cascade/api_server.py` (lines 1386-1407)

Added immediate logging when messages are added at Drain Point 1:

1. **System message logging** (lines 1373-1379): After `create_main_agent_instance()` creates the system message, we now log it immediately.

2. **Drained user message logging** (lines 1386-1398): When draining pending messages from the queue, each message is now logged immediately after being added via `add_message()`.

3. **Current user message logging** (lines 1400-1407): The current user message is now logged immediately after being added.

### Step 3: No Changes Needed to Helper Methods or Tool Loop
The helper methods and tool execution loop already log atomically - they were the source of truth, not the problem.

## Files Modified
1. `agent_cascade/execution_engine.py` - Removed pre-existing sync block (23 lines → 2 lines)
2. `agent_cascade/api_server.py` - Added atomic logging at Drain Point 1

## Verification
Both files pass Python syntax validation:
- `execution_engine.py`: Valid
- `api_server.py`: Valid

## Expected Behavior After Fix
- System message is logged once when instance is created
- User messages are logged once when added via Drain Point 1
- Assistant messages (turn_output) are logged once in `_process_response()` after LLM call
- FUNCTION messages are logged once in the tool execution loop
- Async result messages are logged once by helper methods
- No duplicate entries in JSONL log files

## Testing Recommendations
1. Create a new agent instance and verify system message appears once in log
2. Send a user message and verify it appears once in log
3. Trigger a tool call and verify FUNCTION message appears once in log
4. Check that no duplicate entries exist in the JSONL file