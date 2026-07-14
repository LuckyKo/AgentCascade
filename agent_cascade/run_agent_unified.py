"""
Unified Agent Execution — Phase 4 of the AgentCascade Architecture Rewrite.

Replaces the dual-path code in api_server.py where:
  - Main agent ran through run_agent_thread() → agent_runner.run() using session['history']
  - Sub-agents ran through a separate execution path

After Phase 4, ALL agents (including the main orchestrator) are instances in the
pool, executed through ExecutionEngine.run(), with state read from
pool.instances[name].conversation. NO session['history'].

See DESIGN_REWRITE.md §5 for design rationale.

Key principle: This module provides drop-in replacements for the api_server.py
functions that use ONLY the new unified architecture. The old code remains until
integration is complete.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

from agent_cascade.llm.schema import (
    ASSISTANT, FUNCTION, ROLE, Message,
)
from agent_cascade.log import logger

from .agent_pool import AgentPool



# ═══════════════════════════════════════════════════════════════════════
# 1. Unified run_agent_thread Replacement
# ═══════════════════════════════════════════════════════════════════════

def run_agent_thread_unified(
    pool: AgentPool,
    instance_name: str,
    system_message_content: Optional[str],
    ui_cfg: Dict[str, Any],
    send_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Run the main agent through the unified ExecutionEngine.

    This is a drop-in replacement for api_server.py's run_agent_thread(). It runs
    in a background thread, yields state updates onto the async send_queue, and
    uses ONLY pool.instances[name].conversation — never session['history'].

    Flow:
      1. Create main agent instance if it doesn't exist (with system message)
      2. Apply UI configuration (temperature, max_tokens, etc.)
      3. Run through unified engine with loop recovery
      4. Build state from pool and broadcast via WebSocket send_queue

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the main agent (typically session name like "Maine").
        system_message_content: System prompt text (used only if creating new instance).
        ui_cfg: UI configuration dictionary (temperature, max_tokens, etc.).
        send_queue: Async queue for sending WebSocket updates.
        loop: The asyncio event loop to submit coroutines to.

    Example:
        # In api_server.py WebSocket handler:
        thread = threading.Thread(
            target=run_agent_thread_unified,
            args=(pool, session_name, sys_msg_text, ui_cfg, send_queue, loop),
            daemon=True,
        )
        thread.start()
    """
    from .api_integration import (
        broadcast_stream_update,
        build_stream_update_from_pool,
        create_main_agent_instance,
        run_agent_in_pool_with_recovery,
        build_state_from_pool,
        _apply_ui_config,
        _put_stream_update,
    )

    try:
        # ── Initialize pool state ────────────────────────────────────────
        pool.stopped = False
        if hasattr(pool, '_execution'):
            with pool._execution._state_lock:
                pool._execution.active_stack.clear()

        # Increment run generation to signal old threads they've been superseded.
        # Old execution threads check this value and exit when it changes.
        pool._run_generation += 1
        current_generation = pool._run_generation

        # ── Store send_queue and loop on pool for sub-agent streaming ────
        # Sub-agents execute synchronously inside ExecutionEngine and need
        # access to the WebSocket send_queue to push stream_update events.
        pool._ws_send_queue = send_queue
        pool._ws_loop = loop

        # ── Create main agent instance if it doesn't exist ───────────────
        instance = pool.get_instance(instance_name)
        if (instance is None or not instance.conversation) and system_message_content:
            create_main_agent_instance(
                pool=pool,
                instance_name=instance_name,
                system_message_content=system_message_content,
            )

        # ── Apply UI configuration ───────────────────────────────────────
        _apply_ui_config(pool, instance_name, ui_cfg)

        # ── Extract execution parameters from UI config ──────────────────
        max_auto_retries = ui_cfg.get('max_auto_rollbacks', 3)
        if max_auto_retries == -1:
            max_auto_retries = 999_999
        auto_rollback_enabled = ui_cfg.get('auto_rollback_on_loop', True)

        # ── Run through unified engine with recovery ─────────────────────
        tick_num = 0
        last_send = 0.0

        # Thread-local state container — using a mutable dict avoids the
        # function-attribute pattern which is not thread-safe when multiple
        # WebSocket connections each spawn their own execution thread.
        exec_state = {'last_resp_len': 0}

        for turn_output_raw in run_agent_in_pool_with_recovery(
            pool=pool,
            instance_name=instance_name,
            max_auto_retries=max_auto_retries,
            auto_rollback_enabled=auto_rollback_enabled,
        ):
            # Unpack (turn_output, is_streaming) signal from engine.run()
            if isinstance(turn_output_raw, tuple) and len(turn_output_raw) == 2:
                turn_output, is_streaming_tick = turn_output_raw
            else:
                turn_output, is_streaming_tick = turn_output_raw, False

            # FIX TODO #41: Check all stop conditions including per-instance halt and termination
            if pool.stopped or current_generation != pool._run_generation \
                    or instance_name in pool._halted_instances \
                    or pool.is_instance_terminated(instance_name):
                break

            now = time.monotonic()

            # Check if the last message is a tool call or function result
            has_tool_event = False
            streaming_text = None
            if turn_output:
                last_msg = turn_output[-1]
                msg_role = (
                    last_msg.get(ROLE, '') if isinstance(last_msg, dict)
                    else getattr(last_msg, 'role', '')
                )
                msg_fc = last_msg.get('function_call') if isinstance(last_msg, dict) else getattr(last_msg, 'function_call', None)
                has_tool_event = bool(msg_fc) or msg_role == FUNCTION
                
                # Extract streaming text for activity banner (including reasoning/tools)
                if msg_role == ASSISTANT:
                    content = last_msg.get('content', '') if isinstance(last_msg, dict) else getattr(last_msg, 'content', '')
                    reasoning = last_msg.get('reasoning_content', '') if isinstance(last_msg, dict) else getattr(last_msg, 'reasoning_content', '')
                    
                    if msg_fc:
                        # Show tool call arguments in activity banner for "live" feel
                        fc_name = msg_fc.get('name', '') if isinstance(msg_fc, dict) else getattr(msg_fc, 'name', '')
                        fc_args = msg_fc.get('arguments', '') if isinstance(msg_fc, dict) else getattr(msg_fc, 'arguments', '')
                        streaming_text = f"Tool {fc_name}({str(fc_args)[:100]}...)"
                    elif reasoning:
                        # Show thinking process
                        streaming_text = str(reasoning)
                    else:
                        streaming_text = str(content)
                elif msg_role == FUNCTION:
                    # Tool result (FUNCTION role): show which tool completed + brief preview
                    from agent_cascade.utils.utils import format_tool_result_preview, msg_field
                    tool_name = msg_field(last_msg, 'name', '')
                    content = msg_field(last_msg, 'content', '')
                    streaming_text = format_tool_result_preview(tool_name, content, max_len=120)

            # ── WebSocket broadcast (shared helper handles all throttling) ──
            # Tool events are signaled via is_streaming_tick=True so the helper
            # bypasses its internal throttle immediately.
            last_send, exec_state['last_resp_len'] = broadcast_stream_update(
                pool=pool,
                instance_name=instance_name,
                turn_output=turn_output,
                is_streaming_tick=is_streaming_tick or has_tool_event,
                tick_num=tick_num,
                now_sec=now,
                last_send=last_send,
                last_resp_len=exec_state['last_resp_len'],
            )

            tick_num += 1

        # ── Final state broadcast ────────────────────────────────────────
        final_state = build_state_from_pool(
            pool=pool,
            instance_name=instance_name,
            generating=False,
        )
        if final_state is not None:
            # Match old api_server behavior: type='done' + instance_halted field
            halted = pool.is_instance_halted(instance_name)
            asyncio.run_coroutine_threadsafe(
                send_queue.put({
                    'type': 'done',
                    **final_state,
                    'instance_halted': halted,
                }),
                loop,
            )

    except (KeyboardInterrupt, SystemExit):
        # Never swallow user interrupts or explicit exits
        raise
    except Exception as e:
        # Catch unhandled exceptions — log and yield error state
        logger.error(f"run_agent_thread_unified failed for {instance_name}: {e}")
        error_msg = Message(
            role=ASSISTANT,
            content=f"[SYSTEM ERROR: {e}]",
        )
        try:
            stream_update = build_stream_update_from_pool(
                pool=pool,
                instance_name=instance_name,
                responses=[error_msg],
            )
            if stream_update is not None and send_queue and loop:
                # Defensive guard: send_queue/loop may be stale after exception
                asyncio.run_coroutine_threadsafe(
                    _put_stream_update(send_queue, {
                        'type': 'stream_update',
                        **stream_update,
                    }),
                    loop,
                )
        except Exception as e:
            logger.debug(f"Error state broadcast failed (non-critical): {e}")


