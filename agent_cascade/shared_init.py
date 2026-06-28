"""
Shared initialization utilities for start_api_server.py and start_multi_agent.py.

Extracts the common infrastructure setup (workspace creation, OperationManager,
AgentPool, tool loading) into reusable functions to eliminate ~180 lines of
duplicated code across both entry points.

Issue: C2 - Extract shared initialization code
"""

import os
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
#  3. Tool instantiation helpers (one per tool, with try/except)
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

    Note: CodeInterpreter.__init__ hardcodes self._operation_manager = None (line 243)
    and does not read it from the cfg dict, so direct attribute assignment is required.
    This is safe — _operation_manager is an internal reference used only for dynamic
    extra-folder resolution (_resolve_extra_folders) and is never mutated externally.
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
    tools = {}

    try:
        tools['system_info'] = _instantiate_system_info(agent_pool)
        logger.debug("[INIT] SystemInfo tool loaded")
    except Exception as e:
        logger.debug("[INIT] SystemInfo skipped (not available): %s", e)

    try:
        tools['image_gen'] = _instantiate_image_gen(llm_cfg)
        logger.debug("[INIT] ImageGen tool loaded")
    except Exception as e:
        logger.debug("[INIT] ImageGen tool skipped (not available): %s", e)

    try:
        tools['web_extractor'] = _instantiate_web_extractor(DEFAULT_WORKSPACE)
        logger.debug("[INIT] WebExtractor tool loaded")
    except Exception as e:
        logger.debug("[INIT] WebExtractor tool skipped (not available): %s", e)

    try:
        tools['code_interpreter'] = _instantiate_code_interpreter(
            DEFAULT_WORKSPACE, operation_manager
        )
        logger.debug("[INIT] CodeInterpreter tool loaded")
    except Exception as e:
        logger.debug("[INIT] CodeInterpreter tool skipped (not available): %s", e)

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

    if 'image_gen' in default_tools:
        try:
            agent.function_map['image_gen'] = _instantiate_image_gen(llm_cfg)
            logger.debug("[INIT] ImageGen loaded for %s", label)
        except Exception as e:
            logger.debug("[INIT] ImageGen skipped for %s: %s", label, e)

    if 'web_extractor' in default_tools:
        try:
            agent.function_map['web_extractor'] = _instantiate_web_extractor(DEFAULT_WORKSPACE)
            logger.debug("[INIT] WebExtractor loaded for %s", label)
        except Exception as e:
            logger.debug("[INIT] WebExtractor skipped for %s: %s", label, e)

    if 'storage' in default_tools:
        try:
            agent.function_map['storage'] = _instantiate_storage()
            logger.debug("[INIT] Storage loaded for %s", label)
        except Exception as e:
            logger.debug("[INIT] Storage skipped for %s: %s", label, e)

    if 'retrieval' in default_tools:
        try:
            agent.function_map['retrieval'] = _instantiate_retrieval(DEFAULT_WORKSPACE)
            logger.debug("[INIT] Retrieval loaded for %s", label)
        except Exception as e:
            logger.debug("[INIT] Retrieval skipped for %s: %s", label, e)

    if 'simple_doc_parser' in default_tools:
        try:
            agent.function_map['simple_doc_parser'] = _instantiate_simple_doc_parser(DEFAULT_WORKSPACE)
            logger.debug("[INIT] SimpleDocParser loaded for %s", label)
        except Exception as e:
            logger.debug("[INIT] SimpleDocParser skipped for %s: %s", label, e)

    if 'doc_parser' in default_tools:
        try:
            agent.function_map['doc_parser'] = _instantiate_doc_parser(DEFAULT_WORKSPACE)
            logger.debug("[INIT] DocParser loaded for %s", label)
        except Exception as e:
            logger.debug("[INIT] DocParser skipped for %s: %s", label, e)

    if 'extract_doc_vocabulary' in default_tools:
        try:
            agent.function_map['extract_doc_vocabulary'] = _instantiate_extract_doc_vocabulary(DEFAULT_WORKSPACE)
            logger.debug("[INIT] ExtractDocVocabulary loaded for %s", label)
        except Exception as e:
            logger.debug("[INIT] ExtractDocVocabulary skipped for %s: %s", label, e)

    if 'code_interpreter' in default_tools:
        try:
            agent.function_map['code_interpreter'] = _instantiate_code_interpreter(
                DEFAULT_WORKSPACE, operation_manager
            )
            logger.debug("[INIT] CodeInterpreter loaded for %s", label)
        except Exception as e:
            logger.debug("[INIT] CodeInterpreter skipped for %s: %s", label, e)

    # Report any tool names in default_tools that this function doesn't handle
    HANDLED_TOOLS = {
        'image_gen', 'web_extractor', 'storage', 'retrieval',
        'simple_doc_parser', 'doc_parser', 'extract_doc_vocabulary', 'code_interpreter',
    }
    unhandled = set(default_tools) - HANDLED_TOOLS
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