"""Compression Agent invocation wrapper.

Uses engine.run() to invoke the Compression Agent via _create_system_agent().
This provides full AgentInstance lifecycle (state tracking, WebUI visibility, API points).
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
    caller_name=None,    # Optional: the caller instance name for slot management
):
    """
    Invoke the Compression Agent to generate a summary of target messages.

    Uses engine.run() via _create_system_agent() for full AgentInstance lifecycle
    (state tracking, WebUI visibility, API points).

    Args:
        agent_pool: The AgentPool instance (provides agent loading and state management).
        target_messages: List of messages to summarize.
        existing_summary: Optional previous summary text to compound onto.
        caller_name: Optional caller instance name for slot management. If not provided,
                     reads agent_pool.session_name (falls back to 'Orchestrator').

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
        # ── Use engine-based execution via _create_system_agent() ──
        # This provides full AgentInstance lifecycle (state tracking, WebUI visibility, API points)
        logger.info(
            "Compression agent invoked via engine-based execution"
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
            # ── SLOT BYPASS FOR COMPRESSION ──
            # When forced compression triggers during an agent's turn, the Compressor runs 
            # on the SAME thread as the caller. The caller holds the shared sequential slot.
            # This path skips acquisition entirely — the caller retains the slot.
            
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

            # Read conversation one final time AFTER the generator completes.
            # The assistant's response is added to instance.conversation in _process_response()
            # (execution_engine.py:1543), which runs after the LLM call but may not trigger
            # another yield if no tools are used. Reading here ensures we capture the complete
            # conversation state including the assistant's final message.
            with comp_instance._compression_lock:
                final_msgs = list(comp_instance.conversation) if comp_instance.conversation else []

            logger.debug(f"[COMPRESSION] final_msgs has {len(final_msgs)} messages, roles: {[m.get('role') if isinstance(m, dict) else getattr(m, 'role', '') for m in final_msgs]}")

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
        if comp_state_key in agent_pool.instance_state:
            with agent_pool._execution._state_lock:
                agent_pool.instance_state[comp_state_key]['active'] = False
                try:
                    agent_pool.active_stack_remove(comp_state_key)
                except Exception:
                    pass  # Already removed or never existed - non-critical