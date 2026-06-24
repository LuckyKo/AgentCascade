# Compression Duplication Bug — Full Mutation Trace & Analysis

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
    def __setitem__(self, key, val):  # Write → calls inst.rebuild_conversation(list(value)) (line 92)
```

Key: `__setitem__` does TWO things simultaneously:
- **Line 92**: Calls `inst.rebuild_conversation(list(value))` — updates instance.conversation + caches
- **Line 93**: Also stores in dict's internal storage via `super().__setitem__(key, value)`

---

## FORCED COMPRESSION FLOW — Step-by-Step Mutation Trace

### Phase 1: Compression Trigger (execution_engine.py lines 1159-1184)
`_check_and_trigger_compression()` calculates usage_pct → calls `_force_compression()` at line 1184.

### Phase 2: Halt Other Agents (handler.py lines 216-219)
No pool mutations here — just halting.

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

**Step C — Boundary adjustment (lines 291-322)**: `insert_pos` can increase if tool calls span the boundary.

**Step D — Build new_history (line 335)**:
```python
new_history = history[:active_start_idx] + [marker_message] + history[insert_pos:]
```

### **MUTATION #1** — Write to pool (core.py line 336):
```python
agent_pool.instance_conversations[target_agent_name] = new_history
```

Call chain:
1. `instance_conversations` property accessed → returns `_InstanceConversationMapping`
2. `__setitem__(target_agent_name, new_history)` called (line 82-93):
   - **Line 92**: `inst.rebuild_conversation(list(value))`:
     ```python
     self.conversation = list(new_messages)       # line 321
     self._cached_messages = list(new_messages)    # line 322
     self._cached_llm_messages = list(new_messages) # line 323
     ```
   - **Line 93**: `super().__setitem__(key, value)` — stores in dict

**After MUTATION #1:**
- `instance.conversation` = new_history (via rebuild_conversation)
- `instance._cached_messages` = copy of new_history
- `instance._cached_llm_messages` = copy of new_history
- `_instance_conversations` dict storage = new_history

### Phase 4: Working Set Rebuild (handler.py line 238)

```python
self.engine._rebuild_working_set(messages, llm_messages, inst_name)
```

Inside `_rebuild_working_set` (execution_engine.py lines 1283-1336):
1. **Line 1307**: `rebuild_working_set(messages, self.pool, inst_name)` from helpers.py:
   - Gets fresh copy via `get_conversation(agent_name)` → returns new_history
   - Clears and extends `messages` list with deepcopy
   
2. **Lines 1310-1316**: Rebuilds `llm_messages`

**After Phase 4:** Working sets (`messages`, `llm_messages`) match pool state (new_history).

### **MUTATION #2** — Notification Injection (handler.py lines 250-279):
```python
with instance._compression_lock:
    notification_exists = any(
        m.role == USER and isinstance(m.content, str) and notification_text == m.content
        for m in instance.conversation          # line 264-267
    )
    
    if not notification_exists:
        notification_msg = Message(role=USER, content=notification_text)
        instance.append_message(notification_msg)   # line 272
```

Inside `append_message` (agent_instance.py lines 161-177):
```python
self.conversation.append(message)          # line 174
self._cached_messages.append(message)      # line 175
self._cached_llm_messages.append(message)  # line 176
```

**After MUTATION #2:**
- `instance.conversation` = new_history + [notification_msg]
- `_cached_messages`, `_cached_llm_messages` also have notification appended

### Phase 5: Validation & Recovery (handler.py lines 283-314)

```python
conv = self.pool.get_conversation(inst_name)       # line 283 — includes notification now
if not validate_message_pool(conv, inst_name):     # line 287
    recov = logger.data.get('history', [])         # line 291
    instance.rebuild_conversation(list(recov))      # line 294 ← MUTATION #3 (conditional)
```

---

## TOOL-BASED COMPRESSION FLOW (compress_context tool, /compress command)

### Via compress_context tool (handler.py lines 345-410):

**Line 376-384**: Calls `compress_context()` → same as forced compression up to MUTATION #1.

Then:
```python
conv = self.pool.get_conversation(target_agent_name)   # line 392
messages_list = list(conv)                              # line 394
llm_messages_list = list(self.pool.slice_history_for_llm(conv))  # line 395
self.engine._rebuild_working_set(messages_list, llm_messages_list, target_agent_name)  # line 396
```

**No notification injection** in tool-based flow — correct.

---

## DUPLICATION BUG ANALYSIS

### Finding #1: Stale Dict Storage via Lazy Sync ⭐⭐

The version tracking mechanism (agent_pool.py lines 298-300):
```python
self._instances_version = 0        # increments on create/remove/dismiss/reset ONLY
self._mapping_synced_to_version = -1
```

**`_instances_version` is NOT incremented when conversation content changes!** This includes:
- Compression writes via `instance_conversations[target] = new_history` (core.py line 336)
- Notification appends via `instance.append_message(...)` (handler.py line 272)

The lazy sync in `instance_conversations` property checks `_instances_version`, but compression doesn't change that version. The mapping stays "in sync" by virtue of its `__setitem__` propagating to instances AND storing in dict storage simultaneously.

**However**, the `_sync_from_instances()` method (line 44-63) does a FULL rebuild from instances:
```python
for name, inst in self._pool.instances.items():
    with inst._compression_lock:
        super().__setitem__(name, list(inst.conversation))
