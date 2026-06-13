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

from .agent_instance import LoopDetectedError
from .agent_pool import AgentPool
from .loop_detection import detect_loop as _detect_loop_func


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
        create_main_agent_instance,
        run_agent_in_pool_with_recovery,
        build_state_from_pool,
        build_stream_update_from_pool,
        _apply_ui_config,
        _put_stream_update,
        _build_activity_update,  # For lightweight activity banner updates
    )

    try:
        # ── Initialize pool state ────────────────────────────────────────
        pool.stopped = False
        if hasattr(pool, '_execution'):
            with pool._execution._state_lock:
                pool._execution.active_stack.clear()

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

        for turn_output in run_agent_in_pool_with_recovery(
            pool=pool,
            instance_name=instance_name,
            max_auto_retries=max_auto_retries,
            auto_rollback_enabled=auto_rollback_enabled,
        ):
            # NOTE: run_agent_in_pool_with_recovery handles LoopDetectedError
            # with retry logic. Only non-loop exceptions are terminal and
            # yield [SYSTEM ERROR] — they don't trigger retries.
            # Check for stop request or pool shutdown
            if pool.stopped:
                break

            now = time.time()

            # ── Detect state changes for sub-agent refresh ───────────────
            resp_len = len(turn_output)
            len_changed = (resp_len != exec_state['last_resp_len'])
            exec_state['last_resp_len'] = resp_len

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
                
                # Extract streaming text for activity banner (ASSISTANT role only)
                if msg_role == ASSISTANT:
                    streaming_text = (
                        last_msg.get('content', '') if isinstance(last_msg, dict)
                        else getattr(last_msg, 'content', '')
                    )

            # ── Throttle state broadcasts ────────────────────────────────
            should_broadcast = (
                now - last_send > 0.15
                or len_changed
                or has_tool_event
            )

            if should_broadcast:
                # Fix #2: Force full state refresh every 100 ticks (~15 seconds) to recover
                # from sync gaps. During partial streaming, some messages may be missed;
                # periodic full refresh ensures eventual consistency.
                force_full = (tick_num % 100 == 0)
                
                # Build lightweight stream update (only serializes changing messages)
                # build_stream_update_from_pool internally handles sub-agent snapshots
                stream_update = build_stream_update_from_pool(
                    pool=pool,
                    instance_name=instance_name,
                    responses=turn_output,
                    force_full=force_full,
                )
                if stream_update is not None:
                    event = {
                        'type': 'stream_update',
                        **stream_update,
                    }
                    # Use put_nowait to avoid blocking the agent thread when
                    # the send_queue is full (stale events are dropped).
                    # QueueFull is raised inside the event loop; since we don't
                    # check the Future's result, it's silently swallowed — which
                    # is exactly what we want (drop stale stream_updates).
                    asyncio.run_coroutine_threadsafe(
                        _put_stream_update(send_queue, event),
                        loop,
                    )
                    last_send = now
                    
                    # ── Push lightweight activity update for faster banner feedback ───
                    # Activity updates are separate from full state broadcasts and can
                    # be sent more frequently to provide near-real-time UI feedback.
                    if streaming_text:
                        # Throttle activity updates to minimum 50ms interval (Critical Issue #2)
                        # During fast LLM streaming this prevents dozens of updates per second
                        exec_state.setdefault('_last_activity_send', 0)
                        now_time = time.time()
                        if now_time - exec_state['_last_activity_send'] >= 0.05:  # 50ms min interval
                            exec_state['_last_activity_send'] = now_time
                            activity_data = _build_activity_update(
                                pool=pool,
                                instance_name=instance_name,
                                streaming_text=streaming_text,
                            )
                            if activity_data:
                                # Send activity update at higher frequency (50ms vs 150ms)
                                activity_event = {
                                    'type': 'activity_update',
                                    **activity_data,
                                }
                                asyncio.run_coroutine_threadsafe(
                                    _put_stream_update(send_queue, activity_event),
                                    loop,
                                )

                # ── Loop detection (throttled to every 10 ticks) ─────────
                if tick_num % 10 == 0:
                    _detect_loop_in_instance(pool, instance_name, turn_output)

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
    except LoopDetectedError:
        # Re-raise — let run_agent_in_pool_with_recovery handle loop recovery.
        # This MUST come before the generic Exception handler to avoid treating
        # a loop as a terminal error (which would skip the surgical rollback + retry).
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
            if stream_update is not None:
                # Use put_nowait via helper - QueueFull handled inside event loop
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
        return {
            'history_tokens': 0,
            'history_words': 0,
            'total_messages': 0,
            'active_messages': 0,
            'max_tokens': 128000,
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


# ═══════════════════════════════════════════════════════════════════════
# 4. Loop Detection During Streaming
# ═══════════════════════════════════════════════════════════════════════

def _detect_loop_in_instance(
    pool: AgentPool,
    instance_name: str,
    responses: List[Message],
) -> None:
    """Check for loops in the agent's conversation during streaming (throttled).

    This is called every 10 ticks from run_agent_thread_unified. It reads
    the conversation directly from the pool and checks for repetitive patterns.

    NOTE: Loop detection also happens inside ExecutionEngine._pre_llm_checks().
    This is a secondary check for the streaming path — it catches loops while
    the agent is still generating, before they propagate to the engine.

    If a loop IS detected during streaming, we raise LoopDetectedError which
    will be caught by run_agent_in_pool_with_recovery and trigger surgical
    rollback + retry.
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        return

    # Get the full working set. After ExecutionEngine yields, turn_output messages
    # have already been appended to instance.conversation (Phase 4 of engine.run).
    # So we just use the conversation directly — no need to concatenate responses.
    with instance._compression_lock:
        all_msgs = list(instance.conversation)

    try:
        loop_info = _detect_loop_func(all_msgs)
        if loop_info:
            reason, pop_count = loop_info
            # Raise LoopDetectedError so the recovery wrapper handles rollback
            raise LoopDetectedError(reason=reason, pop_count=pop_count)
    except LoopDetectedError:
        # Re-raise — let run_agent_in_pool_with_recovery handle surgical rollback + retry
        raise
    except Exception as e:
        logger.debug(f"Loop detection failed for {instance_name}: {e}")