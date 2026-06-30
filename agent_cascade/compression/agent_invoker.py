"""Compression Agent invocation wrapper.

Uses engine.run() to invoke the Compression Agent via _create_system_agent().
This provides full AgentInstance lifecycle (state tracking, WebUI visibility, API points).
"""
import logging
import threading
import time as _time
from agent_cascade.prompts.dna import COMPRESSION_PROMPT
from agent_cascade.llm.schema import SYSTEM, USER
from agent_cascade.utils.thinking_block import strip_thinking_blocks
from agent_cascade.utils.utils import extract_text_from_message, _format_tool_calls_for_text, _reasoning_to_text

# Import shared broadcast helper (replaces duplicated inline broadcast loops)
from agent_cascade.api_integration import broadcast_stream_update

# Lazy import of ExecutionEngine to break circular dependency chain:
# execution_engine.py → compression/handler.py → core.py → agent_invoker.py (→ ExecutionEngine would loop back)
logger = logging.getLogger(__name__)

# Module-level counter for generating unique Compressor instance names.
# Each compression invocation gets a fresh instance name so the logger cache key
# (instance_name, agent_class) is unique — prevents TAIL SYNC DRIFT from reusing
# a cached logger with stale history data from previous compression cycles.
_lock = threading.Lock()

_compressor_invocation_counter = 0

# Conversational filler prefixes to strip from summaries
_SUMMARY_PREFIXES = [
    "here is a summary", "here is the summary", "summary:",
    "in summary,", "here's a summary", "**summary**:",
]


def _is_content_empty(val):
    """Check if content is empty (handles whitespace-only strings and missing values)."""
    if isinstance(val, str):
        return val.strip() == ''
    return not val


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

        # Handle multi-modal content (list of items) — flatten to text string
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

        # Check for reasoning_content even if content is populated (handles str and list types)
        if isinstance(msg, dict):
            rc = msg.get('reasoning_content', '') or ''
        else:
            rc = getattr(msg, 'reasoning_content', '') or ''

        rc_text = _reasoning_to_text(rc)
        if rc_text:
            if not _is_content_empty(content):
                # Prepend reasoning before content for prominence
                content = f"[THOUGHT: {rc_text}]\n{content}"
            else:
                # No content, use reasoning as the text
                content = f"[THOUGHT: {rc_text}]"

        # If content is empty/missing, use shared helper to surface function_call/tool_calls as text
        if _is_content_empty(content):
            tool_text = _format_tool_calls_for_text(msg)
            if tool_text:
                content = tool_text

        history_text += f"{role}: {content}\n\n"
    return history_text


