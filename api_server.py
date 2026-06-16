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
from operation_manager import SECURITY_ADVISOR_TIMEOUT_SECONDS, SECURITY_ADVISOR_WARNING_SECONDS

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

# Import LoopDetectedError for loop detection in api_server.py (used with agent_orchestrator.py pattern)
from agent_orchestrator import LoopDetectedError

# Pre-compiled regexes moved to agent_cascade.utils.thinking_block


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
        content = m.get(CONTENT, '')
        if isinstance(content, list):
            # For multimodal content, just use the text parts
            text_parts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text']
            content = " ".join(text_parts)
        content = str(content)
        
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
        role = m.get(ROLE) if isinstance(m, dict) else getattr(m, 'role', '')
        if role != SYSTEM:
            feat = get_feature(m)
            # Skip consecutive duplicates to avoid treating incremental streaming updates as loops.
            # Only consecutive duplicates are filtered (not global), so real repeated patterns
            # later in the conversation are still detected correctly.
            if not features or features[-1] != feat:
                features.append(feat)
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

def create_app(agents, agent_pool, config=None, root_agent=None):
    """
    Create the FastAPI application.

    Args:
        agents:     List of Agent objects (orchestrator first, then sub-agents)
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
            history = session.get('history', [])
            
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
        try:
            if not agent_pool:
                return [], ""
                
            if hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                log_dir = agent_pool.operation_manager.base_dir / 'logs'
            else:
                log_dir = Path(DEFAULT_WORKSPACE) / 'logs'
            
            # Orchestrator logs might be named session_NAME.jsonl or follow the sub-agent pattern
            path = log_dir / f"session_{name}.jsonl"
            if not path.exists():
                # Try finding a log with the Orchestrator pattern
                potential = list(log_dir.glob(f"Orchestrator_{name}_*.jsonl"))
                if potential:
                    potential.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                    path = potential[0]

            if not path.exists():
                return [], ""

            # Use the AgentPool's standardized loading logic to handle slicing
            # and system message preservation
            status = agent_pool.load_session_from_log(str(path), target_instance=name)
            if status.startswith("Error"):
                logger.error(f"Failed to load session {name} via pool: {status}")
                return [], ""
            
            loaded_history = agent_pool.instance_conversations.get(name, [])
            loaded_summary = agent_pool.instance_summaries.get(name, "")
            
            return loaded_history, loaded_summary
        except Exception as e:
            logger.error(f"Failed to load session history: {e}")
        return [], ""

    # ── Shared session state ──────────────────────────────────────────────
    default_session_name = config.get('session_name', 'Maine')
    session: Dict[str, Any] = {
        'history': [], # Will be loaded below
        'agent_index': 0,
        'session_name': default_session_name,
        'generating': False,
        'stop_requested': False,
        'generation_id': 0,         # Increment on each run to prevent stale appends
        'summary': "",
        'root_agent_class': None,
    }
    # Initial load
    session['history'], session['summary'] = _load_session_history(default_session_name)
    if agent_pool:
        agent_pool.instance_conversations[default_session_name] = session['history']
        agent_pool.instance_summaries[default_session_name] = session['summary']


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
    # Protects: session['generating'], session['stop_requested'], session['history'] mutations.
    session_lock = threading.Lock()



    def get_agent():
        if session['session_name'] == 'Root' and root_agent is not None:
            return root_agent
        idx = session['agent_index']
        if 0 <= idx < len(agents):
            return agents[idx]
        return agents[0]


    def get_sub_agent_state(streaming=False):
        result = {}
        if agent_pool and hasattr(agent_pool, 'sub_agent_state'):
            for name, state in agent_pool.sub_agent_state.items():
                msgs = state.get('messages', [])
                
                # Extract the actual agent class from agent_name.
                # agent_name is stored as "instance_name (AgentClass)" by the orchestrator,
                # e.g. "worker1 (Coder)". We need just "Coder" to look up the agent template.
                raw_agent_name = state.get('agent_name', name)
                if ' (' in raw_agent_name and raw_agent_name.endswith(')'):
                    agent_class = raw_agent_name.split(' (')[-1].rstrip(')')
                else:
                    agent_class = raw_agent_name
                
                # Get max tokens for this sub-agent's own model/endpoint.
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
                
                # FIX1 (sub-agent index mismatch): Always compute from the sliced/active set.
                # slice_history_for_llm can reduce the message count during compression,
                # so we track active_count (len of sliced history) instead of len(msgs).
                # This ensures all indexing into active_msgs is consistent.
                active_msgs = agent_pool.slice_history_for_llm(msgs) if agent_pool else msgs
                active_count = len(active_msgs)
                
                # Incremental token counting for sub-agents.
                # During streaming, sub-agent histories are mostly static — they only change
                # when a new message arrives from that sub-agent (rare during main agent ticks).
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
                # Threshold raised to 30 to reduce partial updates for short conversations.
                # Tail size is proportional (10% of messages, min 5) to reduce sync gaps.
                if streaming and state.get('active') and len(msgs) > 30:
                    tail_size = max(5, len(msgs) // 10)  # Send at least 10% or 5 messages as tail
                    start_idx = max(0, len(msgs) - tail_size)
                    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs[-tail_size:], start_idx)]
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
            if agent_pool and hasattr(agent_pool, 'telemetry'):
                return agent_pool.telemetry.get_session_summary()
        except Exception:
            pass
        return None

    def build_state(responses=None, generating=None):
        """Build a full state snapshot for the frontend."""
        msgs = list(session['history'])
        if responses:
            msgs.extend(responses)

        # Calculate tokens for the main session
        orch_agent = get_agent()
        max_tokens = get_agent_max_tokens(orch_agent)
        
        # Calculate tokens for the active 'working set' (after compression)
        active_h = agent_pool.slice_history_for_llm(session['history']) if agent_pool else session['history']
        
        # When show_active_only mode is enabled, only send the active working set to the frontend
        ui_cfg = session.get('generate_cfg', {})
        show_active_only = ui_cfg.get('show_active_only', False)
        display_msgs = list(active_h) if show_active_only else msgs
        if responses:
            display_msgs = display_msgs + list(responses)
        
        # FIX3: Cache main history stats at session level to avoid re-tokenizing on every build_state() call
        hist_count = len(active_h)
        cached_hist_count = session.get('_cached_hist_stats_count', -1)
        if hist_count > cached_hist_count:
            # History grew — compute incrementally (only new messages)
            if cached_hist_count >= 0 and cached_hist_count < len(active_h):
                cached_h_stats = session.get('_cached_hist_stats', {'tokens': 0, 'words': 0})
                new_msgs = active_h[cached_hist_count:]
                new_stats = get_history_stats(new_msgs)
                h_stats = {
                    'tokens': cached_h_stats['tokens'] + new_stats['tokens'],
                    'words': cached_h_stats['words'] + new_stats['words']
                }
            else:
                # First access or cache missing — compute full stats
                h_stats = get_history_stats(active_h)
            session['_cached_hist_stats'] = h_stats.copy()
            session['_cached_hist_stats_count'] = hist_count
        elif hist_count < cached_hist_count:
            # FIX2 (history shrank due to compression): Recompute from scratch.
            # slice_history_for_llm may have dropped older messages after a context_summary,
            # so the cached stats would overcount tokens if we reused them.
            h_stats = get_history_stats(active_h)
            session['_cached_hist_stats'] = h_stats.copy()
            session['_cached_hist_stats_count'] = hist_count
        elif '_cached_hist_stats' in session:
            # Same messages — use cached stats
            h_stats = session['_cached_hist_stats'].copy()
        else:
            # First access — compute and cache
            h_stats = get_history_stats(active_h)
            session['_cached_hist_stats'] = h_stats.copy()
            session['_cached_hist_stats_count'] = hist_count
        
        r_stats = get_history_stats(responses) if responses else {'tokens': 0, 'words': 0}
        
        total_tokens = h_stats['tokens'] + r_stats['tokens']
        total_words = h_stats['words'] + r_stats['words']
        
        # (max_tokens is already calculated above)

        # Sync session summary from history if it was just compressed
        current_summary = session.get('summary', '')
        for msg in reversed(session['history']):
            content = msg.get(CONTENT, '')
            if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                import re
                # Only match content INSIDE the tags, allowing optional newlines/whitespace
                match = _CONTEXT_SUMMARY_RE.search(content)
                if match:
                    current_summary = match.group(1).strip()
                break
        session['summary'] = current_summary
        if agent_pool:
            agent_pool.instance_summaries[session['session_name']] = current_summary

        return {
            'messages': [serialize_message(m, i) for i, m in enumerate(display_msgs)],
            'sub_agents': get_sub_agent_state(),
            'active_stack': get_active_stack(),
            'approvals': get_approvals(),
            'generating': generating if generating is not None else session['generating'],
            'session_name': session['session_name'],
            'agent_index': session['agent_index'],
            'root_agent_class': session.get('root_agent_class'),
            'total_tokens': total_tokens,
            'total_words': total_words,
            'max_tokens': max_tokens,
            'summary': current_summary,
            'telemetry': _safe_get_telemetry(),
            'agents': [
                {'name': getattr(a, 'name', f'Agent-{i}'), 'index': i,
                 'agent_type': getattr(a, 'agent_type', 'orchestrator').lower(),
                 'description': getattr(a, 'description', ''),
                 'tools': list(a.function_map.keys()) if hasattr(a, 'function_map') else [],
                 'default_tools': getattr(a, 'default_tools', list(a.function_map.keys()) if hasattr(a, 'function_map') else [])}
                for i, a in enumerate(agents)
            ],
            'current_model': getattr(get_agent().llm, 'model', 'Unknown') if hasattr(get_agent(), 'llm') and get_agent().llm else 'Unknown',
            'default_workspace': str(agent_pool.operation_manager.base_dir) if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager else str(DEFAULT_WORKSPACE),
            'has_queued_messages': agent_pool.has_messages(session['session_name']) if agent_pool else False,
            'is_waiting': agent_pool.api_router.is_waiting(session['session_name']) if agent_pool and hasattr(agent_pool, 'api_router') else False,
            'api_router': agent_pool.api_router.to_dict() if agent_pool and hasattr(agent_pool, 'api_router') else {'endpoints': [], 'agent_priorities': {}},
        }

    def build_stream_update(responses, cached_h_stats=None, sub_agents=None, telemetry=None, update_counter=True):
        """Build a lightweight streaming delta (skips re-serializing stable history).
    
        Args:
            responses: Current partial response messages from the agent runner.
            cached_h_stats: Pre-computed history stats to avoid O(n) recalculation each tick.
                           If None, falls back to get_history_stats(session['history']).
            sub_agents: Pre-serialized sub-agent state. Only recompute every ~5 ticks;
                       on intermediate ticks the client tolerates slight staleness.
            telemetry: Pre-serialized session telemetry summary. Only recompute every ~20 ticks
                       (approx 3 seconds) to avoid heavy re-aggregation during streaming.
                update_counter: If True (default), update _last_sent_resp_count for delta tracking.
                               Set to False for sidebar calls (e.g., security check thread) that 
                               shouldn't affect the main streaming loop's counter.
        """
        history_count = len(session['history'])

        # Only serialize the changing response messages (history is already on the client)
        active_h = agent_pool.slice_history_for_llm(session['history']) if agent_pool else session['history']

        # When show_active_only, only send the active window + new responses
        ui_cfg = session.get('generate_cfg', {})
        show_active_only = ui_cfg.get('show_active_only', False)
        history_count = len(active_h) if show_active_only else history_count

        # FIX: Send only delta messages to prevent duplication (critical fix for UI duplication issue)
        # Track how many response messages were last sent to compute the delta
        last_sent_resp_count = session.get('_last_sent_resp_count', 0)
        
        if not responses:
            response_msgs = []
        elif len(responses) > last_sent_resp_count:
            # New message(s) added — send only the NEW messages (delta)
            new_msgs = responses[last_sent_resp_count:]
            response_msgs = [serialize_message(m, history_count + last_sent_resp_count + i) 
                            for i, m in enumerate(new_msgs)]
        elif last_sent_resp_count > 0 and len(responses) == last_sent_resp_count:
            # Same count — only re-serialize the LAST message (its content grew during streaming)
            # This prevents sending all messages again when only the last one is growing
            response_msgs = [serialize_message(responses[-1], history_count + last_sent_resp_count - 1)]
        else:
            # Fallback: should not happen in normal flow, but send all if count decreased (e.g., after rollback)
            response_msgs = [serialize_message(m, history_count + i) for i, m in enumerate(responses)]
        
        # Update tracking for next call (only if update_counter is True)
        if update_counter:
            session['_last_sent_resp_count'] = len(responses) if responses else 0
        
        orch_agent = get_agent()
        max_tokens = get_agent_max_tokens(orch_agent)
        
        if cached_h_stats is None:
            # Try session-level cache first (avoids full re-tokenization)
            hist_count = len(active_h)
            cached_hist_count = session.get('_cached_hist_stats_count', -1)
            if hist_count <= cached_hist_count and '_cached_hist_stats' in session:
                h_stats = session['_cached_hist_stats'].copy()
            else:
                # Use the raw active history just like build_state, matching base.py's "ALL tokens"
                h_stats = get_history_stats(active_h)
                session['_cached_hist_stats'] = h_stats.copy()
                session['_cached_hist_stats_count'] = len(active_h)
        else:
            h_stats = cached_h_stats
            
        # FIX1: Only recompute full response stats when new messages are added, not during streaming growth.
        # But we do estimate the tokens of the growing message to keep the UI responsive!
        resp_len_stats = len(responses) if responses else 0
        if resp_len_stats > session.get('_last_resp_len_stats', 0):
            # New message(s) added — compute full stats via tiktoken
            r_stats = get_history_stats(responses)
            session['_last_resp_len_stats'] = resp_len_stats
            # We cache the stats of ALL PREVIOUS messages, excluding the current growing one
            prev_responses = responses[:-1] if len(responses) > 1 else []
            session['_cached_r_stats'] = get_history_stats(prev_responses)
            # Reset content length baseline — old message's length is no longer relevant
            session['_last_resp_content_len'] = 0
        else:
            # Same messages, just growing text — use cached stats + quick estimate for the last message
            cached_r_stats = session.get('_cached_r_stats', {'tokens': 0, 'words': 0})
            if responses:
                last_msg = responses[-1]
                content = last_msg.get('content', '') if isinstance(last_msg, dict) else getattr(last_msg, 'content', '')
                if isinstance(content, list):
                    content = " ".join([str(item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')) for item in content])
                else:
                    content = str(content)
                # FIX: Only estimate the delta (new chars since last tick) to avoid cumulative drift.
                # Previously this added the full content length each tick on top of cached_r_stats,
                # causing token counts to inflate by hundreds over long streaming sessions.
                prev_len = session.get('_last_resp_content_len', 0)
                delta_len = len(content) - prev_len
                session['_last_resp_content_len'] = len(content)
                if delta_len > 0:
                    est_words = max(1, delta_len // 5)  # approx 5 chars per word
                    est_tokens = max(1, delta_len // 4)  # approx 4 chars per token
                    r_stats = {
                        'tokens': cached_r_stats['tokens'] + est_tokens,
                        'words': cached_r_stats['words'] + est_words
                    }
                else:
                    r_stats = cached_r_stats.copy()
            else:
                r_stats = cached_r_stats.copy()

        return {
            'history_count': history_count,
            'response_messages': response_msgs,
            'sub_agents': sub_agents,  # None means "no update this tick" — frontend reuses cached state
            'active_stack': get_active_stack(),
            'approvals': get_approvals(),
            'generating': True,
            'total_tokens': h_stats['tokens'] + r_stats['tokens'],
            'total_words': h_stats['words'] + r_stats['words'],
            'max_tokens': get_agent_max_tokens(orch_agent),
            'current_model': getattr(orch_agent.llm, 'model', 'Unknown') if hasattr(orch_agent, 'llm') and orch_agent.llm else 'Unknown',
            'telemetry': telemetry,
        }

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
            except Exception:
                ws_connections.discard(conn)

    # ── Agent execution thread ────────────────────────────────────────────

    def run_agent_thread(history_for_agent, agent_runner, gen_id, loop):
        """
        Runs agent.run() in a background thread.
        Pushes state updates onto the async send_queue.
        """
        # Set event loop reference for dismissal callback to use
        if agent_pool:
            agent_pool._ws_loop = loop
        
        try:
            with session_lock:
                session['generating'] = True
            if agent_pool:
                agent_pool.stopped = False
                if hasattr(agent_pool, 'active_stack'):
                    agent_pool.active_stack.clear()

            if hasattr(agent_runner, 'session_name'):
                agent_runner.session_name = session['session_name']

            # Inject ui sampling params securely
            ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))
            
            def sanitize_cfg(cfg: dict):
                floats = ['temperature', 'top_p', 'presence_penalty', 'frequency_penalty', 'repetition_penalty', 'repeat_penalty', 'repeatPenalty', 'min_p']
                ints = ['max_tokens', 'max_completion_tokens', 'top_k', 'seed', 'max_input_tokens', 'max_turns', 'read_file_limit', 'tool_result_max_chars', 'grep_char_limit', 'shell_char_limit', 'code_char_limit']
                new_cfg = {}
                for k, v in cfg.items():
                    try:
                        if k in floats and v is not None:
                            new_cfg[k] = float(v)
                        elif k in ints and v is not None:
                            new_cfg[k] = int(float(v))
                        else:
                            new_cfg[k] = v
                    except (ValueError, TypeError):
                        new_cfg[k] = v
                if 'repeat_penalty' in new_cfg:
                    pen = new_cfg['repeat_penalty']
                    new_cfg['repetition_penalty'] = pen
                    new_cfg['repeatPenalty'] = pen
                if 'maxTokens' in new_cfg:
                    new_cfg['max_tokens'] = new_cfg.pop('maxTokens')
                return new_cfg

            ui_cfg = sanitize_cfg(ui_cfg)
            mcp_servers = ui_cfg.get('mcpServers')
            disabled_tools = ui_cfg.get('disabled_tools')
            work_access_folders = ui_cfg.get('work_access_folders')

            # Keys that should not be passed to the underlying LLM chat API
            NON_LLM_KEYS = (
                'max_auto_rollbacks', 'auto_rollback_on_loop', 'auto_continue', 
                'max_turns', 'mcpServers', 'work_access_folders', 'seed',
                'tool_result_max_chars', 'grep_char_limit', 'grep_spillover', 'shell_char_limit', 'code_char_limit',
                'disabled_tools'
            )

            if work_access_folders is not None and agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                # Legacy format: work_access_folders is treated as RW folders (no RO)
                agent_pool.operation_manager.set_extra_work_folders([], work_access_folders)

            has_llm = hasattr(agent_runner, 'llm') and agent_runner.llm
            old_cfg = None
            if has_llm:
                old_cfg = copy.deepcopy(agent_runner.llm.generate_cfg)
                agent_runner.llm.generate_cfg.pop('mcpServers', None)
                pure_llm_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
                agent_max_turns = ui_cfg.get('max_turns')
                agent_auto_continue = ui_cfg.get('auto_continue')

                # Remove any existing keys that should not be passed to LLM
                for key in NON_LLM_KEYS:
                    agent_runner.llm.generate_cfg.pop(key, None)

                agent_runner.llm.generate_cfg.update(pure_llm_cfg)
                if disabled_tools is not None:
                    agent_runner.llm.generate_cfg['disabled_tools'] = disabled_tools
                
                # Propagate tool limits to the agent runner for the orchestrator to use
                tool_result_max_chars = ui_cfg.get('tool_result_max_chars')
                if tool_result_max_chars is not None:
                    agent_runner.tool_result_max_chars = tool_result_max_chars
                
                if agent_pool:
                    agent_pool.update_llm_cfg(ui_cfg)
                if agent_max_turns is not None:
                    agent_runner.max_turns = agent_max_turns
                if agent_auto_continue is not None:
                    agent_runner.auto_continue_enabled = agent_auto_continue

            # Load MCP tools if requested
            if mcp_servers:
                try:
                    from agent_cascade.tools.mcp_manager import MCPManager
                    mcp_tools = MCPManager().initConfig({'mcpServers': mcp_servers})
                    for tool in mcp_tools:
                        for agent_inst in agents:
                            if tool.name not in agent_inst.function_map:
                                agent_inst.function_map[tool.name] = tool
                except Exception as e:
                    logger.warning(f"[MCP] Failed to initialize MCP tools: {e}")

            # ── Retry Loop for Auto-Rollback ──
            retry_count = 0
            max_auto_retries = ui_cfg.get('max_auto_rollbacks', 3)
            # -1 means infinity
            if max_auto_retries == -1:
                max_auto_retries = 999999
            
            auto_rollback_enabled = ui_cfg.get('auto_rollback_on_loop', True)
            current_history = history_for_agent # Start with the copy provided

            # ── Telemetry: Record turn start with config fingerprint ──
            _telem = agent_pool.telemetry if agent_pool and hasattr(agent_pool, 'telemetry') else None
            try:
                if _telem:
                    from telemetry import TelemetryCollector
                    _model_name = getattr(agent_runner.llm, 'model', 'unknown') if has_llm else 'unknown'
                    _tool_names = list(agent_runner.function_map.keys()) if hasattr(agent_runner, 'function_map') else []
                    _sys_prompt = ''
                    if current_history and current_history[0]:
                        first_msg = current_history[0]
                        if isinstance(first_msg, dict):
                            if first_msg.get(ROLE) == SYSTEM:
                                _sys_prompt = str(first_msg.get(CONTENT, '') or '')[:2000]
                        elif hasattr(first_msg, 'role'):
                            if getattr(first_msg, 'role', None) == SYSTEM:
                                _sys_prompt = str(getattr(first_msg, 'content', '') or '')[:2000]
                    _cfg_fp = TelemetryCollector.fingerprint_config(
                        model=_model_name,
                        generate_cfg=ui_cfg,
                        system_prompt=_sys_prompt,
                        tools=_tool_names,
                    )
                    _cfg_desc = TelemetryCollector.describe_config(
                        model=_model_name,
                        generate_cfg=ui_cfg,
                        tools=_tool_names,
                    )
                    _telem.record_turn_start(
                        session['session_name'],
                        config_fingerprint=_cfg_fp,
                        config_description=_cfg_desc,
                    )
            except Exception:
                pass  # Telemetry must never block agent execution
            
            while retry_count <= max_auto_retries:
                should_retry = False
                responses = []
                session['_last_resp_len'] = 0
                session['_last_resp_sig'] = ''  # Reset content signature tracker
                session['_last_sent_resp_count'] = 0  # Reset delta counter for clean state on retry
                session.pop('_last_sa_msg_counts', None)  # Reset sub-agent message counts on retry
                last_send = 0
                tick_num = 0
                prev_responses_len = 0  # Track previous response length for loop detection
                
                # Capture snapshots of sub-agent states before starting the run
                pool_snapshots = {}
                if agent_pool:
                    pool_snapshots = agent_pool.capture_snapshots()

                # Pre-compute history stats for streaming updates (based on active slice)
                # Also use as the sliced working set — no need to call twice
                working_history = agent_pool.slice_history_for_llm(current_history) if agent_pool else current_history
                cached_h_stats = get_history_stats(working_history)
                sub_agents_cache = None

                try:
                    
                    # LLM safe config: filter out UI-only or Orchestrator-specific keys
                    llm_safe_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}

                    # Run the agent on the working set
                    for partial in agent_runner.run(working_history, **llm_safe_cfg):
                        if session['stop_requested'] or session['generation_id'] != gen_id:
                            if agent_pool:
                                agent_pool.stopped = True
                            break

                        responses = partial
                        now = time.time()
                        
                        current_stack = list(get_active_stack())
                        stack_changed = (current_stack != getattr(agent_pool, '_last_seen_stack', None))
                        
                        # Detect if a new message was added or a tool is being called/returned
                        resp_len = len(responses)
                        last_resp_len = session.get('_last_resp_len', 0)
                        len_changed = (resp_len != last_resp_len)
                        
                        has_tool_event = False
                        if resp_len > 0:
                            last_m = responses[-1]
                            has_tool_event = _get_msg_func_call(last_m) or _get_msg_role(last_m) == FUNCTION
                        
                        # Track content signature to detect actual changes (prevents duplicate sends on time-only triggers)
                        # Signature combines message count + last message content length
                        # Performance rationale: full serialization/hash is too expensive for 0.15s tick intervals,
                        # but count+length catches the common case of new messages or growing content
                        if responses and resp_len > 0:
                            last_msg = responses[-1]
                            # Defensive: handle malformed entries where last_msg might be None
                            if not last_msg:
                                current_sig = f"{resp_len}:0"
                            else:
                                content = last_msg.get('content', '') if isinstance(last_msg, dict) else getattr(last_msg, 'content', '')
                                if isinstance(content, list):
                                    content_len = sum(len(item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')) for item in content)
                                else:
                                    content_len = len(str(content))
                                current_sig = f"{resp_len}:{content_len}"
                        else:
                            current_sig = "0:0"
                        last_sig = session.get('_last_resp_sig', '')
                        content_changed = (current_sig != last_sig)

                        if now - last_send > 0.15 or stack_changed or len_changed or has_tool_event or content_changed:
                            session['_last_resp_len'] = resp_len
                            session['_last_resp_sig'] = current_sig  # Track signature for next comparison
                            
                            # Sub-agent state update strategy:
                            # - On stack changes or tool events: force a refresh immediately.
                            # - When sub-agents are active: check if anything changed before computing state.
                            #   Only call get_sub_agent_state when at least one agent's message count differs,
                            #   OR the last message of an active agent has grown (streaming text).
                            #   This avoids O(N*M) work on ticks where nothing happened.
                            any_sa_active = any(sa.get('active') for sa in agent_pool.sub_agent_state.values()) if (agent_pool and hasattr(agent_pool, 'sub_agent_state')) else False
                            
                            # Quick change detection: compare current per-agent message counts + last msg sizes
                            _sa_changed = stack_changed or has_tool_event
                            if not _sa_changed and any_sa_active and tick_num % 2 == 0:
                                # Check if any sub-agent's message count or last message size changed since last refresh
                                _last_sa_counts = session.get('_last_sa_msg_counts', {})
                                _cur_sa_counts = {}
                                for sname, sstate in agent_pool.sub_agent_state.items():
                                    msgs = sstate.get('messages', [])
                                    msg_count = len(msgs)
                                    # Also track the last active agent's last message size to detect streaming growth
                                    is_waiting = sstate.get('is_waiting', False)
                                    is_halted = sstate.get('is_halted', False)
                                    has_queued = sstate.get('has_queued_messages', False)
                                    if sstate.get('active') and msgs:
                                        last_msg = msgs[-1]
                                        content = last_msg.get('content', '') if isinstance(last_msg, dict) else getattr(last_msg, 'content', '')
                                        _cur_sa_counts[sname] = (msg_count, len(content), is_waiting, is_halted, has_queued)
                                    else:
                                        _cur_sa_counts[sname] = (msg_count, 0, is_waiting, is_halted, has_queued)
                                if _cur_sa_counts != _last_sa_counts:
                                    _sa_changed = True
                                    session['_last_sa_msg_counts'] = _cur_sa_counts
                            
                            # Force recompute sub-agent state every 20 ticks to prevent staleness from missed change detection.
                            # The _sa_changed flag relies on message count/content length tracking, which can miss some changes.
                            # Every 100 iterations we force a full sub-agent state update (streaming=False) 
                            # to ensure any missed partial messages are eventually recovered.
                            force_full = (tick_num % 100 == 0)
                            if _sa_changed or any_sa_active or tick_num % 20 == 0:
                               sub_agents_cache = get_sub_agent_state(streaming=(not force_full))
                               if agent_pool:
                                     agent_pool._last_seen_stack = current_stack
                            
                            # Throttle telemetry to ~3s (every 20 ticks) to keep it lightweight
                            _telem_payload = None
                            if tick_num % 20 == 0:
                                _telem_payload = _safe_get_telemetry()

                            # UI expects history count to match current_history
                            delta = build_stream_update(responses, cached_h_stats=cached_h_stats, sub_agents=sub_agents_cache, telemetry=_telem_payload)
                            # Override history_count in delta for consistency (unless show_active_only is enabled)
                            if not ui_cfg.get('show_active_only', False):
                                delta['history_count'] = len(current_history)
                            
                            asyncio.run_coroutine_threadsafe(
                                send_queue.put({'type': 'stream_update', **delta}), loop
                            )
                            last_send = now
                            
                            # Loop Detection — throttled to every 10th tick to reduce overhead
                            if tick_num % 10 == 0:
                                # Check only NEW messages since last iteration to avoid false positives from
                                # accumulated responses lists. agent_runner.run() yields accumulating lists, so
                                # each iteration's 'responses' contains all previous messages plus new ones.
                                new_responses = responses[prev_responses_len:] if len(responses) > prev_responses_len else []
                                if new_responses:  # Only check when there are actually new messages
                                    loop_info = detect_loop(current_history + new_responses)
                                    if loop_info:
                                        loop_reason, pop_count = loop_info
                                        # Raise LoopDetectedError to be caught by outer try/except for cleaner state management.
                                        # This matches the approach used in agent_orchestrator.py for consistency.
                                        # FIX: The original code did `del new_responses[-refined_pop:]` which deleted from a slice copy,
                                        # not the original responses list, causing rollback to fail silently.
                                        raise LoopDetectedError(
                                            reason=loop_reason,
                                            agent_name='Orchestrator',
                                            pop_count=pop_count,
                                            turn_pop_count=len(responses),
                                            resp_snapshot=list(responses)
                                        )
                            
                                prev_responses_len = len(responses)  # Track for next iteration

                            tick_num += 1
                except Exception as e:
                    if isinstance(e, LoopDetectedError):
                        loop_reason = e.reason
                        agent_name = e.agent_name
                        pop_count = e.pop_count
                        turn_pop_count = getattr(e, 'turn_pop_count', 0)
                        is_sub_agent = agent_pool and agent_name in agent_pool.instance_conversations
                        
                        if auto_rollback_enabled and retry_count < max_auto_retries:
                            logger.warning(f"Loop detected for {agent_name}: {loop_reason}. Surgical rollback enabled (Retry {retry_count+1}/{max_auto_retries}).")
                            
                            # 1. Surgical Rollback
                            if is_sub_agent:
                                # Sub-agent loop that exhausted internal retries.
                                # The internal retry loop in _stream_sub_agent_call already
                                # performed surgical rollbacks on the sub-agent's conv.
                                # Do NOT rollback again — just inject hints below.
                                logger.info(f"Sub-agent {agent_name} loop escalated to main. Internal retries already rolled back sub-agent history.")
                            elif agent_pool and pop_count:
                                # Orchestrator itself looped — rollback main history.
                                # pop_count is relative to the orchestrator's messages (which
                                # includes uncommitted turn output), so subtract turn_pop_count.
                                main_pop = max(0, pop_count - turn_pop_count)
                                if main_pop > 0:
                                    refined_pop = _refine_pop_count(current_history, main_pop)
                                    if len(current_history) >= refined_pop:
                                        del current_history[-refined_pop:]
                                        logger.info(f"Surgically rolled back main history by {refined_pop} messages.")
                                        
                                        # Record soft rollback in orchestrator log
                                        orch_logger = agent_pool.get_logger(session['session_name'], 'Orchestrator')
                                        orch_logger.rollback(refined_pop, soft=True, reason=loop_reason)
                                        
                                        # FIX: Reset delta counter after rollback to force full re-send on next tick
                                        # This prevents stale counter from causing messages to be dropped or duplicated
                                        session['_last_sent_resp_count'] = 0
                            elif agent_pool:
                                # Fallback to snapshots if pop_count is missing
                                agent_pool.rollback_to_snapshots(pool_snapshots, soft=True, reason=loop_reason)
                                
                            # 1b. Inject a hint directly into the sub-agent's history so it knows it looped
                            if is_sub_agent:
                                sub_hint = f"[SYSTEM]: Your last actions resulted in a repetitive loop ({loop_reason}). Please try a different approach to solve the task."
                                agent_pool.instance_conversations[agent_name].append({ROLE: USER, CONTENT: sub_hint})
                                
                            # 2. Inject hint into main orchestrator history
                            loop_hint = f"[SYSTEM]: A repetitive loop was detected for {agent_name} ({loop_reason}). Please try a different approach."
                            current_history.append({ROLE: USER, CONTENT: loop_hint})
                            if agent_pool:
                                agent_pool.instance_conversations[session['session_name']].append({ROLE: USER, CONTENT: loop_hint})
                            
                            # 3. Notify UI that we are retrying
                            asyncio.run_coroutine_threadsafe(
                                send_queue.put({
                                    'type': 'error', 
                                    'message': f"🔄 Loop detected for {agent_name}. Surgically rolling back and retrying ({retry_count+1}/{max_auto_retries})..."
                                }), loop
                            )
                            
                            should_retry = True
                            session['stop_requested'] = False
                        else:
                            logger.warning(f"Loop detected for {agent_name}: {loop_reason}. Stopping generation.")
                            if agent_pool:
                                # For sub-agents, we should NOT blunt-rollback to start of turn
                                # if we already have surgical information or if it's already rolled back.
                                if is_sub_agent:
                                    logger.info(f"Sub-agent {agent_name} loop stop: keeping surgical state.")
                                    # We still pop the last orchestrator message if it was the call that failed
                                    if current_history:
                                        current_history.pop()
                                else:
                                    agent_pool.rollback_to_snapshots(pool_snapshots, soft=True, reason=loop_reason)
                                    if current_history:
                                        current_history.pop()
                            responses = []
                            session['stop_requested'] = True
                            if agent_pool:
                                agent_pool.stopped = True
                            
                            asyncio.run_coroutine_threadsafe(
                                send_queue.put({
                                    'type': 'error', 
                                    'message': f"🔄 {loop_reason} in {agent_name}. The agent has been stopped. History rolled back."
                                }), loop
                            )
                    else:
                        traceback.print_exc()
                        asyncio.run_coroutine_threadsafe(
                            send_queue.put({'type': 'error', 'message': f"Generation error: {str(e)}"}), loop
                        )

                if should_retry:
                    retry_count += 1
                    continue
                else:
                    break

            # ── Finalize ──
            if session['generation_id'] != gen_id:
                return

            # If we retried or rolled back, or if a tool like compress_context mutated the pool,
            # we MUST sync session['history'] from the authoritative pool state.
            with session_lock:
                if agent_pool and session['session_name'] in agent_pool.instance_conversations:
                    session['history'] = copy.deepcopy(agent_pool.instance_conversations[session['session_name']])
                else:
                    session['history'] = current_history

                if agent_pool:
                    # CRITICAL: Sync back to pool so tools like CompressionTool see the current history
                    agent_pool.instance_conversations[session['session_name']] = session['history']

            if hasattr(agent_runner, 'turn_final_messages') and agent_runner.turn_final_messages:
                tfm = agent_runner.turn_final_messages
                
                # Check if this is just a sliced view (starts with SYSTEM + summary marker)
                is_slice = False
                if len(tfm) > 0 and len(tfm) < len(session['history']):
                    # Robust check: if ANY of the first few messages contain the compression marker, it's a slice.
                    # This handles cases where System messages might be duplicated or shifted.
                    from agent_cascade.prompts.dna import COMPRESSION_MARKER
                    for m in tfm[:5]:
                        content = m.get(CONTENT, '') if isinstance(m, dict) else getattr(m, 'content', '')
                        if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                            is_slice = True
                            break
                    
                    # Fallback: if we just started a turn and the number of messages returned 
                    # matches our working set (plus any new responses), it's likely a slice.
                    if not is_slice and 'working_history' in locals():
                        if len(tfm) >= len(working_history):
                            is_slice = True

                if not is_slice and len(tfm) < len(session['history']):
                    logger.info(f"Syncing history from agent state ({len(tfm)} vs {len(session['history'])} messages).")
                    session['history'].clear()
                    for res in tfm:
                        msg = res.model_dump() if hasattr(res, 'model_dump') else (res if isinstance(res, dict) else {})
                        # FIX: Keep system message to maintain consistency with pool conversation.
                        # Previously stripped at line 1356, but this caused inconsistency where
                        # session['history'] lost the system message while pool kept it.
                        # This triggered full context reprocessing when slice_history_for_llm()
                        # prepended a new system message reference to the working set.
                        session['history'].append(msg)
                agent_runner.turn_final_messages = None

            _save_session_history()

            # ── Telemetry: Record turn end ──
            try:
                if _telem:
                    _telem.record_turn_end(session['session_name'])
            except Exception:
                pass

            final = build_state(generating=False)
            halted = agent_pool.is_halted(session['session_name']) if (agent_pool and hasattr(agent_pool, 'is_halted')) else False
            asyncio.run_coroutine_threadsafe(send_queue.put({'type': 'done', **final, 'instance_halted': halted}), loop)

        except Exception as e:
            traceback.print_exc()
            asyncio.run_coroutine_threadsafe(send_queue.put({'type': 'error', 'message': str(e)}), loop)
        finally:
            with session_lock:
                session['generating'] = False
                session['stop_requested'] = False
            # FIX1: Reset cached response stats so next generation starts fresh
            session.pop('_last_resp_len_stats', None)
            session.pop('_cached_r_stats', None)
            # FIX3: Invalidate history stats caches — messages may have been added/removed during run
            session.pop('_cached_hist_stats', None)
            session.pop('_cached_hist_stats_count', None)
            # FIX2: Invalidate sub-agent stats caches — their histories may have changed
            for key in list(session.keys()):
                if key.startswith('_sa_stats_'):
                    session.pop(key, None)
            # FIX4: Clean up content signature tracker to prevent stale state
            session.pop('_last_resp_sig', None)
            # FIX5: Clean up response length and sent count trackers for cross-generation safety
            session.pop('_last_resp_len', None)
            session.pop('_last_sent_resp_count', None)
            session.pop('_last_resp_content_len', None)
            # FIX6: Clean up sub-agent message counts tracker to prevent staleness between generations
            session.pop('_last_sa_msg_counts', None)
            if agent_pool:
                agent_pool.stopped = False
            if has_llm and old_cfg:
                agent_runner.llm.generate_cfg = old_cfg

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
                UI-initiated dismissals have their own direct broadcast in terminate_sub_agent handler.
                """
                ws_loop = getattr(agent_pool, '_ws_loop', None)
                if ws_loop and not ws_loop.is_closed() and send_queue:
                    try:
                        msg = {'type': 'dismissal', 'instance_name': instance_name}
                        asyncio.run_coroutine_threadsafe(send_queue.put(msg), ws_loop)
                    except Exception:
                        pass  # Never let callback errors disrupt agent execution
            
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
                logger.error(f"Sender loop exception: {e}")
    
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
            except Exception:
                pass

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
        session['history'] = []
        _save_session_history() # Ensure persistent file is cleared
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
            except Exception:
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
        if agent_pool and hasattr(agent_pool, 'telemetry'):
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
        if agent_pool and hasattr(agent_pool, 'telemetry'):
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
        
        from api_router import APIEndpoint
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
        nonlocal root_agent
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
                except json.JSONDecodeError:
                    continue

                msg_type = data.get('type', '')

                # ── Send message / async inject ──
                if msg_type == 'message':
                    text = data.get('text', '').strip()
                    if not text:
                        continue

                    target_agent = data.get('target_agent')
                    if session['generating']:
                        # Async injection while agent is running — route to target agent
                        if agent_pool:
                            target = target_agent or session.get('session_name', 'Maine')
                            agent_pool.enqueue_message(target, text)
                        continue

                    # Route normal messages to the selected sub-agent if one is active.
                    if target_agent and target_agent != session.get('session_name', 'Maine') and agent_pool and hasattr(agent_pool, 'sub_agent_state') and target_agent in agent_pool.sub_agent_state:
                        agent_pool.enqueue_message(target_agent, text)
                        await broadcast({'type': 'state', **build_state()})
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
                            except ValueError:
                                pass
                        
                        # Rollback N messages
                        for _ in range(n):
                            if session['history']:
                                session['history'].pop()
                        
                        # Record soft rollback in orchestrator log to keep internal state in sync
                        if agent_pool:
                            try:
                                agent_runner_for_log = get_agent()
                                main_logger = agent_pool.get_logger(session['session_name'], agent_runner_for_log.__class__.__name__)
                                main_logger.truncate_to(len(session['history']), soft=True, reason="Manual /rollback command")
                            except Exception:
                                pass
                            agent_pool.reset()
                        await broadcast({'type': 'state', **build_state()})
                        continue

                    # Add user message to history (parsed for multimodal items)
                    parsed_content = _parse_multimodal_content(text)
                    with session_lock:
                        session['history'].append({ROLE: USER, CONTENT: parsed_content})

                    # Start agent generation
                    with session_lock:
                        session['stop_requested'] = False
                    if agent_pool:
                        agent_pool.stopped = False
                        # Sync history to pool so tools can see it
                        agent_pool.instance_conversations[session['session_name']] = session['history']
                    
                    session['generation_id'] += 1
                    gen_id = session['generation_id']
                    agent_runner = get_agent()
                    history_copy = copy.deepcopy(session['history'])
                    loop = asyncio.get_event_loop()

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
                            # Inject continuation and start fresh generation
                            cont_msg = "[SYSTEM]: You were paused. Please continue from where you left off."
                            parsed_content = _parse_multimodal_content(cont_msg)
                            with session_lock:
                                session['history'].append({'role': USER, 'content': parsed_content})
                            _save_session_history()
                            
                            # Start agent generation
                            with session_lock:
                                session['stop_requested'] = False
                            if agent_pool:
                                agent_pool.stopped = False
                                agent_pool.instance_conversations[session['session_name']] = session['history']
                                
                                # ── Fix 3: Restore sub-agent pools from JSONL logs if corrupted ──
                                # After a failed forced compression cycle, sub-agent pools may be empty/corrupted.
                                # Read directly from log files on disk to recover.
                                try:
                                    # Import validate_message_pool locally (defined in agent_orchestrator.py)
                                    from agent_orchestrator import validate_message_pool
                                    
                                    for sa_name in list(agent_pool.instance_classes.keys()):
                                        if sa_name == session['session_name']:
                                            continue  # Already synced above
                                        
                                        try:
                                            agent_class = agent_pool.instance_classes.get(sa_name, '')
                                            
                                            # Check if current pool data is valid before restoring
                                            current_pool_data = agent_pool.instance_conversations.get(sa_name, [])
                                            if validate_message_pool(current_pool_data, sa_name):
                                                continue  # Pool is fine, no need to restore
                                            
                                            # Find the actual log file via existing logger or glob
                                            recov = []
                                            logger_inst = agent_pool.instance_loggers.get(sa_name)
                                            
                                            if logger_inst and hasattr(logger_inst, 'log_path') and logger_inst.log_path:
                                                actual_log_path = logger_inst.log_path
                                            else:
                                                # Search for the most recent log file matching this sub-agent
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
                                                        except json.JSONDecodeError:
                                                            continue
                                            
                                            # Only overwrite pool if recovered data is valid
                                            if recov and validate_message_pool(recov, sa_name):
                                                logger.info(
                                                    f"Restoring sub-agent {sa_name} pool from log during resume "
                                                    f"({len(recov)} messages)"
                                                )
                                                agent_pool.instance_conversations[sa_name] = copy.deepcopy(recov)
                                            else:
                                                logger.warning(
                                                    f"Could not restore sub-agent {sa_name} pool — "
                                                    f"no valid recovery data found in logs"
                                                )
                                        except Exception as _e:
                                            # Single agent failure shouldn't block resume for others
                                            logger.warning(f"Failed to restore sub-agent {sa_name} pool: {_e}")
                                except ImportError:
                                    logger.warning("validate_message_pool not available — skipping sub-agent pool restoration")
                                # ── End Fix 3 ──
                            
                            session['generation_id'] += 1
                            gen_id = session['generation_id']
                            agent_runner = get_agent()
                            history_copy = copy.deepcopy(session['history'])
                            loop = asyncio.get_event_loop()

                            thread = threading.Thread(
                                target=run_agent_thread,
                                args=(history_copy, agent_runner, gen_id, loop),
                                daemon=True,
                            )
                            thread.start()

                            await broadcast({'type': 'state', **build_state(generating=True)})
                        elif not is_generating:
                            # Not halted and not generating — just update UI state (no-op from user's perspective)
                            await broadcast({'type': 'state', **build_state()})
                    
                    # For sub-agents: inject a continuation message into their queue so when 
                    # the orchestrator next calls them, they continue from where they left off
                    elif target_instance != session['session_name'] and agent_pool and was_halted:
                        cont_msg = f"[SYSTEM]: Agent {target_instance} was paused. Please continue from where you left off."
                        agent_pool.enqueue_message(target_instance, cont_msg)
                        logger.info(f"Injected continuation message into sub-agent {target_instance}'s queue.")

                elif msg_type == 'terminate_sub_agent':
                    instance_name = data.get('instance_name')
                    if instance_name and agent_pool:
                        agent_pool.dismiss_instance(instance_name)
                    # Force immediate state broadcast to update UI (remove tab)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'retry':
                    if session['generating']:
                        continue
                    # Remove trailing assistant/function messages
                    while (session['history']
                           and _get_msg_role(session['history'][-1]) in (ASSISTANT, FUNCTION)):
                        session['history'].pop()

                    # Roll back one more (the user message) to allow a clean re-trigger
                    # and ensure consistency with the last_turn_snapshots.
                    last_user_msg = None
                    if session['history'] and _get_msg_role(session['history'][-1]) == USER:
                        last_user_msg = session['history'].pop()

                    if not session['history'] and not last_user_msg:
                        _save_session_history()
                        await broadcast({'type': 'state', **build_state()})
                        continue
                    
                    _save_session_history()
                    if agent_pool:
                        # Clear active tools/agent stack since we are retrying from the main input level
                        agent_pool.active_stack.clear()
                        agent_pool.last_tool_args.clear()

                        # 1. Rollback sub-agents to the start of the last turn
                        if session.get('last_turn_snapshots'):
                            agent_pool.rollback_to_snapshots(session['last_turn_snapshots'], soft=True, reason="User retry")
                            
                            # Sync sub_agent_state so build_state() sees the rolled-back histories
                            # (sub_agent_state[name]['messages'] is a deep copy and won't reflect truncation)
                            for name in session['last_turn_snapshots']:
                                if name != session['session_name'] and name in agent_pool.sub_agent_state:
                                    agent_pool.sub_agent_state[name]['messages'] = list(agent_pool.instance_conversations.get(name, []))
                        
                        # 2. Rollback the main orchestrator log to match the shortened history
                        # This now points to the state before the user message we just popped.
                        try:
                            agent_runner_for_log = get_agent()
                            main_logger = agent_pool.get_logger(session['session_name'], agent_runner_for_log.__class__.__name__)
                            main_logger.truncate_to(len(session['history']), soft=True, reason="User retry")
                        except Exception:
                            pass
                    
                    # Now "send it again": re-append the user message.
                    # This ensures the agent has the correct input to respond to, 
                    # but the system state is now cleanly positioned as if the message was just sent.
                    if last_user_msg:
                        session['history'].append(last_user_msg)
                        # Re-log it to keep the persistent log file in sync with history
                        try:
                            agent_runner_for_log = get_agent()
                            main_logger = agent_pool.get_logger(session['session_name'], agent_runner_for_log.__class__.__name__)
                            main_logger.log_message(last_user_msg)
                        except Exception:
                            pass
                    if 'generate_cfg' in data:
                        session['generate_cfg'] = data['generate_cfg']

                    with session_lock:
                        session['stop_requested'] = False
                        session['generation_id'] += 1
                    if agent_pool:
                        agent_pool.stopped = False
                    gen_id = session['generation_id']
                    agent_runner = get_agent()
                    history_copy = copy.deepcopy(session['history'])
                    loop = asyncio.get_event_loop()

                    thread = threading.Thread(
                        target=run_agent_thread,
                        args=(history_copy, agent_runner, gen_id, loop),
                        daemon=True,
                    )
                    thread.start()
                    await broadcast({'type': 'state', **build_state(generating=True)})

                elif msg_type == 'reset':
                    with session_lock:
                        session['history'] = []
                        session['generating'] = False
                        session['stop_requested'] = False
                        session['generation_id'] += 1
                    _save_session_history()
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
                        if 'work_access_folders_ro' in ui_cfg or 'work_access_folders_rw' in ui_cfg or 'work_access_folders' in ui_cfg:
                            if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                                ro = ui_cfg.get('work_access_folders_ro', [])
                                rw = ui_cfg.get('work_access_folders_rw', [])
                                # Support legacy 'work_access_folders' as RW for backward compatibility
                                legacy = ui_cfg.get('work_access_folders', [])
                                rw = list(set(rw + legacy))
                                agent_pool.operation_manager.set_extra_work_folders(ro, rw)
                        if 'default_workspace' in ui_cfg:
                            if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                                new_ws = ui_cfg['default_workspace']
                                if new_ws:
                                    agent_pool.operation_manager.set_base_dir(new_ws)
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
                                try:
                                    import platform
                                    import json
                                    import copy

                                    with app.security_check_lock:
                                        if not agent_pool.get_agent('Security'):
                                            agent_pool.load_agent('Security')
                                        sec_agent = agent_pool.get_agent('Security')
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
                                        
                                        # Register security advisor in sub_agent_state so it shows a tab
                                        sec_state_key = 'Security'
                                        agent_pool.sub_agent_state[sec_state_key] = {
                                            'active': True,
                                            'agent_name': f"Security",
                                            'messages': list(history),
                                        }
                                        agent_pool.instance_conversations[sec_state_key] = list(history)
                                        if sec_state_key not in agent_pool.active_stack:
                                            agent_pool.active_stack.append(sec_state_key)
                                        # Broadcast initial state so the tab appears immediately
                                        asyncio.run_coroutine_threadsafe(
                                            send_queue.put({'type': 'stream_update', **build_stream_update([], sub_agents=get_sub_agent_state(streaming=True), update_counter=False)}),
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
                    
                                        # Fix #1: Extract generator to close it on timeout (prevents resource leak)
                                        run_gen = sec_agent.run(history, agent_instance_name='Security', **llm_safe_cfg)
                                        
                                        # Schedule warning AFTER generator creation so timer is only created if gen succeeded
                                        def _sec_warning_injector():
                                            try:
                                                agent_pool.enqueue_message(
                                                    'Security',
                                                    "[SYSTEM WARNING] Your analysis is taking longer than expected. "
                                                    "Please provide a verdict as soon as possible — the approval request may timeout soon."
                                                )
                                            except Exception:
                                                pass  # Best-effort warning, don't fail the security check
                                        
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
                                                # Update sub_agent_state with current message history during streaming
                                                agent_pool.sub_agent_state[sec_state_key]['messages'] = list(history) + list(final_msgs) if isinstance(final_msgs, list) else [history[0]] + list(final_msgs)
                                                agent_pool.instance_conversations[sec_state_key] = list(agent_pool.sub_agent_state[sec_state_key]['messages'])
                                                # Only broadcast at start and end of security check (not every token)
                                                if sec_first_broadcast:
                                                    asyncio.run_coroutine_threadsafe(
                                                                                                         send_queue.put({'type': 'stream_update', **build_stream_update([], sub_agents=get_sub_agent_state(streaming=True), update_counter=False)}),
                                                        loop
                                                    )
                                                    sec_first_broadcast = False
                                        finally:
                                            # Cancel the warning timer if we finished before it fires
                                            sec_warning_timer.cancel()
                                            # Fix #1: Close the generator to abort any active LLM call / HTTP connection
                                            try:
                                                run_gen.close()
                                            except Exception:
                                                pass  # Best-effort close; don't fail security check if cleanup throws

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
                                        agent_pool.halt_instance('Security')
                                        
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
                                    # Always clean up security advisor state when done
                                    # sec_state_key is defined at function scope (as 'Security'), so direct reference is safe
                                    if sec_state_key and sec_state_key in agent_pool.sub_agent_state:
                                        agent_pool.sub_agent_state[sec_state_key]['active'] = False
                                        if sec_state_key in agent_pool.active_stack:
                                            agent_pool.active_stack.remove(sec_state_key)
                                    if hasattr(app, 'active_security_checks') and rid:
                                        with app.active_security_checks_lock:
                                            app.active_security_checks.discard(rid)
                            
                            threading.Thread(target=_security_check, daemon=True).start()

                elif msg_type == 'edit_message':
                    idx = data.get('index')
                    content = data.get('content', '')
                    target_name = data.get('instance_name') or session['session_name']
                    
                    history = []
                    if target_name == session['session_name']:
                        history = session['history']
                    elif agent_pool and target_name in agent_pool.instance_conversations:
                        history = agent_pool.instance_conversations[target_name]
                        
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
                            logger_inst = agent_pool.get_logger(target_name, 'Orchestrator' if target_name == session['session_name'] else 'SubAgent')
                            success = logger_inst.reset_history(history, rewrite=True)
                            if not success:
                                logger.error(f"Failed to write edited history to log for '{target_name}'")
                            
                            # Sync sub_agent_state so build_state() sees the edit
                            # (sub_agent_state[name]['messages'] may be a separate reference and won't
                            #  reflect in-place edits to instance_conversations)
                            if target_name != session['session_name'] and target_name in agent_pool.sub_agent_state:
                                agent_pool.sub_agent_state[target_name]['messages'] = list(history)
                            
                            # Also sync main session back to pool so next generation doesn't
                            # overwrite the edit (line 1180 does copy.deepcopy from pool)
                            if target_name == session['session_name']:
                                agent_pool.instance_conversations[target_name] = list(history)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'delete_messages':
                    if session['generating']:
                        continue
                    target_name = data.get('instance_name') or session['session_name']
                    
                    history = []
                    if target_name == session['session_name']:
                        history = session['history']
                    elif agent_pool and target_name in agent_pool.instance_conversations:
                        history = agent_pool.instance_conversations[target_name]
                        
                    indices = sorted(data.get('indices', []), reverse=True)
                    for idx in indices:
                        if 0 <= idx < len(history):
                            history.pop(idx)
                    if agent_pool:
                        logger_inst = agent_pool.get_logger(target_name, 'Orchestrator' if target_name == session['session_name'] else 'SubAgent')
                        success = logger_inst.reset_history(history, rewrite=True)
                        if not success:
                            logger.error(f"Failed to write deleted history to log for '{target_name}'")
                        
                        # Sync sub_agent_state so build_state() sees the deletion
                        if target_name != session['session_name'] and target_name in agent_pool.sub_agent_state:
                            agent_pool.sub_agent_state[target_name]['messages'] = list(history)
                        
                        # Also sync main session back to pool so next generation doesn't
                        # re-deep-copy the un-deleted version (line 1180 does copy.deepcopy from pool)
                        if target_name == session['session_name']:
                            agent_pool.instance_conversations[target_name] = list(history)
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'select_agent':
                    session['agent_index'] = int(data.get('index', 0))
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'set_root_agent_class':
                    root_agent_class = data.get('agent_class')
                    # Validate incoming agent class against available pool agents
                    valid = False
                    if root_agent_class and agent_pool:
                        try:
                            available = [a.lower() for a in agent_pool.list_agents()]
                            valid = root_agent_class.lower() in available
                        except Exception:
                            valid = False

                    if not valid:
                        # Reject invalid classes and inform clients, include current valid state
                        await broadcast({
                            'type': 'error',
                            'message': f'Unknown agent class: {root_agent_class}',
                            'current_root_agent_class': session.get('root_agent_class')
                        })
                    else:
                        session['root_agent_class'] = root_agent_class
                        if agent_pool and hasattr(agent_pool, 'instance_classes'):
                            agent_pool.instance_classes['Root'] = root_agent_class
                        if agent_pool and hasattr(agent_pool, 'sub_agent_state') and 'Root' in agent_pool.sub_agent_state:
                            agent_pool.sub_agent_state['Root']['agent_name'] = f"Root ({root_agent_class})"
                        
                        # Dynamically reload/update root_agent to use the new soul.md and configuration
                        if root_agent is not None and agent_pool:
                            try:
                                from agent_factory import prepare_root_agent
                                # Fetch the llm_cfg currently active
                                llm_cfg = getattr(agent_pool, 'llm_cfg', {})
                                old_func_map = root_agent.function_map  # Save shared tools before reload
                                new_root_agent = prepare_root_agent(agent_pool, llm_cfg, root_agent_class=root_agent_class)
                                # Merge shared tool references from old agent that aren't already registered.
                                # This preserves heavy shared singletons (ddg_search, code_interpreter, etc.)
                                # without overwriting the standard file tools that prepare_root_agent just set up.
                                for key, val in old_func_map.items():
                                    if key not in new_root_agent.function_map:
                                        new_root_agent.function_map[key] = val
                                root_agent = new_root_agent
                                logger.info(f"Successfully reloaded root_agent for class: {root_agent_class}")
                            except Exception as reload_err:
                                logger.error(f"Failed to reload root_agent for class {root_agent_class}: {reload_err}")
                                
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
                            # Successfully loaded. Update history in session
                            instance_name = session.get('session_name', 'Maine')
                            if instance_name in agent_pool.instance_conversations:
                                session['history'] = copy.deepcopy(agent_pool.instance_conversations[instance_name])
                                session['summary'] = agent_pool.instance_summaries.get(instance_name, "")
                                session['generating'] = False
                                session['stop_requested'] = False
                                if agent_pool:
                                    agent_pool.stopped = False

                                # CRITICAL: If restoring a Root session, sync the root agent and class
                                if instance_name == 'Root':
                                    loaded_class = agent_pool.instance_classes.get('Root')
                                    if loaded_class:
                                        session['root_agent_class'] = loaded_class
                                        try:
                                            from agent_factory import prepare_root_agent
                                            llm_cfg = getattr(agent_pool, 'llm_cfg', {})
                                            old_func_map = root_agent.function_map if root_agent is not None else {}
                                            new_root_agent = prepare_root_agent(agent_pool, llm_cfg, instance_name='Root', root_agent_class=loaded_class)
                                            # Merge shared tool references from old agent that aren't already registered
                                            for key, val in old_func_map.items():
                                                if key not in new_root_agent.function_map:
                                                    new_root_agent.function_map[key] = val
                                            root_agent = new_root_agent
                                            logger.info(f"Successfully loaded and re-prepared Root agent for class: {loaded_class}")
                                        except Exception as err:
                                            logger.error(f"Failed to prepare Root agent after loading session: {err}")

                                await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'inject':
                    text = data.get('text', '').strip()
                    target = data.get('target_agent') or session.get('session_name', 'Maine')
                    if text and agent_pool:
                        agent_pool.enqueue_message(target, text)

        except WebSocketDisconnect:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            ws_connections.discard(websocket)

    # ── Serve frontend static files ───────────────────────────────────────
    web_ui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web_ui')

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
                except PermissionError:
                    pass
                    
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
        file_path = os.path.join(web_ui_dir, path)
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
    from agent_pool import AgentPool

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
    
    agent_pool = AgentPool(
        llm_cfg=initial_llm_cfg, 
        workspace_dir=args.workspace,
        idle_timeout_seconds=idle_timeout,
        idle_check_interval=idle_check_interval,
    )
    
    # Pre-load agents
    orch_agent = agent_pool.get_instance('Maine', 'Orchestrator')
    
    use_root = os.getenv('QWEN_USE_ROOT_SUBAGENT', '1').lower() in ('1','true','yes','on')
    root_agent_class_env = os.getenv('QWEN_ROOT_AGENT_CLASS')
    root_agent = None
    if use_root:
        from agent_factory import prepare_root_agent
        root_agent = prepare_root_agent(agent_pool, initial_llm_cfg, root_agent_class=root_agent_class_env)
    
    app = create_app(agents=[orch_agent], agent_pool=agent_pool, root_agent=root_agent)
    
    logger.info("Starting AgentCascade API Server on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)
