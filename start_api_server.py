"""
Multi-Agent API Server — Entry Point

Same agent initialization as start_multi_agent.py, but launches the
WebSocket/REST API server instead of Gradio.

Usage:
    python start_api_server.py
    Open http://127.0.0.1:8765 in your browser.

CLI Flags:
    --auto_security   Start with Auto-Ask Security mode enabled. The security advisor
                      will auto-check all tool calls before execution (same as toggling
                      "Auto-Ask Security" on in the UI). By default, security checks run
                      only when triggered by agent prompts.
"""

import os
from pathlib import Path

from agent_cascade.log import logger

# ── Workspace Detection (shared) ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.absolute()
from agent_cascade.shared_init import detect_workspace_dir, ensure_workspace
WORKSPACE_DIR = detect_workspace_dir(PROJECT_ROOT)
ensure_workspace(WORKSPACE_DIR)

# Tool availability is driven by AVAILABLE_TOOLS in dna.py.
llm_cfg = {
    'model': 'whatever_is_on',
    'model_server': 'http://localhost:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
    'max_input_tokens': 65536,
}


def initialize_agents():
    """Set up agents, pool, and config. Returns (all_agents, agent_pool, chatbot_config)."""
    logger.info("Initializing Agent Orchestrator (API Server)...")
    logger.info("=" * 50)

    # ── Infrastructure initialization (delegated to shared module) ────────────
    from agent_cascade.shared_init import (
        initialize_infrastructure, load_orchestrator, build_all_agents_list,
    )

    operation_mgr, agent_pool = initialize_infrastructure(PROJECT_ROOT, llm_cfg)

    # Tools are already registered by register_standard_tools() during agent loading
    # (via AgentPool._discover_agents → load_agent_template → load_agent).
    # No additional tool distribution needed — AVAILABLE_TOOLS is the single source of truth.

    all_agents = build_all_agents_list(agent_pool, load_orchestrator(agent_pool))

    logger.info("[OK] Available agents: %s", [a.name for a in all_agents])
    logger.info("=" * 50)

    chatbot_config = {
        'session_name': 'Maine',
        'verbose': False,
    }

    return all_agents, agent_pool, chatbot_config


if __name__ == '__main__':
    import sys

    # ── CLI argument parsing (shared) ────────────────────────────────────────
    from agent_cascade.shared_init import parse_cli_args
    args = parse_cli_args(description='AgentCascade Multi-Agent API Server')

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

        app = create_app(
            all_agents, agent_pool, chatbot_config,
            auto_security=args.auto_security,
        )
        logger.debug("FastAPI app created successfully")
        if args.auto_security:
            logger.info("[OK] Auto-Ask Security mode ENABLED (all tool calls will be security-checked)")
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
