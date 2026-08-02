"""
Instance ID module — validation, retrieval, and path helpers for parallel AC instances.

Allows multiple Agent Cascade instances to run in parallel with isolated logs,
settings, and telemetry data. Empty instance ID = legacy single-instance behavior.
"""

import os
import re
from pathlib import Path

_INSTANCE_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_]+$')
_MAX_INSTANCE_ID_LENGTH = 64


def validate_instance_id(instance_id: str) -> str:
    """Validate and normalize an instance ID string.

    Args:
        instance_id: Raw instance ID from env var or CLI

    Returns:
        Validated, stripped instance ID (empty string if input was empty)

    Raises:
        ValueError: If instance ID contains invalid characters or exceeds max length
    """
    if instance_id is None or not str(instance_id).strip():  # Covers None, empty, whitespace-only
        return ""

    normalized = str(instance_id).strip()

    if not _INSTANCE_ID_PATTERN.match(normalized):
        raise ValueError(
            "Invalid instance ID: must contain only alphanumeric characters and underscores (a-z, A-Z, 0-9, _)"
        )

    if len(normalized) > _MAX_INSTANCE_ID_LENGTH:
        raise ValueError(
            f"Instance ID exceeds maximum length of {_MAX_INSTANCE_ID_LENGTH} characters"
        )

    return normalized


def get_instance_id() -> str:
    """Return the current AC instance ID, or empty string for legacy single-instance mode.

    Reads from AGENT_CASCADE_INSTANCE_ID environment variable.
    The value is validated at startup; this function assumes it's already valid.
    """
    return os.getenv("AGENT_CASCADE_INSTANCE_ID", "")


def get_instance_suffix() -> str:
    """Return '_<instance_id>' suffix for file paths, or empty string if no instance ID."""
    iid = get_instance_id()
    return f"_{iid}" if iid else ""


def make_instance_dir(base_path: str) -> str:
    """Create an instance-specific directory path.

    If instance ID is set, appends it as suffix to the base directory name.
    Example: 'workspace/telemetry' + instance_id='prod' → 'workspace/telemetry_prod'

    Args:
        base_path: Base directory path (e.g., 'workspace/logs')

    Returns:
        Instance-specific directory path or original if no instance ID
    """
    suffix = get_instance_suffix()
    if not suffix:
        return base_path

    p = Path(base_path)
    result = str(p.parent / f"{p.name}{suffix}")
    return result.replace("\\", "/")