# Implementation Plan: Security Agent Endpoint Inheritance Fix

## Problem (todo.md line 90)

Security agent (and any agent without configured endpoints) falls back to slot 0 (global default) during slot collision detection, even though at runtime it inherits the caller's endpoint via Tier 2 fallback in `get_endpoint_chain()`. This causes false slot collisions:

- Agent A runs on slot 0
- Agent A calls Agent B async on slot 1
- Agent B triggers Security review → Security needs a slot
- Slot collision detection sees Security using global default (slot 0)
- But slot 0 is occupied by A → deadlock/blocking
- Meanwhile, at runtime Security would actually use B's inherited endpoint

## Root Cause

Mismatch between:
1. **Slot collision detection path** (`tool_dispatcher.py` → `get_agent_slot_info()` → `get_llm_config()`) — does NOT pass caller context
2. **Runtime LLM call path** (`execution_engine.py` → `call_with_fallback()` → `get_endpoint_chain(caller_agent_type=...)`) — DOES pass caller context

## Solution

Make slot collision detection use the same endpoint resolution logic as runtime by passing `caller_agent_type` through the chain.

## Files to Modify

### 1. `agent_cascade/api_router.py`

#### Change 1: `get_effective_concurrency()` (line 773)
Add `caller_agent_type` parameter. When agent has no **enabled** endpoints, check caller's before falling back to global default. Must mirror `get_endpoint_chain` logic — check for enabled endpoints, not just priorities existence.

```python
def get_effective_concurrency(self, agent_type: str, caller_agent_type: Optional[str] = None) -> int:
    """...docstring updated..."""
    defaults = self.default_llm_cfg or {}
    with self._lock:
        normalized_agent_type = self._normalize_agent_type(agent_type)
        
        # Tier 1: Agent-specific priorities — check for enabled endpoints
        has_enabled_endpoints = False
        for eid in self.agent_priorities.get(normalized_agent_type, []):
            ep = self.endpoints.get(eid)
            if ep and ep.enabled:
                has_enabled_endpoints = True
                return ep.concurrency_limit
        
        # Tier 2: Caller inheritance (NEW) — when agent has no enabled endpoints, use caller's
        # Mirrors get_endpoint_chain logic: checks for enabled endpoints, not just priorities existence
        if not has_enabled_endpoints and caller_agent_type:
            normalized_caller = self._normalize_agent_type(caller_agent_type)
            for eid in self.agent_priorities.get(normalized_caller, []):
                ep = self.endpoints.get(eid)
                if ep and ep.enabled:
                    return ep.concurrency_limit
        
        # Tier 3+: Fall back to default endpoint by api_base
        default_base = defaults.get('api_base') or defaults.get('model_server', '')
        for ep in self.endpoints.values():
            if ep.api_base == default_base:
                return ep.concurrency_limit
    
    if defaults.get('api_base') or defaults.get('model_server'):
        return 0
    return -1
```

#### Change 2: `get_llm_config()` (line 840)
Add `caller_agent_type` parameter and pass it to `get_endpoint_chain()`.

```python
def get_llm_config(self, agent_type: str, caller_agent_type: Optional[str] = None) -> dict:
    """...docstring updated..."""
    chain = self.get_endpoint_chain(agent_type, caller_agent_type=caller_agent_type)
    if chain:
        return chain[0]
    return copy.deepcopy(self.default_llm_cfg)
```

#### Change 3: `get_agent_slot_info()` (line 809)
Add `caller_agent_type` parameter and pass it through to both helper methods.

```python
def get_agent_slot_info(self, agent_class: str, caller_agent_type: Optional[str] = None) -> dict:
    """...docstring updated..."""
    concurrency = self.get_effective_concurrency(agent_class, caller_agent_type=caller_agent_type)
    if concurrency == -1:
        return {
            'slot_key': None,
            'is_sequential': False,
            'concurrency_limit': -1,
            'api_base': None,
            'needs_slot': False,
        }
    
    llm_cfg = self.get_llm_config(agent_class, caller_agent_type=caller_agent_type)
    api_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')
    
    slot_info = self.scheduler.get_slot_info(api_base, concurrency)
    slot_info['api_base'] = api_base
    slot_info['needs_slot'] = True
    
    return slot_info
```

### 2. `agent_cascade/tool_dispatcher.py`

#### Change: `_handle_call_agent()` (line 274)
Pass caller's agent type when getting child's slot info.

```python
# Before:
child_slot_info = router.get_agent_slot_info(agent_class) if router else None

# After:
caller_type = caller_slot_holder.agent_class if caller_slot_holder else None
child_slot_info = router.get_agent_slot_info(agent_class, caller_agent_type=caller_type) if router else None
```

### 3. `agent_cascade/agent_pool.py`

#### Change: `_acquire_slot()` (line 2453)
Look up the instance's parent and pass its agent type for endpoint resolution. Must check `is_terminated` to match `execution_engine.py` pattern — terminated parents shouldn't contribute their endpoints.

```python
def _acquire_slot(self, agent_class: str, instance_name: str):
    """..."""
    if not hasattr(self, 'api_router') or not self.api_router:
        return None

    router = self.api_router
    
    # Resolve caller context from instance's parent for endpoint inheritance
    # Mirrors execution_engine.py pattern (line 3206-3214): check is_terminated
    instance = self.get_instance(instance_name)
    caller_agent_type = None
    if instance and getattr(instance, 'parent_instance', None):
        parent = self.get_instance(instance.parent_instance)
        if parent and hasattr(parent, 'agent_class') and not getattr(parent, 'is_terminated', False):
            caller_agent_type = parent.agent_class
    
    try:
        concurrency_limit = router.get_effective_concurrency(agent_class, caller_agent_type=caller_agent_type)
        llm_cfg = router.get_llm_config(agent_class, caller_agent_type=caller_agent_type)
        api_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')
        
        logger.debug(
            f"[CALL_AGENT_DEBUG] _acquire_slot — agent_class={agent_class}, "
            f"instance_name={instance_name}, api_base={api_base}, concurrency_limit={concurrency_limit}"
            + (f", inherited_from={caller_agent_type}" if caller_agent_type else "")
        )
        
        return router.scheduler.acquire(api_base, concurrency_limit, instance_name, agent_class)
    except Exception as e:
        logger.error(f"Failed to acquire endpoint slot for {instance_name}: {e}")
        raise
```

## Risk Assessment

- **Low risk**: Changes are additive (optional parameters with defaults), backward compatible
- The Tier 2 inheritance logic already exists and is tested — we're just extending its use to the slot resolution path
- No changes to endpoint scheduler or concurrency control mechanisms

## Testing Checklist

1. Security agent invoked from async child uses parent's endpoint for slot collision detection
2. Agent with own configured endpoints still uses its own (Tier 1 takes precedence)
3. Nested inheritance is single-level only (no recursive parent chasing)
4. Existing behavior unchanged when no caller context available
5. Run existing test: `test_cursor_rotation_fallback_chain.py`

## Success Criteria

- No false slot collisions when Security agent (or any endpoint-less agent) is invoked by an async child
- Slot collision detection and runtime endpoint resolution produce the same result
- Backward compatible — no changes required to existing configurations