"""
Shared initialization utilities for start_api_server.py and start_multi_agent.py.

Extracts the common infrastructure setup (workspace creation, OperationManager,
AgentPool, tool loading) into reusable functions to eliminate ~180 lines of
duplicated code across both entry points.

Issue: C2 - Extract shared initialization code
"""

import os
import signal as _signal_mod  # internal alias — avoids polluting module namespace with bare 'signal'
from pathlib import Path

from agent_cascade.log import logger
from agent_cascade.settings import DEFAULT_WORKSPACE


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
#  3. Tool instantiation helpers (one per tool)
# ──────────────────────────────────────────────────────────────────────────────

def _instantiate_image_gen(llm_cfg):
    """Instantiate the ImageGen tool."""
    from agent_cascade.tools import image_gen
    return image_gen.ImageGen(cfg={'llm_cfg': llm_cfg})


def _instantiate_web_extractor(work_dir: str):
    """Instantiate the WebExtractor tool."""
    from agent_cascade.tools import web_extractor
    return web_extractor.WebExtractor(cfg={'work_dir': work_dir})


def _instantiate_storage():
    """Instantiate the Storage tool."""
    from agent_cascade.tools import storage
    return storage.Storage()


def _instantiate_retrieval(work_dir: str):
    """Instantiate the Retrieval tool."""
    from agent_cascade.tools import retrieval as retrieval_mod
    return retrieval_mod.Retrieval(cfg={'work_dir': work_dir})


def _instantiate_simple_doc_parser(work_dir: str):
    """Instantiate the SimpleDocParser tool."""
    from agent_cascade.tools import simple_doc_parser
    return simple_doc_parser.SimpleDocParser(cfg={'work_dir': work_dir})


def _instantiate_doc_parser(work_dir: str):
    """Instantiate the DocParser tool."""
    from agent_cascade.tools import doc_parser
    return doc_parser.DocParser(cfg={'work_dir': work_dir})


def _instantiate_extract_doc_vocabulary(work_dir: str):
    """Instantiate the ExtractDocVocabulary tool."""
    from agent_cascade.tools import extract_doc_vocabulary
    return extract_doc_vocabulary.ExtractDocVocabulary(cfg={'work_dir': work_dir})


def _instantiate_code_interpreter(work_dir: str, operation_manager=None):
    """Instantiate the CodeInterpreter tool and attach OperationManager if provided.

    Note: CodeInterpreter.__init__ hardcodes self._operation_manager = None and does not
    read it from the cfg dict, so direct attribute assignment is required. This is safe —
    _operation_manager is an internal reference used only for dynamic extra-folder
    resolution (_resolve_extra_folders) and is never mutated externally.
    """
    from agent_cascade.tools import code_interpreter
    inst = code_interpreter.CodeInterpreter(cfg={'work_dir': work_dir})
    if operation_manager:
        inst._operation_manager = operation_manager  # cfg doesn't support this — direct assignment required
    return inst


def _instantiate_system_info(agent_pool):
    """Instantiate the SystemInfo tool."""
    from agent_cascade.tools.custom import SystemInfo
    return SystemInfo(agent_pool=agent_pool)


# ──────────────────────────────────────────────────────────────────────────────
#  Tool registry: maps tool_name -> (factory_fn, list_of_required_param_names)
#
# Each entry declares which keyword arguments the factory needs. The loader
# inspects this list and builds a kwargs dict from whatever is available at call
# time. If a required param is None/missing the tool is skipped (logged at DEBUG).
# ──────────────────────────────────────────────────────────────────────────────

TOOL_REGISTRY = {
    'image_gen':            (_instantiate_image_gen,           ['llm_cfg']),
    'web_extractor':        (_instantiate_web_extractor,       ['work_dir']),
    'storage':              (_instantiate_storage,             []),
    'retrieval':            (_instantiate_retrieval,           ['work_dir']),
    'simple_doc_parser':    (_instantiate_simple_doc_parser,   ['work_dir']),
    'doc_parser':           (_instantiate_doc_parser,          ['work_dir']),
    'extract_doc_vocabulary': (_instantiate_extract_doc_vocabulary, ['work_dir']),
    'code_interpreter':     (_instantiate_code_interpreter,    ['work_dir', 'operation_manager']),
    'system_info':          (_instantiate_system_info,         ['agent_pool']),
}

