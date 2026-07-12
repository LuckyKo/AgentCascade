"""
API Server Integration — Phase 3 of the AgentCascade Architecture Rewrite.

Thin bridge module between the API server (WebSocket/REST) and the new unified
ExecutionEngine. Replaces the dual-path code in api_server.py where:
  - Main agent ran through run_agent_thread() → agent_runner.run() using session['history']
  - Sub-agents ran through a separate execution path

After Phase 3, ALL agents (including the main orchestrator) are instances in the
pool, executed through ExecutionEngine.run(), with state read from
pool.instances[name].conversation. NO session['history'].

See DESIGN_REWRITE.md §5 for design rationale.
"""

from typing import Any, Dict, Iterator, List, Optional

import sys
import threading

from agent_cascade.llm.schema import (
    ASSISTANT, CONTENT, NAME, REASONING_CONTENT, ROLE, SYSTEM, USER, Message,
)
from agent_cascade.log import logger

from .agent_instance import AgentInstance, AgentState
from .agent_pool import AgentPool
from .execution_engine import ExecutionEngine


# ═══════════════════════════════════════════════════════════════════════
# Performance Caches — Centralized CacheManager (Phase 1A refactoring)
# ═══════════════════════════════════════════════════════════════════════

class CacheManager:
    """Centralized performance cache management for API integration.
    
    Consolidates 5 separate module-level caches (and their locks) into a single
    thread-safe structure. This eliminates cache sprawl and makes clearing/eviction
    atomic across all caches.
    
    PAIRED CACHE EVICTION NOTE:
        The stream_versions and cached_instances caches are paired — they share
        the same lock in the original code. When evicting from one, the corresponding
        entry in the other is also removed to prevent orphaned data.
    """
    
    def __init__(self):
        self._lock = threading.RLock()  # Single reentrant lock for all caches
        
        # Token stats cache: (msg_count, last_msg_id, stream_len) -> stats dict
        self.token_stats: Dict[tuple, dict] = {}
        
        # Stream version tracking: instance_name -> (msg_count, id, stream_len)
        self.stream_versions: Dict[str, tuple] = {}
        
        # Cached serialized instance data: instance_name -> dict
        self.cached_instances: Dict[str, dict] = {}
        
        # UI serialization cache: msg_id -> serialized dict
        self.ui_serialization: Dict[int, dict] = {}
        
        # Stream token stats: instance_name -> (h_stats, r_stats) tuple of dicts
        self.stream_token_stats: Dict[str, tuple] = {}
    
    def clear_all(self) -> None:
        """Clear all caches. Called during session reset."""
        with self._lock:
            self.token_stats.clear()
            self.stream_versions.clear()
            self.cached_instances.clear()
            self.ui_serialization.clear()
            self.stream_token_stats.clear()
    
    def evict_if_full(self, cache_name: str, maxsize: int) -> None:
        """Evict oldest entry if cache exceeds max size (FIFO).
        
        Handles paired cache eviction for stream_versions/cached_instances.
        """
        with self._lock:
            target = getattr(self, cache_name, {})
            
            # Determine paired cache (stream_versions <-> cached_instances)
            paired = None
            if cache_name == 'stream_versions':
                paired = ('cached_instances', self.cached_instances)
            elif cache_name == 'cached_instances':
                paired = ('stream_versions', self.stream_versions)
            
            while len(target) >= maxsize:
                oldest_key = next(iter(target))
                target.pop(oldest_key)
                if paired and oldest_key in paired[1]:
                    paired[1].pop(oldest_key, None)
    
    def evict_instance(self, instance_name: str) -> None:
        """Evict all cached data for a specific instance (paired eviction)."""
        with self._lock:
            self.stream_versions.pop(instance_name, None)
            self.cached_instances.pop(instance_name, None)
            self.stream_token_stats.pop(instance_name, None)


# Module-level CacheManager instance
_cache_mgr = CacheManager()

_TOKEN_STATS_CACHE_MAXSIZE = 5000
_UI_CACHE_MAXSIZE = 2000
_STREAM_TOKEN_STATS_CACHE_MAXSIZE = 100


