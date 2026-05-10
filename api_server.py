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
from agent_cascade.log import logger
from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
from agent_cascade.utils.utils import extract_text_from_message, get_message_stats, get_history_stats, IMAGE_REGEX
from agent_cascade.prompts.dna import SECURITY_ADVISOR_PROMPT

try:
    from agent_cascade.agents.user_agent import PENDING_USER_INPUT
except ImportError:
    PENDING_USER_INPUT = 'PENDING_USER_INPUT'


def _parse_multimodal_content(text):
    """
    Parse markdown images ![alt](data:...) and return a list of content items.
    If no images are found, returns the original text.
    """
    pattern = r'!\[([^\]]*)\]\((data:image/[^;]+;base64,[a-zA-Z0-9+/=]+)\)'
    parts = []
    last_end = 0
    for match in re.finditer(pattern, text):
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
    



    return {'tokens': total_tokens, 'words': total_words}


def get_agent_max_tokens(agent) -> int:
    """Resolve the effective max_input_tokens from agent LLM config."""
    from agent_cascade.settings import DEFAULT_MAX_INPUT_TOKENS
    if hasattr(agent, 'llm') and hasattr(agent.llm, 'cfg'):
        cfg = agent.llm.cfg
        agent_max = cfg.get('generate_cfg', {}).get('max_input_tokens') or cfg.get('max_input_tokens')
        if agent_max:
            return int(agent_max)
    return DEFAULT_MAX_INPUT_TOKENS


def detect_loop(messages: List[dict]) -> Optional[Tuple[str, int]]:
    """
    Detect if the agent is stuck in a loop.
    Returns (reason, pop_count) if found, else None.
    """
    if len(messages) < 6:
        return None
    
    # Extract identifying features, ignoring SYSTEM messages
    def get_feature(m):
        role = m.get(ROLE)
        content = str(m.get(CONTENT, ''))
        reasoning = str(m.get('reasoning_content', ''))
        
        # If content is empty but reasoning is present (common in some agents), use reasoning as feature
        if not content and reasoning:
            content = reasoning
            
        fc = m.get('function_call')
        if fc:
            return f"{role}:{fc.get('name')}:{fc.get('arguments')}"
        return f"{role}:{content[:200]}" # Truncate for comparison

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
    if len(history) > 0 and history[0].get(ROLE) == SYSTEM:
        keep_at_least = 1
        if len(history) > 1 and history[1].get(ROLE) == USER:
            keep_at_least = 2
            
    removable = len(history) - keep_at_least
    if removable <= 0:
        return 0
        
    # Cap pop_count at removable length
    if new_pop > removable:
        new_pop = removable
        
    while new_pop < removable:
        start_idx = len(history) - new_pop
        if start_idx >= keep_at_least and history[start_idx].get(ROLE) == FUNCTION:
            new_pop += 1
        elif start_idx >= keep_at_least and history[start_idx].get(ROLE) == ASSISTANT and history[start_idx].get('function_call'):
            new_pop += 1
            break
        else:
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


# ─── App factory ──────────────────────────────────────────────────────────────

