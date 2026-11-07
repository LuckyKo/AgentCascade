"""Tests for the settings registry (single source of truth for settings keys).

Covers Phase 0 exit criteria:
  * registry is internally consistent (validate_registry),
  * POOL_SETTINGS_KEYS / EXTRA_PERSIST_KEYS do not drift from the registry,
  * every broadcast=True spec key appears in the state snapshot,
  * golden snapshot of the historical pool_settings payload is byte-compatible.
"""

import json

import pytest

from agent_cascade.agent_instance import PoolSettings
from agent_cascade.api_integration_pkg.state_builder import build_pool_settings_snapshot
from agent_cascade.config_handlers import (
    EXPORT_IMPORT_ALLOWLIST,
    EXTRA_PERSIST_KEYS,
    LLM_CFG_PERSIST_KEYS,
    POOL_SETTINGS_KEYS,
)
from agent_cascade.settings_registry import (
    BROADCAST_KEYS,
    NEVER_BROADCAST_PERSIST_KEYS,
    POOL_SETTINGS_KEYS as REG_POOL,
    SETTINGS_REGISTRY as SETTING_SPECS,
    SettingSpec,
    validate_registry,
)


# ── Registry integrity ──────────────────────────────────────────────────

def test_registry_is_consistent():
    """validate_registry() must pass: no drift, every spec has a handler or is server-only."""
    validate_registry()


def test_read_file_max_chars_is_plumbed_through_registry():
    """read_file_max_chars (UI-exposed char limit) must be wired end-to-end through the
    registry single source of truth: a pool spec sourced from llm_cfg, a default, presence in
    the derived persist set, a registered config handler, and validate_registry still passes."""
    from agent_cascade.config_handlers import CONFIG_HANDLERS
    from agent_cascade.settings_registry import _LLM_CFG_SOURCED, _DEFAULTS

    # Pool spec with llm_cfg value-source (registry drives LLM_CFG_PERSIST_KEYS, not a list).
    assert 'read_file_max_chars' in SETTING_SPECS
    assert SETTING_SPECS['read_file_max_chars'].value_source == 'llm_cfg'
    assert SETTING_SPECS['read_file_max_chars'].default == 4000
    # Explicit membership in the source sets.
    assert 'read_file_max_chars' in _LLM_CFG_SOURCED
    assert _DEFAULTS['read_file_max_chars'] == 4000
    assert 'read_file_max_chars' in LLM_CFG_PERSIST_KEYS
    # Bidirectional handler coverage: a registered handler exists for the key.
    assert SETTING_SPECS['read_file_max_chars'].handler == 'read_file_max_chars'
    assert 'read_file_max_chars' in CONFIG_HANDLERS
    validate_registry()  # no count drift / orphan-handler failure


def test_every_pool_key_has_a_spec():
    """Every key in POOL_SETTINGS_KEYS must have a registry spec (no drift)."""
    missing = POOL_SETTINGS_KEYS - set(SETTING_SPECS)
    assert not missing, f"POOL_SETTINGS_KEYS keys missing from registry: {sorted(missing)}"


def test_registry_pool_set_matches_config_handlers():
    """The registry-derived pool set must equal the re-exported POOL_SETTINGS_KEYS."""
    assert REG_POOL == POOL_SETTINGS_KEYS


def test_extra_persist_keys_match():
    assert set(EXTRA_PERSIST_KEYS) == {'disabled_tools', 'auto_security', 'compression_fraction'}


def test_disk_only_keys_have_specs():
    """The 7 disk-only keys written by _save_pool_settings must have specs."""
    disk_only = {'afk_enabled', 'afk_message', 'afk_mode', 'compression_min_usage_pct',
                 'holder_dms_max_bound_s', 'holder_dms_window_s', 'tool_loop_sim_threshold'}
    for key in disk_only:
        assert key in SETTING_SPECS, f"disk-only key {key!r} missing from registry"


def test_export_import_allowlist_covers_disk_only():
    """BUG_0214: the export/import allowlist must include the 7 disk-only keys."""
    disk_only = {'afk_enabled', 'afk_message', 'afk_mode', 'compression_min_usage_pct',
                 'holder_dms_max_bound_s', 'holder_dms_window_s', 'tool_loop_sim_threshold'}
    assert disk_only <= EXPORT_IMPORT_ALLOWLIST


