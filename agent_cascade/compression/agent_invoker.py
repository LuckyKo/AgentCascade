"""Compression Agent invocation wrapper.

Uses the call_agent pattern (via _stream_sub_agent_call) when an orchestrator
reference is available, providing session tracking, WebUI visibility, and
consistent error handling. Falls back to direct comp_agent.run() when called
outside the orchestrator context (e.g., from API server forced compression).
"""
import logging
import time as _time
import copy
from agent_cascade.prompts.dna import COMPRESSION_PROMPT
from agent_cascade.llm.schema import SYSTEM, USER
from agent_cascade.utils.thinking_block import strip_thinking_blocks
from agent_cascade.utils.utils import extract_text_from_message
from agent_cascade.execution_engine import ExecutionEngine

logger = logging.getLogger(__name__)

# Conversational filler prefixes to strip from summaries
_SUMMARY_PREFIXES = [
    "here is a summary", "here is the summary", "summary:",
    "in summary,", "here's a summary", "**summary**:",
]


def _format_messages_for_summary(target_messages):
    """
    Format a list of messages into plain text for the compression prompt.

    Handles both dict and Message objects, including multi-modal content lists.

    Args:
        target_messages: List of messages (dicts or Message objects) to format.

    Returns:
        A single string with role-prefixed message contents.
    """
    history_text = ""
    for msg in target_messages:
        if isinstance(msg, dict):
            role = msg.get('role', 'unknown').upper()
            content = msg.get('content', '')
        else:
            role = getattr(msg, 'role', 'unknown').upper()
            content = getattr(msg, 'content', '')

        # Handle multi-modal content (list of items)
        # Only include text parts; skip images and other non-text items to avoid "None" strings
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get('text', '') or ''
                    if text:
                        text_parts.append(str(text))
                else:
                    text = getattr(item, 'text', None)
                    if text:
                        text_parts.append(str(text))
            content = " ".join(text_parts)
        history_text += f"{role}: {content}\n\n"
    return history_text


