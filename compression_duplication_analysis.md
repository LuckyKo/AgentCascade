# Compression Duplication Bug — Complete Mutation Trace & Root Cause Analysis

## Data Model Overview

There are **3 sources of truth** that need to stay in sync:
1. `instance.conversation` (on `AgentInstance`) — the canonical source
2. `_instance_conversations` dict storage (internal dict backing the mapping)
3. Local working sets (`messages`, `llm_messages`) in execution engine loop

---

## The _InstanceConversationMapping Bridge (agent_pool.py lines 32-94)

```python
class _InstanceConversationMapping(dict):
    def __getitem__(self, key):       # Read → from instance.conversation (line 74)
    def __setitem__(self, key, val):  # Write → calls inst.rebuild_conversation(list(value)) + dict storage
```

Key: `__setitem__` does TWO things simultaneously:
- **Line 92**: Calls `inst.rebuild_conversation(list(value))`:
  ```python
  self.conversation = list(new_messages)       # line 321
  self._cached_messages = list(new_messages)    # line 322
  self._cached_llm_messages = list(new_messages) # line 323
  ```
- **Line 93**: `super().__setitem__(key, value)` — stores in dict

---

## FORCED COMPRESSION FLOW — Step-by-Step Mutation Trace

### Phase 1: Compression Trigger (execution_engine.py lines 1159-1184)
`_check_and_trigger_compression()` calculates usage_pct → calls `_force_compression()`.

### Phase 2: Halt Other Agents (handler.py lines 216-219)
No pool mutations.

### Phase 3: Compression Execution (core.py)

**Step A — Get active set (line 74-76)**:
```python
active_start_idx, active_set, latest_summary_idx = agent_pool.get_compression_target_set(target_agent_name)
```
→ `get_conversation()` returns a **COPY** of `inst.conversation` (agent_pool.py line 1451). No mutation.

**Step B — Fresh history snapshot (lines 280-282)**:
```python
history = agent_pool.get_conversation(target_agent_name)  # NEW COPY at line 281
insert_pos = active_start_idx + target_discard_count      # e.g., 5
```

**Step C — Build new_history (line 335)**:
```python
new_history = history[:active_start_idx] + [marker_message] + history[insert_pos:]
```

### **MUTATION #1** — Write to pool (core.py line 336):
```python
agent_pool.instance_conversations[target_agent_name] = new_history
```

After MUTATION #1:
- `instance.conversation` = new_history (via rebuild_conversation)
- `instance._cached_messages` = copy of new_history (created by `list(value)` in __setitem__)
- `instance._cached_llm_messages` = copy of new_history

### Phase 4: Working Set Rebuild (handler.py line 238)

```python
self.engine._rebuild_working_set(messages, llm_messages, inst_name)
```

Inside `_rebuild_working_set` (execution_engine.py lines 1283-1341):

**Line 1307**: `helpers.rebuild_working_set(messages, self.pool, inst_name)` from helpers.py:
- Gets fresh copy via `get_conversation(agent_name)` → returns new_history
- Clears and extends `messages` list with deepcopy of pool state (line 325-326)

**Lines 1310-1316**: Rebuilds `llm_messages`:
```python
conv = self.pool.get_conversation(inst_name)       # line 1310
sliced = self.pool.slice_history_for_llm(conv)     # line 1314
llm_messages.clear()                                # line 1315
llm_messages.extend(list(sliced))                   # line 1316
```

**Lines 1320-1336**: Cache invalidation + sync:
```python
inst._cached_token_count = 0                        # line 1321
...
inst._cached_messages = messages                    # line 1333 ← SHALLOW ASSIGN!
inst._cached_llm_messages = llm_messages            # line 1335 ← SHALLOW ASSIGN!
```

**After Phase 4:**
- `messages` list (local) = deepcopy of new_history
- `llm_messages` list (local) = sliced version
- `instance._cached_messages` = **SAME OBJECT as `messages`** (shallow assignment at line 1333)
- `instance._cached_llm_messages` = **SAME OBJECT as `llm_messages`** (line 1335)

### **MUTATION #2** — Notification Injection (handler.py lines 250-279):

```python
# Lines 264-274:
notification_msg = Message(role=USER, content=notification_text)
instance.append_message(notification_msg)   # line 272 → adds to instance.conversation + _cached_messages + _cached_llm_messages
messages.append(notification_msg)           # line 273 → adds to local working set (SAME object as _cached_messages!)
llm_messages.append(notification_msg)       # line 274 → adds to LLM working set (SAME object as _cached_llm_messages!)
```

**After MUTATION #2:**
- `instance.conversation` = new_history + [notification_msg]
- `_cached_messages` (= `messages`) has notification appended TWICE? Let's check...

Actually no — `append_message` appends to `instance._cached_messages`:
```python
self._cached_messages.append(message)  # line 175 in agent_instance.py
```

Then `messages.append(notification_msg)` at line 273 appends AGAIN because `messages` IS `_cached_messages`.

**⚠️ POTENTIAL DOUBLE-APPEND BUG:** If the same message object is appended to `_cached_messages` via `append_message()` AND then again via `messages.append()`, it's the SAME list object, so the notification appears once. This is correct.

---

## DUPLICATION BUG SCENARIOS IDENTIFIED

### Scenario #1: Cache Extend Duplication (MOST LIKELY ROOT CAUSE) ⭐⭐⭐

**Trigger**: After forced compression rebuilds working sets, the next turn uses cache extend logic instead of full rebuild.

