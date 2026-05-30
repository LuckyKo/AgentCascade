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

import asyncio
import copy
import glob
import json
import os
import re
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
from agent_cascade.settings import DEFAULT_WORKSPACE, DEFAULT_MAX_INPUT_TOKENS
from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
from agent_cascade.utils.utils import extract_text_from_message, get_message_stats, get_history_stats, IMAGE_REGEX
from agent_cascade.prompts.dna import SECURITY_ADVISOR_PROMPT, COMPRESSION_MARKER
from agent_cascade.llm.base import _truncate_input_messages_roughly

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

# Pre-compiled regexes moved to agent_cascade.utils.thinking_block


def _get_main_history(agent_pool, session_name):
    """Get the main agent's conversation history from the pool.
    
    Replaces reads of session['history']. Returns a defensive copy of the
    conversation list from the pool instance, or an empty list if not found.
    
    Args:
        agent_pool: The AgentPool instance managing all instances.
        session_name: Name of the main session/instance
        
    Returns:
        list of message objects
    """
    if not agent_pool:
        return []
    inst = agent_pool.get_instance(session_name)
    if inst is not None:
        with inst._compression_lock:
            return list(inst.conversation)
    return []


def _get_msg_role(msg):
    """Extract the 'role' field from a message, handling both dict and Message object types."""
    if isinstance(msg, dict):
        return msg.get(ROLE)
    return getattr(msg, 'role', None)


def _get_msg_func_call(msg):
    """Extract the 'function_call' field from a message, handling both dict and Message object types."""
    if isinstance(msg, dict):
        return msg.get('function_call')
    return getattr(msg, 'function_call', None)


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
    










def detect_loop(messages: List[dict]) -> Optional[Tuple[str, int]]:
    """
    Detect if the agent is stuck in a loop.
    Returns (reason, pop_count) if found, else None.
    """
    if len(messages) < 6:
        return None
    
    # Extract identifying features, ignoring SYSTEM messages
    def get_feature(m):
        if hasattr(m, 'model_dump'):
            m = m.model_dump()
        elif not isinstance(m, dict):
            # Fallback for other objects
            m = {
                ROLE: getattr(m, 'role', ''),
                CONTENT: getattr(m, 'content', ''),
                'reasoning_content': getattr(m, 'reasoning_content', getattr(m, 'thought', '')),
                'function_call': getattr(m, 'function_call', None)
            }
        role = m.get(ROLE)
        content = str(m.get(CONTENT, ''))
        reasoning = str(m.get('reasoning_content', '') or m.get('thought', ''))
        
        # Combine reasoning and content for better loop detection
        if reasoning and not content.startswith('<think>'):
            text_feature = f"{reasoning}\n{content}"
        else:
            text_feature = content or reasoning
            
        text_feature = _TOOL_TRUNCATED_RE.sub('[TOOL RESPONSE TRUNCATED]', text_feature)
            
        fc = m.get('function_call')
        if fc:
            # Handle both dict and object function calls
            name = fc.get('name') if isinstance(fc, dict) else getattr(fc, 'name', '')
            args = fc.get('arguments') if isinstance(fc, dict) else getattr(fc, 'arguments', '')
            return f"{role}:{name}:{args}"
        # For plain messages, use first 3000 chars of content to distinguish long reasoning
        return f"{role}:{text_feature[:3000]}"

    # Only look at the last 40 messages to detect recent loops
    window = messages[-40:]
    features = []
    feature_to_window_idx = []
    for i, m in enumerate(window):
        if m.get(ROLE) != SYSTEM:
            features.append(get_feature(m))
            feature_to_window_idx.append(i)
    
    if len(features) < 4:
        return None

    # Generic loop detection for pattern length L repeating K times
    for L in range(1, 21):
        K = 3 if L < 5 else 2
        
        if len(features) < L * K:
            continue
            
        for i in range(len(features) - (L * K), -1, -1):
            pattern = features[i : i + L]
            is_loop = True
            for k in range(1, K):
                if features[i + k * L : i + (k + 1) * L] != pattern:
                    is_loop = False
                    break
            if is_loop:
                if features[-L:] == pattern:
                    roles = [p.split(':')[0] for p in pattern]

                    # Skip False Positives: 
                    # L=1 pattern of FUNCTION or USER role is usually parallel tool responses
                    # or consecutive user inputs, which are NOT agent loops.
                    if L == 1 and roles[0] in (FUNCTION, USER):
                        continue

                    # Calculate how many messages to pop from the end of 'messages'.
                    # Only remove the DUPLICATE repetitions (K-1 copies), not the first occurrence.
                    # feature_to_window_idx[i + L] is the window-level start of the 2nd repetition.
                    second_rep_feature_idx = i + L
                    second_rep_window_idx = feature_to_window_idx[second_rep_feature_idx]
                    # Convert window index to a pop-count from the end of 'messages'
                    pop_count = len(window) - second_rep_window_idx
                    reason = f"Detected repeated sequence loop ({', '.join(roles)} repeating {K} times)"
                    return reason, pop_count
            
    return None


def _refine_pop_count(history, pop_count):
    """Adjust pop_count to ensure we don't leave a dangling tool call."""
    new_pop = pop_count
    
    # Safety: Determine core messages (SYSTEM + first USER) that must NEVER be removed
    keep_at_least = 0
    if len(history) > 0 and _get_msg_role(history[0]) == SYSTEM:
        keep_at_least = 1
        if len(history) > 1 and _get_msg_role(history[1]) == USER:
            keep_at_least = 2
            
    removable = len(history) - keep_at_least
    if removable <= 0:
        return 0
        
    # Cap pop_count at removable length
    if new_pop > removable:
        new_pop = removable
        
    while new_pop < removable:
        start_idx = len(history) - new_pop
        if start_idx >= keep_at_least and _get_msg_role(history[start_idx]) == FUNCTION:
            # If we land on a function response, we must also remove the call that preceded it.
            new_pop += 1
        elif start_idx >= keep_at_least and _get_msg_role(history[start_idx]) == ASSISTANT and _get_msg_func_call(history[start_idx]):
            # If we land on a tool call, it means we've already included it (or it's the start of our rollback).
            # We stop here to keep the history clean without rolling back into the previous successful turn.
            break
        else:
            # Landed on a regular message (USER or text ASSISTANT). 
            # This is a safe boundary to stop.
            break
    return new_pop


# ─── Message serialization ────────────────────────────────────────────────────