def create_app(agents, agent_pool, config=None):
    """
    Create the FastAPI application.

    Args:
        agents:     List of Agent objects (orchestrator first, then sub-agents)
        agent_pool: The AgentPool instance
        config:     Optional chatbot config dict
    """
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
                    match = re.search(r"<context_summary>\s*\n(.*?)\s*</context_summary>", content, re.DOTALL)
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
    }
    # Initial load
    session['history'], session['summary'] = _load_session_history(default_session_name)
    if agent_pool:
        agent_pool.instance_conversations[default_session_name] = session['history']
        agent_pool.instance_summaries[default_session_name] = session['summary']


    ws_connections: Set[WebSocket] = set()
    send_queue: asyncio.Queue = asyncio.Queue()



    def get_agent():
        idx = session['agent_index']
        if 0 <= idx < len(agents):
            return agents[idx]
        return agents[0]

    def get_sub_agent_state():
        result = {}
        if agent_pool and hasattr(agent_pool, 'sub_agent_state'):
            for name, state in agent_pool.sub_agent_state.items():
                msgs = state.get('messages', [])
                agent_class = state.get('agent_name', name)
                
                # Get max tokens for this agent class
                agent_template = agent_pool.get_agent(agent_class)
                max_tokens = get_agent_max_tokens(agent_template) if agent_template else 58000
                
                stats = get_history_stats(msgs)
                
                # Dynamically extract summary from messages if missing from tracker (e.g. after restart)
                summary = agent_pool.instance_summaries.get(name, "")
                if not summary:
                    for msg in reversed(msgs):
                        content = msg.get(CONTENT, '')
                        if isinstance(content, str) and "<context_summary>" in content:
                            import re
                            match = re.search(r"<context_summary>\s*\n(.*?)\s*</context_summary>", content, re.DOTALL)
                            if match:
                                summary = match.group(1).strip()
                            break
                
                result[name] = {
                    'active': state.get('active', False),
                    'agent_name': agent_class,
                    'messages': [serialize_message(m, i) for i, m in enumerate(msgs)],
                    'total_tokens': stats['tokens'],
                    'total_words': stats['words'],
                    'max_tokens': max_tokens,
                    'summary': summary
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
        
        # Optimize: History stats are cached, partial responses are calculated on the fly
        h_stats = get_history_stats(session['history'])
        r_stats = get_history_stats(responses) if responses else {'tokens': 0, 'words': 0}
        
        total_tokens = h_stats['tokens'] + r_stats['tokens']
        total_words = h_stats['words'] + r_stats['words']
        
        max_tokens = get_agent_max_tokens(orch_agent)

        # Sync session summary from history if it was just compressed
        current_summary = session.get('summary', '')
        for msg in reversed(session['history']):
            content = msg.get(CONTENT, '')
            if isinstance(content, str) and "<context_summary>" in content:
                import re
                # Only match content INSIDE the tags
                match = re.search(r"<context_summary>\s*\n(.*?)\s*</context_summary>", content, re.DOTALL)
                if match:
                    current_summary = match.group(1).strip()
                break
        session['summary'] = current_summary
        if agent_pool:
            agent_pool.instance_summaries[session['session_name']] = current_summary

        return {
            'messages': [serialize_message(m, i) for i, m in enumerate(msgs)],
            'sub_agents': get_sub_agent_state(),
            'active_stack': get_active_stack(),
            'approvals': get_approvals(),
            'generating': generating if generating is not None else session['generating'],
            'session_name': session['session_name'],
            'agent_index': session['agent_index'],
            'total_tokens': total_tokens,
            'total_words': total_words,
            'max_tokens': max_tokens,
            'summary': current_summary,
            'telemetry': _safe_get_telemetry(),
            'agents': [
                {'name': getattr(a, 'name', f'Agent-{i}'), 'index': i,
                 'description': getattr(a, 'description', ''),
                 'tools': list(a.function_map.keys()) if hasattr(a, 'function_map') else [],
                 'default_tools': getattr(a, 'default_tools', list(a.function_map.keys()) if hasattr(a, 'function_map') else [])}
                for i, a in enumerate(agents)
            ],
            'current_model': getattr(get_agent().llm, 'model', 'Unknown') if hasattr(get_agent(), 'llm') and get_agent().llm else 'Unknown',
        }

    def build_stream_update(responses, cached_h_stats=None, sub_agents=None, telemetry=None):
        """Build a lightweight streaming delta (skips re-serializing stable history).

        Args:
            responses: Current partial response messages from the agent runner.
            cached_h_stats: Pre-computed history stats to avoid O(n) recalculation each tick.
                           If None, falls back to get_history_stats(session['history']).
            sub_agents: Pre-serialized sub-agent state. Only recompute every ~5 ticks;
                       on intermediate ticks the client tolerates slight staleness.
            telemetry: Pre-serialized session telemetry summary. Only recompute every ~20 ticks
                       (approx 3 seconds) to avoid heavy re-aggregation during streaming.
        """
        history_count = len(session['history'])

        # Only serialize the changing response messages (history is already on the client)
        response_msgs = [serialize_message(m, history_count + i) for i, m in enumerate(responses)] if responses else []

        # Stats: use cached h_stats when available to skip O(n) history iteration each tick
        h_stats = cached_h_stats if cached_h_stats is not None else get_history_stats(session['history'])
        r_stats = get_history_stats(responses) if responses else {'tokens': 0, 'words': 0}

        orch_agent = get_agent()
        return {
            'history_count': history_count,
            'response_messages': response_msgs,
            'sub_agents': sub_agents if sub_agents is not None else get_sub_agent_state(),
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
        """Send JSON to all connected WebSocket clients."""
        nonlocal ws_connections
        text = json.dumps(data, ensure_ascii=False, default=str)
        dead = set()
        for conn in ws_connections:
            try:
                await conn.send_text(text)
            except Exception:
                dead.add(conn)
        if dead:
            ws_connections = ws_connections - dead

    # ── Agent execution thread ────────────────────────────────────────────

    def run_agent_thread(history_for_agent, agent_runner, gen_id, loop):
        """
        Runs agent.run() in a background thread.
        Pushes state updates onto the async send_queue.
        """
        try:
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
                ints = ['max_tokens', 'max_completion_tokens', 'top_k', 'seed', 'max_input_tokens', 'max_turns', 'read_file_limit']
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
            mcp_servers = ui_cfg.pop('mcpServers', None)
            disabled_tools = ui_cfg.pop('disabled_tools', None)
            work_access_folders = ui_cfg.pop('work_access_folders', None)

            # Keys that should not be passed to the underlying LLM chat API
            NON_LLM_KEYS = (
                'max_auto_rollbacks', 'auto_rollback_on_loop', 'auto_continue', 
                'max_turns', 'mcpServers', 'work_access_folders', 'seed',
                'read_file_limit', 'grep_char_limit', 'shell_char_limit', 'code_char_limit',
                'disabled_tools'
            )

            if work_access_folders is not None and agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                agent_pool.operation_manager.set_extra_work_folders(work_access_folders)

            has_llm = hasattr(agent_runner, 'llm') and agent_runner.llm
            old_cfg = None
            if has_llm:
                old_cfg = copy.deepcopy(agent_runner.llm.generate_cfg)
                agent_runner.llm.generate_cfg.pop('mcpServers', None)
                pure_llm_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
                agent_max_turns = ui_cfg.get('max_turns')
                agent_auto_continue = ui_cfg.get('auto_continue')
                read_file_limit = ui_cfg.get('read_file_limit')

                # Remove any existing keys that should not be passed to LLM
                for key in NON_LLM_KEYS:
                    agent_runner.llm.generate_cfg.pop(key, None)

                agent_runner.llm.generate_cfg.update(pure_llm_cfg)
                if disabled_tools is not None:
                    agent_runner.llm.generate_cfg['disabled_tools'] = disabled_tools
                if agent_pool:
                    agent_pool.update_llm_cfg(agent_runner.llm.generate_cfg)
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
                    if current_history and current_history[0].get(ROLE) == SYSTEM:
                        _sys_prompt = current_history[0].get(CONTENT, '')[:2000]
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
                last_send = 0
                tick_num = 0
                
                # Capture snapshots of sub-agent states before starting the run
                pool_snapshots = {}
                if agent_pool:
                    pool_snapshots = agent_pool.capture_snapshots()

                # Pre-compute history stats for streaming updates
                cached_h_stats = get_history_stats(current_history)
                sub_agents_cache = None

                try:
                    # Sliced working set for the LLM
                    working_history = agent_pool.slice_history_for_llm(current_history) if agent_pool else current_history
                    
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

                        if now - last_send > 0.15 or stack_changed:
                            if tick_num % 5 == 0 or stack_changed:
                                sub_agents_cache = get_sub_agent_state()
                                if agent_pool:
                                    agent_pool._last_seen_stack = current_stack
                            
                            # Throttle telemetry to ~3s (every 20 ticks) to keep it lightweight
                            _telem_payload = None
                            if tick_num % 20 == 0:
                                _telem_payload = _safe_get_telemetry()

                            # UI expects history count to match current_history
                            delta = build_stream_update(responses, cached_h_stats=cached_h_stats, sub_agents=sub_agents_cache, telemetry=_telem_payload)
                            # Override history_count in delta for consistency with current_history
                            delta['history_count'] = len(current_history)
                            
                            asyncio.run_coroutine_threadsafe(
                                send_queue.put({'type': 'stream_update', **delta}), loop
                            )
                            
                            # Loop Detection
                            loop_info = detect_loop(current_history + responses)
                            if loop_info:
                                loop_reason, pop_count = loop_info
                                if auto_rollback_enabled and retry_count < max_auto_retries:
                                    logger.warning(f"Loop detected: {loop_reason}. Surgical rollback enabled (Retry {retry_count+1}/{max_auto_retries}).")
                                    
                                    # 1. Surgical Rollback
                                    # pop_count is relative to full_history (current_history + responses)
                                    full_h = current_history + responses
                                    refined_pop = _refine_pop_count(full_h, pop_count)
                                    
                                    if refined_pop > len(responses):
                                        # Loop started in previous history turns
                                        excess = refined_pop - len(responses)
                                        if len(current_history) >= excess:
                                            del current_history[-excess:]
                                        responses = []
                                    else:
                                        # Loop is entirely within current response
                                        del responses[-refined_pop:]

                                    # 2. Inject a hint to avoid the loop in the next attempt
                                    loop_hint = f"[SYSTEM]: A repetitive loop was detected ({loop_reason}). Please try a different approach."
                                    current_history.append({ROLE: USER, CONTENT: loop_hint})
                                    
                                    # 3. Notify UI that we are retrying
                                    asyncio.run_coroutine_threadsafe(
                                        send_queue.put({
                                            'type': 'error', 
                                            'message': f"🔄 Loop detected. Surgically rolling back and retrying ({retry_count+1}/{max_auto_retries})..."
                                        }), loop
                                    )
                                    
                                    should_retry = True
                                    session['stop_requested'] = False
                                    break 
                                else:
                                    logger.warning(f"Loop detected: {loop_reason}. Stopping generation.")
                                    
                                    # Rollback even on final stop to keep history clean for user intervention
                                    if agent_pool:
                                        agent_pool.rollback_to_snapshots(pool_snapshots)
                                    if current_history:
                                        current_history.pop()
                                    
                                    # Clear responses so the loop garbage isn't appended to history
                                    responses = []
                                    
                                    session['stop_requested'] = True
                                    if agent_pool:
                                        agent_pool.stopped = True
                                    
                                    asyncio.run_coroutine_threadsafe(
                                        send_queue.put({
                                            'type': 'error', 
                                            'message': f"🔄 {loop_reason}. The agent has been stopped to prevent an infinite loop. History has been rolled back to the last stable state."
                                        }), loop
                                    )
                                    break

                            last_send = now
                            tick_num += 1
                except Exception as e:
                    from agent_orchestrator import LoopDetectedError
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
                            elif agent_pool:
                                # Fallback to snapshots if pop_count is missing
                                agent_pool.rollback_to_snapshots(pool_snapshots)
                                
                            # 1b. Inject a hint directly into the sub-agent's history so it knows it looped
                            if is_sub_agent:
                                sub_hint = f"[SYSTEM]: Your last actions resulted in a repetitive loop ({loop_reason}). Please try a different approach to solve the task."
                                agent_pool.instance_conversations[agent_name].append({ROLE: USER, CONTENT: sub_hint})
                                
                            # 2. Inject hint into main orchestrator history
                            loop_hint = f"[SYSTEM]: A repetitive loop was detected for {agent_name} ({loop_reason}). Please try a different approach."
                            current_history.append({ROLE: USER, CONTENT: loop_hint})
                            
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
                                agent_pool.rollback_to_snapshots(pool_snapshots)
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

            # If we retried or rolled back, session['history'] is now stale.
            # Sync it with current_history (which includes our rollbacks/hints).
            session['history'] = current_history

            if responses:
                for r in responses:
                    c = r.get(CONTENT) if isinstance(r, dict) else getattr(r, 'content', '')
                    if c == PENDING_USER_INPUT:
                        continue
                    if isinstance(r, dict):
                        session['history'].append(r)
                    elif hasattr(r, 'model_dump'):
                        session['history'].append(r.model_dump())
                    else:
                        session['history'].append({ROLE: str(getattr(r, 'role', '')), CONTENT: str(getattr(r, 'content', ''))})

            if agent_pool:
                # CRITICAL: Sync back to pool so tools like CompressionTool see the current history
                agent_pool.instance_conversations[session['session_name']] = session['history']

            if hasattr(agent_runner, 'turn_final_messages') and agent_runner.turn_final_messages:
                tfm = agent_runner.turn_final_messages
                
                # Check if this is just a sliced view (starts with SYSTEM + <context_summary> USER message)
                # If it's a slice, we should NOT clear the full session history, as that would
                # cause a 'full rollback' effect in the UI.
                is_slice = False
                if len(tfm) > 1 and len(tfm) < len(session['history']):
                    msg1_content = tfm[1].get(CONTENT, '') if isinstance(tfm[1], dict) else getattr(tfm[1], 'content', '')
                    if isinstance(msg1_content, str) and "<context_summary>" in msg1_content:
                        # It's a sliced view. Only sync if it's SHORTER than even the working history
                        # we started with (which would imply a rollback happened inside the turn).
                        if len(tfm) >= len(working_history):
                            is_slice = True
                
                if not is_slice and len(tfm) < len(session['history']):
                    logger.info(f"Syncing history from agent state ({len(tfm)} vs {len(session['history'])} messages).")
                    session['history'].clear()
                    for res in tfm:
                        msg = res.model_dump() if hasattr(res, 'model_dump') else (res if isinstance(res, dict) else {})
                        if msg.get(ROLE) != SYSTEM:
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
            asyncio.run_coroutine_threadsafe(send_queue.put({'type': 'done', **final}), loop)

        except Exception as e:
            traceback.print_exc()
            asyncio.run_coroutine_threadsafe(send_queue.put({'type': 'error', 'message': str(e)}), loop)
        finally:
            session['generating'] = False
            session['stop_requested'] = False
            if agent_pool:
                agent_pool.stopped = False
            if has_llm and old_cfg:
                agent_runner.llm.generate_cfg = old_cfg

    # ── Background tasks ──────────────────────────────────────────────────

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(_sender_loop())
        asyncio.create_task(_approval_loop())

    async def _sender_loop():
        """Global loop: reads from send_queue → broadcasts to all clients."""
        while True:
            try:
                data = await send_queue.get()
                await broadcast(data)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

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
                except json.JSONDecodeError:
                    continue

                msg_type = data.get('type', '')

                # ── Send message / async inject ──
                if msg_type == 'message':
                    text = data.get('text', '').strip()
                    if not text:
                        continue

                    if session['generating']:
                        # Async injection while agent is running
                        if agent_pool:
                            agent_pool.async_message_queue.append(text)
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
                        
                        _save_session_history()
                        if agent_pool:
                            agent_pool.reset()
                        await broadcast({'type': 'state', **build_state()})
                        continue

                    # Add user message to history (parsed for multimodal items)
                    parsed_content = _parse_multimodal_content(text)
                    session['history'].append({ROLE: USER, CONTENT: parsed_content})

                    # Start agent generation
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
                    session['stop_requested'] = True
                    if agent_pool:
                        agent_pool.stopped = True

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
                           and session['history'][-1].get(ROLE) in (ASSISTANT, FUNCTION)):
                        session['history'].pop()

                    # Roll back one more (the user message) to allow a clean re-trigger
                    # and ensure consistency with the last_turn_snapshots.
                    last_user_msg = None
                    if session['history'] and session['history'][-1].get(ROLE) == USER:
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
                            agent_pool.rollback_to_snapshots(session['last_turn_snapshots'])
                        
                        # 2. Rollback the main orchestrator log to match the shortened history
                        # This now points to the state before the user message we just popped.
                        try:
                            agent_runner_for_log = get_agent()
                            main_logger = agent_pool.get_logger(session['session_name'], agent_runner_for_log.__class__.__name__)
                            main_logger.truncate_to(len(session['history']))
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

                    session['stop_requested'] = False
                    if agent_pool:
                        agent_pool.stopped = False
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

                elif msg_type == 'reset':
                    session['history'] = []
                    _save_session_history()
                    session['generating'] = False
                    session['stop_requested'] = False
                    session['generation_id'] += 1
                    if agent_pool:
                        agent_pool.stopped = True
                        agent_pool.reset()
                    await broadcast({'type': 'done', **build_state()})

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
                                print(f"[MCP] Eagerly loaded {len(mcp_tools)} tools.")
                            except Exception as e:
                                print(f"[MCP] Eager initialization failed: {e}")
                        if 'work_access_folders' in ui_cfg:
                            if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
                                agent_pool.operation_manager.set_extra_work_folders(ui_cfg['work_access_folders'])
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
                    rid = data.get('request_id')
                    auto_apply = data.get('auto_apply', False)
                    if rid and agent_pool:
                        pending = agent_pool.operation_manager.list_pending_approvals()
                        ap = next((a for a in pending if a['request_id'] == rid), None)
                        if ap:
                            loop = asyncio.get_running_loop()
                            def _security_check():
                                try:
                                    import platform, re, json, copy
                                    
                                    if not agent_pool.get_agent('security_advisor'):
                                        agent_pool.load_agent('security_advisor')
                                    sec_agent = agent_pool.get_agent('security_advisor')

                                    workspace_info = f"Main workspace: {agent_pool.operation_manager.base_dir}\n"
                                    if agent_pool.operation_manager.extra_work_folders:
                                        extra = [str(p) for p in agent_pool.operation_manager.extra_work_folders]
                                        workspace_info += f"Additional allowed folders: {', '.join(extra)}\n"
                                        
                                    prompt = SECURITY_ADVISOR_PROMPT.format(
                                        tool_name=ap.get('tool_name', 'unknown'),
                                        description=ap.get('description', ''),
                                        arguments=json.dumps(ap.get('tool_args', {})),
                                        os_info=f"{platform.system()} {platform.release()}",
                                        workspace_info=workspace_info
                                    )
                                    
                                    history = [{'role': 'user', 'content': prompt}]
                                    
                                    NON_LLM_KEYS = (
                                        'max_auto_rollbacks', 'auto_rollback_on_loop', 'auto_continue', 
                                        'max_turns', 'mcpServers', 'work_access_folders', 'seed',
                                        'read_file_limit', 'grep_char_limit', 'shell_char_limit', 'code_char_limit',
                                        'disabled_tools'
                                    )
                                    ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))
                                    llm_safe_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
                                    
                                    final_msgs = []
                                    for partial in sec_agent.run(history, agent_instance_name='security_advisor', **llm_safe_cfg):
                                        final_msgs = partial
                                        
                                    display_response = ""
                                    parsing_response = ""
                                    
                                    new_msgs = final_msgs[1:]
                                    for msg in new_msgs:
                                        role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                                        content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
                                        reasoning = msg.get('reasoning_content', '') if isinstance(msg, dict) else getattr(msg, 'reasoning_content', '')
                                        fc = msg.get('function_call', None) if isinstance(msg, dict) else getattr(msg, 'function_call', None)
                                        
                                        if role == 'assistant':
                                            # Deduplicate: if content already contains the reasoning, don't add it twice
                                            clean_content = content
                                            if reasoning:
                                                # Check if content starts with a thinking block that matches reasoning
                                                think_match = re.search(r'<(think|thought)>([\s\S]*?)(</think>|$)', content, re.IGNORECASE)
                                                if think_match:
                                                    embedded_thought = think_match.group(2).strip()
                                                    # If they are very similar, we consider it a duplicate
                                                    if reasoning.strip() in embedded_thought or embedded_thought in reasoning.strip():
                                                        # Remove the embedded thinking block from display_response part
                                                        clean_content = (content[:think_match.start()] + content[think_match.end():]).strip()
                                                
                                                display_response += f"<think>\n{reasoning.strip()}\n</think>\n\n"
                                            
                                            if fc:
                                                fname = fc.get('name', '') if isinstance(fc, dict) else getattr(fc, 'name', '')
                                                display_response += f"*(Tool call: {fname})*\n\n"

                                            if clean_content:
                                                display_response += f"{clean_content}\n\n"
                                                
                                            # For parsing, we always want the content WITHOUT any thinking blocks
                                            if content:
                                                parsing_response = re.sub(r'<(think|thought)>.*?(</\1>|$)', '', content, flags=re.IGNORECASE | re.DOTALL).strip()
                                        elif role == 'function':
                                            fname = msg.get('name', '') if isinstance(msg, dict) else getattr(msg, 'name', '')
                                            display_response += f"*(Result from {fname} - {len(str(content))} chars)*\n\n"

                                    # Use the unstripped content for verdict detection to be safe, 
                                    # but use stripped content for the display if needed.
                                    raw_parsing_text = parsing_response
                                    
                                    parsing_text = raw_parsing_text
                                    parsing_text = re.sub(r'<(think|thought)>.*?</\1>', '', parsing_text, flags=re.IGNORECASE | re.DOTALL)
                                    parsing_text = re.sub(r'\[(THINK|THOUGHT)\].*?\[/\1\]', '', parsing_text, flags=re.IGNORECASE | re.DOTALL)
                                    # Only strip unclosed blocks at the very end of the string
                                    parsing_text = re.sub(r'<(think|thought)>[^<]*$', '', parsing_text, flags=re.IGNORECASE | re.DOTALL)
                                    
                                    parsing_response = parsing_text.strip()
                                    
                                    # Check verdict on both stripped and unstripped to be robust
                                    check_text = (parsing_response + " " + raw_parsing_text).upper()
                                    is_yes = "[YES]" in check_text or parsing_response.upper().strip().startswith("YES")
                                    is_no = "[NO]" in check_text or parsing_response.upper().strip().startswith("NO")
                                    
                                    # Extract reason: find the first occurrence of [NO] and take everything after it
                                    no_reason = ""
                                    if is_no:
                                        match = re.search(r'\[NO\]', raw_parsing_text, re.IGNORECASE)
                                        if match:
                                            no_reason = raw_parsing_text[match.end():].strip()
                                            # Strip leading "Reason:" or similar if present
                                            no_reason = re.sub(r'^[:\s-]*(Reason|Justification)[:\s-]*', '', no_reason, flags=re.IGNORECASE).strip()
                                        else:
                                            no_reason = parsing_response
                                    
                                    if not is_yes and not is_no:
                                        # Strict enforcement: Invalid format = Automatic NO
                                        logger.info(f"[SECURITY] Automatic Rejection for {rid} (Ambiguous/Invalid Format)")
                                        reject_msg = f"SECURITY VERIFICATION FAILED: The security advisor provided an ambiguous response without a clear [YES] or [NO] verdict. For safety, the operation has been automatically rejected. Please ensure your response ends with an explicit [YES] or [NO] verdict."
                                        agent_pool.operation_manager.user_reject(rid, reject_msg)
                                        
                                        # Also notify the UI
                                        asyncio.run_coroutine_threadsafe(
                                            send_queue.put({'type': 'security_response', 'request_id': rid, 'response': display_response + f"\n\n**[AUTO-REJECTED: Ambiguous Format]**", 'verdict': 'AMBIGUOUS'}),
                                            loop
                                        )
                                    elif auto_apply:
                                        if is_yes:
                                            logger.info(f"[SECURITY] Automated Approval for {rid}")
                                            agent_pool.operation_manager.user_approve(rid)
                                        else: # is_no
                                            logger.info(f"[SECURITY] Automated Rejection for {rid}. Reason: {no_reason}")
                                            agent_pool.operation_manager.user_reject(rid, no_reason)
                                    else:
                                        # Valid format but auto_apply is off: Send to UI for manual confirmation
                                        asyncio.run_coroutine_threadsafe(
                                            send_queue.put({
                                                'type': 'security_response', 
                                                'request_id': rid, 
                                                'response': display_response,
                                                'verdict': 'YES' if is_yes else 'NO',
                                                'reason': no_reason if is_no else ""
                                            }),
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
                            
                            threading.Thread(target=_security_check, daemon=True).start()

                elif msg_type == 'edit_message':
                    idx = data.get('index')
                    content = data.get('content', '')
                    if (idx is not None
                            and not session['generating']
                            and 0 <= idx < len(session['history'])):
                        msg = session['history'][idx]
                        if isinstance(msg, dict):
                            msg[CONTENT] = _parse_multimodal_content(content)
                        _save_session_history()
                    await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'delete_messages':
                    if session['generating']:
                        continue
                    indices = sorted(data.get('indices', []), reverse=True)
                    for idx in indices:
                        if 0 <= idx < len(session['history']):
                            session['history'].pop(idx)
                    _save_session_history()
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

                elif msg_type == 'edit_summary':
                    target_name = data.get('instance_name')
                    new_summary_content = data.get('content', '')
                    
                    if not target_name:
                        target_name = session['session_name']
                    
                    # 1. Update history list (find the compression message)
                    history_to_update = []
                    if target_name == session['session_name']:
                        history_to_update = session['history']
                    elif agent_pool and target_name in agent_pool.instance_conversations:
                        history_to_update = agent_pool.instance_conversations[target_name]
                    
                    if history_to_update:
                        for msg in history_to_update:
                            old_content = msg.get(CONTENT, '')
                            if isinstance(old_content, str) and "<context_summary>" in old_content:
                                # Replace the inner summary while keeping markers
                                import re
                                prefix_match = re.search(r"(.*?<context_summary>\s*\n)", old_content, re.DOTALL)
                                suffix_match = re.search(r"(\s*</context_summary>.*)", old_content, re.DOTALL)
                                
                                if prefix_match and suffix_match:
                                    msg[CONTENT] = prefix_match.group(1) + new_summary_content + suffix_match.group(1)
                                    break
                    
                    # 2. Update AgentPool tracker
                    if agent_pool:
                        agent_pool.instance_summaries[target_name] = new_summary_content
                        
                        # 3. Sync to persistent log file
                        if target_name in agent_pool.instance_loggers:
                            agent_pool.instance_loggers[target_name].reset_history(history_to_update)
                    
                    if target_name == session['session_name']:
                        _save_session_history()
                        
                    await broadcast({'type': 'state', **build_state()})
                    new_name = data.get('name', 'Maine')
                    if new_name != session['session_name']:
                        session['session_name'] = new_name
                        # Auto-load history for the new session name
                        session['history'] = _load_session_history(new_name)
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
                                await broadcast({'type': 'state', **build_state()})

                elif msg_type == 'inject':
                    text = data.get('text', '').strip()
                    if text and agent_pool:
                        agent_pool.async_message_queue.append(text)

        except WebSocketDisconnect:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            ws_connections.discard(websocket)

    # ── Serve frontend static files ───────────────────────────────────────
    web_ui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web_ui')

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

    return app
