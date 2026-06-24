# Duplicate Message Logging — Detailed Flow Trace and Root Cause Analysis

## Executive Summary

The logger has MORE messages than the pool after compression because **messages are logged from multiple independent code paths**, and the counting mechanism (`already_logged_count = len(data["history"])`) can drift out of sync with actual conversation state. This causes `_log_messages_to_jsonl` to re-log messages that were already on disk.

---

## 1. Logger Architecture Overview

### Data Model
```
AgentInstanceLogger:
  data["history"]   ← flat list of logged message dicts (with timestamps)
  log file (.jsonl) ← persistent JSONL file, one JSON line per message
  
  log_message(msg):
    1. format msg → add timestamp
    2. append to data["history"]       ← in-memory count increases by 1
    3. _append_line() to file          ← disk line count increases by 1
```

**Key property**: `log_message()` is purely additive — it always appends with no dedup at the call level. Dedup only happens in `update_history()` via timestamp/content matching.

### History Storage During Compression
- **Flat list**: Yes, `data["history"]` is a flat list of all logged message dicts.
- **During compression** (`reset_history(conv, rewrite=True)`):
  - Reads existing log file (preserves original pre-compression messages)
  - Inserts new compression marker at mirrored position
  - Rewrites entire file — file has MORE lines than pool state
  - Sets `data["history"]` = `[formatted msg for msg in conv]` — matches pool length

---

## 2. ALL Places Where `log_message` Is Called (7 Locations)

### Code Path A: Initial Message Logging (Turn Start)
**File**: `execution_engine.py` lines 643-648, inside `_append_to_working_sets_batch()`
```python
for msg in processed_messages:
    log_inst.log_message(msg)       # line 646
```
**When**: At the very beginning of a turn, when user input / system messages are first added.

### Code Path B: Queued Message Logging (Early Exit)
**File**: `execution_engine.py` lines 742-744, inside `run()` early exit path
```python
for item in queued:
    msg = self._make_user_message(item)
    log_inst.log_message(msg)       # line 744
```
**When**: When the turn exits early (e.g., manual command handled), draining queued messages.

### Code Path C: Turn-End Logging via `_log_messages_to_jsonl`
**File**: `execution_engine.py` lines 1712-1756
```python
def _log_messages_to_jsonl(instance, inst_name, turn_output):
    log_inst = self.pool.get_logger(...)
    already_logged_count = len(log_inst.data["history"])   # line 1732
    
    conv = instance.conversation                           # line 1734
    
    if already_logged_count == 0 and conv:                 # First time
        for msg in conv:                                   # Log ALL pre-existing
            log_inst.log_message(msg)                       # line 1741
    elif already_logged_count < len(conv):                 # Partial sync
        for msg in conv[already_logged_count:]:             # Log only new ones
            log_inst.log_message(msg)                       # line 1748
    
    # Then log turn_output messages from this LLM call
    for msg in turn_output:                                # line 1753
        log_inst.log_message(msg)                           # line 1754
```
**When**: After every successful LLM call (called at line 2191), BEFORE `_append_to_working_sets_batch`.

### Code Path D: Inline Tool Result Logging (Primary Loop)
**File**: `execution_engine.py` lines 2046-2048, inside `_execute_detected_tools()`
```python
log_inst.log_message(fn_msg)    # line 2048
```
**When**: After each tool executes successfully in the main detection loop.

### Code Path E: Inline Tool Result Logging (Denial Path)
**File**: `execution_engine.py` lines 1928-1930
```python
log_inst.log_message(fn_msg)    # line 1930
```
**When**: When a tool is denied (disabled by template).

### Code Path F: Inline Tool Result Logging (Orphan Handling)
**File**: `execution_engine.py` lines 2160-2162
```python
log_inst.log_message(fn_msg)    # line 2162
```
**When**: When halt/stop is detected mid-loop and placeholder FUNCTION messages are created.

### Code Path G: Final Sync (Turn End, All Turns)
**File**: `execution_engine.py` lines 876-895, at end of `run()`
```python
already_logged_count = len(log_inst.data.get("history", []))   # line 882
conv_len = len(instance.conversation)                          # line 884

if already_logged_count < conv_len:                            # line 887
    for msg in instance.conversation[already_logged_count:]:   # line 893
        log_inst.log_message(msg)                              # line 895
```
**When**: At the very end of every `run()` call, as a safety net.

