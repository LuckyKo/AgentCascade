# Feature 007: Truncation/Spillover Unification

## Overview
Unify truncation detection and spillover handling across AgentCascade unified branch. Replace fragile string-match guards with thread-local state tracking, ensure MAX_SPILL_SIZE consistency (50MB everywhere), thread agent_name through grep call chain for correct spillover filenames, remove dead code and redundant markers, add explicit max_spill_size in code_interpreter, and add collision guard in tool_utils.py.

## Changes Required

### 1. Thread-Local State Tracking for Truncation Detection
**File**: `tool_utils.py`

Replace the fragile string-match guard (`'[TOOL RESPONSE TRUNCATED' in tool_result`) with thread-local state tracking:

```python
import threading

# Thread-local storage for truncation state
_thread_locals = threading.local()

def mark_tool_call_truncated(instance_name: str, tool_name: str):
    """Mark that a tool call was truncated for the current thread."""
    if not hasattr(_thread_locals, 'truncated_calls'):
        _thread_locals.truncated_calls = {}
    key = f"{instance_name}:{tool_name}"
    _thread_locals.truncated_calls[key] = True

def was_tool_call_truncated(instance_name: str, tool_name: str) -> bool:
    """Check if a tool call was truncated in the current thread."""
    if not hasattr(_thread_locals, 'truncated_calls'):
        return False
    key = f"{instance_name}:{tool_name}"
    return _thread_locals.truncated_calls.get(key, False)

def clear_truncation_state():
    """Clear truncation state for the current thread."""
    if hasattr(_thread_locals, 'truncated_calls'):
        _thread_locals.truncated_calls = {}
```

### 2. MAX_SPILL_SIZE Consistency (50MB everywhere)
**Files**: `execution_engine.py`, `operation_manager.py`, `code_interpreter.py`

Ensure all spillover operations use 50MB limit:
- `MAX_SPILL_SIZE = 50 * 1024 * 1024`  # 50MB

Currently some places may have 10MB - fix to 50MB consistently.

### 3. Thread agent_name Through grep Call Chain
**Files**: `file_ops.py`, `operation_manager.py`

Ensure spillover filenames include the correct agent_name for traceability:
- Grep class should pass `agent_instance_name` through kwargs
- Operation manager's grep method should use this for spillover filename generation

### 4. Remove Dead Code and Redundant Truncation Markers
**Files**: `execution_engine.py`, `operation_manager.py`, `code_interpreter.py`

Remove redundant `[TOOL RESPONSE TRUNCATED` string checks that are no longer needed with thread-local state tracking.

### 5. Add Explicit max_spill_size in code_interpreter
**File**: `code_interpreter.py`

Add MAX_SPILL_SIZE constant and cap spillover file size:
```python
MAX_SPILL_SIZE = 50 * 1024 * 1024  # 50MB

# In truncation logic:
if len(result) > MAX_SPILL_SIZE:
    result = result[:MAX_SPILL_SIZE] + "\n\n[SPILL FILE TRUNCATED — exceeded maximum size]"
```

### 6. Add Collision Guard in tool_utils.py with Counter Cap < 1000
**File**: `tool_utils.py`

Add collision detection for spillover filenames:
```python
def generate_spillover_filename(instance_name: str, tool_name: str, base_dir: Path) -> str:
    """Generate a unique spillover filename with collision detection.
    
    Args:
        instance_name: The agent instance name
        tool_name: The tool name
        base_dir: Directory to write spillover files
        
    Returns:
        Unique filename string (not full path)
        
    Raises:
        ValueError: If counter exceeds 1000 collisions
    """
    from datetime import datetime
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    safe_tool = re.sub(r'[^a-zA-Z0-9_-]', '_', tool_name)
    safe_instance = re.sub(r'[^a-zA-Z0-9_-]', '_', instance_name)
    
    counter = 1
    while counter < 1000:
        if counter == 1:
            spill_filename = f"{safe_instance}_{safe_tool}_{timestamp}.txt"
        else:
            spill_filename = f"{safe_instance}_{safe_tool}_{timestamp}_{counter}.txt"
        
        spill_path = base_dir / spill_filename
        if not spill_path.exists():
            return spill_filename
        
        counter += 1
    
    raise ValueError(f"Spillover filename collision exceeded 1000 attempts for {instance_name}/{tool_name}")
```

## Files Modified
1. `agent_cascade/execution_engine.py` - Update truncation detection, MAX_SPILL_SIZE
2. `agent_cascade/tool_utils.py` - Add thread-local state tracking, collision guard
3. `agent_cascade/operation_manager.py` - MAX_SPILL_SIZE consistency, agent_name threading
4. `agent_cascade/tools/custom/file_ops.py` - Pass agent_name through grep chain
5. `agent_cascade/tools/code_interpreter.py` - Add explicit max_spill_size

## Testing Checklist
- [ ] Thread-local truncation state works correctly
- [ ] MAX_SPILL_SIZE is 50MB in all locations
- [ ] Spillover filenames include correct agent_name
- [ ] No redundant truncation markers remain
- [ ] code_interpreter caps spillover size at 50MB
- [ ] Collision guard prevents infinite loops (cap < 1000)

## Rollback Plan
If issues arise:
1. Revert thread-local state changes in tool_utils.py
2. Restore string-match guards temporarily
3. Verify MAX_SPILL_SIZE consistency