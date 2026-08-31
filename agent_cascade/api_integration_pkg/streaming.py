"""Stream-update broadcasting helpers (moved verbatim from api_integration.py).

Phase 3b pure-move refactor. ``broadcast_stream_update`` calls
``build_stream_update_from_pool`` (state_builder) and ``_put_stream_update`` (this module).
"""

import asyncio
from typing import List, Optional

from agent_cascade.log import logger
from agent_cascade.agent_pool import AgentPool
from agent_cascade.llm.schema import Message
from agent_cascade.api_integration_pkg.cache import _cache_mgr, _STREAM_TOKEN_STATS_CACHE_MAXSIZE
from agent_cascade.api_integration_pkg.state_builder import build_stream_update_from_pool

async def _put_stream_update(queue: 'asyncio.Queue', event: dict) -> None:
    """Put a stream_update event onto the queue, dropping it if full.

    This helper is used with run_coroutine_threadsafe to push events from
    the agent thread into the async send_queue. It calls put_nowait() directly
    (synchronous) so stale stream_updates are dropped rather than blocking
    the agent thread.

    NOTE: The function is marked 'async' solely so it can be scheduled via
    run_coroutine_threadsafe from worker threads — that API requires a coroutine.
    QueueFull is caught inside the event loop and never propagated to caller.
    """
    import asyncio  # Lazy import to avoid module-level dependency
    try:
        queue.put_nowait(event)  # Synchronous, raises QueueFull if full
    except asyncio.QueueFull:
        pass  # Drop stale event; a newer one will arrive soon

def broadcast_stream_update(
    pool: AgentPool,
    instance_name: str,
    turn_output: Optional[List[Message]],
    is_streaming_tick: bool,
    tick_num: int,
    now_sec: float,
    last_send: float,
    last_resp_len: int,
    send_queue=None,       # Explicit queue (preferred) or None to use pool._ws_send_queue
    loop=None,             # Explicit loop (preferred) or None to use pool._ws_loop
) -> tuple[float, int]:
    """Build and push a stream_update event for an agent instance.

    This is the single shared broadcast helper used by all three execution paths
    (main agent in run_agent_unified.py, Security in api_server.py, Compressor
    in compression/agent_invoker.py). It encapsulates the throttling algorithm,
    force-full-refresh logic, and queue dispatch — eliminating ~60 lines of
    duplicated code per caller.

    Algorithm:
        1. Detect if response length changed (new committed messages)
        2. Broadcast if any of these conditions are true:
           - is_streaming_tick (explicit signal from ExecutionEngine or tool event)
           - len_changed (new message added to conversation)
           - 100ms elapsed since last send (throttle interval)
        3. Force full state serialization every 100 ticks (~10s at ~150ms/tick)
           to recover from sync gaps where individual stream_update messages
           may have been dropped due to queue-full conditions.

    NOTE on tool events: The main agent has an extra condition (has_tool_event).
    Pass is_streaming_tick=True when a tool event occurs — the helper treats it
    identically to a streaming tick and will bypass the throttle immediately.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the active instance (e.g., "Maine", "Security_op_abc").
        turn_output: Current partial response messages from engine.run() yield.
        is_streaming_tick: True if this tick carries streaming content updates or tool events.
        tick_num: Monotonically increasing tick counter for force_full scheduling.
        now_sec: Current monotonic time (from time.monotonic()).
        last_send: Monotonic time of the last successful broadcast.
        last_resp_len: Response length from the previous tick (for change detection).
        send_queue: Optional explicit asyncio.Queue. If None, reads pool._ws_send_queue.
        loop: Optional event loop. If None, reads pool._ws_loop.

    Returns:
        Tuple (new_last_send: float, new_resp_len: int) for the caller to update its state.
        The returned last_send is updated only if a broadcast was actually sent.
    """
    import asyncio

    # Detect response length changes (new committed messages)
    resp_len = len(turn_output) if turn_output else 0
    len_changed = (resp_len != last_resp_len)

    # Throttle: broadcast only on meaningful events or periodic interval.
    # Streaming ticks are floored at MIN_STREAM_BROADCAST_INTERVAL (~5x/sec) so a burst
    # of SSE chunks no longer triggers one full-payload build+send per chunk; committed
    # messages (len_changed) and non-streaming metadata updates still pass through on the
    # original 100ms cadence.
    MIN_STREAM_BROADCAST_INTERVAL = 0.2
    should_broadcast = (
        len_changed
        or (is_streaming_tick and (now_sec - last_send >= MIN_STREAM_BROADCAST_INTERVAL))
        or (not is_streaming_tick and (now_sec - last_send > 0.1))  # 100ms throttle
    )

    if not should_broadcast:
        return (last_send, resp_len)

    # Resolve send_queue and loop: prefer explicit params, fall back to pool attributes
    ws_queue = send_queue or getattr(pool, '_ws_send_queue', None)
    ws_loop = loop or getattr(pool, '_ws_loop', None)

    if not ws_queue or not ws_loop:
        return (last_send, resp_len)

    try:
        if ws_loop.is_closed():
            return (last_send, resp_len)

        # Force full state refresh every 100 ticks (~10s) to recover from sync gaps.
        # During partial streaming some events may be dropped; periodic full refresh
        # ensures eventual UI consistency even if individual stream_update messages
        # were lost due to queue-full conditions.
        force_full = (tick_num % 100 == 0)

        stream_update = build_stream_update_from_pool(
            pool=pool,
            instance_name=instance_name,
            responses=turn_output,
            force_full=force_full,
        )

        if stream_update is not None:
            asyncio.run_coroutine_threadsafe(
                _put_stream_update(
                    ws_queue,
                    {'type': 'stream_update', **stream_update},
                ),
                ws_loop,
            )

        return (now_sec, resp_len)

    except Exception as e:
        # RuntimeError if event loop is closed; catch-all for safety
        logger.debug(
            f"[STREAM_BROADCAST] Update failed for {instance_name} "
            f"(non-critical): {e}"
        )
        return (last_send, resp_len)

def _calc_stream_token_stats(
    pool: AgentPool, instance_name: str,
    conv_snapshot: List[Message], stream_resp_snapshot: Optional[List[Message]],
    responses: Optional[List[Message]],
) -> tuple:
    """Calculate token stats for streaming updates with caching.
    
    Computes h_stats and r_stats from the combined conversation + streaming snapshot,
    then caches them keyed by instance_name for reuse during active generation.
    
    Returns:
        (h_stats, r_stats) tuple of dicts with 'tokens' and 'words' keys.
    """
    # Include streaming responses in combined snapshot for accurate stats
    combined_snapshot = conv_snapshot + (stream_resp_snapshot if stream_resp_snapshot else [])
    active_h = pool.slice_history_for_llm(combined_snapshot) if combined_snapshot else conv_snapshot

    try:
        from agent_cascade.utils.utils import get_history_stats
        h_stats = get_history_stats(active_h)
        r_stats = get_history_stats(responses) if responses else {'tokens': 0, 'words': 0}
    except Exception as e:
        logger.debug(f"Token stats calculation failed for stream update (using estimate): {e}")
        h_stats = {'tokens': len(active_h) * 4, 'words': 0}
        r_stats = {'tokens': 0, 'words': 0}
    
    # Cache the computed stats for reuse during active generation
    _cache_mgr.evict_if_full('stream_token_stats', _STREAM_TOKEN_STATS_CACHE_MAXSIZE)
    with _cache_mgr._lock:
        _cache_mgr.stream_token_stats[instance_name] = (h_stats, r_stats)

    return h_stats, r_stats
