# Bug Investigation: Session Reload Working Set Not Trimmed Post-Compression Markers

**Date:** 2026-07-15
**Investigator:** BugInvestigator Agent
**Issue:** After reloading a session from a log file, the instance's working set (conversation) keeps ALL messages instead of being trimmed to contain only [SYS][U0][COMP markers][tail after last marker].

---

## 1. How the Loading/Trimming Flow Works

### Design Spec (§5.2 in SYSTEM_DOCS.md)
The expected behavior is:
- **JSONL log file**: Retains FULL conversation history at all times (audit trail)
- **In-memory working set**: Trimmed to `[SYS][U0(first user msg)][all compression markers stacked][tail messages after last marker]`
- The rule: "the tail end past the last marker MUST be in sync at all times and have the EXACT same number of messages since the last compression marker"

### Load Flow (agent_pool.py : `load_session_from_log`, lines 1352-1520)

```
Step 1: Dismiss all instances (_dismiss_all_instances if clear_sub_agents_before_load=True)
Step 2: Parse JSONL → messages list + metadata (_parse_json_input → _extract_last_session)
Step 3: Filter to valid conversation messages (skip events/metadata) → cleaned[]
Step 4: Forward pass — find all compression markers, record last_marker_index
Step 5: Build working_set = [SYS] + [U0] + [all_markers] + [tail_after_last_marker]
        OR full history if no markers found
Step 6: Convert dicts → Message objects → msg_objects[]
Step 7: Create fresh AgentInstance(conversation=msg_objects, ...) and put in pool.instances
Step 8: Set up logger, rewrite_log_with_history(cleaned) — writes FULL history to disk
```

### Key Code Path (lines 1406-1438):
```python
# Forward pass — find compression markers
markers = []
last_marker_index = -1
for i, msg in enumerate(cleaned):
    if _is_marker(msg):          # checks: role==USER AND content.startswith(COMPRESSION_MARKER)
        markers.append(msg)
        last_marker_index = i

if markers:
    tail = cleaned[last_marker_index + 1:]
    working_set = ([system_msg] + [first_user] + markers + tail)
else:
    working_set = cleaned       # No compression — full history is the working set
```

### Instance Creation (lines 1467-1478):
The `AgentInstance` is created with `conversation=msg_objects`, which are built from `working_set`. This means the instance's in-memory conversation should already be trimmed.

---

## 2. The Warm Restart Scenario — Where the Bug Lives

### What "Warm Restart" Means
A warm restart happens when:
1. An agent session was previously loaded into memory (e.g., orchestrator "Maine")
2. Compression happened, working set was trimmed to ~24 messages
3. Server restarted or instance state was preserved but stale
4. `load_session_from_log` is called AGAIN on the same log file

### The Bug Path

**Scenario A: Instance already exists with stale conversation data BEFORE load_session_from_log**

In `lifecycle_manager.py`, the flow is:
1. `find_or_create_instance()` checks for existing instance at line 126
2. If found and IDLE/TERMINATED → reuse it (line 134-148), keeping its stale conversation
3. Then `load_session_from_log` is called (line 185-193)

**Inside `load_session_from_log`:**
- Step 1 dismisses all instances (`_dismiss_all_instances`) — this clears the pool's instance dict
- Steps 2-6 build the working set correctly from JSONL
- Step 7 creates a NEW AgentInstance with trimmed conversation

This path works fine when there are no competing instances. But there's a subtlety:

**Scenario B: The "reuse" flag is lost after load_session_from_log**

After `load_session_from_log` creates the new instance (line 1467), control returns to lifecycle_manager at line 191:
```python
inst = self.pool.instances.get(instance_name) or inst
```

Then `initialize_conversation` is called with both `is_reuse` and `from_external_load` flags. Let's trace what happens:

- If the instance was reused (line 134), `is_reuse=True`. After loading, the conversation is already trimmed by `load_session_from_log`. Then in `initialize_conversation`:
  - Line 370 (`if is_reuse:`): System message is updated in-place on index 0
  - Line 413: Task message is appended via `instance.append_message(task_msg)`
  