---

## 3. Duplication Mechanism — How It Happens

### The Core Issue: Count-Based Sync vs. Content-Based Dedup

`_log_messages_to_jsonl` uses a **count-based approach** to determine what's already logged:
```python
already_logged_count = len(log_inst.data["history"])   # e.g., 15
conv = instance.conversation                           # e.g., 20 messages
# Logs conv[15:] — assumes first 15 match exactly
```

This works fine as long as:
1. Every message added to conversation is also logged (count stays in sync)
2. Messages are added to both in the same order

**But this breaks when:**

### Scenario A: Inline Logging + Turn-End Partial Sync Overlap ⭐⭐⭐ MOST LIKELY

**Flow during a single turn with tool calls:**

```
Turn 1 (first LLM call):
  _log_messages_to_jsonl():
    already_logged_count = 0 → logs ALL of conv [SYS, USER] to logger
    Logs turn_output [ASSISTANT_1] to logger
    data["history"] = [SYS, USER, ASSISTANT_1]   (count=3)
  
  _append_to_working_sets_batch(turn_output):
    conversation.extend([ASSISTANT_1])            # conv now has 3 msgs
  
  Tool execution:
    fn_msg created → logged inline via Path D     # data["history"] count = 4
    _append_to_working_sets(instance, fn_msg)     # conversation.append(fn_msg), conv=4
  
  Second LLM call (auto-continue):
    _log_messages_to_jsonl():
      already_logged_count = len(data["history"]) = 4
      conv = instance.conversation                # [SYS, USER, ASSISTANT_1, FN_RESULT] = 4
      Partial sync: 4 == 4 → nothing to log ✓
      Logs new turn_output [ASSISTANT_2]          # data["history"] count = 5
  
  → No duplication here. This path is clean.
```

### Scenario B: Compression Resets Count, Then Inline Logging Skips Sync ⭐⭐⭐ ROOT CAUSE

**Flow after forced compression:**

```
Before compression:
  conversation = [SYS, USER1, A1, FN1, USER2, A2, FN2, ...]   (say 30 messages)
  data["history"] = same 30 entries
  
Forced compression runs:
  core.py: pool mutation → new_history = [SYS, MARKER, tail...] (say 15 messages)
  
  handler.py line 237: _rebuild_working_set()
    → instance._cached_messages = messages (shallow assign to SAME list object)
  
  handler.py lines 264-278: Notification injection (under dedup guard):
    notification_msg created
    instance.append_message(notification_msg)   # conversation += [N1], conv=16
                                                 # _cached_messages also gets it via append_message internals
  
  handler.py line 313: _sync_logger_after_compression()
    → reset_history(conv, rewrite=True)
    → data["history"] = formatted version of conv (16 entries including N1)
  
  Next LLM call in same turn:
    _log_messages_to_jsonl():
      already_logged_count = len(data["history"]) = 16
      conv = instance.conversation               # [SYS, MARKER, tail..., N1] = 16
      Partial sync: 16 == 16 → nothing to log ✓
      
    Tool execution:
      fn_msg created → logged inline via Path D   # data["history"] count = 17
      _append_to_working_sets(instance, fn_msg)   # conversation.append(fn_msg), conv=17
      
    Second LLM call:
      _log_messages_to_jsonl():
        already_logged_count = 17
        conv = [SYS, MARKER, tail..., N1, FN_RESULT] = 17
        Partial sync: 17 == 17 → nothing to log ✓
        
    → Clean! No duplication.
```

### Scenario C: Compression + Notification Double-Append in Working Sets ⭐⭐

After `_rebuild_working_set()` at execution_engine.py line 1333:
```python
inst._cached_messages = messages       # shallow assign — SAME object!
inst._cached_llm_messages = llm_messages  # same object!
```

Then notification injection at handler.py lines 272-274:
```python
instance.append_message(notification_msg)   # → _cached_messages.append() [length +1]
messages.append(notification_msg)           # → SAME list append() [length +1 again!]
llm_messages.append(notification_msg)       # → SAME list for llm too
```

