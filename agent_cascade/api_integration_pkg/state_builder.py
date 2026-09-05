"""Pool → UI state serialization (moved verbatim from api_integration.py).

Phase 3b pure-move refactor. Imports the SAME ``_cache_mgr`` singleton from cache.py.
"""

import copy as _copy
from typing import Any, Dict, List, Optional

from agent_cascade.log import logger
from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.agent_pool import AgentPool
from agent_cascade.constants import POOL_SETTINGS_TO_BROADCAST
from agent_cascade.llm.schema import ASSISTANT, CONTENT, NAME, REASONING_CONTENT, ROLE, SYSTEM, USER, Message
from agent_cascade.api_integration_pkg.cache import (
    _cache_mgr,
    _TOKEN_STATS_CACHE_MAXSIZE,
    _UI_CACHE_MAXSIZE,
    _store_ui_cache,
)
from agent_cascade.api_integration_pkg.tokens import _get_max_tokens_for_instance

# ────────────────────────────────────────────────────────────────────────────
# Per-op streaming tick timing — env-gated diagnostic (AGENT_CASCADE_STREAM_TIMING=1).
# Zero overhead when off: _stream_time() early-returns and the `_t0 = perf_counter() if ENABLED`
# guards mean no counter calls in the disabled path. Used for latency regression
# detection in CI and production debugging. See
# .agent_lessons/streaming-per-tick-timing-instrumentation.md.
# ────────────────────────────────────────────────────────────────────────────
import os as _os_stream_timing
import time as _time_stream_timing

_STREAM_TIMING_ENABLED = _os_stream_timing.environ.get("AGENT_CASCADE_STREAM_TIMING") == "1"

# ────────────────────────────────────────────────────────────────────────────
# Tail-only streaming serialization (WS send-queue backpressure fix).
#
# Re-sending the whole conversation every tick made per-frame payload bytes grow
# linearly with history, which slowed `broadcast.send_text` (17ms→121ms as the
# frame grew 64KB→480KB) and saturated the bounded WS send queue (maxsize=128),
# so frames waited 9-15s to drain. For normal ticks we now serialize only the
# last _STREAM_TAIL_SIZE committed messages + the in-flight streaming partial;
# every ~100th tick (force_full) still sends the FULL state as a resync safety net.
# The frontend already splices the tail via startIdx = history_count - len(messages).
# Env-overridable for tuning (e.g. larger if UI context is too short).
# ────────────────────────────────────────────────────────────────────────────
_STREAM_TAIL_SIZE = int(_os_stream_timing.environ.get("AGENT_CASCADE_STREAM_TAIL", "20"))
# Byte budget for the serialized committed-message tail (excluding streaming partial).
# If the last _STREAM_TAIL_SIZE messages exceed this, reduce to fewer messages until
# under budget. This guarantees bounded per-frame WS payload regardless of content growth,
# preventing send-queue backpressure. 15KB → ~3ms send_text on localhost; leaves room
# for the streaming partial + metadata within a ~20KB total frame target.
_STREAM_TAIL_BYTE_BUDGET = int(_os_stream_timing.environ.get("AGENT_CASCADE_STREAM_TAIL_BYTES", "15360"))
_stream_op_timings: dict = {}   # op -> [count, total_seconds]
# Per-turn breakdown (turn index = len(active instance conversation), which grows
# monotonically across the agent loop). Lets us see how each op's cost scales with
# conversation length. turn -> {op: [count, total_s]}.
_stream_turn_timings: dict = {}


def _stream_time(op: str, dt: float, turn: int = -1) -> None:
    """Accumulate one sample for `op`. No-op (early return) when timing is disabled.

    When `turn >= 0`, also bucket the sample into a per-turn breakdown so the test can
    report how each op's cost grows with conversation length (the O(N) question).
    """
    if not _STREAM_TIMING_ENABLED:
        return
    try:
        rec = _stream_op_timings.get(op)
        if rec is None:
            _stream_op_timings[op] = [1, dt]
        else:
            rec[0] += 1
            rec[1] += dt
        if turn >= 0:
            tb = _stream_turn_timings.setdefault(turn, {})
            trec = tb.get(op)
            if trec is None:
                tb[op] = [1, dt]
            else:
                trec[0] += 1
                trec[1] += dt
    except Exception:
        pass


def dump_stream_timings() -> dict:
    """Return {op: (count, total_s, avg_ms)} sorted by total descending.

    Returns {} when AGENT_CASCADE_STREAM_TIMING is unset (the test hook at
    tests/test_streaming_fullstack_e2e.py:903 prints a "(disabled...)" note then).
    """
    if not _STREAM_TIMING_ENABLED:
        return {}
    out = {}
    for op, (cnt, tot) in _stream_op_timings.items():
        avg_ms = (tot / cnt * 1000.0) if cnt else 0.0
        out[op] = (cnt, tot, avg_ms)
    return dict(sorted(out.items(), key=lambda kv: kv[1][1], reverse=True))


def dump_stream_timings_per_turn() -> dict:
    """Return {turn: {op: (count, total_s, avg_ms)}} for the per-turn breakdown.

    Returns {} when timing is disabled. turn = len(active instance conversation) at the
    time of each build (grows monotonically across the agent loop).
    """
    if not _STREAM_TIMING_ENABLED:
        return {}
    out = {}
    for turn, ops in _stream_turn_timings.items():
        out[turn] = {op: (cnt, tot, (tot / cnt * 1000.0) if cnt else 0.0) for op, (cnt, tot) in ops.items()}
    return dict(sorted(out.items()))


# Per-turn send-queue depth samples (diagnostic): turn -> [count, sum, max]. Lets the
# test report whether the bounded send_queue (maxsize=128) is building up backpressure.
_stream_qdepth_turns: dict = {}


def record_send_queue_depth(depth: int, turn: int = -1) -> None:
    """Record one send-queue depth sample, bucketed by turn. No-op when timing is off."""
    if not _STREAM_TIMING_ENABLED:
        return
    try:
        # Opportunistic bound so a long-running server doesn't grow this dict unbounded.
        if len(_stream_qdepth_turns) > 512:
            for _k in list(_stream_qdepth_turns):
                del _stream_qdepth_turns[_k]
        rec = _stream_qdepth_turns.get(turn)
        if rec is None:
            _stream_qdepth_turns[turn] = [1, depth, depth]
        else:
            rec[0] += 1
            rec[1] += depth
            if depth > rec[2]:
                rec[2] = depth
    except Exception:
        pass


def dump_queue_depths() -> dict:
    """Return {turn: (count, avg_depth, max_depth)}. {} when timing is disabled."""
    if not _STREAM_TIMING_ENABLED:
        return {}
    out = {}
    for turn, (cnt, tot, mx) in _stream_qdepth_turns.items():
        out[turn] = (cnt, (tot / cnt if cnt else 0.0), mx)
    return dict(sorted(out.items()))


# Per-turn payload byte breakdown (diagnostic): turn -> {field: [count, sum_bytes, max_bytes]}.
# Lets the e2e test answer "which field of the stream_update frame is growing with conv_len?"
# after the tail-only serialization fix. Recorded in api_server.broadcast() right after json.dumps.
_stream_payload_turns: dict = {}


def record_payload_breakdown(turn: int, total: int, instances: int,
                             primary_msgs: int, msg_count: int) -> None:
    """Record one frame's byte breakdown, bucketed by turn (conv_len). No-op when timing is off.

    Fields recorded per turn (each [count, sum_bytes, max_bytes]):
      - total        : len(json.dumps(full frame)) — the on-wire payload size.
      - instances    : bytes of json.dumps(data['instances']) (ALL serialized instances).
      - primary_msgs : bytes of the PRIMARY instance's messages list only (tail + streaming partial).
      - other        : max(0, total - instances) — everything NOT in the 'instances' sub-dict:
                       top-level non-instance fields (active_stack, pool_settings, telemetry, generating,
                       tokens, current_model, ...) PLUS JSON structural overhead (the 'agent_instances'
                       alias key name, separators, braces). It is a residual, not an exact field sum.
    msg_count is the number of serialized messages in the primary instance (should be <= tail+1 if
    the tail cut is active; == conv_len+1 means FULL history was sent — the cut is inert for that frame).
    """
    if not _STREAM_TIMING_ENABLED:
        return
    try:
        # Opportunistic bound (same pattern as record_send_queue_depth above): wipe-all once past
        # 512 buckets rather than FIFO-evict. Intentional — this diagnostic is read+dumped at the end
        # of a test run, and we want to KEEP the early low-conv_len buckets (the trend-line baseline),
        # so evicting oldest would drop exactly the data we need. A long-running server that exceeds
        # 512 distinct conv_len values is out of scope for this throwaway measurement.
        if len(_stream_payload_turns) > 512:
            for _k in list(_stream_payload_turns):
                del _stream_payload_turns[_k]
        tb = _stream_payload_turns.setdefault(turn, {})

        def _rec(field, val):
            r = tb.get(field)
            if r is None:
                tb[field] = [1, val, val]
            else:
                r[0] += 1
                r[1] += val
                if val > r[2]:
                    r[2] = val

        _rec("total", total)
        _rec("instances", instances)
        _rec("primary_msgs", primary_msgs)
        _rec("other", max(0, total - instances))
        _rec("msg_count", msg_count)
    except Exception:
        pass