**Flow:**
1. Forced compression runs → `_rebuild_working_set()` at handler.py line 238
   - `instance._cached_messages` = `messages` (shallow assign at line 1333)
   - `instance._last_config_version = self.pool._config_version` (line 1336)

2. Notification injected at lines 272-274 → appended to all working sets including `_cached_messages`

3. **Compression returns**. The execution loop continues with the next LLM call.

4. After LLM response, `_process_response()` adds turn_output messages via `_append_to_working_sets_batch()`:
   ```python
   instance.append_messages(turn_output)  # line 2195 in _process_response
   ```

5. **Next turn** — `_setup_turn()` is called (execution_engine.py lines 938-962):
   ```python
   can_use_cache = (
       instance._last_config_version == self.pool._config_version and
       instance._cached_messages and
       instance._cached_llm_messages
   )
   ```
   
   Cache is valid → enters cache extend path at lines 946-962:
   ```python
   cached_len = len(instance._cached_messages)     # e.g., 15
   current_len = len(instance.conversation)         # e.g., 17 (2 new messages from turn_output)
   
   if current_len > cached_len:
       new_messages = list(instance.conversation[cached_len:])  # gets the 2 new messages
       instance._cached_messages.extend(new_messages)           # extends _cached_messages
   ```

6. **Then next forced compression happens**:
   - `_rebuild_working_set()` clears and refills `messages` from pool state (line 1307 via helpers.py)
   - Pool state includes: [SYS, COMP1, tail..., N1, A1, U2, A2] — all correct
   
   **BUT** the cache extend at step 5 already extended `_cached_messages`. After rebuild, `messages` is cleared and refilled. The `_cached_messages` reference is replaced with the new `messages` list (line 1333). So this path is clean.

### Scenario #2: Notification Appearing in Active Set on Re-compression ⭐⭐

**Flow:**
1. First compression at turn N → conversation = [SYS, COMP1, tail..., N1]
2. Agent generates A1, user sends U2 → conversation = [SYS, COMP1, tail..., N1, A1, U2]
3. Second forced compression triggers:
   - `get_compression_target_set()` gets conv copy: [SYS, COMP1, tail..., N1, A1, U2]
   - `find_last_marker()` finds COMP1 at index 1 → active_start_idx = 2
   - Active set includes N1, A1, U2 and all subsequent messages
   - Compression summarizes the active set including notification N1
   
4. **This is correct behavior** — notifications are part of the conversation history that gets summarized.

### Scenario #3: The Stale Cache Extend Path ⭐⭐⭐ (ROOT CAUSE)

**The exact duplication scenario:**

Look at `_setup_turn()` cache extend logic (lines 946-962):
```python
cached_len = len(instance._cached_messages)     # e.g., 15
current_len = len(instance.conversation)         # e.g., 17

new_messages = list(instance.conversation[cached_len:])
instance._cached_messages.extend(new_messages)
```

**After forced compression rebuild (line 1333)**:
- `instance._cached_messages` points to the SAME object as local `messages` variable
- Both have length, say, 15 messages

**Then notification is appended**:
- Via `instance.append_message(notification_msg)` at line 272 → adds to `_cached_messages` (length becomes 16)
- Via `messages.append(notification_msg)` at line 273 → adds AGAIN to the SAME list (length becomes 17!)

**⚠️ DOUBLE APPEND BUG CONFIRMED!**

Let me verify: After rebuild at line 1333, `_cached_messages` and `messages` are the **same object**. Then:
- Line 272: `instance.append_message(notification_msg)` → internally calls `self._cached_messages.append(message)` → length 16
- Line 273: `messages.append(notification_msg)` → same list! → length 17

**The notification appears TWICE in the working set!**

Similarly for llm_messages (lines 1335, 274):
- Line 272: `_cached_llm_messages.append(message)` inside append_message → adds once
- Line 274: `llm_messages.append(notification_msg)` → same list! → adds again

**This is the root cause of duplication.**

### Scenario #4: Logger Recovery Duplication ⭐⭐

After forced compression, validation fails (handler.py lines 283-314):
```python
conv = self.pool.get_conversation(inst_name)       # includes notification N1
if not validate_message_pool(conv, inst_name):     # validation fails
    recov = logger.data.get('history', [])         # logger history WITHOUT notification
    instance.rebuild_conversation(list(recov))      # replaces conversation
```

Logger was synced AFTER compression but BEFORE notification injection. So recovery data has: [SYS, COMP1, tail...] without N1. This is correct — no duplication. But if the agent generates content similar to N1 on next turn, semantic duplication occurs (not a bug per se).

---

## ROOT CAUSE SUMMARY

**The compression duplication bug is caused by double-append in the notification injection path:**

After `_rebuild_working_set()` assigns caches via shallow reference (lines 1333-1335):
```python
inst._cached_messages = messages       # same object!
inst._cached_llm_messages = llm_messages  # same object!
```

Notification injection appends to BOTH the instance AND the local working sets:
```python
instance.append_message(notification_msg)   # → _cached_messages.append() [length +1]
messages.append(notification_msg)           # → SAME list append() [length +1 again!]
llm_messages.append(notification_msg)       # → SAME list append() [length +1 again!]
```

**Result**: Each notification message appears **twice** in the working sets.

The same bug could also occur for:
- Tool result messages appended via `_append_to_working_sets()` followed by manual appends
- Continue-saved messages merged at line 2239-2240

---

## VERIFICATION TEST PLAN

1. Add debug logging after rebuild to check `id(messages) == id(instance._cached_messages)`
2. Count notification occurrences in working sets after forced compression
3. Check if the same message object appears twice (identity comparison, not equality)