def _clear_performance_caches():
    """Clear all module-level performance caches. Called during session reset."""
    _cache_mgr.clear_all()


# ═══════════════════════════════════════════════════════════════════════
# WebSocket Queue Helper — safely put stream_update events without blocking
# ═══════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════
# Shared Broadcast Helper — eliminates duplication across Security/Compressor/Main
# ═══════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════
# 1. Main Agent Instance Creation
# ═══════════════════════════════════════════════════════════════════════

def create_main_agent_instance(
    pool: AgentPool,
    instance_name: str,
    system_message_content: str,
    conversation: Optional[List[Message]] = None,
    max_turns: Optional[int] = None,
) -> AgentInstance:
    """Create the main agent (orchestrator) as just another instance in the pool.

    In the unified model, there is no special "main agent" — it's simply the first
    instance created with parent_instance=None. The system message is prepended to
    the conversation so ExecutionEngine.run() can pick it up.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Unique name for the main agent (typically the session name).
        system_message_content: The system prompt text.
        conversation: Optional existing conversation history (for session restore).
            If provided, the system message is NOT prepended — it should already
            be present as the first message.
        max_turns: Per-instance turn limit (None = default 50).

    Returns:
        The newly created AgentInstance.

    Example:
        pool = AgentPool(llm_cfg=...)
        sys_msg = Message(role=SYSTEM, content="You are Maine...")
        create_main_agent_instance(
            pool, "Maine", system_message_content="You are Maine...",
            conversation=[sys_msg],
        )
    """
    if not conversation:  # Changed from "is None" to catch empty list edge case too
        # Build initial conversation with system message
        sys_msg = Message(role=SYSTEM, content=system_message_content)
        conversation = [sys_msg]

    instance = pool.create_instance(
        instance_name=instance_name,
        agent_class='orchestrator',
        parent_instance=None,  # Root agent — no parent
        max_turns=max_turns,
        conversation=conversation,
    )

    # FIX: Log initial messages to JSONL so index-based sync in _log_messages_to_jsonl() works correctly.
    # Load existing history from file first (for session restore) so we don't double-log.
    # Only log initial messages if the history was empty (new session).
    try:
        log_inst = pool.get_logger(instance_name, 'orchestrator')
        # Load existing history from file so in-memory count matches disk state
        log_inst.load_history_from_file()
        # Only log initial messages for new sessions (no existing history loaded)
        if not log_inst.data.get("history"):
            for msg in conversation:
                if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                    try:
                        log_inst.log_message(msg)
                    except Exception as e:
                        logger.warning(f"Failed to log message for {instance_name}: {e}")
            
            # ── Tail sync check after initial session logging (design doc §5.2 — D1 fix) ──
            try:
                if getattr(pool.settings, 'tail_sync_check_enabled', True):
                    from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                    with instance._compression_lock:
                        conv = list(instance.conversation)
                    _check_tail(instance_name, conv, log_inst.log_path, context="api_integration_init")
            except Exception:
                pass  # Non-critical diagnostic check
    except Exception as e:
        logger.warning(f"Logging initial messages for {instance_name} failed: {e}")

    # Populate instance_state for the main instance so get_session_history() can read it.
    # Register under the actual instance name — no legacy 'root' key needed post-unification.
    agent_label = f"{instance_name} (Orchestrator)"
    with instance._compression_lock:
        conv_snapshot = list(instance.conversation)
    
    # FIX 4: Read state under _state_lock for thread safety
    with instance._state_lock:
        current_state = instance.state
    
    pool.instance_state[instance_name] = {
        'active': False,
        'agent_state': current_state.name,  # Send actual state name for activity indicator coloring
        'agent_name': agent_label,
        'messages': conv_snapshot,
    }

    logger.info(f"Created main agent instance: {instance_name}")
    return instance


# ═══════════════════════════════════════════════════════════════════════
# 2. Unified Agent Execution
# ═══════════════════════════════════════════════════════════════════════

