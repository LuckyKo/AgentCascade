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
import asyncio
import os
import sys

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
# boot_timing captures _T0 here (near process start) so all later [BOOT-TIMING]
# marks share one origin. Pure logging — no behavior change.
from agent_cascade.utils import boot_timing

boot_timing.mark('boot_timing_ready')  # instance-id parse predates _T0; marks boot_timing coming online

from pathlib import Path

# ── Workspace Detection (shared) ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.absolute()
from agent_cascade.shared_init import detect_workspace_dir, ensure_workspace

WORKSPACE_DIR = detect_workspace_dir(PROJECT_ROOT)
ensure_workspace(WORKSPACE_DIR)
boot_timing.mark('imports_workspace')

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
from agent_cascade.settings import SKILL_ALWAYS_PROTECTED_DEFAULT, format_skill_always_protected

llm_cfg = {
    'model': 'whatever_is_on',
    'model_server': 'http://localhost:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
    'max_input_tokens': 65536,
    # LLM generation params (persisted to config/api_endpoints.json::default_llm_cfg).
    'max_output_tokens': 4096,
    'stop_sequences': '',  # LLM stop-sequences param (NOT the UI Stop button).
    'timeout': 120.0,
    # llm_cfg-sourced skill/memory settings (persisted to config/pool_settings.json).
    'memory_hint_skill_suggestions': True,
    'memory_hint_skill_threshold': 0.0,
    'memory_hint_skill_autoload_enabled': True,
    'memory_hint_skill_autoload_threshold': 3,
    'skill_always_protected': format_skill_always_protected(SKILL_ALWAYS_PROTECTED_DEFAULT),
}
# NOTE: the Tier-4 visibility log for this cfg is emitted in __main__ AFTER
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


# ── BUG_0353: fatal-exception hardening (observability + self-recovery) ──────
# These hooks are INERT until an unhandled exception occurs; the normal boot path
# is unchanged. Handlers resolve the LIVE agent_cascade logger at call time (local
# import) so they reach the per-instance child logger when --instance-id is set.

_FATAL_EXCEPTHOOK_INSTALLED = False
_prev_asyncio_handler = None


def _ac_asyncio_exception_handler(loop, context):
    """Log an unhandled asyncio task exception to console.log at CRITICAL.

    Installed as the running loop's exception handler (see _ac_serve_with_handler)
    so that unhandled exceptions raised inside a task — which do NOT propagate out
    of server.serve() and are the silent-death signature of BUG_0353 — are
    diagnosable in console.log instead of vanishing.

    Truly inert absent an unhandled task exception: asyncio's call_exception_handler
    also fires for non-exception loop diagnostics (slow-callback warnings, "Task was
    destroyed but it is pending!", "coroutine ignored GeneratorExit"). Those are
    delegated to the previously-installed handler (captured in _prev_asyncio_handler)
    so we never suppress asyncio's standard handling of them.
    """
    exc = context.get('exception')
    if exc is None:
        # Non-exception loop diagnostic: route to the previous handler (asyncio's
        # default if none was installed). In the unit test _prev is None → dropped.
        prev = _prev_asyncio_handler
        if prev is not None:
            prev(loop, context)
        return
    try:
        from agent_cascade.log import logger  # live logger (instance-safe)
        logger.critical('[FATAL] unhandled asyncio exception: %s', exc, exc_info=exc)
    except Exception:
        pass  # never let the handler itself raise (would mask asyncio's default routing)


async def _ac_serve_with_handler(server, sock):
    """Serve via uvicorn's Server.serve() with the fatal-exception handler armed.

    Provably equivalent to server.run(sockets=[sock]) (uvicorn 0.34.3: run() ==
    config.setup_event_loop(); asyncio.run(self.serve(sockets))) EXCEPT it sets the
    running loop's exception handler FIRST (capturing the previous handler so
    non-exception diagnostics keep their standard routing), so unhandled task
    exceptions are logged to console.log. The pre-bound sock is still handed to
    uvicorn via sockets=[sock] — do NOT revert to bare server.run() (it
    re-introduces the port-bind race).
    """
    global _prev_asyncio_handler
    loop = asyncio.get_running_loop()
    _prev_asyncio_handler = loop.get_exception_handler()
    loop.set_exception_handler(_ac_asyncio_exception_handler)
    await server.serve(sockets=[sock])


def _install_fatal_excepthook():
    """Wrap sys.excepthook so any unhandled main-thread exception is logged to
    console.log at CRITICAL with a [FATAL] marker BEFORE the original hook runs.

    The original hook (installed by init_logging(), _logging_excepthook) is captured
    and delegated to, so normal traceback-to-stderr / exit behavior is preserved.
    Idempotent: safe to call more than once (would otherwise double-log).
    """
    global _FATAL_EXCEPTHOOK_INSTALLED
    if _FATAL_EXCEPTHOOK_INSTALLED:
        return
    original = sys.excepthook

    def _fatal_excepthook(exc_type, exc_value, exc_tb):
        try:
            from agent_cascade.log import logger  # live logger (instance-safe)
            logger.critical('[FATAL] unhandled exception', exc_info=(exc_type, exc_value, exc_tb))
        except Exception:
            pass  # never mask the original hook; logging may be unavailable at shutdown
        original(exc_type, exc_value, exc_tb)

    sys.excepthook = _fatal_excepthook
    _FATAL_EXCEPTHOOK_INSTALLED = True