def dump_payload_breakdowns() -> dict:
    """Return {turn: {field: (count, avg_bytes, max_bytes)}}. {} when timing is disabled."""
    if not _STREAM_TIMING_ENABLED:
        return {}
    out = {}
    for turn, fields in _stream_payload_turns.items():
        out[turn] = {
            f: (cnt, (tot / cnt if cnt else 0.0), mx)
            for f, (cnt, tot, mx) in fields.items()
        }
    return dict(sorted(out.items()))


def _serialize_loop_settings(ps):
    """Serialize loop detection settings from PoolSettings instance."""
    return {
        'loop_min_chars': getattr(ps, 'loop_min_chars', 4000),
        'loop_max_chars': getattr(ps, 'loop_max_chars', 40960),
        'loop_char_run_limit': getattr(ps, 'loop_char_run_limit', 129),
        'loop_char_run_enabled': getattr(ps, 'loop_char_run_enabled', True),
        'loop_max_chars_enabled': getattr(ps, 'loop_max_chars_enabled', True),
        'loop_two_phase_enabled': getattr(ps, 'loop_two_phase_enabled', False),
        'loop_suspicion_threshold': getattr(ps, 'loop_suspicion_threshold', 7),
        'loop_confirm_required': getattr(ps, 'loop_confirm_required', 3),
        'loop_cooldown_feeds': getattr(ps, 'loop_cooldown_feeds', 50),
    }

def _get_instance_messages(pool: AgentPool, instance_name: str,
                           responses: Optional[List[Message]] = None) -> List[Message]:
    """Get messages list from pool instance, extending with optional responses."""
    instance = pool.get_instance(instance_name)
    if instance is None:
        return []
    with instance._compression_lock:
        msgs = list(instance.conversation)
    if responses:
        msgs.extend(responses)
    return msgs

def _calc_token_stats(pool: AgentPool, full_conversation: List[Message],
                      partial_responses: Optional[List[Message]] = None) -> tuple:
    """Calculate h_stats and r_stats for a message list with error handling.
    
    Args:
        pool: The AgentPool (used for slice_history_for_llm).
        full_conversation: Complete conversation messages (used for h_stats via slicing).
        partial_responses: Current partial response messages from engine (for r_stats).
        
    Returns:
        (h_stats, r_stats) tuple of dicts with 'tokens' and 'words' keys.
    """
    active_h = pool.slice_history_for_llm(full_conversation) if full_conversation else full_conversation
    
    try:
        from agent_cascade.utils.utils import get_history_stats
        h_stats = get_history_stats(active_h)
        r_stats = get_history_stats(partial_responses) if partial_responses else {'tokens': 0, 'words': 0}
    except Exception as e:
        logger.debug(f"Token stats calculation failed (using estimate): {e}")
        h_stats = {'tokens': len(active_h) * 4, 'words': 0}
        r_stats = {'tokens': 0, 'words': 0}
    return h_stats, r_stats

def _calc_stream_r_stats(partial_responses: Optional[List[Message]]) -> dict:
    """Compute token stats for ONLY the streaming partial (r_stats).

    Fix B helper: during a turn's streaming the committed conversation is unchanged, so
    the expensive history stats (h_stats) are cached and reused across ticks. Only this
    small in-flight partial changes each tick — computing it fresh keeps total_tokens
    live at O(tail) cost without touching the full conversation.

    Returns:
        dict with 'tokens' and 'words' keys ({0, 0} when there is no partial).
    """
    if not partial_responses:
        return {'tokens': 0, 'words': 0}
    try:
        from agent_cascade.utils.utils import get_history_stats
        return get_history_stats(partial_responses)
    except Exception as e:
        logger.debug(f"Token stats calculation failed for streaming partial (using estimate): {e}")
        return {'tokens': len(partial_responses) * 4, 'words': 0}

def _serialize_all_instances(pool: AgentPool, instance_snapshot: Dict[str, Any],
                              streaming: bool = False,
                              max_messages: Optional[int] = None) -> Dict[str, dict]:
    """Serialize all instances in a pool snapshot.
    
    Args:
        pool: The AgentPool managing all instances.
        instance_snapshot: Snapshot of pool.instances for safe iteration.
        streaming: If True, uses tail optimization within each instance's 
            _serialize_instance call (partial messages only).
        max_messages: When set, limits committed messages per instance to the last N
            (tail-only). None = full history (backward-compatible default).
    """
    all_instances = {}
    for name, inst in instance_snapshot.items():
        with inst._compression_lock:
            inst_streaming = list(inst._streaming_responses) if len(inst._streaming_responses) > 0 else None
        all_instances[name] = _serialize_instance(
            inst, pool, include_messages=True, streaming=streaming,
            streaming_responses=inst_streaming,
            max_messages=max_messages,
        )
    return all_instances

def _get_session_name(instance_snapshot: Dict[str, Any], fallback: str) -> str:
    """Derive session name from root instances (first parentless instance)."""
    root_instances = [
        name for name, inst in instance_snapshot.items()
        if inst.parent_instance is None
    ]
    return root_instances[0] if root_instances else fallback

def _get_current_model(pool: AgentPool, instance: AgentInstance) -> str:
    """Get the current model name from the instance's template LLM."""
    template = pool.get_template(instance.agent_class)
    if template and hasattr(template, 'llm') and template.llm:
        return getattr(template.llm, 'model', 'Unknown')
    return 'Unknown'

def _safe_get_telemetry(pool: AgentPool, instance_name: str) -> Optional[dict]:
    """Get telemetry summary for an instance (never blocks state building)."""
    if hasattr(pool, 'telemetry') and pool.telemetry:
        try:
            return pool.telemetry.get_session_summary()
        except Exception as e:
            logger.debug(f"Telemetry summary fetch failed for {instance_name} (non-critical): {e}")
    return None

def _safe_get_api_router_state(pool: AgentPool) -> dict:
    """Get API router state dict (never blocks state building)."""
    if hasattr(pool, 'api_router') and pool.api_router:
        try:
            return pool.api_router.to_dict()
        except Exception as e:
            logger.debug(f"API router state serialization failed (using empty): {e}")
    return {'endpoints': [], 'agent_priorities': {}}

def _get_default_workspace(pool: AgentPool) -> str:
    """Get default workspace path from pool or settings."""
    from agent_cascade.settings import DEFAULT_WORKSPACE
    default_workspace = str(DEFAULT_WORKSPACE)
    if pool and hasattr(pool, 'operation_manager') and pool.operation_manager:
        default_workspace = str(pool.operation_manager.base_dir)
    return default_workspace

def _build_active_stack(pool: AgentPool) -> list:
    """Get the active execution stack from the pool.

    Thread-safe: uses the locked pool.active_stack property (which reads under
    _execution._state_lock and returns a defensive copy). This may run in a
    worker thread (build_state offloaded via run_in_executor) while execution
    threads mutate active_stack concurrently.
    """
    if hasattr(pool, 'active_stack'):
        return list(pool.active_stack)
    if hasattr(pool, '_execution'):
        lock = getattr(pool._execution, '_state_lock', None)
        if lock is not None:
            with lock:
                return list(pool._execution.active_stack)
        return list(pool._execution.active_stack)
    return []

def _get_msg_content(m):
    """Get content from a Message object or dict."""
    if isinstance(m, dict):
        return m.get(CONTENT, '') or ''
    return getattr(m, CONTENT, '') or ''

