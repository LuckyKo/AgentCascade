"""Compression Agent invocation wrapper.

Uses engine.run() to invoke the Compression Agent via _create_system_agent().
This provides full AgentInstance lifecycle (state tracking, WebUI visibility, API points).
"""
import copy
import logging
import re
import threading
import time as _time
import uuid
from typing import Any, List, Tuple

from agent_cascade import compressor_reuse
from agent_cascade.agent_instance import AgentState
# Import shared broadcast helper (replaces duplicated inline broadcast loops)
from agent_cascade.api_integration import broadcast_stream_update
from agent_cascade.compression.helpers import format_marker_timestamp
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.llm.schema import ASSISTANT
from agent_cascade.prompts.dna import (COMPACTION_REVIEWER_PROMPT, COMPRESSION_END_MARKER, COMPRESSION_PROMPT,
                                       CONSOLIDATION_PRESERVE_ITEMS_INSTRUCTION, CONSOLIDATION_PROMPT)
from agent_cascade.settings import (COMPACTION_REVIEWER_MAX_TURNS, COMPRESSION_AGENT_TIMEOUT, COMPRESSION_MAX_RETRIES,
                                    COMPRESSION_REVIEW_MAX_RETRIES, COMPRESSOR_AGENT_MAX_TURNS, COMPRESSOR_REUSE_ENABLED,
                                    COMPRESSOR_REVIEWER_WARM_NAME, COMPRESSOR_WARM_NAME)
# Shared slot-yield helper (three-path yield + pool-holder diagnostic), deduplicated
# from security_handler.py. Depends only on stdlib+logging — no circular import.
from agent_cascade.slot_yield_utils import yield_caller_slot
from agent_cascade.utils.streaming import stream_total_deadline
from agent_cascade.utils.thinking_block import (_MARKDOWN_BOLD_RE, _THINK_BLOCK_BRACKET_RE, _THINK_BLOCK_RE,
                                                strip_thinking_blocks)
from agent_cascade.utils.utils import (_format_tool_calls_for_text, _msg_field_or_extra, _reasoning_to_text,
                                       extract_text_from_message)

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

# Separate counter so reviewer launches never collide with compressor instance names in the
# logger cache (instance_name, agent_class). Same lock guards both counters.
_reviewer_invocation_counter = 0

# Conversational filler prefixes to strip from summaries
_SUMMARY_PREFIXES = [
    'here is a summary', 'here is the summary', 'summary:',
    'in summary,', "here's a summary", '**summary**:',
]

# Regex for the optional caption that rides on the SAME line right after the end marker:
#   --- END SUMMARY --- CAPTION: <one short sentence>
# `CAPTION:` is anchored to the start of the tail (after only whitespace), so stray text
# before it is treated as malformed. `[^\n]*` captures a single line only — any trailing
# newline would fail the `\s*$` anchor and be flagged malformed.
_CAPTION_TAIL_RE = re.compile(r'^\s*CAPTION:\s*(?P<caption>[^\n]*)\s*$')

# Reviewer verdict item list: ``[LOSS: itemA, itemB, ...]``. Non-greedy up to the first ``]`` so a
# stray bracket inside an item label cannot swallow the rest of the line. Used by
# _parse_compaction_verdict; the leading ``[LOSS`` is matched case-insensitively.
_REVIEWER_LOSS_RE = re.compile(r'\[LOSS:\s*(?P<items>[^\]]*)\]', re.IGNORECASE)


def _parse_compression_output(raw: str) -> Tuple[str, str]:
    """Split raw compressor output into (summary_body, caption).

    The compressor is instructed to terminate its summary with the end marker
    ``COMPRESSION_END_MARKER`` and, on the SAME line right after it, an optional
    one-line caption of the form `` CAPTION: <text>``. This helper parses that tail
    out so the caption can be stored in metadata WITHOUT leaking into the summary
    body (which is what goes back into model context).

    Rules:
      - The summary body is everything BEFORE the LAST occurrence of the end marker,
        stripped. Safe whether or not a caption was present.
      - An empty summary body (nothing before the marker) raises RuntimeError so the
        existing retry/validation path still fires.
      - A missing end marker raises RuntimeError (same as the old ``endswith`` check).
      - The tail after the marker:
          * `` CAPTION: <text>``  -> caption = <text>.strip() (single line; multi-line
            input is rejected and treated as malformed, never leaked into the body).
          * empty/whitespace-only -> caption = "" (backward compatible with old output).
          * anything else (stray text) -> warning + caption = "" (never leaks into body).

    Returns:
        (summary_body, caption) where both are stripped strings.

    Raises:
        RuntimeError: If the end marker is missing or the summary body is empty.
    """
    raw_stripped = raw.strip()
    marker_idx = raw_stripped.rfind(COMPRESSION_END_MARKER)
    if marker_idx == -1:
        raise RuntimeError(
            f"output missing end marker '{COMPRESSION_END_MARKER}' — "
            f"compressor may have hallucinated or continued the task"
        )

    summary_body = raw_stripped[:marker_idx].strip()
    # Defensive fallback: if the LAST marker leaves another copy of the marker in the body,
    # the model echoed the marker string (e.g. the summarized content itself contained
    # "--- END SUMMARY ---", or it leaked into the caption). Re-anchor on the FIRST marker so
    # the summary body is guaranteed marker-free — a stray marker in the body would otherwise
    # be mis-detected as a compression marker later. Rare but cheap to guard, and tested
    # (see tests/test_session_caption.py); the normal single-marker case is unaffected.
    if COMPRESSION_END_MARKER in summary_body:
        first_idx = raw_stripped.find(COMPRESSION_END_MARKER)
        alt_body = raw_stripped[:first_idx].strip()
        if alt_body and COMPRESSION_END_MARKER not in alt_body:
            marker_idx = first_idx
            summary_body = alt_body

    if not summary_body:
        raise RuntimeError('Agent returned an empty summary')

    tail = raw_stripped[marker_idx + len(COMPRESSION_END_MARKER):]
    m = _CAPTION_TAIL_RE.match(tail)
    if m is not None:
        caption = m.group('caption').strip()
    elif tail.strip():
        # Non-empty trailing text that is not a well-formed CAPTION: line.
        logger.warning(
            f"Malformed caption after end marker (ignored, not leaked into summary): "
            f"'{tail.strip()[:80]}'"
        )
        caption = ''
    else:
        caption = ''

    return summary_body, caption


