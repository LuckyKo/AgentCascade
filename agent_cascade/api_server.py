"""
API Server for AgentCascade Multi-Agent Framework

WebSocket + REST API that replaces the Gradio WebUI.
Any frontend (HTML/JS, Electron, etc.) can connect to control the agents.

WebSocket protocol (all JSON):
  Client → Server:
    {"type": "message", "text": "...", "agent_index": 0, "session_name": "..."}
    {"type": "stop"}
    {"type": "retry"}
    {"type": "reset"}
    {"type": "approve", "request_id": "..."}
    {"type": "reject", "request_id": "...", "reason": "..."}
    {"type": "edit_message", "index": N, "content": "new text"}
    {"type": "delete_messages", "indices": [N, M, ...]}
    {"type": "select_agent", "index": N}
    {"type": "set_session_name", "name": "..."}
    {"type": "inject", "text": "..."}

  Server → Client:
    {"type": "state",  ...full state snapshot...}
    {"type": "done",   ...final state snapshot...}
    {"type": "error",  "message": "..."}
    {"type": "approvals", "approvals": [...]}
"""

import argparse
import asyncio
import copy
import glob
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from agent_cascade.llm.schema import (
    ASSISTANT, CONTENT, FUNCTION, NAME, REASONING_CONTENT,
    ROLE, SYSTEM, USER, Message,
)
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from agent_cascade.log import logger
from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
from agent_cascade.utils.utils import extract_text_from_message, get_message_stats, get_history_stats, IMAGE_REGEX
from agent_cascade.prompts.dna import SECURITY_ADVISOR_PROMPT, COMPRESSION_MARKER
from agent_cascade.llm.base import _truncate_input_messages_roughly
from agent_cascade.execution_engine import ExecutionEngine

# Timeout constants for security advisor checks
from agent_cascade.operation_manager import SECURITY_ADVISOR_TIMEOUT_SECONDS, SECURITY_ADVISOR_WARNING_SECONDS

from agent_cascade.utils.thinking_block import (
    _THINK_BLOCK_RE, _THINK_BLOCK_UNCLOSED_RE, _THINK_BLOCK_BRACKET_RE,
    _THINK_SEARCH_RE, _TOOL_TRUNCATED_RE, _CONTEXT_SUMMARY_RE,
    _MARKDOWN_BOLD_RE, _JUSTIFICATION_PREFIX_RE, _IMAGE_DATA_RE as _IMAGE_DATA_PATTERN,
    strip_thinking_blocks
)

try:
    from agent_cascade.agents.user_agent import PENDING_USER_INPUT
except ImportError:
    PENDING_USER_INPUT = 'PENDING_USER_INPUT'

# Agent state management imports for Stop handler
from agent_cascade.agent_pool import ACTIVE_STATES
from agent_cascade.agent_instance import AgentState, InvalidStateTransition

# Pre-compiled regexes moved to agent_cascade.utils.thinking_block

# Module-level lock used by helper functions before create_app() runs.
# Overwritten inside create_app() for the per-app instance, but safe as a fallback.
session_lock = threading.Lock()

# LLM config keys for update_config optimization (defense-in-depth)
LLM_CONFIG_KEYS = frozenset({
    'model', 'api_base', 'api_key', 'temperature', 'max_tokens',
    'max_input_tokens', 'max_output_tokens', 'top_p', 'frequency_penalty',
    'presence_penalty', 'stop', 'timeout', 'model_type'
})


def _get_msg_role(msg):
    """Extract the 'role' field from a message, handling both dict and Message object types."""
    if isinstance(msg, dict):
        return msg.get(ROLE)
    return getattr(msg, 'role', None)


def _parse_multimodal_content(text):
    """
    Parse markdown images ![alt](data:...) and return a list of content items.
    If no images are found, returns the original text.
    """
    parts = []
    last_end = 0
    for match in _IMAGE_DATA_PATTERN.finditer(text):
        start, end = match.span()
        if start > last_end:
            parts.append({'text': text[last_end:start]})
        alt, url = match.groups()
        parts.append({'image': url})
        last_end = end
    
    if last_end < len(text):
        parts.append({'text': text[last_end:]})
    
    if not parts:
        return text
    if len(parts) == 1 and 'text' in parts[0]:
        return parts[0]['text']
    return parts
    










def _validate_disabled_tools(ui_cfg: Dict[str, Any]) -> None:
    """Validate disabled_tools in a generate_cfg dict against the tool registry.

    Extracted to eliminate duplicate validation blocks in both 'message' and
    'continue' WebSocket handlers (Fix #5 from second review).

    Args:
        ui_cfg: The ``generate_cfg`` dictionary from the client message.
    """
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


# serialize_message imported from agent_cascade.api_integration (unified version)

# ── Logging Setup ─────────────────────────────────────────────────────────────

# ─── App factory ──────────────────────────────────────────────────────────────

def _is_generating(session: dict) -> bool:
    """Check if currently generating (thread-safe)."""
    with session_lock:
        return session.get('generating', False)


def _start_generation(session: dict) -> int:
    """Atomically start generation and return the new generation_id."""
    with session_lock:
        session['stop_requested'] = False
        session['generation_id'] += 1
        session['generating'] = True
        return session['generation_id']


def _stop_generation(session: dict) -> None:
    """Atomically stop generation."""
    with session_lock:
        session['generating'] = False


def _signal_stop(session: dict) -> None:
    """Signal that a stop is requested."""
    with session_lock:
        session['stop_requested'] = True


def _set_generating_true(session: dict) -> None:
    """Atomically mark session as generating."""
    with session_lock:
        session['generating'] = True