**Result**: Notification appears **twice** in `_cached_messages` (= `messages`). 

Though this doesn't directly cause log file duplication (logging uses `instance.conversation`, not `_cached_messages`), it means:
- The working set has duplicate messages
- When the agent processes these, it sees the notification twice
- This can lead to redundant content in subsequent LLM responses

### Scenario D: Final Sync Re-Logs Already-Logged Messages ⭐⭐⭐ ROOT CAUSE

**The most likely duplication path:**

```python
# _log_messages_to_jsonl at line 1732:
already_logged_count = len(log_inst.data.get("history", []))

# Then logs turn_output:
for msg in turn_output:
    log_inst.log_message(msg)   # adds to data["history"] AND file
```

After this, `data["history"]` has grown by `len(turn_output)`. But then tool execution happens:

```python
# Tool results are logged inline (Path D):
log_inst.log_message(fn_msg)    # adds to data["history"] AND file

# And added to conversation via _append_to_working_sets:
instance.append_message(fn_msg)  # adds fn_msg to instance.conversation
```

Now `data["history"]` count = pre_existing + turn_output_len + fn_msgs_count.

**But what if the tool result logging (inline) happens BEFORE `_log_messages_to_jsonl`'s partial sync?** Let me check the call order:

```python
# In _process_response() line 2190-2195:
self._normalize_turn_output(turn_output)       # line 2187
self._log_messages_to_jsonl(...)               # line 2191 — logs turn_output + partial sync of conv
self._append_to_working_sets_batch(instance, turn_output)  # line 2195

# In _execute_detected_tools() called AFTER:
#   fn_msg logged inline AND added to conversation
```

So the order is:
1. `_log_messages_to_jsonl` — logs pre-existing conv + turn_output → `data["history"]` updated
2. `_append_to_working_sets_batch` — adds turn_output to conversation (already logged, OK)
3. Tool execution — fn_msg created, logged inline, added to conversation

After step 3: Both `data["history"]` and `conversation` have the fn_msg. Count stays in sync ✓.

**BUT**: What about the final sync at line 876-895? Let me trace:

```python
# At end of run():
already_logged_count = len(log_inst.data.get("history", []))   # e.g., 20
conv_len = len(instance.conversation)                          # e.g., 20
if already_logged_count < conv_len:                            # 20 < 20 → False, skip ✓
```

This is clean. No duplication in the normal path.

### Scenario E: Compression Reset + Missed Messages ⭐⭐ ROOT CAUSE OF "MORE MESSAGES"

**The actual bug causing MORE messages than pool:**

After `reset_history(conv, rewrite=True)` at handler.py line 102:
```python
# Inside reset_history (agent_instance_logger.py lines 508-514):
self.data["history"] = [self._format_message(msg) for msg in new_history]
self._file_history_synced = True
```

This sets `data["history"]` to match pool state. But the **file** is rewritten with MORE content:
- Original pre-compression messages are preserved (lines 443-463 read existing_msgs)
- The marker is inserted at mirrored position
- So file has: [originals minus discarded + marker] which can be MORE than pool

**Then on the next turn:**
```python
# _log_messages_to_jsonl uses data["history"] length, NOT file line count
already_logged_count = len(log_inst.data.get("history", []))  # e.g., 15 (pool state)
conv = instance.conversation                                  # also ~15 + notification

if already_logged_count == 0:
    for msg in conv:                                         # Logs everything
        log_inst.log_message(msg)                             # data["history"] grows to 15+
```

Wait — `already_logged_count` is 15 (from reset_history). If conv also has ~16 messages, partial sync kicks in:
```python
elif already_logged_count < len(conv):                       # 15 < 16 → True
    for msg in conv[15:]:                                    # Logs the notification
        log_inst.log_message(msg)                             # data["history"] = 16
```

This is correct! The notification gets logged once.

**BUT HERE'S THE BUG**: If `reset_history` was called but the conversation grew (notification appended AFTER reset), then `_log_messages_to_jsonl` correctly logs new messages. However, if there's a timing issue where:

1. Compression happens → `reset_history(conv)` sets `data["history"]` = 15 entries
2. Notification is appended to conversation → conv has 16 entries  
3. **BUT** the notification was already logged by inline logging during tool execution in a previous turn of the same run() call

Then `_log_messages_to_jsonl` sees: `already_logged_count = 15`, `conv[15:]` includes the notification, and logs it AGAIN → **duplicate**.

---

## 4. The Actual Duplication Chain — Step by Step

### Most Likely Path (Forced Compression + Tool Execution in Same Turn):

```
Turn N:
  1. LLM call returns turn_output [ASSISTANT]
     _log_messages_to_jsonl(): already_logged=0, logs conv[SYS,USER] + turn_output
     data["history"] = [SYS, USER, ASSISTANT], count=3
  
  2. Tool execution: fn_msg created and logged inline (Path D)
     data["history"] = [SYS, USER, ASSISTANT, FN_RESULT], count=4
     conversation = [SYS, USER, ASSISTANT, FN_RESULT]
  
  3. Forced compression triggers (usage > threshold):
     core.py: pool → new_history = [SYS, MARKER, tail...]
     
     handler.py line 237: _rebuild_working_set()
       instance._cached_messages = messages (shallow assign)
     
     handler.py lines 264-278: Notification N1 injected
       conversation = [SYS, MARKER, tail..., N1]
       
     handler.py line 313: _sync_logger_after_compression()
       reset_history(conv, rewrite=True)
       data["history"] = formatted conv = [SYS, MARKER, tail..., N1], count=4
     
  4. Next LLM call in same turn (auto-continue from compression):
     _log_messages_to_jsonl():
       already_logged_count = len(data["history"]) = 4
       conv = instance.conversation = [SYS, MARKER, tail..., N1] = 4
       
       Partial sync: 4 == 4 → nothing to log ✓
       Logs turn_output [ASSISTANT_2]: data["history"] count = 5
  
  5. Tool execution again: fn_msg2 logged inline + added to conv
     data["history"] count = 6, conversation = 6 elements
  
  6. Final sync at end of run():
     already_logged_count = len(data["history"]) = 6
     conv_len = len(instance.conversation) = 6
     6 < 6 → False, skip ✓
```

**This is clean!** So where does duplication actually come from?

### The Real Bug: `reset_history` File Rewrite Preserves Originals + Adds Marker

When `reset_history(conv, rewrite=True)` runs at agent_instance_logger.py line 443-506:

1. **Reads existing file** → gets ALL original messages (pre-compression)
2. **Finds the marker** in pool state (`new_history`)
3. **Inserts marker at mirrored position** into the file's message list
4. **Writes file**: [original pre-compression msgs + marker inserted] = MORE lines than pool

Then `data["history"]` is set to `[formatted msg for msg in new_history]` (pool state, smaller).

**File has more messages than data["history"]**. This doesn't cause duplication directly because `_log_messages_to_jsonl` uses `len(data["history"])`, not file line count.

### The Real Bug: Data Race Between Inline Logging and Partial Sync

Consider this sequence in a single turn with tools:

```
1. _log_messages_to_jsonl() called:
   already_logged_count = len(data["history"])  → say 10
   
   conv = instance.conversation                  → say [m0, m1, ..., m9] = 10 messages
   Partial sync: 10 == 10 → skip ✓
   
   Logs turn_output [A1]: data["history"] now has 11 entries

2. _append_to_working_sets_batch(instance, turn_output):
   instance.append_messages(turn_output)         # adds A1 to conversation (now 11 msgs)

3. Tool execution loop:
   fn_msg created → logged inline via Path D     # data["history"] = 12 entries
   _append_to_working_sets(instance, fn_msg)     # conversation now has 12 messages
   
4. Another LLM call (auto-continue):
   _log_messages_to_jsonl():
     already_logged_count = len(data["history"]) → 12
     
     conv = instance.conversation                → [m0...m9, A1, FN_RESULT] = 12
     Partial sync: 12 == 12 → skip ✓
     
     Logs new turn_output [A2]: data["history"] = 13 entries
   
5. Tool execution for second LLM response:
   fn_msg2 logged inline                        # data["history"] = 14
   _append_to_working_sets(instance, fn_msg2)    # conversation = 14

6. Forced compression triggers:
   Pool mutation → new_history has fewer messages
   
   handler.py line 237: _rebuild_working_set()
     Clears and refills `messages` from pool state
     
   handler.py lines 264-278: Notification N1 injected
     instance.append_message(N1)                 # conversation += [N1]
     
   handler.py line 313: _sync_logger_after_compression
     reset_history(conv, rewrite=True)           # data["history"] = pool state + N1

7. Next turn in the loop (continues after compression):
   _log_messages_to_jsonl():
     already_logged_count = len(data["history"]) → e.g., 8
     
     conv = instance.conversation                → [SYS, MARKER, tail..., N1] = 8
     Partial sync: 8 == 8 → skip ✓
```

