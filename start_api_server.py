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
parser.add_argument('--port', type=int, default=12345, help='Port to bind to (default: 12345)')
parser.add_argument(
    '--instance-id',
    type=str,
    default=None,
    help='Instance ID for parallel AC instances (alphanumeric + underscore, max 64 chars). '
    'Use --instance-id= to explicitly clear instance mode and ignore AGENT_CASCADE_INSTANCE_ID env var.')

args, remaining = parser.parse_known_args()

# Determine raw ID: CLI overrides env var; validate ALWAYS (even env-only source)
# None means "not provided" → fall back to env var. Empty string means "explicitly clear".
from agent_cascade.instance_id import validate_instance_id

if args.instance_id is not None:
    raw_id = args.instance_id  # CLI provided (including explicit empty string to clear)
else:
    raw_id = os.getenv('AGENT_CASCADE_INSTANCE_ID', '')  # Fall back to env var

try:
    validated_id = validate_instance_id(raw_id)
    os.environ['AGENT_CASCADE_INSTANCE_ID'] = validated_id  # Always set normalized value
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
#
# GLOBAL DEFAULT ENDPOINT (Tier-4 fallback):
#   - model='whatever_is_on' is the LM Studio "use whatever model is currently loaded"
#     sentinel — llm/oai.py special-cases this string so it never pins a specific model.
#   - This cfg becomes the router's Tier-4 global default: get_endpoint_chain() ALWAYS
#     appends it as the last-resort endpoint for every agent, even one with no assigned
#     endpoints of its own. It is intentionally NOT filtered by cooldown/blacklist.
#   - Values are deliberately left as-is (do not "fix" them); they are only documented +
#     logged at startup so the source of this fallback is visible in the logs.
llm_cfg = {
    'model': 'whatever_is_on',
    'model_server': 'http://localhost:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
    'max_input_tokens': 65536,
}
# NOTE (Fix 4): the Tier-4 visibility log for this cfg is emitted in __main__ AFTER
# init_logging() — logging a bare logger at module-import time would be dropped because
# no handlers are attached yet. See the `logger.info(...Tier-4 fallback...)` call below.