def create_app(agents, agent_pool, config=None):
    """
    Create the FastAPI application.

    Args:
        agents:     List of Agent objects (orchestrator first, then agent instances)
        agent_pool: The AgentPool instance
        config:     Optional chatbot config dict
    """
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, UploadFile, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware

    config = config or {}
    app = FastAPI(title="AgentCascade API")

    # Initialize concurrency control for Security advisor checks
    # Security runs on a separate daemon thread (line 2245), so we need a semaphore
    # to limit concurrent Security agent invocations to 1, preventing unlimited parallelism
    if not hasattr(app, 'security_check_semaphore'):
        app.security_check_semaphore = threading.Semaphore(1)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Unified architecture imports (Phase 5) ───────────────────────────
    from agent_cascade.run_agent_unified import run_agent_thread_unified
    from agent_cascade.api_integration import (
        broadcast_stream_update,
        build_state_from_pool,
        build_stream_update_from_pool,
        create_main_agent_instance,
        serialize_message,
        _apply_ui_config,
        _build_agents_list,
        _get_approvals,
        _resolve_max_tokens,
        _find_user_message_insertion_point,
        _put_stream_update,
    )

    # ── Helpers ───────────────────────────────────────────────────────────
    def _save_session_history():
        try:
            if not agent_pool:
                return
                
            name = session.get('session_name', 'Maine')
            # Phase 6: Read history directly from pool instance instead of session['history']
            inst = agent_pool.get_instance(name)
            if inst is not None:
                with inst._compression_lock:
                    history = list(inst.conversation)
            else:
                logger.warning(f"Session instance '{name}' not found in pool during save — skipping history sync")
                history = []
            
            # Use the standardized logger to ensure append-only behavior
            logger_inst = agent_pool.get_logger(name, 'Orchestrator')
            logger_inst.update_history(history)
            logger_inst._file_history_synced = True
            
            # Also sync to instance_summaries for the UI if history was compressed
            for msg in reversed(history):
                content = msg.get(CONTENT, '')
                if isinstance(content, str) and "<context_summary>" in content:
                    import re
                    match = _CONTEXT_SUMMARY_RE.search(content)
                    if match:
                        agent_pool.instance_summaries[name] = match.group(1).strip()
                    break
        except Exception as e:
            logger.error(f"Failed to save session history: {e}")

    def _load_session_history(name):
        """Load session history into the pool from log files.
        
        Returns True on success, False on failure. The pool already has the data
        after load_session_from_log(), so returning the tuples is redundant.
        """
        try:
            if not agent_pool:
                return False
                
            if hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                log_dir = agent_pool.operation_manager.base_dir / 'logs'
            else:
                log_dir = Path(DEFAULT_WORKSPACE) / 'logs'
            
            # Orchestrator logs might be named session_NAME.jsonl or follow the agent instance pattern
            path = log_dir / f"session_{name}.jsonl"
            if not path.exists():
                # Try finding a log matching any agent_class prefix (handles lowercase naming like orchestrator_Maine_*.jsonl)
                potential = list(log_dir.glob(f"*_{name}_*.jsonl"))
                if potential:
                    potential.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                    path = potential[0]

            if not path.exists():
                return False

            # Use the AgentPool's standardized loading logic to handle slicing
            # and system message preservation
            status = agent_pool.load_session_from_log(str(path), target_instance=name)
            if status.startswith("Error"):
                logger.error(f"Failed to load session {name} via pool: {status}")
                return False
            
            return True
        except Exception as e:
            logger.error(f"Failed to load session history: {e}")
        return False

    def get_session_history(session, instance_name=None):
        """Read session history from the pool (unified single source of truth).

        Phase 2: Always reads from agent_pool.get_instance().conversation.
        Falls back to instance_state only for very old sessions that might not
        have instances registered in the pool yet (should be rare post-unification).

        Args:
            session: Flask session dict (used to resolve default instance_name)
            instance_name: Agent instance name. If None, defaults to
                session['session_name'] (the root/main chat instance).

        Returns:
            list of message objects
        """
        # Resolve default: when no instance_name is given, use the session's
        # session_name so callers don't need to know about "root".
        if instance_name is None:
            instance_name = session.get('session_name') or 'Maine'

        # Primary path: read from agent_pool.instances (the single source of truth)
        inst = agent_pool.get_instance(instance_name) if agent_pool else None
        if inst and hasattr(inst, 'conversation'):
            return list(inst.conversation)

        # Fallback for very old sessions that might not have pool instances yet
        state = agent_pool.instance_state.get(instance_name, {}) if agent_pool else {}
        return state.get('messages', [])

    def get_agent_state(instance_name):
        """Get state for any agent instance, including root.
        
        All agents including root come from agent_pool.instance_state.
        
        Args:
            instance_name: Agent instance name ('root' for main chat, or agent name)
        
        Returns:
            dict with 'messages', 'active', etc. or None if not found
        """
        if not agent_pool:
            return None
        state = agent_pool.instance_state.get(instance_name)
        return state.copy() if state else None

    # ── Shared session state ──────────────────────────────────────────────
    default_session_name = config.get('session_name', 'Maine')
    session: Dict[str, Any] = {
        'agent_index': 0,
        'session_name': default_session_name,
        'generating': False,
        'stop_requested': False,
        'generation_id': 0,         # Increment on each run to prevent stale appends
    }
    # Initial load — pool is the single source of truth for conversation state (Phase 6)
    if agent_pool:
        loaded = _load_session_history(default_session_name)
        # If log loading failed, create an empty instance so build_state_from_pool() doesn't return None.
        # This mirrors how the original branch always had session['history'] = [] even on load failure.
        if not loaded and agent_pool.get_instance(default_session_name) is None:
            try:
                # Extract system message from the orchestrator agent (first in agents list)
                sys_content = None
                if agents:
                    orch = agents[0]
                    if hasattr(orch, 'base_system_message') and orch.base_system_message:
                        sys_content = str(orch.base_system_message)
                    elif hasattr(orch, 'system_message') and orch.system_message:
                        sys_content = str(orch.system_message)
                if sys_content:
                    create_main_agent_instance(
                        pool=agent_pool,
                        instance_name=default_session_name,
                        system_message_content=sys_content,
                    )
            except Exception as e:
                logger.error(f"Failed to create fallback main agent instance: {e}")
                logger.warning("Server starting without main agent instance — first user message may fail")

    # ── Unified token cache (coexists with old _cached_hist_stats during transition) ──
    from config.token_cache import AgentTokenCache
    unified_token_cache = AgentTokenCache(ttl=300)

    # ── E2E Encryption State ─────────────────────────────────────────────
    server_private_key = x25519.X25519PrivateKey.generate()
    server_public_key = server_private_key.public_key()
    server_public_bytes = server_public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    
    # Session storage for external API clients
    # key: session_token (string), value: shared_secret (bytes)
    api_sessions: Dict[str, bytes] = {}

    ws_connections: Set[WebSocket] = set()
    # Bounded queue to prevent stale event buildup during sustained streaming.
    # When full, the oldest pending stream_update is dropped (put_nowait raises QueueFull).
    # Non-stream events (state, done, dismissal) still get priority via regular put().
    # Increased from 32 to 128 to reduce dropped updates during heavy multi-agent activity.
    send_queue: asyncio.Queue = asyncio.Queue(maxsize=128)

    # Lock for session state accessed across threads (asyncio loop + agent thread).
    # Protects: session['generating'], session['stop_requested'].
    # Phase 6: session['history'] removed — pool is the single source of truth.
    session_lock = threading.Lock()



    def get_agent():
        idx = session['agent_index']
        if 0 <= idx < len(agents):
            return agents[idx]
        return agents[0]

    def get_active_stack():
        if agent_pool and hasattr(agent_pool, 'active_stack'):
            return agent_pool.active_stack
        return []

    def get_approvals():
        if agent_pool and hasattr(agent_pool, 'operation_manager'):
            return agent_pool.operation_manager.list_pending_approvals()
        return []

    def _safe_get_telemetry():
        """Get telemetry summary safely — never crash state serialization."""
        try:
            if agent_pool and hasattr(agent_pool, 'telemetry') and agent_pool.telemetry:
                return agent_pool.telemetry.get_session_summary()
        except Exception as e:
            logger.debug(f"Telemetry fetch failed (non-critical): {e}")
        return None

    def build_state(responses=None, generating=None):
        """Build a full state snapshot for the frontend.
        
        Delegates to build_state_from_pool which reads from pool.instances
        instead of session['history']. This is the unified single-source path.
        """
        instance_name = session['session_name']
        # FIX B: Protect session['generating'] read with lock (consistent with Fix 1/4)
        if generating is None:
            gen = _is_generating(session)
        else:
            gen = generating
        
        result = build_state_from_pool(
            pool=agent_pool,
            instance_name=instance_name,
            responses=responses,
            generating=gen,
        )
        
        # If unified build returned None (instance not yet created), return minimal state
        if result is None:
            agents_list = _build_agents_list(agent_pool) if agent_pool else []
            approvals = _get_approvals(agent_pool) if agent_pool else []
            stopped = agent_pool.stopped if agent_pool else False
            has_queued = agent_pool.has_messages(instance_name) if agent_pool else False
            
            # Get current model from the orchestrator agent (first in agents list)
            current_model = 'Unknown'
            if agents:
                orch = agents[0]
                if hasattr(orch, 'llm') and orch.llm:
                    current_model = getattr(orch.llm, 'model', 'Unknown')
            
            # Resolve max_tokens via shared helper (same logic as _get_max_tokens_for_instance)
            orch_inst = agent_pool.get_instance(instance_name) if agent_pool else None
            fallback_max_tokens = _resolve_max_tokens(agent_pool, orch_inst)
            
            # Get API router state
            api_router_state = {'endpoints': [], 'agent_priorities': {}}
            if agent_pool and hasattr(agent_pool, 'api_router') and agent_pool.api_router:
                try:
                    api_router_state = agent_pool.api_router.to_dict()
                except Exception as e:
                    logger.debug(f"API router state serialization failed (using empty): {e}")
            
            # Get default workspace
            from agent_cascade.settings import DEFAULT_WORKSPACE
            default_workspace = str(DEFAULT_WORKSPACE)
            if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                default_workspace = str(agent_pool.operation_manager.base_dir)
            
            return {
                'messages': [],
                'instances': {},
                'agent_instances': {},
                'active_stack': [],
                'approvals': approvals,
                'generating': gen,
                'session_name': instance_name,
                'instance_name': instance_name,
                'agent_index': session.get('agent_index', 0),
                'total_tokens': 0,
                'total_words': 0,
                'max_tokens': fallback_max_tokens,
                'summary': '',
                'has_queued_messages': has_queued,
                'stopped': stopped,
                'agents': agents_list,
                'current_model': current_model,
                'telemetry': None,
                'default_workspace': default_workspace,
                'is_waiting': False,
                'api_router': api_router_state,
            }
        
        return result

    def build_stream_update(responses, cached_h_stats=None, agent_instances=None, telemetry=None):
        """Build a lightweight streaming delta (skips re-serializing stable history).
        
        Delegates to build_stream_update_from_pool which reads from pool.instances.
        Legacy parameters are accepted but ignored — callers should use the unified path directly.
        """
        instance_name = session['session_name']
        
        result = build_stream_update_from_pool(
            pool=agent_pool,
            instance_name=instance_name,
            responses=responses,
        )
        
        # Fallback to minimal state if unified build failed
        if result is None:
            approvals = _get_approvals(agent_pool) if agent_pool else []
            stopped = agent_pool.stopped if agent_pool else False

            # Get current model
            current_model = 'Unknown'
            if agents:
                orch = agents[0]
                if hasattr(orch, 'llm') and orch.llm:
                    current_model = getattr(orch.llm, 'model', 'Unknown')

            # Resolve max_tokens via shared helper (same logic as _get_max_tokens_for_instance)
            orch_inst = agent_pool.get_instance(instance_name) if agent_pool else None
            stream_max_tokens = _resolve_max_tokens(agent_pool, orch_inst)

            # Build minimal root instance state — frontend reads from agent_instances now
            serialized_msgs = [serialize_message(m, i) for i, m in enumerate(responses or [])]
            root_state = {
                'instance_name': instance_name,
                'agent_class': 'orchestrator',
                'active': True,
                'is_halted': False,
                'parent_instance': None,
                'has_queued_messages': False,
                'messages': serialized_msgs,
                'history_count': len(responses or []),
                'is_partial': True,
                'total_tokens': 0,
                'total_words': 0,
                'max_tokens': stream_max_tokens,
            }

            return {
                'instances': {instance_name: root_state},
                'agent_instances': {instance_name: root_state},
                'active_stack': [],
                'approvals': approvals,
                'generating': True,
                'total_tokens': 0,
                'total_words': 0,
                'max_tokens': stream_max_tokens,
                'current_model': current_model,
                'telemetry': None,
                'stopped': stopped,
            }
        
        return result

    async def broadcast(data):
        """Send JSON to all connected WebSocket clients.
        
        Uses a snapshot (frozenset) of ws_connections to avoid RuntimeError
        from set-size-changed-during-iteration when a client disconnects
        mid-broadcast.
        """
        nonlocal ws_connections
        text = json.dumps(data, ensure_ascii=False, default=str)
        # Snapshot the set so concurrent add/discard from other coroutines
        # (e.g. a new client connecting) won't raise RuntimeError.
        snapshot = frozenset(ws_connections)
        for conn in snapshot:
            try:
                await conn.send_text(text)
            except Exception as e:
                logger.debug(f"WebSocket send failed, removing connection: {e}")
                ws_connections.discard(conn)

    # ── Agent execution thread ────────────────────────────────────────────

    def run_agent_thread(history_for_agent, agent_runner, gen_id, loop, target_instance_name=None):
        """
        Unified agent execution entry point. Delegates to run_agent_thread_unified
        which uses ExecutionEngine and pool.instances instead of session['history'].
        
        Signature:
          - history_for_agent → used to extract system message content (can be None if instance exists in pool)
          - agent_runner → not used (engine is stateless, reads from pool)
          - gen_id → tracked in session for stop detection
          - loop → asyncio event loop for send_queue
          - target_instance_name → override the target instance (for sub-agent routing, bug #42 fix)
        """
        with session_lock:
            session['generation_id'] = gen_id

        # Extract system message content for instance creation.
        # Priority: history_for_agent arg > existing pool instance > None
        system_message_content = None
        if history_for_agent and len(history_for_agent) > 0:
            first_msg = history_for_agent[0]
            if isinstance(first_msg, dict):
                if first_msg.get(ROLE) == SYSTEM:
                    system_message_content = str(first_msg.get(CONTENT, '') or '')
            elif hasattr(first_msg, 'role'):
                if getattr(first_msg, 'role', None) == SYSTEM:
                    system_message_content = str(getattr(first_msg, 'content', '') or '')

        # If no history provided but instance exists in pool, extract system message from it
        if system_message_content is None and agent_pool:
            target_inst_name = target_instance_name or session['session_name']
            inst = agent_pool.get_instance(target_inst_name)
            if inst:
                with inst._compression_lock:
                    if inst.conversation:
                        first_msg = inst.conversation[0]
                    else:
                        first_msg = None
                    if first_msg is not None and isinstance(first_msg, dict):
                        if first_msg.get(ROLE) == SYSTEM:
                            system_message_content = str(first_msg.get(CONTENT, '') or '')
                    elif first_msg is not None and hasattr(first_msg, 'role'):
                        if getattr(first_msg, 'role', None) == SYSTEM:
                            system_message_content = str(getattr(first_msg, 'content', '') or '')
        instance_name = target_instance_name or session['session_name']
        ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))

        try:
            # Delegate to the unified execution thread (handles everything internally)
            run_agent_thread_unified(
                pool=agent_pool,
                instance_name=instance_name,
                system_message_content=system_message_content,
                ui_cfg=ui_cfg,
                send_queue=send_queue,
                loop=loop,
            )
        finally:
            # FIX 2 (Cleanup): Reset session['generating'] when thread completes
            # This ensures the flag is cleared automatically after execution finishes
            _stop_generation(session)


    # ── Background tasks ──────────────────────────────────────────────────

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(_sender_loop())
        asyncio.create_task(_approval_loop())
        
        # Register dismissal callback for real-time UI tab removal when LLM calls dismiss_agent
        if agent_pool and hasattr(agent_pool, 'on_dismissed'):
            def _on_dismiss_callback(instance_name, log_path):
                """Fire a state broadcast when an agent is dismissed (runs from tool thread).
                
                Uses agent_pool._ws_loop to access the event loop set by run_agent_thread.
                Handles all dismissals: both LLM-initiated (during active generation cycle)
                and UI-initiated (via terminate_agent_instance handler).
                """
                ws_loop = getattr(agent_pool, '_ws_loop', None)
                if ws_loop and not ws_loop.is_closed() and send_queue:
                    try:
                        msg = {'type': 'dismissal', 'instance_name': instance_name}
                        asyncio.run_coroutine_threadsafe(send_queue.put(msg), ws_loop)
                    except Exception as e:
                        logger.debug(f"Dismissal callback failed (non-critical): {e}")
                else:
                    logger.warning(f"Dismissal broadcast skipped for {instance_name}: ws_loop={ws_loop}, closed={ws_loop.is_closed() if ws_loop else 'N/A'}, queue={'yes' if send_queue else 'no'}")
            
            agent_pool.on_dismissed(_on_dismiss_callback)

    async def _sender_loop():
        """Global loop: reads from send_queue → broadcasts to all clients."""
        while True:
            try:
                data = await send_queue.get()
                # Handle dismissal signal: build full state and broadcast (real-time tab removal)
                if data.get('type') == 'dismissal':
                    try:
                        await broadcast({'type': 'state', **build_state()})
                    except Exception as e:
                        logger.error(f"Failed to build state for dismissal broadcast: {e}")
                else:
                    await broadcast(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Sender loop iteration failed (continuing): {e}")

    async def _approval_loop():
        """Poll for pending approvals and push to clients."""
        known_ids: Set[str] = set()
        while True:
            try:
                await asyncio.sleep(0.3)
                pending = get_approvals()
                current_ids = {a['request_id'] for a in pending}
                if current_ids != known_ids:
                    known_ids = current_ids.copy()
                    await broadcast({'type': 'approvals', 'approvals': pending})
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Approval loop iteration failed (continuing): {e}")

    # ── E2E Encrypted REST API ────────────────────────────────────────────

    @app.get("/api/keys")
    async def api_get_keys():
        """Returns the server's X25519 public key (Base64)."""
        return {
            "public_key": base64.b64encode(server_public_bytes).decode('utf-8'),
            "algorithm": "X25519"
        }

    @app.post("/api/handshake")
    async def api_handshake(data: dict):
        """
        Performs X25519 handshake.
        Client sends its public_key, server returns a session_token.
        """
        client_pub_b64 = data.get("public_key")
        if not client_pub_b64:
            return JSONResponse(status_code=400, content={"message": "Missing public_key"})
            
        try:
            client_pub_bytes = base64.b64decode(client_pub_b64)
            client_public_key = x25519.X25519PublicKey.from_public_bytes(client_pub_bytes)
            
            # Derive shared secret
            shared_secret = server_private_key.exchange(client_public_key)
            
            # Generate a session token
            import secrets
            token = secrets.token_hex(16)
            api_sessions[token] = shared_secret
            
            return {"session_token": token}
        except Exception as e:
            return JSONResponse(status_code=400, content={"message": f"Handshake failed: {str(e)}"})

    @app.post("/api/message")
    async def api_inject_message(data: dict):
        """
        Inject an E2E encrypted message into an agent's queue.
        Payload must be AES-GCM encrypted using the shared secret.
        """
        token = data.get("session_token")
        encrypted_b64 = data.get("payload")
        nonce_b64 = data.get("nonce")
        
        if not all([token, encrypted_b64, nonce_b64]):
            return JSONResponse(status_code=400, content={"message": "Missing token, payload, or nonce"})
            
        shared_secret = api_sessions.get(token)
        if not shared_secret:
            return JSONResponse(status_code=401, content={"message": "Invalid or expired session token"})
            
        try:
            # Decrypt payload
            nonce = base64.b64decode(nonce_b64)
            ciphertext = base64.b64decode(encrypted_b64)
            
            aesgcm = AESGCM(shared_secret)
            decrypted_bytes = aesgcm.decrypt(nonce, ciphertext, None)
            payload = json.loads(decrypted_bytes.decode('utf-8'))
            
            target = payload.get("target") or session.get('session_name', 'Maine')
            text = payload.get("text", "").strip()
            
            if not text:
                return JSONResponse(status_code=400, content={"message": "Empty message text"})
                
            if agent_pool:
                agent_pool.enqueue_message(target, text)
                logger.info(f"REST API: Injected message into {target}: {text[:50]}...")
                return {"status": "success", "queued": True, "target": target}
            else:
                return JSONResponse(status_code=503, content={"message": "Agent pool not initialized"})
                
        except Exception as e:
            return JSONResponse(status_code=400, content={"message": f"Decryption failed: {str(e)}"})

    @app.get("/api/status")
    async def api_get_status(token: str = None):
        """Returns the current state of the agents."""
        if not token or token not in api_sessions:
             return JSONResponse(status_code=401, content={"message": "Invalid session token"})
        
        # FIX 1: Protect session reads with session_lock to prevent race condition
        with session_lock:
            gen = session['generating']
            sess_name = session['session_name']
            
        return {
            "generating": gen,
            "active_agent": sess_name,
            "agents": agent_pool.list_agents() if agent_pool else [],
            "active_stack": get_active_stack(),
            "instance_halted": agent_pool.is_instance_halted(sess_name) if (agent_pool and hasattr(agent_pool, 'is_instance_halted')) else False,
        }

    # ── REST endpoints ────────────────────────────────────────────────────

    @app.get("/api/agents")
    async def api_list_agents():
        return [
            {
                'name': getattr(a, 'name', f'Agent-{i}'),
                'index': i,
                'description': getattr(a, 'description', ''),
                # Use _get_active_functions() to respect disabled_tools configuration
                'tools': [f['name'] for f in a._get_active_functions()] if hasattr(a, '_get_active_functions') else [],
            }
            for i, a in enumerate(agents)
        ]

    @app.get("/api/state")
    async def api_get_state():
        try:
            return build_state()
        except Exception as e:
            logger.warning("State build failed: %s", e, exc_info=True)
            return {"agents": [], "messages": [], "agent_instances": {}, "instances": {}, "active_stack": [], "generating": False, "session_name": "Maine", "instance_name": "Maine", "total_tokens": 0, "total_words": 0, "max_tokens": 2048, "summary": "", "has_queued_messages": False, "stopped": False, "current_model": "Unknown", "telemetry": None, "default_workspace": str(DEFAULT_WORKSPACE), "is_waiting": False, "api_router": {"endpoints": [], "agent_priorities": {}}}

    @app.post("/api/reset")
    async def api_reset():
        # Clear pool instance conversation (unified path)
        if agent_pool:
            inst = agent_pool.get_instance(session['session_name'])
            if inst is not None:
                inst.reset_conversation()  # PR3: centralized API handles full reset with cache sync
                # Create a new logger session so messages go to a new JSONL file (Fix: New Session was appending to old logs)
                try:
                    agent_pool._logger.create_new_session(
                        session['session_name'], inst.agent_class
                    )
                except Exception as e:
                    logger.debug(f"Logger reset during new session failed (non-critical): {e}")
        
        # Phase 6: No need to clear session['history'] — pool is the source of truth
        # Fix #1 & #3: Clear performance caches on session reset
        try:
            from agent_cascade.api_integration import _clear_performance_caches
            _clear_performance_caches()
        except Exception as e:
            logger.debug(f"Cache clearing failed during session reset (non-critical): {e}")
        
        # Fix #3 (Feature 020): Wrap session state modifications with session_lock to prevent race condition
        with session_lock:
            session['generating'] = False
            session['generation_id'] += 1
        
        if agent_pool:
            agent_pool.reset()
        await broadcast({'type': 'done', **build_state()})
        return {"status": "ok"}

    @app.post("/api/approve/{request_id}")
    async def api_approve(request_id: str):
        if agent_pool and hasattr(agent_pool, 'operation_manager'):
            result = agent_pool.operation_manager.user_approve(request_id)
            return {"status": "ok", "result": result}
        return {"status": "error", "message": "No operation manager"}

    @app.post("/api/reject/{request_id}")
    async def api_reject(request_id: str, reason: str = "Rejected by user"):
        if agent_pool and hasattr(agent_pool, 'operation_manager'):
            result = agent_pool.operation_manager.user_reject(request_id, reason)
            return {"status": "ok", "result": result}
        return {"status": "error", "message": "No operation manager"}

    @app.post("/api/resume_all")
    async def api_resume_all():
        """Resume all paused agent instances (global resume)."""
        if agent_pool:
            agent_pool.resume()  # clear global pause flag — agents wake naturally from sleep loop
            return {"status": "ok", "message": "All instances resumed"}
        return {"status": "error", "message": "Agent pool not available"}

    @app.get("/api/sessions")
    async def api_list_sessions():
        from pathlib import Path
        if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
            log_dir = agent_pool.operation_manager.base_dir / 'logs'
        else:
            log_dir = Path(DEFAULT_WORKSPACE) / 'logs'
            
        if not log_dir.exists():
            return {"sessions": []}
        
        sessions = []
        for p in log_dir.glob('*.jsonl'):
            try:
                # Basic info from filename: agent_class_instance_name_timestamp.jsonl
                parts = p.stem.split('_')
                if len(parts) >= 3:
                    agent_class = parts[0]
                    timestamp = parts[-2] + "_" + parts[-1]
                    instance_name = "_".join(parts[1:-2])
                else:
                    agent_class = "Unknown"
                    instance_name = p.stem
                    timestamp = "Unknown"
                
                sessions.append({
                    "path": str(p),
                    "name": instance_name,
                    "agent": agent_class,
                    "timestamp": timestamp,
                    "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime
                })
            except Exception as e:
                logger.debug(f"Failed to parse session log file info: {e}")
                continue
        
        # Sort by mtime descending
        sessions.sort(key=lambda x: x['mtime'], reverse=True)
        return {"sessions": sessions}

    @app.get("/api/file")
    async def api_serve_file(path: str):
        from fastapi.responses import FileResponse, JSONResponse
        import os
        
        # Clean file:/// if present
        if path.startswith("file:///"):
            path = path[8:]
        elif path.startswith("file://"):
            path = path[7:]
            
        # Support for windows paths like n:/...
        # Sometimes file:///N:/... gets parsed as N:/...
        
        if os.path.exists(path):
            return FileResponse(path)
        return JSONResponse(status_code=404, content={"message": "File not found"})

    @app.get("/api/telemetry")
    async def api_telemetry():
        """Return session telemetry summary and per-config comparison data."""
        if agent_pool and hasattr(agent_pool, 'telemetry') and agent_pool.telemetry:
            return {
                "session": agent_pool.telemetry.get_session_summary(),
                "configs": agent_pool.telemetry.get_config_comparison(),
                "recent_events": agent_pool.telemetry.get_recent_events(50),
            }
        return {"session": {}, "configs": [], "recent_events": []}

    @app.get("/api/telemetry/export")
    async def api_telemetry_export():
        """Download the raw telemetry JSONL log file."""
        from fastapi.responses import FileResponse, JSONResponse
        if agent_pool and hasattr(agent_pool, 'telemetry') and agent_pool.telemetry:
            path = agent_pool.telemetry.export_jsonl()
            import os
            if os.path.exists(path):
                return FileResponse(path, media_type='application/jsonlines', filename=os.path.basename(path))
        return JSONResponse(status_code=404, content={"message": "No telemetry data available"})

    # ── API Router Endpoints ──────────────────────────────────────────────

    @app.get("/api/endpoints")
    async def api_list_endpoints():
        """List all configured API endpoints and agent priorities."""
        if agent_pool and hasattr(agent_pool, 'api_router'):
            return agent_pool.api_router.to_dict()
        return {"endpoints": [], "agent_priorities": {}}

    @app.post("/api/endpoints")
    async def api_add_endpoint(data: dict):
        """Add a new API endpoint."""
        if not agent_pool or not hasattr(agent_pool, 'api_router'):
            return JSONResponse(status_code=500, content={"message": "No API router"})
        
        from agent_cascade.api_router import APIEndpoint
        try:
            ep = APIEndpoint(
                name=data.get('name', 'New Endpoint'),
                api_base=data.get('api_base', ''),
                api_key=data.get('api_key', 'EMPTY'),
                model=data.get('model', ''),
                model_type=data.get('model_type', 'qwenvl_oai'),
                enabled=data.get('enabled', True),
                max_retries=data.get('max_retries', 2),
            )
            ep_id = agent_pool.api_router.add_endpoint(ep)
            await broadcast({'type': 'state', **build_state()})
            return {"status": "ok", "endpoint_id": ep_id}
        except Exception as e:
            return JSONResponse(status_code=400, content={"message": str(e)})

    @app.put("/api/endpoints/{endpoint_id}")
    async def api_update_endpoint(endpoint_id: str, data: dict):
        """Update an existing API endpoint."""
        if not agent_pool or not hasattr(agent_pool, 'api_router'):
            return JSONResponse(status_code=500, content={"message": "No API router"})
        
        ok = agent_pool.api_router.update_endpoint(endpoint_id, data)
        if ok:
            await broadcast({'type': 'state', **build_state()})
            return {"status": "ok"}
        return JSONResponse(status_code=404, content={"message": "Endpoint not found"})

    @app.delete("/api/endpoints/{endpoint_id}")
    async def api_delete_endpoint(endpoint_id: str):
        """Delete an API endpoint."""
        if not agent_pool or not hasattr(agent_pool, 'api_router'):
            return JSONResponse(status_code=500, content={"message": "No API router"})
        
        ok = agent_pool.api_router.remove_endpoint(endpoint_id)
        if ok:
            await broadcast({'type': 'state', **build_state()})
            return {"status": "ok"}
        return JSONResponse(status_code=404, content={"message": "Endpoint not found"})

    @app.post("/api/endpoints/priorities")
    async def api_set_priorities(data: dict):
        """Set agent-type API endpoint priorities.
        
        Body: { "agent_priorities": { "orchestrator": ["id1", "id2"], ... } }
        """
        if not agent_pool or not hasattr(agent_pool, 'api_router'):
            return JSONResponse(status_code=500, content={"message": "No API router"})
        
        priorities = data.get('agent_priorities', {})
        for agent_type, endpoint_ids in priorities.items():
            agent_pool.api_router.set_agent_priorities(agent_type, endpoint_ids)
        await broadcast({'type': 'state', **build_state()})
        return {"status": "ok"}

    @app.post("/api/endpoints/bulk")
    async def api_bulk_update_endpoints(data: dict):
        """Bulk update all endpoints and priorities (from UI save).
        
        Body: { "endpoints": [...], "agent_priorities": {...} }
        """
        if not agent_pool or not hasattr(agent_pool, 'api_router'):
            return JSONResponse(status_code=500, content={"message": "No API router"})
        
        agent_pool.api_router.from_dict(data)
        await broadcast({'type': 'state', **build_state()})
        return {"status": "ok"}

    # ── WebSocket ─────────────────────────────────────────────────────────

    @app.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket):
        await websocket.accept()
        ws_connections.add(websocket)

        # Send initial state
        try:
            init = {'type': 'state', **build_state()}
            await websocket.send_text(json.dumps(init, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f"WebSocket initial state send failed: {e}")
            ws_connections.discard(websocket)
            return

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.warning(f"Malformed WebSocket message received (skipping): {e}")
                    continue

                msg_type = data.get('type', '')

                # ── Send message / async inject ──
                if msg_type == 'message':
                    text = data.get('text', '').strip()
                    if not text:
                        continue

                    # FIX 1 (Race Condition): Protect session['generating'] check with session_lock
                    # to prevent two concurrent engine.run() calls for the same instance.
                    # Without this lock, two WebSocket messages arriving nearly simultaneously
                    # could both read generating=False and start separate runs.
                    is_generating = _is_generating(session)

                    if is_generating:
                        # Async injection while agent is running — route to target agent
                        if agent_pool:
                            target = data.get('target_agent') or session.get('session_name', 'Maine')
                            agent_pool.enqueue_message(target, text)
                        continue

                    # Update session config if provided
                    if 'agent_index' in data:
                        session['agent_index'] = int(data['agent_index'])
                    if 'session_name' in data:
                        session['session_name'] = data['session_name']
                    if 'generate_cfg' in data:
                        _validate_disabled_tools(data['generate_cfg'])
                        session['generate_cfg'] = data['generate_cfg']

                    # Add user message to the pool instance's conversation (unified path).
                    # Note: /compress and /rollback commands are now handled through the agent turn loop
                    # in handler.py, allowing a single unified processing path for all messages.
                    # This replaces: session['history'].append(...) + sync to pool.
                    parsed_content = _parse_multimodal_content(text)
                    instance_name = data.get('target_agent') or session['session_name']
                    
                    # Ensure the main agent instance exists before adding the message.
                    # If it doesn't, create it with the system message from the agent runner.
                    if agent_pool:
                        inst = agent_pool.get_instance(instance_name)
                        if inst is None or not inst.conversation:
                            # Extract system message content from the active agent runner
                            # Priority: base_system_message > system_message (the actual attribute names on Agent)
                            sys_content = None
                            agent_runner = get_agent()
                            if hasattr(agent_runner, 'base_system_message') and agent_runner.base_system_message:
                                sys_content = str(agent_runner.base_system_message)
                            elif hasattr(agent_runner, 'system_message') and agent_runner.system_message:
                                sys_content = str(agent_runner.system_message)
                            elif hasattr(agent_runner, 'llm') and hasattr(agent_runner.llm, 'cfg'):
                                sys_content = agent_runner.llm.cfg.get('system', '') or agent_runner.llm.cfg.get('system_message', '')
                            
                            if sys_content:
                                create_main_agent_instance(
                                    pool=agent_pool,
                                    instance_name=instance_name,
                                    system_message_content=sys_content,
                                )
                        
                        # FIX Major #4: Clear any stale continue state for fresh turn
                        inst = agent_pool.get_instance(instance_name)  # Re-get for fresh turn (may differ from continue handler's instance ref)
                        if inst is not None:
                            with inst._compression_lock:
                                if inst._continue_saved_msg is not None:
                                    logger.debug(f"[CONTINUE_FIX] Cleared stale _continue_saved_msg for {instance_name} on new message")
                                    inst._continue_saved_msg = None
                        
                        # Enqueue the user message — same queue drained during turn loop via _drain_and_inject
                        agent_pool.enqueue_message(instance_name, parsed_content)

                    # Start agent generation
                    gen_id = _start_generation(session)
                    if agent_pool:
                        agent_pool.stopped = False
                    
                    agent_runner = get_agent()
                    loop = asyncio.get_event_loop()

                    thread = threading.Thread(
                        target=run_agent_thread,
                        args=(None, agent_runner, gen_id, loop, instance_name),
                        daemon=True,
                    )
                    thread.start()

                    await broadcast({'type': 'state', **build_state(generating=True)})

                elif msg_type == 'continue':
                    # Continue generation WITHOUT inserting a new user message.
                    # Just send the existing conversation to the LLM so it can resume if it wants.
                    
                    # FIX 1d: Protect session['generating'] read with lock (consistent with Fix 1)
                    # Continue DOES start threads - concurrent continue messages need the same guard.
                    is_generating = _is_generating(session)
                    if is_generating:
                        continue
                    
                    # Update session config if provided
                    if 'agent_index' in data:
                        session['agent_index'] = int(data['agent_index'])
                    if 'session_name' in data:
                        session['session_name'] = data['session_name']
                    if 'generate_cfg' in data:
                        _validate_disabled_tools(data['generate_cfg'])
                        session['generate_cfg'] = data['generate_cfg']

                    # Resolve the target instance (prefer target_agent from frontend, fallback to session)
                    continue_instance_name = data.get('target_agent') or session['session_name']
                    inst = None
                    if agent_pool:
                        inst = agent_pool.get_instance(continue_instance_name)
                    
                    # Start agent generation with existing history (no new user message)
                    gen_id = _start_generation(session)
                    if agent_pool:
                        agent_pool.stopped = False

                    agent_runner = get_agent()
                    loop = asyncio.get_event_loop()

                    # Get the current history from the pool (unified path)
                    if inst is None:
                        await broadcast({'type': 'error', 'message': 'No agent instance found to continue'})
                        continue
                    
                    # FIX: Option B - Pop trailing assistant message before deepcopying history.
                    # This prevents duplication because when Continue is clicked, the engine sends 
                    # the full conversation (including last assistant message) to LLM, which then 
                    # generates a NEW assistant message that gets appended separately. By popping 
                    # it first and merging after LLM responds, we get a single concatenated message.
                    saved_assistant_msg = None
                    with inst._compression_lock:
                        if inst.conversation:
                            last_msg = inst.conversation[-1]
                            last_role = _get_msg_role(last_msg)
                            if last_role == ASSISTANT:
                                saved_assistant_msg = inst.conversation.pop()
                                # Store on instance for merging in execution_engine._process_response
                                inst._continue_saved_msg = saved_assistant_msg
                    
                    history_copy = copy.deepcopy(inst.conversation)

                    thread = threading.Thread(
                        target=run_agent_thread,
                        args=(history_copy, agent_runner, gen_id, loop, continue_instance_name),
                        daemon=True,
                    )
                    thread.start()

                    await broadcast({'type': 'state', **build_state(generating=True)})

                elif msg_type == 'stop':
                    # Stop all streaming and set ALL active agents to IDLE state.
                    with session_lock:
                        session['stop_requested'] = True
                        session['generating'] = False
                        session['generation_id'] += 1
                    
                    if agent_pool:
                        # Transition ALL active agents to IDLE state (not just reset)
                        for inst_name, instance in list(agent_pool.instances.items()):
                            try:
                                # CRITICAL FIX: Mark activity BEFORE transitioning to IDLE so idle timer starts from stop event
                                agent_pool._mark_activity(inst_name)
                                
                                # Read state INSIDE lock to avoid race condition (Reviewer Finding #2)
                                with instance._state_lock:
                                    current_state = instance.state
                                    if current_state in ACTIVE_STATES:
                                        # Use _transition() instead of direct assignment for proper validation (Finding #1)
                                        instance._transition(AgentState.IDLE)
                                        logger.info(f"Stop: Transitioned {inst_name} from {current_state.name} to IDLE")
                                
                                # FIX Critical #1: Clear any pending continue merge state on stop
                                with instance._compression_lock:
                                    if instance._continue_saved_msg is not None:
                                        logger.debug(f"[CONTINUE_FIX] Stop handler cleared _continue_saved_msg for {inst_name}")
                                        instance._continue_saved_msg = None
                            except InvalidStateTransition as e:
                                logger.warning(f"Invalid state transition for {inst_name}: {e}")
                            except Exception as e:
                                logger.warning(f"Failed to transition {inst_name} to IDLE: {e}")
                        
                        # Halt threads, release slots, and unblock pending approvals (non-destructive — preserves sessions)
                        agent_pool.stop_session()

                        # Increment run generation AFTER slot release so old threads see both signals:
                        # pool.stopped=True + _run_generation bumped. New resume threads snapshot the
                        # incremented value; stale threads detect mismatch on next _is_stopped() check.
                        agent_pool._run_generation += 1
                    
                    # FIX 1 & 2: Clean up active stack and halted state after stop_session()
                    if agent_pool:
                        try:
                            from agent_cascade.agent_instance import AgentState as InstanceAgentState
                            
                            # Slot release is handled by stop_session() — no need to duplicate here.
                            # Just clean up active_stack and _halted_instances.
                            
                            # CRIT-3 FIX: Use active_stack[:] = [...] to mutate list in place, not replace it.
                            # Other code may hold references to the old list; mutation ensures all refs see updates.
                            if hasattr(agent_pool, '_execution') and hasattr(agent_pool._execution, 'active_stack'):
                                with agent_pool._execution._state_lock:
                                    logger.debug(f"[STOP_STACK_CLEANUP] Cleaning active_stack: {agent_pool._execution.active_stack}")
                                    original_len = len(agent_pool._execution.active_stack)
                                    # Mutate in place instead of replacing the list
                                    agent_pool._execution.active_stack[:] = [
                                        (name, depth) for name, depth in agent_pool._execution.active_stack
                                        if name not in agent_pool.terminated_instances
                                    ]
                                    removed_count = original_len - len(agent_pool._execution.active_stack)
                                    if removed_count > 0:
                                        logger.debug(f"[STOP_STACK_CLEANUP] Removed {removed_count} terminated entries from active_stack")
                            
                            # MINOR-4 FIX: Clear _halted_instances to prevent stale pause state after stop
                            if hasattr(agent_pool, '_halted_instances'):
                                agent_pool._halted_instances.clear()
                                logger.debug("[STOP_HALTED_CLEANUP] Cleared _halted_instances")
                        except Exception as e:
                            logger.warning(f"[STOP_CLEANUP_ERROR] Error during slot/stack cleanup: {e}")
                    
                    await broadcast({'type': 'done', **build_state()})

                elif msg_type == 'pause':
                    # Pause ALL running instances by setting global flag
                    if agent_pool:
                        inst_names = list(agent_pool.instances.keys())
                        agent_pool.pause()
                        logger.info(f"Paused all instances: {inst_names}")
                        # Mark session as not generating while paused (Issue #7)
                        _stop_generation(session)
                    # Broadcast updated state so frontend reflects paused status for all agents
                    try:
                        await broadcast({'type': 'state', **build_state(generating=False)})
                    except Exception as e:
                        logger.warning(f"Failed to broadcast pause state: {e}")

                elif msg_type == 'resume_all':
                    # Resume ALL paused instances by clearing the global flag.
                    # Agents wake up naturally from their 100ms sleep loop — no thread restart needed.
                    if agent_pool:
                        agent_pool.resume()  # clear global pause flag
                        logger.info("Cleared global pause flag — all agents will resume naturally")
                    
                    # Mark session as generating again
                    _set_generating_true(session)
                
                    # Broadcast updated state so frontend reflects resumed status
                    try:
                        await broadcast({'type': 'state', **build_state(generating=True)})
                    except Exception as e:
                        logger.warning(f"Failed to broadcast resume_all state: {e}")
                                
                elif msg_type == 'resume':
                    # Resume a paused instance — clear the global flag so agents wake up naturally
                    target_instance = data.get('instance_name', session['session_name'])
                    is_generating = _is_generating(session)
                    
                    was_halted = False
                    if agent_pool:
                        was_halted = agent_pool.is_instance_halted(target_instance)
                        agent_pool.resume()  # clear global pause flag
                        logger.info(f"Instance {target_instance} resumed by user. Was halted: {was_halted}")
                    
                    # For the main session: only restart generation if it was actually halted
                    if target_instance == session['session_name']:
                        if is_generating and was_halted:
                            # Currently generating but was halted — stop old thread first, then restart generation
                            logger.info(f"Main session was still generating — signalling stop before resume.")
                            _signal_stop(session)
                            agent_pool.stopped = True
                            # Brief delay to allow old thread to observe the stop signal
                            await asyncio.sleep(0.1)
                        
                        if was_halted:
                            # Was halted — agents wake naturally from sleep loop, no continuation message needed
                            # Start agent generation
                            with session_lock:
                                session['stop_requested'] = False
                            if agent_pool:
                                agent_pool.stopped = False
                                
                                # ── Fix 3: Restore agent instance conversations from JSONL logs if corrupted ──
                                # After a failed forced compression cycle, agent instance pools may be empty/corrupted.
                                # Read directly from log files on disk to recover.
                                # Import validate_message_pool locally (moved to utils/pool_validation.py in Phase 2)
                                from agent_cascade.utils.pool_validation import validate_message_pool
                                
                                for sa_name, agent_class in list(agent_pool.instance_classes.items()):
                                        if sa_name == session['session_name']:
                                            continue  # Skip main session — already synced above
                                        
                                        # Skip orphaned or root instances (only recover agent instances)
                                        sa_inst = agent_pool.get_instance(sa_name)
                                        if sa_inst is None:
                                            continue  # Instance not found, skip
                                        
                                        try:
                                            # Check if current conversation is valid before restoring
                                            with sa_inst._compression_lock:
                                                conv_snapshot = list(sa_inst.conversation)
                                            if validate_message_pool(conv_snapshot, sa_name):
                                                continue  # Conversation is fine, no need to restore
                                            
                                            # Find the actual log file via existing logger or glob
                                            recov = []
                                            logger_inst = agent_pool.instance_loggers.get(sa_name)
                                            
                                            if logger_inst and hasattr(logger_inst, 'log_path') and logger_inst.log_path:
                                                actual_log_path = logger_inst.log_path
                                            else:
                                                # Search for the most recent log file matching this agent instance
                                                if hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                                                    log_dir = agent_pool.operation_manager.base_dir / 'logs'
                                                else:
                                                    log_dir = Path(DEFAULT_WORKSPACE) / 'logs'
                                                pattern = f"{agent_class}_{sa_name}_*.jsonl"
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
                                                            if "metadata" not in item and "event" not in item:  # Skip metadata lines and event entries
                                                                recov.append(item)
                                                        except json.JSONDecodeError as e:
                                                            logger.debug(f"Skipping malformed JSONL line in agent pool recovery: {e}")
                                                            continue
                                            
                                            # Only overwrite pool if recovered data is valid
                                            if recov and validate_message_pool(recov, sa_name):
                                                logger.info(
                                                    f"Restoring agent instance {sa_name} conversation from log during resume "
                                                    f"({len(recov)} messages)"
                                                )
                                                sa_inst = agent_pool.get_instance(sa_name)
                                                if sa_inst is not None:
                                                    sa_inst.rebuild_conversation(recov)  # PR3: centralized API handles full rebuild with cache sync
                                            else:
                                                logger.warning(
                                                    f"Could not restore agent instance {sa_name} pool — "
                                                    f"no valid recovery data found in logs"
                                                )
                                        except Exception as _e:
                                            # Single agent failure shouldn't block resume for others
                                            logger.warning(f"Failed to restore agent instance {sa_name} pool: {_e}")
                                # ── End Fix 3 ──
                            
                            # Fix #3 (Feature 020): Wrap session state modifications with session_lock to prevent race condition
                            if not _is_generating(session):
                                gen_id = _start_generation(session)
                                
                                agent_runner = get_agent()
                                loop = asyncio.get_event_loop()

                                thread = threading.Thread(
                                    target=run_agent_thread,
                                    args=(None, agent_runner, gen_id, loop, target_instance),
                                    daemon=True,
                                )
                                thread.start()

                            # FIX 2: Move await broadcast outside session_lock to avoid holding lock during async I/O
                            await broadcast({'type': 'state', **build_state(generating=True)})
                        elif not is_generating:
                            # Not halted and not generating — just update UI state (no-op from user's perspective)
                            await broadcast({'type': 'state', **build_state()})
                    
                    # For agent instances: agents wake naturally from sleep loop, no continuation message needed
                elif msg_type in ('terminate_agent_instance', 'terminate_sub_agent'):
                    """Terminate the specified agent instance and set it to TERMINATED state."""
                    # Add session_lock guard for consistency with other handlers (Finding #5)
                    is_generating = _is_generating(session)
                    
                    instance_name = data.get('instance_name')
                    if instance_name and agent_pool:
                        # SAFEGUARD: Never allow terminating the root orchestrator — it breaks the session.
                        # If a frontend bug sends the session name, just transition it to IDLE instead.
                        inst = agent_pool.get_instance(instance_name)
                        is_root = (inst is not None and inst.parent_instance is None)
                        
                        if is_root:
                            logger.warning(f"Terminate requested for root orchestrator '{instance_name}' — blocked. Transitioning to IDLE instead.")
                            with session_lock:
                                session['stop_requested'] = True
                                session['generating'] = False
                                session['generation_id'] += 1
                            agent_pool._stopped_event.set()
                            
                            # CRITICAL FIX: Mark activity BEFORE transitioning to IDLE so idle timer starts from stop event
                            agent_pool._mark_activity(instance_name)
                            
                            with inst._state_lock:
                                current_state = inst.state
                                if current_state in ACTIVE_STATES:
                                    inst._transition(AgentState.IDLE)
                            await broadcast({'type': 'done', **build_state()})
                            continue
                        
                        # Get parent instance name for feedback BEFORE dismissal (Finding #4)
                        parent_instance = getattr(inst, 'parent_instance', None) if inst else None
                        
                        # Enqueue feedback message to parent/caller agent BEFORE dismiss_instance() removes it
                        if parent_instance and parent_instance != instance_name:
                            feedback_msg = f"[SYSTEM]: Agent '{instance_name}' has been terminated by user."
                            agent_pool.enqueue_message(parent_instance, feedback_msg)
                            logger.info(f"Enqueued termination feedback to {parent_instance}: {feedback_msg}")
                        
                        # dismiss_instance() handles:
                        # - Recursive dismissal of child agents (cascade termination)
                        # - State transition to TERMINATED for active agents
                        # - Removal from the pool
                        agent_pool.dismiss_instance(instance_name)
                        
                        # Broadcast updated state immediately so frontend reflects terminated agent
                        await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'retry':
                    # FIX 1c: Protect session['generating'] read with lock (consistent with Fix 1)
                    # Retry DOES start threads - concurrent retry messages need the same guard.
                    is_generating = _is_generating(session)
                    if is_generating:
                        continue
                    
                    instance_name = data.get('target_agent') or session['session_name']
                    
                    # Remove trailing assistant/function messages from the pool instance's conversation (unified path)
                    if agent_pool:
                        inst = agent_pool.get_instance(instance_name)
                        if inst is not None:
                            # PR3 migration: Use centralized API for retry rollback
                            # Count trailing assistant/function messages first, then trim once (reviewer optimization)
                            count_to_trim = 0
                            while inst.conversation and count_to_trim < len(inst.conversation) and _get_msg_role(inst.conversation[-1 - count_to_trim]) in (ASSISTANT, FUNCTION):
                                count_to_trim += 1
                            if count_to_trim > 0:
                                removed = inst.trim_tail(count_to_trim)

                    # Roll back one more (the user message) to allow a clean re-trigger
                    last_user_msg = None
                    inst = agent_pool.get_instance(instance_name)
                    if inst is not None and inst.conversation and _get_msg_role(inst.conversation[-1]) == USER:
                        # PR3 migration: Use centralized API for removing user message
                        removed = inst.trim_tail(1)
                        last_user_msg = removed[0] if removed else None

                    # Sync JSONL log with trimmed conversation state to prevent ghost entries (desync fix)
                    if agent_pool and inst is not None:
                        try:
                            log_inst = agent_pool.get_logger(instance_name, inst.agent_class)
                            log_inst.reset_history(list(inst.conversation), rewrite=True)
                        except Exception as e:
                            logger.debug(f"Logger sync after retry trim failed for {instance_name} (non-critical): {e}")

                    # Post-unification: use pool instance directly — no legacy fallback needed
                    inst = agent_pool.get_instance(instance_name) if agent_pool else None
                    if not inst and not (inst.conversation if inst else []) and not last_user_msg:
                        _save_session_history()
                        await broadcast({'type': 'state', **build_state()})
                        continue
                    
                    # Clear active tools/agent stack since we are retrying from the main input level
                    if agent_pool:
                        agent_pool.active_stack_clear()  # active_stack property returns defensive copy; use mutation method
                        agent_pool.last_tool_args.clear()

                        # 1. Rollback agent instances to the start of the last turn
                        if session.get('last_turn_snapshots'):
                            agent_pool.rollback_to_snapshots(session['last_turn_snapshots'], reason="User retry")
                            
                            # Sync instance_state so build_state() sees the rolled-back histories
                            for name in session['last_turn_snapshots']:
                                if name != session['session_name'] and name in agent_pool.instance_state:
                                    sa_inst = agent_pool.get_instance(name)
                                    if sa_inst is not None:
                                        with sa_inst._compression_lock:
                                            conv_snapshot = list(sa_inst.conversation)
                                        agent_pool.instance_state[name]['messages'] = conv_snapshot

                    # Now "send it again": re-append the user message to pool instance (unified path)
                    if last_user_msg and agent_pool:
                        inst = agent_pool.get_instance(instance_name)
                        if inst is not None:
                            # PR3 migration: Use centralized API for insert at arbitrary position
                            # Use insertion point logic to avoid splitting tool call/response pairs
                            insert_pos = _find_user_message_insertion_point(inst.conversation)
                            inst.insert_message_at(insert_pos, last_user_msg)
                            
                            # Log the re-inserted user message to JSONL (desync fix)
                            try:
                                log_inst = agent_pool.get_logger(instance_name, inst.agent_class)
                                with inst._compression_lock:
                                    conv_snapshot = list(inst.conversation)
                                log_inst.update_history(conv_snapshot)
                            except Exception as e:
                                logger.debug(f"Logger sync after retry re-insert failed for {instance_name} (non-critical): {e}")
                        else:
                            # Fallback: create the instance first, then enqueue the message
                            create_main_agent_instance(
                                pool=agent_pool,
                                instance_name=instance_name,
                                system_message_content="",
                            )
                            agent_pool.enqueue_message(instance_name, last_user_msg.content)

                    if 'generate_cfg' in data:
                        session['generate_cfg'] = data['generate_cfg']

                    gen_id = _start_generation(session)
                    if agent_pool:
                        agent_pool.stopped = False
                    agent_runner = get_agent()
                    loop = asyncio.get_event_loop()

                    thread = threading.Thread(
                        target=run_agent_thread,
                        args=(None, agent_runner, gen_id, loop, instance_name),
                        daemon=True,
                    )
                    thread.start()
                    await broadcast({'type': 'state', **build_state(generating=True)})

                elif msg_type == 'reset':
                    # Clear pool instance conversation (unified path)
                    if agent_pool:
                        inst = agent_pool.get_instance(session['session_name'])
                        if inst is not None:
                            inst.reset_conversation()  # PR3: centralized API handles full reset with cache sync
                            # Create a new logger session so messages go to a new JSONL file (Fix: New Session was appending to old logs)
                            try:
                                agent_pool._logger.create_new_session(
                                    session['session_name'], inst.agent_class
                                )
                            except Exception as e:
                                logger.debug(f"Logger reset during stop failed (non-critical): {e}")
                    
                    with session_lock:
                        # Phase 6: No need to clear session['history'] — pool is the source of truth
                        session['stop_requested'] = True
                        session['generating'] = False
                        session['generation_id'] += 1
                    if agent_pool:
                        agent_pool.stopped = True
                        agent_pool.reset()
                    await broadcast({'type': 'done', **build_state()})

                elif msg_type == 'refresh_souls':
                    if agent_pool:
                        agent_pool.refresh_agents()
                        # Update the global agents list used by build_state
                        nonlocal agents
                        agents = [agent_pool.get_agent(name) for name in agent_pool.list_agents()]
                        # Ensure orchestrator is at index 0 if possible
                        if 'orchestrator' in agent_pool.agents:
                            orch = agent_pool.agents['orchestrator']
                            agents = [orch] + [a for a in agents if a != orch]
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'restart_server':
                    logger.warning("Server restart requested via UI")
                    await broadcast({'type': 'error', 'message': 'Server is restarting... Please wait.'})
                    import sys
                    import os
                    os.execl(sys.executable, sys.executable, *sys.argv)

                elif msg_type == 'update_config':
                    if 'generate_cfg' in data:
                        session['generate_cfg'] = data['generate_cfg']
                        ui_cfg = data['generate_cfg']
                        if 'mcpServers' in ui_cfg:
                            mcp_servers = ui_cfg['mcpServers']
                            try:
                                from agent_cascade.tools.mcp_manager import MCPManager
                                mcp_tools = MCPManager().initConfig({'mcpServers': mcp_servers})
                                for tool in mcp_tools:
                                    for agent_inst in agents:
                                        if tool.name not in agent_inst.function_map:
                                            agent_inst.function_map[tool.name] = tool
                                logger.info("[MCP] Eagerly loaded %d tools.", len(mcp_tools))
                            except Exception as e:
                                logger.warning("[MCP] Eager initialization failed: %s", e)
                        # Defense-in-depth optimization: only call set_extra_work_folders if values actually changed
                        # This avoids unnecessary function calls when UI sends full config on any setting change (font size, colors, etc.)
                        if 'work_access_folders_ro' in ui_cfg or 'work_access_folders_rw' in ui_cfg:
                            if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                                om = agent_pool.operation_manager
                                ro_new = [p.strip() for p in ui_cfg.get('work_access_folders_ro', []) if p.strip()]
                                rw_new = [p.strip() for p in ui_cfg.get('work_access_folders_rw', []) if p.strip()]
                                # Compare against current state (normalized to strings for comparison)
                                ro_current = [str(p) for p in om.extra_work_folders_ro]
                                rw_current = [str(p) for p in om.extra_work_folders_rw]
                                # Sort and lowercase for order-independent, case-insensitive comparison
                                # Note: This is intentionally more aggressive than the setter's frozenset comparison.
                                # On Windows (case-insensitive FS), this prevents unnecessary calls even when casing differs.
                                ro_sorted = sorted([p.lower() for p in ro_new])
                                rw_sorted = sorted([p.lower() for p in rw_new])
                                ro_curr_sorted = sorted([p.lower() for p in ro_current])
                                rw_curr_sorted = sorted([p.lower() for p in rw_current])
                                if ro_sorted != ro_curr_sorted or rw_sorted != rw_curr_sorted:
                                    om.set_extra_work_folders(ro_new, rw_new)
                                else:
                                    logger.debug("[update_config] Extra work folders unchanged (RO=%d, RW=%d), skipping set_extra_work_folders", len(ro_new), len(rw_new))
                        # Defense-in-depth optimization: only call set_base_dir if value actually changed
                        if 'default_workspace' in ui_cfg:
                            if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                                new_ws = ui_cfg['default_workspace']
                                if not new_ws:
                                    logger.debug("[update_config] Empty default_workspace received, skipping set_base_dir")
                                else:
                                    # Compare against current base_dir before calling setter
                                    new_ws_path = Path(new_ws).resolve()
                                    if new_ws_path != agent_pool.operation_manager.base_dir:
                                        agent_pool.operation_manager.set_base_dir(new_ws)
                                    else:
                                        logger.debug("[update_config] Base workspace unchanged (%s), skipping set_base_dir", new_ws)
                        # Update idle timeout from UI settings (Bug #3 fix)
                        if 'idle_timeout_seconds' in ui_cfg and agent_pool and hasattr(agent_pool, 'settings'):
                            val = float(ui_cfg['idle_timeout_seconds'])
                            agent_pool.settings.idle_timeout_seconds = max(0.0, val)  # 0 disables auto-dismissal
                        # Update approval timeout from UI settings
                        if 'approval_timeout_seconds' in ui_cfg and agent_pool:
                            try:
                                agent_pool.operation_manager.set_approval_timeout(int(ui_cfg['approval_timeout_seconds']))
                            except Exception as e:
                                logger.warning(f"Failed to set approval timeout: {e}")
                        if 'enable_approval_timeout' in ui_cfg and agent_pool:
                            try:
                                agent_pool.operation_manager.set_enable_timeout(bool(ui_cfg['enable_approval_timeout']))
                            except Exception as e:
                                logger.warning(f"Failed to set approval timeout toggle: {e}")
                        # Update max parallel agents — resize ThreadPoolExecutor (Bug #16 fix)
                        if 'max_parallel_agents' in ui_cfg and agent_pool and hasattr(agent_pool, 'settings'):
                            val = int(ui_cfg['max_parallel_agents'])
                            agent_pool.settings.max_workers = max(1, val)  # Clamp to at least 1
                            # Resize the executor if it exists
                            if hasattr(agent_pool._execution, 'executor') and agent_pool._execution.executor is not None:
                                agent_pool._execution.resize_executor(agent_pool.settings.max_workers)
                            else:
                                logger.warning("[THREAD_POOL] resize_executor skipped — executor is None (pool just initialized?)")
                        # Apply auto_continue immediately (takes effect without waiting for next agent run)
                        if 'auto_continue' in ui_cfg and agent_pool and hasattr(agent_pool, 'settings'):
                            agent_pool.settings.auto_continue = bool(ui_cfg['auto_continue'])
                        # Defense-in-depth optimization: only call update_default_llm_cfg if LLM-related keys changed
                        # This avoids unnecessary function calls when UI sends full config on non-LLM setting changes (font size, colors, etc.)
                        if agent_pool and hasattr(agent_pool, 'api_router'):
                            # Extract only LLM-relevant keys from ui_cfg to compare
                            # Note: Only checks keys present in new_llm_cfg (partial update semantics), matching update_default_llm_cfg behavior
                            new_llm_cfg = {k: v for k, v in ui_cfg.items() if k in LLM_CONFIG_KEYS}
                            current_llm_cfg = agent_pool.api_router.default_llm_cfg or {}  # Defensive: handle None
                            # Compare only the LLM-relevant keys
                            if new_llm_cfg != {k: current_llm_cfg.get(k) for k in new_llm_cfg}:
                                # BUG FIX: Pass only LLM-relevant keys to prevent polluting default_llm_cfg with non-LLM settings
                                agent_pool.api_router.update_default_llm_cfg(new_llm_cfg)
                            else:
                                logger.debug("[update_config] LLM config unchanged (%d keys), skipping update_default_llm_cfg", len(new_llm_cfg))
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'update_endpoints':
                    # Bulk update all endpoints and priorities from UI
                    if agent_pool and hasattr(agent_pool, 'api_router'):
                        ep_count = len(data.get('endpoints', []))
                        ap_count = len(data.get('agent_priorities', {}))
                        logger.info(f"[update_endpoints] Received: {ep_count} endpoints, {ap_count} agent priority mappings")
                        agent_pool.api_router.from_dict(data)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'update_api_priorities':
                    # Update just the agent-type → endpoint priority mappings
                    if agent_pool and hasattr(agent_pool, 'api_router'):
                        priorities = data.get('agent_priorities', {})
                        logger.info(f"[update_api_priorities] Received {len(priorities)} priority mappings: "
                                   f"{list(priorities.keys())}")
                        for agent_type, endpoint_ids in priorities.items():
                            agent_pool.api_router.set_agent_priorities(agent_type, endpoint_ids)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'approve':
                    rid = data.get('request_id')
                    if rid and agent_pool:
                        is_auto = data.get('automated', False)
                        logger.info(f"[{'AUTO' if is_auto else 'USER'}] Approving request: {rid}")
                        agent_pool.operation_manager.user_approve(rid)
                        # Fix #7: Immediate broadcast after approve to update UI instantly (~300ms latency reduction)
                        await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'reject':
                    rid = data.get('request_id')
                    reason = data.get('reason', 'Rejected by user')
                    if rid and agent_pool:
                        is_auto = data.get('automated', False)
                        logger.info(f"[{'AUTO' if is_auto else 'USER'}] Rejecting request: {rid}. Reason: {reason}")
                        agent_pool.operation_manager.user_reject(rid, reason)
                        # Fix #7: Immediate broadcast after reject to update UI instantly (~300ms latency reduction)
                        await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'ask_security':
                    if not hasattr(app, 'security_check_lock'):
                        app.security_check_lock = threading.Lock()
                        
                    rid = data.get('request_id')
                    auto_apply = data.get('auto_apply', False)
                    if rid and agent_pool:
                        pending = agent_pool.operation_manager.list_pending_approvals()
                        ap = next((a for a in pending if a['request_id'] == rid), None)
                        if ap:
                            if not hasattr(app, 'active_security_checks_lock'):
                                app.active_security_checks_lock = threading.Lock()
                            if not hasattr(app, 'active_security_checks'):
                                app.active_security_checks = set()
                            
                            with app.active_security_checks_lock:
                                if rid in app.active_security_checks:
                                    logger.warning(f"Security check already active for request {rid}, ignoring duplicate.")
                                    continue
                                app.active_security_checks.add(rid)

                            loop = asyncio.get_running_loop()
                            def _security_check(rid=rid, auto_apply=auto_apply, ap=ap, loop=loop):
                                sec_state_key = None  # Defined early so finally can reference it directly (Fix #4)
                                sec_instance = None   # Pre-initialize for defensive programming
                                engine = None         # Pre-initialize for defensive programming
                                try:
                                    import platform
                                    import json
                                    import copy

                                    with app.security_check_lock:
                                        workspace_info = f"Main workspace: {agent_pool.operation_manager.base_dir}\n"
                                        if agent_pool.operation_manager.extra_work_folders_ro:
                                            extra = [str(p) for p in agent_pool.operation_manager.extra_work_folders_ro]
                                            workspace_info += f"Additional RO folders: {', '.join(extra)}\n"
                                        if agent_pool.operation_manager.extra_work_folders_rw:
                                            extra = [str(p) for p in agent_pool.operation_manager.extra_work_folders_rw]
                                            workspace_info += f"Additional RW folders: {', '.join(extra)}\n"
                                    
                                        prompt = SECURITY_ADVISOR_PROMPT.format(
                                            tool_name=ap.get('tool_name', 'unknown'),
                                            description=ap.get('description', ''),
                                            arguments=json.dumps(ap.get('tool_args', {})),
                                            os_info=f"{platform.system()} {platform.release()}",
                                            workspace_info=workspace_info
                                        )

                                        # FIX 1: Use unique instance name per request_id to prevent state corruption
                                        # This ensures concurrent security checks don't overwrite each other's state
                                        sec_state_key = f'Security_{rid}'  # e.g., 'Security_op_091f048b'
                                        
                                        # FIX 2: Create engine and instance INSIDE the lock to prevent race conditions
                                        # Previously, instances were created before semaphore acquire, causing lifecycle_manager collisions
                                        engine = ExecutionEngine(agent_pool)
                                        # initialize() now called automatically in __init__ (Phase 4.5 cleanup)
                                        sec_instance = engine._create_system_agent(
                                            agent_class='Security',  # Agent CLASS name (NOT instance name — that's sec_state_key)
                                            instance_name=sec_state_key,  # Use unique name from FIX 1
                                            task=prompt,
                                            caller=session.get('session_name', 'Orchestrator')
                                        )

                                        # Configure with UI settings using centralized constants and utilities.
                                        # Note: propagate_settings() already ran inside _create_system_agent(), but we
                                        # intentionally replace its disabled_tools result with ui_cfg + defense-in-depth
                                        # defaults to ensure auto-launched agents never get more tools than intended.
                                        from agent_cascade.constants import NON_LLM_KEYS, DEFAULT_SECURITY_DISABLED_TOOLS
                                        from agent_cascade.utils import merge_disabled_tools_for_auto_agent
                                        
                                        ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))
                                        llm_safe_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
                                        # Add disabled_tools back — needed for tool filtering but must not leak to LLM API
                                        if 'disabled_tools' in ui_cfg:
                                            llm_safe_cfg['disabled_tools'] = ui_cfg['disabled_tools']
                                        # Defense-in-depth: disable user-approval tools for auto-launched Security agent
                                        existing_disabled = llm_safe_cfg.get('disabled_tools', [])
                                        llm_safe_cfg['disabled_tools'] = merge_disabled_tools_for_auto_agent(
                                            existing_disabled, 'Security', DEFAULT_SECURITY_DISABLED_TOOLS
                                        )
                                    
                                        template = agent_pool.get_template('Security')  # Template lookup by CLASS name (NOT instance name)
                                        if template and hasattr(template, 'llm'):
                                            cfg = (template.llm.generate_cfg or {}).copy()
                                            # logger.debug(f"[SECURITY] Before update - llm_safe_cfg disabled_tools: {llm_safe_cfg.get('disabled_tools', 'MISSING')}")
                                            cfg.update(llm_safe_cfg)
                                            sec_instance._generate_cfg_override = cfg
                                            # logger.info(f"[SECURITY] Set _generate_cfg_override for '{sec_state_key}': disabled_tools={cfg.get('disabled_tools', 'NOT SET')}")
                                        else:
                                            logger.warning(f"[SECURITY] Template missing or has no llm attribute for '{sec_state_key}'")
                                            # Fallback: set disabled_tools directly even without template
                                            sec_instance._generate_cfg_override = {'disabled_tools': llm_safe_cfg.get('disabled_tools', [])}

                                        logger.info(f"[SECURITY] Created AgentInstance '{sec_state_key}' for request {rid}")
                                        # Initialize variables for engine.run() flow
                                        sec_timeout_reached = False
                                        sec_elapsed_at_timeout = None
                                        sec_start_time = time.monotonic()

                                        # Schedule warning timer (keep existing _sec_warning_injector logic)
                                        def _sec_warning_injector():
                                            try:
                                                # FIX 1 continuation: Use unique sec_state_key for message routing
                                                agent_pool.enqueue_message(
                                                    sec_state_key,
                                                    "[SYSTEM WARNING] Your analysis is taking longer than expected. "
                                                    "Please provide a verdict as soon as possible — the approval request may timeout soon."
                                                )
                                            except Exception as e:
                                                logger.debug(f"Security advisor warning injection failed (non-critical): {e}")

                                        sec_warning_timer = threading.Timer(SECURITY_ADVISOR_WARNING_SECONDS, _sec_warning_injector)
                                        sec_warning_timer.daemon = True
                                        sec_warning_timer.start()

                                    # ── SLOT BYPASS FOR SECURITY ADVISOR ──
                                    # The Security agent runs on a separate daemon thread (line 2252).
                                    # The caller's concurrency slot is bound to the caller's execution context
                                    # and cannot be acquired from a different thread — attempting so would either
                                    # bypass the intended serialization or deadlock.
                                    #
                                    # Fix: Set _skip_slot_acquire=True so engine.run() skips slot acquisition.
                                    # Concurrency is controlled by app.security_check_semaphore (Semaphore(1)) instead,
                                    # which is a standard cross-thread synchronization primitive.
                                    
                                    # Get caller name from session for logging/debugging
                                    caller_name_sec = session.get('session_name', 'Orchestrator')
                                    caller_inst_sec = agent_pool.get_instance(caller_name_sec) if caller_name_sec else None
                                    
                                    # Log warning if caller_name couldn't be resolved properly (consistent with Compressor pattern)
                                    if caller_name_sec == 'Orchestrator' and not hasattr(agent_pool, 'session_name'):
                                        logger.warning(
                                            f"[SECURITY] Using fallback caller_name='Orchestrator' - "
                                            f"slot management may not work correctly. Pass caller_name explicitly."
                                        )
                                    
                                    # Set skip flag so engine.run() bypasses slot acquisition
                                    sec_instance._skip_slot_acquire = True
                                    logger.debug(
                                        f"[SECURITY_SLOT_BYPASS] Skipping slot acquire for Security - "
                                        f"caller={caller_name_sec}, caller_holds_slot={(getattr(caller_inst_sec, '_slot_release', None) is not None) if caller_inst_sec else False}"
                                    )

                                    # Acquire concurrency semaphore for Security checks (prevents unlimited parallelism)
                                    app.security_check_semaphore.acquire()
                                    try:
                                        # Log instance state before running (debug level to reduce log noise)
                                        # override_disabled = getattr(sec_instance, '_generate_cfg_override', {}).get('disabled_tools', 'NOT SET')
                                        # logger.debug(f"[SECURITY] Before engine.run - sec_instance._generate_cfg_override['disabled_tools']={override_disabled}")
                                        
                                        # Execute via engine.run() — this handles LLM call, retries, and streaming
                                        _last_sec_send = 0.0
                                        _sec_tick_num = 0
                                        _sec_last_resp_len = 0
                                        for resp in engine.run(sec_instance):
                                            # Check for pool shutdown / generation change
                                            if agent_pool.stopped:
                                                break

                                            elapsed = time.monotonic() - sec_start_time
                                            if elapsed > SECURITY_ADVISOR_TIMEOUT_SECONDS:
                                                sec_timeout_reached = True
                                                sec_elapsed_at_timeout = elapsed
                                                logger.warning(
                                                    f"[SECURITY] Timeout reached after {elapsed:.0f}s for request {rid}. "
                                                    f"Terminating security advisor to prevent AFK rejection."
                                                )
                                                break

                                            now_sec = time.monotonic()

                                            # Unpack (turn_output, is_streaming_tick) from engine.run() yield
                                            if isinstance(resp, tuple) and len(resp) == 2:
                                                sec_turn_output, sec_is_streaming_tick = resp
                                            else:
                                                sec_turn_output, sec_is_streaming_tick = resp, False

                                            # ── WebSocket broadcast for Security agent (shared helper) ──
                                            _last_sec_send, _sec_last_resp_len = broadcast_stream_update(
                                                pool=agent_pool,
                                                instance_name=sec_state_key,
                                                turn_output=sec_turn_output,
                                                is_streaming_tick=sec_is_streaming_tick,
                                                tick_num=_sec_tick_num,
                                                now_sec=now_sec,
                                                last_send=_last_sec_send,
                                                last_resp_len=_sec_last_resp_len,
                                            )

                                            _sec_tick_num += 1

                                            # Update instance_state for UI visibility (thread-safe)
                                            with agent_pool._execution._state_lock:
                                                if sec_state_key in agent_pool.instance_state:
                                                    agent_pool.instance_state[sec_state_key]['message_count'] = len(sec_instance.conversation)

                                    except Exception as e:
                                        logger.error(f"Security agent execution error: {e}")
                                        raise
                                    finally:
                                        # Release concurrency semaphore for Security checks
                                        app.security_check_semaphore.release()
                                        
                                        # Cancel the warning timer (keep existing cleanup)
                                        sec_warning_timer.cancel()

                                        # Note: engine.run() handles IDLE state transition internally.

                                    # Extract output using helper function
                                    from agent_cascade.compression.helpers import extract_instance_output
                                    parsing_response = extract_instance_output(sec_instance.conversation, sec_state_key)
                                        
                                    # Clean up: Remove thinking blocks before [YES]/[NO] parsing
                                    clean_text = parsing_response
                                    try:
                                        if '<think' in clean_text.lower() or '<thought' in clean_text.lower():
                                            clean_text = _THINK_BLOCK_RE.sub('', clean_text)
                                        if '[think' in clean_text.lower() or '[thought' in clean_text.lower():
                                            clean_text = _THINK_BLOCK_BRACKET_RE.sub('', clean_text).strip()
                                    except Exception as e:
                                        logger.debug(f"Thinking block stripping failed (non-critical): {e}")
                                        
                                    # Initialize verdict defaults before parsing
                                    is_yes = False
                                    is_no = False
                                    justification = ""
                                    try:
                                        
                                        # 2. Simplified Verdict Extraction: Check ONLY the last non-empty line
                                        lines = [l.strip() for l in clean_text.split('\n') if l.strip()]
                                        last_line = lines[-1] if lines else ""
                                        
                                        # Remove markdown bolding if present (e.g. **[YES]**)
                                        last_line_clean = _MARKDOWN_BOLD_RE.sub('', last_line).strip()
                                        last_line_upper = last_line_clean.upper()
                                        
                                        is_yes = last_line_upper.startswith('[YES]')
                                        is_no = last_line_upper.startswith('[NO]')
                                        
                                        justification = ""
                                        if is_yes:
                                            justification = last_line_clean[5:].strip()
                                        elif is_no:
                                            justification = last_line_clean[4:].strip()
                                            
                                        if is_yes or is_no:
                                            # Strip "Reason:", "Justification:", etc.
                                            justification = _JUSTIFICATION_PREFIX_RE.sub('', justification).strip()
                                        
                                        # Fallback 1: if no [YES]/[NO] on last line, check if the entire response is JUST the verdict
                                        if not is_yes and not is_no and len(lines) == 1:
                                            if last_line_upper == 'YES' or last_line_upper == 'SAFE':
                                                is_yes = True
                                                justification = last_line
                                            elif last_line_upper == 'NO' or last_line_upper == 'UNSAFE':
                                                is_no = True
                                                justification = last_line
                                        
                                        # Fallback 2: LLM may add text after verdict — find whichever [YES]/[NO] appears LAST
                                        if not is_yes and not is_no:
                                            upper_text = clean_text.upper()
                                            yes_pos = upper_text.rfind('[YES]')
                                            no_pos = upper_text.rfind('[NO]')
                                            if yes_pos > no_pos:
                                                is_yes = True
                                            elif no_pos > yes_pos:
                                                is_no = True
                                            if is_yes or is_no:
                                                # Extract justification from the matching line
                                                for line in lines:
                                                    lc = _MARKDOWN_BOLD_RE.sub('', line).strip().upper()
                                                    if (is_yes and '[YES]' in lc) or (is_no and '[NO]' in lc):
                                                        just_text = lc.replace('[YES]', '', 1).replace('[NO]', '', 1).strip()
                                                        justification = _JUSTIFICATION_PREFIX_RE.sub('', just_text).strip()
                                                        break
                                    except Exception as e:
                                        logger.error(f"Error extracting security verdict from {sec_state_key}: {e}")
                                        is_yes = False
                                        is_no = False
                                        justification = ""
                                    
                                    # ── Handle security advisor timeout ──
                                    if sec_timeout_reached:
                                        elapsed = sec_elapsed_at_timeout  # Guaranteed non-None since timeout was just hit
                                        logger.info(f"[SECURITY] Timeout after {elapsed:.0f}s for request {rid}. Auto-rejecting to prevent AFK rejection cascade.")
                                        
                                        # Halt the security advisor instance to stop it cleanly.
                                        # Note: This is best-effort — only works between turns, not during active LLM calls.
                                        # The actual timeout enforcement happens inside the for loop via `break` + generator.close().
                                        # FIX 1: Use unique sec_state_key instead of hardcoded 'Security'
                                        if sec_state_key:
                                            agent_pool.halt_instance(sec_state_key)
                                        
                                        # Common timeout handling for BOTH modes: reject and notify UI
                                        reject_msg = (
                                            "SECURITY ADVISOR TIMEOUT: The security check took too long to complete. "
                                            "This may indicate an overly complex request or insufficient justification. "
                                            "Please resubmit the request with a clearer, more specific justification "
                                            "to help the security advisor reach a verdict faster."
                                        )
                                        agent_pool.operation_manager.user_reject(rid, reject_msg)

                                        # Build response text — manual mode adds extra guidance for UI display
                                        response_text = f"[TIMEOUT] Security check exceeded {SECURITY_ADVISOR_TIMEOUT_SECONDS}s limit after {elapsed:.0f}s."
                                        if not auto_apply:
                                            response_text += " Please resubmit with clearer justification if needed."

                                        # Notify UI about the timeout
                                        asyncio.run_coroutine_threadsafe(
                                            send_queue.put({
                                                'type': 'security_response',
                                                'request_id': rid,
                                                'response': response_text,
                                                'verdict': 'TIMEOUT'
                                            }),
                                            loop
                                        )

                                        # Broadcast updated approval list so stale card is removed from frontend after timeout rejection
                                        asyncio.run_coroutine_threadsafe(
                                            send_queue.put({
                                                'type': 'approvals',
                                                'approvals': agent_pool.operation_manager.list_pending_approvals()
                                            }),
                                            loop
                                        )

                                    elif is_yes or is_no:
                                        # Check if Auto-Ask is still enabled BEFORE auto-applying
                                        auto_ask_still_on = getattr(app, 'current_auto_security', True)
                                        
                                        if auto_apply and auto_ask_still_on:
                                            if is_yes:
                                                logger.info(f"[SECURITY] Automatic Approval for {rid} with justification: {justification[:50]}...")
                                                agent_pool.operation_manager.user_approve(rid, reason=justification)
                                            else:
                                                logger.info(f"[SECURITY] Automatic Rejection for {rid} with reason: {justification[:50]}...")
                                                # Auto-rejection message
                                                reject_msg = f"SECURITY REJECTED: {justification}" if justification else "SECURITY REJECTED: The security advisor flagged this operation as unsafe."
                                                agent_pool.operation_manager.user_reject(rid, reject_msg)
                                            
                                            # Broadcast updated approvals list to UI after auto-apply
                                            asyncio.run_coroutine_threadsafe(
                                                send_queue.put({
                                                    'type': 'approvals',
                                                    'approvals': agent_pool.operation_manager.list_pending_approvals()
                                                }),
                                                loop
                                            )
                                        else:
                                            # Auto-Ask toggled off — send to UI for manual confirmation instead
                                            asyncio.run_coroutine_threadsafe(
                                                send_queue.put({
                                                    'type': 'security_response', 
                                                    'request_id': rid, 
                                                    'response': parsing_response,
                                                    'verdict': 'YES' if is_yes else 'NO',
                                                    'reason': justification if is_no else ""
                                                }),
                                                loop
                                                )
                                    else:
                                        if auto_apply:
                                            # Strict enforcement: Invalid format = Automatic NO (Safety)
                                            logger.info(f"[SECURITY] Automatic Rejection for {rid} (Ambiguous/Invalid Format)")
                                            reject_msg = f"SECURITY VERIFICATION FAILED: The security advisor provided an ambiguous response without a clear [YES] or [NO] verdict. For safety, the operation has been automatically rejected. Please try a different method or provide a clearer justification."
                                            agent_pool.operation_manager.user_reject(rid, reject_msg)
                                            
                                            # Also notify the UI
                                            asyncio.run_coroutine_threadsafe(
                                                send_queue.put({'type': 'security_response', 'request_id': rid, 'response': parsing_response + f"\n\n**[AUTO-REJECTED: Ambiguous Format]**", 'verdict': 'AMBIGUOUS'}),
                                                loop
                                            )
                                        else:
                                            # Manual mode: Let the user see the ambiguous response and decide
                                            logger.info(f"[SECURITY] Ambiguous response for {rid} in manual mode. Waiting for user decision.")
                                            asyncio.run_coroutine_threadsafe(
                                                send_queue.put({'type': 'security_response', 'request_id': rid, 'response': parsing_response, 'verdict': 'AMBIGUOUS'}),
                                                loop
                                            )
                                except Exception as e:
                                    logger.error(f"Security check failed: {e}")
                                    if auto_apply:
                                        agent_pool.operation_manager.user_reject(rid, f"Security check error: {e}")
                                    else:
                                        asyncio.run_coroutine_threadsafe(
                                            send_queue.put({'type': 'security_response', 'request_id': rid, 'response': f"Error during security check: {e}"}),
                                            loop
                                        )
                                finally:
                                    # Always clean up security advisor state when done (thread-safe)
                                    if sec_state_key and sec_state_key in agent_pool.instance_state:
                                        with agent_pool._execution._state_lock:
                                            agent_pool.instance_state[sec_state_key]['active'] = False
                                        try:
                                            agent_pool.active_stack_remove(sec_state_key)
                                        except Exception as e:
                                            logger.debug(f"Active stack removal failed for {sec_state_key} (non-critical): {e}")

                                    # Fix #3: Release endpoint slot when done
                                    if hasattr(app, 'active_security_checks') and rid:
                                        with app.active_security_checks_lock:
                                            app.active_security_checks.discard(rid)
                                        logger.debug(f"[SECURITY] Released active check for {rid}")
                            
                            threading.Thread(target=_security_check, daemon=True).start()

                elif msg_type == 'set_auto_security':
                    # User toggled Auto-Ask on/off — store current state for security checks to reference
                    enabled = data.get('enabled', False)
                    app.current_auto_security = enabled

                elif msg_type == 'edit_message':
                    idx = data.get('index')
                    content = data.get('content', '')
                    target_name = data.get('instance_name') or session['session_name']
                    
                    # Get the conversation from pool instance (unified path)
                    history = []
                    if agent_pool:
                        inst = agent_pool.get_instance(target_name)
                        if inst is not None:
                            with inst._compression_lock:
                                history = list(inst.conversation)  # Defensive copy under lock
                    
                    # Phase 6: No fallback to session['history'] — pool is the source of truth
                    
                    # Note: session_lock check removed (2026-06-16 simplification).
                    # Message edit doesn't start new threads, no race condition protection needed.
                    
                    if (idx is not None
                            and 0 <= idx < len(history)):
                        msg = history[idx]
                        
                        # Get current content regardless of message type (dict or Message object)
                        old_content = msg.get(CONTENT, "") if isinstance(msg, dict) else getattr(msg, 'content', "")
                        new_parsed_content = _parse_multimodal_content(content)
                        
                        # If this is a compression marker, ensure tags are preserved
                        is_compression_msg = str(old_content).startswith(COMPRESSION_MARKER)
                        if is_compression_msg:
                            if COMPRESSION_MARKER in content or "<context_summary>" in content:
                                new_parsed_content = content
                            else:
                                new_parsed_content = f"{COMPRESSION_MARKER}\n\n<context_summary>\n{content}\n</context_summary>"
                            
                            # Sync the summary tracker
                            match = _CONTEXT_SUMMARY_RE.search(new_parsed_content)
                            if match and agent_pool:
                                agent_pool.instance_summaries[target_name] = match.group(1).strip()
                        
                        # Apply edit — handle both dict and Message object types
                        if isinstance(msg, dict):
                            msg[CONTENT] = new_parsed_content
                            if '_ui_cache' in msg:
                                del msg['_ui_cache']
                        elif hasattr(msg, 'content'):
                            # Pydantic Message object or dataclass — set content directly
                            msg.content = new_parsed_content
                        
                        if agent_pool:
                            # Write back the edited history using centralized API (PR3 migration)
                            inst = agent_pool.get_instance(target_name)
                            if inst is not None:
                                inst.rebuild_conversation(history)  # PR3: centralized API handles full rebuild with cache sync
                            
                            logger_inst = agent_pool.get_logger(target_name, 'Orchestrator' if target_name == session['session_name'] else 'SubAgent')
                            logger_inst.reset_history(history, rewrite=True)
                            
                            # Sync instance_state so build_state() sees the edit
                            if target_name != session['session_name'] and target_name in agent_pool.instance_state:
                                agent_pool.instance_state[target_name]['messages'] = list(history)
                    
                    # Fix #1 & #3: Clear performance caches on message edit
                    try:
                        from agent_cascade.api_integration import _clear_performance_caches
                        _clear_performance_caches()
                    except Exception as e:
                        logger.debug(f"Cache clearing failed during message edit (non-critical): {e}")
                    
                    await broadcast({'type': 'state', **build_state()})
                            
                elif msg_type == 'delete_messages':
                    # Note: session_lock check removed (2026-06-16 simplification).
                    # Message delete doesn't start new threads, no race condition protection needed.
                    
                    target_name = data.get('instance_name') or session['session_name']
                    
                    # Get the conversation from pool instance (unified path)
                    history = []
                    if agent_pool:
                        inst = agent_pool.get_instance(target_name)
                        if inst is not None:
                            with inst._compression_lock:
                                history = list(inst.conversation)  # Defensive copy under lock
                    
                    # Phase 6: No fallback to session['history'] — pool is the source of truth
                    
                    indices = sorted(data.get('indices', []), reverse=True)
                    for idx in indices:
                        if 0 <= idx < len(history):
                            history.pop(idx)
                    if agent_pool:
                        # Write back the pruned history using centralized API (PR3 migration)
                        inst = agent_pool.get_instance(target_name)
                        if inst is not None:
                            inst.rebuild_conversation(history)  # PR3: centralized API handles full rebuild with cache sync
                        
                        logger_inst = agent_pool.get_logger(target_name, 'Orchestrator' if target_name == session['session_name'] else 'SubAgent')
                        logger_inst.reset_history(history, rewrite=True)
                        
                        # Sync instance_state so build_state() sees the deletion
                        if target_name != session['session_name'] and target_name in agent_pool.instance_state:
                            agent_pool.instance_state[target_name]['messages'] = list(history)
                    
                    # Fix #1 & #3: Clear performance caches on message deletion
                    try:
                        from agent_cascade.api_integration import _clear_performance_caches
                        _clear_performance_caches()
                    except Exception as e:
                        logger.debug(f"Cache clearing failed during message deletion (non-critical): {e}")
                    
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'select_agent':
                    session['agent_index'] = int(data.get('index', 0))
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'set_session_name':
                    old_name = session['session_name']
                    new_name = data.get('name', 'Maine')
                    session['session_name'] = new_name
                    if agent_pool:
                        # Migrate instance_summaries to new name in pool
                        if old_name in agent_pool.instance_summaries:
                            agent_pool.instance_summaries[new_name] = agent_pool.instance_summaries.pop(old_name)
                    
                    await broadcast({'type': 'state', **build_state()})



                elif msg_type == 'load_session':
                    path = data.get('path')
                    if path and agent_pool:
                        # Issue 4 fix: Wrap pool modifications with session_lock for thread safety
                        with session_lock:
                            # Issue 1 fix: Use clear_sub_agents_before_load parameter instead of separate call
                            status = agent_pool.load_session_from_log(
                                path, 
                                target_instance=session.get('session_name'),
                                clear_sub_agents_before_load=True
                            )
                        if status.startswith("Error"):
                            await websocket.send_text(json.dumps({"type": "error", "message": status}, ensure_ascii=False))
                        else:
                            # Phase 6: Pool is already loaded by load_session_from_log — no need to sync to session
                            instance_name = session.get('session_name', 'Maine')
                            inst = agent_pool.get_instance(instance_name)
                            if inst is not None:
                                # Fix #3 (Feature 020): Wrap session state modifications with session_lock to prevent race condition
                                _stop_generation(session)
                                _signal_stop(session)
                                if agent_pool:
                                    agent_pool.stopped = False
                                # Fix #1 & #3: Clear performance caches on session load
                                try:
                                    from agent_cascade.api_integration import _clear_performance_caches
                                    _clear_performance_caches()
                                except Exception as e:
                                    logger.debug(f"Cache clearing failed during session load (non-critical): {e}")
                                await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'inject':
                    text = data.get('text', '').strip()
                    target = data.get('target_agent') or session.get('session_name', 'Maine')
                    if text and agent_pool:
                        agent_pool.enqueue_message(target, text)

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"WebSocket handler error for client: {e}")
            traceback.print_exc()
        finally:
            ws_connections.discard(websocket)

    # ── Serve frontend static files ───────────────────────────────────────
    # api_server.py is inside agent_cascade/, but web_ui/ is at the project root — go one level up
    web_ui_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'web_ui'))

    # Validate that the web UI directory actually exists before serving static files
    if not os.path.isdir(web_ui_dir):
        logger.warning("Web UI directory does not exist: %s — static file serving will fail", web_ui_dir)

    @app.post("/api/find_file")
    async def find_file(request: Request):
        try:
            data = await request.json()
            filename = data.get('filename')
            if not filename:
                return JSONResponse(status_code=400, content={"message": "Filename required"})
                
            base_dir = Path(DEFAULT_WORKSPACE)
            if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                base_dir = agent_pool.operation_manager.base_dir
                
            matches = []
            
            # Simple recursive search, ignoring common large/irrelevant directories
            ignore_dirs = {'.git', 'node_modules', '__pycache__', '.pytest_cache', 'venv', 'env', '.env'}
            
            def search_dir(current_dir):
                try:
                    for item in current_dir.iterdir():
                        if item.is_dir() and item.name not in ignore_dirs:
                            search_dir(item)
                        elif item.is_file() and item.name == filename:
                            matches.append(str(item.absolute()))
                except PermissionError as e:
                    logger.debug(f"Permission denied searching directory (skipping): {e}")
                    
            search_dir(base_dir)
            
            return {"matches": matches}
        except Exception as e:
            logger.error(f"Failed to find file: {e}")
            return JSONResponse(status_code=500, content={"message": str(e)})

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(web_ui_dir, 'index.html'))

    @app.get("/{path:path}")
    async def serve_static(path: str):
        file_path = os.path.normpath(os.path.join(web_ui_dir, path))
        # Path traversal protection: ensure resolved path is still within web_ui_dir
        if not (file_path.startswith(web_ui_dir + os.sep) or file_path == web_ui_dir):
            return JSONResponse(status_code=403, content={"message": "Forbidden"})
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        # SPA fallback
        return FileResponse(os.path.join(web_ui_dir, 'index.html'))

    @app.post("/api/parse")
    async def parse_document(file: UploadFile = File(...)):
        try:
            from agent_cascade.tools.simple_doc_parser import SimpleDocParser
            import shutil
            import tempfile

            # Create a temporary directory inside the workspace
            temp_dir = Path(DEFAULT_WORKSPACE) / 'temp_uploads'
            temp_dir.mkdir(exist_ok=True)
            
            file_path = temp_dir / file.filename
            with file_path.open("wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            doc_extractor = SimpleDocParser({'structured_doc': False, 'work_dir': str(DEFAULT_WORKSPACE)})
            text = doc_extractor.call({'url': str(file_path)})
            
            # Clean up the temp file
            try:
                file_path.unlink()
            except Exception as cleanup_err:
                logger.warning(f"Failed to clean up temp file {file_path}: {cleanup_err}")

            return {"text": text, "filename": file.filename}
        except Exception as e:
            logger.error(f"Failed to parse document: {e}")
            return JSONResponse(status_code=500, content={"message": str(e)})

    return app


if __name__ == "__main__":
    import uvicorn
    from agent_cascade.agent_pool import AgentPool

    # Resolve project root: api_server.py lives inside agent_cascade/, so go up one level
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

    parser = argparse.ArgumentParser(description="AgentCascade API Server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=12345, help="Port to bind to")
    parser.add_argument("--workspace", type=str, default=str(DEFAULT_WORKSPACE), help="Workspace directory")
    parser.add_argument("--idle-timeout", type=float, default=None,
                        help="Seconds of inactivity before auto-dismissing an idle agent (default: 300). "
                             "Also settable via QWEN_AGENT_IDLE_TIMEOUT env var.")
    parser.add_argument("--idle-check-interval", type=float, default=None,
                        help="Seconds between idle-check sweeps (default: 60). "
                             "Also settable via QWEN_AGENT_IDLE_CHECK_INTERVAL env var.")
    args = parser.parse_args()

    # Initialize the global agent_pool
    initial_llm_cfg = {
        'model': os.getenv('QWEN_AGENT_MODEL', 'gpt-4o'),
        'api_base': os.getenv('QWEN_AGENT_API_BASE', 'https://api.openai.com/v1'),
        'api_key': os.getenv('QWEN_AGENT_API_KEY', 'EMPTY'),
    }

    # Resolve idle timeout settings: CLI > env var > default
    idle_timeout = args.idle_timeout if args.idle_timeout is not None else float(os.getenv('QWEN_AGENT_IDLE_TIMEOUT', 300.0))
    idle_check_interval = args.idle_check_interval if args.idle_check_interval is not None else float(os.getenv('QWEN_AGENT_IDLE_CHECK_INTERVAL', 60.0))

    # Create OperationManager for blocking user approvals on mutating operations
    from agent_cascade.operation_manager import OperationManager
    try:
        operation_mgr = OperationManager(base_dir=args.workspace)
        logger.debug("OperationManager initialized with base_dir: %s", args.workspace)
    except Exception as e:
        logger.error("[FATAL] OperationManager initialization failed: %s", e)
        raise SystemExit(1)

    try:
        agent_pool = AgentPool(
            llm_cfg=initial_llm_cfg,
            agents_dir=str(PROJECT_ROOT / 'agents'),
            workspace_dir=args.workspace,
            operation_manager=operation_mgr,
        )
        logger.debug("AgentPool created successfully")
    except Exception as e:
        logger.error("[FATAL] AgentPool creation failed: %s", e)
        raise SystemExit(1)

    operation_mgr.agent_pool = agent_pool

    # Set idle timeout settings via PoolSettings (new pool uses PoolSettings instead of constructor args)
    agent_pool.settings.idle_timeout_seconds = idle_timeout
    agent_pool.settings.idle_check_interval = idle_check_interval

    # Create the root orchestrator instance in the new pool (use lowercase to match template key)
    try:
        agent_pool.create_instance('Maine', 'orchestrator')
        logger.debug("Orchestrator instance 'Maine' created")
    except Exception as e:
        logger.error("[FATAL] Failed to create orchestrator instance: %s", e)
        raise SystemExit(1)

    # Start background services (idle checker thread, etc.)
    try:
        agent_pool.start()
        logger.debug("AgentPool background services started")
    except Exception as e:
        logger.error("[FATAL] Failed to start AgentPool background services: %s", e)
        raise SystemExit(1)

    # Get the orchestrator agent template for create_app (new pool separates instances from templates)
    orch_agent = agent_pool.get_agent('orchestrator')

    # Inject system message into Maine's conversation so the soul content flows into the instance
    # Priority: base_system_message > system_message (matches the priority chain in run_agent_thread)
    sys_msg_content = None
    if hasattr(orch_agent, 'base_system_message') and orch_agent.base_system_message:
        sys_msg_content = str(orch_agent.base_system_message)
    elif hasattr(orch_agent, 'system_message') and orch_agent.system_message:
        sys_msg_content = str(orch_agent.system_message)

    if sys_msg_content:
        from agent_cascade.llm.schema import Message, SYSTEM
        maine_inst = agent_pool.get_instance('Maine')
        if maine_inst and not maine_inst.conversation:
            # PR3 migration: Use centralized API for system message append at server startup
            maine_inst.append_messages([Message(role=SYSTEM, content=sys_msg_content)])

    try:
        app = create_app(agents=[orch_agent], agent_pool=agent_pool)
        logger.debug("FastAPI app created successfully")
    except Exception as e:
        logger.error("[FATAL] Failed to create API server app: %s", e)
        raise SystemExit(1)

    port = args.port
    logger.info("\n[OK] API Server ready!")
    logger.info("    -> Open http://127.0.0.1:%d in your browser", port)
    logger.info("    -> WebSocket at ws://127.0.0.1:%d/ws/chat", port)
    logger.info("    -> REST API at http://127.0.0.1:%d/api/", port)
    logger.info("=" * 50)

    # Set up graceful shutdown handler
    def handle_shutdown(signum, frame):
        logger.info("\n[INFO] Initiating graceful shutdown...")
        agent_pool.stopped = True
        if hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
            try:
                agent_pool.operation_manager.cleanup_backups()
            except Exception as e:
                logger.debug(f"Backup cleanup failed during shutdown (non-critical): {e}")
        logger.info("[INFO] Terminated.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        uvicorn.run(app, host=args.host, port=port)
    except OSError as e:
        if e.errno == 98 or 'address already in use' in str(e).lower():
            logger.error("[FATAL] Port %d is already in use. Use --port to specify a different port.", port)
        else:
            logger.error("[FATAL] Server failed to start: %s", e)
        raise SystemExit(1)
    except Exception as e:
        logger.error("[FATAL] Server crashed: %s", e)
        raise SystemExit(1)