def serialize_message(msg, index=None):
    """Convert a Message object or dict to a JSON-serializable dict with caching."""
    # Use cache if available to avoid expensive re-serialization of large history messages
    if isinstance(msg, dict) and '_ui_cache' in msg:
        res = dict(msg['_ui_cache'])
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

    # Normalize content to string
    content = d.get('content', '')
    if isinstance(content, list):
        parts = []
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
    
    # UI Performance: Truncate exceptionally large content at the wire level.
    # The full content is still preserved in the backend 'history' and persistent logs.
    if isinstance(content, str) and len(content) > 100000:
        content = content[:100000] + "\n\n... [TRUNCATED IN UI FOR PERFORMANCE. Full content is available in the session logs.]"
    
    d['content'] = content or ''

    # Normalize function_call
    fc = d.get('function_call')
    if fc:
        if hasattr(fc, 'name'):
            d['function_call'] = {'name': fc.name, 'arguments': fc.arguments}
        # else: already a dict, keep it
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
    d.pop('_ui_cache', None)
    
    # Extract tool_success from extra before stripping — frontend needs it for isToolFailure()
    if 'extra' in d and isinstance(d['extra'], dict):
        ts = d['extra'].get('tool_success')
        if ts is not None:
            d['tool_success'] = bool(ts)
    
    d.pop('extra', None)
    
    # UI Performance: Store in cache if the input is a persistent history dict.
    # CRITICAL: We DO NOT cache if it's the very last message in the list,
    # as the orchestrator often mutates the latest turn's messages (merging reasoning, 
    # async injections, etc.) and we don't want the UI to "hang" on a stale version.
    if isinstance(msg, dict) and index is not None and index > 0:
        msg['_ui_cache'] = dict(d)

    if index is not None:
        d['index'] = index

    return d


# Global caches for UI performance

# ── Logging Setup ─────────────────────────────────────────────────────────────

