"""
Agent instance function proxy.

These are NOT called via _call_tool; ExecutionEngine intercepts them as
streaming generators. They exist only so the LLM sees them in the function list.

Schema definitions (CALL_AGENT_SCHEMA, DISMISS_AGENT_SCHEMA) live in prompts/dna.py
as the single source of truth. This module re-exports them for backward compatibility.
"""

from agent_cascade.prompts.dna import CALL_AGENT_SCHEMA, DISMISS_AGENT_SCHEMA
from agent_cascade.tools.base import BaseTool


class _AgentInstanceFunctionProxy(BaseTool):
    """Schema-only proxy for agent instance function registration (call_agent, dismiss_agent).

    This class exists solely so the LLM sees these functions in its tool list
    (via self.function_map[].function). Actual execution is handled by
    ExecutionEngine, which intercepts them before they reach this proxy.

    The .call() method should never be invoked at runtime — if it is, something is wrong.
    """
    def __init__(self, schema: dict):
        self.name = schema['name']
        self.description = schema['description']
        self.parameters = schema['parameters']
        super().__init__()

    def call(self, params=None, **kwargs):
        # Should never be reached — intercepted in ExecutionEngine._process_response
        raise RuntimeError(
            f"Proxy '{self.name}' called directly (should be intercepted by ExecutionEngine)."
        )