def run_agent_in_pool(
    pool: AgentPool,
    instance_name: str,
) -> Iterator[List[Message]]:
    """Run any agent through the unified ExecutionEngine.

    This is THE entry point for agent execution from the API server. It replaces
    both run_agent_thread() → agent_runner.run() for main agents and the old
    sub-agent execution path.

    The instance must already exist in the pool (created via create_main_agent_instance
    or via call_agent tool). The engine yields List[Message] on each phase transition,
    which the API server converts to WebSocket updates.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the instance to execute.

    Yields:
        List[Message]: Current conversation state after each execution phase.

    Raises:
        KeyError: If instance_name is not found in the pool.

    Example:
        engine = ExecutionEngine(pool)
        for messages in run_agent_in_pool(pool, "Maine"):
            # Build and send WebSocket update from 'messages'
            delta = build_stream_update_from_pool(pool, "Maine", messages)
            send_to_websocket(delta)
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        raise KeyError(f"Instance '{instance_name}' not found in pool")

    # Note: Pre-check guard removed (2026-06-16 simplification).
    # The session_lock protecting session['generating'] read in api_server.py (L1)
    # is sufficient to prevent race conditions. This pre-check held _state_lock for
    # minutes, blocking pause/resume/terminate operations.
    
    engine = ExecutionEngine(pool)
    # initialize() now called automatically in __init__ (Phase 4.5 cleanup)
    yield from engine.run(instance)


def run_agent_in_pool_with_recovery(
    pool: AgentPool,
    instance_name: str,
    max_auto_retries: int = 3,
    auto_rollback_enabled: bool = True,
) -> Iterator[List[Message]]:
    """Run an agent with automatic loop detection recovery.

    On loop detection the wrapper performs a surgical rollback of the detected
    agent's conversation and injects a hint message before retrying. After
    exhausting retries (or on non-loop errors), it yields an error message.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the instance to execute.
        max_auto_retries: Max retry attempts (default 3). -1 for unlimited.
        auto_rollback_enabled: If True, perform surgical rollback on loop detection.

    Yields:
        List[Message]: Current conversation state after each execution phase.
    """
    from agent_cascade.loop_detection import LoopDetectedError

    retry_limit = sys.maxsize if max_auto_retries == -1 else max_auto_retries

    for attempt in range(retry_limit + 1):
        try:
            yield from run_agent_in_pool(pool, instance_name)
            return
        except LoopDetectedError as e:
            target = e.agent_name or instance_name

            if auto_rollback_enabled:
                inst = pool.get_instance(target) or pool.get_instance(instance_name)
                if inst is not None:
                    hint = Message(
                        role=USER,
                        content=(
                            f"[SYSTEM]: You appear to be stuck in a loop ({e.reason}). "
                            f"Try a different approach."
                        ),
                    )
                    inst.append_message(hint)

                pool.surgical_rollback(target, e.pop_count)

                if attempt < retry_limit:
                    # Re-check instance after rollback (it may have been evicted)
                    check = pool.get_instance(target) or pool.get_instance(instance_name)
                    if check is None:
                        last_msgs = [Message(role=USER, content=f"[SYSTEM]: Loop detected — rollback performed but loop recovery failed for {target}: {e.reason}")]
                        yield last_msgs
                        return

            if attempt < retry_limit:
                continue

            # Exhausted retries — yield error message (single list of Messages)
            last_msgs = [Message(role=USER, content=f"[SYSTEM]: Loop detected — rollback performed but loop recovery failed for {target}: {e.reason}")]
            yield last_msgs
            return
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            # Non-loop error — yield message and stop (single list of Messages)
            yield [Message(role=USER, content=f"[SYSTEM ERROR]: Rollback performed but loop recovery failed ({e})")]
            return

    # Fallback: should not reach here but guard against infinite loops
    yield [Message(role=USER, content="[SYSTEM]: Loop recovery exhausted")]


# ═══════════════════════════════════════════════════════════════════════
# 3. State Building from Pool (replacing session['history'] reads)
# ═══════════════════════════════════════════════════════════════════════

# ── Helper functions for build_state_from_pool / build_stream_update_from_pool ──

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


def _serialize_all_instances(pool: AgentPool, instance_snapshot: Dict[str, Any],
                              streaming: bool = False) -> Dict[str, dict]:
    """Serialize all instances in a pool snapshot.
    
    Args:
        pool: The AgentPool managing all instances.
        instance_snapshot: Snapshot of pool.instances for safe iteration.
        streaming: If True, uses tail optimization within each instance's 
            _serialize_instance call (partial messages only).
    """
    all_instances = {}
    for name, inst in instance_snapshot.items():
        with inst._compression_lock:
            inst_streaming = list(inst._streaming_responses) if len(inst._streaming_responses) > 0 else None
        all_instances[name] = _serialize_instance(
            inst, pool, include_messages=True, streaming=streaming,
            streaming_responses=inst_streaming,
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
    """Get the active execution stack from the pool."""
    return list(pool._execution.active_stack) if hasattr(pool, '_execution') else []


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
            if name == instance_name or current_version != prev_version or force_full:
                all_instances[name] = _serialize_instance(
                    inst, pool, include_messages=True,
                    streaming=(not force_full),
                    streaming_responses=inst_streaming_responses,
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
                    )
                    _cache_mgr.stream_versions[name] = current_version
                    _cache_mgr.cached_instances[name] = all_instances[name]
    
    return all_instances

def build_state_from_pool(
    pool: AgentPool,
    instance_name: str,
    responses: Optional[List[Message]] = None,
    generating: bool = False,
    streaming: bool = False,  # Controls tail optimization for large conversations
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
        streaming: Deprecated — previously controlled tail optimization for large conversations.
            Now all messages are always included regardless of this parameter.

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
    all_instances = _serialize_all_instances(pool, instance_snapshot, streaming=streaming)

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

    return {
        # Kept for backward compat — frontend fallback reads data.messages if root not in agent_instances
        'messages': [serialize_message(m, i) for i, m in enumerate(msgs)],
        'instances': all_instances,
        'agent_instances': all_instances,
        'active_stack': active_stack,
        **( {'approvals': pending_approvals} if pending_approvals else {} ),
        'generating': generating,
        'session_name': session_name,
        'instance_name': instance_name,
        'total_tokens': h_stats['tokens'] + r_stats['tokens'],
        'total_words': h_stats['words'] + r_stats['words'],
        'max_tokens': max_tokens,
        'summary': current_summary,
        'has_queued_messages': pool.has_messages(instance_name),
        'queued_messages': pool.get_queue_previews(instance_name) if pool else [],
        'stopped': pool.stopped,
        'paused': pool.is_paused(),  # Pause state for frontend "Paused" indicator
        # Extra fields for frontend display
        'agents': agents_list,
        'current_model': current_model,
        'telemetry': telemetry_data,
        'default_workspace': default_workspace,
        'is_waiting': is_waiting,
        'api_router': api_router_state,
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
    
    # BUG31 Fix #4: Skip expensive stats computation when conversation hasn't changed.
    # Version uses msg count, last msg id, streaming response count, and content length —
    # including content_len so that growing streaming content invalidates the cache
    # and fresh token stats are computed (total_tokens grows during active streaming).
    stream_content_len = sum(
        len(_get_msg_content(m)) + len(_get_msg_reasoning(m))
        for m in stream_resp_snapshot
    ) if stream_resp_snapshot else 0
    
    current_version = (
        len(conv_snapshot),
        id(conv_snapshot[-1]) if conv_snapshot else None,
        len(stream_resp_snapshot) if stream_resp_snapshot else 0,
        stream_content_len,
    )
    
    # Thread-safe read of cached token stats and last version via CacheManager
    with _cache_mgr._lock:
        cached_stats = _cache_mgr.stream_token_stats.get(instance_name)
        last_version = _cache_mgr.stream_versions.get(instance_name)
    
    if cached_stats is not None and current_version == last_version:
        # Conversation unchanged — reuse previously computed token stats
        h_stats, r_stats = cached_stats
    else:
        h_stats, r_stats = _calc_stream_token_stats(
            pool, instance_name, conv_snapshot, stream_resp_snapshot, responses,
        )

    # Get max tokens via module-level helper (avoids creating ExecutionEngine instance)
    max_tokens = _get_max_tokens_for_instance(pool, instance)

    # Build active stack
    active_stack = _build_active_stack(pool)

    # Build ALL instances snapshot with incremental serialization (Fix #3)
    all_instances = _serialize_instances_incremental(
        pool, instance_name, force_full,
    )

    # Get current model and telemetry via shared helpers
    current_model = _get_current_model(pool, instance)
    telemetry_data = _safe_get_telemetry(pool, instance_name)

    # Get pending approvals (only include if non-empty to prevent UI flickering)
    pending_approvals = _get_approvals(pool)
    
    return {
        'instances': all_instances,
        'agent_instances': all_instances,
        'active_stack': active_stack,
        **( {'approvals': pending_approvals} if pending_approvals else {} ),
        'generating': True,
        'total_tokens': h_stats['tokens'] + r_stats['tokens'],
        'total_words': h_stats['words'] + r_stats['words'],
        'max_tokens': max_tokens,
        'current_model': current_model,
        'telemetry': telemetry_data,
        'stopped': pool.stopped,
        'paused': pool.is_paused(),  # Pause state for frontend "Paused" indicator
    }


# ═══════════════════════════════════════════════════════════════════════
# 4. WebSocket Handler Integration Helpers
# ═══════════════════════════════════════════════════════════════════════

def execute_agent_turn(
    pool: AgentPool,
    instance_name: str,
    user_message_content: str,
    ui_cfg: Optional[Dict[str, Any]] = None,
) -> Iterator[List[Message]]:
    """Add a user message and execute one agent turn through the unified engine.

    This is the core flow for WebSocket message handling:
      1. User sends message via WebSocket
      2. Message is appended to instance.conversation
      3. Engine runs, yielding state updates
      4. API server converts yields to WebSocket updates

    Replaces the old flow:
      WebSocket → session['history'].append() → run_agent_thread → agent_runner.run()

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the agent instance to execute.
        user_message_content: The user's message text.
        ui_cfg: Optional UI configuration (temperature, max_tokens, etc.)
            Applied to the LLM config if present.

    Yields:
        List[Message]: Current conversation state after each execution phase.

    Example:
        # In WebSocket handler:
        for messages in execute_agent_turn(pool, "Maine", user_text):
            delta = build_stream_update_from_pool(pool, "Maine", messages)
            await websocket.send(json.dumps({'type': 'stream_update', **delta}))
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        raise KeyError(f"Instance '{instance_name}' not found in pool")

    # Enqueue the user message — same queue used by tool responses.
    # The existing _drain_and_inject logic will pick it up at normal injection points
    # (pre-LLM, post-tool), appending to working lists just like any other message.
    pool.enqueue_message(instance_name, user_message_content)

    # Apply UI config if provided (sanitize and inject into LLM config)
    if ui_cfg:
        _apply_ui_config(pool, instance_name, ui_cfg)

    # Execute through unified engine — drain logic handles the queued message
    yield from run_agent_in_pool(pool, instance_name)