def invoke_compression_agent(
    agent_pool,
    target_messages,
    existing_summary=None,
    orchestrator=None,   # Optional: the orchestrator instance (for call_agent pattern)
    caller_name=None,    # Optional: the caller instance name for slot management in fallback path
):
    """
    Invoke the Compression Agent to generate a summary of target messages.

    When an orchestrator is provided and has _stream_sub_agent_call, uses the
    call_agent pattern for session tracking, WebUI visibility, and consistent
    error handling. When no orchestrator is available (e.g., forced compression
    from API server), falls back to direct agent.run().

    Args:
        agent_pool: The AgentPool instance (provides agent loading and state management).
        target_messages: List of messages to summarize.
        existing_summary: Optional previous summary text to compound onto.
        orchestrator: Optional orchestrator instance for call_agent pattern.
        caller_name: Optional caller instance name for slot management. If not provided,
                    attempts to infer from agent_pool (may fallback to 'Orchestrator').

    Returns:
        The raw summary string (with thinking blocks stripped).

    Raises:
        RuntimeError: If the compression agent fails or returns an empty summary.
    """
    if not agent_pool:
        raise RuntimeError("agent_pool not connected")

    # 1. Ensure the compression agent is loaded
    # Agent type identifier: 'Compressor' (renamed from 'compression_agent')
    if not agent_pool.get_agent('Compressor'):
        try:
            agent_pool.load_agent('Compressor')
        except Exception as e:
            raise RuntimeError(f"Could not load Compressor: {e}") from e

    comp_agent = agent_pool.get_agent('Compressor')
    if not comp_agent:
        raise RuntimeError("Compressor is None after loading")

    comp_state_key = 'Compressor'

    # Build the history text for the summary prompt
    history_text = _format_messages_for_summary(target_messages)

    # If there's an existing summary, prepend it as context
    if existing_summary:
        history_text = (
            f"EXISTING SUMMARY:\n{existing_summary}\n\n"
            f"NEW CONVERSATION TO SUMMARIZE:\n{history_text}"
        )

    summary_prompt = COMPRESSION_PROMPT.format(history_text=history_text)
    comp_history = [
        {'role': SYSTEM, 'content': (
            'You are a context compression specialist. Your job is to summarize older '
            'conversation history to free up context space while preserving key '
            'information like decisions, facts, task state, and progress.'
        )},
        {'role': USER, 'content': summary_prompt},
    ]

    summary = ""
    try:
        if orchestrator is not None and hasattr(orchestrator, '_stream_sub_agent_call'):
            # ── call_agent pattern via _stream_sub_agent_call ──
            # Build tool_args matching what call_agent expects
            tool_args = {
                'instance_name': comp_state_key,
                'agent_class': 'Compressor',
                'task': summary_prompt,
                'context': None,
            }
            # Use the orchestrator's _stream_sub_agent_call for full lifecycle management.
            # Pass an empty current_response and manager_history since compression is a
            # self-contained LLM call without tool use or async message injection needs.
            current_response = []
            manager_history = comp_history

            final_msgs = []
            subagent_return_value = None  # Capture StopIteration.value as fallback
            start_time = _time.monotonic()   # Monotonic clock — immune to NTP adjustments
            max_poll_time = 300            # 5-minute timeout for large compression tasks
            poll_count = 0

            try:
                gen = orchestrator._stream_sub_agent_call(
                    'call_agent', tool_args, current_response, manager_history
                )
                # Iterate the generator synchronously (same pattern as yield from).
                # Each LLM streaming chunk produces one yield through the chain:
                #   _original_call_llm → hooked_call_llm → _run → run() → _stream_sub_agent_call
                # For large compression tasks (60K+ tokens of input), 1000+ chunks are common.
                # We use a time-based timeout instead of iteration count to handle any task size.
                while True:
                    yielded = next(gen)
                    poll_count += 1

                    # Time-based check — adapts to any streaming chunk rate
                    elapsed = _time.monotonic() - start_time
                    if elapsed > max_poll_time:
                        raise RuntimeError(
                            f"Compression agent timed out after {elapsed:.0f}s "
                            f"({poll_count} iterations) — further processing may have been incomplete"
                        )

                    # The generator yields intermediate state for WebUI — capture final messages
                    if comp_state_key in agent_pool.sub_agent_state:
                        msgs = agent_pool.sub_agent_state[comp_state_key].get('messages', [])
                        if msgs:
                            final_msgs = list(msgs)
            except StopIteration as e:
                # Normal termination of the generator — capture return value as fallback
                if hasattr(e, 'value') and e.value is not None:
                    subagent_return_value = e.value

        else:
            # ── Fallback: Use engine-based execution via _create_system_agent() ──
            # This provides full AgentInstance lifecycle (state tracking, WebUI visibility, API points)
            logger.info(
                "Compression agent invoked via engine-based execution — "
                "no orchestrator reference for call_agent pattern"
            )

            # Get the caller name for parent tracking and slot management
            # Use provided caller_name if available, otherwise fallback to agent_pool (may return 'Orchestrator')
            if caller_name is None:
                caller_name = getattr(agent_pool, 'session_name', 'Orchestrator')
            
            # Log warning if caller_name couldn't be resolved properly
            if caller_name == 'Orchestrator' and not hasattr(agent_pool, 'session_name'):
                logger.warning(
                    f"[COMPRESSION] Using fallback caller_name='Orchestrator' - "
                    f"slot management may not work correctly. Pass caller_name explicitly."
                )
            
            # Create proper AgentInstance via _create_system_agent() — handles all state setup
            # The system message comes from Compressor_soul.md template, task contains the summary prompt
            engine = ExecutionEngine(agent_pool)
            comp_instance = engine._create_system_agent(
                agent_class='Compressor',
                instance_name=comp_state_key,
                task=summary_prompt,  # Contains history_text and existing_summary context
                caller=caller_name,
            )

            # Configure Compressor settings (similar to Security agent pattern)
            NON_LLM_KEYS = (
                'max_auto_rollbacks', 'auto_rollback_on_loop', 'auto_continue', 
                'max_turns', 'mcpServers', 'work_access_folders', 'seed',
                'read_file_limit', 'grep_char_limit', 'grep_spillover', 'shell_char_limit', 'code_char_limit',
                'disabled_tools',
                'model', 'model_server', 'api_base', 'base_url', 'api_key', 'model_type'
            )
            if hasattr(agent_pool, 'operation_manager'):
                session = getattr(agent_pool.operation_manager, '_current_session', None)
                if session:
                    template = agent_pool.templates.get('Compressor')
                    if template and hasattr(template, 'llm'):
                        cfg = (template.llm.generate_cfg or {}).copy()
                        ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))
                        llm_safe_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
                        cfg.update(llm_safe_cfg)
                        comp_instance._generate_cfg_override = cfg
            
            # Execute via engine.run() — handles LLM call, retries, streaming
            final_msgs = []
            start_time = _time.monotonic()
            max_poll_time = 300  # 5-minute timeout for large compression tasks
            
            try:
                # ── SLOT BYPASS FOR COMPRESSION (Path B — fallback only) ──
                # When forced compression triggers during an agent's turn, the Compressor runs 
                # on the SAME thread as the caller. The caller holds the shared sequential slot.
                # Path A (call_agent via _stream_sub_agent_call) uses normal release/reacquire.
                # Path B (this fallback) skips acquisition entirely — the caller retains the slot.
                
                # Get the caller instance from the pool (for logging/debugging)
                caller_inst = agent_pool.get_instance(caller_name) if caller_name else None
                
                # Set skip flag so engine.run() bypasses slot acquisition
                comp_instance._skip_slot_acquire = True
                logger.debug(
                    f"[COMPRESSION_SLOT_BYPASS] Skipping slot acquire for Compressor - "
                    f"caller={caller_name}, caller_holds_slot={(getattr(caller_inst, '_slot_release', None) is not None) if caller_inst else False}"
                )
                
                for resp in engine.run(comp_instance):
                    elapsed = _time.monotonic() - start_time
                    if elapsed > max_poll_time:
                        raise RuntimeError(
                            f"Compression agent timed out after {elapsed:.0f}s — "
                            f"further processing may have been incomplete"
                        )
                    # Capture conversation for summary extraction (engine manages conversation state)
                    if comp_instance.conversation:
                        final_msgs = list(comp_instance.conversation)
                    
            except Exception as e:
                logger.error(f"Compression agent execution error: {e}")
                raise

        # 2. Extract the summary from the last assistant message
        if final_msgs:
            for msg_obj in reversed(final_msgs):
                role = (msg_obj.get('role', '') if isinstance(msg_obj, dict)
                        else getattr(msg_obj, 'role', ''))
                if role == 'assistant':
                    content = extract_text_from_message(msg_obj, add_upload_info=False)
                    summary = strip_thinking_blocks(content)
                    break

            # Strip conversational filler prefixes
            lower_summary = summary.lower()
            for prefix in _SUMMARY_PREFIXES:
                if lower_summary.startswith(prefix):
                    summary = summary[len(prefix):].strip()
                    summary = summary.lstrip(':\n \t')
                    lower_summary = summary.lower()

        # Fallback: if final_msgs was empty but we got a return value from the generator,
        # try to extract the summary from that string. This handles edge cases where the
        # sub_agent_state wasn't populated during streaming.
        if not summary.strip() and subagent_return_value:
            if isinstance(subagent_return_value, str):
                logger.info("Using subagent return value as fallback for summary extraction")
                rv = subagent_return_value
                # Strip the "[instance_name's output]:\n" wrapper added by _stream_sub_agent_call
                if ']:' in rv and '\n' in rv:
                    rv = rv.split(']:\n', 1)[1]
                summary = strip_thinking_blocks(rv)

        # Validate we got a usable summary
        if not summary.strip():
            raise RuntimeError("Compression Agent returned an empty summary")

        return summary.strip()

    except RuntimeError:
        # Re-raise our own errors as-is
        raise
    except Exception as e:
        raise RuntimeError(f"Exception occurred while generating summary: {e}") from e
    finally:
        # Always clean up compression agent state when done
        if comp_state_key in agent_pool.sub_agent_state:
            agent_pool.sub_agent_state[comp_state_key]['active'] = False
            try:
                agent_pool.active_stack_remove(comp_state_key)
            except Exception:
                pass  # Already removed or never existed - non-critical