# ═══════════════════════════════════════════════════════════════════════
# 3. Unified Token Counting Integration
# ═══════════════════════════════════════════════════════════════════════

def get_token_stats_unified(
    pool: AgentPool,
    instance_name: str,
) -> Dict[str, int]:
    """Get token statistics for an agent instance from the unified pool.

    In the old code, token counting was split between session-level caches
    (_cached_hist_stats, _cached_r_stats, etc.) and pool-level tracking.
    In the unified model, each instance tracks its own conversation, and we
    simply calculate tokens on the working set (after compression slicing).

    This is simpler but less incremental — however, the get_history_stats()
    function has built-in LRU caching for Message objects, so repeated calls
    on unchanged messages are fast.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the agent instance to get stats for.

    Returns:
        Dictionary with token statistics:
          - 'history_tokens': Tokens in the active working set (after compression)
          - 'history_words': Words in the active working set
          - 'total_messages': Total messages in conversation
          - 'active_messages': Messages in the active working set
          - 'max_tokens': Maximum token budget for this instance

    Example:
        stats = get_token_stats_unified(pool, "Maine")
        print(f"Using {stats['history_tokens']}/{stats['max_tokens']} tokens")
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        from agent_cascade.settings import DEFAULT_MAX_TOKENS
        return {
            'history_tokens': 0,
            'history_words': 0,
            'total_messages': 0,
            'active_messages': 0,
            'max_tokens': DEFAULT_MAX_TOKENS,
        }

    # Get the active working set (after compression slicing)
    conv = instance.conversation
    active_h = pool.slice_history_for_llm(conv) if conv else conv

    # Calculate token stats with LRU caching
    try:
        from agent_cascade.utils.utils import get_history_stats
        h_stats = get_history_stats(active_h)
    except Exception as e:
        logger.debug(f"Token stats calculation failed for {instance_name} (using estimate): {e}")
        # Fallback: estimate ~4 tokens per message on average (conservative)
        h_stats = {'tokens': len(active_h) * 4, 'words': 0}

    # Get max tokens for this instance
    from .api_integration import _get_max_tokens_for_instance
    max_tokens = _get_max_tokens_for_instance(pool, instance)

    return {
        'history_tokens': h_stats['tokens'],
        'history_words': h_stats['words'],
        'total_messages': len(conv),
        'active_messages': len(active_h),
        'max_tokens': max_tokens,
    }


def get_token_usage_percentage(
    pool: AgentPool,
    instance_name: str,
) -> float:
    """Get the token usage percentage for an agent instance.

    Convenience wrapper around get_token_stats_unified that returns a simple
    percentage (0-100). Used by _pre_llm_checks in ExecutionEngine to decide
    whether to trigger compression.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the agent instance.

    Returns:
        Token usage as a percentage (e.g., 75.5 means 75.5% used).
        Returns 0.0 if instance not found or max_tokens is 0.

    Example:
        usage = get_token_usage_percentage(pool, "Maine")
        if usage > 95:
            # Force compression
            pass
    """
    stats = get_token_stats_unified(pool, instance_name)
    max_tokens = stats['max_tokens']
    if max_tokens <= 0:
        return 0.0
    return (stats['history_tokens'] / max_tokens) * 100.0


# ── Loop detection now handled by canonical detect_loop in loop_detection.py ──