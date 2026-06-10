"""Compression tool — thin wrapper around the unified compress_context() function.

Previous implementation (338 lines) with _generate_summary(), token counting, discard
calculation, and pool sync logic has been replaced by direct delegation to
agent_cascade.compression.compress_context(). See core.py for full compression logic.
"""
import logging
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.compression import compress_context, rebuild_working_set

logger = logging.getLogger(__name__)


@register_tool('compress_context', allow_overwrite=True)
class CompressContext(BaseTool):
    """Tool that delegates to the unified compress_context() function."""

    name = 'compress_context'
    description = TOOL_METADATA['compress_context']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'fraction': {
                'type': 'number',
                'description': TOOL_METADATA['compress_context']['parameters']['fraction'],
                'minimum': 0.3,
                'maximum': 1.0
            },
            'mode': {
                'type': 'string',
                'enum': ['auto', 'manual'],
                'description': TOOL_METADATA['compress_context']['parameters']['mode']
            },
            'justification': {
                'type': 'string',
                'description': TOOL_METADATA['compress_context']['parameters']['justification']
            },
            'summary_text': {
                'type': 'string',
                'description': TOOL_METADATA['compress_context']['parameters']['summary_text']
            },
            'force': {
                'type': 'boolean',
                'default': False,
                'description': 'Bypass validation guards (e.g., minimum message count). Used for critical threshold compression.'
            }
        },
        'required': ['fraction'],
    }

    def __init__(self, agent_pool=None, agent_name=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool
        self.agent_name = agent_name

    def call(self, params: str, **kwargs) -> str:
        """Thin wrapper — extract params and delegate to compress_context()."""
        params = self._verify_json_format_args(params)
        fraction = min(params.get('fraction', 0.5), 1.0)
        mode = params.get('mode', 'auto')
        summary_text = params.get('summary_text')
        force = params.get('force', False)
        justification = params.get('justification', 'Context management')

        # Legacy kwargs from /compress command path in orchestrator._run()
        dry_run = kwargs.get('dry_run', False)
        precomputed_summary = kwargs.get('precomputed_summary')

        if not self.agent_pool:
            return "ERROR: agent_pool not connected to tool"

        # Resolve the target agent name from kwargs or fallback
        agent_obj = kwargs.get('agent_obj')
        agent_name = (
            kwargs.get('agent_instance_name') or
            getattr(agent_obj, 'instance_name', None) or
            self.agent_name or
            'orchestrator'
        )

        # Delegate to the unified compress_context function
        # Note: orchestrator param not passed — agent-triggered compression uses
        # the simpler direct run path (comp_agent.run()), which works fine.
        # Forced compression from orchestrator passes orchestrator=self for full lifecycle.
        result = compress_context(
            agent_pool=self.agent_pool,
            target_agent_name=agent_name,
            fraction=fraction,
            mode=mode,
            summary_text=summary_text,
            force=force,
            justification=justification,
            dry_run=dry_run,
            precomputed_summary=precomputed_summary,
        )

        if result.success:
            # Rebuild caller's working set from pool (single source of truth)
            if 'messages' in kwargs and not dry_run:
                rebuild_working_set(kwargs['messages'], self.agent_pool, agent_name)
            
            # For dry runs, return the actual summary text so callers can use it.
            # The /compress command path depends on receiving raw summary for user approval.
            if dry_run:
                return result.summary_text
            
            return (
                f"Context compressed ({mode} mode): "
                f"{result.messages_discarded} messages summarized for '{agent_name}'."
            )
        else:
            return f"ERROR: {result.error}"