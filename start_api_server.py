"""
Multi-Agent API Server — Entry Point

Same agent initialization as start_multi_agent.py, but launches the
WebSocket/REST API server instead of Gradio.

Usage:
    python start_api_server.py
    Open http://127.0.0.1:8765 in your browser.
"""

import json
import os
import requests
from pathlib import Path

from agent_cascade.log import logger

# ── Workspace Detection ──────────────────────────────────────────────────────
# Detect if we should use a sibling 'AgentWorkspace' instead of the local folder
PROJECT_ROOT = Path(__file__).parent.absolute()
WORKSPACE_DIR = os.getenv('QWEN_AGENT_DEFAULT_WORKSPACE')

if not WORKSPACE_DIR:
    sibling_ws = PROJECT_ROOT.parent / 'AgentWorkspace'
    if sibling_ws.exists():
        WORKSPACE_DIR = str(sibling_ws)
        os.environ['QWEN_AGENT_DEFAULT_WORKSPACE'] = WORKSPACE_DIR
        logger.info("[INIT] Detected sibling workspace: %s", WORKSPACE_DIR)
    else:
        WORKSPACE_DIR = str(PROJECT_ROOT / 'workspace')
        os.environ['QWEN_AGENT_DEFAULT_WORKSPACE'] = WORKSPACE_DIR
        logger.info("[INIT] Using local workspace: %s", WORKSPACE_DIR)

from bs4 import BeautifulSoup
from agent_cascade.tools.base import BaseTool, register_tool
from agent_orchestrator import AgentPool, load_orchestrator_agent

from agent_cascade.tools import (
    image_gen,
    web_extractor,
    storage,
    simple_doc_parser,
    doc_parser,
    extract_doc_vocabulary,
    code_interpreter,
    python_compiler,
)
from agent_cascade.tools.custom import SystemInfo
from agent_cascade.settings import DEFAULT_WORKSPACE


# ── Reuse DDGSearch from start_multi_agent ────────────────────────────────────
@register_tool('ddg_search', allow_overwrite=True)
class DDGSearch(BaseTool):
    name = 'ddg_search'
    description = 'Search for information from the internet using DuckDuckGo (No API key required).'
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'The search query'
            }
        },
        'required': ['query'],
    }

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        query = params['query']
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            url = f'https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}'
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            results = []
            for result in soup.select('.result')[:5]:
                title_elem = result.select_one('.result__title')
                snippet_elem = result.select_one('.result__snippet')
                url_elem = result.select_one('.result__url')
                if title_elem and snippet_elem:
                    title = title_elem.get_text(strip=True)
                    snippet = snippet_elem.get_text(strip=True)
                    url_text = url_elem.get_text(strip=True) if url_elem else ''
                    results.append(f'Title: {title}\nSnippet: {snippet}\nURL: {url_text}')
            if results:
                return '\n\n'.join(results)
            return 'No results found.'
        except Exception as e:
            return f'Search failed: {str(e)}'


# ── Configuration (same as start_multi_agent.py) ──────────────────────────────
llm_cfg = {
    'model': 'whatever_is_on',
    'model_server': 'http://localhost:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
    'max_input_tokens': 65536,
}

DEFAULT_TOOLS = {
    'orchestrator': [
        'call_agent', 'dismiss_agent', 'list_agents',
        'compress_context', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'move_file', 'read_file', 'view_image', 'list_dir', 'grep',
        'ddg_search', 'web_extractor', 'storage', 'retrieval', 'system_info'
    ],
    'coder': [
        'call_agent', 'list_agents',
        'read_file', 'view_image', 'compress_context', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'move_file', 'list_dir', 'grep', 'code_interpreter', 'shell_cmd',
        'ddg_search', 'web_extractor', 'storage'
    ],
    'researcher': [
        'call_agent', 'list_agents',
        'read_file', 'view_image', 'compress_context', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'move_file', 'list_dir', 'grep', 'code_interpreter',
        'ddg_search', 'web_extractor', 'storage', 'retrieval',
        'doc_parser', 'simple_doc_parser', 'extract_doc_vocabulary'
    ],
    'writer': [
        'call_agent', 'list_agents',
        'read_file', 'view_image', 'compress_context', 'write_file', 'edit_file', 'list_dir',
        'ddg_search', 'web_extractor', 'storage',
        'doc_parser', 'simple_doc_parser'
    ],
    'reviewer': [
        'call_agent', 'list_agents',
        'read_file', 'view_image', 'compress_context', 'list_dir', 'grep', 'code_interpreter',
    ],
}