def _get_msg_reasoning(m):
    """Get reasoning_content from a Message object or dict."""
    if isinstance(m, dict):
        return m.get(REASONING_CONTENT, '') or ''
    return getattr(m, REASONING_CONTENT, '') or ''

def _serialize_instances_incremental(
    pool: AgentPool, instance_name: str, force_full: bool,
) -> Dict[str, dict]:
    """Serialize all instances with incremental version-based deduplication.
    
    Only re-serializes instances whose conversation has changed since the last
    stream_update. Version is derived from (msg_count, id_of_last_msg, 
    streaming_response_len). During LLM streaming, the conversation
    doesn't change so most instances are skipped.
    
    Every ~100 ticks (force_full=True) all instances are fully re-serialized to
    recover from sync gaps where individual stream_update messages may have been
    dropped due to queue-full conditions.
    """
    instance_snapshot_data = dict(pool.instances)
    all_instances = {}
    
    for name, inst in instance_snapshot_data.items():
        with inst._compression_lock:
            current_msgs = list(inst.conversation)
            inst_streaming_responses = (
                list(inst._streaming_responses) if len(inst._streaming_responses) > 0 else None
            )
        
        # Calculate content length for this instance's streaming responses (used for version tracking).
        # Include total character count so that growing streaming content invalidates the cache
        # even when message count stays at 1 (single partial response being accumulated).
        stream_content_len = sum(
            len(_get_msg_content(m)) + len(_get_msg_reasoning(m))
            for m in inst_streaming_responses
        ) if inst_streaming_responses else 0
        
        current_version = (
            len(current_msgs),
            id(current_msgs[-1]) if current_msgs else None,
            len(inst_streaming_responses) if inst_streaming_responses else 0,
            stream_content_len,
        )

        # C4: Atomic read-compare-write under lock to prevent TOCTOU race.
        # Lock is acquired per-instance inside the loop — this allows concurrent
        # instance dismissal (evict_instance) between iterations, which is correct
        # but may cause re-serialization of dismissed instances. Acceptable trade-off
        # since RLock prevents deadlocks and worst case is a slightly stale snapshot.
        with _cache_mgr._lock:
            prev_version = _cache_mgr.stream_versions.get(name)
            
            # Serialize if: active instance OR version changed OR forced full refresh
            # Tail-only for normal ticks; FULL state on the periodic resync (force_full).
            _tail = None if force_full else _STREAM_TAIL_SIZE
            if name == instance_name or current_version != prev_version or force_full:
                all_instances[name] = _serialize_instance(
                    inst, pool, include_messages=True,
                    streaming=(not force_full),
                    streaming_responses=inst_streaming_responses,
                    max_messages=_tail,
                )
                _cache_mgr.stream_versions[name] = current_version
                _cache_mgr.cached_instances[name] = all_instances[name]
            else:
                # Reuse the previously serialized data for unchanged instances
                all_instances[name] = _cache_mgr.cached_instances.get(name)
                if all_instances[name] is None:
                    all_instances[name] = _serialize_instance(
                        inst, pool, include_messages=True,
                        streaming=(not force_full),
                        streaming_responses=inst_streaming_responses,
                        max_messages=_tail,
                    )
                    _cache_mgr.stream_versions[name] = current_version
                    _cache_mgr.cached_instances[name] = all_instances[name]
    
    return all_instances

def _add_pool_runtime_settings(pool: Any, pool_settings: dict) -> None:
    """Add runtime pool settings (approval timeout, async shell console window) to pool_settings dict.

    Shared by build_state_from_pool and build_stream_update_from_pool to avoid duplication.
    """
    # Approval timeout settings from operation_manager if available
    if hasattr(pool, 'operation_manager') and pool.operation_manager:
        om = pool.operation_manager
        pool_settings['approval_timeout_seconds'] = getattr(om, 'approval_timeout_seconds', 300)
        pool_settings['enable_approval_timeout'] = getattr(om, 'enable_timeout', True)

    # Async shell console window toggle from pool if available
    pool_settings['enable_async_shell_console_window'] = getattr(pool, '_enable_async_shell_console_window', False)

