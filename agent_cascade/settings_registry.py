"""Backend-authoritative settings registry — the single source of truth for every
configurable key AgentCascade persists, broadcasts, and (eventually) exposes in a
data-driven UI.

Every key the server knows about has exactly one :class:`SettingSpec` in
:data:`SETTINGS_REGISTRY`. The legacy key lists (:data:`POOL_SETTINGS_KEYS`,
:data:`EXTRA_PERSIST_KEYS`) are *derived* from this table, not maintained by hand —
import them from here (or from :mod:`agent_cascade.config_handlers`, which re-exports
them) rather than re-listing the keys.

Design notes
------------
* ``kind`` names the persistence bucket family:
    - ``pool``    → persisted to ``config/pool_settings.json`` (a PoolSettings field,
      a live runtime value, or a value sourced from ``pool.llm_cfg``).
    - ``extra``   → a top-level ``pool_settings.json`` key that is not a PoolSettings
      field (``disabled_tools``, ``auto_security``, ``compression_fraction``).
    - ``llm``     → persisted to ``config/api_endpoints.json::default_llm_cfg``.
    - ``session`` → persisted to ``config/session_state.json`` (server-wide).
* ``save_trigger`` marks the keys that, when received in an ``update_config``
  message, trigger ``AgentPool._save_pool_settings()``. This is exactly the
  historical ``POOL_SETTINGS_KEYS | EXTRA_PERSIST_KEYS`` set.
* ``value_source`` records where a pool/extra key's value is read from at save/broadcast
  time: ``settings`` (a PoolSettings field), ``llm_cfg`` (``pool.llm_cfg``), or
  ``runtime`` (live runtime state / operation manager).

The registry is pure data (no dependency on the pool, the router, or the config
handlers), so it can be imported from anywhere without a circular import.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, FrozenSet, Optional, Tuple

from agent_cascade.constants import MAX_IMAGES_FOR_LLM_DEFAULT
from agent_cascade.session_state_store import DEFAULT_SESSION_NAME
from agent_cascade.settings import (
    AFK_MODE_DEFAULT, AGENT_IDLE_CHECK_INTERVAL, AGENT_IDLE_TIMEOUT, AGENT_MAX_AUTO_ROLLBACKS,
    AGENT_MAX_NESTING_DEPTH, AGENT_MAX_WORKERS, AGENT_SLEEPING_WAKEUP_INTERVAL,
    CACHE_POOL_ENABLED, CACHE_POOL_SIZE, CACHE_THRESHOLD_CHARS,
    CI_EXECUTION_TIMEOUT, CI_STALE_CONTAINER_TTL, CI_WATCHDOG_TIMEOUT, COMPRESSION_DEFAULT_FRACTION_SEED,
    COMPRESSION_FORCE_THRESHOLD, COMPRESSION_MIN_USAGE_PCT, COMPRESSION_SECURITY_CHECK_TIMEOUT,
    COMPRESSION_TIMEOUT, COMPRESSION_WARNING_THRESHOLD, DEFAULT_AUTO_SKILL_MODE, DEFAULT_COMPRESSION_COOLDOWN_SECONDS,
    DEFAULT_COMPRESSION_CONTEXT_RESERVE_TOKENS, DEFAULT_COMPRESSION_MAX_ATTEMPTS,
    DEFAULT_COMPRESSION_PROACTIVE_THRESHOLD, DEFAULT_LOAD_SKILL_MODE, DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_TURNS, DEFAULT_TOOL_RESULT_MAX_CHARS, DEFAULT_WILD_READ_TRUNCATION_CHARS,
    DEFAULT_WORKSPACE, DISMISS_THREAD_JOIN_TIMEOUT, format_skill_always_protected,
    HOLDER_DMS_MAX_BOUND_S, HOLDER_DMS_WINDOW_S,
    LOOP_EXACT_ROLLBACK_ENABLED, LOOP_FUZZY_WARNING_ENABLED, SAFE_MODE_AUTO_RAM_FLOOR_MB,
    SAFE_MODE_AUTO_VRAM_FLOOR_MB, SKILL_ALWAYS_PROTECTED_DEFAULT, SKILL_SCORE_SETTINGS,
    SKILL_SELECTOR_MODE_KEYWORD, SOFT_TURN_LIMIT, STREAM_MAX_SILENCE_SECONDS, STREAM_MAX_TOTAL_SECONDS,
    SYSTEM_AGENT_IDLE_TIMEOUT, TOOL_LOOP_DETECTION_ENABLED, TOOL_LOOP_FUZZY_ROLLBACK_ENABLED,
    TOOL_LOOP_SIM_THRESHOLD,
)

# ── Key name lists (the single source of truth for "which keys exist") ─────────

# PoolSettings-backed keys persisted to config/pool_settings.json. This is the
# historical ``POOL_SETTINGS_KEYS`` (100 keys): the update_config save-trigger set
# and the pool_settings.json persist set.
_POOL_KEYS: Tuple[str, ...] = (
    'allow_parallel_agents', 'approval_timeout_seconds', 'auto_continue', 'auto_rollback_on_loop',
    'auto_skill_enabled', 'auto_skill_extra_turns', 'auto_skill_min_turns', 'auto_skill_mode',
    'cache_pool_enabled', 'cache_pool_size', 'cache_threshold_chars', 'ci_execution_timeout',
    'ci_stale_container_ttl', 'ci_watchdog_timeout', 'code_char_limit', 'completion_classifier_enabled',
    'compression_context_reserve_tokens', 'compression_force_cooldown', 'compression_force_threshold',
    'compression_max_attempts', 'compression_proactive_threshold', 'compression_timeout',
    'compression_warning_threshold', 'default_load_skill_mode', 'default_workspace',
    'dismiss_thread_join_timeout', 'enable_agent_budgeting', 'enable_approval_timeout',
    'enable_async_shell_console_window', 'endpoint_max_retries', 'grep_char_limit', 'grep_spillover',
    'idle_check_interval', 'idle_timeout_seconds', 'image_caption_mode', 'inner_loop_detect_enabled',
    'list_dir_char_limit', 'loop_char_run_enabled', 'loop_char_run_limit', 'loop_confirm_required',
    'loop_cooldown_feeds', 'loop_exact_rollback_enabled', 'loop_fuzzy_warning_enabled', 'loop_max_chars',
    'loop_max_chars_enabled', 'loop_min_chars', 'loop_suspicion_threshold', 'loop_two_phase_enabled',
    'max_auto_rollbacks', 'max_images_for_llm', 'max_nesting_depth', 'max_parallel_agents', 'max_turns',
    'max_workers', 'memory_hint_cooldown_seconds', 'memory_hint_enabled', 'memory_hint_max_entries',
    'memory_hint_query_chars', 'memory_hint_skill_autoload_enabled',
    'memory_hint_skill_autoload_threshold', 'memory_hint_skill_suggestions', 'memory_hint_skill_threshold',
    'memory_hint_threshold', 'read_file_max_chars', 'retry_base_delay', 'retry_max_attempts', 'retry_max_delay',
    'safe_mode_auto_ram_floor_mb', 'safe_mode_auto_vram_floor_mb', 'security_check_timeout',
    'shell_char_limit', 'skill_active_max_cap', 'skill_active_min_cap', 'skill_active_target_k',
    'skill_always_protected', 'skill_auto_invalidate_enabled', 'skill_fair_window_turns',
    'skill_max_evictions_per_pass', 'skill_score_dq', 'skill_score_ghalf', 'skill_score_kq',
    'skill_score_nhalf', 'skill_score_nmin', 'skill_score_q0', 'skill_score_rflood',
    'skill_score_tau_turns', 'skill_selector_mode', 'sleeping_wakeup_interval', 'slot_queue_timeout_seconds',
    'soft_turn_limit', 'state_kv_save_enabled', 'stream_max_silence_seconds', 'stream_max_total_seconds',
    'system_agent_idle_timeout_seconds', 'tail_sync_check_enabled', 'telegram_bridge_enabled',
    'tool_loop_detection_enabled', 'tool_loop_fuzzy_rollback_enabled', 'tool_result_max_chars',
    'unload_unused_tools_post_compression',
    'user_message_log_enabled', 'wild_read_truncation_chars', 'work_access_folders_ro', 'work_access_folders_rw',
    'token_budget',
)

# Top-level extra keys persisted to config/pool_settings.json (historical
# ``EXTRA_PERSIST_KEYS``).
_EXTRA_KEYS: Tuple[str, ...] = (
    'auto_security', 'compression_fraction', 'disabled_tools',
)

# Disk-only keys written by ``_save_pool_settings()`` today but absent from the two
# lists above (premise audit). Each gets a registry entry with persist=True; they are
# added to the export/import allowlist so an import no longer drops them.
_DISK_ONLY_KEYS: Tuple[str, ...] = (
    'afk_enabled', 'afk_message', 'afk_mode',
    'compression_min_usage_pct', 'holder_dms_max_bound_s', 'holder_dms_window_s',
    'tool_loop_sim_threshold',
)

# LLM config keys persisted to config/api_endpoints.json::default_llm_cfg (Phase 1).
_LLM_KEYS: Tuple[str, ...] = (
    'model', 'api_base', 'api_key',
    'temperature', 'top_p', 'top_k', 'min_p', 'repeat_penalty',
    'presence_penalty', 'frequency_penalty',
    'max_tokens', 'max_input_tokens', 'max_output_tokens',
    'stop_sequences', 'timeout', 'model_type', 'log_api_post',
    'vision_enabled',
)

# Server-wide session identity, persisted to config/session_state.json (Phase 1).
_SESSION_KEYS: Tuple[str, ...] = ('session_name',)

# Config keys that HAVE a config handler but are neither persisted to pool_settings.json
# nor broadcast in the state snapshot: MCP server definitions (consumed directly by
# tools/mcp_manager) and the deprecated sleeping_timeout (a PoolSettings field excluded
# from to_dict()). Listed here so handler coverage stays bidirectional.
_HANDLER_ONLY_KEYS: Tuple[str, ...] = ('mcpServers', 'sleeping_timeout')

# ── Flag tables (value-source classification) ───────────────────────────────────

# Pool keys whose value is sourced from ``pool.llm_cfg`` (not a PoolSettings field).
# This is exactly the llm_cfg block that ``_save_pool_settings`` writes to
# pool_settings.json; the registry now drives that list instead of a hardcoded one.
_LLM_CFG_SOURCED: FrozenSet[str] = frozenset({
    'tool_result_max_chars', 'wild_read_truncation_chars', 'grep_char_limit', 'grep_spillover',
    'shell_char_limit', 'code_char_limit', 'list_dir_char_limit', 'read_file_max_chars', 'max_images_for_llm',
    'memory_hint_enabled', 'memory_hint_threshold', 'memory_hint_max_entries',
    'memory_hint_query_chars', 'memory_hint_cooldown_seconds', 'memory_hint_skill_suggestions',
    'memory_hint_skill_threshold', 'memory_hint_skill_autoload_enabled',
    'memory_hint_skill_autoload_threshold',
    'skill_auto_invalidate_enabled', 'skill_active_target_k', 'skill_active_min_cap',
    'skill_active_max_cap',
    'skill_score_q0', 'skill_score_kq', 'skill_score_nhalf', 'skill_score_ghalf',
    'skill_score_rflood', 'skill_score_tau_turns', 'skill_score_dq', 'skill_score_nmin',
    'skill_fair_window_turns', 'skill_max_evictions_per_pass', 'skill_always_protected',
})

# Keys whose value is sourced from live runtime state / operation manager (not a
# PoolSettings field).
_RUNTIME_SOURCED: FrozenSet[str] = frozenset({
    'approval_timeout_seconds', 'enable_approval_timeout',
    'afk_enabled', 'afk_message', 'afk_mode',
    'enable_async_shell_console_window', 'auto_security', 'compression_fraction',
})

# Persist keys that are NOT currently in the state snapshot and gain broadcast in
# Phase 1 (item 11). The snapshot builder emits these from ``pool.settings``.
_NEVER_BROADCAST_PERSIST_KEYS: FrozenSet[str] = frozenset({
    'compression_min_usage_pct', 'holder_dms_max_bound_s', 'holder_dms_window_s',
    'tool_loop_sim_threshold',
})

# ── Default values ───────────────────────────────────────────────────────────────
#
# Every registry key carries a ``default``. It is the value the server falls back to
# when the live source (PoolSettings field, ``pool.llm_cfg`` entry, runtime state) is
# absent, and the baseline the settings-delta validation and the snapshot builder use.
#
# For every key that IS a PoolSettings field the default is read straight from the
# dataclass (:data:`_POOL_FIELD_DEFAULTS`) — re-listing those values here would be a
# second source of truth and would drift (the old hand-maintained table disagreed with
# the dataclass on 10 keys). Only keys with no PoolSettings field carry an explicit seed.
from dataclasses import MISSING as _MISSING, fields as _fields
from agent_cascade.agent_instance import PoolSettings as _PoolSettings

#: Live defaults of the PoolSettings dataclass, keyed by field name.
_POOL_FIELD_DEFAULTS: Dict[str, Any] = {
    field.name: field.default
    for field in _fields(_PoolSettings)
    if field.default is not _MISSING
}

#: Defaults for keys that are NOT PoolSettings fields: the llm_cfg-sourced keys seeded
#: by ``api_server``'s initial ``llm_cfg``, the runtime-state keys, the LLM config keys,
#: and neutral empties for keys whose real value comes from user config (``api_key``,
#: work folders, ``disabled_tools``).
#:
#: Units note: the canonical unit of ``compression_fraction`` in the settings contract
#: is the PERCENT (the UI, the disk and the config handler all use percent);
#: ``runtime_state.set_compression_fraction`` converts it to the 0-1 fraction the
#: compression code consumes. The seed below is therefore percent, not fraction.
_SEED_DEFAULTS: Dict[str, Any] = {
    # Pool keys that are not PoolSettings fields.
    'default_workspace': str(DEFAULT_WORKSPACE),
    'max_parallel_agents': AGENT_MAX_WORKERS,  # alias of max_workers (see _make_specs)
    # Runtime-state-sourced keys.
    'approval_timeout_seconds': 300,
    'enable_approval_timeout': True,
    'enable_async_shell_console_window': False,
    'auto_security': True,
    'compression_fraction': round(COMPRESSION_DEFAULT_FRACTION_SEED * 100, 1),
    'afk_enabled': False,
    'afk_message': '',
    'afk_mode': AFK_MODE_DEFAULT,
    'work_access_folders_ro': [],
    'work_access_folders_rw': [],
    # llm_cfg-sourced keys (seeded by api_server's initial llm_cfg).
    'tool_result_max_chars': DEFAULT_TOOL_RESULT_MAX_CHARS,
    'wild_read_truncation_chars': DEFAULT_WILD_READ_TRUNCATION_CHARS,
    'grep_char_limit': 12000,
    'grep_spillover': True,
    'shell_char_limit': 8000,
    'code_char_limit': 8000,
    'list_dir_char_limit': 8000,
    'read_file_max_chars': 4000,
    'max_images_for_llm': MAX_IMAGES_FOR_LLM_DEFAULT,
    'memory_hint_enabled': False,
    'memory_hint_threshold': 0.0,
    'memory_hint_max_entries': 3,
    'memory_hint_query_chars': 1000,
    'memory_hint_cooldown_seconds': 600,
    'memory_hint_skill_suggestions': True,
    'memory_hint_skill_threshold': 0.0,
    # Auto-load a repeatedly-hinted skill (BUG_0263): master toggle + per-session hint count
    # at which a not-yet-loaded hinted skill is auto-loaded.
    'memory_hint_skill_autoload_enabled': True,
    'memory_hint_skill_autoload_threshold': 3,
    'skill_auto_invalidate_enabled': True,
    'skill_active_target_k': 1.0,
    'skill_active_min_cap': 20,
    'skill_active_max_cap': 200,
    'skill_always_protected': format_skill_always_protected(SKILL_ALWAYS_PROTECTED_DEFAULT),
    **{key: spec['default'] for key, spec in SKILL_SCORE_SETTINGS.items()},
    # LLM-config keys: the real values come from config/env; these are the type
    # defaults used for validation and for an empty default_llm_cfg.
    'model': '',
    'api_base': '',
    'api_key': '',
    'temperature': 0.7,
    'top_p': 1.0,
    'top_k': 40,
    'min_p': 0.0,
    'repeat_penalty': 1.1,
    'presence_penalty': 0.0,
    'frequency_penalty': 0.0,
    'max_tokens': 8192,
    'max_input_tokens': DEFAULT_MAX_INPUT_TOKENS,
    'max_output_tokens': 4096,
    'stop_sequences': '',
    'timeout': 120.0,
    'model_type': 'chat',
    'log_api_post': False,
    # Global default endpoint capability: when False, images are captioned to text before
    # the call (llm/base.py: effective_vision). Absent = True, matching the old behaviour.
    'vision_enabled': True,
    # Session identity.
    'session_name': DEFAULT_SESSION_NAME,
    # Extra top-level pool_settings.json keys.
    'disabled_tools': {},
    # Handler-only keys (see _HANDLER_ONLY_KEYS): neutral values, never persisted.
    'mcpServers': {},
}

#: Registry key names, pre-computed for the default-table merge below.
_KNOWN_KEYS: FrozenSet[str] = (frozenset(_POOL_KEYS) | frozenset(_EXTRA_KEYS) | frozenset(_DISK_ONLY_KEYS)
    | frozenset(_HANDLER_ONLY_KEYS) | frozenset(_LLM_KEYS) | frozenset(_SESSION_KEYS))

#: Complete default table (seeds + every PoolSettings field default). Seeds win over a
#: same-named dataclass field so the llm_cfg/runtime contract stays authoritative;
#: deprecated PoolSettings fields that are not settings (loop_score_threshold,
#: sleeping_timeout, ...) are deliberately skipped.
_DEFAULTS: Dict[str, Any] = {
    **_SEED_DEFAULTS,
    **{key: value for key, value in _POOL_FIELD_DEFAULTS.items()
       if key not in _SEED_DEFAULTS and key in _KNOWN_KEYS},
}

#: Expected types that cannot be inferred from the default value (collection-valued
#: keys). Everything else derives from ``type(default)``.
_PY_TYPES: Dict[str, Any] = {
    'disabled_tools': (dict, list),
    'work_access_folders_ro': list,
    'work_access_folders_rw': list,
    'stop_sequences': (str, list),
}

#: Keys the server never imports from a client-side blob during migration.
_SERVER_WINS_KEYS: FrozenSet[str] = frozenset({
    'disabled_tools',
    'work_access_folders_ro',
    'work_access_folders_rw',
    'auto_security',
    'session_name',
})


def _default_type(default: Any) -> Any:
    """Expected value type for a key, derived from its default."""
    if isinstance(default, bool):
        return bool
    if isinstance(default, (int, float)):
        return (int, float)
    return type(default)



def _value_source(key: str) -> str:
    """Classify where a pool/extra key's value is read from at save/broadcast time."""
    if key in _LLM_CFG_SOURCED:
        return 'llm_cfg'
    if key in _RUNTIME_SOURCED:
        return 'runtime'
    return 'settings'


