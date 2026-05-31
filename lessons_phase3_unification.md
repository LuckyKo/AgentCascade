# Phase 3: Root/Sub-Agent Unification - instance_conversations Cleanup

## Context
Phase 1 & 2 completed. Phase 3 eliminates remaining direct `instance_conversations` writes/reads 
that bypass the proper pool API pattern.

## Bridge Class: `_InstanceConversationMapping` (agent_pool.py)
- Syncs reads/writes between `pool.instance_conversations[name]` and `pool.instances[name].conversation`
- When writing through bridge: propagates to `inst.conversation = list(value)` + invalidates token cache
- When reading through bridge: returns `list(inst.conversation)` under lock

## Why Replace Direct Writes?
The bridge is a transition mechanism. The canonical access path should be:
- **Writes**: `pool.get_instance(name).conversation.clear()` + `.extend(new_list)` or direct assignment
- **Reads**: `pool.get_conversation(name)` which returns `list(inst.conversation)` under lock

## Files Modified in Phase 3

### 1. execution_engine.py (2 writes)
- Line ~539: Recovery path after forced compression → use inst.conversation directly
- Line ~1415: Recovery path after /compress → use inst.conversation directly

### 2. agent_invoker.py /compression/agent_invoker.py (2 writes)
- Line ~147: Initial state setup for compression agent → use proper pool API
- Line ~214: Streaming update during compression → use proper pool API

### 3. core.py /compression/core.py (1 read + 2 writes)
- Line ~254: Pre-mutation snapshot read → use pool.get_conversation()
- Line ~273: Post-compression write → use inst.conversation directly
- Line ~314: Rollback write → use inst.conversation directly

### 4. manager_ops.py /tools/custom/manager_ops.py (2 writes + 4 reads)
- Line ~127: Read messages for sub-agent init → use pool.get_conversation()
- Line ~131: Write messages for sub-agent init → use pool.add_message() pattern
- Line ~267: Get all instance keys for dismiss-all → use pool.instances.keys()
- Line ~278: Check if instance exists in conversations → check pool.instances or use get_conversation()
- Line ~304: Validate instance exists before dismissing → check pool.instances
- Line ~385: Get all instance keys for status listing → use pool.instances.keys()

### 5. system_info.py /tools/custom/system_info.py (1 read)
- Line ~90: Count running sessions → use len(pool.instances) instead of len(pool.instance_conversations)

## Key Pattern for Writes
```python
# Before:
agent_pool.instance_conversations[name] = new_list
# After:
inst = agent_pool.get_instance(name)
if inst:
    with inst._compression_lock:
        inst.conversation = list(new_list)  # or .clear() + .extend()
    inst._last_token_count_conversation_length = -1  # invalidate token cache
```

## Key Pattern for Reads
```python
# Before:
agent_pool.instance_conversations.get(name, [])
# After:
conv = agent_pool.get_conversation(name)  # returns [] if not found
```

## Key Pattern for Keys/Count
```python
# Before:
set(agent_pool.instance_conversations.keys())
len(agent_pool.instance_conversations)
# After:
set(agent_pool.instances.keys())
len(agent_pool.instances)
```