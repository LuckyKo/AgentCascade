"""Shared utility functions for tool execution.

Import via: from agent_cascade.tool_utils import resolve_prev_arg_placeholders
"""

import copy
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional, Tuple

# Maximum size for spillover files (50MB) - consistent across all modules
MAX_SPILL_SIZE = 50 * 1024 * 1024  # 50MB

# Thread-local storage for truncation state tracking
_thread_locals = threading.local()


def mark_tool_call_truncated(instance_name: str, tool_name: str):
    """Mark that a tool call was truncated for the current thread.
    
    Replaces fragile string-match guards like '[TOOL RESPONSE TRUNCATED' in tool_result
    with explicit thread-local state tracking for reliable truncation detection.
    
    Args:
        instance_name: The agent instance name
        tool_name: The tool that produced truncated output
    """
    if not hasattr(_thread_locals, 'truncated_calls'):
        _thread_locals.truncated_calls = {}
    key = f"{instance_name}:{tool_name}"
    _thread_locals.truncated_calls[key] = True


def was_tool_call_truncated(instance_name: str, tool_name: str) -> bool:
    """Check if a tool call was truncated in the current thread.
    
    Args:
        instance_name: The agent instance name
        tool_name: The tool to check for truncation
        
    Returns:
        True if the tool call was marked as truncated, False otherwise
    """
    if not hasattr(_thread_locals, 'truncated_calls'):
        return False
    key = f"{instance_name}:{tool_name}"
    return _thread_locals.truncated_calls.get(key, False)


def clear_truncation_state():
    """Clear truncation state for the current thread.
    
    Call this at the start of each turn or when context is reset to prevent
    stale truncation markers from affecting subsequent operations.
    """
    if hasattr(_thread_locals, 'truncated_calls'):
        _thread_locals.truncated_calls = {}


def generate_spillover_filename(instance_name: str, tool_name: str, base_dir: Path) -> str:
    """Generate a unique spillover filename with collision detection.
    
    Creates filenames in the format: {safe_instance}_{safe_tool}_{timestamp}.txt
    Handles collisions by appending a counter (_1, _2, etc.) up to 1000 attempts.
    
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


def resolve_prev_arg_placeholders(
    tool_args: Any,
    instance_scope: str,
    tool_name: str,
    agent_pool: Any,
    lock: Optional[threading.Lock] = None,
) -> Tuple[Any, Optional[str]]:
    """Resolves __USE_PREV_ARG__ placeholders from the last tool call.

    This is a shared utility that replaces the inline resolution in agent_orchestrator.py.
    Works for both streaming and non-streaming tool paths.

    Thread-safety note: When *lock* is provided, cache reads are protected by it.
    Callers that already hold *lock* must pass ``None`` to avoid deadlock.
    When *lock* is ``None``, this function is NOT thread-safe for the read path.

    Args:
        tool_args: Tool arguments (typically a dict after JSON parsing).
                   Non-dict inputs pass through unchanged with no error.
        instance_scope: The instance name scope (e.g., session_name).
        tool_name: Name of the tool being called.
        agent_pool: Reference to the AgentPool for accessing last_tool_args cache.
        lock: Optional threading.Lock to guard cache reads. Pass ``None`` if the
              caller already holds the relevant lock, or when thread-safety is not
              needed (e.g., tests).

    Returns:
        tuple: (resolved_args, error_message)
            - resolved_args: dict with placeholders replaced, or original args if no
              placeholders were found. On error, returns the UNMODIFIED original
              tool_args; callers MUST NOT use it for execution.
            - error_message: None on success, error string if resolution failed.
    """
    if not isinstance(tool_args, dict):
        # Non-dict inputs pass through unchanged (no placeholders to resolve).
        return tool_args, None

    # Scan for placeholders
    placeholders_found = [key for key, val in tool_args.items() if val == "__USE_PREV_ARG__"]

    if not placeholders_found:
        return tool_args, None

    resolved_args = copy.deepcopy(tool_args)

    try:
        if lock is not None:
            with lock:
                scope_cache = agent_pool.last_tool_args.get(instance_scope, {})
                prev_args = scope_cache.get(tool_name)
                global_args = scope_cache.get("__GLOBAL__", {})
        else:
            scope_cache = agent_pool.last_tool_args.get(instance_scope, {})
            prev_args = scope_cache.get(tool_name)
            global_args = scope_cache.get("__GLOBAL__", {})
    except AttributeError:
        # Defensive: agent_pool may not have last_tool_args in unusual setups.
        return tool_args, None

    if not prev_args and not global_args:
        return tool_args, (
            f"Error: Cannot use __USE_PREV_ARG__ for '{tool_name}' because no previous "
            f"call to this tool was recorded for instance '{instance_scope}'."
        )

    for arg_key in placeholders_found:
        if prev_args and arg_key in prev_args:
            # Deepcopy resolved values to prevent cache mutation via shared refs.
            resolved_args[arg_key] = copy.deepcopy(prev_args[arg_key])
        elif arg_key in global_args:
            resolved_args[arg_key] = copy.deepcopy(global_args[arg_key])
        else:
            return tool_args, (
                f"Error: Cannot use __USE_PREV_ARG__ for argument '{arg_key}' because "
                f"it was not found in previous calls (neither specific to '{tool_name}' "
                f"nor globally)."
            )

    return resolved_args, None