def _parse_compaction_verdict(raw: str) -> tuple[bool, list[str]]:
    """Parse the compaction reviewer's [PASS] / [LOSS: items] verdict.

    Mirrors the multi-fallback discipline of security_handler._parse_verdict: it normalizes
    thinking blocks and the [END] token, strips markdown bold, then picks the verdict token that
    appears LAST in the text (the reviewer is instructed to put the verdict on its final line).

    The caller passes the reviewer's summary body — the COMPRESSION_END_MARKER is already stripped
    upstream — so this parser only looks at the verdict tokens.

    Ambiguous / unparseable input resolves to PASS (fail-open): a reviewer that cannot decide must
    never trigger a retry-storm or block compaction. A ``[LOSS`` token with no parseable bracketed
    item list is likewise treated as ambiguous → PASS.

    Returns:
        (is_pass, missing_items) where missing_items is a list of short item labels, empty on PASS.
    """
    text = raw or ''
    # Defensive: the shipped caller (_execute_compressor_and_extract_summary) already strips thinking blocks and [END]; re-striping here keeps the parser correct if a future raw-text caller feeds it unprocessed model output.
    # Strip thinking blocks before parsing (same guards as _parse_verdict).
    try:
        if '<think' in text.lower() or '<thought' in text.lower():
            text = _THINK_BLOCK_RE.sub('', text)
        if '[think' in text.lower() or '[thought' in text.lower():
            text = _THINK_BLOCK_BRACKET_RE.sub('', text)
    except Exception as e:  # Defensive: a parser failure must never escalate to a compaction failure.
        logger.debug(f"Thinking-block stripping in compaction verdict failed (non-critical): {e}")

    text = re.sub(r'\s*\[END\]\s*', ' ', text, flags=re.IGNORECASE)
    text = _MARKDOWN_BOLD_RE.sub('', text)  # **[LOSS: a, b]** -> [LOSS: a, b]

    upper = text.upper()
    pass_pos = upper.rfind('[PASS')
    loss_pos = upper.rfind('[LOSS')

    if loss_pos > pass_pos and loss_pos != -1:
        m = _REVIEWER_LOSS_RE.search(text, loss_pos)
        if m is not None:
            items = [it.strip() for it in m.group('items').split(',') if it.strip()]
            if items:
                return False, items
        # A [LOSS token that carries no bracketed item list is ambiguous — fail-open to PASS.
        logger.debug(f"Malformed [LOSS] verdict (no parseable item list) — treating as PASS: {text[loss_pos:loss_pos + 80]!r}")
        return True, []

    if pass_pos != -1:
        return True, []

    # No verdict token at all → ambiguous → PASS (fail-open).
    logger.debug('Compaction reviewer produced no recognizable verdict token — treating as PASS (fail-open)')
    return True, []


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
    history_text = ''
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
            content = ' '.join(text_parts)

        # Check for reasoning_content even if content is populated (handles str and list types)
        # Only process reasoning for assistant messages — matches extract_text_from_message behavior
        # NOTE: provider encrypted-reasoning blobs (Message.reasoning_blocks) are deliberately
        # NOT folded here — they are opaque/encrypted and unsummarizable. Kept (post-compression)
        # messages retain their reasoning_blocks field, so the echo still works for the surviving
        # tail; blobs on the summarized portion drop with it. Consequence: the reasoning chain
        # RESTARTS at the compression boundary — acceptable, compression already discards exact
        # history and matches the provider's "starts from scratch" fallback (no hard error).
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
    """Configure Compressor instance settings from the caller's UI-disabled tools.

    Passes through the caller's UI-disabled tools if present; otherwise leaves disabled_tools
    unset so the resolver applies its safe baseline at runtime (no hardcoded merge — the UI is
    the single source of truth, seeded by the one-time class-defaults migration).
    Mutates comp_instance._generate_cfg_override in-place.

    Args:
        agent_pool: The AgentPool instance.
        comp_instance: The Compressor AgentInstance to configure.
        caller_name: Name of the calling agent instance.
    """
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

    # No hardcoded merge — UI config is authoritative (seeded by the one-time migration).
    # Pass through the caller's UI-disabled tools if present; otherwise leave unset so the
    # resolver applies the Layer 4 safe baseline at runtime. Compressor keeps shell_cmd available
    # in restricted mode (the migration seeds its config without shell_cmd).
    if ui_disabled_tools:
        cfg['disabled_tools'] = ui_disabled_tools

    if template and hasattr(template, 'llm'):
        comp_instance._generate_cfg_override = cfg
    else:
        logger.warning(
            '[COMPRESSION] Could not apply defense-in-depth disabled_tools for Compressor — '
            'template not available or missing llm attribute. Agent may have unrestricted tools.'
        )