# ── Broadcast completeness ──────────────────────────────────────────────

def _make_pool():
    from agent_cascade.pool.config_persist import ConfigPersistMixin

    class _Pool(ConfigPersistMixin):
        def __init__(self):
            self.settings = PoolSettings()
            self.llm_cfg = {'tool_result_max_chars': 25000, 'grep_char_limit': 12000,
                            'memory_hint_enabled': False, 'skill_score_ghalf': 0.5}
            self._ui_disabled_tools = {}
            import threading
            self._ui_disabled_tools_lock = threading.Lock()
            self.operation_manager = None
            self._enable_async_shell_console_window = False
            self._pool_settings_path = None
            self._settings_save_lock = threading.Lock()

    return _Pool()


def test_every_broadcast_key_in_snapshot():
    """Every BROADCAST_KEYS entry must appear in the state snapshot."""
    from agent_cascade.settings_registry import BROADCAST_KEYS
    pool = _make_pool()
    snapshot = build_pool_settings_snapshot(pool)
    missing = BROADCAST_KEYS - set(snapshot)
    assert not missing, f"broadcast keys missing from snapshot: {sorted(missing)}"


def test_runtime_sourced_keys_carry_live_values():
    """auto_security / compression_fraction live on the runtime-state singleton (not
    PoolSettings), so the snapshot must carry the LIVE values, not a null default.

    Unit note: runtime_state holds the 0-1 FRACTION that the compression code consumes,
    but the settings contract (UI control, pool_settings.json, config handler) is the
    PERCENT — so the snapshot emits percent.
    """
    from agent_cascade.runtime_state import state as _runtime_state
    pool = _make_pool()
    # Save, mutate, build, restore — assert the snapshot reflects the live values.
    saved = (bool(_runtime_state.auto_security), _runtime_state.compression_fraction)
    try:
        _runtime_state.auto_security = False
        _runtime_state.compression_fraction = 0.42
        snapshot = build_pool_settings_snapshot(pool)
        assert snapshot['auto_security'] is False, \
            f"auto_security broadcast as {snapshot['auto_security']!r}, expected False"
        assert snapshot['compression_fraction'] == 42.0, \
            f"compression_fraction broadcast as {snapshot['compression_fraction']!r}, expected 42.0 (percent)"
        # And the default (True) case:
        _runtime_state.auto_security = True
        _runtime_state.compression_fraction = 0.5
        snapshot = build_pool_settings_snapshot(pool)
        assert snapshot['auto_security'] is True
        assert snapshot['compression_fraction'] == 50.0
    finally:
        _runtime_state.auto_security, _runtime_state.compression_fraction = saved


def test_never_broadcast_keys_now_broadcast():
    """The 4 never-broadcast persist keys must now be emitted in the snapshot."""
    pool = _make_pool()
    snapshot = build_pool_settings_snapshot(pool)
    for key in NEVER_BROADCAST_PERSIST_KEYS:
        assert key in snapshot, f"never-broadcast key {key!r} not in snapshot"


