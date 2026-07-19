"""Shared utility functions for tools."""

import json
from typing import Any, Dict, Union


def parse_tool_params(params: Union[str, Dict[str, Any], None]) -> Dict[str, Any]:
    """Parse tool parameters from a JSON string or dict.

    Args:
        params: A JSON string, a dict, or None.

    Returns:
        Parsed dict. Returns an empty dict on parse failure or invalid input.
    """
    if isinstance(params, str):
        try:
            return json.loads(params) if params.strip() else {}
        except json.JSONDecodeError:
            return {}
    if isinstance(params, dict):
        return params
    return {}