def build_state_from_pool(
    pool: AgentPool,
    instance_name: str,
    responses: Optional[List[Message]] = None,
    generating: bool = False,
    streaming: bool = False,  # Controls tail optimization for large conversations
    max_messages: Optional[int] = None,  # Tail-only limit (None = full history)
) -> Optional[Dict[str, Any]]:
    """Build a full state snapshot for the frontend directly from the pool.

    Replaces build_state() which read from session['history']. In the unified model,
    ALL state comes from pool.instances[name].conversation.

    Takes a snapshot of pool.instances to avoid RuntimeError during concurrent
    agent add/remove (C3 fix from DESIGN_REWRITE §4.2).

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the primary instance (main agent) for this state.
        responses: Optional current partial response messages to include.
        generating: Whether the agent is currently generating.
        streaming: If True, includes in-flight streaming responses (partial LLM content)
            in the serialized state. If False, only committed messages are included.

    Returns:
        Dictionary with full state snapshot, or None if instance not found.

    Example:
        # Full state for initial broadcast (includes all messages)
        state = build_state_from_pool(pool, "Maine", generating=True, streaming=False)
        await websocket.send(json.dumps(state))
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        return None

    # Build messages list and calculate token stats via helpers
    msgs = _get_instance_messages(pool, instance_name, responses)
    h_stats, r_stats = _calc_token_stats(pool, msgs, responses)

    # Get max tokens via module-level helper (avoids creating ExecutionEngine instance)
    max_tokens = _get_max_tokens_for_instance(pool, instance)

    # Extract compression summary from conversation markers
    current_summary = instance.compression_summary or ""

    # Build sub-agent state snapshot (C3: take snapshot before iterating)
    instance_snapshot = dict(pool.instances)
    all_instances = _serialize_all_instances(pool, instance_snapshot, streaming=streaming, max_messages=max_messages)

    # Derive session name from root instance
    session_name = _get_session_name(instance_snapshot, instance_name)

    # Build active stack
    active_stack = _build_active_stack(pool)

    # Build agents list for UI (from templates — the canonical source of agent definitions)
    agents_list = _build_agents_list(pool)

    # Get current model, telemetry, workspace, API router state via helpers
    current_model = _get_current_model(pool, instance)
    telemetry_data = _safe_get_telemetry(pool, instance_name)
    default_workspace = _get_default_workspace(pool)
    api_router_state = _safe_get_api_router_state(pool)

    # Check if instance is waiting (endpoint slot blocked)
    is_waiting = _check_is_waiting(pool, instance_name)

    # Get pending approvals (only include if non-empty to prevent UI flickering)
    pending_approvals = _get_approvals(pool)

    # Extract pool settings for frontend sync (ALL non-cosmetic persistent settings)
    pool_settings = {}
    if hasattr(pool, 'settings'):
        ps = pool.settings
        pool_settings.update({
            # Core pool/agent settings
            'idle_timeout_seconds': getattr(ps, 'idle_timeout_seconds', 900.0),
            'system_agent_idle_timeout_seconds': getattr(ps, 'system_agent_idle_timeout_seconds', 900.0),
            'max_parallel_agents': getattr(ps, 'max_workers', 10),
            'auto_continue': getattr(ps, 'auto_continue', True),
            'enable_agent_budgeting': getattr(ps, 'enable_agent_budgeting', True),
            'max_turns': getattr(ps, 'max_turns', 50),
            'max_auto_rollbacks': getattr(ps, 'max_auto_rollbacks', 3),
            'auto_rollback_on_loop': getattr(ps, 'auto_rollback_on_loop', True),
            # Two-tier loop detection (2026-08 redesign)
            'loop_exact_rollback_enabled': getattr(ps, 'loop_exact_rollback_enabled', True),
            'loop_fuzzy_warning_enabled': getattr(ps, 'loop_fuzzy_warning_enabled', True),
            'tool_loop_fuzzy_rollback_enabled': getattr(ps, 'tool_loop_fuzzy_rollback_enabled', False),
            # DEPRECATED: legacy kill switch for the fuzzy tier (can disable but never enable)
            'tool_loop_detection_enabled': getattr(ps, 'tool_loop_detection_enabled', True),
            # Inner-loop detection
            **{'inner_loop_detect_enabled': getattr(ps, 'inner_loop_detect_enabled', False)},
            **_serialize_loop_settings(ps),
            # Skills system
            'default_load_skill_mode': getattr(ps, 'default_load_skill_mode', 'AUTO'),
            'auto_skill_enabled': getattr(ps, 'auto_skill_enabled', True),
            'auto_skill_mode': getattr(ps, 'auto_skill_mode', 'basic'),
            # Retry policy settings (Phase 6)
            'retry_max_attempts': getattr(ps, 'retry_max_attempts', 3),
            'endpoint_max_retries': getattr(ps, 'endpoint_max_retries', 1),
            'retry_base_delay': getattr(ps, 'retry_base_delay', 1.0),
            'retry_max_delay': getattr(ps, 'retry_max_delay', 8.0),
            # Code interpreter
            'ci_execution_timeout': getattr(ps, 'ci_execution_timeout', 120),
            'ci_watchdog_timeout': getattr(ps, 'ci_watchdog_timeout', 300),
            'ci_stale_container_ttl': getattr(ps, 'ci_stale_container_ttl', 1200),
            # Cache pool
            'cache_pool_enabled': getattr(ps, 'cache_pool_enabled', True),
            'cache_pool_size': getattr(ps, 'cache_pool_size', 64),
            'cache_threshold_chars': getattr(ps, 'cache_threshold_chars', 1000),
            # Streaming timeout settings
            'stream_max_silence_seconds': getattr(ps, 'stream_max_silence_seconds', 120.0),
            'stream_max_total_seconds': getattr(ps, 'stream_max_total_seconds', 900.0),
        })

    # Add tool char limits from pool.llm_cfg if available
    if hasattr(pool, 'llm_cfg'):
        for key in POOL_SETTINGS_TO_BROADCAST:
            if key in pool.llm_cfg:
                pool_settings[key] = pool.llm_cfg[key]

    # Add runtime pool settings (approval timeout, async shell console window)
    _add_pool_runtime_settings(pool, pool_settings)

    # Add disabled_tools from live cache if available
    if hasattr(pool, '_ui_disabled_tools') and pool._ui_disabled_tools:
        try:
            with pool._ui_disabled_tools_lock:
                if pool._ui_disabled_tools:
                    pool_settings['disabled_tools'] = dict(pool._ui_disabled_tools)
        except Exception:
            pass  # Don't let lock issues break state broadcast

    # Add work folders and default workspace from operation_manager
    if hasattr(pool, 'operation_manager') and pool.operation_manager:
        om = pool.operation_manager
        pool_settings['work_access_folders_ro'] = [str(p) for p in om.extra_work_folders_ro]
        pool_settings['work_access_folders_rw'] = [str(p) for p in om.extra_work_folders_rw]
        pool_settings['default_workspace'] = str(om.base_dir)

    return {
        # Kept for backward compat — frontend fallback reads data.messages if root not in agent_instances
        'messages': [serialize_message(m, i) for i, m in enumerate(msgs)],
        'instances': all_instances,
        'agent_instances': all_instances,
        'active_stack': active_stack,
        'approvals': pending_approvals,
        'generating': generating,
        'session_name': session_name,
        'instance_name': instance_name,
        'total_tokens': h_stats['tokens'] + r_stats['tokens'],
        'total_words': h_stats['words'] + r_stats['words'],
        'max_tokens': max_tokens,
        'summary': current_summary,
        'has_queued_messages': pool.has_messages(instance_name),
        'queued_messages': pool.get_queue_messages(instance_name) if pool else [],
        'stopped': pool.stopped,
        'paused': pool.is_paused(),  # Pause state for frontend "Paused" indicator
        # Extra fields for frontend display
        'agents': agents_list,
        'current_model': current_model,
        'telemetry': telemetry_data,
        'default_workspace': default_workspace,
        'is_waiting': is_waiting,
        'api_router': api_router_state,
        'pool_settings': pool_settings,
    }

def build_stream_update_from_pool(
    pool: AgentPool,
    instance_name: str,
    responses: Optional[List[Message]] = None,
    force_full: bool = False,
) -> Optional[Dict[str, Any]]:
    """Build a lightweight streaming delta directly from the pool.

    Replaces build_stream_update() which read from session['history']. Only
    serializes the changing response messages - history is already on the client.

    Includes sub_agents, current_model, and telemetry fields to match the frontend expected output format.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the primary instance for this stream.
        responses: Current partial response messages from the engine.
        force_full: If True, serialize all instances with full state (streaming=False)
            to recover from sync gaps. Used periodically (~every 100 ticks) to ensure
            any missed partial messages are recovered.

    Returns:
        Dictionary with streaming delta, or None if instance not found.

    Example:
        for messages in run_agent_in_pool(pool, "Maine"):
            delta = build_stream_update_from_pool(pool, "Maine", messages)
            await websocket.send(json.dumps(delta))
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        return None

    # Get active working set for token stats (single snapshot)
    with instance._compression_lock:
        conv_snapshot = list(instance.conversation)
        stream_resp_snapshot = list(instance._streaming_responses) if instance._streaming_responses else None
    
    # Fix B: key token-stats caching on STABLE conversation identity only.
    # The version uses (msg_count, id_of_last_msg, streaming_response_COUNT) — deliberately
    # NOT the growing content length. During a turn's streaming the committed conversation
    # is unchanged, so h_stats (stats over the full stable history) can be cached and reused
    # across ticks instead of recomputing two full get_history_stats() passes per tick
    # (the O(N)-per-tick cost that made streaming latency grow with turn count).
    current_version = (
        len(conv_snapshot),
        id(conv_snapshot[-1]) if conv_snapshot else None,
        len(stream_resp_snapshot) if stream_resp_snapshot else 0,
    )

    # Thread-safe read of cached token stats and last version via CacheManager.
    # BUG_0005 follow-up: Use a SEPARATE version dict for token-stats caching, because
    # _serialize_instances_incremental writes stream_versions with a 4-tuple (including
    # stream_content_len) while build_stream_update_from_pool uses a 3-tuple (without it).
    # Sharing the same dict caused permanent cache misses → O(N) recompute every tick.
    with _cache_mgr._lock:
        cached_stats = _cache_mgr.stream_token_stats.get(instance_name)
        last_version = _cache_mgr.stream_token_stats_versions.get(instance_name)

    # Turn index for per-turn timing breakdown = committed conversation length (grows
    # monotonically across the agent loop, so it tracks turn count). Only computed when
    # timing is on; zero overhead otherwise.
    _turn_idx = len(conv_snapshot) if _STREAM_TIMING_ENABLED else -1

    if cached_stats is not None and current_version == last_version:
        # Conversation unchanged — reuse the cached STABLE history stats (h_stats).
        h_stats = cached_stats[0]
        # Recompute ONLY the streaming partial (r_stats) fresh each tick so total_tokens
        # stays live as the partial grows. This is cheap/O(tail): it only touches the small
        # in-flight response, never the full conversation.
        #
        # BUG_0004 / O(N)-per-tick fix: use `stream_resp_snapshot` (the actual in-flight
        # streaming partial read from the instance) instead of `responses`. The engine yields
        # `response + turn_output + partial_msgs` as the "current view", so `responses` is the
        # FULL conversation — passing it here made r_stats O(full-conversation) every tick.
        _t0 = _time_stream_timing.perf_counter() if _STREAM_TIMING_ENABLED else 0.0
        r_stats = _calc_stream_r_stats(stream_resp_snapshot)
        if _STREAM_TIMING_ENABLED:
            _stream_time("build.token_stats_r", _time_stream_timing.perf_counter() - _t0, _turn_idx)
    else:
        # Lazy import to avoid a module-level circular dependency: streaming.py
        # imports build_stream_update_from_pool from this module (state_builder).
        from agent_cascade.api_integration_pkg.streaming import _calc_stream_token_stats
        _t0 = _time_stream_timing.perf_counter() if _STREAM_TIMING_ENABLED else 0.0
        h_stats, r_stats = _calc_stream_token_stats(
            pool, instance_name, conv_snapshot, stream_resp_snapshot, responses,
        )
        if _STREAM_TIMING_ENABLED:
            _stream_time("build.token_stats_h", _time_stream_timing.perf_counter() - _t0, _turn_idx)
        # BUG_0005 follow-up: Store the version in the separate token-stats version dict
        # so subsequent ticks with the same conversation identity can hit the cache.
        with _cache_mgr._lock:
            _cache_mgr.stream_token_stats_versions[instance_name] = current_version

    # Get max tokens via module-level helper (avoids creating ExecutionEngine instance)
    max_tokens = _get_max_tokens_for_instance(pool, instance)

    # Build active stack
    active_stack = _build_active_stack(pool)

    # Build ALL instances snapshot with incremental serialization (Fix #3)
    _t0 = _time_stream_timing.perf_counter() if _STREAM_TIMING_ENABLED else 0.0
    all_instances = _serialize_instances_incremental(
        pool, instance_name, force_full,
    )
    if _STREAM_TIMING_ENABLED:
        _stream_time("build.serialize_all", _time_stream_timing.perf_counter() - _t0, _turn_idx)

    # Get current model and telemetry via shared helpers
    current_model = _get_current_model(pool, instance)
    telemetry_data = _safe_get_telemetry(pool, instance_name)

    # NOTE: pending approvals are intentionally NOT computed/included in stream updates.
    # See the return-dict comment below for why the 'approvals' field is omitted here.

    # Extract pool settings for frontend sync (ALL non-cosmetic persistent settings)
    pool_settings = {}
    if hasattr(pool, 'settings'):
        ps = pool.settings
        pool_settings.update({
            # Core pool/agent settings
            'idle_timeout_seconds': getattr(ps, 'idle_timeout_seconds', 900.0),
            'system_agent_idle_timeout_seconds': getattr(ps, 'system_agent_idle_timeout_seconds', 900.0),
            'max_parallel_agents': getattr(ps, 'max_workers', 10),
            'auto_continue': getattr(ps, 'auto_continue', True),
            'enable_agent_budgeting': getattr(ps, 'enable_agent_budgeting', True),
            'max_turns': getattr(ps, 'max_turns', 50),
            'max_auto_rollbacks': getattr(ps, 'max_auto_rollbacks', 3),
            'auto_rollback_on_loop': getattr(ps, 'auto_rollback_on_loop', True),
            # Two-tier loop detection (2026-08 redesign)
            'loop_exact_rollback_enabled': getattr(ps, 'loop_exact_rollback_enabled', True),
            'loop_fuzzy_warning_enabled': getattr(ps, 'loop_fuzzy_warning_enabled', True),
            'tool_loop_fuzzy_rollback_enabled': getattr(ps, 'tool_loop_fuzzy_rollback_enabled', False),
            # DEPRECATED: legacy kill switch for the fuzzy tier (can disable but never enable)
            'tool_loop_detection_enabled': getattr(ps, 'tool_loop_detection_enabled', True),
            # Inner-loop detection
            **{'inner_loop_detect_enabled': getattr(ps, 'inner_loop_detect_enabled', False)},
            **_serialize_loop_settings(ps),
            # Skills system
            'default_load_skill_mode': getattr(ps, 'default_load_skill_mode', 'AUTO'),
            'auto_skill_enabled': getattr(ps, 'auto_skill_enabled', True),
            'auto_skill_mode': getattr(ps, 'auto_skill_mode', 'basic'),
            # Retry policy settings (Phase 6)
            'retry_max_attempts': getattr(ps, 'retry_max_attempts', 3),
            'endpoint_max_retries': getattr(ps, 'endpoint_max_retries', 1),
            'retry_base_delay': getattr(ps, 'retry_base_delay', 1.0),
            'retry_max_delay': getattr(ps, 'retry_max_delay', 8.0),
            # Code interpreter
            'ci_execution_timeout': getattr(ps, 'ci_execution_timeout', 120),
            'ci_watchdog_timeout': getattr(ps, 'ci_watchdog_timeout', 300),
            'ci_stale_container_ttl': getattr(ps, 'ci_stale_container_ttl', 1200),
            # Cache pool
            'cache_pool_enabled': getattr(ps, 'cache_pool_enabled', True),
            'cache_pool_size': getattr(ps, 'cache_pool_size', 64),
            'cache_threshold_chars': getattr(ps, 'cache_threshold_chars', 1000),
            # Streaming timeout settings
            'stream_max_silence_seconds': getattr(ps, 'stream_max_silence_seconds', 120.0),
            'stream_max_total_seconds': getattr(ps, 'stream_max_total_seconds', 900.0),
        })

    # Add tool char limits from pool.llm_cfg if available
    if hasattr(pool, 'llm_cfg'):
        for key in POOL_SETTINGS_TO_BROADCAST:
            if key in pool.llm_cfg:
                pool_settings[key] = pool.llm_cfg[key]
    # Add runtime pool settings (approval timeout, async shell console window)
    _add_pool_runtime_settings(pool, pool_settings)

    # Add disabled_tools from live cache if available
    if hasattr(pool, '_ui_disabled_tools') and pool._ui_disabled_tools:
        try:
            with pool._ui_disabled_tools_lock:
                if pool._ui_disabled_tools:
                    pool_settings['disabled_tools'] = dict(pool._ui_disabled_tools)
        except Exception:
            pass  # Don't let lock issues break state broadcast

    # Add work folders and default workspace from operation_manager
    if hasattr(pool, 'operation_manager') and pool.operation_manager:
        om = pool.operation_manager
        pool_settings['work_access_folders_ro'] = [str(p) for p in om.extra_work_folders_ro]
        pool_settings['work_access_folders_rw'] = [str(p) for p in om.extra_work_folders_rw]
        pool_settings['default_workspace'] = str(om.base_dir)

    return {
        'instances': all_instances,
        'agent_instances': all_instances,
        'active_stack': active_stack,
        # Intentionally NO 'approvals' key here. Approvals are delivered exclusively via
        # the dedicated {'type':'approvals'} WS message broadcast by _approval_loop; a
        # stream tick built before an approval is registered would carry a stale [] and,
        # if included, clobber live approval state on the client (banner disappears).
        'generating': True,
        'total_tokens': h_stats['tokens'] + r_stats['tokens'],
        'total_words': h_stats['words'] + r_stats['words'],
        'max_tokens': max_tokens,
        'current_model': current_model,
        'telemetry': telemetry_data,
        'stopped': pool.stopped,
        'paused': pool.is_paused(),  # Pause state for frontend "Paused" indicator
        'pool_settings': pool_settings,
    }