def _reset_terminated_compressor_for_retry(agent_pool: Any, comp_instance: Any, comp_state_key: str) -> None:
    """Reset a loop-terminated compressor back to IDLE so the retry can re-run it.

    When the compressor's loop-detection exhausts its rollback budget it is terminated
    (state -> TERMINATED, ``is_terminated=True``, added to ``pool.terminated_instances``).
    The run() exit-finally in engine/core.py does NOT transition a TERMINATED instance
    back to IDLE — it only logs "already TERMINATED". Reusing that instance on the next
    retry therefore trips the L1 race guard (engine.run() requires state == IDLE) and
    aborts, so every subsequent compression operation fails -> outer fallback-compression
    retry storm.

    This helper clears exactly that stale termination signal — scoped to the compression
    retry path only (never applied globally). It does NOT touch the L1 race guard, which
    still protects real races for all other callers. Safe because:
      - The compressor is a single-shot system agent with no children (its tool set is
        restricted), so there is no cascade-termination state to unwind.
      - No engine.run() thread is in flight at call time (we are between attempts), so
        the TERMINATED->IDLE transition cannot race against an active run.

    Args:
        agent_pool: The AgentPool instance (provides ``terminated_instances``).
        comp_instance: The compressor AgentInstance to reset.
        comp_state_key: Instance name for logging and pool bookkeeping.
    """
    with comp_instance._state_lock:
        if comp_instance.state != AgentState.TERMINATED:
            return  # Only the loop-terminated case needs recovery; leave everything else alone.
        comp_instance.is_terminated = False
        # TERMINATED has no valid transitions out in the state matrix (terminal by design),
        # so set directly under the lock rather than via _transition(). IDLE is a valid
        # entry state for engine.run(), which then transitions IDLE -> RUNNING.
        comp_instance.state = AgentState.IDLE

    # Remove the stale termination signal from the pool so status checks / idle-manager
    # no longer treat this instance as terminated. Scoped to compression path only.
    with agent_pool._pool_lock:
        agent_pool.terminated_instances.discard(comp_state_key)

    logger.warning(
        f"[COMPRESSION] Resetting loop-terminated compressor '{comp_state_key}' "
        f"TERMINATED->IDLE for retry (BUG_0001)."
    )


