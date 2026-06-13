# Compressor Agent Regularization Summary

## Overview
Refactored the Compressor agent to follow the same lifecycle pattern as other agents (Coder, Researcher, Security), providing full AgentInstance lifecycle management.

## Changes Made

### 1. agent_cascade/compression/agent_invoker.py (Path B - Direct Execution)

**Before**: Used manual `sub_agent_state` setup and direct `comp_agent.run()` call on the template.

**After**: Uses proper `_create_system_agent()` + `engine.run()` pattern, matching system agent invocation pattern.

**Key Changes**:
- Replaced lines 180-234 with engine-based execution
- Creates proper AgentInstance via `engine._create_system_agent()`
- Executes via `engine.run(comp_instance)` 
- Cleans up active_stack using established `active_stack_remove()` method (thread-safe)
- Preserves summary_prompt structure for consistent compression behavior

**Code Pattern**:
```python
# Create proper AgentInstance via _create_system_agent()
engine = agent_pool._execution
comp_instance = engine._create_system_agent(
    agent_class='Compressor',
    instance_name=comp_state_key,
    task=summary_prompt,  # Contains history_text and existing_summary context
    caller=caller_name,
)

# Execute via engine.run()
for resp in engine.run(comp_instance):
    # Engine handles LLM call, retries, streaming

# Cleanup active_stack (engine.run() handles internal state transitions)
agent_pool.active_stack_remove(comp_state_key)
```

### 2. agent_cascade/execution_engine.py (Path A - call_agent Tool)

**Change**: Added `force_fresh=True` for Compressor (and Security) agents when called via the call_agent tool.

**Location**: Line 2051-2052

**Code**:
```python
# Pass force_fresh=True for system agents (Security, Compressor) to ensure fresh instances
force_fresh = agent_class in ('Security', 'Compressor')
inst, conv = self._create_and_run_agent(agent_class, instance_name, args, caller_name, child_depth, force_fresh=force_fresh)
```

**Rationale**: Ensures Compressor always gets a fresh instance without conversation history carryover when invoked via call_agent tool.

## Benefits

1. **Full AgentInstance Lifecycle**: Compressor now appears in the frontend with its own tab
2. **API Points Allocation**: Gets API points via the regular mechanism
3. **State Management**: Properly transitions to IDLE when done (handled by engine.run())
4. **WebUI Visibility**: Full visibility in the interface during execution
5. **Consistent Pattern**: Uses established `_create_system_agent()` pattern
6. **Thread-Safe**: Uses proper `active_stack_remove()` method for cleanup

## Testing Considerations

1. **Path A (call_agent)**: Already tested via orchestrator compression calls
2. **Path B (direct execution)**: Used when no orchestrator reference available (e.g., API server forced compression)
3. **System Message**: Now uses Compressor_soul.md template instead of custom message
4. **Task Prompt**: Still contains full history_text and existing_summary context via summary_prompt

## Files Modified

1. `agent_cascade/compression/agent_invoker.py` - Path B refactoring (simplified cleanup)
2. `agent_cascade/execution_engine.py` - force_fresh logic for call_agent path

## Backward Compatibility

- Existing compression behavior preserved
- Summary extraction logic unchanged
- Error handling maintained
- Timeout logic preserved (300 seconds)