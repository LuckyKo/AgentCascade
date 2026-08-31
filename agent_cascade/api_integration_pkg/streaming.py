"""Stream-update broadcasting helpers (moved verbatim from api_integration.py).

Phase 3b pure-move refactor. ``broadcast_stream_update`` calls
``build_stream_update_from_pool`` (state_builder) and ``_put_stream_update`` (this module).
"""

import asyncio
import threading
import time
from pathlib import Path
from typing import List, Optional

from agent_cascade.log import logger
from agent_cascade.agent_pool import AgentPool
from agent_cascade.llm.schema import Message
from agent_cascade.api_integration_pkg.cache import _cache_mgr, _STREAM_TOKEN_STATS_CACHE_MAXSIZE
from agent_cascade.api_integration_pkg.state_builder import build_stream_update_from_pool

# ────────────────────────────────────────────────────────────────────────────
# STREAMING BACKLOG PROBE — TEMPORARY DIAGNOSTIC (evidence-gathering only)
# Set False / remove after diagnosis. When False, nothing below is logged and
# there is no measurable overhead. See reports/streaming_backend_probe_HOWTO.md
# ────────────────────────────────────────────────────────────────────────────
STREAM_BACKEND_DEBUG = False  # streaming backlog probe — diagnosis complete (Fix A+B verified); re-enable only when re-investigating a WS send-path stall. See reports/streaming_backend_probe_HOWTO.md

_PROBE_LOCK = threading.Lock()
_PROBE_STATE: dict = {}


def _probe_get_logger():
    """Lazily build a dedicated logger that writes ONLY to the probe file.

    Kept separate from the app's main ``logger`` so probe lines never pollute
    console.log. Safe to call from any thread (idempotent, lock-guarded).
    """
    global _PROBE_LOCK
    if not STREAM_BACKEND_DEBUG:
        return None
    try:
        import logging
        # streaming.py is at <root>/agent_cascade/api_integration_pkg/streaming.py
        project_root = Path(__file__).resolve().parent.parent.parent
        log_dir = project_root / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        probe_logger = logging.getLogger('stream_probe_backend')
        if not probe_logger.handlers:  # idempotent
            probe_logger.setLevel(logging.INFO)
            fh = logging.FileHandler(str(log_dir / 'stream_probe_backend.log'), encoding='utf-8', delay=True)
            fh.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S'))
            probe_logger.addHandler(fh)
            probe_logger.propagate = False  # never leak into the root/main logger
        return probe_logger
    except Exception:
        return None


def _probe_record(instance_name: str, t_yield, t_enqueue: float, resp_len: int,
                  is_streaming_tick: bool, len_changed: bool, qsize) -> None:
    """Record one broadcast timing sample. NON-SPAMMY by design:

      * logs a line only when yield_to_enqueue_ms > 100 (meaningful backend delay),
        OR every 50th broadcast as a heartbeat with running max/avg;
      * logs a single "BACKLOG DETECTED" line on a <50ms -> >500ms jump;
      * hard-caps output at ~1 line/sec per instance so a pathological case
        can never flood the file.

    This function only READS and LOGS — it must never raise into the caller.
    """
    if not STREAM_BACKEND_DEBUG or t_yield is None:
        return
    try:
        delay_ms = (t_enqueue - t_yield) * 1000.0
        now_wall = time.time()
        with _PROBE_LOCK:
            st = _PROBE_STATE.get(instance_name)
            if st is None:
                st = {'count': 0, 'sum_ms': 0.0, 'max_ms': 0.0,
                      'prev_delay_ms': None, 'last_log_wall': 0.0}
                _PROBE_STATE[instance_name] = st
            st['count'] += 1
            st['sum_ms'] += delay_ms
            if delay_ms > st['max_ms']:
                st['max_ms'] = delay_ms

            # Backlog detection: a sharp jump from <50ms to >500ms → one line.
            backlog = (st['prev_delay_ms'] is not None
                       and st['prev_delay_ms'] < 50.0 and delay_ms > 500.0)

            # Sampling / threshold gating.
            heartbeat = (st['count'] % 50 == 0)
            over_thresh = (delay_ms > 100.0)
            should_log = backlog or heartbeat or over_thresh

            if should_log:
                avg_ms = st['sum_ms'] / st['count']
                # Rate cap: ~1 line/sec per instance (backlog lines bypass the cap).
                if not backlog and (now_wall - st['last_log_wall']) < 1.0:
                    pass  # suppress this sample to keep output non-spammy
                else:
                    _probe_get_logger().info(
                        f"{'BACKLOG ' if backlog else ''}inst={instance_name} "
                        f"yield_to_enqueue_ms={delay_ms:.1f} avg={avg_ms:.1f} max={st['max_ms']:.1f} "
                        f"n={st['count']} resp_len={resp_len} tick={int(is_streaming_tick)} "
                        f"len_chg={int(len_changed)} qsize={qsize}"
                    )
                    st['last_log_wall'] = now_wall

            st['prev_delay_ms'] = delay_ms
    except Exception:
        pass  # probe must never break the broadcast path


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
    yield_time: Optional[float] = None,  # PROBE: time.monotonic() captured at the call site right after engine yielded; ignored when STREAM_BACKEND_DEBUG is False
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

    # Throttle: broadcast only on meaningful events or periodic interval
    should_broadcast = (
        is_streaming_tick
        or len_changed
        or (now_sec - last_send > 0.1)  # 100ms throttle
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
            # ── PROBE: capture t_enqueue right before dispatch ──────────────
            # yield_to_enqueue_ms = (t_enqueue - t_yield)*1000 is THE key number.
            # Gated on STREAM_BACKEND_DEBUG; no-op when disabled or yield_time absent.
            if STREAM_BACKEND_DEBUG and yield_time is not None:
                _probe_record(
                    instance_name=instance_name,
                    t_yield=yield_time,
                    t_enqueue=time.monotonic(),
                    resp_len=resp_len,
                    is_streaming_tick=is_streaming_tick,
                    len_changed=len_changed,
                    qsize=(ws_queue.qsize() if hasattr(ws_queue, 'qsize') else -1),
                )

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