def _execute_compressor_and_extract_summary(
    agent_pool: Any,
    engine: Any,
    comp_instance: Any,
    comp_state_key: str,
    caller_name: str,
    timeout_label: str = 'Compression',
    agent_class: str = 'Compressor',
) -> Tuple[str, str]:
    """Execute compressor via engine.run() with slot bypass and extract summary.

    Shared logic for both compression and consolidation invocations.

    Args:
        agent_pool: The AgentPool instance.
        engine: ExecutionEngine instance.
        comp_instance: The Compressor AgentInstance to execute.
        comp_state_key: Instance name/state key for tracking.
        caller_name: Name of the calling agent instance.
        timeout_label: Label for timeout error messages ("Compression" or "Consolidation").
        agent_class: Agent class for the telemetry call record (defaults to 'Compressor'; the
            compaction reviewer also passes 'Compressor' — it is a separate instance, not a soul).
            Run mechanics are identical.

    Returns:
        A 2-tuple ``(summary, caption)`` — summary with thinking blocks and end marker
        stripped (safe for model context), plus the optional same-line caption.

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
        # NOTE: NO save_fn is passed here, deliberately. Compression/consolidation
        # REWRITES the caller's context (summarizes/discards history) on success, so a
        # pre-action KV save would be stale the moment the summary lands — dead weight.
        # save_fn defaults to None (slot_yield_utils.yield_caller_slot), so no save runs;
        # the consume-once gate in engine/slot_lifecycle.reacquire_after_slot_yield
        # (_state_label is not None) then makes the finally-side restore a no-op
        # automatically, while the FIFO slot re-acquire and DMS re-stamp still run.
        # The Security-check yield (context unchanged) is the path that legitimately keeps
        # its save_fn — see security_handler.py.
        _yielded_slot = yield_caller_slot(
            agent_pool, engine, caller_inst, caller_name,
            log_prefix='COMPRESSION_SLOT_YIELD',
            release_reason='before_compression',
            before_action='compression',
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
        # BUG_0344 (Fix A): scope this invocation's wall-clock deadline onto the
        # stream total-duration guard so a stall blocked on the FIRST token is cut
        # off inside the compressor budget, not after the 900s stream default. The
        # yield-gated timeout check below stays as a secondary guard (its message
        # contract is preserved for already-yielding streams).
        with stream_total_deadline(start_time + max_poll_time):
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
                    comp_state_key, agent_class, caller_name, latency_ms=_call_latency_ms,
                )
            except Exception:
                pass

        # ── SLOT REACQUIRE: restore caller's slot if we yielded it ──
        # Runs in the outer finally so it fires whether the inner block succeeded or
        # raised. NOTE: NO KV save was made on this path (see the yield above) because
        # compression rewrites the caller's context, so the pre-action KV would be stale.
        # reacquire_after_slot_yield still re-acquires the FIFO slot and re-stamps DMS;
        # its consume-once gate (_state_label is not None) then self-skips the KV restore
        # with a debug log — no HTTP, keeping the warm KV cache. tolerate_failure=True: the
        # compression caller is mid-turn and must continue even if re-acquisition fails.
        if _yielded_slot and caller_inst is not None:
            logger.debug(
                f"[COMPRESSION_SLOT_REACQUIRE] Restoring slot for '{caller_name}' after compression"
            )
            engine.reacquire_after_slot_yield(
                caller_inst, caller_name, context='after_compression', tolerate_failure=True)

    # Extract the summary from the last assistant message.
    # BUG_0203 (F1): skip engine error strings that the Compressor commits as its own
    # assistant message (a failed LLM turn surfaces "[SYSTEM ERROR: …]" as the last
    # committed message). Feeding those to _parse_compression_output
    # mis-classifies the failure as retryable (a "missing end marker" / "empty summary"
    # format error) and re-sends the same oversized payload every round.
    # ([RETRYING] messages are transient — yielded to the UI but not committed to
    # history — so they never appear in the conversation and need no skip clause.)
    summary = ''
    llm_error_surfaced = False
    if final_msgs:
        for msg_obj in reversed(final_msgs):
            role = (msg_obj.get('role', '') if isinstance(msg_obj, dict)
                    else getattr(msg_obj, 'role', ''))
            if role != 'assistant':
                continue
            content = strip_thinking_blocks(
                extract_text_from_message(msg_obj, add_upload_info=False)).strip()
            if not content:
                continue  # empty assistant message — keep scanning for a real summary
            if content.startswith('[SYSTEM ERROR'):
                # Engine error surfaced as an assistant message — not a usable summary.
                # Keep scanning; if no usable message remains, this is the terminal case.
                llm_error_surfaced = True
                continue
            summary = content
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
        if llm_error_surfaced:
            # BUG_0203 (F1): the LLM produced no output — the engine surfaced an error
            # string as the last assistant message. This is NON-retryable on the same
            # endpoint (the payload is unchanged), so classify it distinctly to avoid
            # re-sending the same oversized payload every round.
            logger.warning(
                f"[COMPRESSION_LLM_FAILURE] {timeout_label} LLM produced no output "
                f"(engine error surfaced as assistant message); not retryable on the same endpoint")
            raise RuntimeError(
                f"{timeout_label} LLM produced no output (engine error surfaced as assistant "
                f"message); not retryable on the same endpoint")
        raise RuntimeError(f"{timeout_label} Agent returned an empty summary")

    # Parse + strip the end marker (and optional same-line caption) in one pass.
    # This ensures compressor didn't hallucinate or continue agentic task, and that the
    # caption — which must NEVER appear in the <context_summary> body sent back to the
    # model — is captured separately for metadata storage. Raises RuntimeError on a
    # missing marker / empty summary so the retry path still fires.
    summary_body, caption = _parse_compression_output(summary)

    return summary_body, caption


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
        raise RuntimeError('Compressor is None after loading')


def invoke_compression_agent(
    agent_pool: Any,
    target_messages: List[Any],
    existing_summary: str | None = None,
    caller_name: str | None = None,
    want_caption: bool = False,
    keep_warm_session: bool = False,
) -> Tuple[str, str] | Tuple[str, str, Any]:
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
        A 2-tuple ``(summary, caption)`` where ``summary`` is the cleaned summary body
        (thinking blocks stripped, end marker removed — safe to put back into model
        context) and ``caption`` is the optional one-line session caption parsed from
        the same line after the end marker ("" if absent/malformed).

        When ``keep_warm_session`` is True AND the warm fixed-name instance actually served
        the request, returns a 3-tuple ``(summary, caption, session)`` where ``session`` is
        the still-held ``WarmCompressorSession``. Claim ownership transfers to the caller (the
        finally below does NOT release it — the caller owns the single release). Otherwise the
        return stays a plain 2-tuple; the final element is never present, so existing 2-tuple
        callers are unaffected.

    Raises:
        RuntimeError: If the compression agent fails or returns an empty summary
                      after exhausting all retry attempts.
    """
    if not agent_pool:
        raise RuntimeError('agent_pool not connected')

    _ensure_compressor_loaded(agent_pool)

    comp_state_key = _generate_compressor_instance_name()

    # BUG_0330: with warm reuse ON, the fixed-name warm Compressor is tried first and the fresh
    # per-op name above becomes the fallback (and the instance_state/finally key is re-pointed to
    # whichever name was actually used). With the flag OFF, reuse_name is None → the warm path is
    # never attempted and behavior is identical to today.
    reuse_name = COMPRESSOR_WARM_NAME if COMPRESSOR_REUSE_ENABLED else None
    rid = uuid.uuid4().hex

    # Build the history text for the summary prompt
    history_text = _format_messages_for_summary(target_messages)

    # If there's an existing summary, prepend it as context
    if existing_summary:
        history_text = (
            f"EXISTING SUMMARY:\n{existing_summary}\n\n"
            f"NEW CONVERSATION TO SUMMARIZE:\n{history_text}"
        )

    # Build the end instruction: marker always, caption only when requested.
    from agent_cascade.prompts.dna import CAPTION_INSTRUCTION, END_MARKER_INSTRUCTION
    end_instruction = ' ' + END_MARKER_INSTRUCTION + (CAPTION_INSTRUCTION if want_caption else '')

    summary_prompt = COMPRESSION_PROMPT.format(history_text=history_text, end_instruction=end_instruction)

    # Get the caller name for parent tracking and slot management
    if caller_name is None:
        caller_name = getattr(agent_pool, 'session_name', None) or 'Orchestrator'

    # Log warning if caller_name couldn't be resolved properly
    if caller_name == 'Orchestrator' and not getattr(agent_pool, 'session_name', None):
        logger.warning(
            f"[COMPRESSION] Using fallback caller_name='Orchestrator' - "
            f"slot management may not work correctly. Pass caller_name explicitly."
        )

    from agent_cascade.execution_engine import ExecutionEngine
    engine = ExecutionEngine(agent_pool)

    max_retries = COMPRESSION_MAX_RETRIES
    if max_retries < 1:
        raise RuntimeError(f"COMPRESSION_MAX_RETRIES must be >= 1, got {max_retries}")

    # Set ONLY when keep_warm_session is requested AND the warm fixed-name instance served the
    # request (success path); the finally then suppresses its release because claim ownership
    # has transferred to the caller. Stays None on every failure/fresh-spawn path → released as usual.
    _l1_session = None

    try:
        logger.info('Compression agent invoked via engine-based execution')

        # Acquire the compressor ONCE — reused across retries. Warm fixed-name instance when
        # reuse is enabled, fresh per-op spawn otherwise. This lives INSIDE the try so the
        # finally below always releases the warm claim (the engine hands off ownership on a
        # successful warm acquire).
        comp_instance, _reused, comp_state_key = compressor_reuse.acquire_compressor_agent(
            engine, 'Compressor', reuse_name, comp_state_key, summary_prompt, caller_name, rid)

        _configure_compressor_instance(agent_pool, comp_instance, caller_name)

        # Bound the system-launched Compressor with its own turn budget (decoupled from the caller's limit).
        # Re-applied unconditionally: the warm-hit reset nulls max_turns/_generate_cfg_override.
        comp_instance.max_turns = COMPRESSOR_AGENT_MAX_TURNS

        # Capture the initial conversation state ([system_msg, task_msg]) for retry resets.
        # After engine.run() completes, the conversation will contain the assistant's
        # (bad) response. On retry we reset back to this initial state so the LLM sees
        # a clean [system, user_task] prompt — a true "resend the original prompt" retry.
        # Deep copy ensures message objects are not shared with the live conversation,
        # preventing stale/mutated state (timestamps, cache markers) from leaking into retries.
        with comp_instance._compression_lock:
            initial_conversation = copy.deepcopy(list(comp_instance.conversation))

        for attempt in range(1, max_retries + 1):
            # Stop-check before each retry: after a user Stop, do not re-invoke
            # the compressor (each re-entry would trip the L1 race guard if the
            # previous run's generator was abandoned mid-flight). Abort cleanly.
            if agent_pool.stopped:
                raise AgentTerminatedError(comp_state_key)

            try:
                summary, caption = _execute_compressor_and_extract_summary(
                    agent_pool, engine, comp_instance, comp_state_key, caller_name,
                    timeout_label='Compression',
                )
                # Warm-session hand-off: when keep_warm_session was requested AND the warm
                # fixed-name instance (not the fresh per-op fallback) served the request, return
                # the still-held session so the caller can thread L2 onto this instance's warm
                # KV prefix instead of re-acquiring. The claim transfers to the caller; the
                # finally below skips its release. Any other path stays a plain 2-tuple.
                if (keep_warm_session and reuse_name is not None
                        and comp_state_key == COMPRESSOR_WARM_NAME):
                    from agent_cascade.compressor_reuse import WarmCompressorSession
                    _l1_session = WarmCompressorSession(
                        comp_instance, comp_state_key, rid, engine, agent_pool, caller_name)
                    return summary, caption, _l1_session
                return summary, caption

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
                # BUG_0001 FIX: if the previous attempt loop-terminated the compressor,
                # it is still TERMINATED (the run() exit-finally does not reset it), and
                # re-running it would trip the L1 race guard. Reset it to IDLE first so
                # the retry can actually execute. No-op unless state == TERMINATED.
                _reset_terminated_compressor_for_retry(agent_pool, comp_instance, comp_state_key)

                # Reset conversation back to initial [system_msg, task_msg] state.
                # rebuild_conversation() also invalidates message caches so _setup_turn
                # will rebuild the working set from scratch on the next engine.run().
                comp_instance.rebuild_conversation(initial_conversation)

                # BUG_0203 (F2b): advance the endpoint cursor between compression retry rounds
                # so each round tries a DIFFERENT endpoint — a down class-pinned `compressor`
                # endpoint does not block all rounds. No-op if the router is absent/mocked.
                _router = getattr(agent_pool, 'api_router', None)
                if _router is not None:
                    _router.advance_instance_endpoint(comp_state_key)

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
        # Release the warm claim LAST, after instance_state cleanup (BUG_0330). rid-guarded
        # no-op when the flag was OFF or the acquire fell back to a fresh spawn. When a live
        # WarmCompressorSession was handed to the caller, claim ownership transferred to it (it
        # owns the single release) — suppress here so the claim is released exactly once.
        if _l1_session is None:
            compressor_reuse.release_claim(reuse_name, rid)


