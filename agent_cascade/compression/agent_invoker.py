"""Compression Agent invocation wrapper.

Uses engine.run() to invoke the Compression Agent via _create_system_agent().
This provides full AgentInstance lifecycle (state tracking, WebUI visibility, API points).
"""
import copy
import logging
import threading
import time as _time
from typing import Any, List
from agent_cascade.prompts.dna import COMPRESSION_PROMPT, CONSOLIDATION_PROMPT
from agent_cascade.settings import COMPRESSION_END_MARKER, COMPRESSION_AGENT_TIMEOUT, COMPRESSION_MAX_RETRIES
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, Message
from agent_cascade.utils.thinking_block import strip_thinking_blocks
from agent_cascade.utils.utils import extract_text_from_message, _format_tool_calls_for_text, _reasoning_to_text, _msg_field_or_extra

# Import shared broadcast helper (replaces duplicated inline broadcast loops)
from agent_cascade.api_integration import broadcast_stream_update

# Shared slot-yield helper (three-path yield + pool-holder diagnostic), deduplicated
# from security_handler.py. Depends only on stdlib+logging — no circular import.
from agent_cascade.slot_yield_utils import yield_caller_slot

# Lazy import of ExecutionEngine to break circular dependency chain:
# execution_engine.py → compression/handler.py → core.py → agent_invoker.py (→ ExecutionEngine would loop back)
logger = logging.getLogger(__name__)

# Module-level counter for generating unique Compressor instance names.
# Each compression operation gets a fresh instance name so the logger cache key
# (instance_name, agent_class) is unique — prevents TAIL SYNC DRIFT from reusing
# a cached logger with stale history data from previous compression cycles.
# Within a single compression operation, the same instance is reused across retries
# (conversation reset to initial state before each retry), so no new instances are
# spawned on retryable failures.
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


def _format_messages_for_summary(target_messages: List[Any]) -> str:
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
                    # Include image captions instead of dropping images silently
                    img = item.get('image')
                    if img:
                        caption = item.get('caption')
                        if caption:
                            text_parts.append(f'[Image: {caption}]')
                        else:
                            text_parts.append('[Image]')
                else:
                    text = getattr(item, 'text', None)
                    if text:
                        text_parts.append(str(text))
                    # Include image captions for ContentItem objects
                    if getattr(item, 'image', None):
                        caption = getattr(item, 'caption', None)
                        if caption:
                            text_parts.append(f'[Image: {caption}]')
                        else:
                            text_parts.append('[Image]')
            content = " ".join(text_parts)

        # Check for reasoning_content even if content is populated (handles str and list types)
        # Only process reasoning for assistant messages — matches extract_text_from_message behavior
        rc = _msg_field_or_extra(msg, 'reasoning_content') or ''

        rc_text = _reasoning_to_text(rc)
        if rc_text and role == ASSISTANT.upper():
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


def _configure_compressor_instance(
    agent_pool: Any,
    comp_instance: Any,
    caller_name: str,
) -> None:
    """Configure Compressor instance settings with defense-in-depth disabled_tools.

    Reads the caller's UI-disabled tools config and merges it with defaults.
    Mutates comp_instance._generate_cfg_override in-place.

    Args:
        agent_pool: The AgentPool instance.
        comp_instance: The Compressor AgentInstance to configure.
        caller_name: Name of the calling agent instance.
    """
    from agent_cascade.constants import DEFAULT_COMPRESSOR_DISABLED_TOOLS
    from agent_cascade.utils import merge_disabled_tools_for_auto_agent

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


