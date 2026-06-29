"""
Multi-Agent API Server — Entry Point

Same agent initialization as start_multi_agent.py, but launches the
WebSocket/REST API server instead of Gradio.

Usage:
    python start_api_server.py
    Open http://127.0.0.1:8765 in your browser.
"""

import os
from pathlib import Path

from agent_cascade.log import logger

# ── Workspace Detection (shared) ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.absolute()
from agent_cascade.shared_init import detect_workspace_dir, ensure_workspace
WORKSPACE_DIR = detect_workspace_dir(PROJECT_ROOT)
ensure_workspace(WORKSPACE_DIR)

from agent_cascade.tools.custom import DDGSearch
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
        'compress_context', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'read_file', 'view_image', 'list_dir', 'grep',
        'ddg_search', 'web_extractor', 'system_info'
    ],
    'coder': [
        'call_agent', 'list_agents',
        'read_file', 'view_image', 'compress_context', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'list_dir', 'grep', 'code_interpreter', 'shell_cmd',
        'ddg_search', 'web_extractor'
    ],
    'researcher': [
        'call_agent', 'list_agents',
        'read_file', 'view_image', 'compress_context', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'list_dir', 'grep', 'code_interpreter',
        'ddg_search', 'web_extractor'
    ],
    'writer': [
        'call_agent', 'list_agents',
        'read_file', 'view_image', 'compress_context', 'write_file', 'edit_file', 'list_dir',
        'ddg_search', 'web_extractor'
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

    # ── Infrastructure initialization (delegated to shared module) ────────────
    from agent_cascade.shared_init import (
        initialize_infrastructure, load_orchestrator, build_all_agents_list,
    )

    operation_mgr, agent_pool, shared_tools = initialize_infrastructure(
        PROJECT_ROOT, llm_cfg, use_shared_tools=True,
    )

    # Add DDGSearch to the shared tools dict (not loaded by shared_init since it's defined per-entry-point)
    shared_tools['ddg_search'] = DDGSearch()

    # NOTE: storage, retrieval, simple_doc_parser, doc_parser, extract_doc_vocabulary are intentionally NOT added.
    # They remain in TOOL_REGISTRY (needed by Memory/RAG internally) but are hidden from agents.

    # Add tools to all agents based on their role
    for agent_name in agent_pool.list_agents():
        agent = agent_pool.get_agent(agent_name)
        if agent:
            default_tools = DEFAULT_TOOLS.get(agent_name, DEFAULT_TOOLS['writer'])
            agent.default_tools = default_tools

            for tool_name, tool_inst in shared_tools.items():
                agent.function_map[tool_name] = tool_inst

    # Load orchestrator from the pool (already discovered during AgentPool.__init__ → _discover_agents)
    orchestrator = load_orchestrator(agent_pool)

    default_orch_tools = DEFAULT_TOOLS['orchestrator']
    orchestrator.default_tools = default_orch_tools

    for tool_name, tool_inst in shared_tools.items():
        orchestrator.function_map[tool_name] = tool_inst

    all_agents = build_all_agents_list(agent_pool, orchestrator)

    logger.info("[OK] Available agents: %s", [a.name for a in all_agents])
    logger.info("[OK] Loaded tools: %s", list(shared_tools.keys()))
    logger.info("=" * 50)

    chatbot_config = {
        'session_name': 'Maine',
        'verbose': False,
    }

    return all_agents, agent_pool, chatbot_config


if __name__ == '__main__':
    import sys

    try:
        all_agents, agent_pool, chatbot_config = initialize_agents()
    except SystemExit:
        raise
    except Exception as e:
        logger.error("[FATAL] Agent initialization failed: %s", e)
        raise SystemExit(1)

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
            except Exception as e:
                logger.warning("Async input listener error: %s", e)
                break
    threading.Thread(target=async_input_listener, daemon=True).start()

    # Create and launch the API server
    try:
        from agent_cascade.api_server import create_app
        import uvicorn

        app = create_app(all_agents, agent_pool, chatbot_config)
        logger.debug("FastAPI app created successfully")
    except Exception as e:
        logger.error("[FATAL] Failed to create API server app: %s", e)
        raise SystemExit(1)

    port = int(os.getenv('QWEN_AGENT_PORT', 8765))
    logger.info("\n[OK] API Server ready!")
    logger.info("    -> Open http://127.0.0.1:%d in your browser", port)
    logger.info("    -> WebSocket at ws://127.0.0.1:%d/ws/chat", port)
    logger.info("    -> REST API at http://127.0.0.1:%d/api/", port)
    logger.info("\n[TIP] Type in this terminal to inject messages into the active agent.")
    logger.info("=" * 50)

    # Create server first so signal handler can reference it
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    # Use shared signal handler from shared_init (Phase 5B — deduplicated shutdown logic)
    from agent_cascade.shared_init import setup_signal_handler
    setup_signal_handler(agent_pool, server=server)

    # Prevent uvicorn from installing its own signal handlers (ours are already registered)
    server.install_signal_handlers = lambda: None

    try:
        server.run()
    except OSError as e:
        if e.errno == 98 or 'address already in use' in str(e).lower():
            logger.error("[FATAL] Port %d is already in use. Change QWEN_AGENT_PORT env var or stop the other process.", port)
        else:
            logger.error("[FATAL] Server failed to start: %s", e)
        raise SystemExit(1)
    except Exception as e:
        logger.error("[FATAL] Server crashed: %s", e)
        raise SystemExit(1)
