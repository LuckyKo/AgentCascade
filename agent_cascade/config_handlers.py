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

from agent_cascade.constants import MAX_IMAGES_FOR_LLM_DEFAULT
from agent_cascade.settings import (CI_MIN_EXECUTION_TIMEOUT, CI_MIN_STALE_CONTAINER_TTL, CI_MIN_WATCHDOG_TIMEOUT,
                                    CLI_AGENT_MIN_CHILDREN, CLI_AGENT_MIN_TIMEOUT,
                                    SKILL_ALWAYS_PROTECTED_DEFAULT, SKILL_SCORE_SETTINGS, clamp_skill_setting,
                                    format_skill_always_protected)
from agent_cascade.settings_registry import (
    EXPORT_IMPORT_ALLOWLIST, EXTRA_PERSIST_KEYS, LLM_CFG_KEYS, LLM_CFG_PERSIST_KEYS, POOL_SETTINGS_KEYS,
)

# POOL_SETTINGS_KEYS / EXTRA_PERSIST_KEYS / EXPORT_IMPORT_ALLOWLIST / LLM_CFG_KEYS /
# LLM_CFG_PERSIST_KEYS are registry-derived (single source of truth); re-exported here so
# existing imports (ws_handlers, tests) keep working.

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
        _logger.info('[MCP] Eagerly loaded %d tools.', len(mcp_tools))
    except Exception as e:
        _logger.warning('[MCP] Eager initialization failed: %s', e)


def _normalize_paths(paths: list) -> list:
    """Strip and filter paths from a list."""
    return [p.strip() for p in paths if p.strip()]