def _find_user_message_insertion_point(conversation: list) -> int:
    """Find the correct insertion point for a user message in the conversation.

    Scans backwards from the end of the conversation to find a safe insertion point
    that doesn't split tool call/response pairs. Supports both legacy function_call
    format (OpenAI <2023-07-06 API) and modern tool_calls array format.

    Args:
        conversation: List of message dicts or Message objects.

    Returns:
        Index where a new user message should be inserted (0 to len(conversation)).
        Returns len(conversation) if appending at the end is safe.
    """
    if not conversation:
        return 0

    # Scan backwards from the end
    i = len(conversation) - 1
    while i >= 0:
        msg = conversation[i]
        # Extract role safely (handle both dict and object types)
        if isinstance(msg, dict):
            role = msg.get('role', '').lower()
        else:
            role = getattr(msg, 'role', '').lower()

        if role == 'user':
            # Found a user message — safe to insert before it
            return i
        elif role == 'assistant':
            # Check if this assistant message has pending tool calls
            # Support both legacy function_call and modern tool_calls formats
            if isinstance(msg, dict):
                func_call = msg.get('function_call')
                tool_calls = msg.get('tool_calls', [])
            else:
                func_call = getattr(msg, 'function_call', None)
                tool_calls = getattr(msg, 'tool_calls', [])

            # Check for legacy function_call format
            if func_call is not None:
                # Assistant made a function call — need to find matching response
                # Don't insert before this message
                i -= 1
                continue

            # Check for modern tool_calls array format
            if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
                # Assistant made tool calls — need to find matching responses
                # Don't insert before this message
                i -= 1
                continue

            # No pending tool calls — safe to insert before this assistant message
            return i
        elif role == 'function' or role == 'tool':
            # This is a function/tool response — continue scanning backwards
            # to find where the original tool call was made
            i -= 1
            continue
        else:
            # Unknown role type — safe to insert before it
            return i

    # If we get here, all messages are part of tool call chains
    # Insert at the beginning
    return 0

