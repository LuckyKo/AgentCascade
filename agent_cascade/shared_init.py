"""
Shared initialization utilities for start_api_server.py and start_multi_agent.py.

Extracts the common infrastructure setup (workspace detection, OperationManager,
AgentPool) into reusable functions to eliminate duplicated code across both entry points.

Issue: C2 - Extract shared initialization code
"""

import os
import signal as _signal_mod  # internal alias — avoids polluting module namespace with bare 'signal'
from pathlib import Path

from agent_cascade.log import logger


# ──────────────────────────────────────────────────────────────────────────────
#  1. Workspace detection & directory creation
# ──────────────────────────────────────────────────────────────────────────────

def detect_workspace_dir(project_root: Path) -> str:
    """
    Detect the workspace directory to use. Prefers a sibling 'AgentWorkspace'
    folder, falls back to <project_root>/workspace. Sets the env var so
    downstream modules can read it.

    Returns the resolved workspace path as a string.
    """
    workspace_dir = os.getenv('QWEN_AGENT_DEFAULT_WORKSPACE')

    if not workspace_dir:
        sibling_ws = project_root.parent / 'AgentWorkspace'
        if sibling_ws.exists():
            workspace_dir = str(sibling_ws)
            logger.info("[INIT] Detected sibling workspace: %s", workspace_dir)
        else:
            workspace_dir = str(project_root / 'workspace')
            logger.info("[INIT] Using local workspace: %s", workspace_dir)

    os.environ['QWEN_AGENT_DEFAULT_WORKSPACE'] = workspace_dir
    return workspace_dir


def ensure_workspace(workspace_dir: str) -> Path:
    """Create the workspace directory if it doesn't exist. Raises SystemExit on failure."""
    try:
        path = Path(workspace_dir)
        path.mkdir(parents=True, exist_ok=True)
        logger.debug("[INIT] Workspace directory verified: %s", workspace_dir)
        return path
    except Exception as e:
        logger.error("[FATAL] Cannot create/access workspace directory %s: %s", workspace_dir, e)
        raise SystemExit(1)


# ──────────────────────────────────────────────────────────────────────────────
#  2. OperationManager & AgentPool initialization
# ──────────────────────────────────────────────────────────────────────────────

def create_operation_manager(workspace_dir: str):
    """Create and return an OperationManager instance."""
    from agent_cascade.operation_manager import OperationManager
    try:
        op_mgr = OperationManager(base_dir=workspace_dir)
        logger.debug("[INIT] OperationManager initialized with base_dir: %s", op_mgr.base_dir)
        return op_mgr
    except Exception as e:
        logger.error("[FATAL] OperationManager initialization failed: %s", e)
        raise SystemExit(1)


def create_agent_pool(llm_cfg, agents_path: str, workspace_dir: str, operation_manager):
    """Create and return an AgentPool instance."""
    from agent_cascade.agent_pool import AgentPool
    try:
        pool = AgentPool(
            llm_cfg, agents_path, workspace_dir=workspace_dir,
            operation_manager=operation_manager,
        )
        logger.debug("[INIT] AgentPool created successfully")
        return pool
    except Exception as e:
        logger.error("[FATAL] AgentPool creation failed: %s", e)
        raise SystemExit(1)


def configure_and_start_pool(agent_pool, idle_timeout: float, idle_check_interval: float):
    """
    Configure pool settings (idle timeout), set the back-reference on OperationManager,
    and start background services.
    """
    # Set back-reference so OperationManager can check pool.stopped during approval loops
    agent_pool.operation_manager.agent_pool = agent_pool

    # Configure pool settings before starting background services (avoids race window)
    if hasattr(agent_pool, 'settings'):
        agent_pool.settings.idle_timeout_seconds = idle_timeout
        agent_pool.settings.idle_check_interval = idle_check_interval

    try:
        agent_pool.start()
        logger.debug("[INIT] AgentPool background services started")
    except Exception as e:
        logger.error("[FATAL] Failed to start AgentPool background services: %s", e)
        raise SystemExit(1)


