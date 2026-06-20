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

from agent_cascade.llm.schema import (
    ASSISTANT, CONTENT, NAME, REASONING_CONTENT, ROLE, SYSTEM, USER, Message,
)
from agent_cascade.log import logger

from .agent_instance import AgentInstance, AgentState, LoopDetectedError
from .agent_pool import AgentPool
from .execution_engine import ExecutionEngine, _build_resources_block, _replace_resources_block, _build_session_metadata, _replace_section


# ═══════════════════════════════════════════════════════════════════════
# Performance Caches — Fix #1 (Token Stat Caching) & Fix #3 (Incremental Serialization)
# ═══════════════════════════════════════════════════════════════════════

# Fix #1: Token stat cache keyed by (msg_count, last_msg_id) → stats dict.
# During LLM streaming, the conversation doesn't change — only partial streamed content changes.
# So stats should be cached aggressively and only recalculated when a new message is added.
# BUG31: Increased maxsize from 100 to 5000 to prevent premature cache eviction during multi-instance sessions.
_token_stats_cache: Dict[tuple, dict] = {}
_TOKEN_STATS_CACHE_MAXSIZE = 5000

# BUG31 Fix #2: Cache _get_max_tokens_for_instance result per instance name.
# The max tokens value never changes during a session, so caching avoids expensive lookups.
# Key: instance_name, Value: max_input_tokens (int)
_max_tokens_cache: Dict[str, int] = {}

# Fix #3: Version tracker per instance. Incremented each time a message is added.
# Used to skip serializing instances whose conversation hasn't changed.
# Key: instance_name, Value: (msg_count, id_of_last_msg)
# NOTE: This dict serves dual purpose — it's used by both Fix #3 (serialization dedup
# in build_stream_update_from_pool lines ~511-522) AND Fix #4 (token stats cache invalidation
# at line ~459). Both use the same (msg_count, id(last_msg)) tuple format.
_last_stream_versions: Dict[str, tuple] = {}
# Cached serialized instance data for unchanged instances (reused across stream_updates)
_cached_instance_data: Dict[str, dict] = {}

# BUG31 Fix #4: Cache token stats per instance for build_stream_update_from_pool.
# During active generation, the conversation doesn't change — skip expensive slice_history_for_llm + get_history_stats.
# Key: instance_name, Value: (h_stats, r_stats) tuple of dicts
_stream_token_stats_cache: Dict[str, tuple] = {}
_STREAM_TOKEN_STATS_CACHE_MAXSIZE = 100  # Bounded cache (FIFO eviction) to prevent unbounded growth


def _clear_performance_caches():
    """Clear all module-level performance caches. Called during session reset."""
    global _token_stats_cache, _max_tokens_cache, _cached_instance_data, _stream_token_stats_cache
    _token_stats_cache.clear()
    _max_tokens_cache.clear()
    _cached_instance_data.clear()
    _stream_token_stats_cache.clear()


# ═══════════════════════════════════════════════════════════════════════
# Activity Update Helper — lightweight streaming updates for UI banner
# ═══════════════════════════════════════════════════════════════════════

# DEPRECATED: _build_activity_update is no longer used by the unified execution path.
# The dual update paths (activity_update + stream_update) created a split perception where
# the banner updated faster than the conversation. Removed in favor of stream_update only.
# Kept for potential future use or other code paths.