def initialize_agents():
    """Set up agents, pool, and config. Returns (all_agents, agent_pool, chatbot_config)."""
    logger.info("Initializing Agent Orchestrator (API Server)...")
    logger.info("=" * 50)

    agent_pool = AgentPool(llm_cfg, 'agents', workspace_dir=WORKSPACE_DIR)

    # Instantiate heavy tools ONCE to share across all agents and prevent OOM
    shared_tools = {}
    shared_tools['system_info'] = SystemInfo(agent_pool=agent_pool)
    shared_tools['ddg_search'] = DDGSearch()
    try:
        shared_tools['image_gen'] = image_gen.ImageGen(llm_cfg=llm_cfg)
    except Exception:
        pass
    shared_tools['web_extractor'] = web_extractor.WebExtractor(cfg={'work_dir': DEFAULT_WORKSPACE})
    shared_tools['storage'] = storage.Storage()
    
    from agent_cascade.tools import retrieval
    shared_tools['retrieval'] = retrieval.Retrieval(cfg={'work_dir': DEFAULT_WORKSPACE})
    shared_tools['simple_doc_parser'] = simple_doc_parser.SimpleDocParser(cfg={'work_dir': DEFAULT_WORKSPACE})
    shared_tools['doc_parser'] = doc_parser.DocParser(cfg={'work_dir': DEFAULT_WORKSPACE})
    shared_tools['extract_doc_vocabulary'] = extract_doc_vocabulary.ExtractDocVocabulary(cfg={'work_dir': DEFAULT_WORKSPACE})
    
    try:
        shared_tools['code_interpreter'] = code_interpreter.CodeInterpreter(cfg={'work_dir': DEFAULT_WORKSPACE})
        if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
            shared_tools['code_interpreter']._operation_manager = agent_pool.operation_manager
    except Exception:
        pass
        
    try:
        from agent_cascade.tools import python_compiler
        shared_tools['python_compiler'] = python_compiler.PythonCompiler(cfg={'work_dir': DEFAULT_WORKSPACE})
    except Exception:
        pass

    # Add tools to all agents based on their role
    for agent_name in agent_pool.list_agents():
        agent = agent_pool.get_agent(agent_name)
        if agent:
            default_tools = DEFAULT_TOOLS.get(agent_name, DEFAULT_TOOLS['writer'])
            agent.default_tools = default_tools
            
            for tool_name, tool_inst in shared_tools.items():
                agent.function_map[tool_name] = tool_inst

    # Load orchestrator
    orchestrator = load_orchestrator_agent(agent_pool, llm_cfg)

    default_orch_tools = DEFAULT_TOOLS['orchestrator']
    orchestrator.default_tools = default_orch_tools
    
    for tool_name, tool_inst in shared_tools.items():
        orchestrator.function_map[tool_name] = tool_inst

    all_agents = [orchestrator]
    for agent_name in agent_pool.list_agents():
        if agent_name != 'orchestrator':
            sub_agent = agent_pool.get_agent(agent_name)
            if sub_agent:
                all_agents.append(sub_agent)

    logger.info("[OK] Available agents: %s", [a.name for a in all_agents])
    logger.info("=" * 50)

    chatbot_config = {
        'session_name': 'Maine',
        'verbose': False,
    }

    return all_agents, agent_pool, chatbot_config


if __name__ == '__main__':
    import sys

    all_agents, agent_pool, chatbot_config = initialize_agents()

    # Set up async terminal input (same as start_multi_agent.py)
    import threading
    def async_input_listener():
        while True:
            try:
                msg = sys.stdin.readline().strip()
                if msg:
                    target = 'Maine'  # Default to orchestrator
                    agent_pool.enqueue_message(target, msg)
                    logger.info("\n[QUEUED] '%s' → %s (will be injected on next turn)", msg, target)
            except Exception:
                break
    threading.Thread(target=async_input_listener, daemon=True).start()

    # Create and launch the API server
    from api_server import create_app
    import uvicorn

    app = create_app(all_agents, agent_pool, chatbot_config)

    port = 8765
    logger.info("\n[OK] API Server ready!")
    logger.info("    -> Open http://127.0.0.1:%d in your browser", port)
    logger.info("    -> WebSocket at ws://127.0.0.1:%d/ws/chat", port)
    logger.info("    -> REST API at http://127.0.0.1:%d/api/", port)
    logger.info("\n[TIP] Type in this terminal to inject messages into the active agent.")
    logger.info("=" * 50)

    import signal
    import os

    def handle_shutdown(signum, frame):
        logger.info("\n[INFO] Initiating graceful shutdown...")
        agent_pool.stopped = True
        if hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
            try:
                agent_pool.operation_manager.cleanup_backups()
            except Exception:
                pass
        logger.info("[INFO] Terminated.")
        os._exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, handle_shutdown)

    # Note: Uvicorn overrides signal handlers. We need to tell it not to, or wrap it.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    
    # Overwrite uvicorn's signal handlers so ours runs
    server.install_signal_handlers = lambda: None
    
    server.run()
