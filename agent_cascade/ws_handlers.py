"""WebSocket message handlers for the AgentCascade API server.

Extracted from api_server.py ws_chat() function (Phase 2 refactoring).
Each WebSocket message type has its own handler method dispatched via a lookup table.
"""
import asyncio
import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# ── Module-level helpers used by handlers ───────────────────────────────


def _clear_caches_safely() -> None:
    """Clear performance caches with error suppression."""
    try:
        from agent_cascade.api_integration import _clear_performance_caches
        _clear_performance_caches()
    except Exception as e:
        from agent_cascade.log import logger
        logger.debug(f"Cache clearing failed (non-critical): {e}")


def _validate_disabled_tools(ui_cfg: dict) -> None:
    """Validate disabled_tools in a generate_cfg dict against the tool registry."""
    from agent_cascade.utils.disabled_tools import normalize_disabled_tools, validate_tool_names
    from agent_cascade.tools.base import TOOL_REGISTRY

    if 'disabled_tools' in ui_cfg and ui_cfg['disabled_tools']:
        dt = ui_cfg['disabled_tools']
        known = set(TOOL_REGISTRY.keys())
        if isinstance(dt, dict):
            for tools in dt.values():
                validate_tool_names(normalize_disabled_tools(tools), known_tools=known)
        else:
            validate_tool_names(normalize_disabled_tools(dt), known_tools=known)