def _format_summaries_with_spans(
    marker_summaries: List[str],
    marker_time_ranges: List[Tuple[float | None, float | None]] | None = None,
) -> str:
    """Render the L2 consolidation dataset, annotating each summary with its start time.

    A consolidated narrative that lost all dates is useless across a multi-day session, so
    every summary is labelled with the START of the window it covers. The L2 marker's own
    first/last span is computed in code (``get_marker_time_range``), not by the LLM — these
    labels are context for ordering and for preserving meaningful date references.

    Args:
        marker_summaries: Summary bodies extracted from the consolidated markers.
        marker_time_ranges: Parallel list of ``(start_ts, end_ts)`` per summary, or None.
            Entries may be None/partial; those summaries fall back to an unlabelled header.

    Returns:
        The numbered ``SUMMARY n ...`` block for CONSOLIDATION_PROMPT's ``{summaries_text}``.
    """
    parts = []
    for i, summary in enumerate(marker_summaries, 1):
        start_label = None
        if marker_time_ranges is not None and i - 1 < len(marker_time_ranges):
            span = marker_time_ranges[i - 1]
            if span:
                start_label = format_marker_timestamp(span[0])
        if start_label:
            parts.append(f"SUMMARY {i} (starts {start_label}):\n{summary}")
        else:
            parts.append(f"SUMMARY {i}:\n{summary}")
    return '\n\n'.join(parts)