@register_config_handler('work_access_folders_ro')
def _handle_work_folders(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update read-only and read-write work folders (defense-in-depth: only if changed).

    Only updates when a key is explicitly present AND contains non-empty paths.
    Empty arrays are treated as "no opinion" to prevent stale clients from clearing
    valid config (e.g., multiple browser tabs with different localStorage state).

    To clear all folders, send an explicit 'clear_work_folders' flag or restart server.
    """
    from agent_cascade.log import logger as _logger
    if agent_pool is None or not hasattr(agent_pool, 'operation_manager') or agent_pool.operation_manager is None:
        return

    om = agent_pool.operation_manager

    # Check each key independently — only update if present AND has actual paths.
    ro_changed = False
    rw_changed = False
    ro_new_norm = None
    rw_new_norm = None

    ro_raw = ui_cfg.get('work_access_folders_ro')
    if isinstance(ro_raw, list) and len(ro_raw) > 0:
        ro_new_norm = _normalize_paths(ro_raw)
        if ro_new_norm:
            ro_current = sorted([str(p).lower() for p in om.extra_work_folders_ro])
            ro_new_sorted = sorted([p.lower() for p in ro_new_norm])
            ro_changed = ro_new_sorted != ro_current

    rw_raw = ui_cfg.get('work_access_folders_rw')
    if isinstance(rw_raw, list) and len(rw_raw) > 0:
        rw_new_norm = _normalize_paths(rw_raw)
        if rw_new_norm:
            rw_current = sorted([str(p).lower() for p in om.extra_work_folders_rw])
            rw_new_sorted = sorted([p.lower() for p in rw_new_norm])
            rw_changed = rw_new_sorted != rw_current

    if not ro_changed and not rw_changed:
        _logger.debug('[work_folders] Extra work folders unchanged')
        return

    # Build final lists — use new values only when changed, preserve existing otherwise.
    ro_final = ro_new_norm if ro_changed else [str(p) for p in om.extra_work_folders_ro]
    rw_final = rw_new_norm if rw_changed else [str(p) for p in om.extra_work_folders_rw]

    om.set_extra_work_folders(ro_final, rw_final)


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
            _logger.debug('[update_config] Base workspace unchanged')


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
            agent_pool.operation_manager.set_approval_timeout(int(ui_cfg['approval_timeout_seconds']))
        except Exception as e:
            _logger.warning(f"Failed to set approval timeout: {e}")


@register_config_handler('enable_approval_timeout')
def _handle_enable_approval_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle approval timeout enablement on the operation manager."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None:
        try:
            agent_pool.operation_manager.set_enable_timeout(bool(ui_cfg['enable_approval_timeout']))
        except Exception as e:
            _logger.warning(f"Failed to set approval timeout toggle: {e}")


@register_config_handler('enable_async_shell_console_window')
def _handle_enable_async_shell_console_window(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update async shell console window toggle state. Set via UI config update."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None:
        try:
            agent_pool._enable_async_shell_console_window = bool(ui_cfg['enable_async_shell_console_window'])
        except Exception as e:
            _logger.warning(f"Failed to set async shell console window toggle: {e}")


@register_config_handler('max_parallel_agents')
def _handle_max_parallel_agents(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update max parallel agents and resize the thread pool executor."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg['max_parallel_agents'])
        agent_pool.settings.max_workers = max(1, val)
        if hasattr(agent_pool, '_async_registry'):
            agent_pool._async_registry.resize_executor(agent_pool.settings.max_workers)
        else:
            _logger.debug('[THREAD_POOL] resize skipped — no _async_registry on pool')


@register_config_handler('max_workers')
def _handle_max_workers(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Alias for max_parallel_agents — update max workers directly (for import compatibility)."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg['max_workers'])
        agent_pool.settings.max_workers = max(1, val)
        if hasattr(agent_pool, '_async_registry'):
            agent_pool._async_registry.resize_executor(agent_pool.settings.max_workers)
        else:
            _logger.debug('[THREAD_POOL] resize skipped — no _async_registry on pool')


@register_config_handler('auto_continue')
def _handle_auto_continue(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update auto_continue setting on the agent pool."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.auto_continue = bool(ui_cfg['auto_continue'])


@register_config_handler('completion_classifier_enabled')
def _handle_completion_classifier_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle the completion classifier (BUG_0048). Default True when absent."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.completion_classifier_enabled = bool(
            ui_cfg.get('completion_classifier_enabled', True))


@register_config_handler('inner_loop_detect_enabled')
def _handle_inner_loop_detect(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle inner-loop detection during streaming (catches LLM generation loops mid-stream)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.inner_loop_detect_enabled = bool(ui_cfg['inner_loop_detect_enabled'])


@register_config_handler('loop_max_chars_enabled')
def _handle_loop_max_chars_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle max chars hard limit guard."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_max_chars_enabled = bool(ui_cfg['loop_max_chars_enabled'])


@register_config_handler('loop_two_phase_enabled')
def _handle_loop_two_phase_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle two-phase semantic loop detection."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_two_phase_enabled = bool(ui_cfg['loop_two_phase_enabled'])


@register_config_handler('loop_suspicion_threshold')
def _handle_loop_suspicion_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update suspicion threshold for two-phase loop detection [5-15]."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg['loop_suspicion_threshold'])
        agent_pool.settings.loop_suspicion_threshold = max(5, min(15, val))


@register_config_handler('loop_confirm_required')
def _handle_loop_confirm_required(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update confirm required for two-phase loop detection [2-6]."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg['loop_confirm_required'])
        agent_pool.settings.loop_confirm_required = max(2, min(6, val))


@register_config_handler('loop_cooldown_feeds')
def _handle_loop_cooldown_feeds(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update cooldown feeds for two-phase loop detection [10-200]."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg['loop_cooldown_feeds'])
        agent_pool.settings.loop_cooldown_feeds = max(10, min(200, val))


@register_config_handler('default_load_skill_mode')
def _handle_default_load_skill_mode(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update default load_skill mode (AUTO or NONE)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = str(ui_cfg.get('default_load_skill_mode', 'AUTO')).upper()
        if val in ('AUTO', 'NONE'):
            agent_pool.settings.default_load_skill_mode = val
            if val == 'NONE':
                # Clear registry to free memory (under lock for thread safety)
                if hasattr(agent_pool, 'skill_manager'):
                    sm = agent_pool.skill_manager
                    with sm._write_lock:
                        sm._skills_registry.clear()
                        sm._rebuild_index()
                    sm.invalidate_cache()  # force rediscovery on next scan


@register_config_handler('auto_skill_enabled')
def _handle_auto_skill_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle auto-skill generation/proposal on/off."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.auto_skill_enabled = bool(ui_cfg.get('auto_skill_enabled', True))


@register_config_handler('user_message_log_enabled')
def _handle_user_message_log_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle the Agent-Messages JSONL audit trail on/off (t154).

    Pure settings gate — no runtime side effect. The writer reads this flag on
    every call via getattr(..., 'user_message_log_enabled', True), so it takes
    effect immediately for subsequent messages. Default ON.
    """
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.user_message_log_enabled = bool(ui_cfg.get('user_message_log_enabled', True))


@register_config_handler('telegram_bridge_enabled')
def _handle_telegram_bridge_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle the AC-owned Telegram bridge in-process daemon thread on/off.

    Sets the persisted PoolSettings field AND drives the supervisor (attached as
    ``agent_pool.telegram_supervisor`` by api_server.main()). The UI sends a full
    settings snapshot on every save, so this handler fires on *every* save — the
    supervisor's set_enabled() is idempotent (no double-start on repeated "on",
    safe no-op on "off" when not running).
    """
    if agent_pool is None or not hasattr(agent_pool, 'settings'):
        return
    enabled = bool(ui_cfg.get('telegram_bridge_enabled', False))
    agent_pool.settings.telegram_bridge_enabled = enabled
    supervisor = getattr(agent_pool, 'telegram_supervisor', None)
    if supervisor is not None:
        supervisor.set_enabled(enabled)


@register_config_handler('auto_skill_mode')
def _handle_auto_skill_mode(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update AUTO Skill Helper sub-mode (basic, advanced, or none).

    Only meaningful when default_load_skill_mode == "AUTO". Invalid values fall
    back to the default ("basic") so a bad UI payload can never break skill loading.
    "none" disables system-injected auto-matched skills while preserving
    Self-Augmentation and caller-explicit load_skill lists.
    """
    from agent_cascade.settings import (AUTO_SKILL_MODE_ADVANCED, AUTO_SKILL_MODE_BASIC, AUTO_SKILL_MODE_NONE,
                                        DEFAULT_AUTO_SKILL_MODE)
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = str(ui_cfg.get('auto_skill_mode', DEFAULT_AUTO_SKILL_MODE)).strip().lower()
        if val not in (AUTO_SKILL_MODE_BASIC, AUTO_SKILL_MODE_ADVANCED, AUTO_SKILL_MODE_NONE):
            val = DEFAULT_AUTO_SKILL_MODE
        agent_pool.settings.auto_skill_mode = val