def serialize_message(
    msg: Any,
    index: Optional[int] = None,
    for_ui: bool = True,
) -> dict:
    """Serialize a Message object or dict to a JSON-serializable dict.

    Handles Message objects (Pydantic or dataclass), raw dicts, and any object
    with role/content attributes.

    Features:
      - UI cache via module-level dict keyed by id(msg) — never mutates input
      - Content list normalization for multimodal messages (text, image, audio, video, file)
      - Large content truncation at 100K characters when for_ui=True
      - function_call normalization (handles objects with .name/.arguments attributes)
      - None value stripping and internal cache key cleanup (_tokens/_words)
      - Extra field extraction (tool_success from extra dict)

    Args:
        msg: A Message object, dict, or any object with role/content attributes.
        index: Optional message index for UI ordering.
        for_ui: If True (default), truncate large content at 100K chars and use
            the serialization cache. Set to False when serializing for agent
            reasoning pipelines where full fidelity is needed.

    Returns:
        JSON-serializable dictionary.
    """
    # M1: Look up in CacheManager (keyed by id(msg)) instead of mutating input.
    # Cache stores truncated UI versions — only use when for_ui=True.
    msg_id = id(msg)  # Works for both dicts and Message objects
    with _cache_mgr._lock:
        cached = _cache_mgr.ui_serialization.get(msg_id)
    if cached is not None and for_ui:
        res = dict(cached)  # Copy to avoid mutating the cache entry
        # Strip internal keys that might leak from stale cache data
        res.pop('_tokens', None)
        res.pop('_words', None)
        # Also strip any None values (defensive against old code versions)
        for key in list(res.keys()):
            if res[key] is None:
                del res[key]
        if index is not None:
            res['index'] = index
        return res

    if hasattr(msg, 'model_dump'):
        d = msg.model_dump()
    elif isinstance(msg, dict):
        d = dict(msg)
    else:
        d = {}
        for k in ['role', 'content', 'name', 'function_call', 'reasoning_content']:
            val = getattr(msg, k, None)
            if val is not None:
                d[k] = val

    # Normalize content to string (handles multimodal message lists)
    content = d.get('content', '')
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if 'text' in item:
                    parts.append(item['text'])
                elif 'image' in item:
                    cap = item.get('caption')
                    if cap:
                        parts.append(f"![image]({item['image']})\nCaption: {cap}")
                    else:
                        parts.append(f"![image]({item['image']})")
                elif 'audio' in item:
                    parts.append(f"[Audio: {item['audio']}]")
                elif 'video' in item:
                    parts.append(f"[Video: {item['video']}]")
                elif 'file' in item:
                    parts.append(f"[File: {item['file']}]")
            elif isinstance(item, str):
                parts.append(item)
            elif hasattr(item, 'text') and item.text:
                parts.append(item.text)
            elif hasattr(item, 'image') and item.image:
                cap = getattr(item, 'caption', None)
                if cap:
                    parts.append(f"![image]({item.image})\nCaption: {cap}")
                else:
                    parts.append(f"![image]({item.image})")
        content = '\n'.join(parts)

    # Keep content intact — frontend handles truncation via renderToolResult()

    d['content'] = content or ''

    # Normalize function_call (handles objects with .name/.arguments attributes)
    fc = d.get('function_call')
    if fc:
        if hasattr(fc, 'name'):
            d['function_call'] = {'name': fc.name, 'arguments': fc.arguments}
        # else: not an object with .name — keep as-is (should already be a dict)
    else:
        d.pop('function_call', None)

    # Strip None values and internal fields
    for key in list(d.keys()):
        if d[key] is None:
            del d[key]

    # FIX3 (internal cache keys leak): Remove _tokens/_words injected by get_history_stats
    # so they don't serialize to the frontend.
    d.pop('_tokens', None)
    d.pop('_words', None)

    # Surface the UI-only completion timestamp. It is a declared Message field excluded
    # from model_dump (never leaks to LLMs), so read it from the object/dict directly.
    ts_val = getattr(msg, 'ts', None) if not isinstance(msg, dict) else msg.get('ts')
    if ts_val is not None:
        d['ts'] = float(ts_val)

    # Extract tool_success from extra before stripping — frontend needs it for isToolFailure()
    if 'extra' in d and isinstance(d['extra'], dict):
        ts = d['extra'].get('tool_success')
        if ts is not None:
            d['tool_success'] = bool(ts)

    d.pop('extra', None)

    # M1: Store in module-level cache keyed by id(msg), never mutate the input dict.
    # Only cache for persistent history dicts (skip index=0 latest turn messages).
    # BUG_0005: Also cache Pydantic Message objects — committed-conversation Messages
    # are appended once and never mutated in-place, so id(msg) is a stable key. This
    # avoids re-running model_dump() on every streaming tick for unchanged history.
    if msg_id is not None and for_ui and (isinstance(msg, dict) or hasattr(msg, 'model_dump')) and index is not None and index > 0:
        _store_ui_cache(msg_id, d)

    if index is not None:
        d['index'] = index

    return d

def _check_is_waiting(pool: AgentPool, instance_name: str) -> bool:
    """Check if an agent is waiting for an API slot (with defensive error handling)."""
    try:
        api_router = getattr(pool, 'api_router', None)
        if api_router and callable(getattr(api_router, 'is_waiting', None)):
            return api_router.is_waiting(instance_name)
    except Exception as e:
        logger.debug(f"is_waiting check failed for {instance_name}: {e}")
    return False

