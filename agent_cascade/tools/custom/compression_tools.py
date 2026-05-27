"""Compression tool — thin wrapper around the unified compress_context() function.

Previous implementation (338 lines) with _generate_summary(), token counting, discard
calculation, and pool sync logic has been replaced by direct delegation to
agent_cascade.compression.compress_context(). See core.py for full compression logic.
"""
import copy
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

        # Before compressing, ensure the pool has the current state of the conversation.
        # When a sub-agent calls compress_context mid-turn, its local 'messages' list contains
        # messages from this turn that haven't been written back to the pool yet (pool is only
        # updated after the turn ends via conv.extend(final_resp)). Without this sync,
        # compress_context reads stale data from the pool and fails with "Not enough messages".
        if 'messages' in kwargs and agent_name != 'orchestrator' and not dry_run:
            try:
                # Resolve the correct key in instance_conversations (case-insensitive fallback)
                pool_key = agent_name
                if pool_key not in self.agent_pool.instance_conversations:
                    for key in self.agent_pool.instance_conversations:
                        if key.lower() == pool_key.lower():
                            pool_key = key
                            break
                
                # Append only NEW messages that aren't already in the pool.
                # The sub-agent's local 'messages' starts with a sliced working set (already in pool)
                # plus any new tool calls/results from this turn. We must NOT replace the pool.
                pool_list = self.agent_pool.instance_conversations.get(pool_key, [])
                local_messages = kwargs['messages']
                
                if pool_list and local_messages:
                    # The sub-agent's local 'messages' = sliced working set (already in pool) + new tool calls/results.
                    # Build a set of (role, content) signatures from the pool, then append only messages not in it.
                    def _sig(msg):
                        r = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                        c = str(msg.get('content', '')) if isinstance(msg, dict) else str(getattr(msg, 'content', ''))
                        return (r, c)
                    
                    pool_sigs = {sig for sig in map(_sig, pool_list)}
                    new_start_idx = len(local_messages)  # default: nothing is new
                    
                    for i in range(len(local_messages) - 1, -1, -1):
                        if _sig(local_messages[i]) not in pool_sigs:
                            new_start_idx = i
                        else:
                            break
                    
                    # Append only new messages beyond the overlap
                    if new_start_idx < len(local_messages):
                        for msg in local_messages[new_start_idx:]:
                            pool_list.append(copy.deepcopy(msg))
                elif not pool_list and local_messages:
                    # Pool doesn't exist yet — populate it with the local messages
                    self.agent_pool.instance_conversations[pool_key] = copy.deepcopy(local_messages)
            except Exception as e:
                logger.warning(f"Failed to sync messages to pool before compression for '{agent_name}': {e}")

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