# ── Golden snapshot (pinned baseline for the historical core payload) ─────
#
# Pinned key→value baseline of the historical core (PoolSettings-sourced) pool_settings
# payload. Values are the PoolSettings field defaults — the byte-compatible historical
# values. This is a REAL baseline (literal constants, not re-read from the live pool), so
# a future change to a PoolSettings default, a dropped key, or a renamed key is caught.
# If a value legitimately changes, update this dict in the same change and note why.
#
# Note on the registry-driven refactor: build_pool_settings_snapshot now walks
# SETTINGS_REGISTRY, so an llm_cfg-sourced key that is absent from the live pool's
# llm_cfg is emitted with its registry default instead of being omitted. That is the
# intended behaviour (the snapshot is the client's complete settings contract); the
# pinned values below are the values a default PoolSettings + empty llm_cfg produce.
_GOLDEN_CORE = {
    'idle_timeout_seconds': 900.0,
    'system_agent_idle_timeout_seconds': 300.0,
    'max_parallel_agents': 3,
    'auto_continue': True,
    'completion_classifier_enabled': True,
    'enable_agent_budgeting': False,
    'allow_parallel_agents': True,
    'state_kv_save_enabled': False,
    'max_turns': 250,
    'soft_turn_limit': False,
    'max_auto_rollbacks': 5,
    'auto_rollback_on_loop': True,
    'loop_exact_rollback_enabled': True,
    'loop_fuzzy_warning_enabled': True,
    'tool_loop_fuzzy_rollback_enabled': False,
    'tool_loop_detection_enabled': True,
    'inner_loop_detect_enabled': True,
    'default_load_skill_mode': 'AUTO',
    'auto_skill_enabled': False,
    'auto_skill_min_turns': 20,
    'auto_skill_extra_turns': 25,
    'auto_skill_mode': 'basic',
    'skill_selector_mode': 'keyword',
    'telegram_bridge_enabled': False,
    'retry_max_attempts': 3,
    'endpoint_max_retries': 1,
    'retry_base_delay': 1.0,
    'retry_max_delay': 8.0,
    'slot_queue_timeout_seconds': 300,
    'ci_execution_timeout': 120,
    'ci_watchdog_timeout': 300,
    'ci_stale_container_ttl': 1200,
    'cache_pool_enabled': False,
    'cache_pool_size': 50,
    'cache_threshold_chars': 1000,
    'stream_max_silence_seconds': 180.0,
    'stream_max_total_seconds': 900.0,
    'image_caption_mode': 'auto',

    # ── llm_cfg-sourced keys (new in the registry-driven snapshot) ──
    # build_pool_settings_snapshot is now registry-driven: every broadcast key is
    # emitted, falling back to the registry default when pool.llm_cfg does not carry
    # the key. The old builder omitted those keys entirely, so this block pins the
    # NEW intended payload for every llm_cfg key the test pool does not set. The
    # four keys the test pool DOES set (tool_result_max_chars, grep_char_limit,
    # memory_hint_enabled, skill_score_ghalf) are live values, not defaults, and are
    # pinned in test_runtime_sourced_keys_carry_live_values-style checks instead.
    # A future change to any of these defaults must be an explicit decision here.
    'code_char_limit': 8000,
    'list_dir_char_limit': 8000,
    'shell_char_limit': 8000,
    'read_file_max_chars': 4000,
    'grep_spillover': True,
    'max_images_for_llm': 2,
    'memory_hint_cooldown_seconds': 600,
    'memory_hint_max_entries': 3,
    'memory_hint_query_chars': 1000,
    'memory_hint_skill_suggestions': True,
    'memory_hint_skill_threshold': 0.0,
    'memory_hint_threshold': 0.0,
    'skill_active_max_cap': 200,
    'skill_active_min_cap': 20,
    'skill_active_target_k': 1.0,
    'skill_always_protected': (
        'bug-tracker-entry-format,project-memory-writing,self-augmentation,skill-creator'
    ),
    'skill_auto_invalidate_enabled': True,
    'skill_fair_window_turns': 50,
    'skill_max_evictions_per_pass': 25,
    'skill_score_dq': 0.5,
    'skill_score_kq': 5,
    'skill_score_nhalf': 8.0,
    'skill_score_nmin': 5,
    'skill_score_q0': 5.0,
    'skill_score_rflood': 0.5,
    'skill_score_tau_turns': 200,
    'wild_read_truncation_chars': 4000,
}


def test_golden_snapshot_core_keys():
    """The historical core pool_settings payload must match the pinned baseline exactly."""
    pool = _make_pool()
    snapshot = build_pool_settings_snapshot(pool)
    missing = [k for k in _GOLDEN_CORE if k not in snapshot]
    assert not missing, f"golden core keys missing from snapshot: {missing}"
    for k, v in _GOLDEN_CORE.items():
        assert snapshot[k] == v, f"golden key {k!r} drifted: {snapshot[k]!r} != {v!r}"


def test_snapshot_is_json_serializable():
    pool = _make_pool()
    snapshot = build_pool_settings_snapshot(pool)
    # Must round-trip through JSON (the WS/REST transport).
    payload = json.loads(json.dumps(snapshot))
    assert payload == snapshot