# ═══════════════════════════════════════════════════════════════════════
# 5. Utility Functions
# ═══════════════════════════════════════════════════════════════════════

def _resolve_max_tokens(pool, instance=None):
    """Resolve effective max_input_tokens using unified priority order.

    Shared helper to eliminate code duplication across execution_engine,
    api_integration, and api_server. Called from all 5 resolution sites.

    Resolution Order (highest → lowest priority):
      1. Per-instance override (_generate_cfg_override) — absolute priority set by supervisor
      2. API Router effective limit — authoritative live source, always checked before caches
      3. Template static config (from settings, via llm.cfg) — original user-set value
      4. Instance's allocated max_input_tokens — fallback from last LLM call (can be stale)
      5. Runtime-detected LLM limit (OAI detection in shared generate_cfg) — last resort
      6. User-configured DEFAULT_MAX_INPUT_TOKENS from settings

    Why this order matters:
      - The API Router is the authoritative source for max_tokens because it reflects
        the CURRENT endpoint configuration. Cached values can become stale when endpoints
        are reconfigured (e.g., agent switched from a 128K model to a 32K model).
      - Per-instance overrides (step 1) short-circuit everything because they represent
        an explicit supervisor decision that should never be overridden by auto-detection.
      - Template static config (step 3) is more reliable than per-instance cache (step 4)
        because it was set at initialization and doesn't change based on which endpoint
        happened to serve the last request.
      - Runtime-detected LLM limit from shared generate_cfg (step 5) is checked last
        because it's a TEMPLATE-level mutable dict shared across ALL instances of that
        agent type — one instance's OAI detection pollutes every other instance.

    Args:
        pool: The AgentPool (or None for safe fallback).
        instance: The agent instance (or None for orchestrator-only lookups).

    Returns:
        Maximum input token count as integer.
    """
    # Import DEFAULT_MAX_INPUT_TOKENS locally to avoid circular import issues
    try:
        from agent_cascade.settings import DEFAULT_MAX_INPUT_TOKENS
    except ImportError:
        DEFAULT_MAX_INPUT_TOKENS = 58000

    # ── Step 1: Per-instance override (from execution engine propagation) ──
    # Absolute priority — supervisor-set overrides should never be second-guessed
    if instance and hasattr(instance, '_generate_cfg_override') and instance._generate_cfg_override:
        inst_override = instance._generate_cfg_override.get('max_input_tokens')
        if inst_override:
            return int(inst_override)

    # ── Step 2: API Router (per-endpoint priority-based selection) — AUTHORITATIVE SOURCE ──
    # Always checked before any cached value to avoid stale-state bugs when endpoints change
    router_limit = 0
    if pool and hasattr(pool, 'api_router') and pool.api_router:
        try:
            agent_class = instance.agent_class.lower() if instance else 'orchestrator'
            router_limit = pool.api_router.get_effective_max_tokens(agent_class)
        except Exception as e:
            logger.debug(f"API Router lookup failed for {agent_class}: {e}")

    # ── Gather fallback sources (template-level lookups, done once) ──
    static_llm_limit = 0
    allocated = 0
    runtime_max = 0
    try:
        if instance and hasattr(pool, 'templates'):
            template = pool.get_template(instance.agent_class)
            if template and hasattr(template, 'llm'):
                llm = template.llm

                # Step 3: Template static config (from settings, via llm.cfg dict)
                cfg = getattr(llm, 'cfg', {})
                agent_max = (
                    (cfg.get('generate_cfg') or {}).get('max_input_tokens') or
                    cfg.get('max_input_tokens')
                )
                if agent_max:
                    static_llm_limit = int(agent_max)

                # Step 5: Runtime-detected LLM limit (OAI detection writes to shared generate_cfg)
                runtime_max = getattr(llm, 'generate_cfg', {}).get('max_input_tokens', 0)
    except Exception as e:
        logger.debug(f"Template fallback lookup failed for {instance.agent_class if instance else '?'}: {e}")

    # Instance-level allocated max from last LLM call (Feature 006)
    if instance and hasattr(instance, '_allocated_max_input_tokens'):
        allocated = instance._allocated_max_input_tokens

    # ── Priority Resolution — router always wins over cached values ──
    if router_limit > 0:
        return router_limit       # Live API Router limit (authoritative)
    if static_llm_limit > 0:
        return static_llm_limit   # Template's original config from settings
    if allocated > 0:
        return allocated          # Per-instance cache from last LLM call (can be stale)
    if runtime_max:
        return runtime_max        # Shared template generate_cfg (last resort, can be polluted)

    return DEFAULT_MAX_INPUT_TOKENS   # User-configured default from settings (final fallback)