# Human-readable display names (used in log messages)
TOOL_DISPLAY = {
    'image_gen':              'ImageGen',
    'web_extractor':          'WebExtractor',
    'storage':                'Storage',
    'retrieval':              'Retrieval',
    'simple_doc_parser':      'SimpleDocParser',
    'doc_parser':             'DocParser',
    'extract_doc_vocabulary': 'ExtractDocVocabulary',
    'code_interpreter':       'CodeInterpreter',
    'system_info':            'SystemInfo',
}


def _load_tool(name, registry, work_dir=None, llm_cfg=None, operation_manager=None, agent_pool=None):
    """
    Look up *name* in the tool registry and instantiate it.

    Returns the tool instance on success, or ``None`` on failure (logged at WARNING).
    """
    if name not in registry:
        return None

    factory, required_params = registry[name]
    display_name = TOOL_DISPLAY.get(name, name)

    # Source pool of all available keyword arguments
    param_pool = {
        'work_dir': work_dir or DEFAULT_WORKSPACE,
        'llm_cfg': llm_cfg,
        'operation_manager': operation_manager,
        'agent_pool': agent_pool,
    }

    # Build kwargs from the subset the factory actually needs
    kwargs = {}
    for p in required_params:
        val = param_pool.get(p)
        if val is None and p != 'work_dir':  # work_dir always has a default via DEFAULT_WORKSPACE
            logger.debug("[INIT] %s skipped (missing required param '%s')", display_name, p)
            return None
        kwargs[p] = val

    try:
        instance = factory(**kwargs)
        logger.debug("[INIT] %s tool loaded", display_name)
        return instance
    except Exception as e:
        logger.warning("[INIT] %s tool skipped (not available): %s", display_name, e)
        return None