def _build_activity_update(
    pool: 'AgentPool',
    instance_name: str,
    streaming_text: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build a lightweight activity update for the UI banner.
    
    DEPRECATED: No longer used by run_agent_unified.py. The 50ms activity_update path
    was removed to eliminate split perception (banner updates fast, conversation lags).
    
    This is a minimal payload that updates the activity banner without
    building the full state. Used for near-real-time feedback during LLM streaming.
    
    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the active instance.
        streaming_text: Current partial text being generated (if any).
        
    Returns:
        Dictionary with activity update data, or None if instance not found.
    
    Example:
            # No longer called by unified path — kept for reference only
            # activity = _build_activity_update(pool, "Maine", "Thinking about...")
            # asyncio.run_coroutine_threadsafe(...)
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        return None
    
    # Extract preview from streaming text or last message
    preview = ''
    if streaming_text and streaming_text.strip():
        # Use the streaming text directly (last N chars for brevity)
        preview = streaming_text[-200:] if len(streaming_text) > 200 else streaming_text
    else:
        # Fallback to last message content
        with instance._compression_lock:
            if instance.conversation:
                last_msg = instance.conversation[-1]
                last_content = (
                    last_msg.get('content', '') if isinstance(last_msg, dict)
                    else getattr(last_msg, 'content', '')
                )
                if last_content:
                    # Clean up markdown for display
                    preview = str(last_content).replace('\n', ' ').strip()
                    preview = preview[-200:] if len(preview) > 200 else preview
    
    # Check if instance is waiting for API slot (Major Issue #4: inline logic to avoid circular import)
    is_waiting = False
    api_router = getattr(pool, 'api_router', None)
    if api_router and callable(getattr(api_router, 'is_waiting', None)):
        try:
            is_waiting = api_router.is_waiting(instance_name)
        except Exception as e:
            logger.debug(f"is_waiting check failed for {instance_name}: {e}")
    
    # Get token count for display (use cached value if available)
    token_count = 0
    if hasattr(instance, '_last_actual_token_count') and instance._last_actual_token_count > 0:
        token_count = instance._last_actual_token_count
    
    # FIX 3: Thread-safe state read - snapshot under lock before returning
    with instance._state_lock:
        current_state = instance.state
    
    return {
        'instance_name': instance_name,
        'preview': preview,
        'is_active': current_state == AgentState.RUNNING,
        'is_waiting': is_waiting,
        'token_count': token_count,
    }


# ═══════════════════════════════════════════════════════════════════════
# WebSocket Queue Helper — safely put stream_update events without blocking
# ═══════════════════════════════════════════════════════════════════════

async def _put_stream_update(queue: 'asyncio.Queue', event: dict) -> None:
    """Put a stream_update event onto the queue, dropping it if full.

    This helper is used with run_coroutine_threadsafe to push events from
    the agent thread into the async send_queue. It calls put_nowait() directly
    (synchronous) so stale stream_updates are dropped rather than blocking
    the agent thread.

    The function is async only so it can be scheduled via run_coroutine_threadsafe.
    QueueFull is caught inside the event loop — never propagated to caller.
    """
    import asyncio  # Lazy import to avoid module-level dependency
    try:
        queue.put_nowait(event)  # Synchronous, raises QueueFull if full
    except asyncio.QueueFull:
        pass  # Drop stale event; a newer one will arrive soon


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
        LoopDetectedError: Propagated from ExecutionEngine for recovery at caller level.

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

    Wrapper around run_agent_in_pool that catches LoopDetectedError and retries
    after surgical rollback. This replaces the retry loop in run_agent_thread().

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the instance to execute.
        max_auto_retries: Maximum number of auto-rollback retries (-1 for unlimited).
        auto_rollback_enabled: Whether to attempt recovery on loop detection.

    Yields:
        List[Message]: Current conversation state after each execution phase.
    """

    if max_auto_retries == -1:
        max_auto_retries = 999_999

    retry_count = 0
    instance = pool.get_instance(instance_name)
    if instance is None:
        raise KeyError(f"Instance '{instance_name}' not found in pool")

    while retry_count <= max_auto_retries:
        try:
            # Execute through unified engine
            yield from run_agent_in_pool(pool, instance_name)
            return  # Success — no loop detected

        except LoopDetectedError as e:
            if not auto_rollback_enabled or retry_count >= max_auto_retries:
                logger.warning(
                    f"Loop detected for {instance_name}: {e.reason}. "
                    f"Exceeded retries ({retry_count}/{max_auto_retries}). Stopping."
                )
                # Yield error state so UI can display it
                error_msg = Message(
                    role=ASSISTANT,
                    content=f"[SYSTEM: Loop detected — {e.reason}]",
                )
                yield [error_msg]
                return

            logger.warning(
                f"Loop detected for {instance_name}: {e.reason}. "
                f"Surgical rollback (Retry {retry_count + 1}/{max_auto_retries})."
            )

            # Surgical rollback + hint injection under per-instance lock for atomicity
            pool.surgical_rollback(instance_name, e.pop_count, reason=e.reason)

            # Inject loop avoidance hint (atomic with rollback)
            hint_msg = Message(
                role=USER,
                content=f"[SYSTEM]: A repetitive loop was detected ({e.reason}). "
                        f"Please try a different approach.",
            )
            with instance._compression_lock:
                instance.conversation.append(hint_msg)
                # Invalidate token count cache — conversation length changed
                instance._last_token_count_conversation_length = -1

            retry_count += 1

        except (KeyboardInterrupt, SystemExit):
            # Never swallow user interrupts or explicit exits
            raise

        except Exception as e:
            # Catch non-loop errors (LLM failure, tool crash, etc.) — yield error state
            logger.error(f"Execution failed for {instance_name}: {e}")
            error_msg = Message(
                role=ASSISTANT,
                content=f"[SYSTEM ERROR: {e}]",
            )
            yield [error_msg]
            return


# ═══════════════════════════════════════════════════════════════════════
# 3. State Building from Pool (replacing session['history'] reads)
# ═══════════════════════════════════════════════════════════════════════

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
        streaming: Controls tail optimization for large conversations. When False (default),
            all messages are included including system message at index 0. When True, only
            the last 10% of messages are sent if agent is RUNNING with >50 messages.

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

    # Build messages list: conversation + any partial responses (single snapshot)
    with instance._compression_lock:
        msgs = list(instance.conversation)
    if responses:
        msgs.extend(responses)

    # Calculate token stats for the active working set (after compression slicing)
    active_h = pool.slice_history_for_llm(msgs) if msgs else msgs

    # Get max tokens via module-level helper (avoids creating ExecutionEngine instance)
    # BUG31 Fix #2: Cache result per instance to avoid expensive repeated lookups
    if instance_name not in _max_tokens_cache:
        _max_tokens_cache[instance_name] = _get_max_tokens_for_instance(pool, instance)
    max_tokens = _max_tokens_cache[instance_name]

    # Calculate history stats
    try:
        from agent_cascade.utils.utils import get_history_stats
        h_stats = get_history_stats(active_h)
        r_stats = get_history_stats(responses) if responses else {'tokens': 0, 'words': 0}
    except Exception as e:
        logger.debug(f"Token stats calculation failed for {instance_name} (using estimate): {e}")
        # Fallback: estimate ~4 tokens per message on average (conservative)
        h_stats = {'tokens': len(active_h) * 4, 'words': 0}
        r_stats = {'tokens': 0, 'words': 0}

    # Extract compression summary from conversation markers
    current_summary = instance.compression_summary or ""

    # Build sub-agent state snapshot (C3: take snapshot before iterating)
    instance_snapshot = dict(pool.instances)
    all_instances = {}
    for name, inst in instance_snapshot.items():
        # Read _streaming_responses under lock and pass to _serialize_instance
        with inst._compression_lock:
            inst_streaming = list(inst._streaming_responses) if len(inst._streaming_responses) > 0 else None
        # Full state includes messages; use streaming parameter to control tail optimization
        all_instances[name] = _serialize_instance(inst, pool, include_messages=True, streaming=streaming, streaming_responses=inst_streaming)

    # Derive session name from root instance (M1/M4 fix)
    root_instances = [
        name for name, inst in instance_snapshot.items()
        if inst.parent_instance is None
    ]
    session_name = root_instances[0] if root_instances else instance_name

    # Build active stack
    active_stack = list(pool._execution.active_stack) if hasattr(pool, '_execution') else []

    # Build agents list for UI (from templates — the canonical source of agent definitions)
    agents_list = _build_agents_list(pool)

    # Get current model from template's LLM (for frontend display)
    current_model = 'Unknown'
    template = pool.get_template(instance.agent_class)
    if template and hasattr(template, 'llm') and template.llm:
        current_model = getattr(template.llm, 'model', 'Unknown')

    # Get telemetry (must never block state building)
    telemetry_data = None
    if hasattr(pool, 'telemetry') and pool.telemetry:
        try:
            telemetry_data = pool.telemetry.get_summary(instance_name)
        except Exception as e:
            logger.debug(f"Telemetry summary fetch failed for {instance_name} (non-critical): {e}")

    # Get default workspace from operation manager or settings default
    from agent_cascade.settings import DEFAULT_WORKSPACE
    default_workspace = str(DEFAULT_WORKSPACE)
    if pool and hasattr(pool, 'operation_manager') and pool.operation_manager:
        default_workspace = str(pool.operation_manager.base_dir)

    # Build API router state (must never block state building)
    api_router_state = {'endpoints': [], 'agent_priorities': {}}
    if hasattr(pool, 'api_router') and pool.api_router:
        try:
            api_router_state = pool.api_router.to_dict()
        except Exception as e:
            logger.debug(f"API router state serialization failed (using empty): {e}")

    # Check if instance is waiting (endpoint slot blocked)
    is_waiting = False
    if hasattr(pool, 'api_router') and pool.api_router:
        try:
            is_waiting = pool.api_router.is_waiting(instance_name)
        except Exception as e:
            logger.debug(f"API router waiting check failed for {instance_name} (using default): {e}")

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
        'stopped': pool.stopped,
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
    # Streaming UI Content Update Fix: Also read _streaming_responses under lock
    with instance._compression_lock:
        conv_snapshot = list(instance.conversation)
        stream_resp_snapshot = list(instance._streaming_responses) if instance._streaming_responses else None
    
    # Streaming UI Content Update Fix: Calculate character length of streaming content
    # to ensure token-by-token growth triggers version changes.
    stream_content_len = _streaming_content_length(stream_resp_snapshot)

    # BUG31 Fix #4: Skip expensive slice_history_for_llm + get_history_stats during active
    # generation when the conversation hasn't changed since last stream update.
    # Streaming UI Step 3 Fix: Include streaming response length AND content length in cache key 
    # so growing streaming content triggers cache invalidation and fresh stats computation
    current_version = (len(conv_snapshot), id(conv_snapshot[-1]) if conv_snapshot else None, len(stream_resp_snapshot) if stream_resp_snapshot else 0, stream_content_len)
    cached_stats = _stream_token_stats_cache.get(instance_name)
    
    # Feature Plan #023 Fix: Import get_history_stats before conditional to ensure availability in all branches
    from agent_cascade.utils.utils import get_history_stats
    
    if cached_stats is not None and current_version == _last_stream_versions.get(instance_name):
        # Conversation unchanged — reuse previously computed token stats
        h_stats, r_stats = cached_stats
    else:
        # Conversation changed or first call — compute fresh stats
        # Streaming UI Content Update Fix: Include streaming responses in combined snapshot
        combined_snapshot = conv_snapshot + (stream_resp_snapshot if stream_resp_snapshot else [])
        active_h = pool.slice_history_for_llm(combined_snapshot) if combined_snapshot else conv_snapshot

        # Calculate token stats
        try:
            h_stats = get_history_stats(active_h)
            r_stats = get_history_stats(responses) if responses else {'tokens': 0, 'words': 0}
        except Exception as e:
            logger.debug(f"Token stats calculation failed for stream update (using estimate): {e}")
            # Fallback: estimate ~4 tokens per message on average (conservative)
            h_stats = {'tokens': len(active_h) * 4, 'words': 0}
            r_stats = {'tokens': 0, 'words': 0}
        
        # Cache the computed stats for next tick
        # Evict oldest entry if cache is full (FIFO-style eviction)
        if len(_stream_token_stats_cache) >= _STREAM_TOKEN_STATS_CACHE_MAXSIZE:
            oldest_key = next(iter(_stream_token_stats_cache))
            del _stream_token_stats_cache[oldest_key]
        _stream_token_stats_cache[instance_name] = (h_stats, r_stats)

    # Get max tokens via module-level helper (avoids creating ExecutionEngine instance)
    # BUG31 Fix #2: Cache result per instance to avoid expensive repeated lookups
    if instance_name not in _max_tokens_cache:
        _max_tokens_cache[instance_name] = _get_max_tokens_for_instance(pool, instance)
    max_tokens = _max_tokens_cache[instance_name]

    # Build active stack
    active_stack = list(pool._execution.active_stack) if hasattr(pool, '_execution') else []

    # Build ALL instances snapshot (C3: take snapshot before iterating)
    # Root agent is included alongside sub-agents — no special treatment.
    # Each agent carries its own messages/history_count via _serialize_instance.
    # Note: dict(pool.instances) creates a shallow copy for safe iteration.
    # Instance conversations are protected by inst._compression_lock inside
    # _serialize_instance. Concurrent add/remove of instances during snapshot
    # is acceptable — worst case is a stale or partially-complete snapshot,
    # which the frontend handles gracefully via history_count merging.
    instance_snapshot_data = dict(pool.instances)

    # Fix #3: Incremental serialization — only serialize instances whose
    # conversation changed since the last stream_update. Version is derived
    # from (msg_count, id_of_last_msg) which changes only when a new message
    # is appended. During LLM streaming, the conversation doesn't change.
    
    # Streaming UI Content Update Fix: Read _streaming_responses under compression lock for thread safety
    all_instances = {}
    for name, inst in instance_snapshot_data.items():
        with inst._compression_lock:
            current_msgs = list(inst.conversation)
            # Read streaming responses for this instance (field always exists due to dataclass default_factory)
            inst_streaming_responses = list(inst._streaming_responses) if len(inst._streaming_responses) > 0 else None
        
        # Calculate content length for this instance's streaming responses
        inst_stream_content_len = _streaming_content_length(inst_streaming_responses)
        
        current_version = (len(current_msgs), id(current_msgs[-1]) if current_msgs else None, len(inst_streaming_responses) if inst_streaming_responses else 0, inst_stream_content_len)

        # Fix #2: Periodic full state refresh — every ~100 ticks force a complete
        # serialization (streaming=False) to recover from sync gaps. This ensures
        # any missed partial messages are recovered within ~10 seconds.
        is_full_refresh = force_full
        
        # Always serialize the primary instance (it's actively streaming)
        # and any instance whose version changed since last stream_update
        if name == instance_name or current_version != _last_stream_versions.get(name) or is_full_refresh:
            # Use streaming=False for full refresh to send complete state
            all_instances[name] = _serialize_instance(inst, pool, include_messages=True, streaming=(not is_full_refresh), streaming_responses=inst_streaming_responses)
            _last_stream_versions[name] = current_version
            _cached_instance_data[name] = all_instances[name]
        else:
            # Reuse the previously serialized data for unchanged instances
            all_instances[name] = _cached_instance_data.get(name)
            # If for some reason the cached data is missing, serialize fresh
            if all_instances[name] is None:
                all_instances[name] = _serialize_instance(inst, pool, include_messages=True, streaming=(not is_full_refresh), streaming_responses=inst_streaming_responses)
                _cached_instance_data[name] = all_instances[name]
                _last_stream_versions[name] = current_version

    # Get current model from template's LLM (for frontend display)
    template = pool.get_template(instance.agent_class)
    current_model = 'Unknown'
    if template and hasattr(template, 'llm') and template.llm:
        current_model = getattr(template.llm, 'model', 'Unknown')

    # Get telemetry if available
    telemetry_data = None
    if hasattr(pool, 'telemetry') and pool.telemetry:
        try:
            telemetry_data = pool.telemetry.get_summary(instance_name)
        except Exception as e:
            logger.debug(f"Telemetry summary fetch failed for {instance_name} in stream (non-critical): {e}")

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

    Resolution Order (short-circuit on first hit):
      1. API router effective limit (per-endpoint MIN logic) — checked first
      2. Per-instance override (_generate_cfg_override) — absolute priority, short-circuits below
      2b. Instance's allocated max_input_tokens (Feature 006) — from last LLM call for consistency
      3. Runtime-detected LLM limit (OAI detection writes to llm.generate_cfg)
      4. Template static config (from settings, via llm.cfg)
      5. User-configured DEFAULT_MAX_INPUT_TOKENS from settings

    Note: Per-instance override (step 2 in code) short-circuits before router limit check
    because supervisor-propagated overrides should be absolute. The router limit is checked
    first for efficiency but can be overridden by per-instance configuration.

    CRITICAL FIX: OAI detection writes to llm.generate_cfg['max_input_tokens']
    (an attribute dict), but old resolution only read from llm.cfg['generate_cfg']
    (a nested dict in cfg). These are DIFFERENT objects. We now check both paths.

    Feature 006: Step 2b checks instance._allocated_max_input_tokens for consistent tool 
    truncation thresholds when ground-truth values are available from previous LLM call.

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

    # ── Step 1: API Router (per-endpoint MIN logic) ──
    router_limit = 0
    if pool and hasattr(pool, 'api_router') and pool.api_router:
        try:
            agent_class = instance.agent_class.lower() if instance else 'orchestrator'
            router_limit = pool.api_router.get_effective_max_tokens(agent_class)
        except Exception as e:
            logger.debug(f"API Router lookup failed for {agent_class}: {e}")

    # ── Step 2: Per-instance override (from execution engine propagation) ──
    if instance and hasattr(instance, '_generate_cfg_override') and instance._generate_cfg_override:
        inst_override = instance._generate_cfg_override.get('max_input_tokens')
        if inst_override:
            return int(inst_override)

    # ── Step 2b: Instance's allocated max_input_tokens (Feature 006) ──
    # Check ground-truth value from last LLM call for consistent tool truncation thresholds
    if instance and hasattr(instance, '_allocated_max_input_tokens'):
        allocated = instance._allocated_max_input_tokens
        if allocated > 0:
            return allocated

    # ── Step 3: Runtime-detected LLM limit (OAI detection writes here directly) ──
    llm_limit = 0
    if instance and hasattr(pool, 'templates'):
        template = pool.get_template(instance.agent_class)
        if template and hasattr(template, 'llm'):
            llm = template.llm
            # OAI detection in oai.py writes to self.generate_cfg['max_input_tokens'] directly
            runtime_max = getattr(llm, 'generate_cfg', {}).get('max_input_tokens')
            if runtime_max:
                llm_limit = int(runtime_max)

    # ── Step 4: Template LLM config (static from settings, via llm.cfg dict) ──
    static_llm_limit = 0
    if instance and hasattr(pool, 'templates'):
        template = pool.get_template(instance.agent_class)
        if template and hasattr(template, 'llm'):
            llm = template.llm
            cfg = getattr(llm, 'cfg', {})
            agent_max = (
                cfg.get('generate_cfg', {}).get('max_input_tokens') or
                cfg.get('max_input_tokens')
            )
            if agent_max:
                static_llm_limit = int(agent_max)

    # ── Step 5: Resolve priority ──
    if router_limit > 0:
        return router_limit       # User-set or configured limit (Step 1)
    if llm_limit > 0:
        return llm_limit         # Runtime-detected from OAI endpoint (Step 3)
    if static_llm_limit > 0:
        return static_llm_limit  # Static config from settings (Step 4)
    
    return DEFAULT_MAX_INPUT_TOKENS   # User-configured default from settings (Step 5 fallback)


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


def serialize_message(msg: Any, index: Optional[int] = None) -> dict:
    """Serialize a Message object or dict to a JSON-serializable dict for UI rendering.

    Handles Message objects (Pydantic or dataclass), raw dicts, and any object with role/content attributes.
    Includes optional index field for UI ordering.

    Args:
        msg: A Message object, dict, or any object with role/content attributes.
        index: Optional message index for UI ordering.

    Returns:
        JSON-serializable dictionary.
    """
    if isinstance(msg, dict):
        result = dict(msg)
    elif hasattr(msg, 'model_dump'):
        # Pydantic model
        result = msg.model_dump()
    else:
        # Message dataclass or similar
        result = {
            ROLE: getattr(msg, 'role', ''),
            CONTENT: getattr(msg, 'content', ''),
        }
        if hasattr(msg, 'function_call') and msg.function_call:
            result['function_call'] = msg.function_call
        if hasattr(msg, 'name') and msg.name:
            result[NAME] = msg.name
        if hasattr(msg, 'reasoning_content') and msg.reasoning_content:
            result[REASONING_CONTENT] = msg.reasoning_content

    if index is not None:
        result['index'] = index

    return result


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

    Streaming optimisation: during active generation for large conversations (>30
    messages), only a proportional tail (10% of messages, minimum 5) is sent to avoid
    O(N²) serialisation on every ~150ms tick. Smaller conversations are sent in full.
    
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
    
    # Streaming UI Content Update Fix: Include content length of streaming messages in the version key.
    # Previously, the key only included message count, making it blind to token-by-token growth.
    stream_content_len = _streaming_content_length(stream_responses)

    if streaming and current_state == AgentState.RUNNING and len(msgs) > 50:
        # During active generation only send the tail for large conversations (>50 messages) to avoid
        # O(N²) serialisation on every ~150ms tick. Tail size is proportional (10% of
        # messages, minimum 5) to reduce sync gaps while still reducing bandwidth.
        # Smaller conversations are sent in full — dropping early context during
        # mid-conversation streaming would break incremental rendering.
        tail_size = max(5, len(msgs) // 10)  # Send at least 10% or 5 messages as tail
        start_idx = max(0, len(msgs) - tail_size)
        serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs[-tail_size:], start_idx)]
        result['is_partial'] = True
    else:
        start_idx = 0
        serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]
        result['is_partial'] = False

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
    
    # Streaming UI Content Update Fix: Include streaming_responses length in cache key
    # so that growing streaming content causes cache miss and fresh stats computation
    stream_resp_len = len(stream_responses) if stream_responses else 0
    cache_key = (original_history_count, id(msgs[-1]) if msgs else None, stream_resp_len, stream_content_len)
    
    # Streaming UI Content Update Fix: Compute token stats from combined messages (conversation + streaming_responses)
    # Use full_msgs_snapshot (persisted history) to ensure stats reflect total usage, not just the tail.
    all_msgs_for_stats = list(full_msgs_snapshot)
    if stream_responses:
        all_msgs_for_stats.extend(stream_responses)
    
    if cache_key not in _token_stats_cache:
        active_msgs = pool.slice_history_for_llm(all_msgs_for_stats) if all_msgs_for_stats else all_msgs_for_stats
        try:
            from agent_cascade.utils.utils import get_history_stats
            stats = get_history_stats(active_msgs)
        except Exception as e:
            logger.debug(f"Token stats calculation failed for {inst.instance_name} (using estimate): {e}")
            stats = {'tokens': len(all_msgs_for_stats) * 4, 'words': 0}
        # BUG31 Fix #1: Evict oldest entry if cache is full (increased from 100 to 5000)
        if len(_token_stats_cache) >= _TOKEN_STATS_CACHE_MAXSIZE:
            oldest_key = next(iter(_token_stats_cache))
            del _token_stats_cache[oldest_key]
        _token_stats_cache[cache_key] = stats
    else:
        stats = _token_stats_cache[cache_key]

    # BUG31 Fix #2: Cache max_tokens per instance name to avoid expensive repeated lookups
    if inst.instance_name not in _max_tokens_cache:
        _max_tokens_cache[inst.instance_name] = _get_max_tokens_for_instance(pool, inst)
    max_tokens = _max_tokens_cache[inst.instance_name]

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
    """
    agents_list = []
    for idx, (agent_class, template) in enumerate(pool.templates.items()):
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

    # Normalize penalty keys
    if 'repeat_penalty' in sanitized:
        pen = sanitized['repeat_penalty']
        sanitized['repetition_penalty'] = pen
        sanitized['repeatPenalty'] = pen

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
    instance._generate_cfg_override = llm_cfg_copy

    # Apply max_turns to instance (extracted from NON_LLM_KEYS, applied separately)
    if 'max_turns' in ui_cfg:
        instance.max_turns = ui_cfg['max_turns']

    # Apply auto_continue to pool settings (extracted from NON_LLM_KEYS, applied separately)
    # This makes the setting available to execution_engine.py for conditional auto-continue logic
    if 'auto_continue' in ui_cfg and hasattr(pool, 'settings'):
        pool.settings.auto_continue = bool(ui_cfg['auto_continue'])

    # Update agent_pool.llm_cfg and disabled_tools under thread-safe lock
    # (pool is passed as a parameter to this function — no need to look it up)
    if hasattr(pool, 'llm_cfg'):
        try:
            with pool._execution._state_lock:  # Thread-safe write to shared config
                # Re-apply disabled_tools under lock to prevent race with concurrent reads
                if 'disabled_tools' in sanitized and sanitized['disabled_tools'] is not None:
                    dt = sanitized['disabled_tools']
                    if isinstance(dt, (list, dict)):
                        cfg = dict(instance._generate_cfg_override or {})
                        cfg['disabled_tools'] = dt
                        instance._generate_cfg_override = cfg

                for _key in (
                    'tool_result_max_chars', 'grep_char_limit', 'grep_spillover',
                    'shell_char_limit', 'code_char_limit'
                ):
                    if _key in sanitized:
                        pool.llm_cfg[_key] = sanitized[_key]

        except AttributeError:
            # pool._execution or _state_lock doesn't exist — skip safely
            logger.debug("Execution engine not available for disabled_tools update")
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
        'compression_summary': instance.compression_summary,
        'message_count': msg_count,
    }