def _serialize_instance(
    inst: AgentInstance, pool: AgentPool,
    include_messages: bool = False, streaming: bool = False,
    streaming_responses: Optional[List[Message]] = None,
    max_messages: Optional[int] = None,
) -> dict:
    """Serialize an AgentInstance for UI state display.

    When *include_messages* is True, the conversation (or just its tail when
    *max_messages* is set) is appended to the result dict along with token stats
    and max_tokens — matching the legacy API server path.

    ``max_messages``: when not None, only the last ``max_messages`` committed
    messages are serialized (tail-only). The in-flight streaming partial is still
    appended after them, and ``history_count`` stays the TOTAL count so the client
    splices the tail at ``startIdx = history_count - len(messages)``. This keeps
    per-frame WS payload small and constant (send-queue backpressure fix); pass
    None to send the full conversation (backward-compatible default).

    Streaming UI Content Update Fix (Step 3): When streaming_responses is provided, 
    append partial LLM content after persisted messages with fingerprint-based dedup.
    Fingerprint includes (content, reasoning_content, function_call, name) to prevent
    duplicates when messages committed in Phase 4 also appear in _streaming_responses.
    """
    # FIX 3: Thread-safe state read - snapshot state under lock before building result dict
    with inst._state_lock:
        current_state = inst.state  # Snapshot under lock
    
    result = {
        'instance_name': inst.instance_name,
        'agent_class': inst.agent_class,
        'active': current_state == AgentState.RUNNING,          # Maps to frontend's agentData.active (derived from state)
        'agent_state': current_state.name,  # Send actual state name for activity indicator (RUNNING, SLEEPING, IDLE, etc.)
        'is_halted': pool.is_instance_halted(inst.instance_name),
        'parent_instance': inst.parent_instance,
        'has_queued_messages': pool.has_messages(inst.instance_name),
        'queued_messages': pool.get_queue_messages(inst.instance_name) if pool else [],
        # Include is_waiting so ActivityBar can show "Waiting for API slot..."
        'is_waiting': _check_is_waiting(pool, inst.instance_name),
    }

    if not include_messages:
        return result

    # ── Serialise messages ───────────────────────────────────────────────
    with inst._compression_lock:
        full_msgs_snapshot = list(inst.conversation)
        # Read streaming_responses under compression lock for thread safety
        # Use passed parameter if provided, otherwise read from instance (fallback for callers not passing it)
        stream_responses = list(inst._streaming_responses) if streaming and streaming_responses is None and len(inst._streaming_responses) > 0 else streaming_responses

    msgs = full_msgs_snapshot
    original_history_count = len(msgs)

    # Tail-only serialization (WS send-queue backpressure fix): when max_messages is set,
    # serialize only the last `max_messages` committed messages. The client splices this
    # tail into its existing history via startIdx = history_count - len(messages), so we
    # must keep original_history_count as the TOTAL count and give each serialized message
    # its ABSOLUTE index (start_idx + i) to stay consistent with the streaming partials.
    if max_messages is not None:
        start_idx = max(0, original_history_count - max_messages)
        msgs = full_msgs_snapshot[start_idx:]
    else:
        start_idx = 0

    # ── Byte-budget tail reduction ────────────────────────────────────────────
    # Even with a message-count cap, the CONTENT of recent messages can be very large
    # (long reasoning chains, tool outputs). If the serialized tail exceeds _STREAM_TAIL_BYTE_BUDGET,
    # reduce to fewer messages until under budget. This guarantees bounded per-frame WS
    # payload size and prevents send-queue saturation regardless of content growth.
    if max_messages is not None and len(msgs) > 1:
        # Quick estimate: serialize the tail and check size. Use a cheap heuristic first
        # (sum of content+reasoning lengths) to avoid serializing unnecessarily.
        _est_bytes = sum(
            len(_get_msg_content(m)) + len(_get_msg_reasoning(m)) for m in msgs
        )
        if _est_bytes > _STREAM_TAIL_BYTE_BUDGET:
            # Binary-search for the largest suffix that fits within budget.
            # Always keep at least 1 message (the most recent).
            # NOTE: the size estimate above is a best-effort heuristic (sums
            # content+reasoning char lengths); actual JSON-serialized size may be
            # 30-50% larger due to structural overhead. This is acceptable as a soft
            # cap because the binary search always keeps at least 1 message.
            lo, hi = 1, len(msgs)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                _sub_est = sum(
                    len(_get_msg_content(m)) + len(_get_msg_reasoning(m)) for m in msgs[-mid:]
                )
                if _sub_est <= _STREAM_TAIL_BYTE_BUDGET:
                    lo = mid
                else:
                    hi = mid - 1
            # Reduce to `lo` most recent messages
            start_idx = original_history_count - lo
            msgs = full_msgs_snapshot[start_idx:]

    _t0 = _time_stream_timing.perf_counter() if _STREAM_TIMING_ENABLED else 0.0
    serialized_msgs = [serialize_message(m, start_idx + i) for i, m in enumerate(msgs)]
    if _STREAM_TIMING_ENABLED:
        _stream_time("instance.serialize_loop", _time_stream_timing.perf_counter() - _t0)

    # Set is_partial=True when there are active streaming responses so the frontend uses
    # the partial merge path (smart splice with history_count), which properly handles
    # growing content with same message count and avoids stale reference bugs.
    result['is_partial'] = len(stream_responses or []) > 0

    # ── Streaming UI Content Update Fix: Append partial LLM content ────────
    num_streaming = 0
    if stream_responses and len(stream_responses) > 0:
        # Build fingerprint set from existing serialized messages for dedup
        _t0 = _time_stream_timing.perf_counter() if _STREAM_TIMING_ENABLED else 0.0
        existing_fingerprints = set()
        for msg in serialized_msgs:
            content = msg.get(CONTENT, '') or ''
            reasoning = msg.get(REASONING_CONTENT, '') or ''
            func_call = str(msg.get('function_call'))
            name = msg.get(NAME)
            fingerprint = (content, reasoning, func_call, name)
            if fingerprint != ('', '', 'None', None):
                existing_fingerprints.add(fingerprint)
        if _STREAM_TIMING_ENABLED:
            _stream_time("instance.fingerprint_set", _time_stream_timing.perf_counter() - _t0)
        
        # Append streaming responses that aren't already in serialized_msgs
        for j, stream_msg in enumerate(stream_responses):
            # Use absolute index relative to full history for streaming messages
            abs_index = original_history_count + j
            
            stream_content = stream_msg.get(CONTENT, '') if isinstance(stream_msg, dict) else getattr(stream_msg, CONTENT, '') or ''
            stream_reasoning = stream_msg.get(REASONING_CONTENT, '') if isinstance(stream_msg, dict) else getattr(stream_msg, REASONING_CONTENT, '') or ''
            stream_func_call = str(stream_msg.get('function_call') if isinstance(stream_msg, dict) else getattr(stream_msg, 'function_call', None))
            stream_name = stream_msg.get(NAME) if isinstance(stream_msg, dict) else getattr(stream_msg, NAME, None)
            fingerprint = (stream_content, stream_reasoning, stream_func_call, stream_name)
            
            # Only append if not duplicate and has meaningful content
            if fingerprint not in existing_fingerprints and fingerprint != ('', '', 'None', None):
                serialized_msgs.append(serialize_message(stream_msg, abs_index))
                existing_fingerprints.add(fingerprint)
                num_streaming += 1

    # ── Token stats (Fix B: split stable history from streaming partial) ────
    # Cache key uses STABLE conversation identity only — deliberately NOT the growing
    # streaming content length. During a turn's streaming the committed conversation is
    # unchanged, so the expensive full-history stats are cached and reused across ticks
    # instead of recomputing get_history_stats() over the whole conversation per tick
    # (the O(N)-per-tick cost that made streaming latency grow with turn count).
    stream_resp_len = len(stream_responses) if stream_responses else 0
    # Hardening: include instance identity so two distinct instances that happen to share
    # (history_count, last_msg_id, stream_resp_len) never collide in the global token_stats cache.
    # (The pre-fix key also omitted this; adding it is a safe no-op for correctly-unique instances.)
    cache_key = (inst.instance_name, original_history_count, id(msgs[-1]) if msgs else None, stream_resp_len)

    # Thread-safe check and read of the cached HISTORY-ONLY stats via CacheManager.
    # We cache get_history_stats(full_msgs_snapshot) — the stable committed history — NOT
    # the combined history+partial, so the value is identical across all ticks of a turn.
    with _cache_mgr._lock:
        if cache_key not in _cache_mgr.token_stats:
            _t0 = _time_stream_timing.perf_counter() if _STREAM_TIMING_ENABLED else 0.0
            active_msgs = pool.slice_history_for_llm(full_msgs_snapshot) if full_msgs_snapshot else full_msgs_snapshot
            try:
                from agent_cascade.utils.utils import get_history_stats
                h_stats = get_history_stats(active_msgs)
            except Exception as e:
                logger.debug(f"Token stats calculation failed for {inst.instance_name} (using estimate): {e}")
                h_stats = {'tokens': len(full_msgs_snapshot) * 4, 'words': 0}
            if _STREAM_TIMING_ENABLED:
                _stream_time("instance.token_stats_h", _time_stream_timing.perf_counter() - _t0)
            # BUG31 Fix #1: Evict oldest entry if cache is full (increased from 100 to 5000)
            if len(_cache_mgr.token_stats) >= _TOKEN_STATS_CACHE_MAXSIZE:
                oldest_key = next(iter(_cache_mgr.token_stats))
                del _cache_mgr.token_stats[oldest_key]
            _cache_mgr.token_stats[cache_key] = h_stats
        else:
            h_stats = _cache_mgr.token_stats[cache_key]

    # Recompute ONLY the streaming partial (r_stats) fresh each tick so total_tokens stays
    # live as the partial grows. Cheap/O(tail): touches only the small in-flight response,
    # never the full conversation. Added on top of the cached history stats.
    _t0 = _time_stream_timing.perf_counter() if _STREAM_TIMING_ENABLED else 0.0
    r_stats = _calc_stream_r_stats(stream_responses)
    if _STREAM_TIMING_ENABLED:
        _stream_time("instance.token_stats_r", _time_stream_timing.perf_counter() - _t0)

    # Get max tokens via direct call to avoid staleness when endpoints change at runtime
    max_tokens = _get_max_tokens_for_instance(pool, inst)

    # BUG FIX: history_count must reflect the TOTAL length including unique streaming responses
    # so that startIdx = history_count - messages.length lands exactly on the first message
    # of the tail (or 0 if not partial).
    result.update({
        'messages': serialized_msgs,
        'history_count': original_history_count + num_streaming,
        'total_tokens': h_stats['tokens'] + r_stats['tokens'],
        'total_words': h_stats['words'] + r_stats['words'],
        'max_tokens': max_tokens,
    })

    return result

def _get_approvals(pool: AgentPool) -> list:
    """Get pending approvals from the operation manager (if available)."""
    if hasattr(pool, 'operation_manager') and pool.operation_manager:
        try:
            return pool.operation_manager.list_pending_approvals()
        except Exception as e:
            logger.debug(f"Failed to get pending approvals (non-critical): {e}")
    return []

