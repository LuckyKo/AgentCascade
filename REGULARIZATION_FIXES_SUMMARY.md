# Regularization Refactoring Fixes Summary

This document summarizes the fixes applied to address the reviewer's identified issues in the Security/Compressor agent regularization refactoring.

## Overview

All five fixes have been successfully applied to ensure thread-safe active_stack manipulation and consistent settings propagation across the codebase.

---

## Fix 1 (Critical): Compressor Double active_stack Removal

**File:** `agent_cascade/compression/agent_invoker.py`, lines 289-296

**Issue:** The outer cleanup block directly mutated `agent_pool.active_stack` without lock protection, causing potential race conditions.

**Solution:** Replaced direct list mutation with the safe `active_stack_remove()` method:

```python
# Before:
if comp_state_key in agent_pool.active_stack:
    agent_pool.active_stack.remove(comp_state_key)

# After:
try:
    agent_pool.active_stack_remove(comp_state_key)
except Exception:
    pass  # Already removed or never existed - non-critical
```

**Impact:** Thread-safe cleanup that handles edge cases where the key may already be removed.

---

## Fix 2 (Critical): Security Agent active_stack Cleanup Without Lock

**File:** `agent_cascade/api_server.py`, lines 2106-2112

**Issue:** The finally block directly mutated `agent_pool._execution.active_stack` without holding `_state_lock`. The lock was held for instance_state cleanup but released before active_stack cleanup, creating a race condition.

**Solution:** Extracted active_stack cleanup from the locked section and used the safe method:

```python
# Before:
with agent_pool._execution._state_lock:
    agent_pool.instance_state[sec_state_key]['active'] = False
    if any(n == sec_state_key for n, _depth in agent_pool._execution.active_stack):
        for i, (n, _depth) in enumerate(agent_pool._execution.active_stack):
            if n == sec_state_key:
                agent_pool._execution.active_stack.pop(i)
                break

# After:
with agent_pool._execution._state_lock:
    agent_pool.instance_state[sec_state_key]['active'] = False
try:
    agent_pool.active_stack_remove(sec_state_key)
except Exception:
    pass  # Already removed or never existed - non-critical
```

**Impact:** Eliminates race condition by using the thread-safe wrapper method.

---

## Fix 3 (Major): Use Consistent API in _create_system_agent()

**File:** `agent_cascade/execution_engine.py`, lines 3020-3021

**Issue:** Direct list append was used instead of the wrapper method, inconsistent with the refactoring goals.

**Solution:** Replaced direct manipulation with the wrapper method:

```python
# Before:
with self.pool._execution._state_lock:
    self.pool._execution.active_stack.append((instance_name, 0))

# After:
self.pool.active_stack_append(instance_name, 0)
```

**Impact:** Consistent API usage throughout the codebase and proper lock handling.

---

## Fix 4 (Major): Add Settings Propagation for Compressor Path B

**File:** `agent_cascade/compression/agent_invoker.py`, lines 202-219

**Issue:** When using `_create_system_agent()` to create the Compressor instance, LLM settings from the session were not being propagated, potentially causing inconsistent behavior.

**Solution:** Added settings propagation logic similar to the Security agent pattern:

```python
# Configure Compressor settings (similar to Security agent pattern)
NON_LLM_KEYS = (
    'max_auto_rollbacks', 'auto_rollback_on_loop', 'auto_continue', 
    'max_turns', 'mcpServers', 'work_access_folders', 'seed',
    'read_file_limit', 'grep_char_limit', 'grep_spillover', 'shell_char_limit', 'code_char_limit',
    'disabled_tools',
    'model', 'model_server', 'api_base', 'base_url', 'api_key', 'model_type'
)
if hasattr(agent_pool, 'operation_manager'):
    session = getattr(agent_pool.operation_manager, '_current_session', None)
    if session:
        template = agent_pool.templates.get('Compressor')
        if template and hasattr(template, 'llm'):
            cfg = (template.llm.generate_cfg or {}).copy()
            ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))
            llm_safe_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
            cfg.update(llm_safe_cfg)
            comp_instance._generate_cfg_override = cfg
```

Also added `import copy` at the top of the file.

**Impact:** Compressor agent now inherits the same LLM configuration as the parent session, ensuring consistent behavior.

---

## Fix 5 (Minor): Remove Redundant max_turns Assignment

**File:** `agent_cascade/api_server.py`, line 1875

**Issue:** The line `sec_instance.max_turns = 50` was redundant since `engine.run()` defaults to 50 turns.

**Solution:** Removed the redundant assignment.

**Impact:** Cleaner code without functional change.

---

## Files Modified

1. `agent_cascade/compression/agent_invoker.py`
   - Added `import copy`
   - Applied Fix #1 (lines 289-296)
   - Applied Fix #4 (lines 202-219)

2. `agent_cascade/api_server.py`
   - Applied Fix #2 (lines 2106-2112)
   - Applied Fix #5 (removed line 1875)

3. `agent_cascade/execution_engine.py`
   - Applied Fix #3 (lines 3020-3021)

---

## Testing Recommendations

1. **Thread Safety Tests**: Verify that concurrent Security and Compressor agent invocations don't cause race conditions in active_stack manipulation.

2. **Settings Propagation Tests**: Ensure Compressor inherits LLM settings correctly when invoked via both call_agent pattern and engine-based execution.

3. **Cleanup Verification**: Test that all agent state is properly cleaned up after completion, including error scenarios.

---

## Notes

- All fixes use the existing `active_stack_append()` and `active_stack_remove()` methods from `AgentPool`, ensuring consistency with the refactoring goals.
- The try/except patterns in fixes #1 and #2 handle edge cases gracefully without interrupting normal flow.
- Settings propagation follows the exact pattern used for Security agent, ensuring maintainability.

---

**Date:** 2026-06-12
**Reviewer:** To be assigned
**Status:** Fixes applied, ready for review