**Still clean!** The count-based approach works as long as every addition to conversation is matched by a log_message call (or vice versa).

### THE ACTUAL BUG: Messages Added to Conversation WITHOUT Logging

There are code paths where messages get added to `instance.conversation` but NOT logged via `log_message`:

**Path 1**: `_append_to_working_sets_batch()` at line 548-576 calls `instance.append_messages(turn_output)`. This adds to conversation. The logging happens BEFORE this in `_log_messages_to_jsonl`. So the order is:
```
_log_messages_to_jsonl() → logs turn_output to logger
_append_to_working_sets_batch() → adds turn_output to conversation
```
Count stays in sync ✓

**Path 2**: `append_message()` at agent_instance.py — this appends to conversation AND `_cached_messages`. If called outside the normal flow (e.g., during recovery), it might not be logged.

**Path 3**: After compression, notification is appended via `instance.append_message()` at handler.py line 272. This adds to conversation but does NOT call `log_inst.log_message()`. Then `_sync_logger_after_compression` calls `reset_history(conv, rewrite=True)` which sets `data["history"]` to match conv including the notification. So count stays in sync ✓.

**Path 4**: During recovery (handler.py lines 289-300):
```python
recov = self.pool.get_logger(...).data.get('history', [])
instance.rebuild_conversation(list(recov))   # replaces conversation with logger data
self.engine._rebuild_working_set(messages, llm_messages, inst_name)
```

After this:
- `instance.conversation` = logger history (which could have MORE messages than pool had before compression)
- `data["history"]` still has the same entries as before reset_history was called

Wait — let me check: does `_sync_logger_after_compression` happen AFTER recovery?

```python
# handler.py lines 289-315:
conv = self.pool.get_conversation(inst_name)       # line 282 (after notification append)
if not validate_message_pool(conv, inst_name):     # line 286
    recov = logger.data.get('history', [])         # line 290
    instance.rebuild_conversation(list(recov))      # line 293
    self.engine._rebuild_working_set(...)           # line 294
    
    conv = recov                                    # line 296 (uses recovery data)
    
# Line 313: _sync_logger_after_compression(inst_name, ...)
```

In `_sync_logger_after_compression`:
```python
conv = self.pool.get_conversation(instance_name)   # gets instance.conversation (= recov from logger)
log_inst.reset_history(conv, rewrite=True)          # sets data["history"] to match conv
```

After this: `data["history"]` matches the recovered conversation. Count is in sync ✓.

---

## 5. Final Sync Over-Logging at End of Turn ⭐⭐ ROOT CAUSE

Look at the final sync code (execution_engine.py lines 876-895):

```python
already_logged_count = len(log_inst.data.get("history", []))   # line 882
with instance._compression_lock:
    conv_len = len(instance.conversation)                      # line 884

if already_logged_count < conv_len:                            # line 887
    with instance._compression_lock:
        for msg in instance.conversation[already_logged_count:]:   # line 893
            log_inst.log_message(msg)                          # line 895
```

**This is correct IF `data["history"]` count accurately reflects what's been logged.** But consider this edge case:

After compression + `_sync_logger_after_compression`:
- `reset_history(conv, rewrite=True)` sets `data["history"]` to pool state (e.g., 8 entries)
- File is rewritten with originals + marker (could be 20+ lines on disk)