class WsMessageHandler:
    """Dispatches WebSocket messages to appropriate handler methods.

    Each message type has its own method with a consistent signature:
        async def handle_<type>(self, data: dict) -> None

    The handler maintains references to shared state (session, agent_pool, etc.)
    and provides helper methods for common operations."""

    # ── Constructor ───────────────────────────────────────────────────────
    def __init__(
        self,
        session: Dict[str, Any],
        agent_pool,          # AgentPool instance
        agents: list,        # List of agent objects
        send_queue,          # asyncio.Queue
        broadcast_fn: Callable,  # async broadcast(data) -> None
        build_state_fn: Callable,  # build_state(responses=None, generating=None) -> dict
        start_gen_fn: Callable,  # Thread entry point for run_agent_thread
        session_lock: threading.Lock,
        app,                 # FastAPI app object (holds security_check_semaphore, current_auto_security)
    ):
        self.session = session
        self.agent_pool = agent_pool
        self.agents = agents
        self.send_queue = send_queue
        self.broadcast_fn = broadcast_fn
        self.build_state_fn = build_state_fn
        self._start_gen_fn = start_gen_fn
        self._session_lock = session_lock
        self.app = app  # Fix 1: store app reference for security handler wiring

    # ── Dispatch table ────────────────────────────────────────────────────
    @property
    def _dispatch_table(self) -> Dict[str, Callable]:
        return {
            'message': self.handle_message,
            'continue': self.handle_continue,
            'stop': self.handle_stop,
            'pause': self.handle_pause,
            'resume_all': self.handle_resume_all,
            'resume': self.handle_resume,
            'terminate_agent_instance': self.handle_terminate,
            'terminate_sub_agent': self.handle_terminate,  # Alias
            'retry': self.handle_retry,
            'reset': self.handle_reset,
            'refresh_souls': self.handle_refresh_souls,
            'restart_server': self.handle_restart_server,
            'update_config': self.handle_update_config,
            'update_endpoints': self.handle_update_endpoints,
            'update_api_priorities': self.handle_update_api_priorities,
            'approve': self.handle_approve,
            'reject': self.handle_reject,
            'ask_security': self.handle_ask_security,
            'set_auto_security': self.handle_set_auto_security,
            'edit_message': self.handle_edit_message,
            'delete_messages': self.handle_delete_messages,
            'select_agent': self.handle_select_agent,
            'set_session_name': self.handle_set_session_name,
            'load_session': self.handle_load_session,
            'inject': self.handle_inject,
        }

    # ── Public dispatch entry point ───────────────────────────────────────
    async def dispatch(self, data: dict) -> None:
        """Route a parsed WebSocket message to the appropriate handler."""
        msg_type = data.get('type', '')
        handler = self._dispatch_table.get(msg_type)
        if handler is not None:
            await handler(data)
        else:
            # Fallback for unknown types — log and silently ignore
            await self.handle_unknown(msg_type, data)

    async def handle_unknown(self, msg_type: str, data: dict) -> None:
        """Handle unrecognized message types."""
        from agent_cascade.log import logger
        logger.debug(f"Unknown WebSocket message type: {msg_type!r} "
                     f"(data keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'})")

    # ── Broadcast helper ──────────────────────────────────────────────────
    async def _broadcast(self, ws_type: str = 'state', generating: Optional[bool] = None) -> None:
        """Broadcast state update to all WebSocket clients.

        Sends a full state snapshot via the broadcast function. The build_state_fn
        is called with optional generating override; it reads from pool.instances
        and includes all fields needed by the frontend (messages, instances, telemetry).
        """
        await self.broadcast_fn({'type': ws_type, **self.build_state_fn(generating=generating)})

    def _is_paused(self) -> bool:
        """Check if agent pool is currently paused."""
        return self.agent_pool.is_paused() if self.agent_pool else False

    # ── Helper: get active agent runner ───────────────────────────────────
    def _get_agent(self):
        """Get the currently selected agent by index."""
        idx = self.session['agent_index']
        if 0 <= idx < len(self.agents):
            return self.agents[idx]
        return self.agents[0]

    # ── Helper: check generating state (thread-safe) ──────────────────────
    def _is_generating(self) -> bool:
        with self._session_lock:
            return self.session.get('generating', False)

    # ── Helper: start generation atomically ───────────────────────────────
    def _start_generation(self) -> int:
        with self._session_lock:
            self.session['stop_requested'] = False
            self.session['generation_id'] += 1
            self.session['generating'] = True
            return self.session['generation_id']

    # ── Helper: stop generation atomically ────────────────────────────────
    def _stop_generation(self) -> None:
        with self._session_lock:
            self.session['generating'] = False

    # ── Helper: signal stop request ───────────────────────────────────────
    def _signal_stop(self) -> None:
        with self._session_lock:
            self.session['stop_requested'] = True

    # ════════════════════════════════════════════════════════════════════
    #  Handler Methods (one per message type)
    # ════════════════════════════════════════════════════════════════════

    async def handle_message(self, data: dict) -> None:
        """Handle 'message' — user sends a new message to start agent generation."""
        text = data.get('text', '').strip()
        if not text:
            return

        is_generating = self._is_generating()

        if is_generating:
            # Async injection while agent is running — route to target agent
            if self.agent_pool:
                from agent_cascade.api_server import _parse_multimodal_content
                target = data.get('target_agent') or self.session.get('session_name', 'Maine')
                parsed_content = _parse_multimodal_content(text)
                self.agent_pool.enqueue_message(target, parsed_content)
            return

        # Update session config if provided
        if 'agent_index' in data:
            self.session['agent_index'] = int(data['agent_index'])
        if 'session_name' in data:
            self.session['session_name'] = data['session_name']
        if 'generate_cfg' in data:
            _validate_disabled_tools(data['generate_cfg'])
            self.session['generate_cfg'] = data['generate_cfg']

        # Parse multimodal content and resolve target instance
        from agent_cascade.api_server import _parse_multimodal_content, _extract_system_message
        parsed_content = _parse_multimodal_content(text)
        instance_name = data.get('target_agent') or self.session['session_name']

        # Ensure the main agent instance exists before adding the message
        if self.agent_pool:
            inst = self.agent_pool.get_instance(instance_name)
            if inst is None or not inst.conversation:
                sys_content = _extract_system_message(self._get_agent())
                if sys_content:
                    from agent_cascade.api_integration import create_main_agent_instance
                    create_main_agent_instance(
                        pool=self.agent_pool,
                        instance_name=instance_name,
                        system_message_content=sys_content,
                    )

            # Clear any stale continue state for fresh turn
            inst = self.agent_pool.get_instance(instance_name)
            if inst is not None:
                with inst._compression_lock:
                    if inst._continue_saved_msg is not None:
                        from agent_cascade.log import logger
                        logger.debug(f"[CONTINUE_FIX] Cleared stale _continue_saved_msg for {instance_name} on new message")
                        inst._continue_saved_msg = None

            # Enqueue the user message
            self.agent_pool.enqueue_message(instance_name, parsed_content)

        # Start agent generation
        gen_id = self._start_generation()
        if self.agent_pool:
            self.agent_pool.stopped = False

            # Diagnostic: Check pool state before starting
            from agent_cascade.log import logger
            with self.agent_pool._execution._state_lock:
                stack = len(self.agent_pool._execution.active_stack)
            states = {n: i.state.name for n, i in self.agent_pool.instances.items()}
            logger.debug(f"Starting generation gen_id={gen_id}, instances={states}, active_stack={stack}")

        agent_runner = self._get_agent()
        loop = asyncio.get_event_loop()

        thread = threading.Thread(
            target=self._start_gen_fn,
            args=(None, agent_runner, gen_id, loop, instance_name),
            daemon=True,
        )
        thread.start()

        await self._broadcast(generating=True)

    async def handle_continue(self, data: dict) -> None:
        """Handle 'continue' — resume generation without inserting a new user message."""
        is_generating = self._is_generating()
        if is_generating:
            return

        # Update session config if provided
        if 'agent_index' in data:
            self.session['agent_index'] = int(data['agent_index'])
        if 'session_name' in data:
            self.session['session_name'] = data['session_name']
        if 'generate_cfg' in data:
            _validate_disabled_tools(data['generate_cfg'])
            self.session['generate_cfg'] = data['generate_cfg']

        # Resolve the target instance
        continue_instance_name = data.get('target_agent') or self.session['session_name']
        inst = None
        if self.agent_pool:
            inst = self.agent_pool.get_instance(continue_instance_name)

        # Start agent generation with existing history (no new user message)
        gen_id = self._start_generation()
        if self.agent_pool:
            self.agent_pool.stopped = False

        agent_runner = self._get_agent()
        loop = asyncio.get_event_loop()

        if inst is None:
            await self.broadcast_fn({'type': 'error', 'message': 'No agent instance found to continue'})
            return

        # Pop trailing assistant message before deepcopying history
        from agent_cascade.llm.schema import ASSISTANT, ROLE
        from agent_cascade.utils.utils import msg_field

        saved_assistant_msg = None
        with inst._compression_lock:
            if inst.conversation:
                last_msg = inst.conversation[-1]
                last_role = msg_field(last_msg, 'role')
                if last_role == ASSISTANT:
                    saved_assistant_msg = inst.conversation.pop()
                    # Store on instance for merging in execution_engine._process_response
                    inst._continue_saved_msg = saved_assistant_msg

        history_copy = copy.deepcopy(inst.conversation)

        thread = threading.Thread(
            target=self._start_gen_fn,
            args=(history_copy, agent_runner, gen_id, loop, continue_instance_name),
            daemon=True,
        )
        thread.start()

        await self._broadcast(generating=True)

    async def handle_stop(self, data: dict) -> None:
        """Handle 'stop' — stop all streaming and set ALL active agents to IDLE."""
        from agent_cascade.log import logger

        with self._session_lock:
            self.session['stop_requested'] = True
            self.session['generating'] = False
            self.session['generation_id'] += 1

        if self.agent_pool:
            # Transition ALL active agents to IDLE state (not just reset)
            from agent_cascade.agent_pool import ACTIVE_STATES
            from agent_cascade.agent_instance import AgentState, InvalidStateTransition

            transitioned = 0
            for inst_name, instance in list(self.agent_pool.instances.items()):
                try:
                    self.agent_pool._mark_activity(inst_name)

                    with instance._state_lock:
                        current_state = instance.state
                        if current_state in ACTIVE_STATES:
                            instance._transition(AgentState.IDLE)
                            transitioned += 1
                            logger.info(f"Stop: Transitioned {inst_name} from {current_state.name} to IDLE")

                    # Clear any pending continue merge state on stop
                    with instance._compression_lock:
                        if instance._continue_saved_msg is not None:
                            logger.debug(f"[CONTINUE_FIX] Stop handler cleared _continue_saved_msg for {inst_name}")
                            instance._continue_saved_msg = None
                except InvalidStateTransition as e:
                    logger.warning(f"[STOP_ERROR] Invalid state transition for {inst_name}: {e}")
                except Exception as e:
                    logger.warning(f"[STOP_ERROR] Failed to transition {inst_name} to IDLE: {e}")

            # Halt threads, release slots, and unblock pending approvals
            self.agent_pool.stop_session()

            # Increment run generation AFTER slot release
            self.agent_pool._run_generation += 1

            # Diagnostic: Check for stuck slots after stop
            if hasattr(self.agent_pool, 'api_router') and self.agent_pool.api_router:
                sched = self.agent_pool.api_router.scheduler
                with sched._lock:
                    stuck = {k: v for k, v in sched._schedules.items() if v['active_count'] > 0}
                    stuck_holders = {k: v for k, v in sched._slot_holders.items() if v}
                if stuck:
                    logger.warning(
                        f"Stuck slots detected after stop: "
                        f"active={stuck}, holders={stuck_holders}"
                    )
                else:
                    logger.debug("All slots released cleanly")

            logger.debug(f"Transitioned {transitioned} agent(s) to IDLE, generation now={self.agent_pool._run_generation}")

        # Clean up active stack and halted state after stop_session()
        if self.agent_pool:
            try:
                from agent_cascade.log import logger

                if hasattr(self.agent_pool, '_execution') and hasattr(self.agent_pool._execution, 'active_stack'):
                    with self.agent_pool._execution._state_lock:
                        original_len = len(self.agent_pool._execution.active_stack)
                        # Mutate in place instead of replacing the list
                        self.agent_pool._execution.active_stack[:] = [
                            (name, depth) for name, depth in self.agent_pool._execution.active_stack
                            if name not in self.agent_pool.terminated_instances
                        ]
                        removed_count = original_len - len(self.agent_pool._execution.active_stack)
                        if removed_count > 0:
                            logger.debug(f"[STOP_STACK_CLEANUP] Removed {removed_count} terminated entries from active_stack")

                # Clear _halted_instances to prevent stale pause state after stop
                if hasattr(self.agent_pool, '_halted_instances'):
                    self.agent_pool._halted_instances.clear()
            except Exception as e:
                logger.warning(f"[STOP_CLEANUP_ERROR] Error during slot/stack cleanup: {e}")

        await self._broadcast('done')

    async def handle_pause(self, data: dict) -> None:
        """Handle 'pause' — pause ALL running instances by setting global flag.

        IMPORTANT: This does NOT call _stop_generation() or broadcast generating=False.
        Pause should only affect tool response startup (Phase 4), not disrupt ongoing
        streaming (Phase 3). The generating flag and WebSocket state remain unchanged
        so the frontend keeps its streaming UI active while tools are held back.
        """
        if self.agent_pool:
            inst_names = list(self.agent_pool.instances.keys())
            self.agent_pool.pause()  # Sets _paused Event — backend blocks Phase 4 tool execution
            from agent_cascade.log import logger
            logger.info(f"Paused all instances: {inst_names}")

    async def handle_resume_all(self, data: dict) -> None:
        """Handle 'resume_all' — resume ALL paused instances."""
        if self.agent_pool:
            self.agent_pool.resume()  # Clears _paused Event — backend unblocks Phase 4 tool execution
            from agent_cascade.log import logger
            logger.info("Cleared global pause flag — all agents will resume naturally")

    async def handle_resume(self, data: dict) -> None:
        """Handle 'resume' — restore agent pools from logs and restart generation."""
        target_instance = data.get('instance_name', self.session['session_name'])
        is_generating = self._is_generating()

        was_halted = False
        if self.agent_pool:
            was_halted = self.agent_pool.is_instance_halted(target_instance)
            self.agent_pool.resume()
            from agent_cascade.log import logger
            logger.info(f"Instance {target_instance} resumed by user. Was halted: {was_halted}")

        # For the main session: only restart generation if it was actually halted
        if target_instance == self.session['session_name']:
            if is_generating and was_halted:
                logger.info(f"Main session was still generating — signalling stop before resume.")
                self._signal_stop()
                self.agent_pool.stopped = True
                await asyncio.sleep(0.1)

            if was_halted:
                # Was halted — agents wake naturally from sleep loop, no continuation message needed
                with self._session_lock:
                    self.session['stop_requested'] = False
                if self.agent_pool:
                    self.agent_pool.stopped = False

                    # ── Fix 3: Restore agent instance conversations from JSONL logs if corrupted ──
                    from agent_cascade.utils.pool_validation import validate_message_pool
                    from agent_cascade.log import logger as _logger
                    from agent_cascade.settings import DEFAULT_WORKSPACE

                    for sa_name, agent_class in list(self.agent_pool.instance_classes.items()):
                        if sa_name == self.session['session_name']:
                            continue

                        sa_inst = self.agent_pool.get_instance(sa_name)
                        if sa_inst is None:
                            continue

                        try:
                            with sa_inst._compression_lock:
                                conv_snapshot = list(sa_inst.conversation)
                            if validate_message_pool(conv_snapshot, sa_name):
                                continue

                            # Find the actual log file via existing logger or glob
                            recov = []
                            logger_inst = self.agent_pool.instance_loggers.get(sa_name)

                            if logger_inst and hasattr(logger_inst, 'log_path') and logger_inst.log_path:
                                actual_log_path = logger_inst.log_path
                            else:
                                if hasattr(self.agent_pool, 'operation_manager') and self.agent_pool.operation_manager:
                                    log_dir = self.agent_pool.operation_manager.base_dir / 'logs'
                                else:
                                    log_dir = Path(DEFAULT_WORKSPACE) / 'logs'
                                pattern = f"{agent_class}_{sa_name}_*.jsonl"
                                import glob
                                matches = sorted(glob.glob(str(log_dir / pattern)), reverse=True)
                                actual_log_path = matches[0] if matches else None

                            # Read messages from log file
                            if actual_log_path and os.path.exists(actual_log_path):
                                with open(actual_log_path, 'r', encoding='utf-8') as f:
                                    for line in f:
                                        line = line.strip()
                                        if not line:
                                            continue
                                        try:
                                            item = json.loads(line)
                                            if "metadata" not in item and "event" not in item:
                                                recov.append(item)
                                        except json.JSONDecodeError as e:
                                            _logger.debug(f"Skipping malformed JSONL line in agent pool recovery: {e}")

                            # Only overwrite pool if recovered data is valid
                            if recov and validate_message_pool(recov, sa_name):
                                _logger.info(
                                    f"Restoring agent instance {sa_name} conversation from log during resume "
                                    f"({len(recov)} messages)"
                                )
                                sa_inst = self.agent_pool.get_instance(sa_name)
                                if sa_inst is not None:
                                    sa_inst.rebuild_conversation(recov)
                            else:
                                _logger.warning(
                                    f"Could not restore agent instance {sa_name} pool — "
                                    f"no valid recovery data found in logs"
                                )
                        except Exception as _e:
                            _logger.warning(f"Failed to restore agent instance {sa_name} pool: {_e}")

                # Wrap session state modifications with session_lock to prevent race condition
                if not self._is_generating():
                    gen_id = self._start_generation()
                    agent_runner = self._get_agent()
                    loop = asyncio.get_event_loop()

                    thread = threading.Thread(
                        target=self._start_gen_fn,
                        args=(None, agent_runner, gen_id, loop, target_instance),
                        daemon=True,
                    )
                    thread.start()

                await self._broadcast(generating=True)
            elif not is_generating:
                await self._broadcast()

    async def handle_terminate(self, data: dict) -> None:
        """Handle 'terminate_agent_instance' / 'terminate_sub_agent'."""
        is_generating = self._is_generating()

        instance_name = data.get('instance_name')
        if instance_name and self.agent_pool:
            inst = self.agent_pool.get_instance(instance_name)
            from agent_cascade.log import logger
            from agent_cascade.agent_pool import ACTIVE_STATES
            from agent_cascade.agent_instance import AgentState

            # SAFEGUARD: Never allow terminating the root orchestrator
            is_root = (inst is not None and inst.parent_instance is None)

            if is_root:
                logger.warning(f"Terminate requested for root orchestrator '{instance_name}' — blocked. Transitioning to IDLE instead.")
                with self._session_lock:
                    self.session['stop_requested'] = True
                    self.session['generating'] = False
                    self.session['generation_id'] += 1
                self.agent_pool._stopped_event.set()

                self.agent_pool._mark_activity(instance_name)

                with inst._state_lock:
                    current_state = inst.state
                    if current_state in ACTIVE_STATES:
                        inst._transition(AgentState.IDLE)
                await self._broadcast('done')
                return

            # Get parent instance name for feedback BEFORE dismissal
            parent_instance = getattr(inst, 'parent_instance', None) if inst else None

            if parent_instance and parent_instance != instance_name:
                feedback_msg = f"[SYSTEM]: Agent '{instance_name}' has been terminated by user."
                self.agent_pool.enqueue_message(parent_instance, feedback_msg)
                logger.info(f"Enqueued termination feedback to {parent_instance}: {feedback_msg}")

            # dismiss_instance() handles recursive dismissal + state transition + removal
            self.agent_pool.dismiss_instance(instance_name)

            await self._broadcast()

    async def handle_retry(self, data: dict) -> None:
        """Handle 'retry' — trim tail, roll back snapshots, re-enqueue message."""
        is_generating = self._is_generating()
        if is_generating:
            return

        instance_name = data.get('target_agent') or self.session['session_name']

        # Initialize variables before the instance check to avoid undefined references
        last_user_msg = None
        count_to_trim = 0

        # Remove trailing assistant/function messages + the user message using unified rollback helper.
        # The helper handles cache clearing and logger sync internally, so we skip manual sync steps.
        if self.agent_pool:
            inst = self.agent_pool.get_instance(instance_name)
            if inst is not None:
                from agent_cascade.llm.schema import ASSISTANT, FUNCTION, USER
                from agent_cascade.utils.utils import msg_field

                while inst.conversation and count_to_trim < len(inst.conversation) \
                        and msg_field(inst.conversation[-1 - count_to_trim], 'role') in (ASSISTANT, FUNCTION):
                    count_to_trim += 1

                # Check the message BEFORE the trailing assistant/function messages for USER role
                if inst.conversation and count_to_trim < len(inst.conversation):
                    candidate_idx = len(inst.conversation) - 1 - count_to_trim
                    if msg_field(inst.conversation[candidate_idx], 'role') == USER:
                        last_user_msg = inst.conversation[candidate_idx]
                        count_to_trim += 1

                if count_to_trim > 0:
                    self.agent_pool._rollback_instance(
                        instance_name,
                        pop_count=count_to_trim,
                        sync_logger=True,
                        reason="User retry: trim trailing messages",
                    )

        # Check if instance exists and has messages
        inst = self.agent_pool.get_instance(instance_name) if self.agent_pool else None
        if not inst and not (inst.conversation if inst else []) and not last_user_msg:
            await self._broadcast()
            return

        # Clear active tools/agent stack since we are retrying from the main input level
        if self.agent_pool:
            self.agent_pool.active_stack_clear()
            self.agent_pool.last_tool_args.clear()

            # 1. Rollback agent instances to the start of the last turn
            if self.session.get('last_turn_snapshots'):
                self.agent_pool.rollback_to_snapshots(self.session['last_turn_snapshots'], reason="User retry")

                for name in self.session['last_turn_snapshots']:
                    if name != self.session['session_name'] and name in self.agent_pool.instance_state:
                        sa_inst = self.agent_pool.get_instance(name)
                        if sa_inst is not None:
                            with sa_inst._compression_lock:
                                conv_snapshot = list(sa_inst.conversation)
                            self.agent_pool.instance_state[name]['messages'] = conv_snapshot

        # Re-append the user message to pool instance
        if last_user_msg and self.agent_pool:
            inst = self.agent_pool.get_instance(instance_name)
            if inst is not None:
                from agent_cascade.api_integration import _find_user_message_insertion_point
                insert_pos = _find_user_message_insertion_point(inst.conversation)
                inst.insert_message_at(insert_pos, last_user_msg)

                try:
                    log_inst = self.agent_pool.get_logger(instance_name, inst.agent_class)
                    with inst._compression_lock:
                        conv_snapshot = list(inst.conversation)
                    log_inst.update_history(conv_snapshot)
                except Exception as e:
                    from agent_cascade.log import logger
                    logger.debug(f"Logger sync after retry re-insert failed for {instance_name} (non-critical): {e}")
            else:
                from agent_cascade.api_integration import create_main_agent_instance
                create_main_agent_instance(
                    pool=self.agent_pool,
                    instance_name=instance_name,
                    system_message_content="",
                )
                self.agent_pool.enqueue_message(instance_name, last_user_msg.content)

        if 'generate_cfg' in data:
            self.session['generate_cfg'] = data['generate_cfg']

        gen_id = self._start_generation()
        if self.agent_pool:
            self.agent_pool.stopped = False
        agent_runner = self._get_agent()
        loop = asyncio.get_event_loop()

        thread = threading.Thread(
            target=self._start_gen_fn,
            args=(None, agent_runner, gen_id, loop, instance_name),
            daemon=True,
        )
        thread.start()
        await self._broadcast(generating=True)

    async def handle_reset(self, data: dict) -> None:
        """Handle 'reset' — clear conversation and reset session."""
        if self.agent_pool:
            inst = self.agent_pool.get_instance(self.session['session_name'])
            if inst is not None:
                inst.reset_conversation()
                try:
                    self.agent_pool._logger.create_new_session(
                        self.session['session_name'], inst.agent_class
                    )
                except Exception as e:
                    from agent_cascade.log import logger
                    logger.debug(f"Logger reset during stop failed (non-critical): {e}")

        with self._session_lock:
            self.session['stop_requested'] = True
            self.session['generating'] = False
            self.session['generation_id'] += 1
        if self.agent_pool:
            self.agent_pool.stopped = True
            self.agent_pool.reset()
        await self._broadcast('done')

    async def handle_refresh_souls(self, data: dict) -> None:
        """Handle 'refresh_souls' — refresh agent templates."""
        if self.agent_pool:
            self.agent_pool.refresh_agents()

            # Update the global agents list used by build_state
            new_agents = [self.agent_pool.get_agent(name) for name in self.agent_pool.list_agents()]
            # Ensure orchestrator is at index 0 if possible
            if 'orchestrator' in self.agent_pool.agents:
                orch = self.agent_pool.agents['orchestrator']
                new_agents = [orch] + [a for a in new_agents if a != orch]

            # Mutate the agents list in-place so the handler's reference stays valid
            self.agents.clear()
            self.agents.extend(new_agents)

        await self._broadcast()

    async def handle_restart_server(self, data: dict) -> None:
        """Handle 'restart_server' — restart the server process."""
        from agent_cascade.log import logger
        logger.warning("Server restart requested via UI")
        import sys
        await self.broadcast_fn({'type': 'error', 'message': 'Server is restarting... Please wait.'})
        os.execl(sys.executable, sys.executable, *sys.argv)

    async def handle_update_config(self, data: dict) -> None:
        """Handle 'update_config' — delegate to ConfigUpdateRouter for key dispatch."""
        from agent_cascade.config_handlers import ConfigUpdateRouter

        if 'generate_cfg' in data:
            self.session['generate_cfg'] = data['generate_cfg']
            ui_cfg = data['generate_cfg']

            router = ConfigUpdateRouter(self.agent_pool, self.agents)
            await router.apply(ui_cfg)

        await self._broadcast()

    async def handle_update_endpoints(self, data: dict) -> None:
        """Handle 'update_endpoints' — bulk update all endpoints and priorities."""
        if self.agent_pool and hasattr(self.agent_pool, 'api_router'):
            from agent_cascade.log import logger
            ep_count = len(data.get('endpoints', []))
            ap_count = len(data.get('agent_priorities', {}))
            logger.info(f"[update_endpoints] Received: {ep_count} endpoints, {ap_count} agent priority mappings")
            self.agent_pool.api_router.from_dict(data)
        await self._broadcast()

    async def handle_update_api_priorities(self, data: dict) -> None:
        """Handle 'update_api_priorities' — update agent-type → endpoint priority mappings."""
        if self.agent_pool and hasattr(self.agent_pool, 'api_router'):
            from agent_cascade.log import logger
            priorities = data.get('agent_priorities', {})
            logger.info(f"[update_api_priorities] Received {len(priorities)} priority mappings: "
                        f"{list(priorities.keys())}")
            for agent_type, endpoint_ids in priorities.items():
                self.agent_pool.api_router.set_agent_priorities(agent_type, endpoint_ids)
        await self._broadcast()

    async def handle_approve(self, data: dict) -> None:
        """Handle 'approve' — approve a pending request."""
        rid = data.get('request_id')
        if rid and self.agent_pool:
            is_auto = data.get('automated', False)
            from agent_cascade.log import logger
            logger.info(f"[{'AUTO' if is_auto else 'USER'}] Approving request: {rid}")
            self.agent_pool.operation_manager.user_approve(rid)
            await self._broadcast()

    async def handle_reject(self, data: dict) -> None:
        """Handle 'reject' — reject a pending request with reason."""
        rid = data.get('request_id')
        reason = data.get('reason', 'Rejected by user')
        if rid and self.agent_pool:
            is_auto = data.get('automated', False)
            from agent_cascade.log import logger
            logger.info(f"[{'AUTO' if is_auto else 'USER'}] Rejecting request: {rid}. Reason: {reason}")
            self.agent_pool.operation_manager.user_reject(rid, reason)
            await self._broadcast()

    async def handle_ask_security(self, data: dict) -> None:
        """Handle 'ask_security' — run Security advisor check for pending tool approvals.

        Phase 3: Delegates to SecurityAdvisorHandler which manages the full lifecycle:
          prompt building → instance creation → engine execution with streaming →
          verdict parsing (multiple fallback strategies) → auto-apply/reject → cleanup.
        """
        from agent_cascade.security_handler import SecurityAdvisorHandler

        # Fix 2 — pass correct args matching constructor signature:
        #   __init__(self, agent_pool, session, app_state, send_queue, broadcast_fn)
        sec = SecurityAdvisorHandler(
            self.agent_pool, self.session, self.app,
            self.send_queue, self.broadcast_fn,
        )
        await sec.run_check(data)

    async def handle_set_auto_security(self, data: dict) -> None:
        """Handle 'set_auto_security' — toggle Auto-Ask mode."""
        enabled = data.get('enabled', False)
        # Store on app object so SecurityAdvisorHandler can read it via _get_auto_security_enabled()
        self.app.current_auto_security = enabled

    async def handle_edit_message(self, data: dict) -> None:
        """Handle 'edit_message' — edit a message in conversation history."""
        idx = data.get('index')
        content = data.get('content', '')
        target_name = data.get('instance_name') or self.session['session_name']

        # Get the conversation from pool instance
        history = []
        if self.agent_pool:
            inst = self.agent_pool.get_instance(target_name)
            if inst is not None:
                with inst._compression_lock:
                    history = list(inst.conversation)

        from agent_cascade.api_server import _parse_multimodal_content, COMPRESSION_MARKER, _CONTEXT_SUMMARY_RE
        from agent_cascade.utils.utils import msg_field
        from agent_cascade.llm.schema import CONTENT

        if idx is not None and 0 <= idx < len(history):
            msg = history[idx]
            old_content = msg_field(msg, CONTENT, "")
            new_parsed_content = _parse_multimodal_content(content)

            # If this is a compression marker, ensure tags are preserved
            is_compression_msg = str(old_content).startswith(COMPRESSION_MARKER)
            if is_compression_msg:
                if COMPRESSION_MARKER in content or "<context_summary>" in content:
                    new_parsed_content = content
                else:
                    new_parsed_content = f"{COMPRESSION_MARKER}\n\n<context_summary>\n{content}\n</context_summary>"

                match = _CONTEXT_SUMMARY_RE.search(new_parsed_content)
                if match and self.agent_pool:
                    self.agent_pool.instance_summaries[target_name] = match.group(1).strip()

            # Apply edit — handle both dict and Message object types
            if isinstance(msg, dict):
                msg[CONTENT] = new_parsed_content
                if '_ui_cache' in msg:
                    del msg['_ui_cache']
            elif hasattr(msg, 'content'):
                msg.content = new_parsed_content

            if self.agent_pool:
                inst = self.agent_pool.get_instance(target_name)
                if inst is not None:
                    inst.rebuild_conversation(history)

                    logger_inst = self.agent_pool.get_logger(
                        target_name,
                        'Orchestrator' if target_name == self.session['session_name'] else 'SubAgent'
                    )
                    logger_inst.rewrite_log_with_history(history)

                # Sync instance_state so build_state() sees the edit
                    self.agent_pool.instance_state[target_name]['messages'] = list(history)

        _clear_caches_safely()
        await self._broadcast()

    async def handle_delete_messages(self, data: dict) -> None:
        """Handle 'delete_messages' — prune messages from conversation."""
        target_name = data.get('instance_name') or self.session['session_name']

        history = []
        if self.agent_pool:
            inst = self.agent_pool.get_instance(target_name)
            if inst is not None:
                with inst._compression_lock:
                    history = list(inst.conversation)

        indices = sorted(data.get('indices', []), reverse=True)
        for idx in indices:
            if 0 <= idx < len(history):
                history.pop(idx)

        if self.agent_pool:
            inst = self.agent_pool.get_instance(target_name)
            if inst is not None:
                inst.rebuild_conversation(history)

                logger_inst = self.agent_pool.get_logger(
                    target_name,
                    'Orchestrator' if target_name == self.session['session_name'] else 'SubAgent'
                )
                logger_inst.rewrite_log_with_history(history)

                if target_name != self.session['session_name'] and target_name in self.agent_pool.instance_state:
                    self.agent_pool.instance_state[target_name]['messages'] = list(history)

        _clear_caches_safely()
        await self._broadcast()

    async def handle_select_agent(self, data: dict) -> None:
        """Handle 'select_agent' — update the active agent index."""
        self.session['agent_index'] = int(data.get('index', 0))
        await self._broadcast()

    async def handle_set_session_name(self, data: dict) -> None:
        """Handle 'set_session_name' — rename session and migrate summaries."""
        old_name = self.session['session_name']
        new_name = data.get('name', 'Maine')
        self.session['session_name'] = new_name
        if self.agent_pool:
            if old_name in self.agent_pool.instance_summaries:
                self.agent_pool.instance_summaries[new_name] = self.agent_pool.instance_summaries.pop(old_name)
        await self._broadcast()

    async def handle_load_session(self, data: dict) -> None:
        """Handle 'load_session' — load conversation from a log file."""
        path = data.get('path')
        if path and self.agent_pool:
            with self._session_lock:
                status = self.agent_pool.load_session_from_log(
                    path,
                    target_instance=self.session.get('session_name'),
                    clear_sub_agents_before_load=True
                )
            if status.startswith("Error"):
                await self.broadcast_fn({'type': 'error', 'message': status})
            else:
                instance_name = self.session.get('session_name', 'Maine')
                inst = self.agent_pool.get_instance(instance_name)
                if inst is not None:
                    self._stop_generation()
                    self._signal_stop()
                    if self.agent_pool:
                        self.agent_pool.stopped = False
                    _clear_caches_safely()
                    await self._broadcast()

    async def handle_inject(self, data: dict) -> None:
        """Handle 'inject' — enqueue a message into an agent's queue."""
        text = data.get('text', '').strip()
        target = data.get('target_agent') or self.session.get('session_name', 'Maine')
        if text and self.agent_pool:
            self.agent_pool.enqueue_message(target, text)

    # ── Internal helpers (none needed — SecurityAdvisorHandler handles everything) ──