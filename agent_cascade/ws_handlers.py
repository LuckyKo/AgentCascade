"""WebSocket message handlers for the AgentCascade API server.

Extracted from api_server.py ws_chat() function (Phase 2 refactoring).
Each WebSocket message type has its own handler method dispatched via a lookup table.
"""
import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, ClassVar, Dict, Optional

# Re-exports for backward compatibility (Phase 4b refactor)
from agent_cascade import settings
from agent_cascade.session_state_store import DEFAULT_SESSION_NAME
from agent_cascade.ws_helpers import _clear_caches_safely, _validate_disabled_tools


# ── WS message-envelope schema (BUG_0336) ──────────────────────────────────
# Single source of truth for the ``type`` wire values and the envelope field
# names, shared by BOTH the dispatch table and the response builders.
#
# ``MsgType`` is a ``str``-MIXIN enum so its members ARE their string values:
# ``json.dumps({'type': MsgType.ERROR})`` is byte-identical to
# ``{'type': 'error'}`` (wire format unchanged), and a plain string from
# ``data.get('type')`` still resolves against a ``MsgType``-keyed dict because
# str hash/eq are inherited (``hash('message') == hash(MsgType.MESSAGE)``).
# A plain ``Enum`` would break ``dispatch``'s lookup.
class MsgType(str, Enum):
    # Incoming dispatch keys (see ``_dispatch_table``).
    MESSAGE = 'message'
    CONTINUE = 'continue'
    STOP = 'stop'
    KV_STATE = 'kv_state'
    PAUSE = 'pause'
    RESUME_ALL = 'resume_all'
    RESUME = 'resume'
    TERMINATE_AGENT_INSTANCE = 'terminate_agent_instance'
    TERMINATE_SUB_AGENT = 'terminate_sub_agent'
    RETRY = 'retry'
    RESET = 'reset'
    REFRESH_SOULS = 'refresh_souls'
    REFRESH_AGENTS = 'refresh_agents'
    RESTART_SERVER = 'restart_server'
    UPDATE_CONFIG = 'update_config'
    UPDATE_SETTINGS = 'update_settings'
    SET_WORK_FOLDERS = 'set_work_folders'
    UPDATE_ENDPOINTS = 'update_endpoints'
    UPDATE_API_PRIORITIES = 'update_api_priorities'
    APPROVE = 'approve'
    REJECT = 'reject'
    ASK_SECURITY = 'ask_security'
    SET_AUTO_SECURITY = 'set_auto_security'
    SET_CLI_AGENT_KEY = 'set_cli_agent_key'
    CLI_AGENT_OAUTH_MANUAL_CMD = 'cli_agent_oauth_manual_cmd'
    CLI_AGENT_OAUTH_RECHECK = 'cli_agent_oauth_recheck'
    SET_AFK = 'set_afk'
    EDIT_MESSAGE = 'edit_message'
    DELETE_MESSAGES = 'delete_messages'
    SELECT_AGENT = 'select_agent'
    SET_SESSION_NAME = 'set_session_name'
    LOAD_SESSION = 'load_session'
    INJECT = 'inject'
    DISMISS_QUEUE = 'dismiss_queue'
    EXPORT_SETTINGS = 'export_settings'
    IMPORT_SETTINGS = 'import_settings'
    MIGRATE_SETTINGS_LOCAL = 'migrate_settings_local'
    REQUEST_STATE = 'request_state'
    STACKS_LIST = 'stacks_list'
    STACKS_SWAP = 'stacks_swap'
    STACKS_SNAPSHOT = 'stacks_snapshot'
    STACKS_NEW = 'stacks_new'
    STACKS_DELETE = 'stacks_delete'
    # Outgoing-only response types.
    STATE = 'state'
    STREAM_UPDATE = 'stream_update'
    ERROR = 'error'
    REFRESH_AGENTS_RESULT = 'refresh_agents_result'
    KV_STATE_RESULT = 'kv_state_result'
    STACKS_RESULT = 'stacks_result'
    SETTINGS_APPLIED = 'settings_applied'
    SETTINGS_CHANGED = 'settings_changed'
    INFO = 'info'
    WARNING = 'warning'
    CLI_AGENT_OAUTH_FAILED = 'cli_agent_oauth_failed'
    CLI_AGENT_OAUTH_STATUS = 'cli_agent_oauth_status'


@dataclass
class WsEnvelope:
    """WS envelope field-name constants — the one source of truth for every
    envelope key a handler reads from ``data`` or emits in a response dict.
    Each value must stay byte-identical to the original wire literal."""
    TYPE: ClassVar[str] = 'type'
    SESSION_NAME: ClassVar[str] = 'session_name'
    MESSAGE: ClassVar[str] = 'message'
    ERROR: ClassVar[str] = 'error'
    INSTANCE_NAME: ClassVar[str] = 'instance_name'
    STATUS: ClassVar[str] = 'status'
    GENERATE_CFG: ClassVar[str] = 'generate_cfg'
    API_ROUTER: ClassVar[str] = 'api_router'
    # type-specific envelope fields (lower frequency, still envelope keys):
    INSTANCE: ClassVar[str] = 'instance'
    HARNESS: ClassVar[str] = 'harness'
    COMMAND: ClassVar[str] = 'command'
    AUTHENTICATED: ClassVar[str] = 'authenticated'
    REASON: ClassVar[str] = 'reason'
    ACTION: ClassVar[str] = 'action'
    RETRYABLE: ClassVar[str] = 'retryable'
    REVISION: ClassVar[str] = 'revision'
    BASE_REVISION: ClassVar[str] = 'base_revision'
    SETTINGS: ClassVar[str] = 'settings'
    MESSAGE_INDEX: ClassVar[str] = 'message_index'
    # Optional typed shape (documentation; the ClassVars above are load-bearing):
    type: MsgType = MsgType.STATE
    session_name: Optional[str] = None


def _msg_identity(msg) -> tuple:
    """Stable identity key for a message (timestamp + content), used to map a displayed
    (trimmed-pool) message to its counterpart in the FULL on-disk history (Fix BUG_0007).

    Handles both dict and Message-object forms. Falls back to ('', '') when either field is
    missing so identity comparison degrades gracefully rather than crashing."""
    from agent_cascade.utils.utils import msg_field
    ts = msg_field(msg, 'timestamp', '') or ''
    content = msg_field(msg, 'content', '')
    if content is None:
        content = ''
    return (str(ts), str(content))