def invoke_compression_agent(
    agent_pool,
    target_messages,
    existing_summary=None,
    caller_name=None,       # Optional: the caller instance name for slot management
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

    # Generate a unique instance name for each compression invocation.
    # This prevents the logger cache from reusing stale history data (TAIL SYNC DRIFT fix).
    with _lock:
        global _compressor_invocation_counter
        _compressor_invocation_counter += 1
        comp_state_key = f'Compressor_{_compressor_invocation_counter}'

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
        from agent_cascade.execution_engine import ExecutionEngine  # Local import to break circular dependency
        engine = ExecutionEngine(agent_pool)
        # initialize() now called automatically in __init__ (Phase 4.5 cleanup)
        comp_instance = engine._create_system_agent(
            agent_class='Compressor',
            instance_name=comp_state_key,
            task=summary_prompt,  # Contains history_text and existing_summary context
            caller=caller_name,
        )

        # Configure Compressor settings using centralized constants and utilities.
        # Note: propagate_settings() already ran inside _create_system_agent(), but we intentionally replace its
        # disabled_tools result with ui_cfg + defense-in-depth defaults to ensure auto-launched agents never get
        # more tools than intended. If session is unavailable, defaults are still applied for safety.
        from agent_cascade.constants import DEFAULT_COMPRESSOR_DISABLED_TOOLS
        from agent_cascade.utils import merge_disabled_tools_for_auto_agent
        
        # Get user's disabled_tools config from the caller agent instance.
        # The caller's _generate_cfg_override contains the user's UI-disabled tools,
        # which were set by _apply_ui_config. We read it directly here to get the
        # original UI settings before propagate_settings() merged them.
        template = agent_pool.get_template('Compressor')
        cfg = (template.llm.generate_cfg or {}).copy() if template and hasattr(template, 'llm') else {}
        
        caller_inst = agent_pool.get_instance(caller_name) if caller_name else None
        ui_disabled_tools = None
        
        if caller_inst and hasattr(caller_inst, '_generate_cfg_override') and caller_inst._generate_cfg_override:
            raw_dt = caller_inst._generate_cfg_override.get('disabled_tools')
            if raw_dt:
                # Could be a dict (per-agent format) or a flat list
                if isinstance(raw_dt, dict):
                    # Look up Compressor-specific disabled tools from per-agent dict.
                    # Try exact match first, then case-insensitive fallback for robustness.
                    ui_disabled_tools = raw_dt.get('Compressor', []) or []
                    if not ui_disabled_tools:
                        for key in raw_dt:
                            if key.lower() == 'compressor':
                                ui_disabled_tools = raw_dt[key] or []
                                break
                elif isinstance(raw_dt, (list, tuple)):
                    # Flat list applies to all agents
                    ui_disabled_tools = list(raw_dt)
        
        # Merge with defense-in-depth defaults
        if ui_disabled_tools:
            merged = merge_disabled_tools_for_auto_agent(
                ui_disabled_tools, 'Compressor', DEFAULT_COMPRESSOR_DISABLED_TOOLS
            )
        else:
            merged = merge_disabled_tools_for_auto_agent(None, 'Compressor', DEFAULT_COMPRESSOR_DISABLED_TOOLS)
        
        cfg['disabled_tools'] = merged
        
        if template and hasattr(template, 'llm'):
            comp_instance._generate_cfg_override = cfg
        else:
            logger.warning(
                "[COMPRESSION] Could not apply defense-in-depth disabled_tools for Compressor — "
                "template not available or missing llm attribute. Agent may have unrestricted tools."
            )
        
        # Execute via engine.run() — handles LLM call, retries, streaming
        final_msgs = []
        start_time = _time.monotonic()
        max_poll_time = 300  # 5-minute timeout for large compression tasks
        
        try:
            # ── SLOT BYPASS FOR COMPRESSION ──
            # When forced compression triggers during an agent's turn, the Compressor runs 
            # on the SAME thread as the caller. The caller holds the shared sequential slot.
            # This path skips acquisition entirely — the caller retains the slot.
            
            # Get the caller instance from the pool (for slot status logging only)
            caller_for_slot = agent_pool.get_instance(caller_name) if caller_name else None
            
            # Set skip flag so engine.run() bypasses slot acquisition
            comp_instance._skip_slot_acquire = True
            logger.debug(
                f"[COMPRESSION_SLOT_BYPASS] Skipping slot acquire for Compressor - "
                f"caller={caller_name}, caller_holds_slot={(getattr(caller_for_slot, '_slot_release', None) is not None) if caller_for_slot else False}"
            )
            
            _last_comp_send = 0.0
            _comp_tick_num = 0
            _comp_last_resp_len = 0
            for resp in engine.run(comp_instance):
                # Check for pool shutdown / generation change
                if agent_pool.stopped:
                    break

                elapsed = _time.monotonic() - start_time
                if elapsed > max_poll_time:
                    raise RuntimeError(
                        f"Compression agent timed out after {elapsed:.0f}s — "
                        f"further processing may have been incomplete"
                    )

                now_comp = _time.monotonic()

                # Unpack (turn_output, is_streaming_tick) from engine.run() yield
                if isinstance(resp, tuple) and len(resp) == 2:
                    comp_turn_output, comp_is_streaming_tick = resp
                else:
                    comp_turn_output, comp_is_streaming_tick = resp, False

                # Use shared broadcast helper (pool attributes _ws_send_queue/_ws_loop are set by caller thread)
                _last_comp_send, _comp_last_resp_len = broadcast_stream_update(
                    pool=agent_pool,
                    instance_name=comp_state_key,
                    turn_output=comp_turn_output,
                    is_streaming_tick=comp_is_streaming_tick,
                    tick_num=_comp_tick_num,
                    now_sec=now_comp,
                    last_send=_last_comp_send,
                    last_resp_len=_comp_last_resp_len,
                )

                _comp_tick_num += 1

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
        with agent_pool._execution._state_lock:
            if comp_state_key in agent_pool.instance_state:
                agent_pool.instance_state[comp_state_key]['active'] = False
                try:
                    agent_pool.active_stack_remove(comp_state_key)
                except Exception:
                    pass  # Already removed or never existed - non-critical