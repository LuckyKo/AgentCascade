"""Agent execution entry points (moved verbatim from api_integration.py).

Phase 3b pure-move refactor. Top of the dependency DAG — imports state_builder + tokens.
"""

import sys
from typing import Any, Dict, Iterator, List, Optional

from agent_cascade.log import logger
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.agent_pool import AgentPool
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.execution_engine import ExecutionEngine
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.api_integration_pkg.state_builder import _apply_ui_config

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

    instance_name = pool._resolve_instance_name(instance_name)
    instance = pool.create_instance(
        instance_name=instance_name,
        agent_class='orchestrator',
        parent_instance=None,  # Root agent — no parent
        max_turns=max_turns,
        conversation=conversation,
    )

    # ── Skills System: Inject self-augmentation skill into main agent's system message ──
    # Self-augmentation is the foundational root skill that teaches agents how to
    # discover and load specialized skills at runtime. Every main agent instance must
    # receive it so they can bootstrap their own capability expansion. This mirrors
    # the injection in load_session_from_log for consistency between new/restored sessions.
    try:
        from agent_cascade.execution_engine import _inject_self_augmentation_skill
        _inject_self_augmentation_skill(pool, instance)
    except Exception as e:
        logger.warning(f"[SKILLS] Skill injection failed for main agent instance '{instance_name}': {e}")

    # FIX: Log initial messages to JSONL so index-based sync in _log_messages_to_jsonl() works correctly.
    # Load existing history from file first (for session restore) so we don't double-log.
    # Only log initial messages if the history was empty (new session).
    try:
        # FIX (todo.md:117): Root agent's supervisor is "User", not "System"
        log_inst = pool.get_logger(instance_name, 'orchestrator', base_metadata={"supervisor": "User"})
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
    """DEPRECATED (2026-08): Inline loop detection in ExecutionEngine._pre_llm_checks
    handles rollback directly. LoopDetectedError is never raised; this retry wrapper
    is dead code. Kept only for backward compatibility.

    Run an agent with automatic loop detection recovery.

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
            # NOTE: This branch is dead code in production — LoopDetectedError is
            # never raised by the main codebase (loop detection handled inline in
            # ExecutionEngine._pre_llm_checks). Kept only for backward compatibility
            # with tests that artificially raise it. Do not rely on this path.
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
        except AgentTerminatedError:
            # Clean abort from termination — propagate without logging or error message.
            raise
        except Exception as e:
            # Non-loop error — yield message and stop (single list of Messages)
            yield [Message(role=USER, content=f"[SYSTEM ERROR]: Rollback performed but loop recovery failed ({e})")]
            return

    # Fallback: should not reach here but guard against infinite loops
    yield [Message(role=USER, content="[SYSTEM]: Loop recovery exhausted")]

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