```

This OVERWRITES the dict storage. If called between MUTATION #1 and MUTATION #2 (unlikely but possible via concurrent access), the notification appended at line 272 would be overwritten by a stale snapshot.

### Finding #2: Double Notification on Consecutive Compressions ⭐⭐

When forced compression runs TWICE in succession:
1. First compression injects N1 into conversation at usage_pct=96%
2. Second compression's `get_compression_target_set()` gets fresh conv copy including N1
3. N1 has role=USER but content doesn't start with COMPRESSION_MARKER → NOT treated as marker (correct)
4. Dedup guard checks for EXACT text match — since usage_pct changes, N1≠N2

**This is correct behavior.** No duplication here. But notifications accumulate over time without limit.

### Finding #3: Notification Appearing in Both Working Sets AND Pool ⭐⭐⭐

Look at handler.py lines 272-276:
```python
instance.append_message(notification_msg)   # line 272 — adds to instance.conversation + caches
messages.append(notification_msg)           # line 273 — adds to local working set
llm_messages.append(notification_msg)       # line 274 — adds to LLM working set
```

`instance.append_message()` already updates `_cached_llm_messages`. But then `messages` and `llm_messages` are the LOCAL lists passed from the execution loop. These are SEPARATE list objects (rebuilt in Phase 4 via deepcopy). So no duplication here — just keeping local refs in sync.

### Finding #4: The Recovery Path Duplication Bug ⭐⭐⭐ MOST LIKELY

**Scenario**: After forced compression, validation fails → recovery from logger happens.

```python
# handler.py lines 283-296
conv = self.pool.get_conversation(inst_name)       # includes notification N1
if not validate_message_pool(conv, inst_name):     # validation fails
    recov = self.pool.get_logger(...).data.get('history', [])   # logger history
    instance.rebuild_conversation(list(recov))      # replaces conversation with logger data
    self.engine._rebuild_working_set(messages, llm_messages, inst_name)  # rebuilds working sets
```

**The bug**: Logger was synced AFTER compression (line 316 `_sync_logger_after_compression`), but the notification N1 was appended to `instance.conversation` BEFORE logger sync. If validation fails and recovery happens:
- Logger history has: [SYS, COMP1, tail...] — NO notification
- But working sets are rebuilt from pool state which now has logger data — NO notification either

Then on next turn, the agent generates a response that includes the same content as N1 (because it was told to "continue your work"). This creates a semantic duplicate.

### Finding #5: The Compressor Agent's Conversation Leak ⭐⭐

When `invoke_compression_agent()` runs (agent_invoker.py lines 217-231):
```python
for resp in engine.run(comp_instance):
    ...
with comp_instance._compression_lock:
    final_msgs = list(comp_instance.conversation) if comp_instance.conversation else []
```

The compressor agent creates its OWN conversation. But the target's conversation is read via `get_conversation()` which returns a copy. No leak here unless the compressor somehow writes to the target's conversation. Let me check...

**Actually**, look at line 230-231:
```python
with comp_instance._compression_lock:
    final_msgs = list(comp_instance.conversation) if comp_instance.conversation else []
```

This reads the COMPRESSOR'S conversation, not the target's. Correct.

### Finding #6: The Working Set Rebuild Overlap ⭐⭐⭐ MOST LIKELY BUG SOURCE

**The exact duplication scenario:**

1. Forced compression triggers at handler.py line 238:
   ```python
   self.engine._rebuild_working_set(messages, llm_messages, inst_name)
   ```
   
2. Inside `_rebuild_working_set` (execution_engine.py lines 1283-1336):
   - Line 1307: `helpers.rebuild_working_set(messages, self.pool, inst_name)` — clears and extends `messages` with deepcopy of pool state
   - Line 1310: `conv = self.pool.get_conversation(inst_name)` — gets fresh copy
   - Lines 1314-1316: Clears and rebuilds `llm_messages`

3. **BUT** look at lines 1332-1336 (cache sync AFTER rebuild):
   ```python
   if inst:
       inst._cached_messages = messages      # line 1334 — assigns the SAME list object!
       inst._cached_llm_messages = llm_messages  # line 1335
       inst._last_config_version = self.pool._config_version  # line 1336
   ```

**This is a SHALLOW ASSIGNMENT!** `inst._cached_messages` now points to the SAME list object as the local `messages` variable. Any subsequent append to `messages` (like notification at line 273) also appends to `_cached_messages`. But that's fine because both should have the same content.

**HOWEVER**, look at what happens during the NEXT turn:
- The working set `messages` is reused from the previous turn (it's a local variable in the run loop)
- It already contains the notification N1
- When compression runs again, `_rebuild_working_set` clears it and refills — but if there's a race condition where messages are appended between clear and extend...

Actually, forced compression halts all agents so no concurrent modification. Let me look more carefully at the turn loop:

<tool_call>
<function=read_file>
</function>
</tool_call>


```

<tool_call>
<function=forget_last>
<parameter=count>
1