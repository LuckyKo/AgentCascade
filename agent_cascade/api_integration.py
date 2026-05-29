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

from .agent_instance import AgentInstance, LoopDetectedError
from .agent_pool import AgentPool
from .execution_engine import ExecutionEngine


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
    if conversation is None:
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

    # Populate instance_state for the main instance so get_session_history() in
    # unified mode can read it. Register under both 'root' (what api_server expects)
    # and the actual instance name for consistency with agent instance registration.
    agent_label = f"{instance_name} (OrchestratorAgent)"
    # Read conversation under lock for thread safety
    with instance._compression_lock:
        conv_snapshot = list(instance.conversation)
    pool.instance_state['root'] = {
        'active': False,
        'agent_name': agent_label,
        'messages': conv_snapshot,
    }
    if instance_name != 'root':
        pool.instance_state[instance_name] = pool.instance_state['root'].copy()

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

    engine = ExecutionEngine(pool)
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

    Returns:
        Dictionary with full state snapshot, or None if instance not found.

    Example:
        # Full state for initial broadcast
        state = build_state_from_pool(pool, "Maine", generating=True)
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
    max_tokens = _get_max_tokens_for_instance(pool, instance)

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
        all_instances[name] = _serialize_instance(inst, pool)

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
    template = pool.templates.get(instance.agent_class)
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

    return {
        'messages': [serialize_message(m, i) for i, m in enumerate(msgs)],
        'instances': all_instances,
        'agent_instances': {
            name: state for name, state in all_instances.items()
            if state.get('parent_instance') is not None
        },
        'active_stack': active_stack,
        'approvals': _get_approvals(pool),
        'generating': generating,
        'session_name': session_name,
        'instance_name': instance_name,
        'total_tokens': h_stats['tokens'] + r_stats['tokens'],
        'total_words': h_stats['words'] + r_stats['words'],
        'max_tokens': max_tokens,
        'summary': current_summary,
        'has_queued_messages': pool.has_messages(instance_name),
        'stopped': pool.stopped,
        # Extra fields for frontend compatibility (matches old build_state output)
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
) -> Optional[Dict[str, Any]]:
    """Build a lightweight streaming delta directly from the pool.

    Replaces build_stream_update() which read from session['history']. Only
    serializes the changing response messages — history is already on the client.

    Includes sub_agents, current_model, and telemetry fields to match the output
    format of the old build_stream_update() for frontend compatibility.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the primary instance for this stream.
        responses: Current partial response messages from the engine.

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

    # Calculate history count from the active slice (single snapshot)
    with instance._compression_lock:
        conv_snapshot = list(instance.conversation)
    active_h = pool.slice_history_for_llm(conv_snapshot) if conv_snapshot else conv_snapshot
    history_count = len(active_h)

    # Serialize only the response messages (they're what's changing)
    response_msgs = [
        serialize_message(m, history_count + i)
        for i, m in enumerate(responses or [])
    ]

    # Calculate token stats
    try:
        from agent_cascade.utils.utils import get_history_stats
        h_stats = get_history_stats(active_h)
        r_stats = get_history_stats(responses) if responses else {'tokens': 0, 'words': 0}
    except Exception as e:
        logger.debug(f"Token stats calculation failed for stream update (using estimate): {e}")
        # Fallback: estimate ~4 tokens per message on average (conservative)
        h_stats = {'tokens': len(active_h) * 4, 'words': 0}
        r_stats = {'tokens': 0, 'words': 0}

    # Get max tokens via module-level helper (avoids creating ExecutionEngine instance)
    max_tokens = _get_max_tokens_for_instance(pool, instance)

    # Build active stack
    active_stack = list(pool._execution.active_stack) if hasattr(pool, '_execution') else []

    # Build sub-agent snapshot (C3: take snapshot before iterating)
    instance_snapshot_data = dict(pool.instances)
    all_instances = {
        name: _serialize_instance(inst, pool)
        for name, inst in instance_snapshot_data.items()
    }
    agent_instances_data = {
        name: state for name, state in all_instances.items()
        if state.get('parent_instance') is not None
    }

    # Get current model from template's LLM (for frontend display)
    template = pool.templates.get(instance.agent_class)
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

    return {
        'history_count': history_count,
        'response_messages': response_msgs,
        'instances': all_instances,
        'agent_instances': agent_instances_data,
        'active_stack': active_stack,
        'approvals': _get_approvals(pool),
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

    # Append user message to the instance's conversation (single source of truth).
    # Use per-instance lock for thread safety — ExecutionEngine may be reading
    # conversation from another thread during its _setup_turn() snapshot.
    with instance._compression_lock:
        user_msg = Message(role=USER, content=user_message_content)
        instance.conversation.append(user_msg)
        # Invalidate token count cache — conversation length changed
        instance._last_token_count_conversation_length = -1
    pool._mark_activity(instance_name)

    # Apply UI config if provided (sanitize and inject into LLM config)
    if ui_cfg:
        _apply_ui_config(pool, instance_name, ui_cfg)

    # Execute through unified engine
    yield from run_agent_in_pool(pool, instance_name)


# ═══════════════════════════════════════════════════════════════════════
# 5. Utility Functions
# ═══════════════════════════════════════════════════════════════════════

def _get_max_tokens_for_instance(pool: AgentPool, instance: AgentInstance) -> int:
    """Get the effective max_input_tokens for an agent instance.

    Module-level helper that replaces creating a full ExecutionEngine instance
    just to call _get_max_tokens(). Called from build_state_from_pool and
    build_stream_update_from_pool on every tick — must be fast.

    Resolution order (same as ExecutionEngine._get_max_tokens):
      1. API Router per-endpoint limit
      2. Template's LLM config max_input_tokens
      3. Fallback default of 128000

    Args:
        pool: The AgentPool.
        instance: The agent instance to get max tokens for.

    Returns:
        Maximum input token count as integer.
    """
    # 1. Try API Router (handles per-endpoint MIN logic)
    if pool.api_router:
        try:
            router_limit = pool.api_router.get_effective_max_tokens(instance.agent_class.lower())
            if router_limit > 0:
                return router_limit
        except Exception as e:
            logger.debug(f"API Router lookup failed for {instance.agent_class}: {e}")

    # 2. Try template's LLM config
    template = pool.templates.get(instance.agent_class)
    if template and hasattr(template, 'llm'):
        llm = template.llm
        cfg = getattr(llm, 'cfg', {})
        agent_max = (
            cfg.get('generate_cfg', {}).get('max_input_tokens') or
            cfg.get('max_input_tokens')
        )
        if agent_max:
            return int(agent_max)

    # 3. Fallback to reasonable default
    return 128000


def serialize_message(msg: Any, index: Optional[int] = None) -> dict:
    """Serialize a Message object or dict to a JSON-serializable dict for UI rendering.

    Handles both Message objects and raw dicts (backward compatibility).
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


