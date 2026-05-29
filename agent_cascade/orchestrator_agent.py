"""
OrchestratorAgent backward-compatibility stub.

This class is a backward-compatibility stub. Its methods are NOT called at runtime —
ExecutionEngine handles all execution. OrchestratorAgent._run() and _stream_agent_instance_call()
were never invoked at runtime; they existed as dead code paths alongside the ExecutionEngine.

The compat module provides:
- CALL_AGENT_SCHEMA       — tool schema dict used by agent_factory.register_standard_tools()
- _AgentInstanceFunctionProxy  — proxy class registered in agent.function_map (inherits BaseTool)
- OrchestratorAgent       — stub class matching original constructor signatures for
                            create_agent_from_soul and fallback constructor calls,
                            plus stub methods that tests patch.

TODO: Remove this module entirely once all imports are migrated to the new paths.
"""

from agent_cascade.settings import DEFAULT_MAX_INPUT_TOKENS
from agent_cascade.tools.base import BaseTool


# ─── Agent instance function schemas ────────────────────────────────────────────────
# These are NOT called via _call_tool; ExecutionEngine intercepts them as streaming generators.
# They exist only so the LLM sees them in the function list.

CALL_AGENT_SCHEMA = {
    'name': 'call_agent',
    'description': (
        'Delegate a task to a specialized agent instance. '
        'If the instance_name already exists, the session continues with the existing context. '
        'Otherwise, a new session is started using the specified agent_class.\n\n'
        'Example usage:\n'
        '{"name": "call_agent", "arguments": {"agent_class": "coder", "instance_name": "worker1", "task": "Write a script", "parallel_launch": true}}'
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
            'parallel_launch': {
                'type': 'boolean',
                'description': 'Set to true to run the agent asynchronously in the background. Defaults to false (sequential).'
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
    """
    Proxy for agent instance call_agent function. Inherited from BaseTool so that
    agent.py:209 (self.function_map[].function) works correctly.

    Registered in agent.function_map so the LLM sees it, but actual execution
    is handled by ExecutionEngine (not this proxy). Dead code path.

    Adapted from agent_orchestrator.py for backward compatibility.
    """
    def __init__(self, schema: dict):
        self.name = schema['name']
        self.description = schema['description']
        self.parameters = schema['parameters']
        super().__init__()

    def call(self, params: str, **kwargs) -> str:
        # Dead code path — ExecutionEngine handles this.
        return "[call_agent is intercepted by the orchestrator loop, not executed here]"


class OrchestratorAgent:
    """
    Backward-compatibility stub for OrchestratorAgent.

    This class is a backward-compatibility stub. Its methods are NOT called at runtime —
    ExecutionEngine handles all execution. The original OrchestratorAgent._run() and
    _stream_agent_instance_call() were dead code paths alongside the ExecutionEngine.

    This stub exists to:
    1. Satisfy `create_agent_from_soul(agent_class=OrchestratorAgent, ...)` calls in agent_factory.
    2. Match the fallback constructor signature in load_agent().
    3. Provide stub methods that tests patch via spec=OrchestratorAgent.

    Constructor accepts both signatures:
    - create_agent_from_soul passes agent_pool + llm + name + description + system_message through **kwargs
    - Fallback path passes explicit llm, name, agent_type, description, system_message, function_list

    TODO: Remove this stub once all imports are migrated and tests are updated.
    """

    def __init__(self, agent_pool=None, **kwargs):
        """Accept both create_agent_from_soul and fallback constructor signatures."""
        self.agent_pool = agent_pool
        self.agent_type = kwargs.get('agent_type', 'Orchestrator')
        
        # Initialize attributes that Agent.__init__ would set (code may access these)
        llm_cfg = kwargs.get('llm')
        try:
            from agent_cascade.llm import get_chat_model
            self.llm = get_chat_model(llm_cfg) if llm_cfg else None
        except Exception:
            # If LLM init fails (e.g., missing API key), store raw config
            self.llm = llm_cfg
        
        self.function_map = {}
        self.system_message = kwargs.get('system_message', '')
        self.name = kwargs.get('name')
        self.description = kwargs.get('description', '')

    # ── Stub methods (tests patch these; never called at runtime) ─────────────
    def _get_max_tokens(self) -> int:
        """Stub — ExecutionEngine handles token limits. Returns DEFAULT_MAX_INPUT_TOKENS."""
        return DEFAULT_MAX_INPUT_TOKENS

    def _get_history_tokens(self, messages) -> int:
        """Stub — ExecutionEngine handles token counting."""
        return 0

    def _inject_compression_warning_for_agent(self, agent, instance_name, messages):
        """Stub with actual threshold logic so tests that patch _get_max_tokens/_get_history_tokens pass.
        
        Never called at runtime — ExecutionEngine has its own compression injection.
        """
        # Guard: never compress the compression agent itself (prevents recursion)
        if instance_name == 'compression_agent':
            return False
        
        max_tokens = self._get_max_tokens()
        current_tokens = self._get_history_tokens(messages)
        usage_pct = (current_tokens / max_tokens) * 100 if max_tokens > 0 else 0
        
        # Only trigger compression when genuinely over 95%
        if usage_pct > 95.0:
            compressor = agent.function_map.get('compress_context')
            if compressor:
                compressor.call()
            return True
        
        return False