def _load_tool_for_agent(name, registry, agent, label, work_dir=None, llm_cfg=None, operation_manager=None, agent_pool=None):
    """
    Instantiate *name* for a single agent and store it in ``agent.function_map``.

    Returns the tool instance on success, or ``None`` on failure (logged at WARNING).
    """
    if name not in registry:
        return None

    factory, required_params = registry[name]
    display_name = TOOL_DISPLAY.get(name, name)

    # Source pool of all available keyword arguments
    param_pool = {
        'work_dir': work_dir or DEFAULT_WORKSPACE,
        'llm_cfg': llm_cfg,
        'operation_manager': operation_manager,
        'agent_pool': agent_pool,
    }

    kwargs = {}
    for p in required_params:
        val = param_pool.get(p)
        if val is None and p != 'work_dir':  # work_dir always has a default via DEFAULT_WORKSPACE
            logger.debug("[INIT] %s skipped for %s (missing required param '%s')", display_name, label, p)
            return None
        kwargs[p] = val

    try:
        instance = factory(**kwargs)
        if instance is not None:
            agent.function_map[name] = instance
        logger.debug("[INIT] %s loaded for %s", display_name, label)
        return instance
    except Exception as e:
        logger.warning("[INIT] %s skipped for %s: %s", display_name, label, e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
#  4. Tool loading with error handling (returns dict of name -> instance)
# ──────────────────────────────────────────────────────────────────────────────

def load_tools_shared(
    llm_cfg, agent_pool=None, operation_manager=None
):
    """
    Instantiate *shared* tool instances that can be distributed across agents.
    Returns a dict {tool_name: instance}.

    This is the pattern used by start_api_server.py (tools are shared in memory).
    All tools use DEFAULT_WORKSPACE from settings — no per-call work_dir needed.
    """
    # Shared tools subset: only the tools that make sense to share across agents
    SHARED_TOOL_NAMES = ('system_info', 'web_extractor', 'code_interpreter')

    # Explicitly pass DEFAULT_WORKSPACE so the dependency is visible in the call site
    work_dir = DEFAULT_WORKSPACE

    tools = {}
    for name in SHARED_TOOL_NAMES:
        instance = _load_tool(
            name, TOOL_REGISTRY,
            work_dir=work_dir, llm_cfg=llm_cfg,
            operation_manager=operation_manager, agent_pool=agent_pool,
        )
        if instance is not None:
            tools[name] = instance

    # Safety net: catch SHARED_TOOL_NAMES entries missing from TOOL_REGISTRY
    missing = set(SHARED_TOOL_NAMES) - set(TOOL_REGISTRY)
    if missing:
        logger.debug("[INIT] Shared tools not in registry (likely a bug): %s", sorted(missing))

    if not tools:
        logger.warning("[INIT] No tools were successfully loaded — agents will have limited capabilities")
    return tools


def load_tools_per_agent(
    agent, default_tools, llm_cfg, work_dir: str,
    agent_pool=None, operation_manager=None, agent_name: str = ''
):
    """
    Instantiate tools for a *single* agent (per-agent pattern from start_multi_agent.py).

    Only loads tools that are listed in ``default_tools``. Each tool gets its own
    try/except block to avoid cascading failures.
    """
    label = f"agent '{agent_name}'" if agent_name else 'orchestrator'

    # DDGSearch and system_info are added by callers directly (lightweight, no shared state)
    # We only handle the built-in tools here.

    for name in default_tools:
        _load_tool_for_agent(
            name, TOOL_REGISTRY, agent, label,
            work_dir=work_dir, llm_cfg=llm_cfg,
            operation_manager=operation_manager, agent_pool=agent_pool,
        )

    # Report any tool names in default_tools that this function doesn't handle
    unhandled = set(default_tools) - set(TOOL_REGISTRY)
    if unhandled:
        logger.debug("[INIT] Ignoring non-built-in tools for %s: %s", label, sorted(unhandled))

    loaded_count = len([t for t in default_tools if t in agent.function_map])
    if default_tools and loaded_count == 0:
        logger.warning(
            "[INIT] No tools were successfully loaded for %s — agent will have limited capabilities", label
        )


# ──────────────────────────────────────────────────────────────────────────────
#  5. Orchestrator loading with fallback
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
#  6. Build the all_agents list from pool + orchestrator
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
#  7. High-level convenience function (for callers that want one-liner init)
# ──────────────────────────────────────────────────────────────────────────────

def initialize_infrastructure(project_root: Path, llm_cfg, use_shared_tools: bool = True):
    """
    Full infrastructure initialization in a single call.

    Parameters
    ----------
    project_root : Path
        Root of the AgentCascade project (parent of ``agents/`` directory).
    llm_cfg : dict
        LLM configuration dictionary.
    use_shared_tools : bool
        If True, tools are instantiated once and shared across agents
        (start_api_server.py pattern).  If False, each agent gets its own
        tool instances (start_multi_agent.py pattern).

    Returns
    -------
    tuple[OperationManager, AgentPool, dict | None]
        (operation_manager, agent_pool, shared_tools_dict)
        ``shared_tools_dict`` is None when ``use_shared_tools=False``.
    """
    workspace_dir = detect_workspace_dir(project_root)
    ensure_workspace(workspace_dir)

    idle_timeout = float(os.getenv('QWEN_AGENT_IDLE_TIMEOUT', 300.0))
    idle_check_interval = float(os.getenv('QWEN_AGENT_IDLE_CHECK_INTERVAL', 60.0))

    operation_mgr = create_operation_manager(workspace_dir)

    agents_path = str(project_root / 'agents')
    agent_pool = create_agent_pool(llm_cfg, agents_path, workspace_dir, operation_mgr)
    configure_and_start_pool(agent_pool, idle_timeout, idle_check_interval)

    if use_shared_tools:
        shared_tools = load_tools_shared(llm_cfg, agent_pool, operation_mgr)
    else:
        shared_tools = None

    return operation_mgr, agent_pool, shared_tools


# ──────────────────────────────────────────────────────────────────────────────
#  8. Signal handler for graceful shutdown (shared across entry points)
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