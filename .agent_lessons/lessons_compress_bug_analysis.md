# BUG-7 Root Cause Analysis: compress_context Trims Tail to Last User Message

## Executive Summary

**Root Cause Confirmed**: The `fn_msg` (function result message) created after `compress_context` tool execution is **NOT added to the pool** before BUG-7's re-sync block runs. This causes it to be wiped out when `messages` is re-synced from the pool via `slice_history_for_llm()`.

**Additional Finding**: The LLM's assistant message containing the tool call is also lost if it hasn't been synced to the pool yet.

---

## Message Flow Analysis

### Key Data Structures

1. **`agent_pool.instance_conversations[instance_name]`** - The persistent "source of truth" for each agent's conversation history
2. **`messages`** (in `_run()`) - Local working copy of the conversation for the current turn
3. **`llm_messages`** (in `_run()`) - Copy sent to LLM for generation
4. **`logger_inst.data["history"]`** - Persistent log file history

### Normal Message Flow (No Compression)

```
[Turn Start]
1. messages = pool.get_conversation(session_name)  # Load from pool
2. llm_messages = copy.deepcopy(messages)          # Copy for LLM
3. [User message appended to messages & logged]
4. [LLM generates response, appended to messages/llm_messages & logged]
5. [Turn ends, api_server syncs session['history'] from pool]
```

### Compression Message Flow (The Bug)

```
[Turn Start - Before Compression]
1. messages = pool.get_conversation(session_name)  # Full history loaded
2. llm_messages = copy.deepcopy(messages)
3. LLM calls compress_context tool

[During Tool Execution - Lines 1658-1679]
4. compress_context() modifies POOL directly:
   - Pool history trimmed and marker inserted (core.py line 276)
   - Logger notified via insert_compression_marker() (core.py line 299)
   
5. AFTER compression, messages/llm_messages re-synced from pool:
   sliced = pool.slice_history_for_llm(pool.get_conversation())
   llm_messages.clear(); llm_messages.extend(sliced)    # Line 1673-1674
   messages.clear(); messages.extend(sliced)             # Line 1678-1679
   
   CRITICAL: At this point, messages/llm_messages contain ONLY the 
   sliced working set from the pool (SYSTEM + markers + tail).
   They do NOT include:
   - The LLM's assistant message that called compress_context
   - Any tool result messages not yet in the pool

[After Tool Execution - Lines 1713-1730]
6. fn_msg created and appended to messages/llm_messages/response:
   messages.append(fn_msg)              # Line 1725
   llm_messages.append(fn_msg)          # Line 1726
   response.append(fn_msg)              # Line 1727
   
7. fn_msg logged via logger_inst.log_message(fn_msg)  # Line 1730
   - This adds fn_msg to logger.data["history"]
   - BUT does NOT add fn_msg to agent_pool.instance_conversations!

[BUG-7 Block - Lines 1252-1263, runs at START of NEXT iteration]
8. if compress_tracker.get(session_name, False):
     compressed = pool.get_conversation(session_name)  # Get from pool
     sliced = pool.slice_history_for_llm(compressed)   # Slice it
     messages.clear()                                   # Line 1262
     messages.extend(copy.deepcopy(sliced))             # Line 1263
     
   PROBLEM: fn_msg was never added to the pool, so it's NOT in sliced.
   The clear()/extend() wipes out fn_msg from messages!
```

---

## Why fn_msg Isn't in the Pool

### log_message() Does NOT Modify Pool

Looking at `agent_logger.py`:

```python
def log_message(self, message: Any):
    """Append a single message to history and file."""
    self.update_timestamp()
    formatted_msg = self._format_message(message)
    self.data["history"].append(formatted_msg)  # Updates logger's internal history
    self._append_line(formatted_msg)             # Writes to JSONL file
```

**`log_message()` only updates:**
- `logger_inst.data["history"]` (in-memory tracking)
- The JSONL log file

**It does NOT update:**
- `agent_pool.instance_conversations[instance_name]` (the pool)

### No Pool Sync After fn_msg Append

Between line 1725 (fn_msg appended to messages) and line 1252 (BUG-7 re-sync), there's **no code that copies `messages` back to the pool** for the orchestrator's own conversation.

The only place where pool sync happens is in `api_server.py`:
```python
# Line 1310: After entire turn completes
agent_pool.instance_conversations[session['session_name']] = session['history']
```

But this happens **after** the `_run()` generator yields all responses and the turn ends. The BUG-7 block runs at the **start of the next iteration** within the same turn (if tools are batched), BEFORE api_server syncs back.

---

## The Assistant Message (Tool Call) is Also Lost

The LLM's assistant message containing the `compress_context` tool call follows the same pattern:

1. LLM generates response with tool call → appended to `response` and `llm_messages`
2. Response logged via `log_message()` at line ~502 (in streaming loop)
3. But NOT added to pool until api_server syncs at turn end
4. BUG-7 re-sync wipes it out if it runs before turn end

---

## Timeline of Events

