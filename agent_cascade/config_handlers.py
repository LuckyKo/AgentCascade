"""Configuration update handlers for the AgentCascade API server.

Extracted from ws_handlers.py handle_update_config (Phase 4 refactoring).
Each config key has a dedicated handler function registered via decorator.
The ConfigUpdateRouter dispatches incoming config updates to the correct handler.

Import chain: config_handlers -> tools/mcp_manager, api_server.LLM_CONFIG_KEYS
No circular dependencies — this module imports only from existing modules.
"""

from collections import deque as Deque
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from agent_cascade.settings import (
    CI_MIN_EXECUTION_TIMEOUT, CI_MIN_WATCHDOG_TIMEOUT, CI_MIN_STALE_CONTAINER_TTL,
)

# ── LLM config key set (defined locally to avoid circular import with api_server) ────
LLM_CONFIG_KEYS = frozenset({
    'model', 'api_base', 'api_key', 'temperature', 'max_tokens',
    'max_input_tokens', 'max_output_tokens', 'top_p', 'frequency_penalty',
    'presence_penalty', 'stop', 'timeout', 'model_type'
})


# ── Registry of config key → handler function ────────────────────────────
CONFIG_HANDLERS: Dict[str, Callable] = {}


def register_config_handler(key: str) -> Callable:
    """Decorator to register a handler for a specific config key."""
    def decorator(func: Callable) -> Callable:
        CONFIG_HANDLERS[key] = func
        return func
    return decorator


# ── Individual config handlers (preserving exact original behavior) ───────

