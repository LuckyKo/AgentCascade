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
from agent_cascade.constants import MAX_IMAGES_FOR_LLM_DEFAULT

# ── LLM config key set (defined locally to avoid circular import with api_server) ────
LLM_CONFIG_KEYS = frozenset({
    'model', 'api_base', 'api_key', 'temperature', 'max_tokens',
    'max_input_tokens', 'max_output_tokens', 'top_p', 'frequency_penalty',
    'presence_penalty', 'stop', 'timeout', 'model_type'
})

# ── PoolSettings keys — used by ws_handlers.py to trigger centralized save ────
# Includes ALL non-cosmetic settings that should persist to pool_settings.json.
POOL_SETTINGS_KEYS = frozenset({
    # Core pool/agent settings
    'idle_timeout_seconds', 'system_agent_idle_timeout_seconds', 'max_parallel_agents',
    'auto_continue', 'enable_agent_budgeting', 'max_turns', 'max_auto_rollbacks',
    'auto_rollback_on_loop',
    # Inner-loop detection
    'inner_loop_detect_enabled', 'loop_min_chars', 'loop_max_chars',
    'loop_char_run_enabled', 'loop_char_run_limit', 'loop_max_chars_enabled',
    'loop_two_phase_enabled', 'loop_suspicion_threshold',
    'loop_confirm_required', 'loop_cooldown_feeds',
    # Skills system
    'default_load_skill_mode', 'auto_skill_enabled',
    # Retry policy
    'retry_max_attempts', 'endpoint_max_retries', 'retry_base_delay', 'retry_max_delay',
    # Code interpreter
    'ci_execution_timeout', 'ci_watchdog_timeout', 'ci_stale_container_ttl',
    # Cache pool
    'cache_pool_enabled', 'cache_pool_size', 'cache_threshold_chars',
    # Tool char limits (stored in pool.llm_cfg but persisted via pool_settings.json)
    'tool_result_max_chars', 'grep_char_limit', 'grep_spillover',
    'shell_char_limit', 'code_char_limit', 'list_dir_char_limit',
    # Image base64 management
    'max_images_for_llm',
    # Approval timeout settings
    'approval_timeout_seconds', 'enable_approval_timeout',
    # Async shell console window toggle
    'enable_async_shell_console_window',
    # Work folders (persisted alongside PoolSettings fields)
    'work_access_folders_ro', 'work_access_folders_rw',
    # Default workspace
    'default_workspace',
    # Idle management
    'idle_check_interval',
    # Compression settings
    'compression_force_threshold', 'compression_warning_threshold',
    'compression_timeout', 'compression_force_cooldown', 'compression_max_attempts',
    # Security
    'security_check_timeout',
    # Nesting/sleeping limits
    'max_nesting_depth', 'sleeping_wakeup_interval',
    # Sync checks
    'tail_sync_check_enabled',
    # Streaming timeout settings
    'stream_max_silence_seconds', 'stream_max_total_seconds',
})

# ── Non-PoolSettings keys that still trigger persistence (stored at top level of pool_settings.json) ────
EXTRA_PERSIST_KEYS = frozenset({
    'disabled_tools',  # Per-agent-class tool assignments from UI settings panel
    'auto_security',   # Auto-Ask security mode toggle state
    'compression_proactive_threshold',   # Proactive compression threshold (PoolSettings field, persisted here for restart survival)
    'compression_context_reserve_tokens',  # Context reserve tokens (PoolSettings field, persisted here for restart survival)
    'compression_fraction',  # Compression ratio as percentage (maps to COMPRESSION_DEFAULT_FRACTION)
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
        _logger.debug("[work_folders] Extra work folders unchanged")
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


@register_config_handler('auto_skill_enabled')
def _handle_auto_skill_enabled(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle auto-skill generation/proposal on/off."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        agent_pool.settings.auto_skill_enabled = bool(ui_cfg.get('auto_skill_enabled', True))


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


@register_config_handler('max_turns')
def _handle_max_turns(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update default max turns for new agent instances."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('max_turns', 50))
        agent_pool.settings.max_turns = max(1, val)


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


@register_config_handler('tool_result_max_chars')
def _handle_tool_result_max_chars(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update tool result truncation limit."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        val = int(ui_cfg.get('tool_result_max_chars', 10000))
        agent_pool.llm_cfg['tool_result_max_chars'] = max(1000, val)


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


@register_config_handler('max_images_for_llm')
def _handle_max_images_for_llm(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update max images with base64 data sent to LLM (-1 = keep all)."""
    if agent_pool is not None and hasattr(agent_pool, 'llm_cfg'):
        try:
            val = int(ui_cfg.get('max_images_for_llm', MAX_IMAGES_FOR_LLM_DEFAULT))
            agent_pool.llm_cfg['max_images_for_llm'] = val
        except (ValueError, TypeError):
            pass  # Invalid value — keep existing setting


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
    from agent_cascade.utils.disabled_tools import normalize_disabled_tools, validate_tool_names

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

    UI sends a percentage (e.g. 70), we convert to fraction (0.7) and update the module-level
    COMPRESSION_DEFAULT_FRACTION which is used by both forced and proactive compression.
    Thread safety: module-level attribute assignment is atomic in CPython; reads are also atomic,
    so no lock needed for simple float updates.
    """
    import agent_cascade.settings as settings_mod
    val = float(ui_cfg['compression_fraction']) / 100.0  # Convert UI percentage to fraction
    val = min(settings_mod.COMPRESSION_MAX_FRACTION, max(settings_mod.COMPRESSION_MIN_FRACTION, val))
    settings_mod.COMPRESSION_DEFAULT_FRACTION = val


@register_config_handler('enable_agent_budgeting')
def _handle_enable_agent_budgeting(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = bool(ui_cfg['enable_agent_budgeting'])
        agent_pool.settings.enable_agent_budgeting = val


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