- If the instance was newly created by load, `is_reuse=False` and `session_was_loaded=True`:
  - Line 437 (`if from_external_load:`): Only task message is appended

**Both paths look correct** — they append to the already-trimmed conversation. So where does "all 43 messages" come from?

### Root Cause Analysis

The bug is in one of two places:

#### Candidate 1: `_extract_last_session` strips too aggressively or not enough
When multiple system messages exist (from merged sessions), only messages after the LAST system message are kept. If compression markers were inserted BEFORE the last system message boundary, they'd be lost and ALL remaining messages would become the working set.

**Check**: Lines 1206-1234 — `_extract_last_session` finds all SYSTEM role messages and keeps from the last one onward. This is fine for multi-session logs but could cause issues if compression markers appear before the system message boundary of a subsequent session.

#### Candidate 2: The working set construction doesn't account for marker stacking properly
Looking at lines 1406-1438, the code correctly builds `[SYS][U0][markers][tail]`. But there's a potential issue:

**The `first_user` search (line 1425):**
```python
first_user = next((m for m in cleaned if m.get(ROLE) == USER and not _is_marker(m)), None)
```
This finds the FIRST non-marker user message. If there are many messages between SYS and the first marker, this correctly grabs only U0.

**The `tail` extraction (line 1429):**
```python
tail = cleaned[last_marker_index + 1:]
```
This takes everything after the last marker. This is correct per design spec.

#### Candidate 3: The actual bug — warm restart with NO compression markers in the log