def _bind_socket_with_retry(host: str, port: int, *, max_attempts: int = 20, delay: float = 0.5):
    """Bind a listening socket on ``host:port``, retrying EADDRINUSE for up to ~10 s.

    Needed because after a restart (detached child spawned by the exiting parent) the
    port can still be held in the parent-teardown window; uvicorn's own bind path does
    not set SO_REUSEADDR and treats EADDRINUSE as fatal ``sys.exit(1)``. Returns the
    bound socket, ready to hand to ``uvicorn.Server.run(sockets=[sock])``.

    - EADDRINUSE (errno 98 POSIX / 10048 Windows, or 'address already in use' in str):
      close + warn + sleep(delay) + retry.
    - Any other OSError: re-raised immediately (do not retry unrelated errors).
    - Budget exhausted: raises a clear RuntimeError.
    """
    import socket
    import time

    from agent_cascade.log import logger

    budget = max_attempts * delay
    for attempt in range(1, max_attempts + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen()
            return sock
        except OSError as e:
            is_eaddrinuse = (e.errno in (98, 10048)) or ('address already in use' in str(e).lower())
            if not is_eaddrinuse:
                sock.close()
                raise
            logger.warning('[startup] Port %d still in use (attempt %d/%d): %s — retrying in %.1fs',
                           port, attempt, max_attempts, e, delay)
            sock.close()
            time.sleep(delay)
    raise RuntimeError(f"Port {port} still in use after {max_attempts} attempts (~{budget:.0f}s)")


def initialize_agents():
    """Set up agents, pool, and config. Returns (all_agents, agent_pool, chatbot_config)."""
    logger.info('Initializing Agent Orchestrator (API Server)...')
    logger.info('=' * 50)

    # ── Infrastructure initialization (delegated to shared module) ────────────
    from agent_cascade.shared_init import build_all_agents_list, initialize_infrastructure, load_orchestrator

    operation_mgr, agent_pool = initialize_infrastructure(PROJECT_ROOT, llm_cfg)

    # Tools are already registered by register_standard_tools() during agent loading
    # (via AgentPool._discover_agents → load_agent_template → load_agent).
    # No additional tool distribution needed — AVAILABLE_TOOLS is the single source of truth.

    all_agents = build_all_agents_list(agent_pool, load_orchestrator(agent_pool))

    logger.info('[OK] Available agents: %s', [a.name for a in all_agents])
    logger.info('=' * 50)

    chatbot_config = {
        'session_name': 'Maine',
        'verbose': False,
    }

    return all_agents, agent_pool, chatbot_config


if __name__ == '__main__':
    from agent_cascade.log import init_logging, logger
    init_logging()

    # Startup splash (ASCII banner with version + URL). Additive: runs after logging is
    # up so it also lands in console.log. Non-TTY / AGENT_CASCADE_NO_BANNER → one-liner.
    from agent_cascade.splash import print_startup_banner
    print_startup_banner(mode='API Server', port=args.port, host='127.0.0.1')

    # Fix 4: make the hardcoded Tier-4 global default visible at startup. Emitted here
    # (after init_logging) so it actually reaches the configured handlers — logging a
    # bare module-level logger at import time would be dropped (no handlers yet).
    logger.info(f"[APIRouter] Global default endpoint: '{llm_cfg['model']}' @ {llm_cfg['model_server']} "
                f"(Tier-4 fallback)")

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
        logger.error('[FATAL] Agent initialization failed: %s', e)
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
                logger.warning('Async input listener error: %s', e)
                break

    threading.Thread(target=async_input_listener, daemon=True).start()

    # Create and launch the API server
    try:
        import uvicorn

        from agent_cascade.api_server import create_app

        # Use loaded auto_security from pool_settings.json if available, otherwise CLI flag
        effective_auto_security = getattr(agent_pool, '_loaded_auto_security', None)
        if effective_auto_security is None:
            effective_auto_security = args.auto_security

        app = create_app(
            all_agents,
            agent_pool,
            chatbot_config,
            auto_security=effective_auto_security,
        )
        logger.debug('FastAPI app created successfully')
        if args.auto_security:
            logger.info('[OK] Auto-Ask Security mode ENABLED (all tool calls will be security-checked)')
    except Exception as e:
        logger.error('[FATAL] Failed to create API server app: %s', e)
        raise SystemExit(1)

    port = args.port
    logger.info('\n[OK] API Server ready!')
    logger.info('    -> Open http://127.0.0.1:%d in your browser', port)
    logger.info('    -> WebSocket at ws://127.0.0.1:%d/ws/chat', port)
    logger.info('    -> REST API at http://127.0.0.1:%d/api/', port)
    logger.info('\n[TIP] Type in this terminal to inject messages into the active agent.')
    logger.info('=' * 50)

    # Pre-bind the listening socket ourselves (SO_REUSEADDR + bounded EADDRINUSE retry).
    # uvicorn's bare server.run() path creates the socket via asyncio WITHOUT SO_REUSEADDR
    # and turns an EADDRINUSE into an internal sys.exit(1) — so after a restart, while the
    # parent is still tearing down, the child dies with nothing left running. Pre-binding
    # here (and handing the socket to uvicorn below) makes every restart path robust.
    try:
        sock = _bind_socket_with_retry('127.0.0.1', port)
    except Exception as e:
        logger.error('[FATAL] Port %d is already in use after ~%ds; another process may be holding it. '
                     'Use --port <PORT> or stop the other process. (%s)', port, 20 * 0.5, e)
        raise SystemExit(1)

    # Create server first so signal handler can reference it. host/port are still passed to
    # Config for logging/messages; uvicorn skips its own bind because we pass sockets=[sock].
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')
    server = uvicorn.Server(config)
    agent_pool.server_info = ('127.0.0.1', port)

    # Use shared signal handler from shared_init (Phase 5B — deduplicated shutdown logic)
    from agent_cascade.shared_init import setup_signal_handler
    setup_signal_handler(agent_pool, server=server)

    # Prevent uvicorn from installing its own signal handlers (ours are already registered)
    server.install_signal_handlers = lambda: None

    try:
        # NOTE: do NOT revert to bare server.run() — it re-introduces the port-bind race:
        # uvicorn 0.34.x binds internally without SO_REUSEADDR and calls sys.exit(1) on
        # EADDRINUSE (surfacing as SystemExit, not OSError), so the old errno-98 handler
        # was dead code. The pre-bound socket above makes that path unreachable.
        server.run(sockets=[sock])
    except Exception as e:
        logger.error('[FATAL] Server crashed: %s', e)
        raise SystemExit(1)