def _execute_compressor_and_extract_summary(
    agent_pool: Any,
    engine: Any,
    comp_instance: Any,
    comp_state_key: str,
    caller_name: str,
    timeout_label: str = "Compression",
) -> str:
    """Execute compressor via engine.run() with slot bypass and extract summary.

    Shared logic for both compression and consolidation invocations.

    Args:
        agent_pool: The AgentPool instance.
        engine: ExecutionEngine instance.
        comp_instance: The Compressor AgentInstance to execute.
        comp_state_key: Instance name/state key for tracking.
        caller_name: Name of the calling agent instance.
        timeout_label: Label for timeout error messages ("Compression" or "Consolidation").

    Returns:
        The raw summary string (with thinking blocks and marker stripped).

    Raises:
        RuntimeError: If execution fails, times out, or returns invalid output.
    """
    final_msgs = []
    start_time = _time.monotonic()
    max_poll_time = COMPRESSION_AGENT_TIMEOUT

    # Resolve the caller instance BEFORE the try block so it's in scope for the
    # finally-block slot reacquire below. _yielded_slot tracks whether we released
    # the caller's permit — if so, the finally block must re-acquire it (slots are
    # acquired once at engine.run() entry, not per-turn, so without an explicit
    # reacquire the caller stays slotless for the rest of its run()).
    caller_inst = agent_pool.get_instance(caller_name) if caller_name else None
    _yielded_slot = False

    try:
        # Telemetry: track Compressor agent call latency (non-blocking)
        _call_start = _time.perf_counter()

        # ── SLOT YIELD FOR COMPRESSION/CONSOLIDATION ──
        # The Compressor acquires its OWN endpoint slot (no borrowing), so the caller
        # must free its permit first or the compressor deadlocks on the shared
        # sequential slot. Three distinct paths (see slot_yield_utils.yield_caller_slot):
        #   1. Normal yield      — caller holds a live _slot_release callback.
        #   2. Force-release     — callback was cleared but the pool still shows the
        #                          caller holding a permit (leaked/stale state).
        #   3. Skip              — nothing to yield (pool already free); log diagnostic.
        # If we released anything, _yielded_slot is set so the finally block re-acquires.
        _yielded_slot = yield_caller_slot(
            agent_pool, engine, caller_inst, caller_name,
            log_prefix="COMPRESSION_SLOT_YIELD",
            release_reason="before_compression",
            before_action="compression",
        )

        _last_comp_send = 0.0
        _comp_tick_num = 0
        _comp_last_resp_len = 0
        # Bind the generator so we can close it deterministically on early break.
        # Without close(), the suspended run() generator never executes its exit
        # finally block, leaving the instance stuck in RUNNING state; the next
        # re-entry then trips the L1 race guard ([BUG] entered engine.run() in
        # state RUNNING). Same pattern as core.py:691-692 and router.py:717-727.
        gen = engine.run(comp_instance)
        try:
            for resp in gen:
                # Check for pool shutdown / generation change.
                # Raise instead of break: a bare break would leave this invocation's
                # retry loop free to re-invoke the compressor after the user pressed
                # Stop. AgentTerminatedError is a clean abort signal that propagates
                # through compress_context and the fallback-compression handler.
                if agent_pool.stopped:
                    raise AgentTerminatedError(comp_state_key)

                elapsed = _time.monotonic() - start_time
                if elapsed > max_poll_time:
                    raise RuntimeError(
                        f"{timeout_label} agent timed out after {elapsed:.0f}s — "
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

        except AgentTerminatedError:
            raise  # Clean abort signal — propagate to invoker (no retry after Stop)
        except Exception as e:
            logger.error(f"{timeout_label} agent execution error: {e}")
            raise
        finally:
            # Deterministic generator cleanup: close() forces the suspended run()
            # generator to unwind its exit finally block (RUNNING→IDLE transition),
            # so the compressor instance is never left in RUNNING state. Idempotent.
            # Guard with hasattr: engine.run() may be mocked to return a plain
            # iterator (no .close()); only real generators need closing.
            try:
                if hasattr(gen, 'close'):
                    gen.close()
            except RuntimeError:
                pass  # Already closed/exhausted

        # Read conversation one final time AFTER the generator completes.
        # The assistant's response is added to instance.conversation in _process_response()
        # (execution_engine.py:1543), which runs after the LLM call but may not trigger
        # another yield if no tools are used. Reading here ensures we capture the complete
        # conversation state including the assistant's final message.
        with comp_instance._compression_lock:
            final_msgs = list(comp_instance.conversation) if comp_instance.conversation else []

    except Exception as e:
        logger.error(f"{timeout_label} agent execution error: {e}")
        raise
    finally:
        # Telemetry: record Compressor agent instance call (non-blocking, always fires even on timeout/error)
        _call_latency_ms = (_time.perf_counter() - _call_start) * 1000
        if (tel := engine._telemetry()) is not None:
            try:
                tel.record_agent_instance_call(
                    comp_state_key, "Compressor", caller_name, latency_ms=_call_latency_ms,
                )
            except Exception:
                pass

        # ── SLOT REACQUIRE: restore caller's slot if we yielded it ──
        # Runs in the outer finally so it fires whether the inner block succeeded or
        # raised. No KV save/restore here (unlike the compression-HALT path in core.py,
        # which blocks for a long time): this inline compressor call is a single
        # engine.run() that completes normally, and the caller's KV stays resident in
        # RAM during it — same as the security check. Mirrors tool_dispatcher's sync-child
        # yield/reacquire without KV save.
        if _yielded_slot and caller_inst is not None:
            logger.debug(
                f"[COMPRESSION_SLOT_REACQUIRE] Restoring slot for '{caller_name}' after compression"
            )
            engine.reacquire_for(caller_inst, caller_name, context="after_compression")

    # Extract the summary from the last assistant message
    summary = ""
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
        raise RuntimeError(f"{timeout_label} Agent returned an empty summary")

    # Validate compression marker — ensures compressor didn't hallucinate or continue agentic task
    if not summary.strip().endswith(COMPRESSION_END_MARKER):
        raise RuntimeError(
            f"{timeout_label} output missing end marker '{COMPRESSION_END_MARKER}' — "
            f"compressor may have hallucinated or continued the task"
        )

    # Strip the marker from the returned summary (validated above)
    summary = summary.strip()
    summary = summary[:-len(COMPRESSION_END_MARKER)].strip()

    return summary.strip()


def _generate_compressor_instance_name() -> str:
    """Generate a unique instance name for a compressor operation.

    Thread-safe. Each compression/consolidation operation gets a fresh instance name
    so the logger cache key (instance_name, agent_class) is unique — prevents TAIL SYNC
    DRIFT from reusing a cached logger with stale history data. Retries within the same
    operation reuse this single instance (conversation is reset between attempts).

    Returns:
        A unique instance name string like "Compressor_42".
    """
    with _lock:
        global _compressor_invocation_counter
        _compressor_invocation_counter += 1
        return f'Compressor_{_compressor_invocation_counter}'


def _ensure_compressor_loaded(agent_pool) -> None:
    """Ensure the Compressor agent is loaded in the pool.

    Args:
        agent_pool: The AgentPool instance.

    Raises:
        RuntimeError: If the Compressor cannot be loaded or is unavailable.
    """
    if not agent_pool.get_agent('Compressor'):
        try:
            agent_pool.load_agent('Compressor')
        except Exception as e:
            raise RuntimeError(f"Could not load Compressor: {e}") from e

    comp_agent = agent_pool.get_agent('Compressor')
    if not comp_agent:
        raise RuntimeError("Compressor is None after loading")


def invoke_compression_agent(
    agent_pool: Any,
    target_messages: List[Any],
    existing_summary: str | None = None,
    caller_name: str | None = None,
) -> str:
    """
    Invoke the Compression Agent to generate a summary of target messages.

    Uses engine.run() via _create_system_agent() for full AgentInstance lifecycle
    (state tracking, WebUI visibility, API points).

    On retryable validation failures (missing end marker / empty summary), reuses
    the SAME compressor instance and resends the original prompt by resetting the
    conversation back to [system_msg, task_msg]. This avoids spawning a new agent
    instance per attempt while still giving the LLM a clean slate.

    Args:
        agent_pool: The AgentPool instance (provides agent loading and state management).
        target_messages: List of messages to summarize.
        existing_summary: Optional previous summary text to compound onto.
        caller_name: Optional caller instance name for slot management. If not provided,
                     reads agent_pool.session_name (falls back to 'Orchestrator').

    Returns:
        The raw summary string (with thinking blocks stripped).

    Raises:
        RuntimeError: If the compression agent fails or returns an empty summary
                      after exhausting all retry attempts.
    """
    if not agent_pool:
        raise RuntimeError("agent_pool not connected")

    _ensure_compressor_loaded(agent_pool)

    comp_state_key = _generate_compressor_instance_name()

    # Build the history text for the summary prompt
    history_text = _format_messages_for_summary(target_messages)

    # If there's an existing summary, prepend it as context
    if existing_summary:
        history_text = (
            f"EXISTING SUMMARY:\n{existing_summary}\n\n"
            f"NEW CONVERSATION TO SUMMARIZE:\n{history_text}"
        )

    summary_prompt = COMPRESSION_PROMPT.format(history_text=history_text)

    # Get the caller name for parent tracking and slot management
    if caller_name is None:
        caller_name = getattr(agent_pool, 'session_name', 'Orchestrator')

    # Log warning if caller_name couldn't be resolved properly
    if caller_name == 'Orchestrator' and not hasattr(agent_pool, 'session_name'):
        logger.warning(
            f"[COMPRESSION] Using fallback caller_name='Orchestrator' - "
            f"slot management may not work correctly. Pass caller_name explicitly."
        )

    from agent_cascade.execution_engine import ExecutionEngine
    engine = ExecutionEngine(agent_pool)

    # Create the compressor instance ONCE — reused across retries
    comp_instance = engine._create_system_agent(
        agent_class='Compressor',
        instance_name=comp_state_key,
        task=summary_prompt,
        caller=caller_name,
    )

    _configure_compressor_instance(agent_pool, comp_instance, caller_name)

    # Capture the initial conversation state ([system_msg, task_msg]) for retry resets.
    # After engine.run() completes, the conversation will contain the assistant's
    # (bad) response. On retry we reset back to this initial state so the LLM sees
    # a clean [system, user_task] prompt — a true "resend the original prompt" retry.
    # Deep copy ensures message objects are not shared with the live conversation,
    # preventing stale/mutated state (timestamps, cache markers) from leaking into retries.
    with comp_instance._compression_lock:
        initial_conversation = copy.deepcopy(list(comp_instance.conversation))

    max_retries = COMPRESSION_MAX_RETRIES
    if max_retries < 1:
        raise RuntimeError(f"COMPRESSION_MAX_RETRIES must be >= 1, got {max_retries}")

    try:
        logger.info("Compression agent invoked via engine-based execution")

        for attempt in range(1, max_retries + 1):
            # Stop-check before each retry: after a user Stop, do not re-invoke
            # the compressor (each re-entry would trip the L1 race guard if the
            # previous run's generator was abandoned mid-flight). Abort cleanly.
            if agent_pool.stopped:
                raise AgentTerminatedError(comp_state_key)

            try:
                summary = _execute_compressor_and_extract_summary(
                    agent_pool, engine, comp_instance, comp_state_key, caller_name,
                    timeout_label="Compression",
                )
                return summary

            except RuntimeError as e:
                # Only RuntimeError with validation-specific messages are retryable.
                # Non-RuntimeError exceptions (infrastructure, network, etc.) propagate
                # immediately without retry — same behavior as the previous outer loop in core.py.
                err_msg = str(e).lower()
                is_retryable = ('missing end marker' in err_msg or 'empty summary' in err_msg)

                if not is_retryable:
                    # Hard failure (timeout, infra error, etc.) — do not retry
                    raise RuntimeError(f"Compression failed: {e}") from e

                if attempt >= max_retries:
                    logger.error(
                        f"Compression Agent failed after {max_retries} attempts "
                        f"(instance '{comp_state_key}' reused): {e}"
                    )
                    raise RuntimeError(
                        f"Compression failed after {max_retries} attempts: {e}"
                    ) from e

                # Retryable validation failure — reset conversation and retry on same instance
                logger.warning(
                    f"Compression attempt {attempt}/{max_retries} failed: {e} — "
                    f"retrying on same compressor instance '{comp_state_key}'."
                )
                # Reset conversation back to initial [system_msg, task_msg] state.
                # rebuild_conversation() also invalidates message caches so _setup_turn
                # will rebuild the working set from scratch on the next engine.run().
                comp_instance.rebuild_conversation(initial_conversation)

    finally:
        # Always clean up compression agent state when done (runs exactly once).
        # Defensive: guard against missing _execution to avoid masking the original exception.
        try:
            with agent_pool._execution._state_lock:
                if comp_state_key in agent_pool.instance_state:
                    agent_pool.instance_state[comp_state_key]['active'] = False
                    try:
                        agent_pool.active_stack_remove(comp_state_key)
                    except Exception:
                        pass
        except Exception as cleanup_err:
            logger.error(f"Compression cleanup failed for '{comp_state_key}': {cleanup_err}", exc_info=True)


def invoke_consolidation_agent(
    agent_pool: Any,
    marker_summaries: List[str],
    caller_name: str | None = None,
) -> str:
    """Invoke Compressor to consolidate multiple existing summaries into one.

    Same pattern as invoke_compression_agent() but uses CONSOLIDATION_PROMPT with
    numbered summary inputs instead of raw conversation messages.

    Args:
        agent_pool: The AgentPool instance (provides agent loading and state management).
        marker_summaries: List of summary texts extracted from compression markers to consolidate.
        caller_name: Optional caller instance name for slot management. If not provided,
                     reads agent_pool.session_name (falls back to 'Orchestrator').

    Returns:
        The raw consolidated summary string (with thinking blocks stripped).

    Raises:
        RuntimeError: If the consolidation agent fails or returns an empty/invalid summary.
    """
    if not agent_pool:
        raise RuntimeError("agent_pool not connected")

    _ensure_compressor_loaded(agent_pool)

    comp_state_key = _generate_compressor_instance_name()

    # Format input as numbered summaries
    summaries_text = ""
    for i, s in enumerate(marker_summaries, 1):
        summaries_text += f"SUMMARY {i}:\n{s}\n\n"

    consolidation_prompt = CONSOLIDATION_PROMPT.format(summaries_text=summaries_text.strip())

    try:
        logger.info(
            f"Consolidation agent invoked via engine-based execution "
            f"(consolidating {len(marker_summaries)} summaries)"
        )

        # Get the caller name for parent tracking and slot management
        if caller_name is None:
            caller_name = getattr(agent_pool, 'session_name', 'Orchestrator')

        # Create proper AgentInstance via _create_system_agent()
        from agent_cascade.execution_engine import ExecutionEngine
        engine = ExecutionEngine(agent_pool)
        comp_instance = engine._create_system_agent(
            agent_class='Compressor',
            instance_name=comp_state_key,
            task=consolidation_prompt,
            caller=caller_name,
        )

        _configure_compressor_instance(agent_pool, comp_instance, caller_name)

        summary = _execute_compressor_and_extract_summary(
            agent_pool, engine, comp_instance, comp_state_key, caller_name,
            timeout_label="Consolidation",
        )

        return summary

    except Exception as e:
        raise RuntimeError(f"Consolidation failed: {e}") from e
    finally:
        # Always clean up consolidation agent state when done
        with agent_pool._execution._state_lock:
            if comp_state_key in agent_pool.instance_state:
                agent_pool.instance_state[comp_state_key]['active'] = False
                try:
                    agent_pool.active_stack_remove(comp_state_key)
                except Exception:
                    pass