# ── SettingSpec ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingSpec:
    """Declarative description of one configurable key (plan §6.1, extended).

    ``default`` is the fallback value used when the live source is absent;
    ``py_type`` is the expected value type used by
    :func:`agent_cascade.settings_delta.validate_settings_changes` (``bool``,
    ``(int, float)``, ``str``, ``list``, ...). ``handler`` is the
    :data:`agent_cascade.config_handlers.CONFIG_HANDLERS` key for this spec (==
    ``key`` for every pool/extra/llm key that has a handler; ``None`` marks a
    server-only key that no config handler routes to, e.g. ``session_name``).
    ``save_trigger`` marks the update_config keys that trigger
    ``AgentPool._save_pool_settings()``. ``broadcast_as`` is the name under which the
    value is emitted in the state snapshot (defaults to ``key``; used for the
    ``max_workers`` → ``max_parallel_agents`` alias). ``alias_of`` names the canonical
    key whose value this spec mirrors (``max_parallel_agents`` → ``max_workers``).
    ``sensitive`` marks keys whose value must never be logged or exposed in a
    read-only payload (``api_key``). ``server_wins`` marks keys the migration path
    never imports from a client-side blob.

    Removed as dead: ``restart_required``, ``ui_control_ids``, ``legacy_local_keys``
    — nothing in the server or the UI reads them (verified by whole-tree grep).
    """
    key: str
    kind: str
    py_type: Any = object
    default: Any = None
    persist: bool = True
    persist_bucket: str = 'pool_settings'
    handler: Optional[str] = None
    broadcast: bool = True
    sensitive: bool = False
    save_trigger: bool = False
    value_source: str = 'settings'
    alias_of: Optional[str] = None
    broadcast_as: Optional[str] = None
    server_wins: bool = False


