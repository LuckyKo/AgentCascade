"""
Agent instance function proxy.

These tools are NOT called via _call_tool; ExecutionEngine intercepts them as
streaming generators. They exist only so the LLM sees them in the function list.

Schema is built from TOOL_METADATA (dna.py) — the single source of truth for all tool definitions.
"""

from agent_cascade.tools.base import BaseTool


def _build_schema_from_metadata(tool_name: str, metadata_entry: dict) -> dict:
    """Convert a TOOL_METADATA entry into a JSON Schema parameters block.

    Handles two formats found in TOOL_METADATA:
      1. Structured params (dict with 'type', 'description', ... keys) — used by call_agent/dismiss_agent
      2. Simple string params ('param_name': 'description') — used by most other tools

    For structured params, the full dict is used as-is.
    For simple string params, a default type of 'string' is inferred.
    """
    raw_params = metadata_entry.get('parameters', {})

    properties = {}
    for key, value in raw_params.items():
        if isinstance(value, dict):
            # Structured: already has 'type', 'description', etc.
            properties[key] = value
        else:
            # Simple string description — infer type as string
            properties[key] = {'type': 'string', 'description': value}

    return {
        'name': tool_name,
        'description': metadata_entry['description'],
        'parameters': {
            'type': 'object',
            'properties': properties,
            'required': metadata_entry.get('required', list(properties.keys())),
        },
    }


class _AgentInstanceFunctionProxy(BaseTool):
    """Schema-only proxy for agent instance function registration (call_agent, dismiss_agent).

    This class exists solely so the LLM sees these functions in its tool list
    (via self.function_map[].function). Actual execution is handled by
    ExecutionEngine, which intercepts them before they reach this proxy.

    The schema is constructed from TOOL_METADATA at import time.

    The .call() method should never be invoked at runtime — if it is, something is wrong.
    """

    # Class-level cache: built once at first access to avoid repeated lookups
    _schema_cache: dict = {}

    def __init__(self, tool_name: str):
        from agent_cascade.prompts.dna import TOOL_METADATA

        # Build or retrieve cached schema from TOOL_METADATA
        if tool_name not in self._schema_cache:
            meta_entry = TOOL_METADATA.get(tool_name)
            if meta_entry is None:
                raise ValueError(
                    f"Tool '{tool_name}' not found in TOOL_METADATA. "
                    f"Add it to agent_cascade/prompts/dna.py."
                )
            self._schema_cache[tool_name] = _build_schema_from_metadata(tool_name, meta_entry)

        schema = self._schema_cache[tool_name]
        self.name = schema['name']
        self.description = schema['description']
        self.parameters = schema['parameters']
        super().__init__()

    def call(self, params=None, **kwargs):
        # Should never be reached — intercepted in ExecutionEngine._process_response
        raise RuntimeError(
            f"Proxy '{self.name}' called directly (should be intercepted by ExecutionEngine)."
        )