def _validate_session_display_name(name: Optional[str]) -> str:
    """Sanitize and validate session display name."""
    if not name or not isinstance(name, str):
        return ''
    cleaned = ''.join(c for c in name if c.isalnum() or c in (' ', '_', '-')).strip()
    return cleaned[:64]


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
            agent_pool,  # AgentPool instance
            agents: list,  # List of agent objects
            send_queue,  # asyncio.Queue
            broadcast_fn: Callable,  # async broadcast(data) -> None
            build_state_fn: Callable,  # build_state(responses=None, generating=None) -> dict
            start_gen_fn: Callable,  # Thread entry point for run_agent_thread
            session_lock: threading.Lock,
            app,  # FastAPI app object (holds security_check_semaphore, current_auto_security)
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
            MsgType.MESSAGE: self.handle_message,
            MsgType.CONTINUE: self.handle_continue,
            MsgType.STOP: self.handle_stop,
            MsgType.KV_STATE: self.handle_kv_state,
            MsgType.PAUSE: self.handle_pause,
            MsgType.RESUME_ALL: self.handle_resume_all,
            MsgType.RESUME: self.handle_resume,
            MsgType.TERMINATE_AGENT_INSTANCE: self.handle_terminate,
            MsgType.TERMINATE_SUB_AGENT: self.handle_terminate,  # Alias
            MsgType.RETRY: self.handle_retry,
            MsgType.RESET: self.handle_reset,
            MsgType.REFRESH_SOULS: self.handle_refresh_souls,
            MsgType.REFRESH_AGENTS: self.handle_refresh_agents,
            MsgType.RESTART_SERVER: self.handle_restart_server,
            MsgType.UPDATE_CONFIG: self.handle_update_config,
            MsgType.UPDATE_SETTINGS: self.handle_update_settings,
            MsgType.SET_WORK_FOLDERS: self.handle_set_work_folders,
            MsgType.UPDATE_ENDPOINTS: self.handle_update_endpoints,
            MsgType.UPDATE_API_PRIORITIES: self.handle_update_api_priorities,
            MsgType.APPROVE: self.handle_approve,
            MsgType.REJECT: self.handle_reject,
            MsgType.ASK_SECURITY: self.handle_ask_security,
            MsgType.SET_AUTO_SECURITY: self.handle_set_auto_security,
            MsgType.SET_CLI_AGENT_KEY: self.handle_set_cli_agent_key,
            MsgType.CLI_AGENT_OAUTH_MANUAL_CMD: self.handle_cli_agent_oauth_manual_cmd,
            MsgType.CLI_AGENT_OAUTH_RECHECK: self.handle_cli_agent_oauth_recheck,
            MsgType.SET_AFK: self.handle_set_afk,
            MsgType.EDIT_MESSAGE: self.handle_edit_message,
            MsgType.DELETE_MESSAGES: self.handle_delete_messages,
            MsgType.SELECT_AGENT: self.handle_select_agent,
            MsgType.SET_SESSION_NAME: self.handle_set_session_name,
            MsgType.LOAD_SESSION: self.handle_load_session,
            MsgType.INJECT: self.handle_inject,
            MsgType.DISMISS_QUEUE: self.handle_dismiss_queue,
            MsgType.EXPORT_SETTINGS: self.handle_export_settings,
            MsgType.IMPORT_SETTINGS: self.handle_import_settings,
            MsgType.MIGRATE_SETTINGS_LOCAL: self.handle_migrate_settings_local,
            MsgType.REQUEST_STATE: self.handle_request_state,
            MsgType.STACKS_LIST: self.handle_stacks_list,
            MsgType.STACKS_SWAP: self.handle_stacks_swap,
            MsgType.STACKS_SNAPSHOT: self.handle_stacks_snapshot,
            MsgType.STACKS_NEW: self.handle_stacks_new,
            MsgType.STACKS_DELETE: self.handle_stacks_delete,
        }

    # ── Public dispatch entry point ───────────────────────────────────────
    async def dispatch(self, data: dict) -> None:
        """Route a parsed WebSocket message to the appropriate handler."""
        msg_type = data.get(WsEnvelope.TYPE, '')
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

    async def handle_request_state(self, data: dict) -> None:
        """Handle frontend request for a full state refresh (tab switch, resync recovery).

        Builds a force_full stream_update frame for the requested instance and pushes
        it through the shared send queue. The frame is broadcast to all clients but only
        the requesting client's UI panel will render it (instance-scoped rendering).
        """
        from agent_cascade.api_integration_pkg.state_builder import build_stream_update_from_pool
        from agent_cascade.api_integration_pkg.streaming import (_last_force_full, _last_force_full_lock,
                                                                 _put_stream_update)
        from agent_cascade.log import logger

        instance_name = data.get(WsEnvelope.INSTANCE) or self.session['session_name']
        if not self.agent_pool:
            return

        try:
            su = build_stream_update_from_pool(
                pool=self.agent_pool,
                instance_name=instance_name,
                responses=None,
                force_full=True,
            )
            if su is not None and self.send_queue is not None:
                await _put_stream_update(self.send_queue, {WsEnvelope.TYPE: MsgType.STREAM_UPDATE, **su})
                # Reset the periodic timer so the next automatic force_full is 60s from now
                with _last_force_full_lock:
                    _last_force_full[instance_name] = time.monotonic()
        except Exception as e:
            logger.warning(f"request_state failed for {instance_name}: {e}")

    # ── Broadcast helper ──────────────────────────────────────────────────
    async def _broadcast(self, ws_type: str = MsgType.STATE, generating: Optional[bool] = None) -> None:
        """Broadcast state update to all WebSocket clients.

        Sends a full state snapshot via the broadcast function. The build_state_fn
        is called with optional generating override; it reads from pool.instances
        and includes all fields needed by the frontend (messages, instances, telemetry).

        The expensive O(N) build_state_fn call runs in a thread executor so it
        does not block the event loop (e.g. _sender_loop) during saturated send.
        """
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, lambda: self.build_state_fn(generating=generating))
        await self.broadcast_fn({WsEnvelope.TYPE: ws_type, **state})

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

    # ── Shared helpers used across handlers ─────────────────────────────

    def _resolve_instance_name(self, data: dict) -> str:
        """Resolve target agent name from message data."""
        return data.get('target_agent') or self.session['session_name']

    def _apply_session_cfg(self, data: dict) -> None:
        """Apply session config updates (agent_index, session_name, generate_cfg)."""
        if 'agent_index' in data:
            self.session['agent_index'] = int(data['agent_index'])
        if WsEnvelope.SESSION_NAME in data:
            value = str(data[WsEnvelope.SESSION_NAME]).strip()
            if settings.ENABLE_STACK_SWAP and self.agent_pool:
                # The wire field carries the display name; session['session_name'] is
                # the routing identity (which may be birth-stamped).
                resolved = self.agent_pool._resolve_instance_name(value, allow_display=True)
                if resolved in self.agent_pool.instances:
                    self.session['session_name'] = resolved
                elif self.session['session_name'] not in self.agent_pool.instances:
                    self.session['session_name'] = value
            else:
                self.session['session_name'] = value
        if WsEnvelope.GENERATE_CFG in data:
            _validate_disabled_tools(data[WsEnvelope.GENERATE_CFG])
            self.session['generate_cfg'] = data[WsEnvelope.GENERATE_CFG]

    def _sync_sub_agent_states(self) -> None:
        """Update instance_state for sub-agents after snapshot rollback."""
        for name in self.session['last_turn_snapshots']:
            if name != self.session['session_name'] and name in self.agent_pool.instance_state:
                sa_inst = self.agent_pool.get_instance(name)
                if sa_inst is not None:
                    with sa_inst._compression_lock:
                        conv_snapshot = list(sa_inst.conversation)
                    self.agent_pool.instance_state[name]['messages'] = conv_snapshot

    # ── Shared command helpers (used by both WS handlers and REST endpoints) ──
    # These live here (not in api_server.py) so the WebSocket and REST paths share a
    # single implementation and cannot drift. They take their dependencies explicitly
    # rather than relying on self, so create_app() can call them directly too.

    @staticmethod
    def do_stop(session: Dict[str, Any], session_lock: threading.Lock, agent_pool) -> None:
        """Core 'stop' logic — halt execution and transition active agents to IDLE.

        Shared by the WS ``stop`` handler and the REST ``POST /api/stop`` endpoint so
        the two paths stay in lockstep. Sets the stop flags under ``session_lock``,
        transitions every active instance to IDLE, calls ``agent_pool.stop_session()``,
        bumps the run generation, then cleans up the active stack / halted state.
        """
        from agent_cascade.log import logger

        with session_lock:
            session['stop_requested'] = True
            session['generating'] = False
            session['generation_id'] += 1

        if not agent_pool:
            return

        # Transition ALL active agents to IDLE state (not just reset)
        from agent_cascade.agent_instance import ACTIVE_STATES, AgentState, InvalidStateTransition

        transitioned = 0
        # Instances we force-transition to IDLE here. Their in-flight telemetry turn
        # must be closed below (BUG_0287) — record the names as we go.
        closed_turn_instances = []
        for inst_name, instance in list(agent_pool.instances.items()):
            try:
                agent_pool._mark_activity(inst_name)

                with instance._state_lock:
                    current_state = instance.state
                    if current_state in ACTIVE_STATES:
                        instance._transition(AgentState.IDLE)
                        transitioned += 1
                        closed_turn_instances.append(inst_name)
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
        agent_pool.stop_session()

        # BUG_0287: Stop transitions agents to IDLE directly in this handler and
        # bypasses the engine's _run_finalize (engine/run_loop.py) — the only other
        # record_turn_end call site. Without this, the stopped turn stays open in
        # TelemetryManager._active_turns and the next record_turn_start discards it
        # (losing its duration and LLM/tool attribution). record_turn_end pops and
        # no-ops on a missing turn, so this is idempotent even if the engine's own
        # finalize also runs.
        if (tel := getattr(agent_pool, 'telemetry', None)) is not None:
            for name in closed_turn_instances:
                try:
                    tel.record_turn_end(name)
                except Exception as e:
                    logger.warning(f"[STOP_TEL] record_turn_end failed for {name}: {e}")

        # Increment run generation AFTER slot release
        agent_pool._run_generation += 1

        # Diagnostic: Check for stuck slots after stop (only pay the get_status()
        # cost when debug logging is on — avoids per-stop I/O in production).
        if logger.isEnabledFor(logging.DEBUG) and \
                hasattr(agent_pool, 'api_router') and agent_pool.api_router:
            sched = agent_pool.api_router.scheduler
            status = sched.get_status()
            stuck = {k: v for k, v in status.items() if v['active_count'] > 0}
            if stuck:
                logger.warning(f"Stuck slots detected after stop_session: "
                               f"{stuck}")
            else:
                logger.debug('All slots released cleanly after stop_session')

        logger.debug(
            f"Transitioned {transitioned} agent(s) to IDLE, generation now={agent_pool._run_generation}")

        # Clean up active stack and halted state after stop_session()
        try:
            if hasattr(agent_pool, '_execution') and hasattr(agent_pool._execution, 'active_stack'):
                with agent_pool._execution._state_lock:
                    original_len = len(agent_pool._execution.active_stack)
                    # Mutate in place instead of replacing the list
                    agent_pool._execution.active_stack[:] = [
                        (name, depth)
                        for name, depth in agent_pool._execution.active_stack
                        if name not in agent_pool.terminated_instances
                    ]
                    removed_count = original_len - len(agent_pool._execution.active_stack)
                    if removed_count > 0:
                        logger.debug(
                            f"[STOP_STACK_CLEANUP] Removed {removed_count} terminated entries from active_stack")

            # Clear _halted_instances to prevent stale pause state after stop
            if hasattr(agent_pool, '_halted_instances'):
                agent_pool._halted_instances.clear()
        except Exception as e:
            logger.warning(f"[STOP_CLEANUP_ERROR] Error during slot/stack cleanup: {e}")

    @staticmethod
    def apply_afk(agent_pool, enabled: bool, message: Optional[str] = None,
                  mode: Optional[str] = None) -> None:
        """Set the server-backed AFK flag (shared by WS and REST /api/afk).

        ``mode`` (``'basic' | 'admin'``) is optional; ``None`` leaves the stored
        afk_mode untouched (toggle-only callers cannot clobber a UI-set mode).
        """
        om = getattr(agent_pool, 'operation_manager', None) if agent_pool else None
        if om is None:
            return
        om.set_afk(enabled, message, mode)
        if hasattr(agent_pool, '_save_pool_settings'):
            agent_pool._save_pool_settings()

    @staticmethod
    def _kv_result(action: str, instance_name: str, status: str,
                   reason: str = '', message: str = '') -> dict:
        """Canonical kv_state result shape.

        Carries ``action`` and ``instance_name`` on EVERY result — success AND failure —
        because the frontend ``case 'kv_state_result'`` renders ``${data.action}: ${data.message}``
        and the frame-relay test asserts all four fields.
        """
        return {WsEnvelope.STATUS: status, WsEnvelope.ACTION: action,
                WsEnvelope.INSTANCE_NAME: instance_name,
                WsEnvelope.REASON: reason, WsEnvelope.MESSAGE: message}

    @staticmethod
    def _kv_target(action: str, agent_pool, instance_name: str):
        """Shared save/load preamble: pool/instance lookup + capability gate.

        Returns the canonical failure result dict on any failure path, else the
        tuple ``(instance, cfg)``. Callers use::

            target = WsMessageHandler._kv_target('save', agent_pool, instance_name)
            if isinstance(target, dict):
                return target
            instance, cfg = target

        The ``not_capable`` wording is parameterized on ``action`` — save and
        load differ ONLY in that token (and save's extra parenthetical).
        """
        from agent_cascade import state_ops
        if not agent_pool:
            return WsMessageHandler._kv_result(
                action, instance_name, 'failed', 'no_pool', 'Agent pool not available')
        try:
            instance = agent_pool.get_instance(instance_name)
        except Exception:
            instance = None
        if instance is None:
            return WsMessageHandler._kv_result(
                action, instance_name, 'failed', 'no_instance',
                f'Instance {instance_name} not found')

        cfg = getattr(instance, '_last_endpoint_config', None)
        if not isinstance(cfg, dict) or not state_ops.is_state_save_capable(cfg):
            msg = ('KV state saving is not available for this agent '
                   '(no KV-capable endpoint allocated yet)'
                   if action == 'save' else
                   'KV state loading is not available for this agent')
            return WsMessageHandler._kv_result(
                action, instance_name, 'failed', 'not_capable', msg)
        return instance, cfg

    @staticmethod
    def do_save_kv(agent_pool, instance_name: str) -> dict:
        """Core manual KV save — shared by WS 'kv_state' and POST /api/state/save."""
        from agent_cascade.log import logger
        from agent_cascade import state_ops

        target = WsMessageHandler._kv_target('save', agent_pool, instance_name)
        if isinstance(target, dict):
            return target
        instance, cfg = target

        # Pure save, no label (D3), no unload (D4). save_instance_state never raises.
        ok = state_ops.save_instance_state(instance, set_label=False)
        if ok:
            logger.info(f"[KV_MANUAL] Saved KV state for {instance_name} "
                        f"({cfg.get('api_base')}/{cfg.get('model')})")
            return WsMessageHandler._kv_result('save', instance_name, 'ok',
                                               message='KV state saved')
        # save_instance_state logs the specific cause at debug; map the common one.
        return WsMessageHandler._kv_result(
            'save', instance_name, 'failed', 'save_failed',
            'Save failed — model is not loaded on its endpoint '
            '(idle-evicted), or the endpoint rejected the request')

    @staticmethod
    def do_load_kv(agent_pool, instance_name: str) -> dict:
        """Core manual KV load — shared by WS 'kv_state' and POST /api/state/load."""
        from agent_cascade.log import logger
        from agent_cascade import state_ops

        target = WsMessageHandler._kv_target('load', agent_pool, instance_name)
        if isinstance(target, dict):
            return target
        instance, cfg = target

        # Ownership guard (D5): refuse while ANY OTHER instance currently HOLDS the
        # target endpoint. Uses the engine's own signals, never agent state (a
        # released permit can coexist with a RUNNING state — router.py:947-962
        # clears _slot_release before the state transition — and an IDLE instance
        # may still have its model resident; see D5's accepted residual cost).
        target_key = (state_ops._normalize_api_base(cfg.get('api_base') or ''),
                      cfg.get('model') or '')
        # TWO keys, two normalizers — they are NOT interchangeable:
        #  * target_key   → compared against the sibling's own ``_last_endpoint_config``
        #                   (state_ops strips /v1, matching how the KV file is addressed).
        #  * target_map_key → compared against the router's held map, whose values are
        #                     built at router.py:651 with api_router_pkg.normalization
        #                     .normalize_api_base — that one KEEPS /v1 and rewrites
        #                     localhost (see its docstring: "Distinct from
        #                     state_ops._normalize_api_base"). Reusing target_key here
        #                     would never match on any /v1 endpoint, i.e. an inert guard.
        from agent_cascade.api_router_pkg.normalization import normalize_api_base
        target_map_key = (normalize_api_base(cfg.get('api_base') or ''),
                          cfg.get('model') or '')
        router = getattr(agent_pool, 'api_router', None)
        # Snapshot the held map under the router's own lock — it is documented as
        # "guarded by self._lock" (api_router_pkg/router.py:276) and a concurrent
        # acquire/release during iteration raises RuntimeError. Mirrors the engine's
        # own reader `_base_actively_held` (router.py:657-663).
        held_pairs = {}
        if router is not None:
            with router._lock:
                held_pairs = dict(router._instance_held_endpoint)
        def _busy(name):
            """The single busy-refusal shape (both scans report the same thing)."""
            return WsMessageHandler._kv_result(
                'load', instance_name, 'failed', 'busy',
                f'Another agent ({name}) currently holds this endpoint — '
                'Load would evict its model. Try again when it is idle.')

        # Scan (a): pool instances whose own ``_slot_release`` signal says they hold
        # a slot right now; compare against the sibling's own ``_last_endpoint_config``
        # with the state_ops normalizer (how the KV file is addressed). Defensive
        # backstop: the map write at router.py:653 is except-pass guarded, so a failed
        # write leaves the slot signal as the only witness — in the normal case scan
        # (b) already covers pool instances.
        for name, other in list(agent_pool.instances.items()):
            if name == instance_name:
                continue
            try:
                with other._state_lock:
                    holds_slot = getattr(other, '_slot_release', None) is not None
                if not holds_slot:
                    continue
                other_cfg = getattr(other, '_last_endpoint_config', None) or {}
                other_key = (state_ops._normalize_api_base(other_cfg.get('api_base', '')),
                             other_cfg.get('model', ''))
            except Exception:
                continue
            if other_key == target_key:
                return _busy(name)

        # Scan (b): the router's held map — authoritative for EVERY name, including
        # ones no longer in ``pool.instances``: the real orphan class is instances
        # popped from ``pool.instances`` by ``remove_instance`` (pool/lifecycle.py:119)
        # whose slot release (router.py:962, which pops the map entry) has not run
        # yet — dismissed/crashed instances. (Sub-agents are NOT orphans: pool.create_instance
        # registers them in ``pool.instances``, pool/lifecycle.py:94.) The map is also
        # authoritative for pool instances (written at slot acquire, router.py:653), so
        # this scan covers every holder — no per-instance map conjunct is needed above.
        # Compares target_map_key (the router's normalizer) — see the note above.
        for name, held_key in held_pairs.items():
            if name == instance_name:
                continue
            if held_key == target_map_key:
                return _busy(name)

        # Stable-label restore (D6). restore_held_slot_state is defensive by design:
        # it skips (returns False) if the held endpoint can't be resolved or the
        # saved file's endpoint differs (state_ops.py) — never raises.
        ok = state_ops.restore_held_slot_state(agent_pool, instance, instance_name,
                                               label=instance_name)
        if ok:
            logger.info(f"[KV_MANUAL] Loaded KV state for {instance_name}")
            return WsMessageHandler._kv_result('load', instance_name, 'ok',
                                               message='KV state loaded')
        return WsMessageHandler._kv_result(
            'load', instance_name, 'failed', 'load_failed',
            'Load failed — no saved state for this instance, its '
            'endpoint moved, or the model load timed out')

    def _start_gen_thread(self, history_copy=None, instance_name: str = '') -> None:
        """Launch the generation thread (shared boilerplate)."""
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
            args=(history_copy, agent_runner, gen_id, loop, instance_name),
            daemon=True,
        )
        thread.start()

    # ════════════════════════════════════════════════════════════════════
    #  Handler Methods (one per message type)
    # ════════════════════════════════════════════════════════════════════

    async def handle_message(self, data: dict) -> None:
        """Handle 'message' — user sends a new message to start agent generation."""
        text = data.get('text', '').strip()
        if not text:
            return

        if self._is_generating():
            if self.agent_pool:
                from agent_cascade.api_server import _parse_multimodal_content
                target = self._resolve_instance_name(data)
                parsed_content = _parse_multimodal_content(text)
                self.agent_pool.enqueue_message(target, parsed_content)
            return

        self._apply_session_cfg(data)

        from agent_cascade.api_server import _extract_system_message, _parse_multimodal_content
        parsed_content = _parse_multimodal_content(text)
        instance_name = self._resolve_instance_name(data)

        if self.agent_pool:
            inst = self.agent_pool.get_instance(instance_name)
            if inst is None or not inst.conversation:
                sys_content = _extract_system_message(self._get_agent())
                if sys_content:
                    from agent_cascade.api_integration import create_main_agent_instance
                    created = create_main_agent_instance(
                        pool=self.agent_pool,
                        instance_name=instance_name,
                        system_message_content=sys_content,
                    )
                    if created is not None:
                        # The root may have been minted with a birth stamp.
                        instance_name = created.instance_name
                        if self.session.get('session_name') not in self.agent_pool.instances:
                            self.session['session_name'] = instance_name

            inst = self.agent_pool.get_instance(instance_name)
            if inst is not None:
                with inst._compression_lock:
                    if inst._continue_saved_msg is not None:
                        from agent_cascade.log import logger
                        logger.debug(
                            f"[CONTINUE_FIX] Cleared stale _continue_saved_msg for {instance_name} on new message")
                        inst._continue_saved_msg = None

            self.agent_pool.enqueue_message(instance_name, parsed_content)

        self._start_gen_thread(instance_name=instance_name)
        await self._broadcast(generating=True)

    async def handle_continue(self, data: dict) -> None:
        """Handle 'continue' — send current stack as-is to the LLM."""
        if self._is_generating():
            return

        self._apply_session_cfg(data)

        instance_name = self._resolve_instance_name(data)
        inst = self.agent_pool.get_instance(instance_name) if self.agent_pool else None

        if inst is None:
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: 'No agent instance found to continue'})
            return

        with inst._compression_lock:
            history_copy = list(inst.conversation)

        self._start_gen_thread(history_copy=history_copy, instance_name=instance_name)
        await self._broadcast(generating=True)

    async def handle_stop(self, data: dict) -> None:
        """Handle 'stop' — stop all streaming and set ALL active agents to IDLE.

        Delegates the core logic to :meth:`do_stop` (shared with ``POST /api/stop``)
        so the WS and REST paths cannot drift, then broadcasts a ``done`` frame.
        """
        WsMessageHandler.do_stop(self.session, self._session_lock, self.agent_pool)
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
            # pool.pause() itself invalidates the stream cache (SlotsMixin) so the frontend sees
            # the new pause state promptly.
            self.agent_pool.pause()  # Sets _paused Event — backend blocks Phase 4 tool execution
            from agent_cascade.log import logger
            logger.info(f"Paused all instances: {inst_names}")

    async def handle_kv_state(self, data: dict) -> None:
        """Handle 'kv_state' — manual KV state save/load for one instance.

        Core logic lives in do_save_kv/do_load_kv (shared with the REST
        /api/state/save|load endpoints) so the WS and REST paths cannot drift.
        The httpx calls are blocking (30s client timeout; autoloader slot-save
        up to 120s) — offload to a thread so the event loop is never stalled.

        Like handle_pause, this does NOT touch the ``generating`` session flag and
        does NOT call _broadcast('done') — a manual KV op must not disrupt ongoing
        streaming state.
        """
        action = data.get(WsEnvelope.ACTION, '')
        instance_name = data.get(WsEnvelope.INSTANCE_NAME) or self.session['session_name']

        if action == 'save':
            result = await asyncio.to_thread(
                WsMessageHandler.do_save_kv, self.agent_pool, instance_name)
        elif action == 'load':
            result = await asyncio.to_thread(
                WsMessageHandler.do_load_kv, self.agent_pool, instance_name)
        else:
            result = WsMessageHandler._kv_result(
                action or 'unknown', instance_name, 'failed', 'bad_action',
                "kv_state requires action 'save' or 'load'")

        result[WsEnvelope.TYPE] = MsgType.KV_STATE_RESULT
        await self.broadcast_fn(result)
        # Refresh instance state (kv_state_capable) after a successful op.
        if result[WsEnvelope.STATUS] == 'ok':
            await self._broadcast()

    async def _broadcast_stacks(self) -> None:
        """Broadcast updated stacks listing to all clients."""
        try:
            await self.handle_stacks_list({})
        except Exception as e:
            from agent_cascade.log import logger
            logger.debug(f"Failed to broadcast stacks: {e}")

    async def handle_stacks_list(self, data: dict) -> None:
        """Handle 'stacks_list' — report the active stack and every parked stack's snapshots.

        Core logic lives in ``snapshot.list_stacks`` (shared with REST
        ``GET /api/stacks``) so the WS and REST paths cannot drift. Gated on
        ``settings.ENABLE_STACK_SWAP``: returns a WARNING and no payload when off (M1).
        """
        if not settings.ENABLE_STACK_SWAP:
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.WARNING,
                WsEnvelope.MESSAGE: 'stack swap disabled (set ENABLE_STACK_SWAP=true)',
            })
            return
        if self.agent_pool is None:
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.ERROR,
                WsEnvelope.MESSAGE: 'stacks_list: no agent pool',
            })
            return
        from agent_cascade import snapshot
        result = await asyncio.to_thread(snapshot.list_stacks, self.agent_pool)
        result[WsEnvelope.TYPE] = MsgType.STACKS_RESULT
        result[WsEnvelope.STATUS] = 'ok'
        await self.broadcast_fn(result)

    async def handle_stacks_swap(self, data: dict) -> None:
        """Handle 'stacks_swap' — park the active stack and activate ``target_stack_id``.

        Mirrors REST ``POST /api/stacks/swap``: the swap parks + restores (blocking),
        so it runs on a worker thread. Core logic is ``snapshot.swap_stack``. ``ValueError``
        (no such snapshot) is reported as a bad target; ``SnapshotIntegrityError`` as a
        failed activation. Does not touch the ``generating`` session flag.
        """
        if not settings.ENABLE_STACK_SWAP:
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.WARNING,
                WsEnvelope.MESSAGE: 'stack swap disabled (set ENABLE_STACK_SWAP=true)',
            })
            return
        if self.agent_pool is None:
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.ERROR,
                WsEnvelope.MESSAGE: 'stacks_swap: no agent pool',
            })
            return
        target = data.get('target_stack_id')
        target_snapshot_id = data.get('target_snapshot_id')
        payload: Dict[str, Any]
        if not target or not isinstance(target, str):
            payload = {'status': 'failed', 'reason': 'bad_target',
                       'message': "'target_stack_id' (str) required"}
        else:
            from agent_cascade.snapshot import SnapshotIntegrityError, swap_stack
            try:
                await asyncio.to_thread(swap_stack, self.agent_pool, target,
                                        target_snapshot_id=target_snapshot_id)
                reg = getattr(self.agent_pool, '_swap_registry', None)
                payload = {'status': 'ok',
                           'active_stack_id': getattr(reg, 'active_stack_id', None)}
                # Re-point session['session_name'] to the restored root
                roots = [n for n, i in self.agent_pool.instances.items() if getattr(i, 'parent_instance', None) is None]
                if roots:
                    self.session['session_name'] = roots[0]
                    from agent_cascade.agent_identity import strip_birth_stamp
                    from agent_cascade.session_state_store import save_session_name
                    save_session_name(strip_birth_stamp(roots[0]))
            except SnapshotIntegrityError as e:
                payload = {'status': 'failed', 'reason': 'integrity', 'message': str(e)}
            except ValueError as e:
                payload = {'status': 'failed', 'reason': 'bad_target', 'message': str(e)}
            except Exception as e:  # noqa: BLE001
                from agent_cascade.log import logger
                logger.exception('[STACK_SWAP] WS swap to %r failed', target)
                payload = {'status': 'failed', 'message': f'swap failed: {e}'}
        payload[WsEnvelope.TYPE] = MsgType.STACKS_RESULT
        await self.broadcast_fn(payload)
        if payload.get('status') == 'ok':
            self._clear_caches_safely()
            await self._broadcast()
            await self._broadcast_stacks()

    async def handle_stacks_snapshot(self, data: dict) -> None:
        """Handle 'stacks_snapshot' — capture a non-destructive fleet snapshot.

        Mirrors REST ``POST /api/stacks/{stack_id}/snapshot``: core is
        ``snapshot.snapshot_stack`` (captures the live fleet, never draining it).
        ``stack_id`` is optional — when omitted the capture derives it from the root
        instance. A ``None`` result means there was no live fleet to snapshot.
        """
        if not settings.ENABLE_STACK_SWAP:
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.WARNING,
                WsEnvelope.MESSAGE: 'stack swap disabled (set ENABLE_STACK_SWAP=true)',
            })
            return
        if self.agent_pool is None:
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.ERROR,
                WsEnvelope.MESSAGE: 'stacks_snapshot: no agent pool',
            })
            return
        if self._is_generating():
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.WARNING,
                WsEnvelope.MESSAGE: 'Cannot take snapshot while generation is in progress',
            })
            return
        from agent_cascade import snapshot
        stack_id = data.get('stack_id')
        res = await asyncio.to_thread(snapshot.snapshot_stack, self.agent_pool, stack_id, trigger='user')
        if res is None:
            payload = {'status': 'failed', 'reason': 'no_fleet', 'message': 'no live fleet to snapshot'}
        else:
            payload = {'status': 'ok', **res}
        payload[WsEnvelope.TYPE] = MsgType.STACKS_RESULT
        await self.broadcast_fn(payload)
        if payload.get('status') == 'ok':
            await self._broadcast_stacks()

    async def handle_stacks_delete(self, data: dict) -> None:
        """Handle 'stacks_delete' — delete a single session snapshot or whole stack."""
        if not settings.ENABLE_STACK_SWAP:
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.WARNING,
                WsEnvelope.MESSAGE: 'stack swap disabled (set ENABLE_STACK_SWAP=true)',
            })
            return
        if self.agent_pool is None:
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.ERROR,
                WsEnvelope.MESSAGE: 'stacks_delete: no agent pool',
            })
            return
        stack_id = data.get('stack_id')
        snapshot_id = data.get('snapshot_id')
        if not stack_id or not isinstance(stack_id, str):
            payload = {'status': 'failed', 'reason': 'bad_request', 'message': "'stack_id' required"}
            payload[WsEnvelope.TYPE] = MsgType.STACKS_RESULT
            await self.broadcast_fn(payload)
            return

        from agent_cascade.snapshot import ActiveStackDeletionError, delete_snapshot, delete_stack
        try:
            if snapshot_id:
                res = await asyncio.to_thread(delete_snapshot, self.agent_pool, stack_id, snapshot_id)
            else:
                res = await asyncio.to_thread(delete_stack, self.agent_pool, stack_id)
            payload = {'status': 'ok', **res}
        except ActiveStackDeletionError as e:
            payload = {'status': 'failed', 'reason': 'active', 'message': str(e)}
        except ValueError as e:
            payload = {'status': 'failed', 'reason': 'not_found', 'message': str(e)}
        except Exception as e:
            from agent_cascade.log import logger
            logger.exception('[STACK_OPS] WS delete failed for stack %r, snapshot %r', stack_id, snapshot_id)
            payload = {'status': 'failed', 'message': f'delete failed: {e}'}

        payload[WsEnvelope.TYPE] = MsgType.STACKS_RESULT
        await self.broadcast_fn(payload)
        if payload.get('status') == 'ok':
            await self._broadcast_stacks()

    async def handle_stacks_new(self, data: dict) -> None:
        """Park the active session and start a named fresh root (stack-safe New Session).

        ``name`` is the display name; empty -> DEFAULT_SESSION_NAME. The pool mints the birth
        stamp, so an empty name yields ``Maine_<timestamp>``.
        """
        if not settings.ENABLE_STACK_SWAP or self.agent_pool is None:
            await self.handle_reset(data)
            return

        self._stop_generation()
        self._signal_stop()

        from agent_cascade import snapshot
        from agent_cascade.agent_identity import strip_birth_stamp
        from agent_cascade.session_state_store import DEFAULT_SESSION_NAME, save_session_name

        display = _validate_session_display_name(data.get('name'))
        parked = None
        if self.agent_pool.instances:
            parked = await asyncio.to_thread(snapshot.park_active_stack,
                                             self.agent_pool, trigger='new_session')

        from agent_cascade.api_server import _extract_system_message
        from agent_cascade.api_integration import create_main_agent_instance
        sys_content = _extract_system_message(self._get_agent()) or ''
        created = create_main_agent_instance(
            pool=self.agent_pool,
            instance_name=display or DEFAULT_SESSION_NAME,
            system_message_content=sys_content)
        if created is None:
            if parked is not None:
                try:
                    snapshot.activate_stack(self.agent_pool, parked)
                except Exception:
                    from agent_cascade.log import logger
                    logger.critical('[STACK_SWAP] new-session rollback failed', exc_info=True)
            await self._broadcast_stacks()
            return

        self.session['session_name'] = created.instance_name
        save_session_name(strip_birth_stamp(created.instance_name))
        snapshot.register_active_stack(self.agent_pool, created.instance_name)
        if self.agent_pool.stopped:
            self.agent_pool.stopped = False
        self._clear_caches_safely()
        await self._broadcast()
        await self._broadcast_stacks()

    async def handle_resume_all(self, data: dict) -> None:
        """Handle 'resume_all' — resume ALL paused instances."""
        if self.agent_pool:
            # pool.resume() itself invalidates the stream cache (SlotsMixin).
            self.agent_pool.resume()  # Clears _paused Event — backend unblocks Phase 4 tool execution
            from agent_cascade.log import logger
            logger.info('Cleared global pause flag — all agents will resume naturally')

    async def handle_resume(self, data: dict) -> None:
        """Handle 'resume' — restore agent pools from logs and restart generation."""
        target_instance = data.get(WsEnvelope.INSTANCE_NAME, self.session['session_name'])
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
                logger.info('Main session was still generating — signalling stop before resume.')
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
                    from agent_cascade.log import logger as _logger
                    from agent_cascade.utils.pool_validation import validate_message_pool

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
                                from agent_cascade.instance_id import get_session_log_dir
                                log_dir = get_session_log_dir(self.agent_pool)
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
                                            if 'metadata' not in item and 'event' not in item:
                                                recov.append(item)
                                        except json.JSONDecodeError as e:
                                            _logger.debug(f"Skipping malformed JSONL line in agent pool recovery: {e}")

                            # Only overwrite pool if recovered data is valid
                            if recov and validate_message_pool(recov, sa_name):
                                _logger.info(f"Restoring agent instance {sa_name} conversation from log during resume "
                                             f"({len(recov)} messages)")
                                sa_inst = self.agent_pool.get_instance(sa_name)
                                if sa_inst is not None:
                                    sa_inst.rebuild_conversation(recov)
                            else:
                                _logger.warning(f"Could not restore agent instance {sa_name} pool — "
                                                f"no valid recovery data found in logs")
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
        instance_name = data.get(WsEnvelope.INSTANCE_NAME)
        if instance_name and self.agent_pool:
            inst = self.agent_pool.get_instance(instance_name)
            from agent_cascade.agent_instance import ACTIVE_STATES, AgentState
            from agent_cascade.log import logger

            # SAFEGUARD: Never allow terminating the root orchestrator
            is_root = (inst is not None and inst.parent_instance is None)

            if is_root:
                logger.warning(
                    f"Terminate requested for root orchestrator '{instance_name}' — blocked. Transitioning to IDLE instead."
                )
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
        """Handle 'retry' — remove trailing assistant/function messages, keep user message, send stack."""
        if self._is_generating():
            return

        self._apply_session_cfg(data)
        instance_name = self._resolve_instance_name(data)

        # 1. Rollback all agents to start of last turn
        if self.agent_pool and self.session.get('last_turn_snapshots'):
            self.agent_pool.rollback_to_snapshots(self.session['last_turn_snapshots'], reason='User retry')
            self._sync_sub_agent_states()

        # 2. Trim only trailing ASSISTANT/FUNCTION messages (leave user message in place)
        if self.agent_pool:
            inst = self.agent_pool.get_instance(instance_name)
            if inst is not None:
                from agent_cascade.llm.schema import ASSISTANT, FUNCTION
                from agent_cascade.utils.utils import msg_field

                count = 0
                while inst.conversation and count < len(inst.conversation):
                    if msg_field(inst.conversation[-1 - count], 'role') in (ASSISTANT, FUNCTION):
                        count += 1
                    else:
                        break

                if count > 0:
                    self.agent_pool._rollback_instance(
                        instance_name,
                        pop_count=count,
                        sync_logger=True,
                        preserve_system_user=False,
                        reason='User retry: trim trailing assistant/function messages',
                    )

        # Early exit if no usable state
        inst = self.agent_pool.get_instance(instance_name) if self.agent_pool else None
        if not inst or not inst.conversation:
            await self._broadcast()
            return

        if self.agent_pool:
            # Clear active tools/agent stack
            self.agent_pool.active_stack_clear()

        self._start_gen_thread(instance_name=instance_name)
        await self._broadcast(generating=True)

    async def handle_reset(self, data: dict) -> None:
        """Handle 'reset' — clear conversation and reset session."""
        if self.agent_pool:
            inst = self.agent_pool.get_instance(self.session['session_name'])
            if inst is not None:
                inst.reset_conversation()
                try:
                    self.agent_pool._logger.create_new_session(self.session['session_name'], inst.agent_class)
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

    def _do_refresh_agents(self) -> list:
        """Perform agent template refresh. Returns list of agent names after refresh.

        Shared by handle_refresh_souls and handle_refresh_agents to avoid duplication.
        Updates self.agents in-place (clear/extend required because closures in
        create_app() hold references to this exact list object).
        """
        if not self.agent_pool:
            return []

        # Reload templates from disk
        self.agent_pool.refresh_agents()

        # Get new agent list
        agent_names = self.agent_pool.list_agents()
        if not agent_names:
            from agent_cascade.log import logger
            logger.warning('No agents found after refresh')
            return []

        # Filter out None values in case get_agent fails for some template
        new_agents = [a for a in (self.agent_pool.get_agent(name) for name in agent_names) if a is not None]

        # Ensure orchestrator is at index 0 if present
        if 'orchestrator' in self.agent_pool.agents:
            orch = self.agent_pool.agents['orchestrator']
            new_agents = [orch] + [a for a in new_agents if a != orch]

        # Mutate agents list in-place under lock. clear()/extend() is REQUIRED here
        # because closures in api_server.py create_app() reference this exact list
        # object (used by /api/agents, build_state model lookups, etc.). Direct
        # assignment would break those references.
        with self._session_lock:
            self.agents.clear()
            self.agents.extend(new_agents)

        return agent_names

    async def handle_refresh_souls(self, data: dict) -> None:
        """Handle 'refresh_souls' — refresh agent templates and broadcast state."""
        try:
            await asyncio.to_thread(self._do_refresh_agents)
        except Exception as e:
            from agent_cascade.log import logger
            logger.error(f"Agent refresh failed: {e}")
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: f'Agent refresh failed: {e}'})
            return

        await self._broadcast()

    async def handle_refresh_agents(self, data: dict) -> None:
        """Handle 'refresh_agents' — reload templates and return agent list.

        Unlike refresh_souls (broadcast-only), this returns the discovered agent
        names via send_queue so callers (including agents invoking via tool calls)
        can programmatically determine what's loaded after refresh.
        """
        try:
            agent_names = await asyncio.to_thread(self._do_refresh_agents)
        except Exception as e:
            from agent_cascade.log import logger
            logger.error(f"Agent refresh failed: {e}")
            await self.send_queue.put({
                WsEnvelope.TYPE: MsgType.REFRESH_AGENTS_RESULT,
                'agents': [],
                WsEnvelope.ERROR: str(e),
            })
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: f'Agent refresh failed: {e}'})
            return

        # Return result to caller for programmatic use
        await self.send_queue.put({
            WsEnvelope.TYPE: MsgType.REFRESH_AGENTS_RESULT,
            'agents': agent_names,
        })

        # Also broadcast full state update (consistent with handle_refresh_souls)
        await self._broadcast()

    async def handle_restart_server(self, data: dict) -> None:
        """Handle 'restart_server' — restart the server process."""
        from agent_cascade.log import logger
        from agent_cascade.server_restart import restart_server_process
        logger.warning('Server restart requested via UI')
        await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: 'Server is restarting... Please wait.'})
        if settings.ENABLE_STACK_SWAP and settings.SNAPSHOT_AUTOSAVE_ENABLED and self.agent_pool:
            try:
                from agent_cascade import snapshot
                await asyncio.to_thread(snapshot.snapshot_stack, self.agent_pool, trigger='restart')
            except Exception as e:
                logger.warning('[STACK_SWAP] restart autosave failed (non-fatal): %s', e)
        restart_server_process()

    async def handle_update_config(self, data: dict) -> None:
        """Handle 'update_config' — delegate to ConfigUpdateRouter for key dispatch.

        Also updates the live disabled_tools cache in agent_pool and applies UI config
        to all active instances immediately so tool assignment changes take effect in real time.
        Persists ALL non-cosmetic settings to pool_settings.json (including disabled_tools).
        """
        from agent_cascade.api_integration import _apply_ui_config
        from agent_cascade.config_handlers import ConfigUpdateRouter
        from agent_cascade.settings_registry import ALL_KEYS, EXPORT_IMPORT_ALLOWLIST

        if WsEnvelope.GENERATE_CFG in data:
            # DEPRECATED legacy full-push path: route through the registry so unknown keys
            # (UI-only cosmetic keys the server has no handler for) are dropped, not applied.
            # New clients should send the 'update_settings' delta message instead.
            # The session stores the FILTERED dict: other readers of session['generate_cfg']
            # (api_server, security_handler) must not see cosmetic UI-only keys.
            ui_cfg = {k: v for k, v in data[WsEnvelope.GENERATE_CFG].items() if k in ALL_KEYS}
            self.session['generate_cfg'] = ui_cfg

            router = ConfigUpdateRouter(self.agent_pool, self.agents)
            await router.apply(ui_cfg)

            # Save PoolSettings once if any persisted settings were modified
            # (pool fields, extras, and the disk-only keys the registry tracks).
            persist_keys_hit = {k for k in ui_cfg if k in EXPORT_IMPORT_ALLOWLIST}
            if persist_keys_hit and self.agent_pool:
                if hasattr(self.agent_pool, '_save_pool_settings'):
                    self.agent_pool._save_pool_settings()

            # Update live disabled_tools config in agent_pool for real-time resolution
            # (Handler already validates and sets this; apply to instances separately)
            if 'disabled_tools' in ui_cfg and self.agent_pool:
                # Apply UI config to all active instances so changes take effect immediately.
                # Snapshot keys first to avoid RuntimeError from dict changing during iteration.
                for instance_name in list(self.agent_pool.instances.keys()):
                    _apply_ui_config(self.agent_pool, instance_name, ui_cfg)

        await self._broadcast()

    async def handle_update_settings(self, data: dict) -> None:
        """Handle 'update_settings' — the backend-authoritative per-key settings delta.

        Message shape: ``{"type": "update_settings", "base_revision": N,
        "changes": {key: value, ...}}``.

        Pipeline (plan §11): each key is validated against the registry (unknown key →
        rejected per-key; type/enum violation → rejected per-key); accepted keys are applied
        to live state via the config handlers; each touched bucket persists exactly once; the
        shared ``settings_revision`` is bumped. A stale ``base_revision`` never rejects the
        batch — it is logged, the touched keys still apply, and a fresh revision broadcasts
        (no modal).

        Broadcasts ``settings_applied {revision, applied, rejected}`` to ALL clients (there is
        no per-client send channel) and, when keys applied, ``settings_changed {revision,
        changes}`` to all clients.
        """
        from agent_cascade.log import logger
        from agent_cascade.runtime_state import state as _runtime_state
        from agent_cascade.settings_delta import apply_settings_changes, log_stale_revision

        base_revision = data.get(WsEnvelope.BASE_REVISION)
        changes = data.get('changes') or {}
        if not isinstance(changes, dict):
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.SETTINGS_APPLIED,
                                    WsEnvelope.REVISION: int(_runtime_state.settings_revision),
                                    'applied': [], 'rejected': {'changes': 'expected an object'}})
            return

        # Stale-tab guard (plan §11.3): log a revision mismatch but DO NOT reject the batch.
        log_stale_revision(base_revision, _runtime_state.settings_revision, MsgType.UPDATE_SETTINGS.value)

        applied, rejected, changes_out, revision = await apply_settings_changes(
            self.agent_pool, self.agents, changes, session=self.session)

        if applied:
            logger.info('[update_settings] applied %d key(s): %s (revision=%d)',
                        len(applied), applied, revision)

        # Both messages go through broadcast_fn (all clients — no per-client send channel):
        # announce applied/rejected, then broadcast the changed state when keys applied.
        await self.broadcast_fn({WsEnvelope.TYPE: MsgType.SETTINGS_APPLIED, WsEnvelope.REVISION: revision,
                                 'applied': applied, 'rejected': rejected})
        if applied:
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.SETTINGS_CHANGED, WsEnvelope.REVISION: revision,
                                     'changes': changes_out})

    async def handle_export_settings(self, data: dict) -> None:
        """Handle 'export_settings' — return all persistent settings as JSON.

        Returns pool_settings.json contents plus current disabled_tools config.
        Sent back via WebSocket response message.
        """
        from agent_cascade.log import logger

        if not self.agent_pool:
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: 'Agent pool not available for export'})
            return

        try:
            # Load from disk (most authoritative source) and merge with live state
            settings_data = self.agent_pool.persisted_pool_settings()

            # Merge live PoolSettings (in case runtime changes weren't persisted yet)
            if hasattr(self.agent_pool, 'settings'):
                settings_data.update(self.agent_pool.settings.to_dict())

            # Add complete effective disabled_tools for each agent template.
            # Use _ui_disabled_tools as instance_override (same as runtime resolution); after
            # Layer 3 removal the export reflects the effective disabled_tools from user config
            # (UI is the single source of truth) plus the safe baseline for unconfigured agents.
            from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

            with self.agent_pool._ui_disabled_tools_lock:
                ui_disabled_tools = dict(
                    self.agent_pool._ui_disabled_tools) if self.agent_pool._ui_disabled_tools else {}

            # Snapshot templates to avoid concurrent modification during iteration.
            # Templates are only modified during reload (rare), so this is low-risk but good practice.
            effective_disabled = {}
            for name, template in dict(self.agent_pool.templates).items():
                try:
                    agent_name = getattr(template, 'name', name)
                    agent_type = getattr(template, 'agent_type', '') or ''

                    # Resolve same way as runtime: UI config + defense-in-depth defaults
                    disabled = resolve_disabled_tools_for_agent(
                        instance_override={'disabled_tools': ui_disabled_tools},
                        template_cfg=None,
                        agent_name=agent_name,
                        agent_type=agent_type,
                    )

                    if disabled:
                        effective_disabled[agent_name] = sorted(disabled)
                except Exception as e:
                    logger.debug(f"[export_settings] Failed to resolve disabled_tools for template '{name}': {e}")

            if effective_disabled:
                settings_data['disabled_tools'] = effective_disabled

            # Add work folders and workspace from operation_manager
            if hasattr(self.agent_pool, 'operation_manager') and self.agent_pool.operation_manager:
                om = self.agent_pool.operation_manager
                settings_data['work_access_folders_ro'] = [str(p) for p in om.extra_work_folders_ro]
                settings_data['work_access_folders_rw'] = [str(p) for p in om.extra_work_folders_rw]
                settings_data['default_workspace'] = str(om.base_dir)

            # Add auto_security from app state
            settings_data['auto_security'] = getattr(self.app, 'current_auto_security', False)

            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.EXPORT_SETTINGS,
                WsEnvelope.SETTINGS: settings_data,
            })
        except Exception as e:
            logger.error(f"[export_settings] Failed to export settings: {e}")
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: f'Export failed: {str(e)}'})

    async def handle_import_settings(self, data: dict) -> None:
        """Handle 'import_settings' — validate and apply settings from JSON blob.

        Silently skips unknown fields for future-proofing. Validates types where possible.
        For tool assignments: only applies tools that currently exist in the registry.
        """
        from agent_cascade.log import logger

        if not self.agent_pool:
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: 'Agent pool not available for import'})
            return

        settings_json = data.get(WsEnvelope.SETTINGS)
        if not isinstance(settings_json, dict):
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: 'Invalid settings: must be a JSON object'})
            return

        try:
            # Apply via update_config — this routes each key to its handler which validates types
            from agent_cascade.api_integration import _apply_ui_config
            from agent_cascade.config_handlers import ConfigUpdateRouter
            from agent_cascade.settings_registry import EXPORT_IMPORT_ALLOWLIST

            # Filter to known keys only (silently skip unknown fields).
            # Uses the registry-derived allowlist (includes the 7 disk-only keys that the
            # historical POOL_SETTINGS_KEYS|EXTRA_PERSIST_KEYS dropped — BUG_0214 fix).
            filtered_cfg = {k: v for k, v in settings_json.items() if k in EXPORT_IMPORT_ALLOWLIST}

            if not filtered_cfg:
                await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: 'No recognized settings found in import data'})
                return

            # Apply each setting through the router (handles validation)
            router = ConfigUpdateRouter(self.agent_pool, self.agents)
            await router.apply(filtered_cfg)

            # Persist to disk
            if hasattr(self.agent_pool, '_save_pool_settings'):
                self.agent_pool._save_pool_settings()

            # Apply to all active instances
            for instance_name in list(self.agent_pool.instances.keys()):
                _apply_ui_config(self.agent_pool, instance_name, filtered_cfg)

            # Handle auto_security if present in import data.
            # BUG_0029: routes through the single writer (pool.set_auto_security) — this is
            # no longer a separate "third writer"; there is exactly one writer now.
            if 'auto_security' in settings_json:
                if self.agent_pool and hasattr(self.agent_pool, 'set_auto_security'):
                    self.agent_pool.set_auto_security(bool(settings_json['auto_security']))
                    await self.broadcast_fn({
                        WsEnvelope.TYPE: MsgType.INFO,
                        WsEnvelope.MESSAGE: f"Auto-security mode {'enabled' if settings_json['auto_security'] else 'disabled'}"
                    })
                else:
                    logger.warning('No agent_pool available during import — auto_security not applied')

            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.IMPORT_SETTINGS,
                WsEnvelope.STATUS: 'success',
                'applied_keys': list(filtered_cfg.keys()),
                WsEnvelope.SETTINGS: filtered_cfg,
            })
        except Exception as e:
            logger.error(f"[import_settings] Failed to import settings: {e}")
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: f'Import failed: {str(e)}'})

    async def handle_migrate_settings_local(self, data: dict) -> None:
        """Handle 'migrate_settings_local' — one-time migration from browser localStorage.

        Sent by the UI on first connect after upgrade. Carries the old local settings
        blob; the server applies ONLY keys it does not already have persisted (server
        always wins on conflict). The LLM config is seeded from the local blob only when
        ``api_endpoints.json`` has no ``default_llm_cfg`` section.

        Message shape: ``{"type": "migrate_settings_local", "settings": {key: value, ...}}``.

        Idempotency is enforced server-side: after a successful migration the server
        records ``_settings_migrated_v1`` in ``config/session_state.json`` and every later
        request — from a second client or a headless run — is short-circuited by that
        sentinel. The UI-side localStorage flag is only a fast path that avoids sending
        the blob at all.
        """
        from agent_cascade.log import logger
        from agent_cascade.session_state_store import is_settings_migrated, mark_settings_migrated
        from agent_cascade.settings_delta import apply_settings_changes
        from agent_cascade.settings_registry import (
            EXPORT_IMPORT_ALLOWLIST,
            LLM_CFG_KEYS,
            SERVER_WINS_KEYS,
        )

        settings = data.get(WsEnvelope.SETTINGS)
        if not isinstance(settings, dict):
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.MIGRATE_SETTINGS_LOCAL, WsEnvelope.STATUS: 'error',
                                     WsEnvelope.MESSAGE: 'expected a settings object'})
            return

        # Server-side sentinel: the migration is one-time per server. A second client, or
        # a headless run replaying the blob, would otherwise re-apply keys the server
        # deliberately skipped (SERVER_WINS_KEYS) or later changed.
        if is_settings_migrated():
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.MIGRATE_SETTINGS_LOCAL, WsEnvelope.STATUS: 'ok',
                                     'applied': [], 'skipped': 'already_migrated'})
            return

        # Keys the migration may fill gaps in: every persisted pool/extra key plus the
        # llm_cfg keys. Both sets are registry-derived.
        persisted_keys = EXPORT_IMPORT_ALLOWLIST | LLM_CFG_KEYS

        # Which pool/extra keys the server already has on disk (empty ⇒ migrate everything).
        existing_pool = (self.agent_pool.persisted_pool_settings()
                         if hasattr(self.agent_pool, 'persisted_pool_settings') else {})

        # Does the router's persisted config already carry a default_llm_cfg section?
        router = getattr(self.agent_pool, 'api_router', None)
        has_default_llm_cfg = bool(router and router.has_default_llm_cfg())

        # Filter: only send keys the server does NOT already have.
        missing_keys: dict = {}
        for key, value in settings.items():
            if key in SERVER_WINS_KEYS:
                continue  # server wins unconditionally
            if key not in persisted_keys:
                continue  # not a known registry key — skip
            if key in LLM_CFG_KEYS:
                # LLM keys: only migrate if the on-disk file has no default_llm_cfg section.
                if has_default_llm_cfg:
                    continue  # server already has LLM config — skip
                missing_keys[key] = value
            else:
                # Pool/extra keys: skip if already in the on-disk pool settings file.
                if key in existing_pool:
                    continue
                missing_keys[key] = value

        # Migration pass complete: record the sentinel so a later client (or a headless
        # replay of the blob) is short-circuited by is_settings_migrated() above.
        mark_settings_migrated()

        if not missing_keys:
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.MIGRATE_SETTINGS_LOCAL, WsEnvelope.STATUS: 'ok',
                                     'applied': []})
            return

        # Apply through the standard delta pipeline (one persist per bucket).
        applied, rejected, changes_out, revision = await apply_settings_changes(
            self.agent_pool, self.agents, missing_keys, session=self.session)

        logger.info('[migrate_settings_local] applied %d key(s): %s (revision=%d)',
                    len(applied), applied, revision)
        await self.broadcast_fn({WsEnvelope.TYPE: MsgType.MIGRATE_SETTINGS_LOCAL, WsEnvelope.STATUS: 'ok',
                                 'applied': applied, 'rejected': rejected})

    async def handle_set_work_folders(self, data: dict) -> None:
        """Handle 'set_work_folders' — explicit user action to set work folders.

        Unlike update_config (which may come from stale tabs via getGenerateCfg),
        this message type is only sent when the user clicks the Save button in the
        settings panel. Therefore, empty arrays are treated as intentional clears.

        Note: Uses operation_manager.set_extra_work_folders() for consistent path
        resolution and error handling with the rest of the codebase.
        """
        from agent_cascade.log import logger as _logger
        if self.agent_pool is None or not hasattr(self.agent_pool,
                                                  'operation_manager') or self.agent_pool.operation_manager is None:
            return

        om = self.agent_pool.operation_manager

        ro_raw = data.get('work_access_folders_ro')
        rw_raw = data.get('work_access_folders_rw')

        # Collect paths for each type — only process keys explicitly present in the message.
        # Empty arrays are valid here (explicit user action = intentional clear).
        ro_updated = False
        rw_updated = False

        if isinstance(ro_raw, list):
            ro_paths = [p.strip() for p in ro_raw if p.strip()]
            ro_updated = True
        else:
            ro_paths = None

        if isinstance(rw_raw, list):
            rw_paths = [p.strip() for p in rw_raw if p.strip()]
            rw_updated = True
        else:
            rw_paths = None

        # If neither key was present, nothing to do.
        if not ro_updated and not rw_updated:
            _logger.debug('[set_work_folders] No work folder keys provided, ignoring')
            return

        try:
            # Preserve existing values for any type not explicitly updated.
            final_ro = ro_paths if ro_updated else [str(p) for p in om.extra_work_folders_ro]
            final_rw = rw_paths if rw_updated else [str(p) for p in om.extra_work_folders_rw]

            # Delegate to operation_manager for consistent path resolution/validation.
            # This handles invalid paths gracefully (logs warning, skips bad entries).
            om.set_extra_work_folders(final_ro, final_rw)
            _logger.info('[set_work_folders] Explicit save — RO=%s, RW=%s', ro_paths, rw_paths)

            # Persist the explicit save (including an intentional clear) to disk so a
            # restart restores the exact folder state the user last saved.
            if hasattr(self.agent_pool, '_save_pool_settings'):
                self.agent_pool._save_pool_settings()
        except Exception as e:
            _logger.error('[set_work_folders] Failed to set work folders: %s', e)

        await self._broadcast()

    async def handle_update_endpoints(self, data: dict) -> None:
        """Handle 'update_endpoints' — bulk update all endpoints and priorities.

        Import-style operation only (single-endpoint edits go through the per-endpoint REST
        routes). Accepts an optional ``base_revision`` (endpoints-list revision) as a
        stale-tab guard: a mismatch is logged but the bulk write still applies (last-write
        wins, mirroring the settings conflict policy).
        """
        from agent_cascade.log import logger
        from agent_cascade.runtime_state import state as _runtime_state
        from agent_cascade.settings_delta import log_stale_revision
        base_revision = data.get(WsEnvelope.BASE_REVISION)
        log_stale_revision(base_revision, _runtime_state.endpoints_revision, MsgType.UPDATE_ENDPOINTS.value)
        if self.agent_pool and hasattr(self.agent_pool, 'api_router'):
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
        reason = data.get(WsEnvelope.REASON, 'Rejected by user')
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
            self.agent_pool,
            self.session,
            self.app,
            self.send_queue,
            self.broadcast_fn,
        )
        await sec.run_check(data)

    async def handle_set_auto_security(self, data: dict) -> None:
        """Handle 'set_auto_security' — toggle Auto-Ask mode.

        BUG_0029 Phase 2 Step 6: calls state.set_auto_security directly (single setter).
        The pool's set_auto_security wraps this with change-detection + persistence, so
        we call through the pool when available for the save-on-change behaviour.
        """
        enabled = data.get('enabled', False)
        if self.agent_pool:
            self.agent_pool.set_auto_security(enabled)
        else:
            from agent_cascade.runtime_state import state as _runtime_state
            _runtime_state.set_auto_security(enabled)
        # Broadcast updated state to all clients immediately, preventing stale overrides from pending messages
        await self._broadcast()

    async def handle_set_cli_agent_key(self, data: dict) -> None:
        """Persist an external-CLI API key (Web UI "Enter key" button) via the single secrets writer.

        Never routes the key through ``update_settings``/``update_config`` (it must not appear in
        the pool_settings blob or the state broadcast). Key-less harnesses (``adapter.auth_env_var``
        None — e.g. antigravity's cached OAuth) are a no-op: there is nothing to store. The value
        is written by ``secrets_loader.set_secret`` which never logs it.
        """
        harness = data.get(WsEnvelope.HARNESS)
        if not harness or not isinstance(harness, str):
            return
        from agent_cascade.engine.cli_agent_adapters import HarnessNotFoundError, get_adapter
        try:
            adapter = get_adapter(harness)
        except HarnessNotFoundError:
            return
        if getattr(adapter, 'auth_env_var', None) is None:
            return  # key-less harness (antigravity): nothing to store
        from config.secrets_loader import set_secret
        set_secret(f'cli_{harness}_key', str(data.get('key') or ''))
        await self._broadcast()

    async def _emit_cli_agent_oauth(self, payload: dict) -> None:
        """Send one OAuth lifecycle event to the connected clients (no-op in unit harnesses)."""
        broadcast = getattr(self, 'broadcast_fn', None)
        if broadcast is not None:
            await broadcast(payload)

    async def _cli_agent_oauth_fail(self, harness: Optional[str], reason: str,
                                    retryable: bool = False) -> None:
        await self._emit_cli_agent_oauth({
            WsEnvelope.TYPE: MsgType.CLI_AGENT_OAUTH_FAILED, WsEnvelope.HARNESS: harness,
            WsEnvelope.REASON: reason, WsEnvelope.RETRYABLE: retryable,
        })

    async def handle_cli_agent_oauth_manual_cmd(self, data: dict) -> None:
        """Build the one-time manual login command for an OAuth harness (Web UI "Log in").

        agy's OAuth URL is TTY-bound, so the user runs this in a real terminal once; the token
        lands in the shared auth volume and every later one-shot run is zero-touch. Emits
        `cli_agent_oauth_manual_cmd` with the command, or `cli_agent_oauth_failed` on a lookup or
        configuration problem.
        """
        harness = data.get(WsEnvelope.HARNESS) or 'antigravity'
        from agent_cascade.engine.cli_agent_oauth import get_oauth_manager
        try:
            command = await asyncio.to_thread(
                get_oauth_manager().build_manual_command, harness, pool=self.agent_pool)
        except Exception as e:  # noqa: BLE001 — a config error must not take down the WS loop
            await self._cli_agent_oauth_fail(harness, str(e))
            return
        await self._emit_cli_agent_oauth({
            WsEnvelope.TYPE: MsgType.CLI_AGENT_OAUTH_MANUAL_CMD, WsEnvelope.HARNESS: harness, WsEnvelope.COMMAND: command,
        })

    async def handle_cli_agent_oauth_recheck(self, data: dict) -> None:
        """Re-check whether the OAuth volume is authenticated after a manual login.

        Invalidates the cached volume probe, re-reads it, and emits a dedicated
        `cli_agent_oauth_status` message ({harness, authenticated}) — NOT folded into the
        pool-settings broadcast. Fired by the "Re-check auth" button and on tab-open.
        """
        harness = data.get(WsEnvelope.HARNESS) or 'antigravity'
        from agent_cascade.log import logger
        from agent_cascade.engine.cli_agent_oauth import get_oauth_manager
        try:
            authenticated = await asyncio.to_thread(
                get_oauth_manager().recheck_auth, harness, pool=self.agent_pool)
        except Exception as e:  # noqa: BLE001 — an indeterminate probe must not crash the loop
            logger.debug('cli_agent_oauth recheck failed for %s: %s', harness, e)
            authenticated = False
        await self._emit_cli_agent_oauth({
            WsEnvelope.TYPE: MsgType.CLI_AGENT_OAUTH_STATUS, WsEnvelope.HARNESS: harness, WsEnvelope.AUTHENTICATED: authenticated,
        })

    async def handle_set_afk(self, data: dict) -> None:
        """Handle 'set_afk' — toggle the server-backed AFK flag.

        Delegates to :meth:`apply_afk` (shared with ``POST /api/afk``) so the WS and REST
        paths cannot drift, then broadcasts updated state.
        """
        WsMessageHandler.apply_afk(
            self.agent_pool, bool(data.get('enabled', False)), data.get(WsEnvelope.MESSAGE),
            data.get('mode'))
        await self._broadcast()

    async def handle_edit_message(self, data: dict) -> None:
        """Handle 'edit_message' — edit a message in conversation history.

        Fix BUG_0007: apply the edit by identity to BOTH the trimmed pool and the FULL on-disk
        history (design §5.2), then rewrite the file from the full list. The old code rewrote
        the whole file from the trimmed pool, destroying pre-marker raw history.
        """
        idx = data.get('index')
        content = data.get('content', '')
        target_name = data.get(WsEnvelope.INSTANCE_NAME) or self.session['session_name']

        # Get the conversation from pool instance
        history = []
        if self.agent_pool:
            inst = self.agent_pool.get_instance(target_name)
            if inst is not None:
                with inst._compression_lock:
                    history = list(inst.conversation)

        from agent_cascade.api_server import _CONTEXT_SUMMARY_RE, COMPRESSION_MARKER, _parse_multimodal_content
        from agent_cascade.llm.schema import CONTENT
        from agent_cascade.utils.utils import msg_field

        if idx is not None and 0 <= idx < len(history):
            msg = history[idx]
            # Identity of the message being edited (timestamp + ORIGINAL content) — used to
            # locate its counterpart in the full on-disk history.
            edit_key = _msg_identity(msg)
            old_content = msg_field(msg, CONTENT, '')
            new_parsed_content = _parse_multimodal_content(content)

            # If this is a compression marker, ensure tags are preserved
            is_compression_msg = str(old_content).startswith(COMPRESSION_MARKER)
            if is_compression_msg:
                if COMPRESSION_MARKER in content or '<context_summary>' in content:
                    new_parsed_content = content
                else:
                    new_parsed_content = f"{COMPRESSION_MARKER}\n\n<context_summary>\n{content}\n</context_summary>"

                match = _CONTEXT_SUMMARY_RE.search(new_parsed_content)
                if match and self.agent_pool:
                    self.agent_pool.instance_summaries[target_name] = match.group(1).strip()

            # Apply edit to the pool message — handle both dict and Message object types
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
                        target_name, 'Orchestrator' if target_name == self.session['session_name'] else 'SubAgent')

                    # Rewrite the FULL on-disk history with the edit applied by identity, so
                    # pre-marker raw messages are preserved (design §5.2). Shrink guard stays
                    # armed — an edit must never drop messages.
                    full_history = logger_inst.get_full_history()
                    edited_full = []
                    applied = False
                    for fmsg in full_history:
                        if not applied and _msg_identity(fmsg) == edit_key:
                            if isinstance(fmsg, dict):
                                fmsg[CONTENT] = new_parsed_content
                                fmsg.pop('_ui_cache', None)
                            elif hasattr(fmsg, 'content'):
                                fmsg.content = new_parsed_content
                            applied = True
                        edited_full.append(fmsg)

                    # Fix BUG_0007 (#1): if the file is non-empty but the target identity was
                    # NOT found (pool/file content drift), we must NOT write `edited_full` — it
                    # would silently persist a full-history file LACKING an edit the UI already
                    # shows as applied. Safer choice for data integrity: fall back to writing
                    # `history` (the pool, which HAS the edit at lines above). That persists the
                    # user's edit to the working set and keeps the on-disk file consistent with
                    # what the UI claims; aborting instead would lose the edit entirely.
                    if full_history and not applied:
                        from agent_cascade.log import logger as _edit_logger
                        _edit_logger.error(f"[ws_edit] Edit target not found in full on-disk history for "
                                           f"'{target_name}' (identity drift). Falling back to pool working set "
                                           f"so the edit is persisted rather than silently lost.")
                        logger_inst.rewrite_log_with_history(history, caller='ws_edit')
                    else:
                        # File empty/unreadable → fall back to the (already-edited) pool;
                        # otherwise write the full history with the edit applied in place.
                        logger_inst.rewrite_log_with_history(edited_full if full_history else history, caller='ws_edit')

                # Sync instance_state so build_state() sees the edit
                    self.agent_pool.instance_state[target_name]['messages'] = list(history)

        _clear_caches_safely()
        await self._broadcast()

    async def handle_delete_messages(self, data: dict) -> None:
        """Handle 'delete_messages' — prune messages from conversation.

        Fix BUG_0007: operate on the FULL on-disk history (design §5.2), not the trimmed
        pool working set. The UI's `indices` refer to the DISPLAYED (trimmed) list, so each
        is resolved to its file message by identity (timestamp + content), then removed from
        the full list. The file is rewritten from that full list with the shrink guard ARMED —
        a normal delete of 1–few messages must never be allowed to drop thousands of
        pre-marker raw messages.
        """
        target_name = data.get(WsEnvelope.INSTANCE_NAME) or self.session['session_name']

        history = []
        if self.agent_pool:
            inst = self.agent_pool.get_instance(target_name)
            if inst is not None:
                with inst._compression_lock:
                    history = list(inst.conversation)

        indices = data.get('indices', [])

        # Resolve displayed indices → identity keys. The file may hold many more messages than
        # the trimmed pool, so positional mapping would be wrong — match by identity instead.
        target_keys = set()
        for idx in indices:
            if isinstance(idx, int) and 0 <= idx < len(history):
                target_keys.add(_msg_identity(history[idx]))

        logger_inst = None
        if self.agent_pool:
            logger_inst = self.agent_pool.get_logger(
                target_name, 'Orchestrator' if target_name == self.session['session_name'] else 'SubAgent')

        full_history = logger_inst.get_full_history() if logger_inst is not None else []

        # If the file couldn't be read (fresh/empty session), fall back to the pool view so a
        # delete on an uncompressed session still works. In that case pool == file, no loss.
        base = full_history if full_history else [dict(m) for m in history]

        remaining_keys = set(target_keys)
        new_full = []
        for msg in base:
            key = _msg_identity(msg)
            if key in remaining_keys:
                remaining_keys.discard(key)  # remove one file message per displayed deletion
                continue
            new_full.append(msg)

        # Fix BUG_0007 (#4): a requested identity that was never matched means the pool and
        # on-disk file have drifted (e.g. content edited elsewhere). Behavior is unchanged, but
        # surface the desync so it's visible instead of silently dropped.
        if remaining_keys:
            from agent_cascade.log import logger as _del_logger
            _del_logger.warning(f"[ws_delete] {len(remaining_keys)} requested deletion(s) for '{target_name}' "
                                f"were not found in the on-disk history (pool/file identity drift).")

        # Keep the trimmed pool working set in sync: drop the same message(s) by identity.
        new_pool = [m for m in history if _msg_identity(m) not in target_keys]

        if self.agent_pool:
            inst = self.agent_pool.get_instance(target_name)
            if inst is not None:
                inst.rebuild_conversation(new_pool)

                # Shrink guard stays ARMED (no allow_shrink): a single-message delete must not be
                # able to collapse a full-history file. If the rewrite would drop far more than
                # requested, rewrite_log_with_history logs CRITICAL and aborts, leaving the file
                # unchanged — fail safe rather than silently destroy history.
                logger_inst.rewrite_log_with_history(new_full, caller='ws_delete')

                if target_name != self.session['session_name'] and target_name in self.agent_pool.instance_state:
                    self.agent_pool.instance_state[target_name]['messages'] = list(new_pool)

        _clear_caches_safely()
        await self._broadcast()

    async def handle_select_agent(self, data: dict) -> None:
        """Handle 'select_agent' — update the active agent index."""
        self.session['agent_index'] = int(data.get('index', 0))
        await self._broadcast()

    async def handle_set_session_name(self, data: dict) -> None:
        """Handle 'set_session_name' — rename session and migrate summaries."""
        old_name = self.session['session_name']
        new_name = data.get('name', DEFAULT_SESSION_NAME).strip()
        # Persist the server-wide active session name so a restart resumes it.
        from agent_cascade.session_state_store import save_session_name
        if settings.ENABLE_STACK_SWAP:
            # Rename is display-only: the root keeps its persisted birth-stamped
            # identity so logs/KV labels are never orphaned. session_state.json
            # stores the display name only.
            inst = self.agent_pool.get_instance(old_name) if self.agent_pool else None
            if inst is not None and getattr(inst, 'parent_instance', None) is None:
                inst.display_name = new_name
            elif self.agent_pool and old_name not in self.agent_pool.instances:
                self.session['session_name'] = new_name
            save_session_name(new_name)
            await self._broadcast()
            return

        self.session['session_name'] = new_name
        save_session_name(new_name)
        if self.agent_pool:
            if old_name in self.agent_pool.instance_summaries:
                self.agent_pool.instance_summaries[new_name] = self.agent_pool.instance_summaries.pop(old_name)
        await self._broadcast()

    async def handle_load_session(self, data: dict) -> None:
        """Handle 'load_session' — load conversation from a log file."""
        if self._is_generating():
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.WARNING,
                WsEnvelope.MESSAGE: 'Cannot load session while generation is in progress',
            })
            return

        path = data.get('path')
        if not path or not self.agent_pool:
            return

        mode = data.get('mode', 'replace')
        if not settings.ENABLE_STACK_SWAP and mode == 'new_stack':
            mode = 'replace'

        # Extract instance_name from log file metadata BEFORE loading.
        # This avoids fragile regex parsing of status strings and lets the
        # loaded session's own metadata determine its identity.
        instance_name = DEFAULT_SESSION_NAME
        try:
            # encoding='utf-8': log files are written as UTF-8 JSONL; the platform
            # default (cp1252 on Windows) crashes on any non-ASCII byte.
            # errors='replace': a partially-written line must not abort session load.
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                first_line = f.readline().strip()
                if first_line:
                    first_data = json.loads(first_line)
                    if isinstance(first_data, dict):
                        meta = first_data.get('metadata', {})
                        extracted = meta.get('instance_name') if isinstance(meta, dict) else None
                        if extracted:
                            instance_name = str(extracted).strip()
        except (OSError, json.JSONDecodeError, KeyError, UnicodeDecodeError) as e:
            from agent_cascade.log import logger
            logger.debug(f"Failed to read metadata from {path}: {e}, using default instance name")

        from agent_cascade.agent_identity import make_birth_stamped_name, strip_birth_stamp

        if mode == 'new_stack':
            from agent_cascade import snapshot
            if self.agent_pool.instances:
                await asyncio.to_thread(snapshot.park_active_stack, self.agent_pool, trigger='import')

        target_inst = instance_name
        clear_sub = True
        caller_name = None

        if mode == 'add':
            clear_sub = False
            caller_name = self.session.get('session_name')
            display = strip_birth_stamp(instance_name) if settings.ENABLE_STACK_SWAP else instance_name
            if settings.ENABLE_STACK_SWAP:
                target_inst = make_birth_stamped_name(display)
            else:
                idx = 2
                target_inst = f"{display}_{idx}"
                while target_inst in self.agent_pool.instances:
                    idx += 1
                    target_inst = f"{display}_{idx}"
        else:
            caller_name = self.session.get('session_name')
            root_count = sum(1 for inst in self.agent_pool.instances.values() if inst.parent_instance is None)
            exclude_caller = (root_count > 1 and caller_name is not None and caller_name != instance_name and
                              caller_name in self.agent_pool.instances)
            if not exclude_caller:
                caller_name = None

        with self._session_lock:
            status = self.agent_pool.load_session_from_log(
                path,
                target_instance=target_inst,
                clear_sub_agents_before_load=clear_sub,
                caller_name=caller_name,
            )
        if status.startswith('Error'):
            await self.broadcast_fn({WsEnvelope.TYPE: MsgType.ERROR, WsEnvelope.MESSAGE: status})
            return

        loaded_root = target_inst
        roots = [inst_name for inst_name, inst in self.agent_pool.instances.items()
                 if getattr(inst, 'parent_instance', None) is None]
        if target_inst in roots:
            loaded_root = target_inst
        elif roots:
            loaded_root = roots[0]

        if mode != 'add':
            self.session['session_name'] = loaded_root
            if settings.ENABLE_STACK_SWAP:
                from agent_cascade.session_state_store import save_session_name
                from agent_cascade import snapshot
                save_session_name(strip_birth_stamp(loaded_root))
                snapshot.register_active_stack(self.agent_pool, loaded_root)

        inst = self.agent_pool.get_instance(loaded_root)
        if inst is None and mode != 'add':
            await self.broadcast_fn({
                WsEnvelope.TYPE: MsgType.WARNING,
                WsEnvelope.MESSAGE: f"Instance '{loaded_root}' not found in pool after load — session may be empty."
            })
            return

        self._stop_generation()
        self._signal_stop()
        if self.agent_pool:
            self.agent_pool.stopped = False
        _clear_caches_safely()
        await self._broadcast()
        if mode == 'new_stack':
            await self._broadcast_stacks()

    async def handle_inject(self, data: dict) -> None:
        """Handle 'inject' — enqueue a message into an agent's queue."""
        text = data.get('text', '').strip()
        target = data.get('target_agent') or self.session.get('session_name', 'Maine')
        if text and self.agent_pool:
            # Route through the shared user-message parse path so /goal rewriting
            # (todo.md:160) and image handling apply here too — this endpoint was a
            # bypass of _parse_multimodal_content.
            from agent_cascade.api_server import _parse_multimodal_content
            self.agent_pool.enqueue_message(target, _parse_multimodal_content(text))

    async def handle_dismiss_queue(self, data: dict) -> None:
        """Handle 'dismiss_queue' — remove a queued message by index.

        Client sends: {"type": "dismiss_queue", "instance_name": "...", "message_index": N}
        Use message_index=-1 to clear all queued messages for the instance.
        """
        if not self.agent_pool:
            return

        from agent_cascade.log import logger

        instance_name = data.get(WsEnvelope.INSTANCE_NAME) or self.session.get('session_name', 'Maine')
        try:
            message_index = int(data.get(WsEnvelope.MESSAGE_INDEX, 0))
        except (ValueError, TypeError):
            logger.debug(f"Invalid message_index in dismiss_queue: {data.get(WsEnvelope.MESSAGE_INDEX)}")
            return

        removed = self.agent_pool.dismiss_queue_message(instance_name, message_index)
        if removed:
            await self._broadcast()

    # ── Internal helpers (none needed — SecurityAdvisorHandler handles everything) ──