def _streaming_content_length(messages: list) -> int:
    """Calculate total content length of streaming messages for streaming dedup cache.
    
    This helper extracts the pattern used in 3 places to calculate content length
    for cache invalidation during streaming updates. It handles both dict and 
    Message object types.
    
    Args:
        messages: List of message dicts or Message objects from _streaming_responses.
        
    Returns:
        Total character count across content, reasoning_content, and function_call fields.
    """
    if not messages:
        return 0
    
    total_length = 0
    for m in messages:
        # Handle dict, Message object, and unexpected list types
        if isinstance(m, dict):
            total_length += len(m.get(CONTENT, '') or '')
            total_length += len(m.get(REASONING_CONTENT, '') or '')
            total_length += len(str(m.get('function_call') or ''))
        elif isinstance(m, list):
            # Skip unexpected list objects (can occur from streaming/multimodal content)
            continue
        else:
            total_length += len(getattr(m, CONTENT, '') or '')
            total_length += len(getattr(m, REASONING_CONTENT, '') or '')
            total_length += len(str(getattr(m, 'function_call', None) or ''))
    
    return total_length


def _get_max_tokens_for_instance(pool: AgentPool, instance: AgentInstance) -> int:
    """Get the effective max_input_tokens for an agent instance.

    Thin wrapper around _resolve_max_tokens — kept for backward compatibility
    since it's called from build_state_from_pool and build_stream_update_from_pool.
    """
    return _resolve_max_tokens(pool, instance)


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

    # Extract tool_success from extra before stripping — frontend needs it for isToolFailure()
    if 'extra' in d and isinstance(d['extra'], dict):
        ts = d['extra'].get('tool_success')
        if ts is not None:
            d['tool_success'] = bool(ts)

    d.pop('extra', None)

    # M1: Store in module-level cache keyed by id(msg), never mutate the input dict.
    # Only cache for persistent history dicts (skip index=0 latest turn messages).
    if msg_id is not None and for_ui and isinstance(msg, dict) and index is not None and index > 0:
        _store_ui_cache(msg_id, d)

    if index is not None:
        d['index'] = index

    return d