# ──────────────────────────────────────────────────────────────────────────────
#  3. Orchestrator loading with fallback
# ──────────────────────────────────────────────────────────────────────────────

def load_orchestrator(agent_pool):
    """
    Retrieve the orchestrator from the pool. Falls back to manual loading if not found.
    Raises SystemExit if neither path works.
    """
    from agent_cascade.agent_factory import load_orchestrator_agent

    orchestrator = agent_pool.get_agent('orchestrator')
    if orchestrator is None:
        try:
            # llm_cfg is passed through the pool; we need to retrieve it
            orchestrator = load_orchestrator_agent(agent_pool, agent_pool.llm_cfg)
            logger.info("[INIT] Orchestrator loaded via fallback path")
        except Exception as e:
            logger.error("[FATAL] Orchestrator agent could not be loaded: %s", e)
            raise SystemExit(1)

    return orchestrator


# ──────────────────────────────────────────────────────────────────────────────
#  4. Build the all_agents list from pool + orchestrator
# ──────────────────────────────────────────────────────────────────────────────

def build_all_agents_list(agent_pool, orchestrator):
    """Return a list of [orchestrator, *other_agents_from_pool]."""
    all_agents = [orchestrator]
    for agent_name in agent_pool.list_agents():
        if agent_name != 'orchestrator':
            sub_agent = agent_pool.get_agent(agent_name)
            if sub_agent:
                all_agents.append(sub_agent)

    if not all_agents:
        logger.error("[FATAL] No agents available after initialization")
        raise SystemExit(1)

    return all_agents


# ──────────────────────────────────────────────────────────────────────────────
#  5. High-level convenience function (for callers that want one-liner init)
# ──────────────────────────────────────────────────────────────────────────────

def initialize_infrastructure(project_root: Path, llm_cfg):
    """
    Full infrastructure initialization in a single call.

    Parameters
    ----------
    project_root : Path
        Root of the AgentCascade project (parent of ``agents/`` directory).
    llm_cfg : dict
        LLM configuration dictionary.

    Returns
    -------
    tuple[OperationManager, AgentPool]
        (operation_manager, agent_pool)
    """
    workspace_dir = detect_workspace_dir(project_root)
    ensure_workspace(workspace_dir)

    idle_timeout = float(os.getenv('QWEN_AGENT_IDLE_TIMEOUT', 300.0))
    idle_check_interval = float(os.getenv('QWEN_AGENT_IDLE_CHECK_INTERVAL', 60.0))

    operation_mgr = create_operation_manager(workspace_dir)

    agents_path = str(project_root / 'agents')
    agent_pool = create_agent_pool(llm_cfg, agents_path, workspace_dir, operation_mgr)
    configure_and_start_pool(agent_pool, idle_timeout, idle_check_interval)

    return operation_mgr, agent_pool


# ──────────────────────────────────────────────────────────────────────────────
#  6. Signal handler for graceful shutdown (shared across entry points)
# ──────────────────────────────────────────────────────────────────────────────

def setup_signal_handler(agent_pool, server=None):
    """Set up graceful shutdown signal handlers for SIGINT and SIGTERM.

    Parameters
    ----------
    agent_pool : AgentPool
        The agent pool whose ``stopped`` flag will be set on shutdown.
    server : uvicorn.Server | None
        Optional uvicorn server object. If provided, its ``should_exit`` flag
        is set for a clean uvicorn shutdown (avoids resource leaks from sys.exit).
    """
    def handle_shutdown(signum, frame):
        logger.info("\n[INFO] Initiating graceful shutdown...")
        agent_pool.stopped = True
        if hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
            try:
                agent_pool.operation_manager.cleanup_backups()
            except Exception as e:
                logger.warning("Cleanup backups failed during shutdown: %s", e)
        if server is not None:
            # Set should_exit for graceful uvicorn shutdown (avoids resource leaks from sys.exit)
            server.should_exit = True

    _signal_mod.signal(_signal_mod.SIGINT, handle_shutdown)
    if os.name != 'nt':
        _signal_mod.signal(_signal_mod.SIGTERM, handle_shutdown)