# Lessons: Context Reprocessing / Message Stack Fix Investigation

**Date**: 2026-05-23  
**Author**: PlanReviewerCoder  
**Context**: Investigated mutation patterns and deep copy optimization opportunities in AgentCascade

---

## Key Discovery: Message Mutation Containment Story

The original concern was that shallow copy optimizations would allow mutations to leak between shared references. After deep investigation, the actual containment story is more nuanced:

### The Firewall at api_server.py:1291
```python
session['history'] = copy.deepcopy(agent_pool.instance_conversations[session['session_name']])
```

This post-generation sync acts as a **firewall**: it overwrites any session mutations with clean pool state. This means `_append_system_notification` (Site A) mutations DON'T leak into the pool in the normal path, because `llm_messages` is isolated from the pool by this sync.

**But this is fragile** — if the sync fails or is skipped, mutations propagate. Making the mutation sites create new objects (Phase 2A of revised plan) provides defense in depth.

### Message Type Matters
`Message` is a **Pydantic model** (`agent_cascade/llm/schema.py:128`) with `__setitem__`/`__getitem__` for dict-like access. Making it `frozen=True` would break ALL existing code that mutates messages — too invasive for the optimization gain.

### Mutation Sites Summary
| Site | Location | Risk | Fixed by Phase 2A? |
|------|----------|------|-------------------|
| A: _append_system_notification | agent_orchestrator.py:868 | HIGH | Yes |
| B: System msg init update | agent_orchestrator.py:986-987 | MEDIUM | Yes |
| C: User edit via UI | api_server.py:2503-2508 | LOW | No (out-of-band) |
| D: Gemma normalization | agent_orchestrator.py:1285-1286 | NONE | N/A (happens before list insertion) |
| E: Sub-agent system msg | agent_orchestrator.py:1870-1872 | LOW | Yes |

---

## Data Flow During Generation

Understanding this flow is critical for any optimization:

```
Generation Start:
  session['history'] ──deepcopy──► history_copy (passed to thread)
                                         │
                                      current_history = history_copy
                                         │
                          slice_history_for_llm(current_history)
                                         │
                                     working_history (new list, shared msg refs)
                                         │
                              agent_runner.run(working_history, ...)
                                         │
                         Inside _run(): messages = working_history
                                         │
                              llm_messages = deepcopy(messages)  ← isolated working set
                                         │
              _append_system_notification mutates llm_messages only
                                         │
Generation End:
  session['history'] = deepcopy(pool[name])  ← FIREWALL: overwrites any mutations
  session['history'].extend(responses)
  pool[name] = session['history']  ← sync back (same list object, shared refs)
```

---

## Deep Copy Cost Analysis

For a 10K message session (~5MB of message data):

| Operation | Time | Notes |
|-----------|------|-------|
| `copy.deepcopy(list_of_dicts)` | ~5-10ms | Each dict is ~200 bytes, deep copy traverses all nested objects |
| `copy.deepcopy(list_of_Message_objects)` | ~8-15ms | Pydantic models have more internal state to copy |
| `list(existing_list)` (shallow) | <1ms | Just copies the list structure, not elements |

**Total per generation cycle**: ~20-70ms  
**Estimated savings from shallow copy optimization**: ~15-50ms per cycle

---

## Common Pitfalls to Avoid

### 1. Don't Optimize Without Proving Immutability
The original plan's Phase 2 was correctly rejected. Shallow copy is ONLY safe when you can prove that shared message objects are never mutated after the shallow copy point. Always verify by:
- Tracing all mutation sites (grep for `msg['content'] =`, `msg.content =`, etc.)
- Checking that no code path mutates through a shared reference
- Adding safety tests that verify new object creation where mutations happen

### 2. Cache Invalidation is the Hard Part
Caching the compression marker index (Phase 1.2) seems simple but has tricky invalidation scenarios:
- Rollback truncates history past the cached marker → stale cache
- Retry adds messages back → cache might be wrong
- `show_active_only` toggle changes what's being displayed → serialization cache invalid

**Rule**: Always include invalidation logic in the same PR as the cache, not as a follow-up.

### 3. Sub-Agent Isolation Depends on Deep Copy at Clone Time
When a sub-agent is called recursively (line 1765), the conversation is deep copied:
```python
clone_conv = copy.deepcopy(base_conv)
```
If this were changed to shallow copy, mutations in the child agent's working set would leak into the parent's pool entry. This is one of the few places where deep copy is genuinely necessary for correctness.

### 4. `slice_history_for_llm` Returns Shallow References
The function at `agent_pool.py:614-638` creates a new list but shares message object references:
```python
sliced = history[latest_summary_idx:]  # New list, same message refs
return list(sliced)                     # New list, same message refs
```
This is important: any optimization that relies on this function must understand that the returned messages are shared with the source.

---

## Testing Tips

### Safety Test for Mutation Fixes
After fixing a mutation site (e.g., `_append_system_notification`), verify it creates a new object:

```python
def test_append_system_notification_creates_new_object():
    msg = Message(role='assistant', content='Hello')
    messages = [msg]
    original_id = id(msg)
    
    agent._append_system_notification(messages, 'guard', '[NOTIFICATION]')
    
    # The message in the list should be a DIFFERENT object
    assert id(messages[-1]) != original_id, "Mutation site still creates new object"
    # Original message should be UNCHANGED
    assert msg.content == 'Hello', "Original message was mutated"
```

### Memory Profiling for Shallow Copy
Use `tracemalloc` to verify memory savings:

```python
import tracemalloc

tracemalloc.start()
# Run generation cycle with deep copy
snapshot1 = tracemalloc.take_snapshot()

# Run generation cycle with shallow copy  
snapshot2 = tracemalloc.take_snapshot()

stats = snapshot2.compare_to(snapshot1, 'lineno')
for stat in stats[:10]:
    print(stat)
```

---

## Related Files and Line Numbers

### Critical Sync Points
- **api_server.py:1291** — Post-generation sync from pool to session (THE FIREWALL)
- **api_server.py:1309** — Post-generation sync from session back to pool
- **api_server.py:1843** — Pre-generation sync from session to pool

### Mutation Sites
- **agent_orchestrator.py:868** — `_append_system_notification` mutates last message
- **agent_orchestrator.py:986-987** — System message content update during init
- **agent_orchestrator.py:1285-1286** — Gemma normalization (safe, before list insertion)
- **agent_orchestrator.py:1870-1872** — Sub-agent system message update in pool
- **api_server.py:2503-2508** — User edit_message handler

### Deep Copy Locations
- **api_server.py:1848, 1908, 2003** — Generation start (3 locations for different entry points)
- **api_server.py:1291** — Post-generation sync
- **agent_orchestrator.py:1076** — LLM messages working set
- **agent_orchestrator.py:1156, 1160** — Forced compression re-sync
- **agent_cascade/compression/helpers.py:85** — rebuild_working_set

### Rollback Methods
- **agent_pool.py:341-352** — `rollback_to_snapshots`
- **agent_pool.py:353-403** — `surgical_rollback`