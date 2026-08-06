"""
Multi-Agent API Server — Entry Point

Same agent initialization as start_multi_agent.py, but launches the
WebSocket/REST API server instead of Gradio.

Usage:
    python start_api_server.py [--port PORT] [--auto_security] [--instance-id INSTANCE_ID]
    Open http://127.0.0.1:12345 in your browser.

CLI Flags:
    --port            Port to bind to (default: 12345).
    --auto_security   Start with Auto-Ask Security mode enabled. The security advisor
                      will auto-check all tool calls before execution (same as toggling
                      "Auto-Ask Security" on in the UI). By default, security checks run
                      only when triggered by agent prompts.
    --instance-id     Instance ID for parallel AC instances (alphanumeric + underscore, max 64 chars).
"""

import argparse
import os

# ── Parse instance-id BEFORE any agent_cascade imports ───────────────────────
# This is critical because agent_cascade.log reads AGENT_CASCADE_INSTANCE_ID
# at module import time to set up the logger. If we import log first, the env
# var won't be set yet and we'll get the default (shared) console.log.

parser = argparse.ArgumentParser(description='AgentCascade Multi-Agent API Server')
parser.add_argument("--port", type=int, default=12345, help="Port to bind to (default: 12345)")
parser.add_argument("--instance-id", type=str, default=None, 
                    help="Instance ID for parallel AC instances (alphanumeric + underscore, max 64 chars). "
                         "Use --instance-id= to explicitly clear instance mode and ignore AGENT_CASCADE_INSTANCE_ID env var.")

args, remaining = parser.parse_known_args()

# Determine raw ID: CLI overrides env var; validate ALWAYS (even env-only source)
# None means "not provided" → fall back to env var. Empty string means "explicitly clear".
from agent_cascade.instance_id import validate_instance_id
if args.instance_id is not None:
    raw_id = args.instance_id  # CLI provided (including explicit empty string to clear)
else:
    raw_id = os.getenv("AGENT_CASCADE_INSTANCE_ID", "")  # Fall back to env var

try:
    validated_id = validate_instance_id(raw_id)
    os.environ["AGENT_CASCADE_INSTANCE_ID"] = validated_id  # Always set normalized value
except ValueError as e:
    print(f"[FATAL] {e}")
    raise SystemExit(1)

# ── NOW safe to import agent_cascade modules ────────────────────────────────
from pathlib import Path

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
    from agent_cascade.log import init_logging, logger
    init_logging()

    import sys

    # ── Parse remaining args for --auto_security (instance-id already parsed above) ────
    from agent_cascade.shared_init import parse_cli_args as _parse_base

    base_args = _parse_base(remaining)
    # Merge: auto_security from shared parser
    if hasattr(base_args, 'auto_security'):
        args.auto_security = base_args.auto_security
    else:
        args.auto_security = False

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

        # Use loaded auto_security from pool_settings.json if available, otherwise CLI flag
        effective_auto_security = getattr(agent_pool, '_loaded_auto_security', None)
        if effective_auto_security is None:
            effective_auto_security = args.auto_security

        app = create_app(
            all_agents, agent_pool, chatbot_config,
            auto_security=effective_auto_security,
        )
        logger.debug("FastAPI app created successfully")
        if args.auto_security:
            logger.info("[OK] Auto-Ask Security mode ENABLED (all tool calls will be security-checked)")
    except Exception as e:
        logger.error("[FATAL] Failed to create API server app: %s", e)
        raise SystemExit(1)

    port = args.port
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
            logger.error("[FATAL] Port %d is already in use. Use --port <PORT> or stop the other process.", port)
        else:
            logger.error("[FATAL] Server failed to start: %s", e)
        raise SystemExit(1)
    except Exception as e:
        logger.error("[FATAL] Server crashed: %s", e)
        raise SystemExit(1)