def _serialize_instance(inst: AgentInstance, pool: AgentPool) -> dict:
    """Serialize an AgentInstance for UI state display."""
    return {
        'instance_name': inst.instance_name,
        'agent_class': inst.agent_class,
        'is_active': inst.is_active,
        'is_halted': pool.is_instance_halted(inst.instance_name),
        'parent_instance': inst.parent_instance,
        'has_queued_messages': pool.has_messages(inst.instance_name),
    }


def _get_approvals(pool: AgentPool) -> list:
    """Get pending approvals from the operation manager (if available)."""
    if hasattr(pool, 'operation_manager') and pool.operation_manager:
        try:
            return pool.operation_manager.get_pending_approvals()
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
    applying them to the template's LLM config.

    NOTE: The old api_server.run_agent_thread() also mutated template.llm.generate_cfg
    directly — this is a known limitation of the current architecture. In a future phase,
    per-instance LLM config overrides should be supported via instance._generate_cfg_override.
    For now, we use copy.deepcopy to avoid mutating the shared template config.

    Args:
        pool: The AgentPool managing all instances.
        instance_name: Name of the agent whose LLM config should be updated.
        ui_cfg: Raw UI configuration dictionary.
    """
    instance = pool.get_instance(instance_name)
    if instance is None:
        return

    template = pool.templates.get(instance.agent_class)
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
    NON_LLM_KEYS = (
        'max_auto_rollbacks', 'auto_rollback_on_loop', 'auto_continue',
        'max_turns', 'mcpServers', 'work_access_folders',
        'tool_result_max_chars', 'grep_char_limit', 'grep_spillover',
        'shell_char_limit', 'code_char_limit', 'disabled_tools'
    )
    llm_safe = {k: v for k, v in sanitized.items() if k not in NON_LLM_KEYS}

    # Apply to LLM config using deepcopy of generate_cfg, then reassign the reference.
    # Deepcopy prevents multi-session interference on the inner dict, but we still
    # mutate the template in-place (replacing generate_cfg). A future phase should
    # support per-instance overrides via instance._generate_cfg_override.
    import copy as _copy
    llm_cfg_copy = _copy.deepcopy(template.llm.generate_cfg)
    llm_cfg_copy.update(llm_safe)
    template.llm.generate_cfg = llm_cfg_copy

    # Apply max_turns to instance (extracted from NON_LLM_KEYS, applied separately)
    if 'max_turns' in ui_cfg:
        instance.max_turns = ui_cfg['max_turns']

    # Update agent_pool.llm_cfg and disabled_tools under thread-safe lock
    # (pool is passed as a parameter to this function — no need to look it up)
    if hasattr(pool, 'llm_cfg'):
        try:
            with pool._execution._state_lock:  # Thread-safe write to shared config
                # Re-apply disabled_tools under lock to prevent race with concurrent reads
                if 'disabled_tools' in sanitized and sanitized['disabled_tools'] is not None:
                    dt = sanitized['disabled_tools']
                    if isinstance(dt, (list, dict)):
                        template.llm.generate_cfg['disabled_tools'] = dt

                for _key in (
                    'tool_result_max_chars', 'grep_char_limit', 'grep_spillover',
                    'shell_char_limit', 'code_char_limit'
                ):
                    if _key in sanitized:
                        pool.llm_cfg[_key] = sanitized[_key]
        except Exception:
            # Lock access should always work, but don't let it break generation
            pass


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
        'is_active': instance.is_active,
        'is_halted': pool.is_instance_halted(instance_name),
        'parent_instance': instance.parent_instance,
        'has_queued_messages': pool.has_messages(instance_name),
        'compression_summary': instance.compression_summary,
        'message_count': msg_count,
    }