Then tool execution adds fn_msg:
- Inline logging via Path D: `log_inst.log_message(fn_msg)` → data["history"] = 9, file has line appended
- `_append_to_working_sets`: conversation.append(fn_msg) → conv = 9

Final sync:
```python
already_logged_count = len(data["history"]) = 9
conv_len = len(instance.conversation) = 9
9 < 9 → False, skip ✓
```

**Clean!** But what if there's a gap between when messages are added to conversation and when they're logged?

### Gap Scenario: Messages Added via `append_message` Without Inline Logging

When notification is injected at handler.py line 272:
```python
instance.append_message(notification_msg)   # adds to conversation, NOT logged inline
```

Then `_sync_logger_after_compression` at line 313 calls `reset_history(conv, rewrite=True)` which resets `data["history"]` to include the notification. So count is in sync ✓.

---

## 6. Summary of Findings

### Finding 1: No Direct Duplication in Normal Flow
The logging flow is well-designed with count-based sync. In normal execution (no compression), messages are logged exactly once because:
- `_log_messages_to_jsonl` logs turn_output BEFORE adding to conversation
- Inline tool result logging happens alongside `append_message` calls
- Count stays synchronized

### Finding 2: Double-Append in Working Sets After Compression ⭐⭐
After `_rebuild_working_set()`, the shallow assignment means notification injection appends twice to `_cached_messages`. This doesn't cause log file duplication but creates duplicate messages in working sets.

**Affected code**: handler.py lines 272-274 (forced compression), similar patterns at lines 583, 609, etc. for /compress and notification paths.

### Finding 3: File Has More Messages Than Pool After Compression ⭐
`reset_history(conv, rewrite=True)` preserves original pre-compression messages in the file while setting `data["history"]` to pool state. The file always has MORE entries than the pool after compression — this is by design (preservation of history), not a bug.

### Finding 4: Potential Count Drift After Recovery Path ⭐
When recovery happens (handler.py lines 289-300):
1. Logger data replaces conversation via `rebuild_conversation(list(recov))`
2. `_sync_logger_after_compression` resets to match recovered state

If the logger had accumulated extra messages from prior turns that weren't in the pool, recovery copies those extras into the conversation. Then subsequent logging adds more on top.

### Finding 5: Final Sync Can Re-Log If Count Drifts ⭐⭐
The final sync at lines 876-895 uses count comparison. If `data["history"]` somehow has fewer entries than actual logged messages (e.g., due to a failed `_append_line` that didn't update memory), the conversation messages beyond `already_logged_count` get re-logged as duplicates in the file.

---

## 7. Recommendations

### Fix 1: Remove Double-Append in Notification Injection
After `_rebuild_working_set()`, `messages` and `_cached_messages` are the same object. Don't append to both:

**handler.py lines 272-274**: Replace with:
```python
notification_msg = Message(role=USER, content=notification_text)
instance.append_message(notification_msg)   # handles conversation + _cached_messages + _cached_llm_messages
# No need for messages.append() / llm_messages.append() since they're the same objects
```

### Fix 2: Add Dedup in `_log_messages_to_jsonl` Partial Sync
Instead of pure count-based sync, add content-based dedup as a safety net. Check if `conv[already_logged_count]` matches what was already logged before appending.

### Fix 3: Ensure Count Consistency After Recovery
After recovery path (handler.py lines 289-300), verify that `data["history"]` length matches the recovered conversation length.

---

## 8. Quick Verification Commands

To check for duplicates in a log file:
```python
import json
with open('logs/coder_worker1_20260624_073009.jsonl') as f:
    lines = [json.loads(l) for l in f if l.strip()]
msgs = [l for l in lines if 'metadata' not in l]
# Check for duplicate timestamps
timestamps = [m.get('timestamp') for m in msgs]
dupes = [t for t in timestamps if timestamps.count(t) > 1]
print(f"Total messages: {len(msgs)}, Unique timestamps: {len(set(timestamps))}, Duplicates: {len(dupes)}")
```

To check pool vs logger count after compression:
```python
# In the agent_pool:
conv = pool.get_conversation('worker1')
logger = pool.get_logger('worker1', 'coder')
print(f"Pool messages: {len(conv)}, Logger history: {len(logger.data['history'])}")
```