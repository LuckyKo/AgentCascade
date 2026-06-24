# Duplicate Message Logging During Normal Execution — Root Cause Analysis

## Executive Summary

The logger has MORE messages than the pool after compression because **messages are logged from multiple independent code paths**, and after compression events, the logger's in-memory history count (`data["history"]`) can drift out of sync with what `_log_messages_to_jsonl` expects. This causes partial-sync logic to re-log messages that were already on disk.

---

## 1. Logger Architecture Overview

### Data Model
```
AgentInstanceLogger:
  data["history"]   ← in-memory list of logged message dicts (with timestamps)
  log file (.jsonl) ← persistent JSONL file, one JSON line per message
  
  log_message(msg):
    1. format msg → add timestamp
    2. append to data["history"]
    3. _append_line() to file
```

**Key property**: `log_message()` is purely additive — it always appends. There's no dedup at the individual call level. Dedup only happens in `update_history()` via timestamp/content matching.

### History Storage
- **Flat list**: Yes, `data["history"]` is a flat list of all logged message dicts.
- **During compression**: When `_sync_logger_after_compression()` calls `reset_history(conv, rewrite=True)`:
  - It reads the existing log file (preserving original messages)
  - Inserts the new compression marker at the mirrored position
  - Rewrites the entire file
  - Sets `data["history"]` to `[formatted msg for msg in conv]` — i.e., pool state

---

## 2. ALL Places Where `log_message` Is Called

### Code Path A: Initial Message Logging (Turn Start)
**File**: `execution_engine.py` lines 643-648
```python
# In _append_to_working_sets_batch() — logs initial processed messages
for msg in processed_messages:
    log_inst.log_message(msg)       # line 646
```
**When**: At the very beginning of a turn, when user input / system messages are first added.

### Code Path B: Queued Message Logging (Early Exit)
**File**: `execution_engine.py` lines 742-744
```python
# In run() — early exit path when _setup_turn returns empty
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
**When**: After every successful LLM call, before `_append_to_working_sets_batch`.

### Code Path D: Inline Tool Result Logging (Primary Loop)
**File**: `execution_engine.py` lines 2046-2048
```python
# In _execute_detected_tools() — primary tool execution loop
log_inst.log_message(fn_msg)    # line 2048
```
**When**: After each tool executes successfully in the main detection loop.

### Code Path E: Inline Tool Result Logging (Denial Path)
**File**: `execution_engine.py` lines 1928-1930
```python
# In _execute_detected_tools() — tool denial path
log_inst.log_message(fn_msg)    # line 1930
```
**When**: When a tool is denied (disabled by template).

### Code Path F: Inline Tool Result Logging (Orphan Handling)
**File**: `execution_engine.py` lines 2160-2162
```python
# In _execute_detected_tools() — orphan placeholder path
log_inst.log_message(fn_msg)    # line 2162
```
**When**: When halt/stop is detected mid-loop and placeholder FUNCTION messages are created.

### Code Path G: Final Sync (Turn End, All Turns Including No-LLM)
**File**: `execution_engine.py` lines 876-895
```python
# At end of run() — final catch-up sync before state transition
already_logged_count = len(log_inst.data.get("history", []))   # line 882
conv_len = len(instance.conversation)                          # line 884

if already_logged_count < conv_len:                            # line 887
    for msg in instance.conversation[already_logged_count:]:   # line 893
        log_inst.log_message(msg)                              # line 895
```
**When**: At the very end of every `run()` call, as a safety net.

---

## 3. How Compression Affects Logger State

### Step 1: Pool Mutation (core.py line 336)
```python
agent_pool.instance_conversations[target_agent_name] = new_history
# new_history has FEWER messages than before (discarded messages removed, marker inserted)
```

### Step 2: Working Set Rebuild (handler.py line 238 / 391)
```python
self.engine._rebuild_working_set(messages, llm_messages, inst_name)
# This replaces instance._cached_messages with the SAME object as local `messages`
# After rebuild: len(instance.conversation) ≈ len(new_history) + notification_msg
```

### Step 3: Logger Sync (handler.py line 316 / 397)
```python
self._sync_logger_after_compression(inst_name, agent_class, "forced compression")
# Inside _sync_logger_after_compression (handler.py lines 76-107):
conv = self.pool.get_conversation(instance_name)   # e.g., 20 messages
log_inst.reset_history(conv, rewrite=True)          # rewrites file + sets data["history"] to pool state
```

**After Step 3**: `data["history"]` has ~20 messages matching pool state. File is rewritten with original messages + marker at mirrored position (more lines than pool).

---

## 4. Duplication Scenarios Identified

### Scenario 1: Logger Count Drift After Compression ⭐⭐⭐ MOST LIKELY

**Root Cause**: `reset_history(conv, rewrite=True)` sets `data["history"]` to the pool's working set length (e.g., 20 messages). But the **file** has more lines because it preserves original pre-compression messages. 

On the next turn:
1. `_log_messages_to_jsonl()` reads `already_logged_count = len(log_inst.data["history"])` → e.g., 20
2. It compares against `len(instance.conversation)` → also ~20 (correct)
3. **BUT** if any messages were appended between compression and the next LLM call (notification, tool results), they get logged correctly.

**However**, there's a subtle issue: after `reset_history(rewrite=True)`, the file has MORE lines than `data["history"]` because it preserves original messages + inserts marker. If something reads the file later to determine "how many are logged", it'll see more. But `_log_messages_to_jsonl` uses `len(data["history"])` not the file line count, so this should be fine for normal flow.

**Actual duplication trigger**: When the notification message is appended AFTER compression but BEFORE logger sync:
```
handler.py forced compression path:
  Line 237: _rebuild_working_set(...)        # pool state updated
  Lines 250-278: Notification injected       # conversation grows by 1
  Line 282: conv = get_conversation(...)     # includes notification
  Lines 286-314: Validation + recovery       # conditional rebuild
  Line 282: conv re-fetched                  # includes notification
  Line 313: _sync_logger_after_compression   # syncs with notification included
