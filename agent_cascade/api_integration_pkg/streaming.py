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
from agent_cascade.api_integration_pkg.state_builder import (
    build_stream_update_from_pool,
    _stream_time as _st_timing_record,
    _STREAM_TIMING_ENABLED as _ST_TIMING_ON,
)
import time as _time_streaming

# ────────────────────────────────────────────────────────────────────────────
# STREAMING BACKLOG PROBE — TEMPORARY DIAGNOSTIC (evidence-gathering only)
# Set False / remove after diagnosis. When False, nothing below is logged and
# there is no measurable overhead. See reports/streaming_backend_probe_HOWTO.md
# ────────────────────────────────────────────────────────────────────────────
STREAM_BACKEND_DEBUG = False  # streaming backlog probe — code retained for per-instance "weird mode" diagnosis; set True to re-enable (see reports/streaming_backend_probe_HOWTO.md). Probe state is self-cleaning and a no-op when False.

_PROBE_LOCK = threading.Lock()
_PROBE_STATE: dict = {}
# Wall-clock of the most recent probe record per instance, used to detect
# inter-tick GAPS (a "weird mode" stall shows up as a large gap between two
# broadcast calls for the SAME instance while other instances keep streaming).
_PROBE_LAST_WALL: dict = {}
# Stale-entry eviction so _PROBE_STATE/_PROBE_LAST_WALL don't grow unbounded
# over long runs with many agent instances. An instance not seen for more than
# _PROBE_STALE_SECS is considered dead and removed from BOTH dicts. Cleanup is
# opportunistic (see _probe_record): only when the dict grows past a small
# threshold OR at most once per ~_PROBE_CLEANUP_INTERVAL_SECS wall-clock.
_PROBE_STALE_SECS = 60.0
_PROBE_CLEANUP_INTERVAL_SECS = 30.0
_PROBE_CLEANUP_SIZE_THRESHOLD = 64
_probe_last_cleanup_wall: float = 0.0


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
        # Producer thread identity — each instance should have its OWN producer
        # thread; if a "weird" instance shares/loses its thread that's a clue.
        tid = threading.get_ident()
        with _PROBE_LOCK:
            st = _PROBE_STATE.get(instance_name)
            if st is None:
                st = {'count': 0, 'sum_ms': 0.0, 'max_ms': 0.0,
                      'prev_delay_ms': None, 'last_log_wall': 0.0,
                      'max_gap_ms': 0.0, 'tid': tid}
                _PROBE_STATE[instance_name] = st

            # ── Inter-tick GAP detection (KEY for "weird mode") ──────────────
            # gap_ms = wall-clock since this instance's PREVIOUS broadcast call.
            # A healthy streaming instance ticks every ~100ms; a stalled one
            # shows a multi-second gap while OTHER instances keep ticking.
            prev_wall = _PROBE_LAST_WALL.get(instance_name)
            # First tick: no previous wall-clock to measure a gap against. The
            # -1 sentinel is "not yet measurable" — skip the max update so it
            # can't poison max_gap_ms, and log 0 instead of a negative value.
            if prev_wall is None:
                gap_ms = -1.0
            else:
                gap_ms = (now_wall - prev_wall) * 1000.0
                if gap_ms > st['max_gap_ms']:
                    st['max_gap_ms'] = gap_ms
            # Flag a large inter-tick gap (>1.5s) as a potential stall event.
            gap_stall = (gap_ms >= 1500.0)

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
            should_log = backlog or heartbeat or over_thresh or gap_stall

            if should_log:
                avg_ms = st['sum_ms'] / st['count']
                # Rate cap: ~1 line/sec per instance (backlog/gap-stall bypass).
                if not (backlog or gap_stall) and (now_wall - st['last_log_wall']) < 1.0:
                    pass  # suppress this sample to keep output non-spammy
                else:
                    tag = ('BACKLOG ' if backlog else '') + ('GAPSTALL ' if gap_stall else '')
                    _log_probe(
                        f"{tag}inst={instance_name} tid={tid % 100000} "
                        f"yield_to_enqueue_ms={delay_ms:.1f} avg={avg_ms:.1f} max={st['max_ms']:.1f} "
                        f"gap_ms={max(0.0, gap_ms):.0f} max_gap_ms={st['max_gap_ms']:.0f} "
                        f"n={st['count']} resp_len={resp_len} tick={int(is_streaming_tick)} "
                        f"len_chg={int(len_changed)} qsize={qsize}"
                    )
                    st['last_log_wall'] = now_wall

            st['prev_delay_ms'] = delay_ms
            _PROBE_LAST_WALL[instance_name] = now_wall

            # Opportunistic stale-entry eviction (see _PROBE_STALE_SECS). Runs
            # here while already holding _PROBE_LOCK; kept cheap — only when the
            # dict has grown past a small threshold OR at most once per ~30s.
            global _probe_last_cleanup_wall
            if (len(_PROBE_STATE) > _PROBE_CLEANUP_SIZE_THRESHOLD
                    or (now_wall - _probe_last_cleanup_wall) >= _PROBE_CLEANUP_INTERVAL_SECS):
                _probe_last_cleanup_wall = now_wall
                stale_cutoff = now_wall - _PROBE_STALE_SECS
                # Evict instances whose most recent activity is older than the
                # cutoff. Remove from BOTH dicts to keep them consistent.
                for name in list(_PROBE_STATE.keys()):
                    last_activity = max(
                        _PROBE_LAST_WALL.get(name, 0.0),
                        _PROBE_STATE[name].get('last_log_wall', 0.0),
                    )
                    if last_activity < stale_cutoff:
                        del _PROBE_STATE[name]
                        _PROBE_LAST_WALL.pop(name, None)
    except Exception:
        pass  # probe must never break the broadcast path