def _make_specs() -> Dict[str, SettingSpec]:
    """Build the full spec table from the key lists above."""
    specs: Dict[str, SettingSpec] = {}

    def _spec(key: str, kind: str, *, handler: Optional[str],
              save_trigger: bool, persist_bucket: str = 'pool_settings') -> SettingSpec:
        default = _DEFAULTS[key]
        return SettingSpec(
            key=key,
            kind=kind,
            py_type=_PY_TYPES.get(key, _default_type(default)),
            default=default,
            persist=True,
            persist_bucket=persist_bucket,
            handler=handler,
            broadcast=True,
            sensitive=(key == 'api_key'),
            save_trigger=save_trigger,
            value_source=_value_source(key),
            server_wins=key in _SERVER_WINS_KEYS,
        )

    # Pool keys: persisted to pool_settings.json, save-trigger, broadcast.
    for key in _POOL_KEYS:
        specs[key] = _spec(key, 'pool', handler=key, save_trigger=True)

    # Extra keys: top-level pool_settings.json keys, save-trigger, broadcast.
    for key in _EXTRA_KEYS:
        specs[key] = _spec(key, 'extra', handler=key, save_trigger=True)

    # Disk-only keys: persisted to pool_settings.json, NOT a save-trigger, broadcast.
    for key in _DISK_ONLY_KEYS:
        specs[key] = _spec(key, 'pool', handler=key, save_trigger=False)

    # Handler-only keys: a config handler exists for them (legacy UI config / MCP
    # definitions) but their value is neither persisted in pool_settings.json nor
    # broadcast in the state snapshot. They are listed here so validate_registry()'s
    # handler-coverage assertion is bidirectional.
    for key in _HANDLER_ONLY_KEYS:
        specs[key] = replace(_spec(key, 'pool', handler=key, save_trigger=False),
                            persist=False, broadcast=False)

    # LLM config keys: persisted to api_endpoints.default_llm_cfg, not broadcast in the
    # pool_settings snapshot (api_key is sensitive).
    for key in _LLM_KEYS:
        spec = _spec(key, 'llm', handler=key, save_trigger=False,
                    persist_bucket='api_endpoints.default_llm_cfg')
        specs[key] = replace(spec, broadcast=False)

    # Session identity: persisted to session_state.json, broadcast at the state top
    # level (not inside pool_settings), no config handler.
    for key in _SESSION_KEYS:
        spec = _spec(key, 'session', handler=None, save_trigger=False,
                    persist_bucket='session_state')
        specs[key] = replace(spec, broadcast=False,
                            value_source='session_state')

    # ── The max_workers ↔ max_parallel_agents alias ─────────────────────────────
    # ``max_workers`` is the canonical PoolSettings field (persisted under that name).
    # ``max_parallel_agents`` is the UI/broadcast alias: it is accepted as input and is
    # the name the value is emitted under in the state snapshot. Both are save-triggers
    # and both map to the canonical value.
    specs['max_workers'] = SettingSpec(
        key='max_workers',
        kind='pool',
        py_type=_default_type(_DEFAULTS['max_workers']),
        default=_DEFAULTS['max_workers'],
        persist=True,
        persist_bucket='pool_settings',
        handler='max_workers',
        broadcast=True,
        broadcast_as='max_parallel_agents',
        save_trigger=True,
        value_source='settings',
    )
    specs['max_parallel_agents'] = SettingSpec(
        key='max_parallel_agents',
        kind='pool',
        py_type=_default_type(_DEFAULTS['max_parallel_agents']),
        default=_DEFAULTS['max_parallel_agents'],
        persist=True,
        persist_bucket='pool_settings',
        handler='max_parallel_agents',
        broadcast=True,
        alias_of='max_workers',
        broadcast_as='max_parallel_agents',
        save_trigger=True,
        value_source='settings',
    )

    return specs