class _StderrTee:
    """Write-through stderr wrapper for in-process forensic crash capture.

    Forwards every write to BOTH the original stderr stream AND a line-buffered
    log handle, so an unhandled crash on a launch path that does NOT redirect
    fd 2 (direct ``python``, a debugger, an IDE) is still diagnosable from
    ``logs/server_stderr.log``. The original stream is never suppressed. Unknown
    attributes (isatty, fileno, encoding, ...) are delegated to the original so
    callers like uvicorn/the splash keep working.
    """

    def __init__(self, original, log_handle):
        # Assign both before any delegation: __getattr__ resolves missing attrs via
        # self._original, so an unset one would recurse rather than raise AttributeError.
        self._original = original
        self._file = log_handle

    def write(self, data):
        # Capture FIRST: a console encode failure (e.g. a UnicodeEncodeError rendering a
        # traceback) must not skip the forensic line this tee exists to capture. A
        # capture-write failure is swallowed so it can never break the caller.
        try:
            self._file.write(data)
            self._file.flush()           # flush per write so a hard kill loses nothing
        except Exception:
            pass
        n = self._original.write(data)   # then the real stream; its exception still propagates
        return n

    def flush(self):
        try:
            self._original.flush()
        finally:
            try:
                self._file.flush()
            except Exception:
                pass

    def writable(self):
        return True

    def __getattr__(self, name):
        # Delegate everything else to the original stream (isatty/fileno/encoding...).
        return getattr(self._original, name)


def _install_stderr_tee():
    """Tee sys.stderr to ``logs/server_stderr.log`` to close a forensic capture gap.

    start.bat already redirects fd 2 to that file at the OS level, but every OTHER
    launch path sends stderr to a console that is lost on a hard crash. This tees
    stderr to the same file so the crash signature is captured on EVERY path.

    Self-disabling on the start.bat path: if stderr is already redirected to the
    capture file (detected via os.path.samefile, or a ``server_stderr.log`` name
    suffix when the fd can't be stat'd), it is left untouched to avoid double
    writes. On any setup failure a one-line warning is written to the original
    stderr and stderr is left unchanged — logging setup must never crash the server.
    """
    try:
        if isinstance(sys.stderr, _StderrTee):   # already teed (idempotent)
            return
        log_path = os.path.join(PROJECT_ROOT, 'logs', 'server_stderr.log')
        orig = sys.stderr
        orig_name = getattr(orig, 'name', None)
        if orig_name:
            try:
                if os.path.samefile(orig_name, log_path):
                    return                       # start.bat path: already redirected
            except (OSError, ValueError):
                pass
            if str(orig_name).endswith('server_stderr.log'):
                return                           # fd 2 -> file we can't stat by path

        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handle = open(log_path, 'a', encoding='utf-8', errors='backslashreplace',
                      buffering=1)  # buffering=1 == line-buffered text (flush per line)
        sys.stderr = _StderrTee(orig, handle)
    except Exception as e:
        # Graceful degradation: never crash the server over logging setup.
        try:
            sys.stderr.write(f'[WARN] stderr tee disabled: {e}\n')
        except Exception:
            pass