def _preserve_items_instruction(preserve_items) -> str:
    """Single source for the 'MUST retain verbatim' correction wording (BUG_0330 §3f). Used as
    the prompt suffix on a fresh respawn retry AND as the body of a warm-session correction
    turn, so both paths carry byte-identical instructions. Empty string when nothing to keep."""
    if not preserve_items:
        return ''
    _items = ', '.join(str(i).strip() for i in preserve_items if str(i).strip())
    if not _items:
        return ''
    return CONSOLIDATION_PRESERVE_ITEMS_INSTRUCTION.format(items=_items)


def _build_consolidation_prompt(marker_summaries, marker_time_ranges=None, preserve_items=None) -> str:
    """Shared consolidation prompt builder (BUG_0330): used by invoke_consolidation_agent AND
    the warm-session open path so the priming task and any fresh task are byte-identical."""
    from agent_cascade.prompts.dna import END_MARKER_INSTRUCTION
    summaries_text = _format_summaries_with_spans(marker_summaries, marker_time_ranges)
    end_instruction = ' ' + END_MARKER_INSTRUCTION  # marker only, no caption for compaction
    return CONSOLIDATION_PROMPT.format(
        summaries_text=summaries_text.strip(),
        end_instruction=end_instruction + _preserve_items_instruction(preserve_items)
    )


