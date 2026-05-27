# Lessons — Phase 5 Polish (AgentCascade Unified Architecture)

**Date**: 2026-05-27  
**Author**: Phase5Polisher  

## Key Findings

### Two AgentPool Implementations Coexist
There are TWO `agent_pool.py` files in the codebase:
1. **Root level** (`N:/work/WD/AgentCascade_unified/agent_pool.py`, 1102 lines) — old pool, used by `agent_orchestrator.py` and tests
2. **New unified** (`N:/work/WD/AgentCascade_unified/agent_cascade/agent_pool.py`, ~1094 lines) — Phase 1-5 rewrite, used by `api_server.py`

When making changes, ALWAYS verify which pool is being imported. The new one uses `_InstanceConversationMapping`; the old one uses a plain dict for `instance_conversations`.

### _InstanceConversationMapping Pattern
The `_InstanceConversationMapping` class bridges writes to `instance_conversations` with `instances[name].conversation`. Key design:
- **Source of truth**: `pool.instances[name].conversation` (not dict storage)
- **Dict storage**: Only for entries that don't have corresponding instances (session rename pattern)
- **`__getitem__`**: Reads from instances first, falls back to dict storage
- **`__setitem__`**: Writes to BOTH instances AND dict storage

### Session Rename Pattern Creates Dict-Only Entries
When `set_session_name` is called in api_server.py (line 2187):
```python
agent_pool.instance_conversations[new_name] = agent_pool.instance_conversations.pop(old_name)
```
The `pop()` clears the instance's conversation but doesn't delete the instance. The subsequent write creates a dict-only entry under `new_name`. This means:
- `keys()`, `items()`, `values()` must include dict-only entries (Issue #11)
- `_sync_from_instances()` must preserve dict-only entries before clearing (Issue #13 data loss)

### sub_agent_state is a Legacy Shim — Phase 6 Removal Planned
The `sub_agent_state` dict is maintained by both `agent_orchestrator.py` AND `api_server.py`, creating a dual-write problem. It's supposed to be removed in Phase 6. The fix for Issue #9 reads from `pool.instances[name].conversation` directly, bypassing the stale data entirely.

### stopped Event Reset Pattern
The `agent_pool.stopped` property uses `threading.Event` (not a plain bool). After reset sets it True, every new user message clears it via `agent_pool.stopped = False`. No issue — the setter correctly calls `_stopped_event.clear()`.

## Code Patterns to Remember

### Preserving Dict-Only Entries During Sync
```python
def _sync_from_instances(self):
    # Save dict-only entries before clear
    dict_only = {}
    for key in super().keys():
        if key not in self._pool.instances:
            dict_only[key] = super().__getitem__(key)
    
    super().clear()
    for name, inst in self._pool.instances.items():
        super().__setitem__(name, inst.conversation)
    
    # Restore dict-only entries
    for key, val in dict_only.items():
        super().__setitem__(key, val)
```

### Unified Mode Root History Read (Bypassing sub_agent_state)
```python
if instance_name == 'root':
    inst = agent_pool.get_instance(session['session_name']) if agent_pool else None
    if inst is not None:
        return list(inst.conversation)  # Live data
    if session.get('history'):
        return list(session['history'])  # Fallback
    return []
```

## Thread Safety Notes
- `_InstanceConversationMapping` methods don't use explicit locks — they rely on higher-level lock protection
- The pool's `_state_lock` (RLock in ParallelAgentManager) protects `active_stack` and `sub_agent_state`
- `_sync_from_instances()` has no lock protection (single-thread-only assumption)

## Test Tips
- Use minimal mocks for AgentPool when testing specific methods — don't instantiate the full pool (requires agents directory, LLM config, etc.)
- For functional tests of `create_main_agent_instance`, mock just `create_instance()` and verify `sub_agent_state` population