```
Time T0: Turn starts, messages loaded from pool (pre-compression state)
Time T1: LLM generates assistant message with compress_context tool call
         - Assistant message appended to llm_messages/response
         - Logged via log_message() → logger.data["history"] updated
         - NOT in pool yet
Time T2: Tool execution begins
Time T3: compress_context() modifies pool (trims + inserts marker)
Time T4: messages/llm_messages re-synced from sliced pool (lines 1673-1679)
         - Now messages = [SYSTEM, markers, tail]
         - Assistant message lost from messages (wasn't in pool)
         - fn_msg not yet created
Time T5: fn_msg created and appended to messages/llm_messages/response (1725-1727)
         - Logged via log_message() → logger.data["history"] updated
         - NOT in pool yet
Time T6: More tools may execute (if batched)
Time T7: BUG-7 block runs (line 1252) at start of next iteration
         - messages re-synced from sliced pool
         - fn_msg wiped out (wasn't in pool)
         - Assistant message already gone
Time T8: Turn ends, api_server syncs session['history'] from pool
         - Pool still missing fn_msg and assistant message!
```

---

## Why This Causes Tail Trimming to Last User Message

When `slice_history_for_llm()` is called on the pool after compression:

```python
def slice_history_for_llm(self, history):
    latest_summary_idx = self.find_last_marker(history)  # Find marker
    if latest_summary_idx == -1:
        return history
    
    system_msg = history[0] if history[0].role == SYSTEM else None
    sliced = history[latest_summary_idx:]  # Slice from marker to end
    # Ensure system at top...
    return [system_msg] + list(sliced) if system_msg else list(sliced)
```

The pool after compression contains:
```
[SYSTEM, COMP1_marker, COMP2_marker, ..., USER_tail_start, ..., USER_last]
```

But the **tool call and fn_result** that triggered the compression are NOT in the pool. They exist only in:
- `messages` local variable (until wiped by BUG-7)
- `logger.data["history"]` (but logger not used for pool sync)

So when BUG-7 re-syncs from pool, it gets only what's in the pool - which ends at the last user message before compression was triggered.

---

## Solution Options

### Option 1: Add fn_msg and Assistant Message to Pool Immediately

After line 1725-1727 (fn_msg append) and after assistant message is logged, also append to pool:

```python
# After fn_msg created (line 1725-1730)
messages.append(fn_msg)
llm_messages.append(fn_msg)
response.append(fn_msg)
logger_inst.log_message(fn_msg)

# ADD THIS: Sync to pool immediately
pool_conv = self.agent_pool.get_conversation(self.session_name)
if fn_msg not in pool_conv:  # Avoid duplicates
    pool_conv.append(fn_msg)
```

**Pros**: Simple, ensures messages are in pool before BUG-7 runs  
**Cons**: Requires checking for duplicates; may need similar fix for assistant message

### Option 2: Skip BUG-7 Re-Sync if Messages Already Have New Content

Track whether new messages were added since last pool sync, and only re-sync if needed:

```python
# Track message count after compression sync
self._post_compression_msg_count[self.session_name] = len(messages)

# In BUG-7 block, check if messages grew
if self._compress_tracker.get(self.session_name, False):
    if len(messages) > self._post_compression_msg_count.get(self.session_name, 0):
        # Messages have new content (fn_msg, etc.), don't wipe it
        pass
    else:
        # Re-sync from pool
        sliced = pool.slice_history_for_llm(...)
        messages.clear()
        messages.extend(sliced)
```

**Pros**: Preserves new messages  
**Cons**: More state to track; may still miss edge cases

### Option 3: Merge Pool + Messages Instead of Replace

Instead of `clear()/extend()` in BUG-7, merge new messages into the sliced pool content:

```python
if self._compress_tracker.get(self.session_name, False):
    # Get current message IDs (by timestamp) to preserve new ones
    new_msg_timestamps = {msg.get('timestamp') for msg in messages if hasattr(msg, 'get')}
    
    compressed = pool.get_conversation(...)
    sliced = pool.slice_history_for_llm(compressed)
    
    # Re-add any new messages not in sliced
    for msg in messages:
        if msg.get('timestamp') not in [m.get('timestamp') for m in sliced]:
            sliced.append(msg)
    
    messages.clear()
    messages.extend(sliced)
```

**Pros**: Preserves all new content  
**Cons**: More complex; relies on timestamps being unique and stable

### Option 4: Move BUG-7 Block to After All Tool Execution Completes

Currently BUG-7 runs at the start of each iteration (line 1252). If moved to after the tool loop completes (before LLM call), new messages would already be in `messages` and could be synced to pool before re-slicing.

**Pros**: Cleaner separation of concerns  
**Cons**: Requires restructuring the turn loop

---

## Recommended Fix: Option 1 (Immediate Pool Sync)

The simplest and most robust fix is to ensure fn_msg and assistant messages are added to the pool immediately after creation, not just logged. This ensures they're present when BUG-7 re-syncs from the pool.

**Files to Modify:**
1. `agent_orchestrator.py` line ~1725-1730: Add pool sync after fn_msg creation
2. `agent_orchestrator.py` line ~502: Add pool sync after assistant message logged (in streaming loop)

---

## Verification Steps

After applying fix:
1. Run orchestrator with compress_context tool call
2. Check that after compression, the pool contains:
   - SYSTEM message
   - All compression markers
   - Tail messages (post-compression working set)
   - **Assistant message with compress_context tool call**
   - **fn_msg with compress_context result**
3. Verify BUG-7 re-sync doesn't wipe out these messages
4. Check that subsequent turns see the complete history including the compression event

---

## Related Issues

This bug affects any scenario where:
1. `compress_context` is called mid-turn
2. Multiple tools are batched in one turn
3. BUG-7 re-sync runs before api_server syncs back to pool

The same pattern may affect other tools that modify the pool mid-turn.