def invoke_consolidation_agent(
    agent_pool: Any,
    marker_summaries: List[str],
    caller_name: str | None = None,
    marker_time_ranges: List[Tuple[float | None, float | None]] | None = None,
    preserve_items: List[str] | None = None,
    warm_session: Any | None = None,
) -> Tuple[str, str]:
    """Invoke Compressor to consolidate multiple existing summaries into one.

    Same pattern as invoke_compression_agent() but uses CONSOLIDATION_PROMPT with
    numbered summary inputs instead of raw conversation messages.

    Args:
        agent_pool: The AgentPool instance (provides agent loading and state management).
        marker_summaries: List of summary texts extracted from compression markers to consolidate.
        caller_name: Optional caller instance name for slot management. If not provided,
                     reads agent_pool.session_name (falls back to 'Orchestrator').
        marker_time_ranges: Optional parallel list of ``(start_ts, end_ts)`` covered by each
                     summary. When provided, each entry is labelled with its start timestamp.
        preserve_items: Optional list of item labels the previous attempt dropped and that MUST be
                     retained verbatim. When non-empty, a tightening instruction is appended to the
                     prompt (used by the post-consolidation reviewer retry on a LOSS verdict).
                     None/empty → prompt is identical to the no-retry path (backward compatible).

    Returns:
        A 2-tuple ``(summary, caption)`` — same contract as invoke_compression_agent().

    Raises:
        RuntimeError: If the consolidation agent fails or returns an empty/invalid summary.
    """
    if not agent_pool:
        raise RuntimeError('agent_pool not connected')

    _ensure_compressor_loaded(agent_pool)

    comp_state_key = _generate_compressor_instance_name()

    # BUG_0330 warm reuse (see invoke_compression_agent): fixed-name warm Compressor first when
    # enabled; the fresh per-op name is the fallback. OFF → reuse_name None → identical to today.
    reuse_name = COMPRESSOR_WARM_NAME if COMPRESSOR_REUSE_ENABLED else None
    rid = uuid.uuid4().hex

    # Format input as numbered summaries, annotated with each window's start timestamp.
    # Retry path (preserve_items) appends the "MUST retain verbatim" wording to the end
    # instruction, keeping the base CONSOLIDATION_PROMPT byte-identical for the no-retry case.
    # Shared builder with the warm-session open path (§3f single source of truth).
    # Built lazily below: the warm-session branch never uses it (the session primed the
    # task with a byte-identical prompt when it opened).

    try:
        logger.info(
            f"Consolidation agent invoked via engine-based execution "
            f"(consolidating {len(marker_summaries)} summaries)"
        )

        # Get the caller name for parent tracking and slot management
        if caller_name is None:
            caller_name = getattr(agent_pool, 'session_name', None) or 'Orchestrator'

        # Acquire proper AgentInstance: warm fixed-name instance when reuse enabled, fresh
        # per-op spawn otherwise (byte-identical to the pre-BUG_0330 path).
        from agent_cascade.execution_engine import ExecutionEngine
        engine = ExecutionEngine(agent_pool)
        if warm_session is not None:
            # Warm consolidation session (§3f): claim + task message are owned by the session;
            # the consolidation task was already appended when it opened. Do NOT touch the
            # claim here — reuse_name stays None so the finally's release is a no-op.
            comp_instance, comp_state_key = warm_session.instance, warm_session.actual_name
            reuse_name = None
        else:
            consolidation_prompt = _build_consolidation_prompt(
                marker_summaries, marker_time_ranges, preserve_items)
            comp_instance, _reused, comp_state_key = compressor_reuse.acquire_compressor_agent(
                engine, 'Compressor', reuse_name, comp_state_key, consolidation_prompt, caller_name, rid)

        _configure_compressor_instance(agent_pool, comp_instance, caller_name)

        # Bound the system-launched Compressor with its own turn budget (decoupled from the caller's limit).
        # Re-applied unconditionally: the warm-hit reset nulls max_turns/_generate_cfg_override.
        comp_instance.max_turns = COMPRESSOR_AGENT_MAX_TURNS

        summary, caption = _execute_compressor_and_extract_summary(
            agent_pool, engine, comp_instance, comp_state_key, caller_name,
            timeout_label='Consolidation',
        )

        return summary, caption

    except Exception as e:
        raise RuntimeError(f"Consolidation failed: {e}") from e
    finally:
        # Always clean up consolidation agent state when done. Defensive wrapper mirrors the
        # compression invoker: a cleanup error must never skip the warm-claim release below
        # (a leaked claim wedges reuse for the process lifetime — BUG_0330).
        try:
            with agent_pool._execution._state_lock:
                if comp_state_key in agent_pool.instance_state:
                    agent_pool.instance_state[comp_state_key]['active'] = False
                    try:
                        agent_pool.active_stack_remove(comp_state_key)
                    except Exception:
                        pass
        except Exception as cleanup_err:
            logger.error(f"Consolidation cleanup failed for '{comp_state_key}': {cleanup_err}", exc_info=True)
        # Release the warm claim LAST, after cleanup (BUG_0330). rid-guarded no-op when the
        # flag was OFF or the acquire fell back to a fresh spawn.
        compressor_reuse.release_claim(reuse_name, rid)


def _generate_reviewer_instance_name() -> str:
    """Generate a unique instance name for a compaction-reviewer launch.

    Uses its own counter (prefix 'CompressionReviewer_') so reviewer names never collide with
    compressor names in the logger cache key (instance_name, agent_class) — the reviewer now runs
    as a separate Compressor instance. Thread-safe via _lock.
    """
    with _lock:
        global _reviewer_invocation_counter
        _reviewer_invocation_counter += 1
        return f'CompressionReviewer_{_reviewer_invocation_counter}'


def _configure_reviewer_instance(rv_instance: Any) -> None:
    """Force the reviewer's Compressor instance to be fully tool-less (output-only).

    The reviewer is a single-shot diff-check that must never call a tool. Every known tool is
    disabled here, using the same known-tool union the disabled_tools resolver filters against
    (TOOL_REGISTRY + RUNTIME_REGISTERED_TOOLS) so the set stays complete without hardcoding a list.
    Defense-in-depth: the Layer-1 instance override is always merged and the use-time registry
    filter intersects it with the live registry, so the reviewer is offered zero tools regardless
    of its template or UI config.
    """
    from agent_cascade.constants import RUNTIME_REGISTERED_TOOLS
    from agent_cascade.tools.base import TOOL_REGISTRY
    disabled = set(TOOL_REGISTRY.keys()) | set(RUNTIME_REGISTERED_TOOLS)
    # Sentinel that blocks all dynamically registered MCP tools; not a real tool name.
    disabled.add('__all_mcp_tools__')
    rv_instance._generate_cfg_override = {'disabled_tools': sorted(disabled)}