# ─── App factory ──────────────────────────────────────────────────────────────

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
        build_state_from_pool,
        build_stream_update_from_pool,
        create_main_agent_instance,
        _apply_ui_config,
        _build_agents_list,
        _get_approvals,
    )

    # ── Helpers ───────────────────────────────────────────────────────────
    def get_agent_max_tokens(agent) -> int:
        """Resolve the effective max_input_tokens from agent LLM config."""
        # 1. Try API Router if available
        if agent_pool and hasattr(agent_pool, 'api_router') and agent_pool.api_router:
            agent_type = getattr(agent, 'agent_type', 'orchestrator').lower()
            router_limit = agent_pool.api_router.get_effective_max_tokens(agent_type)
            if router_limit > 0:
                return router_limit

        # 2. Try the agent instance logic
        if hasattr(agent, 'llm'):
            if hasattr(agent.llm, 'generate_cfg'):
                agent_max = agent.llm.generate_cfg.get('max_input_tokens')
                if agent_max:
                    return int(agent_max)
            if hasattr(agent.llm, 'cfg'):
                cfg = agent.llm.cfg
                agent_max = cfg.get('generate_cfg', {}).get('max_input_tokens') or cfg.get('max_input_tokens')
                if agent_max:
                    return int(agent_max)
        
        # 3. Fallback to pool/global settings
        if agent_pool:
            llm_cfg = getattr(agent_pool, 'llm_cfg', {})
            pool_max = llm_cfg.get('generate_cfg', {}).get('max_input_tokens') or llm_cfg.get('max_input_tokens')
            if pool_max:
                return int(pool_max)

        return DEFAULT_MAX_INPUT_TOKENS

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
                history = list(agent_pool.instance_conversations.get(name, []))
            
            # Use the standardized logger to ensure append-only behavior
            logger_inst = agent_pool.get_logger(name, 'Orchestrator')
            logger_inst.update_history(history)
            
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

    def get_session_history(session, instance_name='root'):
        """Read session history from the pool (unified single source of truth).
        
        Phase 6: Always reads from agent_pool. Legacy fallback removed.
        
        Args:
            session: Flask session dict
            instance_name: Agent instance name ('root' for main chat, or agent name for agent instances)
        
        Returns:
            list of message objects
        """
        # Read from unified store — always read from pool.instances for live data.
        # For non-root instances, history comes from instance_state which is updated during streaming.
        if instance_name == 'root':
            inst = agent_pool.get_instance(session['session_name']) if agent_pool else None
            if inst is not None:
                with inst._compression_lock:
                    return list(inst.conversation)
            return []
        else:
            # Agent instance history from unified store (instance_state is updated during streaming)
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
    send_queue: asyncio.Queue = asyncio.Queue()

    # Lock for session state accessed across threads (asyncio loop + agent thread).
    # Protects: session['generating'], session['stop_requested'].
    # Phase 6: session['history'] removed — pool is the single source of truth.
    session_lock = threading.Lock()



    def get_agent():
        idx = session['agent_index']
        if 0 <= idx < len(agents):
            return agents[idx]
        return agents[0]

    def get_instance_state(streaming=False):
        result = {}
        if agent_pool and hasattr(agent_pool, 'instance_state'):
            for name, state in agent_pool.instance_state.items():
                msgs = state.get('messages', [])
                
                # Extract the actual agent class from agent_name.
                # agent_name is stored as "instance_name (AgentClass)" by the orchestrator,
                # e.g. "worker1 (Coder)". We need just "Coder" to look up the agent template.
                raw_agent_name = state.get('agent_name', name)
                if ' (' in raw_agent_name and raw_agent_name.endswith(')'):
                    agent_class = raw_agent_name.split(' (')[-1].rstrip(')')
                else:
                    agent_class = raw_agent_name
                
                # Get max tokens for this agent instance's own model/endpoint.
                # Try the agent template first, then fall back to querying the API Router
                # directly by agent type (handles cases where the template wasn't loaded).
                agent_template = agent_pool.get_agent(agent_class)
                if agent_template:
                    max_tokens = get_agent_max_tokens(agent_template)
                elif hasattr(agent_pool, 'api_router') and agent_pool.api_router:
                    # Query the router directly using the agent class as the type key
                    agent_type = agent_class.lower()
                    router_limit = agent_pool.api_router.get_effective_max_tokens(agent_type)
                    max_tokens = router_limit if router_limit > 0 else DEFAULT_MAX_INPUT_TOKENS
                else:
                    max_tokens = DEFAULT_MAX_INPUT_TOKENS
                
                # FIX1 (agent instance index mismatch): Always compute from the sliced/active set.
                # slice_history_for_llm can reduce the message count during compression,
                # so we track active_count (len of sliced history) instead of len(msgs).
                # This ensures all indexing into active_msgs is consistent.
                active_msgs = agent_pool.slice_history_for_llm(msgs) if agent_pool else msgs
                active_count = len(active_msgs)
                
                # Incremental token counting for agent instances.
                # During streaming, agent instance histories are mostly static — they only change
                # when a new message arrives from that agent instance (rare during main agent ticks).
                # NOTE: slice_history_for_llm only appends/removes messages (never mutates in-place),
                # so if active_count == last_active_count the cached stats are guaranteed valid.
                cache_key = '_sa_stats_' + name
                last_active_count = session.get(cache_key + '_count', -1)
                
                if active_count > last_active_count:
                    # New message(s) added — compute stats incrementally
                    if cache_key in session and last_active_count >= 0:
                        # Incremental update: only tokenize the new messages (from last_active_count onward)
                        cached_stats = session.get(cache_key, {'tokens': 0, 'words': 0})
                        new_msgs = active_msgs[last_active_count:] if len(active_msgs) > last_active_count else []
                        if new_msgs:
                            new_stats = get_history_stats(new_msgs)
                            stats = {
                                'tokens': cached_stats['tokens'] + new_stats['tokens'],
                                'words': cached_stats['words'] + new_stats['words']
                            }
                        else:
                            stats = cached_stats.copy()
                    else:
                        # First access or cache missing — compute full stats
                        stats = get_history_stats(active_msgs)
                    
                    session[cache_key + '_count'] = active_count
                    session[cache_key] = stats.copy()  # FIX: Store stats so next call can use the cache
                elif active_count < last_active_count:
                    # FIX2 (history shrank due to compression): Recompute from scratch.
                    # slice_history_for_llm may have dropped older messages after a context_summary,
                    # so the cached stats would overcount tokens if we reused them.
                    stats = get_history_stats(active_msgs)
                    session[cache_key + '_count'] = active_count
                    session[cache_key] = stats.copy()  # FIX: Store stats so next call can use the cache
                elif cache_key in session:
                    # Same active count AND cache exists — use cached stats (avoids tiktoken.encode per tick)
                    stats = session.get(cache_key, {'tokens': 0, 'words': 0}).copy()
                else:
                    # First access or cache missing — compute stats to populate the cache
                    stats = get_history_stats(active_msgs)
                    session[cache_key + '_count'] = active_count  # FIX: Also store count for next comparison
                    session[cache_key] = stats.copy()  # FIX: Store stats so next call can use the cache
                
                # Dynamically extract summary from messages if missing from tracker (e.g. after restart)
                summary = agent_pool.instance_summaries.get(name, "")
                if not summary:
                    for msg in reversed(msgs):
                        # Handle both dict and Message object types
                        if isinstance(msg, dict):
                            role = msg.get(ROLE)
                            content = msg.get(CONTENT, '')
                        else:
                            role = getattr(msg, 'role', None)
                            content = getattr(msg, 'content', '') or ''
                        # Specifically target USER messages for context boundaries
                        if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                            import re
                            # Use the standardized XML-style tag match
                            match = _CONTEXT_SUMMARY_RE.search(content)
                            if match:
                                summary = match.group(1).strip()
                                break
                
                # Optimization: During streaming, we only send the tail of the message list
                # to avoid O(N^2) JSON traffic and parsing lag in the browser.
                if streaming and state.get('active') and len(msgs) > 5:
                    start_idx = max(0, len(msgs) - 3)
                    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs[-3:], start_idx)]
                    is_partial = True
                else:
                    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]
                    is_partial = False
                
                result[name] = {
                    'active': state.get('active', False),
                    'agent_name': agent_class,
                    'messages': serialized_msgs,
                    'is_partial': is_partial,
                    'history_count': len(msgs),
                    'total_tokens': stats['tokens'],
                    'total_words': stats['words'],
                    'max_tokens': max_tokens,
                    'summary': summary,
                    'has_queued_messages': agent_pool.has_messages(name),
                    'is_waiting': agent_pool.api_router.is_waiting(name) if hasattr(agent_pool, 'api_router') else False,
                    'is_halted': agent_pool.is_halted(name) if hasattr(agent_pool, 'is_halted') else False,
                }
        return result

    def get_active_stack():
        if agent_pool and hasattr(agent_pool, 'active_stack'):
            return list(agent_pool.active_stack)
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
        gen = generating if generating is not None else session['generating']
        
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
            
            # Resolve max_tokens via API router (same logic as _get_max_tokens_for_instance)
            fallback_max_tokens = DEFAULT_MAX_INPUT_TOKENS
            if agent_pool and hasattr(agent_pool, 'api_router') and agent_pool.api_router:
                try:
                    router_limit = agent_pool.api_router.get_effective_max_tokens('orchestrator')
                    if router_limit > 0:
                        fallback_max_tokens = router_limit
                except Exception as e:
                    logger.debug(f"API router max_tokens lookup failed (using fallback): {e}")
            
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
            
            # Resolve max_tokens via API router (same as build_state fallback)
            stream_max_tokens = DEFAULT_MAX_INPUT_TOKENS
            if agent_pool and hasattr(agent_pool, 'api_router') and agent_pool.api_router:
                try:
                    rl = agent_pool.api_router.get_effective_max_tokens('orchestrator')
                    if rl > 0:
                        stream_max_tokens = rl
                except Exception as e:
                    logger.debug(f"API router max_tokens lookup failed in stream (using fallback): {e}")
            
            return {
                'history_count': len(_get_main_history(agent_pool, instance_name)),
                'response_messages': [serialize_message(m, i) for i, m in enumerate(responses or [])],
                'instances': {},
                'agent_instances': {},
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

    def run_agent_thread(history_for_agent, agent_runner, gen_id, loop):
        """
        Unified agent execution entry point. Delegates to run_agent_thread_unified
        which uses ExecutionEngine and pool.instances instead of session['history'].
        
        Signature preserved for existing call sites.
          - history_for_agent → used to extract system message content (can be None if instance exists in pool)
          - agent_runner → not used (engine is stateless, reads from pool)
          - gen_id → tracked in session for stop detection
          - loop → asyncio event loop for send_queue
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
            inst = agent_pool.get_instance(session['session_name'])
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
        instance_name = session['session_name']
        ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))

        # Delegate to the unified execution thread (handles everything internally)
        run_agent_thread_unified(
            pool=agent_pool,
            instance_name=instance_name,
            system_message_content=system_message_content,
            ui_cfg=ui_cfg,
            send_queue=send_queue,
            loop=loop,
        )


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
                Only triggers for LLM-initiated dismissals (during active generation cycle).
                UI-initiated dismissals have their own direct broadcast in terminate_agent_instance handler.
                """
                ws_loop = getattr(agent_pool, '_ws_loop', None)
                if ws_loop and not ws_loop.is_closed() and send_queue:
                    try:
                        msg = {'type': 'dismissal', 'instance_name': instance_name}
                        asyncio.run_coroutine_threadsafe(send_queue.put(msg), ws_loop)
                    except Exception as e:
                        logger.debug(f"Dismissal callback failed (non-critical): {e}")
            
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
             
        return {
            "generating": session['generating'],
            "active_agent": session['session_name'],
            "agents": agent_pool.list_agents() if agent_pool else [],
            "active_stack": get_active_stack(),
            "instance_halted": agent_pool.is_halted(session.get('session_name', '')) if (agent_pool and hasattr(agent_pool, 'is_halted')) else False,
        }

    # ── REST endpoints ────────────────────────────────────────────────────

    @app.get("/api/agents")
    async def api_list_agents():
        return [
            {
                'name': getattr(a, 'name', f'Agent-{i}'),
                'index': i,
                'description': getattr(a, 'description', ''),
                'tools': list(a.function_map.keys()) if hasattr(a, 'function_map') else [],
            }
            for i, a in enumerate(agents)
        ]

    @app.get("/api/state")
    async def api_get_state():
        return build_state()

    @app.post("/api/reset")
    async def api_reset():
        # Clear pool instance conversation (unified path)
        if agent_pool:
            inst = agent_pool.get_instance(session['session_name'])
            if inst is not None:
                with inst._compression_lock:
                    inst.conversation.clear()
                    # Invalidate token count cache — conversation cleared
                    inst._last_token_count_conversation_length = -1
                # Create a new logger session so messages go to a new JSONL file (Fix: New Session was appending to old logs)
                try:
                    agent_pool._logger.create_new_session(
                        session['session_name'], inst.agent_class
                    )
                except Exception as e:
                    logger.debug(f"Logger reset during new session failed (non-critical): {e}")
        
        # Phase 6: No need to clear session['history'] — pool is the source of truth
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

    @app.post("/api/halt/{instance_name}")
    async def api_halt_instance(instance_name: str):
        """Halt a specific agent instance (pauses it until resumed)."""
        if agent_pool and hasattr(agent_pool, 'halt_instance'):
            agent_pool.halt_instance(instance_name)
            return {"status": "ok", "message": f"Instance {instance_name} halted"}
        return {"status": "error", "message": "Agent pool not available"}

    @app.post("/api/resume/{instance_name}")
    async def api_resume_instance(instance_name: str):
        """Resume a previously halted agent instance."""
        if agent_pool and hasattr(agent_pool, 'resume_instance'):
            agent_pool.resume_instance(instance_name)
            return {"status": "ok", "message": f"Instance {instance_name} resumed"}
        return {"status": "error", "message": "Agent pool not available"}

    @app.post("/api/resume_all")
    async def api_resume_all():
        """Resume all halted agent instances."""
        if agent_pool and hasattr(agent_pool, 'resume_all_instances'):
            agent_pool.resume_all_instances()
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
        except Exception:
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

                    if session['generating']:
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
                        session['generate_cfg'] = data['generate_cfg']

                    # Check for /rollback command
                    if text.startswith('/rollback'):
                        parts = text.split()
                        n = 1
                        if len(parts) > 1:
                            try:
                                n = int(parts[1])
                            except ValueError as e:
                                logger.warning(f"Invalid rollback count in /rollback command: {e}")
                        
                        # Rollback N messages using pool's surgical_rollback (unified path)
                        if agent_pool:
                            inst = agent_pool.get_instance(session['session_name'])
                            if inst is not None and len(inst.conversation) > 0:
                                agent_pool.surgical_rollback(
                                    session['session_name'], n, reason="Manual /rollback command"
                                )
                            else:
                                await broadcast({'type': 'error', 'message': 'Nothing to roll back — no conversation yet'})
                                continue
                        await broadcast({'type': 'state', **build_state()})
                        continue

                    # Add user message to the pool instance's conversation (unified path).
                    # This replaces: session['history'].append(...) + sync to pool.
                    parsed_content = _parse_multimodal_content(text)
                    instance_name = session['session_name']
                    
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
                        
                        # Now add the user message (instance guaranteed to exist)
                        user_msg = Message(role=USER, content=parsed_content)
                        agent_pool.add_message(instance_name, user_msg)

                    # Start agent generation
                    with session_lock:
                        session['stop_requested'] = False
                    if agent_pool:
                        agent_pool.stopped = False
                    
                    session['generation_id'] += 1
                    gen_id = session['generation_id']
                    agent_runner = get_agent()
                    loop = asyncio.get_event_loop()

                    thread = threading.Thread(
                        target=run_agent_thread,
                        args=(None, agent_runner, gen_id, loop),
                        daemon=True,
                    )
                    thread.start()

                    await broadcast({'type': 'state', **build_state(generating=True)})

                elif msg_type == 'continue':
                    # Continue generation WITHOUT inserting a new user message.
                    # Just send the existing conversation to the LLM so it can resume if it wants.
                    # Update session config if provided
                    if 'agent_index' in data:
                        session['agent_index'] = int(data['agent_index'])
                    if 'session_name' in data:
                        session['session_name'] = data['session_name']
                    if 'generate_cfg' in data:
                        session['generate_cfg'] = data['generate_cfg']

                    # Start agent generation with existing history (no new user message)
                    with session_lock:
                        session['stop_requested'] = False
                    if agent_pool:
                        agent_pool.stopped = False

                    session['generation_id'] += 1
                    gen_id = session['generation_id']
                    agent_runner = get_agent()
                    loop = asyncio.get_event_loop()

                    # Get the current history from the pool (unified path)
                    assert inst is not None, "No agent instance found for session"
                    history_copy = copy.deepcopy(inst.conversation)

                    thread = threading.Thread(
                        target=run_agent_thread,
                        args=(history_copy, agent_runner, gen_id, loop),
                        daemon=True,
                    )
                    thread.start()

                    await broadcast({'type': 'state', **build_state(generating=True)})

                elif msg_type == 'stop':
                    with session_lock:
                        session['stop_requested'] = True
                    if agent_pool:
                        agent_pool.stopped = True

                elif msg_type == 'resume':
                    # Resume a halted instance and restart its generation from where it left off
                    target_instance = data.get('instance_name', session['session_name'])
                    with session_lock:
                        is_generating = session['generating']
                    
                    was_halted = False
                    if agent_pool:
                        was_halted = agent_pool.is_halted(target_instance)
                        agent_pool.resume_instance(target_instance)
                        logger.info(f"Instance {target_instance} resumed by user. Was halted: {was_halted}")
                    
                    # For the main session: only restart generation if it was actually halted
                    if target_instance == session['session_name']:
                        if is_generating and was_halted:
                            # Currently generating but was halted — signal stop first, then restart with continuation
                            logger.info(f"Main session was still generating — signalling stop before resume.")
                            with session_lock:
                                session['stop_requested'] = True
                            agent_pool.stopped = True
                            # Brief delay to allow old thread to observe the stop signal
                            await asyncio.sleep(0.1)
                        
                        if was_halted:
                            # Was halted (regardless of generating state — we already handled the generating case above)
                            # Inject continuation message into pool instance conversation (unified path)
                            cont_msg = "[SYSTEM]: You were paused. Please continue from where you left off."
                            parsed_content = _parse_multimodal_content(cont_msg)
                            if agent_pool:
                                cont_user_msg = Message(role=USER, content=parsed_content)
                                agent_pool.add_message(target_instance, cont_user_msg)
                            
                            # Start agent generation
                            with session_lock:
                                session['stop_requested'] = False
                            if agent_pool:
                                agent_pool.stopped = False
                                
                                # ── Fix 3: Restore agent instance conversations from JSONL logs if corrupted ──
                                # After a failed forced compression cycle, agent instance pools may be empty/corrupted.
                                # Read directly from log files on disk to recover.
                                try:
                                    # Import validate_message_pool locally (defined in execution_engine.py)
                                    from agent_cascade.execution_engine import validate_message_pool
                                    
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
                                                            if "metadata" not in item:  # Skip metadata lines
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
                                                    with sa_inst._compression_lock:
                                                        sa_inst.conversation[:] = recov  # Replace in-place under lock
                                            else:
                                                logger.warning(
                                                    f"Could not restore agent instance {sa_name} pool — "
                                                    f"no valid recovery data found in logs"
                                                )
                                        except Exception as _e:
                                            # Single agent failure shouldn't block resume for others
                                            logger.warning(f"Failed to restore agent instance {sa_name} pool: {_e}")
                                except ImportError:
                                    logger.warning("validate_message_pool not available — skipping agent instance pool restoration")
                                # ── End Fix 3 ──
                            
                            session['generation_id'] += 1
                            gen_id = session['generation_id']
                            agent_runner = get_agent()
                            loop = asyncio.get_event_loop()

                            thread = threading.Thread(
                                target=run_agent_thread,
                                args=(None, agent_runner, gen_id, loop),
                                daemon=True,
                            )
                            thread.start()

                            await broadcast({'type': 'state', **build_state(generating=True)})
                        elif not is_generating:
                            # Not halted and not generating — just update UI state (no-op from user's perspective)
                            await broadcast({'type': 'state', **build_state()})
                    
                    # For agent instances: inject a continuation message into their queue so when 
                    # the orchestrator next calls them, they continue from where they left off
                    elif target_instance != session['session_name'] and agent_pool and was_halted:
                        cont_msg = f"[SYSTEM]: Agent {target_instance} was paused. Please continue from where you left off."
                        agent_pool.enqueue_message(target_instance, cont_msg)
                        logger.info(f"Injected continuation message into agent instance {target_instance}'s queue.")

                elif msg_type == 'terminate_agent_instance':
                    instance_name = data.get('instance_name')
                    if instance_name and agent_pool:
                        agent_pool.dismiss_instance(instance_name)
                    # Force immediate state broadcast to update UI (remove tab)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'retry':
                    if session['generating']:
                        continue
                    
                    instance_name = session['session_name']
                    
                    # Remove trailing assistant/function messages from the pool instance's conversation (unified path)
                    if agent_pool:
                        inst = agent_pool.get_instance(instance_name)
                        if inst is not None:
                            with inst._compression_lock:
                                while inst.conversation and _get_msg_role(inst.conversation[-1]) in (ASSISTANT, FUNCTION):
                                    inst.conversation.pop()

                    # Roll back one more (the user message) to allow a clean re-trigger
                    last_user_msg = None
                    inst = agent_pool.get_instance(instance_name)
                    with inst._compression_lock:
                        if inst.conversation and _get_msg_role(inst.conversation[-1]) == USER:
                            last_user_msg = inst.conversation.pop()

                    if not (agent_pool and agent_pool.get_instance(instance_name)) and not _get_main_history(agent_pool, instance_name) and not last_user_msg:
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
                            with inst._compression_lock:
                                inst.conversation.append(last_user_msg)
                                # Invalidate token count cache — conversation length changed
                                inst._last_token_count_conversation_length = -1
                        else:
                            # Fallback: create the instance first, then add the message
                            create_main_agent_instance(
                                pool=agent_pool,
                                instance_name=instance_name,
                                system_message_content="",
                            )
                            agent_pool.add_message(instance_name, last_user_msg)

                    if 'generate_cfg' in data:
                        session['generate_cfg'] = data['generate_cfg']

                    with session_lock:
                        session['stop_requested'] = False
                        session['generation_id'] += 1
                    if agent_pool:
                        agent_pool.stopped = False
                    gen_id = session['generation_id']
                    agent_runner = get_agent()
                    loop = asyncio.get_event_loop()

                    thread = threading.Thread(
                        target=run_agent_thread,
                        args=(None, agent_runner, gen_id, loop),
                        daemon=True,
                    )
                    thread.start()
                    await broadcast({'type': 'state', **build_state(generating=True)})

                elif msg_type == 'reset':
                    # Clear pool instance conversation (unified path)
                    if agent_pool:
                        inst = agent_pool.get_instance(session['session_name'])
                        if inst is not None:
                            with inst._compression_lock:
                                inst.conversation.clear()
                                # Invalidate token count cache — conversation cleared
                                inst._last_token_count_conversation_length = -1
                            # Create a new logger session so messages go to a new JSONL file (Fix: New Session was appending to old logs)
                            try:
                                agent_pool._logger.create_new_session(
                                    session['session_name'], inst.agent_class
                                )
                            except Exception as e:
                                logger.debug(f"Logger reset during stop failed (non-critical): {e}")
                    
                    with session_lock:
                        # Phase 6: No need to clear session['history'] — pool is the source of truth
                        session['generating'] = False
                        session['stop_requested'] = False
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
                        if 'work_access_folders_ro' in ui_cfg or 'work_access_folders_rw' in ui_cfg:
                            if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                                ro = ui_cfg.get('work_access_folders_ro', [])
                                rw = ui_cfg.get('work_access_folders_rw', [])
                                agent_pool.operation_manager.set_extra_work_folders(ro, rw)
                        if 'default_workspace' in ui_cfg:
                            if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                                new_ws = ui_cfg['default_workspace']
                                if new_ws:
                                    agent_pool.operation_manager.set_base_dir(new_ws)
                        # Update idle timeout from UI settings (Bug #3 fix)
                        if 'idle_timeout_seconds' in ui_cfg and agent_pool and hasattr(agent_pool, 'settings'):
                            val = float(ui_cfg['idle_timeout_seconds'])
                            agent_pool.settings.idle_timeout_seconds = max(0.0, val)  # 0 disables auto-dismissal
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'update_endpoints':
                    # Bulk update all endpoints and priorities from UI
                    if agent_pool and hasattr(agent_pool, 'api_router'):
                        agent_pool.api_router.from_dict(data)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'update_api_priorities':
                    # Update just the agent-type → endpoint priority mappings
                    if agent_pool and hasattr(agent_pool, 'api_router'):
                        priorities = data.get('agent_priorities', {})
                        for agent_type, endpoint_ids in priorities.items():
                            agent_pool.api_router.set_agent_priorities(agent_type, endpoint_ids)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'approve':
                    rid = data.get('request_id')
                    if rid and agent_pool:
                        is_auto = data.get('automated', False)
                        logger.info(f"[{'AUTO' if is_auto else 'USER'}] Approving request: {rid}")
                        agent_pool.operation_manager.user_approve(rid)

                elif msg_type == 'reject':
                    rid = data.get('request_id')
                    reason = data.get('reason', 'Rejected by user')
                    if rid and agent_pool:
                        is_auto = data.get('automated', False)
                        logger.info(f"[{'AUTO' if is_auto else 'USER'}] Rejecting request: {rid}. Reason: {reason}")
                        agent_pool.operation_manager.user_reject(rid, reason)

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
                            def _security_check():
                                sec_state_key = None  # Defined early so finally can reference it directly (Fix #4)
                                sec_endpoint_release = None  # Fix #3: endpoint slot release callback
                                try:
                                    import platform
                                    import json
                                    import copy

                                    with app.security_check_lock:
                                        if not agent_pool.get_agent('security_advisor'):
                                            agent_pool.load_agent('security_advisor')
                                        sec_agent = agent_pool.get_agent('security_advisor')
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
                                        
                                        history = [
                                            {'role': USER, 'content': prompt},
                                        ]
                                        
                                        # ── Fix #3: Acquire endpoint scheduling slot before execution ──
                                        if hasattr(agent_pool, '_execution') and hasattr(agent_pool._execution, '_acquire_slot'):
                                            try:
                                                sec_endpoint_release = agent_pool._execution._acquire_slot('security_advisor', 'security_advisor')
                                            except Exception as e:
                                                logger.warning(f"Failed to acquire endpoint slot for security_advisor: {e}")

                                        # Register security advisor in instance_state so it shows a tab (Fix #4: thread-safe)
                                        sec_state_key = 'security_advisor'
                                        with agent_pool._execution._state_lock:
                                            agent_pool.instance_state[sec_state_key] = {
                                                'active': True,
                                                'agent_name': f"Security Advisor (security_advisor)",
                                                'messages': list(history),
                                            }
                                            agent_pool.instance_conversations[sec_state_key] = list(history)
                                            if sec_state_key not in agent_pool.active_stack:
                                                agent_pool._execution.active_stack.append(sec_state_key)
                                        # Broadcast initial state so the tab appears immediately
                                        asyncio.run_coroutine_threadsafe(
                                            send_queue.put({'type': 'stream_update', **build_stream_update([], agent_instances=get_instance_state(streaming=True))}),
                                            loop
                                        )
                                        
                                        NON_LLM_KEYS = (
                                            'max_auto_rollbacks', 'auto_rollback_on_loop', 'auto_continue', 
                                            'max_turns', 'mcpServers', 'work_access_folders', 'seed',
                                            'read_file_limit', 'grep_char_limit', 'grep_spillover', 'shell_char_limit', 'code_char_limit',
                                            'disabled_tools',
                                            # Exclude endpoint-identifying keys to let the agent use its own assigned API Router config
                                            'model', 'model_server', 'api_base', 'base_url', 'api_key', 'model_type'
                                        )
                                        ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))
                                        llm_safe_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
                                        
                                        final_msgs = []
                                        sec_first_broadcast = True
                                        
                                        # ── Timeout protection: prevent AFK rejection cascades ──
                                        sec_start_time = time.monotonic()
                                        sec_timeout_reached = False
                                        sec_elapsed_at_timeout = None  # Fix #5: store elapsed at the moment of timeout

                                        # ── Fix #2: Route LLM call through API router instead of direct sec_agent.run() ──
                                        api_router_sec = getattr(agent_pool, 'api_router', None)

                                        if api_router_sec and hasattr(api_router_sec, 'call_with_fallback'):
                                            def _security_llm_call(llm_cfg: dict):
                                                """Execute the security advisor's LLM call with the given config."""
                                                # Build messages list — prepend system message like Agent.run() does
                                                messages_to_send = list(history)
                                                if sec_agent.system_message and (not messages_to_send or
                                                        messages_to_send[0].get('role') != SYSTEM):
                                                    messages_to_send.insert(
                                                        0, {'role': SYSTEM, 'content': sec_agent.system_message}
                                                    )

                                                # Merge configs: agent extra → LLM generate_cfg → UI settings → router config
                                                merged_cfg = {}
                                                if hasattr(sec_agent, 'extra_generate_cfg') and sec_agent.extra_generate_cfg:
                                                    merged_cfg.update(sec_agent.extra_generate_cfg)
                                                if hasattr(sec_agent.llm, 'generate_cfg'):
                                                    merged_cfg.update(sec_agent.llm.generate_cfg)
                                                # Apply non-LLM config (max_turns, etc.) from UI settings
                                                merged_cfg.update(llm_safe_cfg)
                                                # Apply router-provided LLM config (model, api_base, etc.) — overrides
                                                merged_cfg.update(llm_cfg)
                                                merged_cfg['agent_name'] = sec_agent.name

                                                return sec_agent.llm.chat(
                                                    messages=messages_to_send,
                                                    stream=True,
                                                    delta_stream=False,
                                                    extra_generate_cfg=merged_cfg,
                                                )

                                            # call_with_fallback handles retries, per-endpoint concurrency semaphores, and failover
                                            run_gen = api_router_sec.call_with_fallback('security_advisor', _security_llm_call)
                                        else:
                                            # Fallback: direct LLM call if no router available (preserves old behavior)
                                            logger.warning("API router unavailable — using direct LLM call for security advisor")
                                            # Prepend system message like Agent.run() does
                                            messages_to_send = list(history)
                                            if sec_agent.system_message and (not messages_to_send or
                                                    messages_to_send[0].get('role') != SYSTEM):
                                                messages_to_send.insert(
                                                    0, {'role': SYSTEM, 'content': sec_agent.system_message}
                                                )
                                            # Include agent_name in config for proper tracking
                                            fallback_cfg = {}
                                            if hasattr(sec_agent, 'extra_generate_cfg') and sec_agent.extra_generate_cfg:
                                                fallback_cfg.update(sec_agent.extra_generate_cfg)
                                            fallback_cfg.update(llm_safe_cfg)
                                            fallback_cfg['agent_name'] = sec_agent.name
                                            run_gen = sec_agent.llm.chat(
                                                messages=messages_to_send,
                                                stream=True,
                                                delta_stream=False,
                                                extra_generate_cfg=fallback_cfg,
                                            )
                                        
                                        # Schedule warning AFTER generator creation so timer is only created if gen succeeded
                                        def _sec_warning_injector():
                                            try:
                                                agent_pool.enqueue_message(
                                                    'security_advisor',
                                                    "[SYSTEM WARNING] Your analysis is taking longer than expected. "
                                                    "Please provide a verdict as soon as possible — the approval request may timeout soon."
                                                )
                                            except Exception as e:
                                                logger.debug(f"Security advisor warning injection failed (non-critical): {e}")
                                        
                                        sec_warning_timer = threading.Timer(SECURITY_ADVISOR_WARNING_SECONDS, _sec_warning_injector)
                                        sec_warning_timer.daemon = True
                                        sec_warning_timer.start()
                                        try:
                                            for partial in run_gen:
                                                # ── Check if we've exceeded the timeout ──
                                                elapsed = time.monotonic() - sec_start_time
                                                if elapsed > SECURITY_ADVISOR_TIMEOUT_SECONDS:
                                                    sec_timeout_reached = True
                                                    sec_elapsed_at_timeout = elapsed  # Fix #5: capture exact elapsed at break point
                                                    logger.warning(
                                                        f"[SECURITY] Timeout reached after {elapsed:.0f}s for request {rid}. "
                                                        f"Terminating security advisor to prevent AFK rejection."
                                                    )
                                                    break
                                                
                                                final_msgs = partial
                                                # Fix #4: Update instance_state with lock protection during streaming
                                                with agent_pool._execution._state_lock:
                                                    agent_pool.instance_state[sec_state_key]['messages'] = (
                                                        list(history) + (list(final_msgs) if isinstance(final_msgs, list) else [final_msgs])
                                                    )
                                                    agent_pool.instance_conversations[sec_state_key] = list(
                                                        agent_pool.instance_state[sec_state_key]['messages']
                                                    )
                                                # Only broadcast at start and end of security check (not every token)
                                                if sec_first_broadcast:
                                                    asyncio.run_coroutine_threadsafe(
                                                        send_queue.put({'type': 'stream_update', **build_stream_update([], agent_instances=get_instance_state(streaming=True))}),
                                                        loop
                                                    )
                                                    sec_first_broadcast = False
                                        finally:
                                            # Cancel the warning timer if we finished before it fires
                                            sec_warning_timer.cancel()
                                            # Fix #1: Close the generator to abort any active LLM call / HTTP connection
                                            try:
                                                run_gen.close()
                                            except Exception as e:
                                                logger.debug(f"Security advisor generator cleanup failed (non-critical): {e}")

                                    display_response = ""
                                    parsing_response = ""

                                    new_msgs = final_msgs
                                    for msg in new_msgs:
                                        role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                                        content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
                                        reasoning = msg.get('reasoning_content', '') if isinstance(msg, dict) else getattr(msg, 'reasoning_content', '')
                                        fc = msg.get('function_call', None) if isinstance(msg, dict) else getattr(msg, 'function_call', None)
                                        
                                        # Normalize content and reasoning to string for regex safety
                                        if isinstance(content, list):
                                            content_str = " ".join([str(item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')) for item in content])
                                        else:
                                            content_str = str(content)
                                            
                                        if isinstance(reasoning, list):
                                            reasoning_str = " ".join([str(item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')) for item in reasoning])
                                        else:
                                            reasoning_str = str(reasoning)

                                        if role == 'assistant':
                                            # Deduplicate: if content already contains the reasoning, don't add it twice
                                            clean_content = content_str
                                            if reasoning_str:
                                                # Check if content starts with a thinking block that matches reasoning
                                                think_match = _THINK_SEARCH_RE.match(content_str)
                                                if think_match:
                                                    embedded_thought = think_match.group(2).strip()
                                                    # If they are very similar, we consider it a duplicate
                                                    if reasoning_str.strip() in embedded_thought or embedded_thought in reasoning_str.strip():
                                                        # Remove embedded thinking block from display_response part (only if at the start)
                                                        clean_content = strip_thinking_blocks(content_str).strip()
                                                
                                                display_response += f"<think>\n{reasoning_str.strip()}\n</think>\n\n"
                                            
                                            if fc:
                                                fname = fc.get('name', '') if isinstance(fc, dict) else getattr(fc, 'name', '')
                                                display_response += f"*(Tool call: {fname})*\n\n"

                                            if clean_content:
                                                display_response += f"{clean_content}\n\n"
                                                
                                            # For parsing, we always want the content WITHOUT any thinking blocks at the start
                                            if content_str:
                                                parsing_response = strip_thinking_blocks(content_str).strip()
                                                
                                        elif role == 'function':
                                            display_response += f"*(Result from {fname} - {len(str(content_str))} chars)*\n\n"

                                    parsing_text = parsing_response
                                    
                                    parsing_response = parsing_text.strip()
                                    
                                    # 1. Clean the text: Remove reasoning blocks completely to avoid false positives in "thinking"
                                    # This handles both <think> tags and [THINK] tags
                                    clean_text = parsing_response
                                    try:
                                        if '<think' in clean_text.lower() or '<thought' in clean_text.lower():
                                            clean_text = _THINK_BLOCK_RE.sub('', clean_text)
                                        if '[think' in clean_text.lower() or '[thought' in clean_text.lower():
                                            clean_text = _THINK_BLOCK_BRACKET_RE.sub('', clean_text).strip()
                                        clean_text = clean_text.strip()
                                        
                                        check_text = clean_text.upper()
                                        
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
                                        logger.error(f"Error extracting security verdict from {security_instance}: {e}")
                                        is_yes = False
                                        is_no = False
                                    
                                    # ── Handle security advisor timeout ──
                                    if sec_timeout_reached:
                                        elapsed = sec_elapsed_at_timeout  # Guaranteed non-None since timeout was just hit
                                        logger.info(f"[SECURITY] Timeout after {elapsed:.0f}s for request {rid}. Auto-rejecting to prevent AFK rejection cascade.")
                                        
                                        # Halt the security advisor instance to stop it cleanly.
                                        # Note: This is best-effort — only works between turns, not during active LLM calls.
                                        # The actual timeout enforcement happens inside the for loop via `break` + generator.close().
                                        agent_pool.halt_instance('security_advisor')
                                        
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
                                        if auto_apply:
                                            if is_yes:
                                                logger.info(f"[SECURITY] Automatic Approval for {rid} with justification: {justification[:50]}...")
                                                agent_pool.operation_manager.user_approve(rid, reason=justification)
                                            else:
                                                logger.info(f"[SECURITY] Automatic Rejection for {rid} with reason: {justification[:50]}...")
                                                # Auto-rejection message
                                                reject_msg = f"SECURITY REJECTED: {justification}" if justification else "SECURITY REJECTED: The security advisor flagged this operation as unsafe."
                                                agent_pool.operation_manager.user_reject(rid, reject_msg)
                                        else:
                                            # Valid format but auto_apply is off: Send to UI for manual confirmation
                                            asyncio.run_coroutine_threadsafe(
                                                send_queue.put({
                                                    'type': 'security_response', 
                                                    'request_id': rid, 
                                                    'response': display_response,
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
                                                send_queue.put({'type': 'security_response', 'request_id': rid, 'response': display_response + f"\n\n**[AUTO-REJECTED: Ambiguous Format]**", 'verdict': 'AMBIGUOUS'}),
                                                loop
                                            )
                                        else:
                                            # Manual mode: Let the user see the ambiguous response and decide
                                            logger.info(f"[SECURITY] Ambiguous response for {rid} in manual mode. Waiting for user decision.")
                                            asyncio.run_coroutine_threadsafe(
                                                send_queue.put({'type': 'security_response', 'request_id': rid, 'response': display_response, 'verdict': 'AMBIGUOUS'}),
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
                                    # Always clean up security advisor state when done (Fix #4: thread-safe)
                                    if sec_state_key and sec_state_key in agent_pool.instance_state:
                                        with agent_pool._execution._state_lock:
                                            agent_pool.instance_state[sec_state_key]['active'] = False
                                            if sec_state_key in agent_pool.active_stack:
                                                agent_pool._execution.active_stack.remove(sec_state_key)

                                    # Fix #3: Release endpoint slot when done
                                    if sec_endpoint_release is not None:
                                        try:
                                            sec_endpoint_release()
                                        except Exception as e:
                                            logger.warning(f"Failed to release endpoint slot for security_advisor: {e}")

                                    if hasattr(app, 'active_security_checks') and rid:
                                        with app.active_security_checks_lock:
                                            app.active_security_checks.discard(rid)
                            
                            threading.Thread(target=_security_check, daemon=True).start()

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
                        elif target_name in agent_pool.instance_conversations:
                            history = agent_pool.instance_conversations[target_name]
                    
                    # Phase 6: No fallback to session['history'] — pool is the source of truth
                    
                    if (idx is not None
                            and not session['generating']
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
                            # Write back the edited history under lock to prevent data race
                            inst = agent_pool.get_instance(target_name)
                            if inst is not None:
                                with inst._compression_lock:
                                    inst.conversation[:] = history  # In-place replace under lock
                            
                            logger_inst = agent_pool.get_logger(target_name, 'Orchestrator' if target_name == session['session_name'] else 'SubAgent')
                            logger_inst.reset_history(history, rewrite=True)
                            
                            # Sync instance_state so build_state() sees the edit
                            # (instance_state[name]['messages'] may be a separate reference and won't
                            #  reflect in-place edits to instance_conversations)
                            if target_name != session['session_name'] and target_name in agent_pool.instance_state:
                                agent_pool.instance_state[target_name]['messages'] = list(history)
                            
                            # Also sync main session back to pool so next generation doesn't
                            # overwrite the edit (pool is source of truth)
                            if target_name == session['session_name']:
                                agent_pool.instance_conversations[target_name] = list(history)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'delete_messages':
                    if session['generating']:
                        continue
                    target_name = data.get('instance_name') or session['session_name']
                    
                    # Get the conversation from pool instance (unified path)
                    history = []
                    if agent_pool:
                        inst = agent_pool.get_instance(target_name)
                        if inst is not None:
                            with inst._compression_lock:
                                history = list(inst.conversation)  # Defensive copy under lock
                        elif target_name in agent_pool.instance_conversations:
                            history = agent_pool.instance_conversations[target_name]
                    
                    # Phase 6: No fallback to session['history'] — pool is the source of truth
                    
                    indices = sorted(data.get('indices', []), reverse=True)
                    for idx in indices:
                        if 0 <= idx < len(history):
                            history.pop(idx)
                    if agent_pool:
                        # Write back the pruned history under lock to prevent data race
                        inst = agent_pool.get_instance(target_name)
                        if inst is not None:
                            with inst._compression_lock:
                                inst.conversation[:] = history  # In-place replace under lock
                        
                        logger_inst = agent_pool.get_logger(target_name, 'Orchestrator' if target_name == session['session_name'] else 'SubAgent')
                        logger_inst.reset_history(history, rewrite=True)
                        
                        # Sync instance_state so build_state() sees the deletion
                        if target_name != session['session_name'] and target_name in agent_pool.instance_state:
                            agent_pool.instance_state[target_name]['messages'] = list(history)
                        
                        # Also sync main session back to pool so next generation doesn't
                        # re-deep-copy the un-deleted version (pool is source of truth)
                        if target_name == session['session_name']:
                            agent_pool.instance_conversations[target_name] = list(history)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'select_agent':
                    session['agent_index'] = int(data.get('index', 0))
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'set_session_name':
                    old_name = session['session_name']
                    new_name = data.get('name', 'Maine')
                    session['session_name'] = new_name
                    if agent_pool:
                        # Migrate history to new name in pool
                        if old_name in agent_pool.instance_conversations:
                            agent_pool.instance_conversations[new_name] = agent_pool.instance_conversations.pop(old_name)
                        if old_name in agent_pool.instance_summaries:
                            agent_pool.instance_summaries[new_name] = agent_pool.instance_summaries.pop(old_name)
                    
                    await broadcast({'type': 'state', **build_state()})



                elif msg_type == 'load_session':
                    path = data.get('path')
                    if path and agent_pool:
                        status = agent_pool.load_session_from_log(path, target_instance=session.get('session_name'))
                        if status.startswith("Error"):
                            await websocket.send_text(json.dumps({"type": "error", "message": status}, ensure_ascii=False))
                        else:
                            # Phase 6: Pool is already loaded by load_session_from_log — no need to sync to session
                            instance_name = session.get('session_name', 'Maine')
                            inst = agent_pool.get_instance(instance_name)
                            if inst is not None:
                                session['generating'] = False
                                session['stop_requested'] = False
                                if agent_pool:
                                    agent_pool.stopped = False
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
    import argparse
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
    operation_mgr = OperationManager(base_dir=args.workspace)
    
    agent_pool = AgentPool(
        llm_cfg=initial_llm_cfg,
        agents_dir=str(PROJECT_ROOT / 'agents'),
        workspace_dir=args.workspace,
        operation_manager=operation_mgr,
    )
    operation_mgr.agent_pool = agent_pool
    
    # Set idle timeout settings via PoolSettings (new pool uses PoolSettings instead of constructor args)
    agent_pool.settings.idle_timeout_seconds = idle_timeout
    agent_pool.settings.idle_check_interval = idle_check_interval
    
    # Create the root orchestrator instance in the new pool (use lowercase to match template key)
    agent_pool.create_instance('Maine', 'orchestrator')
    
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
            maine_inst.conversation.append(Message(role=SYSTEM, content=sys_msg_content))
    
    app = create_app(agents=[orch_agent], agent_pool=agent_pool)
    
    logger.info("Starting AgentCascade API Server on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)
