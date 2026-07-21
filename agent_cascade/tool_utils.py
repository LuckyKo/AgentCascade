"""Shared utility functions for tool execution.

Import via: from agent_cascade.tool_utils import resolve_prev_arg_placeholders
"""

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any, Optional, Tuple

# Maximum size for spillover files (50MB) - consistent across all modules
MAX_SPILL_SIZE = 50 * 1024 * 1024  # 50MB

# Compiled regex for {USE_CACHED_ENTRY_N} resolution (module-level to avoid recompilation in hot path)
_CACHED_ENTRY_PATTERN = re.compile(r'\{USE_CACHED_ENTRY_(\d+)\}')

# Thread-local storage for truncation state tracking
_thread_locals = threading.local()


def resolve_cached_entry_refs(
    parsed: dict,
    cache_pool,
) -> dict[str, list]:
    """Scan a tool-args dict for {USE_CACHED_ENTRY_N} placeholders and resolve them.

    Args:
        parsed: The already-parsed tool arguments dict.
        cache_pool: An ArgumentCachePool instance (or None if not initialized).

    Returns:
        A mapping of key -> [(N, entry_value, placeholder_str), ...] for all found references.
    """
    cached_refs: dict[str, list] = {}
    for key, val in parsed.items():
        if isinstance(val, str):
            matches = list(_CACHED_ENTRY_PATTERN.finditer(val))
            if matches:
                for match in matches:
                    n = int(match.group(1))
                    entry = cache_pool.get(n) if cache_pool else None
                    if entry is not None:
                        cached_refs.setdefault(key, []).append((n, entry.value, match.group(0)))
    return cached_refs


def apply_cached_entry_resolutions(
    resolved_args: dict,
    cached_refs: dict[str, list],
) -> None:
    """Replace {USE_CACHED_ENTRY_N} placeholders in resolved args with cached values.

    Mutates *resolved_args* in place. Handles non-string values via json.dumps fallback.

    Args:
        resolved_args: The deep-copied arguments dict to mutate.
        cached_refs: Output from resolve_cached_entry_refs().
    """
    for key, refs in cached_refs.items():
        val = resolved_args[key]
        for n, entry_value, placeholder_str in refs:
            replacement = entry_value
            if not isinstance(replacement, str):
                try:
                    replacement = json.dumps(replacement)
                except (TypeError, ValueError):
                    replacement = str(replacement)
            val = val.replace(placeholder_str, replacement)
        resolved_args[key] = val


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


def _write_spillover(
    text: str,
    spill_path: Path,
    base_dir: Path,
) -> str:
    """Write text to a spillover file and return its workspace-relative path.

    Args:
        text: Full text content to write.
        spill_path: Absolute path for the spillover file.
        base_dir: Base directory for computing relative path.

    Returns:
        Workspace-relative path string (forward slashes).
    """
    spill_path.parent.mkdir(parents=True, exist_ok=True)
    spill_path.write_text(text, encoding='utf-8')
    try:
        return str(spill_path.relative_to(base_dir)).replace('\\', '/')
    except ValueError:
        return str(spill_path).replace('\\', '/')


def truncate_with_spillover(
    text: str,
    char_limit: int,
    instance_name: str,
    tool_name: str,
    base_dir: Path,
    operation_mode: str = "head",
    spill_path: Optional[Path] = None,
) -> str:
    """Truncate text to char_limit and write full content to a spillover file.

    Returns the truncated text with a truncation notice appended.
    If text is not over the limit, returns it unchanged.

    Args:
        text: The full text to potentially truncate.
        char_limit: Maximum character count before truncating (-1 means no limit).
        instance_name: Agent instance name (for spillover filename).
        tool_name: Tool name (for spillover filename).
        base_dir: Base directory for resolving spillover paths.
        operation_mode: What part of the text to keep:
            - "head": Keep the beginning, drop the end (default, used by grep, read_file).
            - "mid": Keep the beginning and end, drop the middle (used by shell, heartbeats).
            - "tail": Keep the end, drop the beginning.
        spill_path: Optional custom spillover file path. If provided, the full text
            is written there instead of the default auto-generated path.

    Returns:
        Truncated text with notice, or original text if under limit.
    """
    if char_limit == -1 or len(text) <= char_limit:
        return text

    original_len = len(text)

    # Cap output size before writing
    if len(text) > MAX_SPILL_SIZE:
        text = text[:MAX_SPILL_SIZE] + "\n\n[SPILL FILE TRUNCATED — exceeded maximum size]"

    # Resolve spillover path
    if spill_path is not None:
        rel_spill = _write_spillover(text, spill_path, base_dir)
    else:
        log_dir = base_dir / 'logs' / 'spillover'
        log_dir.mkdir(parents=True, exist_ok=True)
        spill_filename = generate_spillover_filename(instance_name, tool_name, log_dir)
        try:
            rel_spill = _write_spillover(text, log_dir / spill_filename, base_dir)
        except (OSError, ValueError):
            rel_spill = f"logs/spillover/{spill_filename}"

    # Apply truncation based on operation_mode
    if operation_mode == "tail":
        truncated = text[-char_limit:]
    elif operation_mode == "mid":
        half = char_limit // 2
        omitted = len(text) - char_limit
        truncated = text[:half] + f"\n\n[... {omitted} chars omitted ...]\n\n" + text[-half:]
    else:
        truncated = text[:char_limit]

    mark_tool_call_truncated(instance_name, tool_name)

    return f"{truncated}\n\n[TRUNCATED — Character limit exceeded. Full output ({original_len} chars) saved to: {rel_spill}]"


def resolve_prev_arg_placeholders(
    tool_args: Any,
    instance_scope: str,
    tool_name: str,
    agent_pool: Any,
    lock: Optional[threading.Lock] = None,
) -> Tuple[Any, Optional[str]]:
    """Resolves {USE_CACHED_ENTRY_N} placeholders using the rolling cache pool.

    This is a shared utility used by both execution_engine.py and ws_handlers.py.
    Works for both streaming and non-streaming tool paths.

    Thread-safety note: When *lock* is provided, cache reads are protected by it.
    Callers that already hold *lock* must pass ``None`` to avoid deadlock.
    When *lock* is ``None``, this function is NOT thread-safe for the read path.

    Args:
        tool_args: Tool arguments (typically a dict after JSON parsing).
                   Non-dict inputs pass through unchanged with no error.
        instance_scope: The instance name scope (e.g., session_name).
        tool_name: Name of the tool being called.
        agent_pool: Reference to the AgentPool for accessing the rolling cache pool.
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

    # Look up instance's cache pool via agent_pool
    inst = None
    for name, i in getattr(agent_pool, 'instance_conversations', {}).items():
        if name == instance_scope:
            inst = i
            break
    cp = getattr(inst, 'cache_pool', None) if inst else None

    # Scan for {USE_CACHED_ENTRY_N} patterns using shared function (avoids regex recompilation + code duplication)
    cached_refs = resolve_cached_entry_refs(tool_args, cp)

    if not cached_refs:
        return tool_args, None

    resolved_args = copy.deepcopy(tool_args)

    # Resolve {USE_CACHED_ENTRY_N} placeholders using shared function (avoids code duplication)
    apply_cached_entry_resolutions(resolved_args, cached_refs)

    return resolved_args, None