def _store_ui_cache(msg_id: int, cached_data: dict) -> None:
    """Store serialized message data in the CacheManager UI cache with bounded size.
    
    Uses a deep copy to prevent nested mutable leakage between cached entries.
    Thread-safe via CacheManager._lock."""
    import copy as _copy  # Lazy import — only used during serialization, not hot path
    with _cache_mgr._lock:
        # Evict oldest entry when cache exceeds max size (FIFO via insertion order)
        if len(_cache_mgr.ui_serialization) >= _UI_CACHE_MAXSIZE:
            _cache_mgr.ui_serialization.pop(next(iter(_cache_mgr.ui_serialization)))
        _cache_mgr.ui_serialization[msg_id] = _copy.deepcopy(cached_data)


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
) -> dict:
    """Serialize an AgentInstance for UI state display.

    When *include_messages* is True, the full conversation (or just the tail
    during streaming) is appended to the result dict along with token stats
    and max_tokens — matching the legacy API server path.

    All messages are always sent — no tail optimization applied. The client merges
    partials correctly so there is no risk of losing early context during streaming.
    
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
        'queued_messages': pool.get_queue_previews(inst.instance_name) if pool else [],
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
    
    # Always send all messages — no tail optimization. The client properly merges partials,
    # and removing the tail cut avoids any risk of losing early context during streaming.
    start_idx = 0
    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]
    
    # Set is_partial=True when there are active streaming responses so the frontend uses
    # the partial merge path (smart splice with history_count), which properly handles
    # growing content with same message count and avoids stale reference bugs.
    result['is_partial'] = len(stream_responses or []) > 0

    # ── Streaming UI Content Update Fix: Append partial LLM content ────────
    num_streaming = 0
    if stream_responses and len(stream_responses) > 0:
        # Build fingerprint set from existing serialized messages for dedup
        existing_fingerprints = set()
        for msg in serialized_msgs:
            content = msg.get(CONTENT, '') or ''
            reasoning = msg.get(REASONING_CONTENT, '') or ''
            func_call = str(msg.get('function_call'))
            name = msg.get(NAME)
            fingerprint = (content, reasoning, func_call, name)
            if fingerprint != ('', '', 'None', None):
                existing_fingerprints.add(fingerprint)
        
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

    # ── Token stats (Fix #1: cached by conversation identity) ─────────────
    # Cache key: (message_count, id_of_last_message). During LLM streaming,
    # the conversation doesn't change — only partial streamed content changes.
    # So stats are only recalculated when a new message is appended.
    
    # Streaming UI Content Update Fix: Include streaming_responses length AND content
    # length in cache key so that growing streaming content causes cache miss and
    # fresh token stats computation (total_tokens grows during active streaming).
    stream_resp_len = len(stream_responses) if stream_responses else 0
    per_agent_stream_content_len = sum(
        len(_get_msg_content(m)) + len(_get_msg_reasoning(m))
        for m in (stream_responses or [])
    )
    cache_key = (original_history_count, id(msgs[-1]) if msgs else None, stream_resp_len, per_agent_stream_content_len)
    
    # Streaming UI Content Update Fix: Compute token stats from combined messages (conversation + streaming_responses)
    # Use full_msgs_snapshot (persisted history) to ensure stats reflect total usage, not just the tail.
    all_msgs_for_stats = list(full_msgs_snapshot)
    if stream_responses:
        all_msgs_for_stats.extend(stream_responses)
    
    # Thread-safe check and read of token stats cache via CacheManager
    with _cache_mgr._lock:
        if cache_key not in _cache_mgr.token_stats:
            active_msgs = pool.slice_history_for_llm(all_msgs_for_stats) if all_msgs_for_stats else all_msgs_for_stats
            try:
                from agent_cascade.utils.utils import get_history_stats
                stats = get_history_stats(active_msgs)
            except Exception as e:
                logger.debug(f"Token stats calculation failed for {inst.instance_name} (using estimate): {e}")
                stats = {'tokens': len(all_msgs_for_stats) * 4, 'words': 0}
            # BUG31 Fix #1: Evict oldest entry if cache is full (increased from 100 to 5000)
            if len(_cache_mgr.token_stats) >= _TOKEN_STATS_CACHE_MAXSIZE:
                oldest_key = next(iter(_cache_mgr.token_stats))
                del _cache_mgr.token_stats[oldest_key]
            _cache_mgr.token_stats[cache_key] = stats
        else:
            stats = _cache_mgr.token_stats[cache_key]

    # Get max tokens via direct call to avoid staleness when endpoints change at runtime
    max_tokens = _get_max_tokens_for_instance(pool, inst)

    # BUG FIX: history_count must reflect the TOTAL length including unique streaming responses
    # so that startIdx = history_count - messages.length lands exactly on the first message
    # of the tail (or 0 if not partial).
    result.update({
        'messages': serialized_msgs,
        'history_count': original_history_count + num_streaming,
        'total_tokens': stats['tokens'],
        'total_words': stats['words'],
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
    # Collect all templates, ensuring orchestrator is first
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
            known_tools = set(TOOL_REGISTRY.keys())
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
            validate_tool_names(normalized, known_tools=set(TOOL_REGISTRY.keys()))
            llm_cfg_copy['disabled_tools'] = list(normalized)  # Convert back to list for storage

    instance._generate_cfg_override = llm_cfg_copy

    # Apply max_turns to instance (extracted from NON_LLM_KEYS, applied separately)
    if 'max_turns' in ui_cfg:
        instance.max_turns = ui_cfg['max_turns']

    # Apply auto_continue to pool settings (extracted from NON_LLM_KEYS, applied separately)
    # This makes the setting available to execution_engine.py for conditional auto-continue logic
    if 'auto_continue' in ui_cfg and hasattr(pool, 'settings'):
        pool.settings.auto_continue = bool(ui_cfg['auto_continue'])

    # Apply enable_agent_budgeting to pool settings (extracted from NON_LLM_KEYS, applied separately)
    # This makes the setting available to lifecycle_manager.py for max_turns propagation logic
    if 'enable_agent_budgeting' in ui_cfg and hasattr(pool, 'settings'):
        pool.settings.enable_agent_budgeting = bool(ui_cfg['enable_agent_budgeting'])

    # Update agent_pool.llm_cfg under thread-safe lock
    # (pool is passed as a parameter to this function — no need to look it up)
    if hasattr(pool, 'llm_cfg'):
        try:
            with pool._execution._state_lock:  # Thread-safe write to shared config

                for _key in (
                    'tool_result_max_chars', 'grep_char_limit', 'grep_spillover',
                    'shell_char_limit', 'code_char_limit'
                ):
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
        'queued_messages': pool.get_queue_previews(instance_name) if pool else [],
        'compression_summary': instance.compression_summary,
        'message_count': msg_count,
    }