@register_config_handler('mcpServers')
def _handle_mcp_servers(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Initialize MCP tools from mcpServers config and register with all agents."""
    from agent_cascade.log import logger as _logger
    mcp_servers = ui_cfg['mcpServers']
    try:
        from agent_cascade.tools.mcp_manager import MCPManager
        mcp_tools = MCPManager().initConfig({'mcpServers': mcp_servers})
        for tool in mcp_tools:
            for agent_inst in agents:
                if tool.name not in agent_inst.function_map:
                    agent_inst.function_map[tool.name] = tool
        _logger.info("[MCP] Eagerly loaded %d tools.", len(mcp_tools))
    except Exception as e:
        _logger.warning("[MCP] Eager initialization failed: %s", e)


@register_config_handler('work_access_folders_ro')
def _handle_work_folders(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update read-only and read-write work folders (defense-in-depth: only if changed)."""
    from agent_cascade.log import logger as _logger
    if agent_pool is None or not hasattr(agent_pool, 'operation_manager') or agent_pool.operation_manager is None:
        return
    om = agent_pool.operation_manager
    ro_new = [p.strip() for p in ui_cfg.get('work_access_folders_ro', []) if p.strip()]
    rw_new = [p.strip() for p in ui_cfg.get('work_access_folders_rw', []) if p.strip()]
    ro_current = [str(p) for p in om.extra_work_folders_ro]
    rw_current = [str(p) for p in om.extra_work_folders_rw]
    ro_sorted = sorted([p.lower() for p in ro_new])
    rw_sorted = sorted([p.lower() for p in rw_new])
    ro_curr_sorted = sorted([p.lower() for p in ro_current])
    rw_curr_sorted = sorted([p.lower() for p in rw_current])
    if ro_sorted != ro_curr_sorted or rw_sorted != rw_curr_sorted:
        om.set_extra_work_folders(ro_new, rw_new)
    else:
        _logger.debug("[update_config] Extra work folders unchanged (RO=%d, RW=%d)", len(ro_new), len(rw_new))


@register_config_handler('work_access_folders_rw')
def _handle_work_folders_rw(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Alias handler — work_access_folders_rw uses the same logic as ro."""
    _handle_work_folders(ui_cfg, agent_pool, agents)


@register_config_handler('default_workspace')
def _handle_default_workspace(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update default workspace base_dir if changed."""
    from agent_cascade.log import logger as _logger
    if agent_pool is None or not hasattr(agent_pool, 'operation_manager') or agent_pool.operation_manager is None:
        return
    new_ws = ui_cfg['default_workspace']
    if new_ws:
        new_ws_path = Path(new_ws).resolve()
        if new_ws_path != agent_pool.operation_manager.base_dir:
            agent_pool.operation_manager.set_base_dir(new_ws)
        else:
            _logger.debug("[update_config] Base workspace unchanged")


@register_config_handler('idle_timeout_seconds')
def _handle_idle_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update idle timeout setting on the agent pool."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        try:
            val = float(ui_cfg['idle_timeout_seconds'])
            if val != val or val == float('inf'):  # NaN or inf check
                raise ValueError(f"Invalid timeout value: {val}")
            agent_pool.settings.idle_timeout_seconds = max(0.0, val)
        except Exception as e:
            _logger.warning(f"Failed to set idle timeout: {e}")


@register_config_handler('system_agent_idle_timeout_seconds')
def _handle_system_agent_idle_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update idle timeout setting for system agents (Compressor, Security)."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        try:
            val = float(ui_cfg['system_agent_idle_timeout_seconds'])
            if val != val or val == float('inf'):  # NaN or inf check
                raise ValueError(f"Invalid timeout value: {val}")
            agent_pool.settings.system_agent_idle_timeout_seconds = max(0.0, val)
        except Exception as e:
            _logger.warning(f"Failed to set system agent idle timeout: {e}")


@register_config_handler('approval_timeout_seconds')
def _handle_approval_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Set approval timeout on the operation manager."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None:
        try:
            agent_pool.operation_manager.set_approval_timeout(
                int(ui_cfg['approval_timeout_seconds'])
            )
        except Exception as e:
            _logger.warning(f"Failed to set approval timeout: {e}")


@register_config_handler('enable_approval_timeout')
def _handle_enable_approval_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle approval timeout enablement on the operation manager."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None:
        try:
            agent_pool.operation_manager.set_enable_timeout(
                bool(ui_cfg['enable_approval_timeout'])
            )
        except Exception as e:
            _logger.warning(f"Failed to set approval timeout toggle: {e}")


@register_config_handler('max_parallel_agents')
def _handle_max_parallel_agents(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update max parallel agents and resize the thread pool executor."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg['max_parallel_agents'])
        agent_pool.settings.max_workers = max(1, val)
        if hasattr(agent_pool._execution, 'executor') and agent_pool._execution.executor is not None:
            agent_pool._execution.resize_executor(agent_pool.settings.max_workers)
        else:
            _logger.warning("[THREAD_POOL] resize_executor skipped — executor is None (pool just initialized?)")


@register_config_handler('auto_continue')
def _handle_auto_continue(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update auto_continue setting on the agent pool."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.auto_continue = bool(ui_cfg['auto_continue'])


@register_config_handler('inner_loop_detect_enabled')
def _handle_inner_loop_detect(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle inner-loop detection during streaming (catches LLM generation loops mid-stream)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.inner_loop_detect_enabled = bool(ui_cfg['inner_loop_detect_enabled'])


@register_config_handler('default_load_skill_mode')
def _handle_default_load_skill_mode(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update default load_skill mode (AUTO or NONE)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = str(ui_cfg.get('default_load_skill_mode', 'AUTO')).upper()
        if val in ('AUTO', 'NONE'):
            agent_pool.settings.default_load_skill_mode = val


@register_config_handler('loop_min_chars')
def _handle_loop_min_chars(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update minimum characters before activating heavy loop detection."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_min_chars = max(500, int(ui_cfg.get('loop_min_chars', 4000)))


@register_config_handler('loop_score_threshold')
def _handle_loop_score_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update cumulative score threshold for loop detection."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_score_threshold = max(50, int(ui_cfg.get('loop_score_threshold', 300)))


@register_config_handler('loop_char_run_enabled')
def _handle_loop_char_run(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle character run detection mode."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_char_run_enabled = bool(
            ui_cfg.get('loop_char_run_enabled', True))


@register_config_handler('loop_sentence_rep_enabled')
def _handle_loop_sentence_rep(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle sentence repetition detection mode."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_sentence_rep_enabled = bool(
            ui_cfg.get('loop_sentence_rep_enabled', True))


@register_config_handler('loop_ngram_rep_enabled')
def _handle_loop_ngram_rep(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle n-gram repetition detection mode."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_ngram_rep_enabled = bool(
            ui_cfg.get('loop_ngram_rep_enabled', True))


@register_config_handler('loop_block_rep_enabled')
def _handle_loop_block_rep(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle block repetition detection mode."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_block_rep_enabled = bool(
            ui_cfg.get('loop_block_rep_enabled', True))


@register_config_handler('loop_entropy_enabled')
def _handle_loop_entropy(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle entropy collapse detection mode."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_entropy_enabled = bool(
            ui_cfg.get('loop_entropy_enabled', True))


@register_config_handler('loop_max_retries')
def _handle_loop_max_retries(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update max retries after inner-loop detection (dedicated budget)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_max_retries = max(0, int(ui_cfg.get('loop_max_retries', 2)))


@register_config_handler('ci_execution_timeout')
def _handle_ci_execution_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update code interpreter execution timeout."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.ci_execution_timeout = max(
            CI_MIN_EXECUTION_TIMEOUT, int(ui_cfg['ci_execution_timeout']))

@register_config_handler('ci_watchdog_timeout')
def _handle_ci_watchdog_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update code interpreter watchdog timeout."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.ci_watchdog_timeout = max(
            CI_MIN_WATCHDOG_TIMEOUT, int(ui_cfg['ci_watchdog_timeout']))

@register_config_handler('ci_stale_container_ttl')
def _handle_ci_stale_container_ttl(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update code interpreter stale container TTL."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.ci_stale_container_ttl = max(
            CI_MIN_STALE_CONTAINER_TTL, int(ui_cfg['ci_stale_container_ttl']))


# LLM config keys — all share one handler (defense-in-depth optimization).
# Registered under each key so any LLM key present triggers the check.

def _handle_llm_config(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update default LLM config if changed (defense-in-depth optimization)."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None and hasattr(agent_pool, 'api_router'):
        new_llm_cfg = {k: v for k, v in ui_cfg.items() if k in LLM_CONFIG_KEYS}
        current_llm_cfg = agent_pool.api_router.default_llm_cfg or {}
        if new_llm_cfg != {k: current_llm_cfg.get(k) for k in new_llm_cfg}:
            agent_pool.api_router.update_default_llm_cfg(new_llm_cfg)
        else:
            _logger.debug("[update_config] LLM config unchanged")

for _llm_key in LLM_CONFIG_KEYS:
    @register_config_handler(_llm_key)
    def _handler(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
        _handle_llm_config(ui_cfg, agent_pool, agents)


# ── Cache pool config handlers ──────────────────────────────────────────

@register_config_handler('cache_pool_enabled')
def _handle_cache_pool_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle cache pool on/off and propagate to all running instances."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = bool(ui_cfg['cache_pool_enabled'])
        agent_pool.settings.cache_pool_enabled = val
        # Propagate toggle to all existing instance cache pools (thread-safe via lock)
        for inst in agent_pool.instance_conversations.values():
            cp = getattr(inst, 'cache_pool', None)
            if cp is not None:
                with cp._lock:
                    cp.enabled = val


@register_config_handler('cache_pool_size')
def _handle_cache_pool_size(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update rolling buffer size for cache pools and propagate to existing instances."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(5, int(ui_cfg['cache_pool_size']))  # Min 5 entries to prevent useless pools
        agent_pool.settings.cache_pool_size = val
        # Propagate size change to all existing instance cache pools (thread-safe via lock)
        for inst in agent_pool.instance_conversations.values():
            cp = getattr(inst, 'cache_pool', None)
            if cp is not None:
                with cp._lock:
                    # deque.maxlen is immutable; replace the entire deque preserving recent entries
                    preserved = list(cp._entries)[-val:]
                    cp._entries = Deque(preserved, maxlen=val)
                    cp.max_size = val


@register_config_handler('cache_threshold_chars')
def _handle_cache_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update character threshold for output and granular arg caching."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(100, int(ui_cfg['cache_threshold_chars']))  # Min 100 chars
        agent_pool.settings.cache_threshold_chars = val


# ── Router class ─────────────────────────────────────────────────────────

class ConfigUpdateRouter:
    """Routes config key updates to their respective handler functions.

    Each registered handler is called only if its corresponding config key
    is present in the incoming ui_cfg dict (defense-in-depth optimization).

    Usage:
        router = ConfigUpdateRouter(agent_pool, agents)
        await router.apply(ui_cfg)
    """

    def __init__(self, agent_pool: Optional[Any], agents: list):
        self.agent_pool = agent_pool
        self.agents = agents

    async def apply(self, ui_cfg: Dict[str, Any]) -> None:
        """Apply all config keys present in ui_cfg to their registered handlers.

        Iterates over keys actually present in ui_cfg (not all 30+ handlers)
        for O(K) dispatch where K = number of changed keys.
        """
        from agent_cascade.log import logger as _logger
        for key in ui_cfg:
            handler = CONFIG_HANDLERS.get(key)
            if handler is not None:
                try:
                    handler(ui_cfg, self.agent_pool, self.agents)
                except Exception as e:
                    _logger.warning(f"Config update failed for '{key}': {e}")