@register_config_handler('skill_selector_mode')
def _handle_skill_selector_mode(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update the skill selector mode (keyword or api).

    Selects which strategy backs SkillManager.match_skills auto-matching. Invalid
    values fall back to the default ("keyword") so a bad UI payload can never break
    skill loading — keyword is always the safe, byte-identical baseline.
    """
    from agent_cascade.settings import (DEFAULT_SKILL_SELECTOR_MODE, SKILL_SELECTOR_MODE_API,
                                        SKILL_SELECTOR_MODE_KEYWORD)
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = str(ui_cfg.get('skill_selector_mode', DEFAULT_SKILL_SELECTOR_MODE)).strip().lower()
        if val not in (SKILL_SELECTOR_MODE_KEYWORD, SKILL_SELECTOR_MODE_API):
            val = DEFAULT_SKILL_SELECTOR_MODE
        agent_pool.settings.skill_selector_mode = val


@register_config_handler('auto_skill_min_turns')
def _handle_auto_skill_min_turns(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update the minimum turns an agent must effectuate before a natural finish triggers auto-skill reflection."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('auto_skill_min_turns', 20))
        agent_pool.settings.auto_skill_min_turns = max(1, min(500, val))


@register_config_handler('auto_skill_extra_turns')
def _handle_auto_skill_extra_turns(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update the fresh turn budget granted for auto-skill reflection at trigger time."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('auto_skill_extra_turns', 25))
        agent_pool.settings.auto_skill_extra_turns = max(1, min(200, val))


@register_config_handler('loop_min_chars')
def _handle_loop_min_chars(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update minimum characters before activating heavy loop detection."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('loop_min_chars', 4000))
        agent_pool.settings.loop_min_chars = max(500, min(20000, val))


@register_config_handler('loop_max_chars')
def _handle_loop_max_chars(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update maximum character limit for inner loop detection."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('loop_max_chars', 40960))
        agent_pool.settings.loop_max_chars = max(1000, min(100000, val))


@register_config_handler('loop_char_run_enabled')
def _handle_loop_char_run_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle character run detection."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_char_run_enabled = bool(ui_cfg.get('loop_char_run_enabled', True))


@register_config_handler('loop_char_run_limit')
def _handle_loop_char_run_limit(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update character run limit (consecutive identical chars). Range [10, 500]."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('loop_char_run_limit', 129))
        agent_pool.settings.loop_char_run_limit = max(10, min(500, val))


# Retry policy settings handlers (Phase 1 of retry refactoring)


@register_config_handler('retry_max_attempts')
def _handle_retry_max_attempts(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update total outer retry attempts. Range [1, 6]."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('retry_max_attempts', 3))
        if val < 1 or val > 6:
            raise ValueError(f"retry_max_attempts must be in [1, 6], got {val}")
        agent_pool.settings.retry_max_attempts = val


@register_config_handler('slot_queue_timeout_seconds')
def _handle_slot_queue_timeout_seconds(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update the slot-queue dead-man's-switch activity window. Range [30, 7200].

    This is the WINDOW, not a total wait budget: it resets on every holder sign of life,
    so it is safe to raise. The hard ceiling (6x) is derived at acquire time
    (pool/slots.py::_queue_limits) and is deliberately not configurable.

    NOTE: the ValueError below is swallowed and downgraded to a warning by the dispatch
    loop, so the UI would silently appear to accept an out-of-range value. That is why the
    range check assigns nothing before raising — pool.settings keeps its previous value.
    """
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('slot_queue_timeout_seconds', 300))
        if val < 30 or val > 7200:
            raise ValueError(f"slot_queue_timeout_seconds must be in [30, 7200], got {val}")
        agent_pool.settings.slot_queue_timeout_seconds = val