def invoke_compaction_reviewer(
    agent_pool: Any,
    incoming_summaries: str,
    outgoing_summary: str,
    caller_name: str,
) -> Tuple[bool, list]:
    """Launch a Compressor instance to audit one consolidated summary for data loss.

    Runs as a SEPARATE Compressor instance (same class/launch path as the system Compressor) with
    ALL tools disabled — there is no dedicated reviewer soul. Single-shot (max_turns=1), fresh
    instance per launch, acquired via the same _create_system_agent / yield_caller_slot /
    _execute_compressor_and_extract_summary plumbing as the Compressor (no save_fn — compaction
    rewrites the caller's context).

    The review role + verdict contract live in COMPACTION_REVIEWER_PROMPT (dna.py) because the
    Compressor soul is summarizer-oriented.

    Returns a parsed scalar verdict. An ambiguous reviewer output (no verdict token) resolves to
    PASS. Launch/infra failures raise RuntimeError so the CALLER's gate fail-opens — this function
    deliberately does NOT swallow its own errors, so a reviewer outage is distinguishable from a
    genuine verdict.

    Args:
        agent_pool: The AgentPool instance (provides agent loading and state management).
        incoming_summaries: Rendered incoming summary block (same rendering as consolidation).
        outgoing_summary: The candidate consolidated summary to audit.
        caller_name: Calling agent instance name for slot management / telemetry attribution.

    Returns:
        (is_pass, missing_items) — missing_items is a list of short labels, empty on PASS.

    Raises:
        RuntimeError: If the reviewer cannot be loaded/launched or returns invalid output.
    """
    if not agent_pool:
        raise RuntimeError('agent_pool not connected')

    _ensure_compressor_loaded(agent_pool)

    reviewer_key = _generate_reviewer_instance_name()

    # BUG_0330 warm reuse: distinct fixed warm name for the reviewer (different config/prompt from
    # the summarizer → distinct claim; a held summarizer never blocks a review). OFF → None, and
    # behavior is identical to today (fresh reviewer per launch).
    reuse_name = COMPRESSOR_REVIEWER_WARM_NAME if COMPRESSOR_REUSE_ENABLED else None
    rid = uuid.uuid4().hex

    from agent_cascade.prompts.dna import END_MARKER_INSTRUCTION
    end_instruction = ' ' + END_MARKER_INSTRUCTION  # marker only, no caption for the review
    review_prompt = COMPACTION_REVIEWER_PROMPT.format(
        incoming_summaries=incoming_summaries.strip(),
        outgoing_summary=outgoing_summary.strip(),
        max_retries=COMPRESSION_REVIEW_MAX_RETRIES,
        end_instruction=end_instruction,
    )

    try:
        # Lazy import to avoid the execution_engine → compression circular dependency.
        from agent_cascade.execution_engine import ExecutionEngine
        engine = ExecutionEngine(agent_pool)
        rv_instance, _reused, reviewer_key = compressor_reuse.acquire_compressor_agent(
            engine, 'Compressor', reuse_name, reviewer_key, review_prompt, caller_name, rid)

        _configure_reviewer_instance(rv_instance)
        # Single-shot: decoupled from COMPRESSOR_AGENT_MAX_TURNS so a Compressor budget bump can't
        # silently give the reviewer a multi-turn tool loop. Must stay 1. Re-applied unconditionally:
        # the warm-hit reset nulls max_turns/_generate_cfg_override.
        rv_instance.max_turns = COMPACTION_REVIEWER_MAX_TURNS

        verdict_text, _ = _execute_compressor_and_extract_summary(
            agent_pool, engine, rv_instance, reviewer_key, caller_name,
            timeout_label='Compaction review',
            agent_class='Compressor',
        )

        return _parse_compaction_verdict(verdict_text)

    except Exception as e:
        # The only consumer is the gate (_validate_and_reconsolidate), which fail-opens on any
        # Exception without parsing the message, so no message-substring laundering can start a
        # retry storm here. Re-wrap as RuntimeError to keep the failure class distinct from the
        # parsed (non-raising) verdicts.
        raise RuntimeError(f"Compaction review failed: {e}") from e
    finally:
        # Clean up reviewer agent state when done (mirrors the consolidation invoker's finally);
        # defensive so a pool that lacks the cleanup path can never escalate a clean verdict.
        try:
            with agent_pool._execution._state_lock:
                if reviewer_key in agent_pool.instance_state:
                    agent_pool.instance_state[reviewer_key]['active'] = False
                    try:
                        agent_pool.active_stack_remove(reviewer_key)
                    except Exception:
                        pass
        except Exception:
            pass
        # Release the warm reviewer claim LAST, after cleanup (BUG_0330). rid-guarded no-op when
        # the flag was OFF or the acquire fell back to a fresh spawn; the fail-open contract of
        # this function is unchanged — verdicts and RuntimeError wrapping propagate as before.
        compressor_reuse.release_claim(reuse_name, rid)
