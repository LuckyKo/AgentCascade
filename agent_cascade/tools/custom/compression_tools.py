import json
import logging
from typing import List, Union
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA, COMPRESSION_PROMPT
from agent_cascade.llm.schema import SYSTEM, USER, Message, FUNCTION

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
                'maximum': 0.8
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
        fraction = min(params.get('fraction', 0.2), 0.8)
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
        
        # Use current messages from kwargs if available to catch the very latest context,
        # otherwise fallback to the persistent pool.
        history = kwargs.get('messages')
        if not history:
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
            
        from agent_cascade.utils.tokenization_qwen import count_tokens
        from agent_cascade.utils.utils import extract_text_from_message
        
        # Calculate total tokens to find the actual fraction of content to compress
        total_tokens = 0
        token_counts = []
        for msg in messages_to_compress:
            content = extract_text_from_message(msg, add_upload_info=False)
            tokens = count_tokens(content) if content else 0
            token_counts.append(tokens)
            total_tokens += tokens
            
        target_tokens = int(total_tokens * fraction)
        
        tokens_seen = 0
        num_to_summarize = 0
        for count in token_counts:
            tokens_seen += count
            num_to_summarize += 1
            if tokens_seen >= target_tokens and num_to_summarize < len(messages_to_compress) - 1:
                break
                
        # Ensure we compress at least 1 message if possible
        num_to_summarize = max(1, num_to_summarize)
            
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
                agent_obj=agent_obj,
            )
            
            # ALSO explicitly compress the active messages list sent by the LLM
            # (which is a deepcopy in FnCallAgent and won't automatically reflect the AgentPool changes)
            if kwargs.get('messages'):
                active_msgs = kwargs['messages']
                start_idx_active = 0
                
                # Check if first message is SYSTEM
                first_msg = active_msgs[0]
                first_role = first_msg.get('role') if isinstance(first_msg, dict) else getattr(first_msg, 'role', '')
                if first_role == SYSTEM:
                    start_idx_active = 1
                    
                messages_to_compress_active = active_msgs[start_idx_active:]
                
                # Use same token-based calculation for the active list
                total_tokens_active = 0
                token_counts_active = []
                for msg in messages_to_compress_active:
                    content = extract_text_from_message(msg, add_upload_info=False)
                    tokens = count_tokens(content) if content else 0
                    token_counts_active.append(tokens)
                    total_tokens_active += tokens
                    
                target_tokens_active = int(total_tokens_active * fraction)
                
                tokens_seen_active = 0
                num_to_remove_active = 0
                for count in token_counts_active:
                    tokens_seen_active += count
                    num_to_remove_active += 1
                    if tokens_seen_active >= target_tokens_active and num_to_remove_active < len(messages_to_compress_active) - 1:
                        break
                        
                num_to_remove_active = max(1, num_to_remove_active)
                # ADJUSTMENT: Ensure the first remaining message is a safe boundary.
                # Specifically, we scan forward from num_to_remove_active to find a message that is NOT a FUNCTION return.
                # A FUNCTION return cannot exist without its preceding ASSISTANT tool call.
                # However, we must NEVER remove the very last message in the history.
                found_safe = False
                temp_remove = num_to_remove_active
                while temp_remove < len(messages_to_compress_active):
                    next_msg = messages_to_compress_active[temp_remove]
                    role = next_msg.get('role') if isinstance(next_msg, dict) else getattr(next_msg, 'role', '')
                    if role != FUNCTION:
                        found_safe = True
                        num_to_remove_active = temp_remove
                        break
                    temp_remove += 1
                
                # If we didn't find a safe message forward, scan BACKWARD.
                if not found_safe:
                    temp_remove = num_to_remove_active - 1
                    while temp_remove >= 0:
                        next_msg = messages_to_compress_active[temp_remove]
                        role = next_msg.get('role') if isinstance(next_msg, dict) else getattr(next_msg, 'role', '')
                        if role != FUNCTION:
                            found_safe = True
                            num_to_remove_active = temp_remove
                            break
                        temp_remove -= 1
                
                # If STILL none found, don't remove anything to avoid crashes.
                if not found_safe:
                    num_to_remove_active = 0

                if num_to_remove_active > 0:
                    summary_content = (
                        f"--- CONTEXT COMPRESSED ({int(fraction*100)}% of history summarized) ---\n"
                        f"The following is a summary of the conversation context that was removed to save space.\n"
                        f"Summary of previous context:\n"
                        f"<context_summary>\n"
                        f"{summary}\n"
                        f"</context_summary>"
                    )
                    
                    new_active = []
                    if start_idx_active == 1:
                        new_active.append(active_msgs[0])
                        
                    # Insert summary as USER to stay API-compliant (Must start with USER after SYSTEM)
                    if isinstance(active_msgs[0], dict):
                        new_active.append({'role': USER, 'content': str(summary_content)})
                    else:
                        new_active.append(Message(role=USER, content=str(summary_content)))
                    new_active.extend(messages_to_compress_active[num_to_remove_active:])
                    
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
