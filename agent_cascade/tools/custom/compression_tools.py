import json
import logging
from typing import List, Union
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA, COMPRESSION_PROMPT
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.utils.thinking_block import strip_thinking_blocks
from agent_cascade.utils.utils import extract_text_from_message

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
        """Internal helper to generate a summary for a list of messages using the Compression Agent."""
        if not self.agent_pool:
            return "ERROR: agent_pool not connected"
            
        # 1. Ensure the compression agent is loaded
        if not self.agent_pool.get_agent('compression_agent'):
            try:
                self.agent_pool.load_agent('compression_agent')
            except Exception as e:
                logger.error(f"Failed to load compression_agent: {e}")
                return f"ERROR: Could not load compression_agent: {e}"
        
        comp_agent = self.agent_pool.get_agent('compression_agent')
        
        # Register compression agent in sub_agent_state so it shows a tab
        comp_state_key = 'compression_agent'
        
        # 2. Format the messages for the summary prompt (moved before init so history is available)
        history_text = ""
        for msg in target_messages:
            role = msg.get('role', 'unknown').upper() if isinstance(msg, dict) else getattr(msg, 'role', 'unknown').upper()
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if isinstance(content, list):
                content = " ".join([str(item.get('text', '') or '') if isinstance(item, dict) else str(getattr(item, 'text', None) or item) for item in content])
            history_text += f"{role}: {content}\n\n"

        summary_prompt = COMPRESSION_PROMPT.format(history_text=history_text)
        history = [
            {'role': SYSTEM, 'content': 'You are a context compression specialist. Your job is to summarize older conversation history to free up context space while preserving key information like decisions, facts, task state, and progress.'},
            {'role': USER, 'content': summary_prompt},
        ]
        
        self.agent_pool.sub_agent_state[comp_state_key] = {
            'active': True,
            'agent_name': f"Compression Agent (compression_agent)",
            'messages': list(history),
        }
        if comp_state_key not in self.agent_pool.active_stack:
            self.agent_pool.active_stack.append(comp_state_key)
        self.agent_pool.instance_conversations[comp_state_key] = list(history)

        # 3. Run the compression agent
        #    We do NOT pass any LLM config overrides here, allowing the agent to follow its 
        #    assigned API Router settings (similar to the Security Advisor fix).
        summary = ""
        try:
            final_msgs = []
            for partial in comp_agent.run(history, agent_instance_name='compression_agent'):
                final_msgs = partial
                # Update sub_agent_state with current message history during streaming
                self.agent_pool.sub_agent_state[comp_state_key]['messages'] = list(history) + (list(final_msgs) if isinstance(final_msgs, list) else [final_msgs])
                self.agent_pool.instance_conversations[comp_state_key] = list(self.agent_pool.sub_agent_state[comp_state_key]['messages'])
            
            if final_msgs:
                # The agent might have multiple assistant messages if it called tools (unlikely for compression)
                # We take the content of the last assistant message.
                for msg_obj in reversed(final_msgs):
                    role = msg_obj.get('role', '') if isinstance(msg_obj, dict) else getattr(msg_obj, 'role', '')
                    if role == 'assistant':
                        content = extract_text_from_message(msg_obj, add_upload_info=False)
                        
                        # Cleanup thinking blocks using shared utility
                        summary = strip_thinking_blocks(content)
                        break
                
                # Strip conversational filler prefixes
                prefixes = ["here is a summary", "here is the summary", "summary:", "in summary,", "here's a summary", "**summary**:"]
                lower_summary = summary.lower()
                for prefix in prefixes:
                    if lower_summary.startswith(prefix):
                        summary = summary[len(prefix):].strip()
                        summary = summary.lstrip(':\n \t')
                        lower_summary = summary.lower()
            
            return summary
        except Exception as e:
            logger.error(f"Failed to generate summary via compression_agent: {e}")
            return f"ERROR: Exception occurred while generating summary: {e}"
        finally:
            # Always clean up compression agent state when done
            if comp_state_key in self.agent_pool.sub_agent_state:
                self.agent_pool.sub_agent_state[comp_state_key]['active'] = False
                if comp_state_key in self.agent_pool.active_stack:
                    self.agent_pool.active_stack.remove(comp_state_key)

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
            
        # 1. Identify the 'active set' of messages (those not yet summarized)
        latest_summary_idx = -1
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if isinstance(content, str) and ("--- CONTEXT COMPRESSED" in content or "<context_summary>" in content):
                latest_summary_idx = i
                break
        
        if latest_summary_idx != -1:
            active_set = history[latest_summary_idx + 1:]
        else:
            active_set = history[start_idx:]
            
        if not active_set:
            return "ERROR: No active messages to compress."

        if len(active_set) < 3:
            return "Context is already optimally compressed; deferring further compression until more messages accumulate."

        # 2. Calculate how many messages to DISCARD from the active set.
        #    We now use a token-aware calculation to ensure the fraction
        #    requested corresponds to actual context space reclaimed.
        from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
        from agent_cascade.utils.utils import extract_text_from_message
        
        total_tokens = 0
        token_counts = []
        for msg in active_set:
            # Simple token estimation for better accuracy than message count
            content = extract_text_from_message(msg if isinstance(msg, Message) else Message(**msg), add_upload_info=True)
            tokens = qwen_count(content)
            token_counts.append(tokens)
            total_tokens += tokens
            
        target_tokens = int(total_tokens * fraction)
        
        tokens_seen = 0
        num_to_discard = 0
        if total_tokens > 0:
            for count in token_counts:
                tokens_seen += count
                num_to_discard += 1
                # Stop once we've reached the target token budget
                if tokens_seen >= target_tokens:
                    break
        else:
            # Fallback to message count if tokens can't be calculated
            num_to_discard = int(len(active_set) * fraction)
        
        # Safety: ensure we leave at least 2 active messages at the tail for continuity.
        # We also allow num_to_discard to be 0 if the history is too short to keep 2 messages.
        num_to_discard = max(0, min(num_to_discard, len(active_set) - 2))

        # 3. Determine the messages to be sent to the Compression Agent for summarization.
        #    To optimize context usage, we surgically send only the messages being 
        #    compressed plus the latest summary as the starting boundary.
        if latest_summary_idx != -1:
            # Part A: The latest summary (context boundary).
            # Part B: The new messages from the active set being discarded.
            target_messages = history[latest_summary_idx : latest_summary_idx + 1 + num_to_discard]
            
            # num_to_summarize remains the contiguous count from start_idx 
            # to be replaced by the new summary in the authoritative history.
            num_to_summarize = (latest_summary_idx - start_idx + 1) + num_to_discard
        else:
            # First compression: just send the messages we are about to compress.
            num_to_summarize = num_to_discard
            target_messages = active_set[:num_to_summarize]
        
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
                    # Use slice_history_for_llm to get the working set (summary + new messages)
                    # rather than the full cumulative pool — this is what actually frees context.
                    if hasattr(self.agent_pool, 'slice_history_for_llm'):
                        working_set = self.agent_pool.slice_history_for_llm(compressed_pool_history)
                    else:
                        working_set = compressed_pool_history

                    # Rebuild: use the sliced working set as the new active list.
                    new_active = []
                    for msg in working_set:
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