# ── LLM SSE chunk-cadence probe (gated on STREAM_BACKEND_DEBUG) ──────────────
# Keyed by a PER-STREAM token (uuid) passed from oai._chat_stream, so concurrent
# streams on the SAME model don't interleave/corrupt state. Tracks the
# inter-arrival gap of real SSE chunks so we can tell "LLM not streaming
# incrementally" (one big burst at the end → huge final gap) from "streaming
# fine but broadcast delayed" (steady small gaps). Non-spammy: logs on a large
# gap (>1.5s, bypasses rate cap), every 20th chunk (rate-capped ~1 line/sec),
# and once at stream end via _probe_llm_flush.
_PROBE_LLM_STATE: dict = {}


def _probe_llm_chunk(stream_id: str, model_name: str) -> None:
    """Record one real SSE chunk arrival for the given stream. No-op when disabled."""
    if not STREAM_BACKEND_DEBUG or not stream_id:
        return
    try:
        now_wall = time.time()
        with _PROBE_LOCK:
            st = _PROBE_LLM_STATE.get(stream_id)
            if st is None:
                st = {'count': 0, 'last_wall': None, 'max_gap_ms': 0.0,
                      'sum_gap_ms': 0.0, 'last_log_wall': 0.0, 'model': model_name}
                _PROBE_LLM_STATE[stream_id] = st
            gap_ms = (now_wall - st['last_wall']) * 1000.0 if st['last_wall'] is not None else -1.0
            st['last_wall'] = now_wall
            st['count'] += 1
            if gap_ms > 0:
                st['sum_gap_ms'] += gap_ms
                if gap_ms > st['max_gap_ms']:
                    st['max_gap_ms'] = gap_ms

            big_gap = (gap_ms >= 1500.0)
            periodic = (st['count'] % 20 == 0)
            # Big-gap events are critical signal → always log (bypass rate cap).
            # Periodic heartbeats are rate-capped to ~1 line/sec.
            if big_gap or (periodic and (now_wall - st['last_log_wall']) >= 1.0):
                avg_gap = st['sum_gap_ms'] / max(1, st['count'] - 1)
                _log_probe(
                    f"{'LLMGAP ' if big_gap else ''}llm_chunk stream={stream_id} model={model_name} "
                    f"gap_ms={gap_ms:.0f} avg_gap_ms={avg_gap:.0f} max_gap_ms={st['max_gap_ms']:.0f} "
                    f"chunks={st['count']}"
                )
                st['last_log_wall'] = now_wall
    except Exception:
        pass  # probe must never break the LLM stream path


def _probe_llm_flush(stream_id: str, model_name: str) -> None:
    """Log a final summary for one completed LLM stream, then reset its state."""
    if not STREAM_BACKEND_DEBUG or not stream_id:
        return
    try:
        with _PROBE_LOCK:
            st = _PROBE_LLM_STATE.pop(stream_id, None)
            if st is not None and st['count'] > 0:
                avg_gap = st['sum_gap_ms'] / max(1, st['count'] - 1)
                # A healthy stream has small steady gaps; a burst-at-end shows a
                # huge max_gap with low chunk count. This line is the summary.
                _log_probe(
                    f"LLMDONE stream={stream_id} model={model_name} chunks={st['count']} "
                    f"avg_gap_ms={avg_gap:.0f} max_gap_ms={st['max_gap_ms']:.0f}"
                )
    except Exception:
        pass