@register_config_handler('endpoint_max_retries')
def _handle_endpoint_max_retries(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update per-endpoint retries before failover. Range [0, 2]."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('endpoint_max_retries', 1))
        if val < 0 or val > 2:
            raise ValueError(f"endpoint_max_retries must be in [0, 2], got {val}")
        agent_pool.settings.endpoint_max_retries = val


@register_config_handler('retry_base_delay')
def _handle_retry_base_delay(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update initial backoff delay in seconds. Must be ≥ 0.1."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = float(ui_cfg.get('retry_base_delay', 1.0))
        if val < 0.1:
            raise ValueError(f"retry_base_delay must be ≥ 0.1, got {val}")
        agent_pool.settings.retry_base_delay = val


@register_config_handler('retry_max_delay')
def _handle_retry_max_delay(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update maximum backoff cap in seconds. Must be ≥ 1.0."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = float(ui_cfg.get('retry_max_delay', 8.0))
        if val < 1.0:
            raise ValueError(f"retry_max_delay must be ≥ 1.0, got {val}")
        agent_pool.settings.retry_max_delay = val


# ── External CLI coding-agent (call_cli_agent) settings handlers ──
@register_config_handler('cli_agent_timeout')
def _handle_cli_agent_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update the hard wall-clock cap per one-shot call_cli_agent run (seconds)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.cli_agent_timeout = max(CLI_AGENT_MIN_TIMEOUT, int(ui_cfg['cli_agent_timeout']))


@register_config_handler('max_cli_agent_children')
def _handle_max_cli_agent_children(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update the concurrent call_cli_agent one-shot container cap."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.max_cli_agent_children = max(CLI_AGENT_MIN_CHILDREN, int(ui_cfg['max_cli_agent_children']))


@register_config_handler('cli_agent_enabled')
def _handle_cli_agent_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Set the per-harness enable map (external CLI auto-setup). Full-map replace; non-bool dropped.

    The Web UI always sends the complete merged map (mirrors disabled_tools), so a replace is
    correct; a partial dict would silently drop other harnesses.
    """
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        v = ui_cfg.get('cli_agent_enabled', {})
        if isinstance(v, dict):
            agent_pool.settings.cli_agent_enabled = {k: bool(x) for k, x in v.items() if isinstance(x, bool)}


@register_config_handler('cli_agent_images')
def _handle_cli_agent_images(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Set the per-harness image map. Full-map replace; non-str dropped."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        v = ui_cfg.get('cli_agent_images', {})
        if isinstance(v, dict):
            agent_pool.settings.cli_agent_images = {k: str(x) for k, x in v.items() if isinstance(x, str)}


@register_config_handler('cli_agent_models')
def _handle_cli_agent_models(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Set the per-harness default-model map. Full-map replace; non-str dropped."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        v = ui_cfg.get('cli_agent_models', {})
        if isinstance(v, dict):
            agent_pool.settings.cli_agent_models = {k: str(x) for k, x in v.items() if isinstance(x, str)}


@register_config_handler('cli_agent_paths_ro')
def _handle_cli_agent_paths_ro(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Master read-only toggle for mounting inherited extra-paths into the CLI container."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.cli_agent_paths_ro = bool(ui_cfg.get('cli_agent_paths_ro', False))


@register_config_handler('ci_execution_timeout')
def _handle_ci_execution_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update code interpreter execution timeout."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.ci_execution_timeout = max(CI_MIN_EXECUTION_TIMEOUT, int(ui_cfg['ci_execution_timeout']))


@register_config_handler('ci_watchdog_timeout')
def _handle_ci_watchdog_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update code interpreter watchdog timeout."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.ci_watchdog_timeout = max(CI_MIN_WATCHDOG_TIMEOUT, int(ui_cfg['ci_watchdog_timeout']))


@register_config_handler('ci_stale_container_ttl')
def _handle_ci_stale_container_ttl(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update code interpreter stale container TTL."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.ci_stale_container_ttl = max(CI_MIN_STALE_CONTAINER_TTL,
                                                         int(ui_cfg['ci_stale_container_ttl']))


@register_config_handler('stream_max_silence_seconds')
def _handle_stream_max_silence_seconds(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update streaming max silence timeout (seconds between chunks before considering stream stalled)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = float(ui_cfg.get('stream_max_silence_seconds', 120.0))
        agent_pool.settings.stream_max_silence_seconds = max(5.0, val)


@register_config_handler('stream_max_total_seconds')
def _handle_stream_max_total_seconds(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update streaming max total duration timeout (seconds for entire streaming response)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = float(ui_cfg.get('stream_max_total_seconds', 900.0))
        agent_pool.settings.stream_max_total_seconds = max(60.0, val)


@register_config_handler('dismiss_thread_join_timeout')
def _handle_dismiss_thread_join_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update dismiss thread join timeout (seconds to wait for agent thread to stop)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = float(ui_cfg.get('dismiss_thread_join_timeout', 2.0))
        agent_pool.settings.dismiss_thread_join_timeout = max(0.5, min(val, 10.0))


@register_config_handler('max_turns')
def _handle_max_turns(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update default max turns for new agent instances."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('max_turns', 50))
        agent_pool.settings.max_turns = max(1, val)


@register_config_handler('soft_turn_limit')
def _handle_soft_turn_limit(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle soft turn-limit mode (warn past max_turns instead of hard-stopping)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.soft_turn_limit = bool(ui_cfg.get('soft_turn_limit', False))


@register_config_handler('token_budget')
def _handle_token_budget(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Set the pool-default token budget (generated tokens). 0 = disabled (default OFF)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('token_budget', 0))
        agent_pool.settings.token_budget = max(0, val)


@register_config_handler('max_auto_rollbacks')
def _handle_max_auto_rollbacks(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update maximum automatic rollback attempts on detected loops.

    -1 means unlimited (∞); all other negatives clamp to 0; positives clamp to [0, 10].
    """
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('max_auto_rollbacks', 3))
        if val == -1:
            agent_pool.settings.max_auto_rollbacks = -1
        else:
            # Clamp all other values to [0, 10]
            agent_pool.settings.max_auto_rollbacks = max(0, min(val, 10))


@register_config_handler('auto_rollback_on_loop')
def _handle_auto_rollback_on_loop(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle automatic rollback on detected loops."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.auto_rollback_on_loop = bool(ui_cfg.get('auto_rollback_on_loop', True))


@register_config_handler('loop_exact_rollback_enabled')
def _handle_loop_exact_rollback_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Tier 1 (exact matcher): run and roll back on exact hits (default True)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_exact_rollback_enabled = bool(ui_cfg.get('loop_exact_rollback_enabled', True))


@register_config_handler('loop_fuzzy_warning_enabled')
def _handle_loop_fuzzy_warning_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Tier 2 (fuzzy matcher): run and inject the advisory warning (default True)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.loop_fuzzy_warning_enabled = bool(ui_cfg.get('loop_fuzzy_warning_enabled', True))


@register_config_handler('tool_loop_fuzzy_rollback_enabled')
def _handle_tool_loop_fuzzy_rollback_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Tier 2 escalation toggle: full rollback when a fuzzy loop persists FUZZY_ESCALATION_TURNS after the warning (default False)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.tool_loop_fuzzy_rollback_enabled = bool(
            ui_cfg.get('tool_loop_fuzzy_rollback_enabled', False))


@register_config_handler('tool_loop_detection_enabled')
def _handle_tool_loop_detection_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """DEPRECATED (2026-08): legacy KILL SWITCH for the fuzzy tier only — can disable Tier 2 but never enable it on its own."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.tool_loop_detection_enabled = bool(ui_cfg.get('tool_loop_detection_enabled', True))


@register_config_handler('tool_result_max_chars')
def _handle_tool_result_max_chars(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update tool result trip threshold.

    Also clamps wild_read_truncation_chars to not exceed the new threshold,
    preventing a misconfiguration where target > trigger.
    """
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        val = int(ui_cfg.get('tool_result_max_chars', 10000))
        new_threshold = max(1000, val)
        agent_pool.llm_cfg['tool_result_max_chars'] = new_threshold
        # Clamp target to not exceed new threshold
        if 'wild_read_truncation_chars' in agent_pool.llm_cfg:
            agent_pool.llm_cfg['wild_read_truncation_chars'] = min(agent_pool.llm_cfg['wild_read_truncation_chars'],
                                                                   new_threshold)


@register_config_handler('wild_read_truncation_chars')
def _handle_wild_read_truncation_chars(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update the lower truncation cut for wild reads when the trip threshold is exceeded.

    Clamped to [500, tool_result_max_chars] — must never exceed the trip threshold.
    """
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        val = int(ui_cfg.get('wild_read_truncation_chars', 4000))
        # Upper bound tracks DEFAULT_TOOL_RESULT_MAX_CHARS (25k global net) so an absent-key
        # clamp path keeps the same trip threshold the rest of the code uses.
        from agent_cascade.settings import DEFAULT_TOOL_RESULT_MAX_CHARS
        upper = agent_pool.llm_cfg.get('tool_result_max_chars', DEFAULT_TOOL_RESULT_MAX_CHARS)
        agent_pool.llm_cfg['wild_read_truncation_chars'] = min(max(500, val), upper)


# ── Memory-hint feature handlers (plan §7) ────────────────────────────────────
@register_config_handler('memory_hint_enabled')
def _handle_memory_hint_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle the memory-hint feature kill switch (default OFF)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        agent_pool.llm_cfg['memory_hint_enabled'] = bool(ui_cfg.get('memory_hint_enabled', False))


@register_config_handler('memory_hint_threshold')
def _handle_memory_hint_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Override floor for the adaptive hint gate (clamped to [0, 1]).

    The primary gate is self-calibrating (EWMA of per-turn top-1 scores + a
    specificity gap); this setting is an optional override that can only RAISE
    the minimum score floor — it makes hints rarer, never more frequent.
    0 = pure adaptive behavior (the default).
    """
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = float(ui_cfg.get('memory_hint_threshold', 0.0))
        except (TypeError, ValueError):
            val = 0.0
        agent_pool.llm_cfg['memory_hint_threshold'] = min(max(0.0, val), 1.0)


@register_config_handler('memory_hint_max_entries')
def _handle_memory_hint_max_entries(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Max number of top-scoring entries listed in a hint. Clamped to [1, 4]."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = int(ui_cfg.get('memory_hint_max_entries', 3))
        except (TypeError, ValueError):
            val = 3
        agent_pool.llm_cfg['memory_hint_max_entries'] = min(max(1, val), 4)


@register_config_handler('memory_hint_query_chars')
def _handle_memory_hint_query_chars(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Chars of turn text used as the hint query key. Clamped to [100, 4000]."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = int(ui_cfg.get('memory_hint_query_chars', 1000))
        except (TypeError, ValueError):
            val = 1000
        agent_pool.llm_cfg['memory_hint_query_chars'] = min(max(100, val), 4000)


@register_config_handler('memory_hint_cooldown_seconds')
def _handle_memory_hint_cooldown_seconds(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Seconds before the same memory may be re-hinted. Clamped to [0, 86400]."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = float(ui_cfg.get('memory_hint_cooldown_seconds', 600))
        except (TypeError, ValueError):
            val = 600.0
        agent_pool.llm_cfg['memory_hint_cooldown_seconds'] = min(max(0.0, val), 86400.0)


@register_config_handler('memory_hint_skill_suggestions')
def _handle_memory_hint_skill_suggestions(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Master sub-toggle for skill suggestions within memory hints (default ON).

    A sub-toggle under ``memory_hint_enabled``: when memory hints are off this is
    moot; when on, the user can independently disable just the skill part. The
    manager reads it live each cycle via _settings() — no other wiring needed.
    """
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        agent_pool.llm_cfg['memory_hint_skill_suggestions'] = bool(ui_cfg.get('memory_hint_skill_suggestions', True))


@register_config_handler('memory_hint_skill_threshold')
def _handle_memory_hint_skill_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Raise-only override floor for the SKILL sub-pipeline (BUG_0041).

    Deliberately separate from ``memory_hint_threshold`` (memory cosine scale) —
    reusing one setting across two score measures is precisely the bug class
    BUG_0041 is about. Clamped to [0, 1.0]. 0 = pure adaptive behavior.
    """
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = float(ui_cfg.get('memory_hint_skill_threshold', 0.0))
        except (TypeError, ValueError):
            val = 0.0
        agent_pool.llm_cfg['memory_hint_skill_threshold'] = min(max(0.0, val), 1.0)


@register_config_handler('memory_hint_skill_autoload_enabled')
def _handle_memory_hint_skill_autoload_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Master toggle for auto-loading a repeatedly-hinted skill (BUG_0263).

    Stored on pool.llm_cfg like the rest of the memory-hint family; the manager reads
    it live each cycle via _settings() — no other wiring needed.
    """
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        agent_pool.llm_cfg['memory_hint_skill_autoload_enabled'] = bool(
            ui_cfg.get('memory_hint_skill_autoload_enabled', True))


@register_config_handler('memory_hint_skill_autoload_threshold')
def _handle_memory_hint_skill_autoload_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Per-session hint count at which a not-yet-loaded hinted skill auto-loads (BUG_0263).

    Clamped to a minimum of 1: a zero/negative value would mean "auto-load on the first
    hint", which is not what the setting is for.
    """
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = int(ui_cfg.get('memory_hint_skill_autoload_threshold', 3))
        except (TypeError, ValueError):
            val = 3
        agent_pool.llm_cfg['memory_hint_skill_autoload_threshold'] = max(1, val)


# ── Skill invalidation handlers (adaptive count-cap, plan §7) ───────────────
@register_config_handler('skill_auto_invalidate_enabled')
def _handle_skill_auto_invalidate_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle the startup adaptive count-cap rebalance pass (default ON)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        agent_pool.llm_cfg['skill_auto_invalidate_enabled'] = bool(ui_cfg.get('skill_auto_invalidate_enabled', True))


@register_config_handler('skill_always_protected')
def _handle_skill_always_protected(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Comma-separated set of meta/process skills that are NEVER evicted by the rebalance pass.

    Normalized to the canonical sorted comma-separated STRING (lowercase; empty/whitespace tokens
    ignored; case-insensitive on both sides) so the value stays JSON-serializable inside the shared
    llm_cfg dict (pool_settings.json / api_endpoints.json). Consumers re-parse it via
    parse_skill_always_protected(). A missing/blank value falls back to the DEFAULT four
    meta-skills, so an old config or a cleared field always keeps them protected. Non-clamped
    STRING setting — modeled on ``skill_auto_invalidate_enabled`` (not the numeric
    SKILL_SCORE_SETTINGS table)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        raw = ui_cfg.get('skill_always_protected', SKILL_ALWAYS_PROTECTED_DEFAULT)
        agent_pool.llm_cfg['skill_always_protected'] = format_skill_always_protected(raw)


@register_config_handler('skill_active_target_k')
def _handle_skill_active_target_k(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Multiplier K: raw target = round(K × N_qualified). Raw is clamped to [MIN_CAP, MAX_CAP]
    for the eviction threshold (never evict below MIN_CAP); re-enable is bounded by the servable
    corpus. Clamped to [0.1, 5.0]."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = float(ui_cfg.get('skill_active_target_k', 1.0))
        except (TypeError, ValueError):
            val = 1.0
        agent_pool.llm_cfg['skill_active_target_k'] = min(max(0.1, val), 5.0)


@register_config_handler('skill_active_min_cap')
def _handle_skill_active_min_cap(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Floor on the active-skill target. Clamped to [1, 200]."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = int(ui_cfg.get('skill_active_min_cap', 20))
        except (TypeError, ValueError):
            val = 20
        agent_pool.llm_cfg['skill_active_min_cap'] = min(max(1, val), 200)


@register_config_handler('skill_active_max_cap')
def _handle_skill_active_max_cap(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Ceiling on the active-skill target. Clamped to [min_cap, 1000]; enforced >= min_cap."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = int(ui_cfg.get('skill_active_max_cap', 200))
        except (TypeError, ValueError):
            val = 200
        min_cap = int(agent_pool.llm_cfg.get('skill_active_min_cap', 20) or 1)
        agent_pool.llm_cfg['skill_active_max_cap'] = min(max(min_cap, val), 1000)


# ── Skill-scoring constant handlers (research §5/§7/§18 + D-SAFE) ────────────
# One handler per setting, all generated from the single source-of-truth table
# settings.SKILL_SCORE_SETTINGS via clamp_skill_setting(). Each clamp is therefore
# BYTE-IDENTICAL to the preview endpoint in api_server.py (score_overrides) and to
# web_ui/app.js getGenerateCfg, so a displayed number always matches what a real
# save+pass would compute. The per-key docstrings live on the table itself.
def _make_skill_score_handler(key: str):
    """Build the config handler for one skill-scoring setting from the shared clamp table."""
    def _handler(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
        if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
            agent_pool.llm_cfg[key] = clamp_skill_setting(key, ui_cfg.get(key))
    _handler.__name__ = f'_handle_{key}'
    return _handler


for _skill_score_key in SKILL_SCORE_SETTINGS:
    register_config_handler(_skill_score_key)(_make_skill_score_handler(_skill_score_key))


@register_config_handler('grep_char_limit')
def _handle_grep_char_limit(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update grep output character limit (-1 = unlimited)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        val = int(ui_cfg.get('grep_char_limit', -1))
        agent_pool.llm_cfg['grep_char_limit'] = val


@register_config_handler('grep_spillover')
def _handle_grep_spillover(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle grep spillover to logs."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        agent_pool.llm_cfg['grep_spillover'] = bool(ui_cfg.get('grep_spillover', True))


@register_config_handler('shell_char_limit')
def _handle_shell_char_limit(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update shell_cmd output character limit (-1 = unlimited)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        val = int(ui_cfg.get('shell_char_limit', -1))
        agent_pool.llm_cfg['shell_char_limit'] = val


@register_config_handler('code_char_limit')
def _handle_code_char_limit(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update code_interpreter output character limit (-1 = unlimited)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        val = int(ui_cfg.get('code_char_limit', -1))
        agent_pool.llm_cfg['code_char_limit'] = val


@register_config_handler('list_dir_char_limit')
def _handle_list_dir_char_limit(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update list_dir output character limit (-1 = unlimited)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        val = int(ui_cfg.get('list_dir_char_limit', -1))
        agent_pool.llm_cfg['list_dir_char_limit'] = val


@register_config_handler('read_file_max_chars')
def _handle_read_file_max_chars(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update read_file no-limit char default (4000; -1/0 disables)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        val = int(ui_cfg.get('read_file_max_chars', 4000))
        agent_pool.llm_cfg['read_file_max_chars'] = val


@register_config_handler('max_images_for_llm')
def _handle_max_images_for_llm(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update max images with base64 data sent to LLM (-1 = keep all)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = int(ui_cfg.get('max_images_for_llm', MAX_IMAGES_FOR_LLM_DEFAULT))
            # Clamp to safe range: -1 (keep all) or 0-50
            if val != -1 and (val < 0 or val > 50):
                val = MAX_IMAGES_FOR_LLM_DEFAULT
            agent_pool.llm_cfg['max_images_for_llm'] = val
        except (ValueError, TypeError):
            pass  # Invalid value — keep existing setting


@register_config_handler('image_caption_mode')
def _handle_image_caption_mode(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update the image caption mode (auto/always/off). Invalid values normalize to 'auto'."""
    if agent_pool is None or not hasattr(agent_pool, 'settings'):
        return
    raw = ui_cfg.get('image_caption_mode', 'auto')
    if raw in ('auto', 'always', 'off'):
        agent_pool.settings.image_caption_mode = raw
    else:
        agent_pool.settings.image_caption_mode = 'auto'


@register_config_handler('disabled_tools')
def _handle_disabled_tools(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update per-agent-class disabled tools configuration from UI settings panel.

    Validates tool names against the registry and silently ignores unknown tools
    for backwards compatibility (tools may be added/removed over time).
    """
    if agent_pool is None or 'disabled_tools' not in ui_cfg:
        return

    from agent_cascade.log import logger as _logger
    from agent_cascade.tools.base import TOOL_REGISTRY
    from agent_cascade.utils.disabled_tools import normalize_disabled_tools

    raw_dt = ui_cfg['disabled_tools']
    if not raw_dt:
        # Empty value — clear all disabled tools
        agent_pool.set_ui_disabled_tools({})
        return

    from agent_cascade.constants import RUNTIME_REGISTERED_TOOLS
    known_tools = set(TOOL_REGISTRY.keys()) | RUNTIME_REGISTERED_TOOLS

    try:
        if isinstance(raw_dt, dict):
            validated_dict = {}
            for agent_key, agent_tools in raw_dt.items():
                normalized = normalize_disabled_tools(agent_tools)
                # Silently filter out unknown tools (backwards compatibility)
                valid_tools = [t for t in normalized if t in known_tools]
                ignored = set(normalized) - set(valid_tools)
                if ignored:
                    _logger.debug(f"[disabled_tools] Ignoring unknown tools for '{agent_key}': {ignored}")
                if isinstance(agent_tools, (list, tuple)):
                    validated_dict[agent_key] = valid_tools
                else:
                    validated_dict[agent_key] = valid_tools
            agent_pool.set_ui_disabled_tools(validated_dict)
        else:
            normalized = normalize_disabled_tools(raw_dt)
            valid_tools = [t for t in normalized if t in known_tools]
            ignored = set(normalized) - set(valid_tools)
            if ignored:
                _logger.debug(f"[disabled_tools] Ignoring unknown tools (global): {ignored}")
            agent_pool.set_ui_disabled_tools(valid_tools)
    except Exception as e:
        _logger.warning(f"[disabled_tools] Failed to update disabled tools config: {e}")


# ── PoolSettings handlers for fields without special logic ───────────────


@register_config_handler('compression_force_threshold')
def _handle_compression_force_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = float(ui_cfg['compression_force_threshold'])
        val = min(99.0, max(51.0, val))  # Keep within valid range (must be > warning threshold)
        agent_pool.settings.compression_force_threshold = val


@register_config_handler('compression_warning_threshold')
def _handle_compression_warning_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = float(ui_cfg['compression_warning_threshold'])
        val = min(99.0, max(50.0, val))  # Keep within valid range
        agent_pool.settings.compression_warning_threshold = val


@register_config_handler('compression_timeout')
def _handle_compression_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(1, int(ui_cfg['compression_timeout']))
        agent_pool.settings.compression_timeout = val


@register_config_handler('compression_force_cooldown')
def _handle_compression_force_cooldown(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(0, int(ui_cfg['compression_force_cooldown']))
        agent_pool.settings.compression_force_cooldown = val


@register_config_handler('compression_max_attempts')
def _handle_compression_max_attempts(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(1, int(ui_cfg['compression_max_attempts']))
        agent_pool.settings.compression_max_attempts = val


@register_config_handler('compression_proactive_threshold')
def _handle_compression_proactive_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = float(ui_cfg['compression_proactive_threshold'])
        val = min(96.0, max(50.0, val))  # Keep within valid range (must be below forced threshold)
        agent_pool.settings.compression_proactive_threshold = val


@register_config_handler('compression_context_reserve_tokens')
def _handle_compression_context_reserve_tokens(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(500, int(ui_cfg['compression_context_reserve_tokens']))
        agent_pool.settings.compression_context_reserve_tokens = val


@register_config_handler('compression_fraction')
def _handle_compression_fraction(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Handle compression_fraction setting.

    UI sends a percentage (e.g. 70); the single writer on runtime_state.state handles
    conversion + clamping. BUG_0029 Phase 2 (D-2b/D-2c): no duplicated clamp here.
    """
    from agent_cascade.runtime_state import state as _runtime_state
    _runtime_state.set_compression_fraction(float(ui_cfg['compression_fraction']))


@register_config_handler('auto_security')
def _handle_auto_security(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Auto-Ask Security toggle.

    Single writer is the pool (runtime_state + change-detected persistence); fall back to
    the runtime singleton when no pool is wired in.
    """
    from agent_cascade.runtime_state import state as _runtime_state
    enabled = bool(ui_cfg['auto_security'])
    setter = getattr(agent_pool, 'set_auto_security', None)
    if setter is not None:
        setter(enabled)
    else:
        _runtime_state.set_auto_security(enabled)


@register_config_handler('compression_min_usage_pct')
def _handle_compression_min_usage_pct(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Refuse agent-triggered compression below this context-usage percentage (0-100)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.compression_min_usage_pct = min(100.0, max(0.0,
                                                                       float(ui_cfg['compression_min_usage_pct'])))


@register_config_handler('tool_loop_sim_threshold')
def _handle_tool_loop_sim_threshold(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Tier-2 (fuzzy) similarity floor for near-duplicate tool-call cores (0-1)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.tool_loop_sim_threshold = min(1.0, max(0.0,
                                                                   float(ui_cfg['tool_loop_sim_threshold'])))


@register_config_handler('holder_dms_window_s')
def _handle_holder_dms_window_s(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Holder-side dead-man's-switch activity window (seconds; 0 disables the watchdog)."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.holder_dms_window_s = max(0.0, float(ui_cfg['holder_dms_window_s']))


@register_config_handler('holder_dms_max_bound_s')
def _handle_holder_dms_max_bound_s(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Fallback DMS bound for sync tools that declare no finite dms_bound()."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.holder_dms_max_bound_s = max(0.0, float(ui_cfg['holder_dms_max_bound_s']))


@register_config_handler('afk_enabled')
def _handle_afk_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle the server-backed AFK flag (message/mode left untouched)."""
    from agent_cascade.runtime_state import state as _runtime_state
    _runtime_state.set_afk(bool(ui_cfg['afk_enabled']))


@register_config_handler('afk_message')
def _handle_afk_message(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Set the AFK nudge message without changing the enabled flag."""
    from agent_cascade.runtime_state import state as _runtime_state
    _runtime_state.set_afk(bool(_runtime_state.afk_enabled), str(ui_cfg['afk_message']))


@register_config_handler('afk_mode')
def _handle_afk_mode(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Set the AFK nudge mode ('basic' | 'admin') without changing the enabled flag."""
    from agent_cascade.runtime_state import state as _runtime_state
    from agent_cascade.settings import normalize_afk_mode
    _runtime_state.set_afk(bool(_runtime_state.afk_enabled), None,
                           normalize_afk_mode(ui_cfg['afk_mode']))


@register_config_handler('enable_agent_budgeting')
def _handle_enable_agent_budgeting(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = bool(ui_cfg['enable_agent_budgeting'])
        agent_pool.settings.enable_agent_budgeting = val


@register_config_handler('allow_parallel_agents')
def _handle_allow_parallel_agents(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.allow_parallel_agents = bool(ui_cfg['allow_parallel_agents'])


@register_config_handler('unload_unused_tools_post_compression')
def _handle_unload_unused_tools_post_compression(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    # MCP tool-unload toggle: sets pool.settings so the compression handler reads it live
    # (compression/handler.py: _maybe_unload_unused_mcp_tools). Persisted via save_trigger.
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.unload_unused_tools_post_compression = bool(ui_cfg['unload_unused_tools_post_compression'])


@register_config_handler('state_kv_save_enabled')
def _handle_state_kv_save_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    # Automatic checkpoint toggle only — NOT a global KV master switch. The manual
    # Save/Load buttons gate on per-endpoint capability (is_state_save_capable)
    # and work with this off; see the state_kv_save_enabled field in agent_instance.py.
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.state_kv_save_enabled = bool(ui_cfg['state_kv_save_enabled'])


@register_config_handler('safe_mode_auto_ram_floor_mb')
def _handle_safe_mode_auto_ram_floor_mb(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.safe_mode_auto_ram_floor_mb = max(0, int(ui_cfg['safe_mode_auto_ram_floor_mb']))


@register_config_handler('safe_mode_auto_vram_floor_mb')
def _handle_safe_mode_auto_vram_floor_mb(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.safe_mode_auto_vram_floor_mb = max(0, int(ui_cfg['safe_mode_auto_vram_floor_mb']))


@register_config_handler('idle_check_interval')
def _handle_idle_check_interval(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(1, int(ui_cfg['idle_check_interval']))
        agent_pool.settings.idle_check_interval = val


@register_config_handler('max_nesting_depth')
def _handle_max_nesting_depth(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(1, int(ui_cfg['max_nesting_depth']))
        agent_pool.settings.max_nesting_depth = val


@register_config_handler('security_check_timeout')
def _handle_security_check_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(1, int(ui_cfg['security_check_timeout']))
        agent_pool.settings.security_check_timeout = val


@register_config_handler('sleeping_timeout')
def _handle_sleeping_timeout(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    # DEPRECATED (2026-08): Kept for backward compatibility only; sleeping_timeout is no longer used.
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(1, int(ui_cfg['sleeping_timeout']))
        agent_pool.settings.sleeping_timeout = val


@register_config_handler('sleeping_wakeup_interval')
def _handle_sleeping_wakeup_interval(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(1, int(ui_cfg['sleeping_wakeup_interval']))
        agent_pool.settings.sleeping_wakeup_interval = val


@register_config_handler('tail_sync_check_enabled')
def _handle_tail_sync_check_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = bool(ui_cfg['tail_sync_check_enabled'])
        agent_pool.settings.tail_sync_check_enabled = val


# LLM config keys — all share one handler (defense-in-depth optimization).
# Registered under each key so any LLM key present triggers the check.


def _handle_llm_config(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update default LLM config if changed (defense-in-depth optimization)."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None and hasattr(agent_pool, 'api_router'):
        new_llm_cfg = {k: v for k, v in ui_cfg.items() if k in LLM_CFG_KEYS}
        current_llm_cfg = agent_pool.api_router.default_llm_cfg or {}
        if new_llm_cfg != {k: current_llm_cfg.get(k) for k in new_llm_cfg}:
            agent_pool.api_router.update_default_llm_cfg(new_llm_cfg)
        else:
            _logger.debug('[update_config] LLM config unchanged')


# Each key is bound at decoration time (register_config_handler(_llm_key)), so there is
# no late-binding closure issue — every registered handler routes to _handle_llm_config.
for _llm_key in LLM_CFG_KEYS:

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
