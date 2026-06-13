# /compress X Command Feedback Bug Fix

**Date:** 2026-06-13  
**Author:** CompressCmdFixCoder  
**File Modified:** `agent_orchestrator.py`  
**Issue:** User-triggered `/compress X` command yielded feedback but didn't persist it, causing endless loops.

---

## Problem Summary

When a user triggered `/compress X`, the orchestrator would:
1. Yield feedback messages to the UI (e.g., "Context compressed successfully.")
2. Return from the generator without appending messages to history
3. On the next turn, see no assistant response in history
4. Re-trigger compression endlessly

**Root Cause:** The `/compress` handler used bare `yield [...]` followed by `return`, which yielded to the UI but didn't:
- Append to `messages` list (working history)
- Append to `llm_messages` list (LLM context)
- Append to `response` list (turn accumulator)
- Log via `logger_inst.log_message()`
- Sync to pool conversation
- Set `self.turn_final_messages`

---

## Solution Implemented

### Step 1: Added Helper Method `_append_feedback_and_return`

**Location:** Lines 434-470 in `agent_orchestrator.py`

This helper method centralizes the feedback message handling logic:

```python
def _append_feedback_and_return(self, feedback_content: str, messages: List[Message], 
                                llm_messages: List[Message], response: List[Message], logger_inst):
    """Append a feedback message to all working sets, log it, sync to pool, and yield it."""
    feedback_msg = Message(role=ASSISTANT, content=feedback_content)
    messages.append(feedback_msg)           # Add to working history
    llm_messages.append(feedback_msg)       # Add to LLM context
    response.append(feedback_msg)           # Add to turn accumulator
    logger_inst.log_message(feedback_msg)   # Log for traceability
    try:
        pool_conv = self.agent_pool.get_conversation(self.session_name)
        pool_conv.append(feedback_msg)      # Sync to persistent storage
    except Exception as e:
        logger.debug(f"Pool conversation sync skipped for feedback message: {e}")
    self.turn_final_messages = messages     # Set final state marker
    yield [feedback_msg]                    # Stream to UI
    return None                             # Signal completion
```

**Key Design Points:**
- Generator method that yields the message (integrates with existing streaming pattern)
- Appends to ALL data structures to maintain consistency
- Returns `None` after yielding (not used by caller, but documents intent)

### Step 2: Updated /compress Handler

**Location:** Lines 1127-1203 in `agent_orchestrator.py`

Changed all feedback paths from:
```python
# BEFORE - bare yield + return
yield [Message(role=ASSISTANT, content="Context compressed successfully.")]
return
```

To:
```python
# AFTER - use helper method
yield from self._append_feedback_and_return(
    "Context compressed successfully.", messages, llm_messages, response, logger_inst
)
return
```

**Four Feedback Paths Updated:**
1. **Success** (lines 1182-1185): "Context compressed successfully."
2. **Cancelled** (lines 1187-1191): "Context compression cancelled: {reason}"
3. **Failure** (lines 1193-1197): "Failed to generate summary: {summary}"
4. **Tool not available** (lines 1200-1203): "Error: compress_context tool is not available."

### Step 3: Persisted Status Message

The intermediate "Generating context summary..." message was also updated to persist properly (lines 1137-1143):

```python
status_msg = Message(role=ASSISTANT, content=f"Generating context summary for {int(fraction*100)}% of history...")
messages.append(status_msg)
llm_messages.append(status_msg)
response.append(status_msg)
logger_inst.log_message(status_msg)
yield [status_msg]
```

### Step 4: Moved Variable Initialization

Moved `llm_messages` and `response` initialization to line 1129-1131, before any branching:

```python
# Initialize llm_messages and response for the feedback helper (before any branching)
llm_messages = copy.deepcopy(messages)
response: List[Message] = []
```

This ensures all code paths have access to these variables.

---

## Issues Resolved

| # | Severity | Issue | Status |
|---|----------|-------|--------|
| 1 | 🔴 Critical | Control flow bug - `yield from` result never checked, causing fallthrough into LLM loop | ✅ Fixed |
| 2 | 🟠 Major | "Generating..." status message not persisted to history | ✅ Fixed |
| 3 | 🟠 Major | No test coverage for `/compress` handler (not blocking) | ⚠️ Noted |
| 4 | 🟡 Minor | Inconsistent variable initialization location | ✅ Fixed |

---

## Test Results

All 74 tests in `tests/test_compression.py` pass.

```
============================= test session starts =============================
collected 74 items
tests/test_compression.py::TestComputeDiscardCount::test_normal_fraction PASSED [  1%]
... (all tests pass) ...
tests/test_compression.py::TestRebuildWorkingSetWithMarker::test_rebuild_without_marker_returns_full_history PASSED [100%]

============================= 74 passed in 4.40s ==============================
```

---

## Code Review

**Reviewer:** compress_fix_reviewer  
**Verdict:** ✅ PASS - All issues resolved, production-ready

The reviewer verified:
- Control flow integrity (all branches terminate correctly)
- Data structure consistency (messages/llm_messages/response/logger/pool all updated)
- No execution path leaks into the main LLM loop after `/compress` response

---

## Lessons Learned

1. **Generator return values matter:** When using `yield from`, the sub-generator's return value becomes the yielded value. If you don't use it, just use `yield from` without assignment.

2. **Centralize repeated patterns:** The helper method approach avoids code duplication across multiple yield/return paths and makes future maintenance easier.

3. **Initialize variables early:** Move variable initialization before branching to avoid `UnboundLocalError` in edge cases.

4. **Test the handler, not just the core logic:** Unit tests for compression logic don't catch orchestration-level bugs like control flow issues.

---

## Related Files

- `agent_orchestrator.py` (lines 434-470, 1127-1203)
- `compression_tools.py` (compress_context function)
- `tests/test_compression.py` (test suite)

---

## Future Improvements

- Add integration test for `/compress` command handler exit behavior
- Add unit test for `_append_feedback_and_return` helper method
- Consider adding similar helper for other early-exit command patterns