def initialize_agents():
    """Set up agents, pool, and config. Returns (all_agents, agent_pool, chatbot_config)."""
    logger.info('Initializing Agent Orchestrator (API Server)...')
    logger.info('=' * 50)

    # ── Infrastructure initialization (delegated to shared module) ────────────
    from agent_cascade.shared_init import (
        build_all_agents_list, initialize_infrastructure, load_orchestrator, initialize_mcp_servers,
    )

    operation_mgr, agent_pool = initialize_infrastructure(PROJECT_ROOT, llm_cfg)

    # Connect enabled MCP servers now that templates (and their function_map) exist.
    # Best-effort: never aborts boot. See shared_init.initialize_mcp_servers (BUG_0269).
    initialize_mcp_servers(agent_pool, PROJECT_ROOT)

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

    # BUG_0353: arm the fatal-exception hooks so an unhandled main-thread exception is
    # logged to console.log at CRITICAL before the original hook runs. Inert on the
    # normal boot path (only fires on an unhandled exception).
    _install_fatal_excepthook()

    # Tee stderr to logs/server_stderr.log so a crash launched outside
    # start.bat (direct python/debugger/IDE) is diagnosable. Independent of the
    # excepthook above; no-op when fd 2 is already redirected there (start.bat).
    _install_stderr_tee()

    # Startup splash (ASCII banner with version + URL). Additive: runs after logging is
    # up so it also lands in console.log. Non-TTY / AGENT_CASCADE_NO_BANNER → one-liner.
    # Resolve the bind host once, early, so the splash + [OK] banner both report the
    # real interface in mesh mode (0.0.0.0 binds but is shown as 127.0.0.1). The
    # fail-closed boot guard still runs immediately before the socket bind (below).
    from agent_cascade import auth as _auth
    _bind_host = _auth.effective_host()
    disp_host = '127.0.0.1' if _bind_host in ('0.0.0.0', '0.0.0.0:') else _bind_host
    from agent_cascade.splash import print_startup_banner
    print_startup_banner(mode='API Server', port=args.port, host=disp_host)

    # Make the hardcoded Tier-4 global default visible at startup. Emitted here
    # (after init_logging) so it actually reaches the configured handlers — logging a
    # bare module-level logger at import time would be dropped (no handlers yet).
    logger.info(f"[APIRouter] Global default endpoint: '{llm_cfg['model']}' @ {llm_cfg['model_server']} "
                f"(Tier-4 fallback)")

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
        boot_timing.mark('create_app')
        logger.debug('FastAPI app created successfully')
        if args.auto_security:
            logger.info('[OK] Auto-Ask Security mode ENABLED (all tool calls will be security-checked)')
    except Exception as e:
        logger.error('[FATAL] Failed to create API server app: %s', e)
        raise SystemExit(1)

    port = args.port
    logger.info('\n[OK] API Server ready!')
    logger.info('    -> Open http://%s:%d in your browser', disp_host, port)
    logger.info('    -> WebSocket at ws://%s:%d/ws/chat', disp_host, port)
    logger.info('    -> REST API at http://%s:%d/api/', disp_host, port)
    logger.info('\n[TIP] Type in this terminal to inject messages into the active agent.')
    logger.info('=' * 50)

    # Pre-bind the listening socket ourselves (SO_REUSEADDR + bounded EADDRINUSE retry).
    # uvicorn's bare server.run() path creates the socket via asyncio WITHOUT SO_REUSEADDR
    # and turns an EADDRINUSE into an internal sys.exit(1) — so after a restart, while the
    # parent is still tearing down, the child dies with nothing left running. Pre-binding
    # here (and handing the socket to uvicorn below) makes every restart path robust.
    # BUG_0435: bind interface from AGENT_CASCADE_HOST (loopback default) and the
    # fail-closed boot check — parity with start_multi_agent. The guard runs BEFORE
    # the socket-bind try so its RuntimeError is never swallowed as "port in use".
    # _auth and _bind_host were resolved early (above the splash); enforce fail-closed
    # now, immediately before the socket bind, so a non-loopback bind with no key can
    # never be swallowed as a generic "port in use" error.
    try:
        _auth.check_boot_preconditions(_bind_host)
    except RuntimeError as e:
        logger.error('[FATAL] %s', e)
        raise SystemExit(1)
    try:
        sock = _bind_socket_with_retry(_bind_host, port)
    except Exception as e:
        logger.error('[FATAL] Port %d is already in use after ~%ds; another process may be holding it. '
                     'Use --port <PORT> or stop the other process. (%s)', port, 20 * 0.5, e)
        raise SystemExit(1)
    boot_timing.mark('bind')

    # Create server first so signal handler can reference it. host/port are still passed to
    # Config for logging/messages; uvicorn skips its own bind because we pass sockets=[sock].
    config = uvicorn.Config(app, host=_bind_host, port=port, log_level='warning')
    server = uvicorn.Server(config)
    agent_pool.server_info = (_bind_host, port)

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
        #
        # uvicorn 0.34.x: run the equivalent two-step (Server.run ==
        # config.setup_event_loop(); asyncio.run(self.serve(sockets))) through
        # _ac_serve_with_handler so the running loop's exception handler is armed FIRST
        # — unhandled task exceptions are logged to console.log at CRITICAL instead of
        # vanishing (the silent-death signature).
        #
        # uvicorn 0.36.0+ removed setup_event_loop (replaced by get_loop_factory); there
        # the standard run() path handles the loop, so degrade to it (still serves via the
        # pre-bound sock; the fatal hook is simply not armed) instead of raising and
        # crash-looping the watchdog. setup_event_loop was removed in 0.36.0 and
        # re-added as a raising stub in 0.36.1 — so it is present on 0.34.x and on 0.36.0+
        # alike, and is NOT a usable predicate (get_loop_factory is absent on 0.34.x /
        # present on 0.36.0+).
        boot_timing.mark('uvicorn_ready')
        if hasattr(config, 'get_loop_factory'):
            server.run(sockets=[sock])
        else:
            config.setup_event_loop()
            asyncio.run(_ac_serve_with_handler(server, sock))
    except Exception as e:
        logger.error('[FATAL] Server crashed: %s', e)
        raise SystemExit(1)
