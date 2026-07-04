"""Message pool validation utilities.

This module provides standalone validation functions for message pools,
independent of the compression module to avoid circular imports.

Moved from compression/helpers.py in Phase 2 refactoring to break
circular dependency chain between execution_engine and compression modules.
"""
from typing import Any
from agent_cascade.log import logger as compression_logger
from agent_cascade.llm.schema import SYSTEM, USER
from agent_cascade.prompts.dna import COMPRESSION_MARKER


def validate_message_pool(messages: list[Any], agent_name: str) -> bool:
    """Validate message pool integrity after compression operations.

    Performs structural integrity checks. Duplicate detection is the
    loop detector's job — this only checks for corruption.

    Checks performed:
      - Pool is not empty (critical)
      - First message is SYSTEM role (warning only)
      - All message roles are valid non-empty strings (critical)
      - No unexpected types (booleans, None, etc.) in the pool (critical)
      - Compression markers are present and well-formed (warning only)

    Args:
        messages: List of Message objects or dicts to validate
        agent_name: Agent name for logging purposes
        
    Returns:
        True if pool passes all critical validation checks
        False if corruption detected (empty pool, invalid roles, or unexpected types)
        
    Note:
        This function logs validation results via compression_logger.
    """
    # Helper: safe string field access for both dict and object-style messages.
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

    # Check that roles are valid strings (not None or empty after compression)
    invalid_roles = sum(1 for m in messages if not _get(m, 'role'))
    if invalid_roles:
        compression_logger.error(f"[MSG POOL VALIDATION] {invalid_roles} messages with invalid roles for agent '{agent_name}'")
        return False

    # Check for unexpected types in the pool (booleans, None, etc.)
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

    # Check compression markers are present and well-formed
    marker_count = 0
    for i, msg in enumerate(messages):
        role = _get(msg, 'role')
        content = _get(msg, 'content')
        if role == USER and content.startswith(COMPRESSION_MARKER):
            marker_count += 1
            # Check marker has a closing summary tag
            if "<context_summary>" in content and not content.strip().endswith("</context_summary>"):
                compression_logger.warning(
                    f"[MSG POOL VALIDATION] Malformed compression marker at index {i} for '{agent_name}' "
                    f"(missing closing </context_summary>)"
                )

    if marker_count > 0 and marker_count > len(messages) // 2:
        compression_logger.warning(
            f"[MSG POOL VALIDATION] Excessive compression markers ({marker_count}/{len(messages)}) for agent '{agent_name}'"
        )

    return True