def _log_probe(msg: str) -> None:
    """Emit one probe line, guarding against a failed logger (returns None)."""
    lg = _probe_get_logger()
    if lg is not None:
        lg.info(msg)


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

        # Turn index for per-turn breakdown = committed conversation length (grows
        # monotonically across the agent loop). Only computed when timing is on.
        _turn_idx = -1
        if _ST_TIMING_ON:
            try:
                _inst = pool.get_instance(instance_name)
                if _inst is not None:
                    with _inst._compression_lock:
                        _turn_idx = len(_inst.conversation)
            except Exception:
                _turn_idx = -1

        _t0 = _time_streaming.perf_counter() if _ST_TIMING_ON else 0.0
        stream_update = build_stream_update_from_pool(
            pool=pool,
            instance_name=instance_name,
            responses=turn_output,
            force_full=force_full,
        )
        if _ST_TIMING_ON:
            _st_timing_record("broadcast.build", _time_streaming.perf_counter() - _t0, _turn_idx)

        if stream_update is not None:
            # ── TIMING (env-gated): json.dumps proxy + enqueue ───────────────
            # The real json.dumps of this exact dict happens later in api_server's
            # _sender_loop/broadcast; we can't instrument that without editing it, so we
            # time a faithful proxy here (same object, same size) to attribute the
            # serialization cost. Zero overhead when AGENT_CASCADE_STREAM_TIMING is off.
            if _ST_TIMING_ON:
                import json as _json_timing
                _t1 = _time_streaming.perf_counter()
                try:
                    _json_timing.dumps({'type': 'stream_update', **stream_update}, default=str)
                except Exception:
                    pass
                _st_timing_record("broadcast.json_dumps", _time_streaming.perf_counter() - _t1, _turn_idx)

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

            _t2 = _time_streaming.perf_counter() if _ST_TIMING_ON else 0.0
            asyncio.run_coroutine_threadsafe(
                _put_stream_update(
                    ws_queue,
                    {'type': 'stream_update', **stream_update},
                ),
                ws_loop,
            )
            if _ST_TIMING_ON:
                _st_timing_record("broadcast.enqueue", _time_streaming.perf_counter() - _t2, _turn_idx)

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

    Computes h_stats over the committed conversation (no streaming partial) and r_stats
    over the in-flight streaming partial — two DISJOINT sets, so their sum is an exact,
    non-double-counted total_tokens. Results are cached keyed by instance_name for reuse
    during active generation (the caller recomputes only the cheap r_stats per tick).

    Returns:
        (h_stats, r_stats) tuple of dicts with 'tokens' and 'words' keys.
    """
    h_stats, r_stats = _calc_stream_token_stats_uncached(
        pool, conv_snapshot, stream_resp_snapshot, responses,
    )

    # Cache the computed stats for reuse during active generation
    _cache_mgr.evict_if_full('stream_token_stats', _STREAM_TOKEN_STATS_CACHE_MAXSIZE)
    with _cache_mgr._lock:
        _cache_mgr.stream_token_stats[instance_name] = (h_stats, r_stats)

    return h_stats, r_stats


def _calc_stream_token_stats_uncached(
    pool: AgentPool,
    conv_snapshot: List[Message], stream_resp_snapshot: Optional[List[Message]],
    responses: Optional[List[Message]],
) -> tuple:
    """Pure computation of (h_stats, r_stats) WITHOUT touching the cache.

    Split out from _calc_stream_token_stats so the caller can recompute only the cheap
    streaming partial (r_stats = get_history_stats(stream_resp_snapshot)) on a per-tick
    basis while reusing a cached h_stats keyed on stable conversation identity.

    BUG_0004 / O(N)-per-tick: h_stats is computed over `conv_snapshot` ONLY (committed
    history, no streaming partial) and r_stats over `stream_resp_snapshot` ONLY (the small
    in-flight partial). The two sets are disjoint, so total_tokens = h + r is exact with no
    double-counting, and r_stats stays O(tail) rather than O(full-conversation).

    Returns:
        (h_stats, r_stats) tuple of dicts with 'tokens' and 'words' keys.
    """
    # BUG_0004 / O(N)-per-tick fix: compute h_stats and r_stats over DISJOINT sets so that
    # total_tokens = h_stats + r_stats is exact with no double-counting of the streaming partial.
    #   - h_stats  = committed history ONLY (no streaming partial) — stable within a turn, cacheable
    #   - r_stats  = in-flight streaming partial ONLY — small, O(tail), recomputed fresh each tick
    # Previously h_stats was computed over `conv + stream_partial` and r_stats over the same
    # partial (via `responses`, which is the engine's full "current view"), so the partial was
    # counted twice AND r_stats ran at O(full-conversation) every tick.
    active_h = pool.slice_history_for_llm(conv_snapshot) if conv_snapshot else []

    try:
        from agent_cascade.utils.utils import get_history_stats
        h_stats = get_history_stats(active_h)
        r_stats = get_history_stats(stream_resp_snapshot) if stream_resp_snapshot else {'tokens': 0, 'words': 0}
    except Exception as e:
        logger.debug(f"Token stats calculation failed for stream update (using estimate): {e}")
        h_stats = {'tokens': len(active_h) * 4, 'words': 0}
        r_stats = {'tokens': 0, 'words': 0}

    return h_stats, r_stats