```

This is actually correct — the notification IS in `conv` when `_sync_logger_after_compression` runs. So no duplication here for forced compression.

### Scenario 2: Double-Append via Shallow Reference ⭐⭐ (Found by prior analysis)

After `_rebuild_working_set()` at line 1333:
```python
inst._cached_messages = messages       # shallow assign — SAME object!
```

Then notification injection at handler.py lines 272-274:
```python
instance.append_message(notification_msg)   # → _cached_messages.append() [length +1]
messages.append(notification_msg)           # → SAME list append() [length +1 again!]
llm_messages.append(notification_msg)       # → SAME list for llm too
```

**Result**: Each notification message appears **twice** in `_cached_messages` (= `messages`). This doesn't directly cause log duplication because logging uses `instance.conversation`, not `_cached_messages`. But it causes the working set to have duplicates, which then get logged when `_log_messages_to_jsonl` iterates over `conv = instance.conversation`.

Wait — let me verify: does `instance.conversation` also double-append? Let's trace:
- `append_message()` (agent_instance.py line 174): `self.conversation.append(message)` → adds once to conversation
- Then `messages.append(notification_msg)` at line 273 → this appends to `_cached_messages` only, NOT to `instance.conversation`

So `instance.conversation` has the notification once. The working set (`_cached_messages`) has it twice. This means:
- **Pool is clean** (conversation has no dupes)
- **Working sets have dupes** but these don't get logged directly — logging uses `conv = instance.conversation`

### Scenario 3: Tool Results Logged Twice ⭐⭐⭐ MOST LIKELY ROOT CAUSE

**Flow during a single turn with tool calls:**

1. LLM returns response with tool calls → `turn_output` contains ASSISTANT messages
2. `_log_messages_to_jsonl()` is called (line 2191):
   - Logs pre-existing messages from conversation via partial sync
   - **Logs all turn_output messages** including ASSISTANT messages (line 1753-1754)
3. Tool execution loop runs → creates FUNCTION result messages `fn_msg`
4. Each `fn_msg` is logged inline (lines 2048, 1930, 2162)
5. `_append_to_working_sets_batch()` adds turn_output to conversation (line 2195):
   - Inside: calls `instance.append_messages(turn_output)` which extends `conversation`

**Now on the next LLM call in the same turn:**
6. New response comes back → new `turn_output` with ASSISTANT messages
7. `_log_messages_to_jsonl()` is called again (line 2191):
   - `already_logged_count = len(log_inst.data["history"])` — includes previous turn_output + inline fn_msgs
   - Partial sync: logs messages from `conv[already_logged_count:]` 
   
**Potential overlap**: If the tool result `fn_msg` was logged inline AND then also appears in the conversation when `_log_messages_to_jsonl` does its partial sync, it could be double-logged.

Let me trace more carefully:
- After step 2: `data["history"]` has [SYS, USER, ASSISTANT_1] = 3 entries (assuming first turn)
- After step 4: inline logging adds fn_msg → `data["history"]` = [SYS, USER, ASSISTANT_1, FN_RESULT] = 4 entries
- File also has these 4 lines appended
- Step 5: `_append_to_working_sets_batch(instance, turn_output)` — this appends turn_output (ASSISTANT_1) to conversation. Conversation now = [SYS, USER, ASSISTANT_1, ...]. Wait, turn_output already includes the assistant message that was logged in step 2.

**Wait** — let me re-check: `_log_messages_to_jsonl` logs `turn_output`, then `_append_to_working_sets_batch` appends `turn_output` to conversation. So after the first LLM call:
- Conversation = [SYS, USER] + turn_output (ASSISTANT_1) = 3 messages
- Logger history = [SYS, USER] + turn_output = 3 entries

Then tool execution adds FN_RESULT to working sets via `_append_to_working_sets` and logs it inline. After tools:
- Conversation = [SYS, USER, ASSISTANT_1, FN_RESULT] 
- Logger history = [SYS, USER, ASSISTANT_1, FN_RESULT] — 4 entries ✓

This is correct IF the tool result is added to conversation via `_append_to_working_sets` which calls `instance.append_message(fn_msg)`. Let me verify:

<tool_call>
<function=read_file>
</function>
</tool_call>


<tool_call>
<function=forget_last>
<parameter=count>
1