SETTINGS_REGISTRY: Dict[str, SettingSpec] = _make_specs()

# ── Derived key sets (compatibility aliases + new registry-derived sets) ────────

#: Historical ``POOL_SETTINGS_KEYS`` — the pool-kind save-trigger set (100 keys).
POOL_SETTINGS_KEYS: FrozenSet[str] = frozenset(
    s.key for s in SETTINGS_REGISTRY.values() if s.kind == 'pool' and s.save_trigger
)

#: Historical ``EXTRA_PERSIST_KEYS`` — the top-level extra persist set (3 keys).
EXTRA_PERSIST_KEYS: FrozenSet[str] = frozenset(
    s.key for s in SETTINGS_REGISTRY.values() if s.kind == 'extra'
)

#: LLM config keys persisted to ``api_endpoints.default_llm_cfg``.
LLM_CFG_KEYS: FrozenSet[str] = frozenset(
    s.key for s in SETTINGS_REGISTRY.values() if s.kind == 'llm'
)

#: Server-wide session keys persisted to ``session_state.json``.
SESSION_KEYS: FrozenSet[str] = frozenset(
    s.key for s in SETTINGS_REGISTRY.values() if s.kind == 'session'
)

#: Every key the registry knows about.
ALL_KEYS: FrozenSet[str] = frozenset(SETTINGS_REGISTRY)

