# Phase 5 Polish — Fix Summary

**Date**: 2026-05-27  
**Author**: Phase5Polisher  
**Scope**: agent_cascade/agent_pool.py, agent_cascade/api_integration.py  

---

## Issues Addressed

### Issue #11: _InstanceConversationMapping keys()/items()/values() Divergence ✅ FIXED

**File**: `agent_cascade/agent_pool.py` (lines 110-157)  
**Severity**: MAJOR

**Problem**: `keys()`, `items()`, and `values()` only iterated over `pool.instances`, but `__contains__` and `__iter__` checked both `pool.instances` AND dict storage. This meant that after a session rename (which creates a dict-only entry), `'new_name' in mapping` returned True, but iterating via `.items()` or `.keys()` would miss it.

**Fix**: Updated all three methods to follow the same pattern as `__iter__`:
- Yield from `pool.instances` first
- Then yield any dict-only entries (from session renames) that don't have corresponding instances
- Uses a `seen` set to avoid duplicates

**Before**:
```python
def keys(self):
    return self._pool.instances.keys()  # Misses dict-only entries

def items(self):
    for name, inst in self._pool.instances.items():
        yield name, inst.conversation  # Misses dict-only entries

def values(self):
    for inst in self._pool.instances.values():
        yield inst.conversation  # Misses dict-only entries
```

**After**: All three methods now include dict-only entries using the same `seen` deduplication pattern as `__iter__`.

---

### Issue #13: _sync_instance_conversations Only Called Once ✅ FIXED (Two-Part Fix)

**File**: `agent_cascade/agent_pool.py` (lines 45-64, 730-746)  
**Severity**: MINOR

**Problem**: The `instance_conversations` property checked `hasattr(self, '_instance_conversations')` and only created the mapping once. If instances were created AFTER the first access (e.g., sub-agents spawned during execution), the mapping's dict storage wouldn't include them.

**Fix Part 1 — Always Sync**: Changed the property to always call `_sync_from_instances()` on the existing mapping, which rebuilds the dict storage from `pool.instances`. This ensures new instances are visible in dict storage on every access.

**Fix Part 2 — Preserve Dict-Only Entries**: The original `_sync_from_instances()` did a blind `super().clear()` which destroyed dict-only entries (from session renames). Now it:
1. Saves dict-only entries before clearing
2. Rebuilds from pool.instances  
3. Restores the dict-only entries

**Before**:
```python
@property
def instance_conversations(self):
    if not hasattr(self, '_instance_conversations'):
        self._sync_instance_conversations()
    return self._instance_conversations  # Stale after first creation

def _sync_from_instances(self):
    super().clear()  # Destroys dict-only entries!
    for name, inst in self._pool.instances.items():
        super().__setitem__(name, inst.conversation)
```

**After**:
```python
@property
def instance_conversations(self):
    if not hasattr(self, '_instance_conversations'):
        self._sync_instance_conversations()
    else:
        # Refresh to include new instances (Fix #13)
        self._instance_conversations._sync_from_instances()
    return self._instance_conversations

def _sync_from_instances(self):
    dict_only = {}  # Save dict-only entries before clear
    for key in super().keys():
        if key not in self._pool.instances:
            dict_only[key] = super().__getitem__(key)
    super().clear()
    for name, inst in self._pool.instances.items():
        super().__setitem__(name, inst.conversation)
    # Restore dict-only entries (Fix #13 data loss)
    for key, val in dict_only.items():
        super().__setitem__(key, val)
```

---

### Issue #9: sub_agent_state Not Populated for Main Session ✅ FIXED (Two-Part Fix)

**Files**: `agent_cascade/api_integration.py` (lines 70-88), `api_server.py` (lines 491-505)  
**Severity**: MAJOR

**Problem**: In unified mode (`USE_UNIFIED_STATE=True`), `get_session_history()` reads from `agent_pool.sub_agent_state.get('root', {})`. But `create_main_agent_instance()` never registered the main session in `sub_agent_state`, causing empty history for the root agent. Even if initialized, the data would go stale during execution.