def _build_agents_list(pool: AgentPool) -> list:
    """Build the agents list for UI display.

    Returns a list of agent metadata dictionaries that the frontend uses to
    show available agents and their capabilities. Built from pool.templates,
    the canonical source of agent definitions.

    Orchestrator is always placed at index 0 to match the handler's agent
    lookup order (api_server.py and ws_handlers.py).
    """
    # Collect all templates, ensuring orchestrator is first.
    # Snapshot under the pool lock if available: this may run in a worker thread
    # (build_state offloaded via run_in_executor) while refresh_agents()/load_agent()
    # mutate pool.templates concurrently. NOTE: no dedicated lock guards templates
    # today (see report); _pool_lock is the closest existing pool-level lock.
    _tpl_lock = getattr(pool, '_pool_lock', None)
    if _tpl_lock is not None:
        with _tpl_lock:
            template_items = list(pool.templates.items())
    else:
        template_items = list(pool.templates.items())
    if template_items:
        # Find orchestrator and move it to front
        orch_item = None
        non_orch_items = []
        for agent_class, template in template_items:
            if template is None:
                continue
            if agent_class.lower() == 'orchestrator':
                orch_item = (agent_class, template)
            else:
                non_orch_items.append((agent_class, template))
        if orch_item:
            template_items = [orch_item] + non_orch_items

    agents_list = []
    for idx, (agent_class, template) in enumerate(template_items):
        if template is None:
            continue
        try:
            agent_type = getattr(template, 'agent_type', 'orchestrator').lower()
            tools_list = list(getattr(template, 'function_map', {}).keys())
            default_tools = getattr(template, 'default_tools', tools_list)
            agents_list.append({
                'name': getattr(template, 'name', f'Agent-{idx}'),
                'index': idx,
                'agent_type': agent_type,
                'description': getattr(template, 'description', ''),
                'tools': tools_list,
                'default_tools': default_tools,
            })
        except Exception as e:
            logger.debug(f"Failed to build agent info for template (skipping): {e}")
    return agents_list

def _apply_ui_config(
    pool: AgentPool,
    instance_name: str,
    ui_cfg: Dict[str, Any],
) -> None:
    """Apply sanitized UI configuration to the LLM for an agent instance.

    Sanitizes config values (floats/ints) and filters out non-LLM keys before
    applying them as a per-instance LLM config override (instance._generate_cfg_override).

    Per-instance overrides are merged into generate_cfg at call time in _execute_llm_call,
    so the shared template is never mutated.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the agent whose LLM config should be updated.
        ui_cfg: Raw UI configuration dictionary.
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        return

    template = pool.get_template(instance.agent_class)
    if not template or not hasattr(template, 'llm') or not template.llm:
        return

    # Sanitize numeric values
    floats = ['temperature', 'top_p', 'presence_penalty', 'frequency_penalty',
              'repetition_penalty', 'repeat_penalty', 'min_p']
    ints = ['max_tokens', 'max_completion_tokens', 'top_k', 'seed',
            'max_input_tokens', 'max_turns']

    sanitized = {}
    for k, v in ui_cfg.items():
        if k in floats and v is not None:
            try:
                sanitized[k] = float(v)
            except (ValueError, TypeError) as e:
                logger.debug(f"UI config float conversion failed for key '{k}': {e}")
        elif k in ints and v is not None:
            try:
                sanitized[k] = int(float(v))
            except (ValueError, TypeError) as e:
                logger.debug(f"UI config int conversion failed for key '{k}': {e}")
        else:
            sanitized[k] = v

    # Normalize repeat_penalty key variants for backend compatibility
    from agent_cascade.api_router import _normalize_repeat_penalty
    if 'repeat_penalty' in sanitized:
        _normalize_repeat_penalty(sanitized, 'repeat_penalty', sanitized['repeat_penalty'])

    # Normalize token key
    if 'maxTokens' in sanitized:
        sanitized['max_tokens'] = sanitized.pop('maxTokens')

    # Filter out non-LLM keys (keys that are for execution control, not LLM API)
    # NOTE: max_turns appears in both the ints list above (for sanitization) AND
    # here in NON_LLM_KEYS (to prevent it leaking to the LLM). This is intentional —
    # we sanitize it as an int but then strip it from LLM config; it goes to instance.max_turns.
    from agent_cascade.constants import NON_LLM_KEYS
    llm_safe = {k: v for k, v in sanitized.items() if k not in NON_LLM_KEYS}

    # Apply to instance override using deepcopy of generate_cfg, then store on instance.
    # This prevents multi-session interference AND avoids mutating the shared template.
    import copy as _copy
    llm_cfg_copy = _copy.deepcopy(template.llm.generate_cfg)
    llm_cfg_copy.update(llm_safe)

    # Remove max_input_tokens from override if user didn't explicitly set it in the UI.
    # Otherwise apply_ui_config copies the template's value into the override, which then
    # short-circuits _resolve_max_tokens() and prevents the API Router from being consulted.
    if 'max_input_tokens' not in llm_safe:
        llm_cfg_copy.pop('max_input_tokens', None)

    # Validate and normalize disabled_tools from the UI before storing in the override.
    # If the UI sent a dict (per-agent format like {"coder": [...]}), preserve it —
    # the centralized resolver at resolve_disabled_tools_for_agent() handles dict lookups.
    # If it was a flat list, validate tool names and store as list.
    from agent_cascade.utils.disabled_tools import (
        normalize_disabled_tools, validate_tool_names,
    )
    from agent_cascade.tools.base import TOOL_REGISTRY

    if 'disabled_tools' in sanitized and sanitized['disabled_tools'] is not None:
        raw_dt = sanitized['disabled_tools']
        if isinstance(raw_dt, dict):
            # Preserve per-agent structure — the resolver handles dict lookups.
            # Validate each agent's tool list individually.
            validated_dict = {}
            from agent_cascade.constants import RUNTIME_REGISTERED_TOOLS
            known_tools = set(TOOL_REGISTRY.keys()) | RUNTIME_REGISTERED_TOOLS
            for agent_key, agent_tools in raw_dt.items():
                normalized = normalize_disabled_tools(agent_tools)
                validate_tool_names(normalized, known_tools=known_tools)
                # Store as list if it was a list/tuple, otherwise keep original format
                if isinstance(agent_tools, (list, tuple)):
                    validated_dict[agent_key] = list(normalized)
                else:
                    validated_dict[agent_key] = normalized
            llm_cfg_copy['disabled_tools'] = validated_dict
        else:
            normalized = normalize_disabled_tools(raw_dt)
            validate_tool_names(normalized, known_tools=set(TOOL_REGISTRY.keys()) | RUNTIME_REGISTERED_TOOLS)
            llm_cfg_copy['disabled_tools'] = list(normalized)  # Convert back to list for storage

    instance._generate_cfg_override = llm_cfg_copy

    # Apply max_turns to instance (extracted from NON_LLM_KEYS, applied separately)
    if 'max_turns' in ui_cfg:
        instance.max_turns = ui_cfg['max_turns']

    # auto_continue and enable_agent_budgeting now handled via config_handlers.py centralized handlers

    # Update agent_pool.llm_cfg under thread-safe lock
    # (pool is passed as a parameter to this function — no need to look it up)
    if hasattr(pool, 'llm_cfg'):
        try:
            with pool._execution._state_lock:  # Thread-safe write to shared config

                for _key in POOL_SETTINGS_TO_BROADCAST:
                    if _key in sanitized:
                        pool.llm_cfg[_key] = sanitized[_key]

        except AttributeError:
            # pool._execution or _state_lock doesn't exist — skip safely
            logger.debug("Execution engine not available for pool config update")
        except Exception as e:
            # Lock access should always work, but don't let it break generation
            logger.exception("Unexpected error updating pool.llm_cfg: %s", e)

def get_agent_state_from_pool(
    pool: AgentPool,
    instance_name: str,
) -> Optional[Dict[str, Any]]:
    """Get current state for any agent instance directly from the pool.

    Replaces get_agent_state() which had dual-track logic (root → session['history'],
    agent instance → pool.instance_state). In unified mode, everything comes from
    pool.instances[name].conversation.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the agent instance to query.

    Returns:
        Dictionary with instance state, or None if not found.
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        return None

    # Read conversation under lock for thread safety (single snapshot)
    with instance._compression_lock:
        msg_list = [serialize_message(m) for m in instance.conversation]
        msg_count = len(instance.conversation)

    return {
        'instance_name': instance.instance_name,
        'agent_class': instance.agent_class,
        'messages': msg_list,
        'is_active': instance.is_running,
        'is_halted': pool.is_instance_halted(instance_name),
        'parent_instance': instance.parent_instance,
        'has_queued_messages': pool.has_messages(instance_name),
        'queued_messages': pool.get_queue_messages(instance_name) if pool else [],
        'compression_summary': instance.compression_summary,
        'message_count': msg_count,
    }