#: Keys emitted in the pool_settings state snapshot (broadcast=True, pool/extra kind).
BROADCAST_KEYS: FrozenSet[str] = frozenset(
    (s.broadcast_as or s.key) for s in SETTINGS_REGISTRY.values()
    if s.broadcast and s.kind in ('pool', 'extra')
)

#: Keys persisted anywhere (pool_settings.json, api_endpoints, session_state).
PERSIST_KEYS: FrozenSet[str] = frozenset(
    s.key for s in SETTINGS_REGISTRY.values() if s.persist
)

#: Export/import allowlist — every persisted pool_settings.json key, INCLUDING the
#: disk-only keys the historical ``POOL_SETTINGS_KEYS | EXTRA_PERSIST_KEYS`` dropped.
EXPORT_IMPORT_ALLOWLIST: FrozenSet[str] = frozenset(
    s.key for s in SETTINGS_REGISTRY.values()
    if s.persist and s.kind in ('pool', 'extra')
)

#: The llm_cfg-sourced pool/extra keys that ``_save_pool_settings`` writes to
#: pool_settings.json (drives the formerly-hardcoded list in config_persist.py).
#: Restricted to the pool_settings bucket — the ``llm``-kind keys persist to
#: api_endpoints.json, not pool_settings.json.
LLM_CFG_PERSIST_KEYS: FrozenSet[str] = frozenset(
    s.key for s in SETTINGS_REGISTRY.values()
    if s.value_source == 'llm_cfg' and s.kind in ('pool', 'extra')
)

