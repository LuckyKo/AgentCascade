import json
import logging
from typing import List, Union
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA, COMPRESSION_PROMPT
from agent_cascade.llm.schema import SYSTEM, USER, Message

logger = logging.getLogger(__name__)

@register_tool('compress_context', allow_overwrite=True)
class CompressContext(BaseTool):
    """Tool to propose a summary of the oldest part of the conversation to free up context space."""
    
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
            }
        },
        'required': ['fraction'],
    }
    
    def __init__(self, agent_pool=None, agent_name=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool
        self.agent_name = agent_name
    
    
    def _generate_summary(self, target_messages: List[Union[dict, Message]]) -> str:
        """Internal helper to generate a summary for a list of messages."""
        # Format the messages for the summary prompt
        history_text = ""
        for msg in target_messages:
            role = msg.get('role', 'unknown').upper() if isinstance(msg, dict) else getattr(msg, 'role', 'unknown').upper()
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if isinstance(content, list):
                # Ensure all parts are converted to strings safely, handling None values in ContentItems
                content = " ".join([str(item.get('text', '') or '') if isinstance(item, dict) else str(getattr(item, 'text', None) or item) for item in content])
            history_text += f"{role}: {content}\n\n"
            
        # Call LLM to generate summary
        from agent_cascade.llm import get_chat_model
        import copy
        llm_cfg = copy.deepcopy(self.agent_pool.llm_cfg)
        
        # Ensure timeout is sufficiently high for massive context summaries locally
        if 'generate_cfg' not in llm_cfg:
            llm_cfg['generate_cfg'] = {}
        llm_cfg['generate_cfg']['request_timeout'] = 300  # 5 minutes
        # Don't truncate before summarizing — we already budget tokens ourselves.
        llm_cfg['generate_cfg'].pop('max_input_tokens', None)

        llm = get_chat_model(llm_cfg)
        
        summary_prompt = COMPRESSION_PROMPT.format(history_text=history_text)
        
        summary = ""
        try:
            from agent_cascade.utils.utils import extract_text_from_message
            responses = list(llm.chat([Message(role=USER, content=summary_prompt)]))
            if responses and responses[-1]:
                # The LLM output could be split across multiple message objects in the final yield
                # (e.g., DeepSeek / OAI client yields one for reasoning, one for content)
                summary_parts = []
                for msg_obj in responses[-1]:
                    part = extract_text_from_message(msg_obj, add_upload_info=False)
                    if part.strip():
                        summary_parts.append(part.strip())
                summary = "\n\n".join(summary_parts)
                
                # Fallback: If content is empty but the model provided reasoning_content, use that
                if not summary.strip():
                    for msg_obj in responses[-1]:
                        reasoning = msg_obj.get('reasoning_content', '') if isinstance(msg_obj, dict) else getattr(msg_obj, 'reasoning_content', '')
                        if reasoning.strip():
                            summary = reasoning.strip()
                            break
                        
                # Cleanup common LM Studio meta-commentary
                import re
                summary = re.sub(r'<(think|thought)>.*?</\1>', '', summary, flags=re.IGNORECASE | re.DOTALL)
                summary = re.sub(r'\[(THINK|THOUGHT)\].*?\[/\1\]', '', summary, flags=re.IGNORECASE | re.DOTALL)
                
                summary = summary.strip()
                
                # Strip conversational filler prefixes
                prefixes = ["here is a summary", "here is the summary", "summary:", "in summary,", "here's a summary", "**summary**:"]
                lower_summary = summary.lower()
                for prefix in prefixes:
                    if lower_summary.startswith(prefix):
                        summary = summary[len(prefix):].strip()
                        summary = summary.lstrip(':\n \t')
                        lower_summary = summary.lower() # update for next check
            return summary
        except Exception as e:
            import traceback
            error_msg = f"{e}\n{traceback.format_exc()}"
            logger.error(f"Failed to generate summary: {error_msg}")
            return f"ERROR: Exception occurred while generating summary. Check logs."

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        fraction = min(params.get('fraction', 0.2), 1.0)
        justification = params.get('justification', 'Context management')
        
        if not self.agent_pool:
            return "ERROR: agent_pool not connected to tool"
            
        # Prioritize instance name: 
        # 1. From kwargs (explicitly passed)
        # 2. From agent_obj (the running agent instance)
        # 3. From self.agent_name (the class-level name set during tool registration)
        agent_obj = kwargs.get('agent_obj')
        agent_name = (
            kwargs.get('agent_instance_name') or 
            getattr(agent_obj, 'instance_name', None) or 
            self.agent_name or 
            'orchestrator'
        )
        
        # Always use the full authoritative history from the Pool for index calculations.
        # Using the sliced 'messages' from kwargs would lead to incorrect marker detection.
        history = self.agent_pool.get_conversation(agent_name)
        
        if not history:
            return "ERROR: No conversation history to compress."
            
        # Handle both dicts (from pool) and Message objects (from kwargs)
        start_idx = 0
        first_msg = history[0]
        first_role = first_msg.get('role') if isinstance(first_msg, dict) else getattr(first_msg, 'role', '')
        
        if first_role == SYSTEM:
            start_idx = 1
            
        messages_to_compress = history[start_idx:]
        
        if len(messages_to_compress) < 3:
            return "ERROR: Conversation history too short to safely compress (need at least 3 messages)."
            
        # 1. Identify the 'active set' of messages (those not yet summarized)
        latest_summary_idx = -1
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if isinstance(content, str) and "--- CONTEXT COMPRESSED" in content:
                latest_summary_idx = i
                break
        
        if latest_summary_idx != -1:
            active_set = history[latest_summary_idx + 1:]
        else:
            active_set = history[start_idx:]
            
        if not active_set:
            return "ERROR: No active messages to compress."

        # 2. Calculate how many messages to DISCARD from the active set.
        #    As per your example: 33% compression on 30 messages -> discard 10, keep 20.
        num_to_discard = int(len(active_set) * fraction)
        
        # Safety: ensure we leave at least 2 active messages at the tail for continuity.
        # We also allow num_to_discard to be 0 if the history is too short to keep 2 messages.
        num_to_discard = max(0, min(num_to_discard, len(active_set) - 2))

        # 3. Determine the total count of messages to be included in the new summary.
        #    In a cumulative history, this is (all previous messages) + (newly discarded messages).
        if latest_summary_idx != -1:
            # messages before summary (latest_summary_idx - start_idx) 
            # + the summary itself (1) + new messages (num_to_discard)
            num_to_summarize = (latest_summary_idx - start_idx + 1) + num_to_discard
        else:
            num_to_summarize = num_to_discard
            
        target_messages = messages_to_compress[:num_to_summarize]
        
        # Determine compression mode (default 'auto' = LLM-generated summary)
        mode = params.get('mode', 'auto')

        if mode == 'manual':
            # Manual mode: agent provides its own summary text
            summary = params.get('summary_text', '')
            if not summary or not str(summary).strip():
                return "ERROR: manual mode requires a non-empty 'summary_text' parameter."
        else:
            # Auto mode (default): generate summary via LLM
            precomputed = kwargs.get('precomputed_summary')
            if precomputed:
                summary = precomputed
            else:
                summary = self._generate_summary(target_messages)

        if not summary:
            return "ERROR: Failed to obtain a summary."
            
        if kwargs.get('dry_run'):
            return summary
            
        # Context compression is an internal operation — apply directly
        try:
            agent_obj = kwargs.get('agent_obj')
            self.agent_pool.operation_manager.apply_context_compression(
                agent_name=agent_name,
                summary=summary,
                fraction=fraction,
                num_to_remove=num_to_summarize,
                agent_obj=agent_obj,
            )
            
            # Sync the caller's active messages list to match the Pool's compressed state.
            # The Pool is the single source of truth for what was compressed (BUG-1 fix).
            # The active messages list (a deepcopy in FnCallAgent) won't automatically
            # reflect the AgentPool changes, so we rebuild it from Pool state.
            if kwargs.get('messages'):
                active_msgs = kwargs['messages']
                compressed_pool_history = self.agent_pool.get_conversation(agent_name)
                
                if compressed_pool_history:
                    # Rebuild: use the Pool's compressed state as the new active list.
                    # This guarantees the same boundary/summary as the Pool.
                    new_active = []
                    for msg in compressed_pool_history:
                        if isinstance(active_msgs[0] if active_msgs else None, dict):
                            # Pool stores dicts — use directly
                            if isinstance(msg, dict):
                                new_active.append(msg)
                            else:
                                new_active.append({'role': getattr(msg, 'role', ''), 'content': getattr(msg, 'content', '')})
                        else:
                            # Active list uses Message objects
                            if isinstance(msg, dict):
                                new_active.append(Message(**{k: v for k, v in msg.items() if k in ('role', 'content', 'name', 'function_call', 'reasoning_content')}))
                            else:
                                new_active.append(msg)
                    
                    # Mutate directly so the caller's reference is updated
                    active_msgs.clear()
                    active_msgs.extend(new_active)

            return (
                f"Context compressed ({mode} mode): {int(fraction*100)}% of older history for agent '{agent_name}' has been summarized and removed from your context window.\n\n"
                f"Summary:\n"
                f"<context_summary>\n"
                f"{summary}\n"
                f"</context_summary>"
            )
        except Exception as e:
            return f"ERROR: Compression failed: {str(e)}"