**Fix Part 1 — Initialization**: Added `sub_agent_state` population in `create_main_agent_instance()`. Registers under both `'root'` (what api_server expects) and the actual instance name for consistency with sub-agent registration.

**Fix Part 2 — Live Data Read**: Changed `get_session_history()` to read directly from `pool.instances[session_name].conversation` instead of stale `sub_agent_state['root']['messages']`. Falls back to `session['history']` if no pool instance exists yet. This eliminates the synchronization gap entirely.

**Before (api_integration.py)**:
```python
def create_main_agent_instance(...):
    instance = pool.create_instance(...)
    return instance  # No sub_agent_state registration
```

**After (api_integration.py)**:
```python
def create_main_agent_instance(...):
    instance = pool.create_instance(...)
    agent_label = f"{instance_name} (OrchestratorAgent)"
    pool.sub_agent_state['root'] = {
        'active': False,
        'agent_name': agent_label,
        'messages': list(instance.conversation),
    }
    if instance_name != 'root':
        pool.sub_agent_state[instance_name] = pool.sub_agent_state['root'].copy()
    return instance
```

**Before (api_server.py)**:
```python
if instance_name == 'root':
    store = agent_pool.sub_agent_state.get('root', {}) if agent_pool else {}
    msgs = store.get('messages', [])  # Stale data!
```

**After (api_server.py)**:
```python
if instance_name == 'root':
    inst = agent_pool.get_instance(session['session_name']) if agent_pool else None
    if inst is not None:
        return list(inst.conversation)  # Live data
    # Fallback to legacy session history
    if session.get('history'):
        return list(session['history'])
    return []
```

---

### Issue #12: agent_pool.stopped Reset Permanently ✅ NO FIX NEEDED

**Severity**: MINOR  
**Finding**: The audit flagged that after reset, `agent_pool.stopped = True` is set permanently. However, the code analysis shows that on every new user message (line 1602 of api_server.py), `agent_pool.stopped = False` is called, which clears the event. The stopped property uses a proper `threading.Event` with correct setter logic (set/clear). No fix needed.

---

## General Polish Tasks

### Unused Imports ✅ NONE FOUND
All imports in `agent_cascade/agent_pool.py` are actively used:
- `time` — 4 usages (`time.monotonic()`)
- `threading` — 3 usages (`threading.Event`, `threading.RLock`)
- `Path` — 7 usages
- `Any, Dict, List, Optional` — type annotations throughout
- `Assistant` — template registry
- `Message` — conversation handling
- `COMPRESSION_MARKER` — marker detection
- `DEFAULT_WORKSPACE` — workspace fallback
- `AgentInstance, PoolSettings` — instance and config management

### NotImplementedError Check ✅ NONE FOUND
No `NotImplementedError` or `raise NotImplemented` in the new pool code.

### Thread Safety ✅ VERIFIED
The `_InstanceConversationMapping` methods (`keys()`, `items()`, `values()`) iterate over shared state without explicit locking. This is consistent with the existing pattern — the original `__iter__` and `__contains__` also don't use locks. At higher levels, pool mutations are protected by `_state_lock` (an RLock in ParallelAgentManager) and instance-level operations hold their own locks.

---

## Files Modified

| File | Lines Changed | Description |
|------|--------------|-------------|
| `agent_cascade/agent_pool.py` | ~70 | Fixed Issue #11 (keys/items/values), Issue #13 (sync on access + preserve dict-only) |
| `agent_cascade/api_integration.py` | ~12 | Fixed Issue #9 part 1 (sub_agent_state initialization for main session) |
| `api_server.py` | ~15 | Fixed Issue #9 part 2 (read live data from pool.instances for root) |
| `tests/test_phase5_polish.py` | new | Test coverage for all three fixes |

---

## Deferred Items

None. All actionable issues from the audit have been addressed.

Issue #10 (NoOpLogger recovery) was noted as a "known limitation" and is deferred to Phase 6 along with the full removal of the `sub_agent_state` compatibility shim.