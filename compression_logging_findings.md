# Duplicate Message Logging During Normal Execution — Findings Report

## Executive Summary

The logger has MORE messages than the pool after compression because **empty assistant message duplicates** are logged alongside every real assistant response. Each LLM-generated assistant message gets an empty copy appended to the log with the same timestamp, inflating the log file by ~20% extra entries. This happens consistently across all turns and is amplified after compression events.

---

## 1. Evidence from Log Analysis

### Sample: `coder_CacheBugInvestigator` (254 messages, 1 compression)
- **Empty assistant duplicates**: 49 out of 254 messages (~19%)
- Every empty duplicate follows a non-empty assistant with the **same timestamp**
- Pattern holds across all turns

### Sample: `coder_CompressDebug1` (166 messages, 7 compressions)
- **Empty assistant duplicates**: 35 out of 166 (~21%)
- More compressions → more duplicate accumulation post-compression

---

## 2. Root Cause: Empty Assistant Messages in Turn Output

### The Mechanism

In the main turn loop (`execution_engine.py` lines 780-806):

```python
# Phase 3: LLM Call
turn_output = []
for msg in self._call_llm_with_injection(instance, llm_messages):
    if msg is None:
        ...  # streaming yield
    elif isinstance(msg, (Message, dict)):
        content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
        turn_output.append(msg)   # ← No empty-content filter!
```

Then in `_process_response` (line 2191-2195):
```python
self._log_messages_to_jsonl(instance, inst_name, turn_output)   # Logs ALL of turn_output
self._append_to_working_sets_batch(instance, turn_output)       # Adds to conversation too
```

**Every message in `turn_output` gets logged AND added to the conversation**, including empty assistant messages.

### Where Empty Messages Come From

The LLM streaming API (`_execute_llm_call_with_retry`) yields accumulated responses. During streaming:
1. The first chunk arrives → full content message yielded
2. Subsequent chunks may produce an **empty continuation** that gets appended to `turn_output` with the same timestamp as the original

Additionally, after compression events, the notification injection path can trigger extra LLM calls where empty messages are more likely due to state transitions.

---

## 3. All Logging Code Paths (7 Locations)

| # | Location | File:Line | What Gets Logged |
|---|----------|-----------|-----------------|
| A | Initial message logging | `_append_to_working_sets_batch()` line 643-648 | User input / system messages at turn start |
| B | Queued message logging | `run()` early exit line 742-744 | Drained queued user messages |
| C | Turn-end batch logging | `_log_messages_to_jsonl()` lines 1730-1756 | Pre-existing conv + turn_output from LLM call |
| D | Inline tool result (success) | `_execute_detected_tools()` line 2046-2048 | FUNCTION results after each successful tool |
| E | Inline tool result (denial) | `_execute_detected_tools()` line 1927-1930 | FUNCTION denial messages |
| F | Inline orphan handling | `_execute_detected_tools()` line 2159-2162 | Placeholder FUNCTION results on halt/stop |
| G | Final safety sync | `run()` end lines 876-895 | Catch-up for any unlogged conversation messages |

**Count-based sync**: Paths C and G use `already_logged_count = len(data["history"])` to determine what's already logged. This works as long as every addition to `instance.conversation` is matched by a corresponding `log_message()` call (or vice versa).

---

## 4. Duplication Scenarios Identified

### Scenario A: Empty Assistant Duplicates in Turn Output ⭐⭐⭐ CONFIRMED
- **Frequency**: Every LLM response produces at least one empty duplicate
- **Evidence**: Log analysis shows 19-21% of messages are empty assistants with same timestamps as preceding non-empty ones
- **Impact**: Inflates log file size, pollutes conversation history

### Scenario B: Double-Append in Working Sets After Compression ⭐⭐ CONFIRMED
After `_rebuild_working_set()` at execution_engine.py line 1333:
```python
inst._cached_messages = messages       # shallow assign — SAME object!
```

Then notification injection at handler.py lines 272-274 appends to both the instance AND the local `messages` list, which are the same object. Result: notification appears twice in `_cached_messages`.

### Scenario C: Count Drift After Recovery Path ⭐ POSSIBLE
When recovery happens (handler.py lines 289-300):
1. Logger data replaces conversation via `rebuild_conversation(list(recov))`
2. If logger had accumulated extra empty duplicates, those get copied into the pool

### Scenario D: Final Sync Re-Logging ⭐ POSSIBLE
The final sync at lines 876-895 uses count comparison. If `data["history"]` somehow has fewer entries than actual logged messages (e.g., due to a failed `_append_line` that didn't update memory), conversation messages beyond the count get re-logged as duplicates in the file.

---

## 5. Compression-Specific Amplification

After forced compression:
1. Pool is mutated → fewer messages
2. Working sets are rebuilt via shallow assignment (Scenario B risk)
3. Notification injected → may double-append to working sets
4. Logger synced via `reset_history(conv, rewrite=True)` → file preserves originals + marker
5. **File has MORE lines than pool** by design (preservation of history), but empty duplicates add on top

---

## 6. Recommendations

### Fix 1: Filter Empty Messages in Turn Output Collection
In the main loop (`execution_engine.py` line 805-806):
```python
# Only append to turn_output if it's a real message (not transient retry notification)
if not is_retrying_msg and content.strip():   # ← Add .strip() check
    turn_output.append(msg)
```

### Fix 2: Remove Double-Append in Notification Injection
After `_rebuild_working_set()`, `messages` and `_cached_messages` are the same object. Don't append to both (handler.py lines 272-274):
```python
instance.append_message(notification_msg)   # handles conversation + _cached_messages
# No need for messages.append() / llm_messages.append()
```

### Fix 3: Add Content Dedup in `_log_messages_to_jsonl` Partial Sync
As a safety net, check if `conv[already_logged_count]` has the same timestamp as what was already logged before appending.

---

## 7. Verification Commands

To check for duplicates in any log file:
```python
import json
from collections import Counter

with open('logs/coder_*.jsonl') as f:
    lines = [json.loads(l) for l in f if l.strip()]
msgs = [l for l in lines if 'metadata' not in l and 'event' not in l]

# Check empty assistant duplicates
empty_dupes = sum(1 for i in range(1, len(msgs)) 
                  if msgs[i].get('role') == 'assistant' 
                  and not str(msgs[i].get('content','')).strip()
                  and msgs[i-1].get('timestamp') == msgs[i].get('timestamp'))

# Check timestamp duplicates
timestamps = [m.get('timestamp', '') for m in msgs]
dup_ts = {ts: c for ts, c in Counter(timestamps).items() if c > 1}
```

---

## Files Analyzed
- `agent_cascade/execution_engine.py` — main turn loop, logging paths, tool execution
- `compression/handler.py` — compression handler, notification injection, logger sync
- `compression/core.py` — pool mutation during compression
- `agent_cascade/logger/agent_instance_logger.py` — JSONL logger implementation

## Files Created
- `compression_logging_dedup_analysis.md` — detailed architecture overview
- `compression_logging_flow_trace.md` — step-by-step flow tracing
- `compression_logging_findings.md` — this summary report (you are here)