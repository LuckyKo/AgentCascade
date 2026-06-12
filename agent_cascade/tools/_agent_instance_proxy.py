"""
Agent instance function proxy and schema.

These are NOT called via _call_tool; ExecutionEngine intercepts them as
streaming generators. They exist only so the LLM sees them in the function list.
"""

from agent_cascade.tools.base import BaseTool


# ─── Agent instance function schemas ────────────────────────────────────────────────

CALL_AGENT_SCHEMA = {
    'name': 'call_agent',
    'description': (
        'Delegate a task to a specialized agent instance. '
        'If the instance_name already exists, the session continues with the existing context. '
        'Otherwise, a new session is started using the specified agent_class.\n\n'
        'Example usage:\n'
        '{"name": "call_agent", "arguments": {"agent_class": "coder", "instance_name": "worker1", "task": "Write a script"}}'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'agent_class': {
                'type': 'string',
                'description': 'The class of agent to call (e.g. "coder", "researcher"). Only required when starting a NEW instance.'
            },
            'instance_name': {
                'type': 'string',
                'description': 'A unique name for this agent instance. If this name exists, the existing session is continued regardless of agent_class.'
            },
            'task': {
                'type': 'string',
                'description': 'The task or question to delegate'
            },
            'context': {
                'type': 'string',
                'description': 'Optional background context for the agent instance'
            },
            'log_file': {
                'type': 'string',
                'description': 'Path to a JSONL log file to restore the agent session from before starting. Useful for resuming old sessions. If provided and the instance_name does not already exist in the pool, the session will be loaded from this log file.'
            },
        },
        'required': ['agent_class', 'instance_name', 'task'],
    },
}


class _AgentInstanceFunctionProxy(BaseTool):
    """Schema-only proxy for call_agent function registration.

    This class exists solely so the LLM sees 'call_agent' in its function list
    (via self.function_map[].function). Actual execution is handled by
    ExecutionEngine, which intercepts call_agent before it reaches this proxy.

    The .call() method should never be invoked at runtime — if it is, something is wrong.
    """
    def __init__(self, schema: dict):
        self.name = schema['name']
        self.description = schema['description']
        self.parameters = schema['parameters']
        super().__init__()

    def call(self, params=None, **kwargs):
        # Should never be reached — intercepted in ExecutionEngine._process_response
        return "[SYSTEM ERROR] call_agent should be intercepted by ExecutionEngine, not executed directly."