#: Persist keys that were never in the state snapshot and gain broadcast in Phase 1.
NEVER_BROADCAST_PERSIST_KEYS: FrozenSet[str] = frozenset(_NEVER_BROADCAST_PERSIST_KEYS)

#: Keys the migration path never imports from a client-side blob (server value wins).
SERVER_WINS_KEYS: FrozenSet[str] = frozenset(
    s.key for s in SETTINGS_REGISTRY.values() if s.server_wins
)

#: Keys whose value must never be logged or exposed in a read-only payload.
SENSITIVE_KEYS: FrozenSet[str] = frozenset(
    s.key for s in SETTINGS_REGISTRY.values() if s.sensitive
)


def strip_sensitive(values: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``values`` with every :data:`SENSITIVE_KEYS` entry removed.

    Used by the read-only settings payload (``GET /api/settings``) so a key flagged
    ``sensitive`` in the registry can never leak, whatever the broadcast set contains.
    """
    return {k: v for k, v in values.items() if k not in SENSITIVE_KEYS}


def spec(key: str) -> SettingSpec:
    """Return the :class:`SettingSpec` for ``key`` (KeyError if unknown)."""
    return SETTINGS_REGISTRY[key]


def validate_registry() -> None:
    """Raise ``AssertionError`` on any internal inconsistency in the table.

    Called by the completeness tests. Checks:
      * no duplicate key names across the source lists;
      * the derived compat sets match their historical sizes (105 / 3);
      * every alias target exists;
      * handler coverage is bidirectional (no spec claims an unregistered handler, no
        handler is registered for an unknown key).
    """
    seen = set()
    for group in (_POOL_KEYS, _EXTRA_KEYS, _DISK_ONLY_KEYS, _HANDLER_ONLY_KEYS, _LLM_KEYS, _SESSION_KEYS):
        for key in group:
            if key in seen:
                raise AssertionError(f"duplicate registry key: {key}")
            seen.add(key)

    if len(POOL_SETTINGS_KEYS) != 105:
        raise AssertionError(f"POOL_SETTINGS_KEYS drifted: {len(POOL_SETTINGS_KEYS)} != 105")
    if len(EXTRA_PERSIST_KEYS) != 3:
        raise AssertionError(f"EXTRA_PERSIST_KEYS drifted: {len(EXTRA_PERSIST_KEYS)} != 3")

    # Every key must carry a default and a py_type: the registry is the source of truth
    # for both, and the snapshot builder falls back to ``spec.default``.
    for key in ALL_KEYS:
        if key not in _DEFAULTS:
            raise AssertionError(f"no default defined for registry key: {key}")
    for key in _DEFAULTS:
        if key not in ALL_KEYS:
            raise AssertionError(f"default defined for unknown key: {key}")
    for s in SETTINGS_REGISTRY.values():
        if s.default is None:
            raise AssertionError(f"spec {s.key} has no default")
        if s.py_type is object:
            raise AssertionError(f"spec {s.key} has no py_type")

    for s in SETTINGS_REGISTRY.values():
        if s.alias_of is not None and s.alias_of not in SETTINGS_REGISTRY:
            raise AssertionError(f"alias target missing for {s.key}: {s.alias_of}")

    if SERVER_WINS_KEYS != _SERVER_WINS_KEYS:
        raise AssertionError('SERVER_WINS_KEYS drifted from the server_wins flags')

    # Handler coverage (bidirectional): a spec that declares a handler must have one
    # registered in config_handlers, and every registered handler must belong to a
    # registry key. Without this, an update_settings batch can report a key as applied
    # while nothing changes (the phantom-applied-key bug class). Imported lazily:
    # config_handlers imports this module, so a module-level import would be circular.
    from agent_cascade.config_handlers import CONFIG_HANDLERS

    unhandled = sorted(s.key for s in SETTINGS_REGISTRY.values()
                       if s.handler and s.handler not in CONFIG_HANDLERS)
    if unhandled:
        raise AssertionError(f"specs declare unregistered handlers: {unhandled}")
    orphan_handlers = sorted(h for h in CONFIG_HANDLERS if h not in SETTINGS_REGISTRY)
    if orphan_handlers:
        raise AssertionError(f"handlers registered for unknown keys: {orphan_handlers}")
