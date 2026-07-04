"""Message pool validation utilities.

This module provides standalone validation functions for message pools,
independent of the compression module to avoid circular imports.

Moved from compression/helpers.py in Phase 2 refactoring to break
circular dependency chain between execution_engine and compression modules.
"""
from typing import Any
from agent_cascade.log import logger as compression_logger
from agent_cascade.llm.schema import SYSTEM


def validate_message_pool(messages: list[Any], agent_name: str) -> bool:
    """Validate message pool integrity after compression operations.

    Performs comprehensive validation checks and logs warnings/errors for issues found.
    Returns True only if all critical checks pass; non-critical issues generate warnings.
    
    Checks performed:
      - Pool is not empty (critical)
      - First message is SYSTEM role (warning only, does not fail validation)
      - No excessive duplicate consecutive messages (>10% threshold) (critical)
      - All message roles are valid non-empty strings (critical)
      - No unexpected types (booleans, None, etc.) in the pool (critical)

    Args:
        messages: List of Message objects or dicts to validate
        agent_name: Agent name for logging purposes
        
    Returns:
        True if pool passes all critical validation checks
        False if corruption detected (empty pool, excessive duplicates, invalid roles, or unexpected types)
        
    Note:
        This function logs validation results via compression_logger. First message role
        mismatches generate warnings but do not cause validation to fail.
    """
    # Helper: safe field access for both dict and object-style messages.
    # Returns string values (or default if missing/None/non-string).
    def _get(obj: Any, field: str, default: str = '') -> str:
        if isinstance(obj, dict):
            val = obj.get(field)
        else:
            val = getattr(obj, field, None)
        if not isinstance(val, str):
            return default
        return val

    if not messages:
        compression_logger.error(f"[MSG POOL VALIDATION] Empty message pool for agent '{agent_name}'")
        return False

    # Check first message is SYSTEM
    first_role = _get(messages[0], 'role')
    if first_role != SYSTEM:
        compression_logger.warning(f"[MSG POOL VALIDATION] First message for '{agent_name}' is not SYSTEM (got {first_role})")

    # Check for duplicate consecutive messages (compression can cause this via extend+clear issues)
    prev_content_key = None
    prev_reasoning_key = None
    prev_role = None
    dup_count = 0
    for i, msg in enumerate(messages):
        role = _get(msg, 'role')
        content_key = _get(msg, 'content')[:500]
        reasoning_key = _get(msg, 'reasoning_content')[:500]

        # Assistant messages should not be empty (no content, reasoning, function_call, or tool_calls)
        if role == 'assistant' and not content_key and not reasoning_key:
            fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
            tc = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
            if not fc and not tc:
                dup_count += 1
                compression_logger.warning(f"[MSG POOL VALIDATION] Empty assistant message at index {i} for '{agent_name}' (no function_call/tool_calls)")

        # Check for duplicate consecutive messages (compare role, content, and reasoning)
        if role == prev_role and content_key == prev_content_key and reasoning_key == prev_reasoning_key:
            dup_count += 1
            compression_logger.warning(f"[MSG POOL VALIDATION] Duplicate consecutive msg at index {i} for '{agent_name}'")

        prev_role = role
        prev_content_key = content_key
        prev_reasoning_key = reasoning_key

    # Check: parallel tool calls must have matching function results
    for i, msg in enumerate(messages):
        tool_calls = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
        if isinstance(tool_calls, list) and len(tool_calls) > 1:
            j = i + 1
            actual_results = 0
            while j < len(messages):
                if _get(messages[j], 'role') not in ('function', 'tool'):
                    break
                actual_results += 1
                j += 1
            if actual_results < len(tool_calls):
                dup_count += 1
                compression_logger.warning(
                    f"[MSG POOL VALIDATION] Parallel tool call mismatch at index {i} for '{agent_name}': "
                    f"{len(tool_calls)} tool calls but only {actual_results} function results"
                )

    # Adaptive threshold: at least 3 duplicates or 10% of messages, whichever is higher
    adaptive_threshold = max(3, int(len(messages) * 0.1))
    if len(messages) > 5 and dup_count > adaptive_threshold:
        compression_logger.error(f"[MSG POOL VALIDATION] Excessive duplicates ({dup_count}/{len(messages)}, threshold={adaptive_threshold}) for agent '{agent_name}'")
        return False

    # Check that roles are valid strings (not None or empty after compression)
    invalid_roles = sum(1 for m in messages if not _get(m, 'role'))
    if invalid_roles:
        compression_logger.error(f"[MSG POOL VALIDATION] {invalid_roles} messages with invalid roles for agent '{agent_name}'")
        return False

    # Check for unexpected types in the pool (booleans, None, etc.)
    # These can leak via JSON parsing or logger recovery paths
    unexpected_types = []
    for i, msg in enumerate(messages):
        if isinstance(msg, bool) or msg is None:
            unexpected_types.append((i, type(msg).__name__))
    
    if unexpected_types:
        compression_logger.error(
            f"[MSG POOL VALIDATION] Found {len(unexpected_types)} unexpected types in message pool for '{agent_name}': "
            f"{unexpected_types[:5]}{'...' if len(unexpected_types) > 5 else ''}"
        )
        return False

    return True