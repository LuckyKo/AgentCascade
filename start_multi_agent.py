"""
Multi-Agent Orchestrator Demo
A supervisor agent that can delegate tasks to specialized sub-agents.

Each sub-agent has its own:
- soul.md (personality & behavior)
- Specialized tools
- Domain expertise

User Approval System:
- Reads: Free access
- All mutating operations: Block until user approves/rejects via WebUI

The Orchestrator is also a launchable agent with its own soul.md!
"""

import os
from pathlib import Path
from agent_cascade.log import logger

# ── Workspace Detection (shared) ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.absolute()
from agent_cascade.shared_init import detect_workspace_dir, ensure_workspace
WORKSPACE_DIR = detect_workspace_dir(PROJECT_ROOT)
ensure_workspace(WORKSPACE_DIR)

# Import built-in tools from agent_cascade (needed for per-agent tool loading)
from agent_cascade.tools import (
    image_gen,
    web_extractor,
    storage,
    simple_doc_parser,
    doc_parser,
    extract_doc_vocabulary,
    code_interpreter,
)
from agent_cascade.settings import DEFAULT_WORKSPACE

# DDGSearch is now a shared module — imported instead of defined inline
from agent_cascade.tools.custom import DDGSearch
llm_cfg = {
    'model': 'whatever_is_on',  # or your preferred vision model
    'model_server': 'http://localhost:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',  # Force multimodal support
    'max_input_tokens': 65536,   # Custom context window limit (override detection)
}

# Define default tools for each agent type
# All tools will be available in UI, but only these will be enabled by default
DEFAULT_TOOLS = {
    'orchestrator': [
        'call_agent', 'dismiss_agent', 'list_agents',
        'compress_context', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'read_file', 'view_image', 'list_dir', 'grep',
        'ddg_search', 'web_extractor', 'storage', 'retrieval', 'system_info'
    ],
    'coder': [
        'call_agent', 'list_agents',
        'read_file', 'view_image', 'compress_context', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'list_dir', 'grep', 'code_interpreter', 'shell_cmd',
        'ddg_search', 'web_extractor', 'storage'
    ],
    'researcher': [
        'call_agent', 'list_agents',
        'read_file', 'view_image', 'compress_context', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'list_dir', 'grep', 'code_interpreter',
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

# Tools available to ALL agents (shown in UI but disabled by default)
ALL_BUILTIN_TOOLS = [
    'image_gen', 'storage', 'retrieval', 'code_interpreter',
    'delete_file', 'shell_cmd', # These are powerful - enable manually
]


from agent_cascade.tools.custom import SystemInfo as _SystemInfo

if __name__ == '__main__':
    import sys

    logger.info("Initializing Agent Orchestrator...")
    logger.info("=" * 50)

    # ── Infrastructure initialization (delegated to shared module) ────────────
    from agent_cascade.shared_init import (
        initialize_infrastructure, load_tools_per_agent,
        load_orchestrator, build_all_agents_list,
    )

    operation_mgr, agent_pool, _ = initialize_infrastructure(
        PROJECT_ROOT, llm_cfg, use_shared_tools=False,
    )

    # Add tools to all agents based on their role (per-agent pattern)
    for agent_name in agent_pool.list_agents():
        agent = agent_pool.get_agent(agent_name)
        if agent:
            # Get default tools for this agent type
            default_tools = DEFAULT_TOOLS.get(agent_name, DEFAULT_TOOLS['writer'])

            # Always add web tools and system info
            agent.function_map['ddg_search'] = DDGSearch()
            agent.function_map['system_info'] = _SystemInfo(agent_pool=agent_pool)

            # Load built-in tools for this agent (delegated to shared module)
            load_tools_per_agent(
                agent, default_tools, llm_cfg, WORKSPACE_DIR,
                agent_pool, operation_mgr, agent_name=agent_name,
            )

    # Load orchestrator from the pool
    orchestrator = load_orchestrator(agent_pool)

    # Add web tools and system info to orchestrator
    orchestrator.function_map['ddg_search'] = DDGSearch()
    orchestrator.function_map['system_info'] = _SystemInfo(agent_pool=agent_pool)

    # Load built-in tools for the orchestrator (delegated to shared module)
    default_orch_tools = DEFAULT_TOOLS['orchestrator']
    load_tools_per_agent(
        orchestrator, default_orch_tools, llm_cfg, WORKSPACE_DIR,
        agent_pool, operation_mgr, agent_name='',  # empty label → 'orchestrator'
    )

    # Also load all sub-agents for the agent selector
    all_agents = build_all_agents_list(agent_pool, orchestrator)

    logger.info(f"[OK] Available agents: {[a.name for a in all_agents]}")
    logger.info("=" * 50)

    # Set up background thread for async terminal messages
    import threading
    def async_input_listener():
        while True:
            try:
                msg = sys.stdin.readline().strip()
                if msg:
                    # Route to orchestrator session by default
                    target = orchestrator.session_name if hasattr(orchestrator, 'session_name') else 'Maine'
                    agent_pool.enqueue_message(target, msg)
                    logger.info(f"\n[QUEUED] '{msg}' → {target} (will be injected on its next turn)")
            except Exception as e:
                logger.warning("Async input listener error: %s", e)
                break
    threading.Thread(target=async_input_listener, daemon=True).start()

    # ── Launch the API server (FastAPI + custom HTML/JS frontend) ──────────────
    try:
        from agent_cascade.api_server import create_app
        import uvicorn

        chatbot_config = {
            'session_name': 'Maine',
            'verbose': False,
        }

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

    import signal

    # Use Config + Server pattern for graceful shutdown support (host 0.0.0.0 allows LAN access)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)

    def handle_shutdown(signum, frame):
        logger.info("\n[INFO] Initiating graceful shutdown...")
        agent_pool.stopped = True
        if hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
            try:
                agent_pool.operation_manager.cleanup_backups()
            except Exception as e:
                logger.warning("Cleanup backups failed during shutdown: %s", e)
        # Set should_exit for graceful uvicorn shutdown (avoids resource leaks from sys.exit)
        server.should_exit = True

    signal.signal(signal.SIGINT, handle_shutdown)
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, handle_shutdown)

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