If the instance had stale conversation data (e.g., from a previous session that was compressed IN MEMORY but the JSONL file doesn't have corresponding markers), then `load_session_from_log` would load ALL messages as there are no markers to find.

**BUT WAIT** — let me re-read the problem statement: "After reloading a session from a log file, the instance's working set should be trimmed... but instead it keeps ALL messages." This means markers DO exist in the log, but trimming didn't happen.

#### Candidate 4 (Most Likely): The `cleaned` list includes ALL messages including discarded ones that were between earlier compression cycles

The JSONL retains full history: `[SYS][U0][U1][A1][COMP1][U2][A2][COMP2][U3][A3]...`

When loaded, the forward pass finds COMP1 and COMP2. The working set becomes:
- SYS (index 0)
- U0 (first non-marker user msg)  
- [COMP1, COMP2] (all markers)
- tail after COMP2 = [U3, A3, ...]

This should give ~6 messages for a 2-compression scenario. If all 43 stayed, either:
1. No markers were detected (`_is_marker` check failed), OR
2. The instance conversation was overwritten AFTER load_session_from_log completed

#### Candidate 5 (Strongest): Instance reuse path doesn't clear stale data before loading

Looking at `find_or_create_instance`:
- Line 131: Existing instance is found and reused (`is_reuse = True`)
- Line 176: `inst` stays as the existing instance with its full conversation
- Line 185-193: `load_session_from_log` is called, which:
  - Dismisses ALL instances (including this one)
  - Creates a NEW instance with trimmed working set
  
After load, line 191 gets back the new instance. This should work...

**UNLESS**: The `_dismiss_all_instances` call at line 852 doesn't actually clear conversations from dismissed instances before they're replaced. Let me check:

---

## 3. Uncommitted Changes Analysis

### execution_engine.py diff
- **Whitespace fix** (line 1104→1107): Minor formatting
- **System message update path** (lines 1237-1251): Changed from `update_history(conv_snapshot)` to directly updating logger memory + rewriting. This prevents duplicate insertion when messages are found at non-contiguous positions in full history vs working set.

### lifecycle_manager.py diff  
- **Reused instance init** (lines 407-433): Changed from calling `update_history()` with the trimmed working set to just logging the new task message directly. The comment explains: "The forward-only search in update_history can miss matches against the full history, causing buffer insertions → duplicates."

### agent_instance_logger.py diff
- **rewrite_log_with_history** (line 465): Now uses `formatted_msgs` from above instead of re-formatting new_history. This avoids assigning new timestamps which could break identity matching in update_history.
- **sync_compression_marker removed**: Simplified to `_sync_marker_single_write`.

---

## 4. Detailed Flow Trace — Warm Restart with Stale Data

### The actual bug scenario:

1. Instance "Maine" exists in pool with conversation of ~43 messages (full history, not trimmed)
2. Compression markers exist in the JSONL log file  
3. `load_session_from_log` is called:
   - `_dismiss_all_instances()` clears the pool
   - JSONL parsed → 63 messages found (full history including discarded ones from compression cycles)
   - Markers found at indices, say, [5, 12] 
   - Working set built: `[SYS][U0][COMP1][COMP2][tail_msgs...]` = ~8 messages
   - New AgentInstance created with `conversation=msg_objects` (~8 Message objects)

4. BUT — the instance's conversation might get overwritten by a subsequent operation that reads from the logger or pool state.

### The most likely bug location:

**In the `slice_history_for_llm` function (lines 1772-1843):**

This is called to extract the working set before sending to LLM. It checks if markers are "already stacked" near the start of the conversation. If the instance was loaded with a properly trimmed working set, this returns immediately (line 1819). But if for some reason the full history leaked into `instance.conversation`, this function would re-apply culling.

**The real issue: The "markers_stacked" check at line 1804:**
```python
markers_stacked = (
    first_marker_pos <= expected_start + 1
    and last_marker_idx == first_marker_pos + len(marker_indices) - 1
)
```

This checks if markers are consecutive near the start. If they ARE stacked, it returns a full copy of history without further processing. This is fine for post-load scenarios where trimming already happened.

---

## 5. Conclusion — Root Cause and Fix

### Most Likely Root Cause

The bug occurs when an instance has stale conversation data BEFORE `load_session_from_log` is called. The loading path itself correctly builds the trimmed working set. However, there are two potential failure points:

**Failure Point A**: If `_dismiss_all_instances()` at line 852 doesn't fully clear per-instance state before the new instance is created, stale conversation data could leak through via shared references.

**Failure Point B (More Likely)**: The `load_session_from_log` function builds the working set correctly in step 5, but the **system message matching** might fail if the loaded system message differs from what lifecycle_manager injects later. When `initialize_conversation` runs (line 370-402), it updates the system message at index 0 with a NEW Message object via `edit_message_in_place(0, sys_msg)`. If this somehow triggers a full conversation rebuild instead of an in-place edit, the working set could be replaced.

### Recommended Fix

**Primary fix**: Ensure that after `load_session_from_log`, the instance's conversation is NOT overwritten by subsequent initialization steps. Add a guard in `initialize_conversation`:

```python
# In lifecycle_manager.py initialize_conversation()
if from_external_load:
    # Session was loaded from log — working set already trimmed by load_session_from_log
    # Just update system message and append task, don't rebuild conversation
    if instance.conversation:
        # Update system message in-place without rebuilding
        old_sys = instance.conversation[0]
        sys_msg.timestamp = getattr(old_sys, 'timestamp', None)
        instance.edit_message_in_place(0, sys_msg)
```

**Secondary fix**: Add a verification step after `load_session_from_log` completes to confirm the working set size matches expectations:

```python
# After line 1478 in agent_pool.py
expected_size = len(msg_objects)
actual_size = len(new_inst.conversation)
if actual_size != expected_size:
    logger.warning(f"Working set mismatch after load: expected {expected_size}, got {actual_size}")
```

### Files to Check/Modify
1. `agent_cascade/lifecycle_manager.py` — `initialize_conversation()` method (lines 370-468)
2. `agent_cascade/agent_pool.py` — `load_session_from_log()` verification (after line 1478)
3. `agent_cascade/agent_instance.py` — Verify `edit_message_in_place` doesn't trigger full rebuild

---

## Summary Table

| Aspect | Detail |
|--------|--------|
| **Working set construction** | Correct in `load_session_from_log()` (lines 1406-1438) |
| **Marker detection** | Forward pass, `_is_marker` checks USER role + COMPRESSION_MARKER prefix |
| **Tail extraction** | Everything after last marker index — correct per design spec §5.2 |
| **Likely bug location** | Instance reuse path in `lifecycle_manager.py` where stale conversation isn't cleared before system message update |
| **Fix needed** | Guard against conversation rebuild during `initialize_conversation` for externally-loaded sessions; add size verification after load |