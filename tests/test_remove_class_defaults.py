#!/usr/bin/env python3
"""Tests for the removal of per-agent-class hardcoded tool defaults (Layer 3).

Covers:
- The resolver no longer applies Layer 3 (per-class hardcoded defaults). Security/Compressor
  with no config fall back to the Layer 4 safe baseline; with UI config only the user's tools
  are disabled. Orchestrator stays excluded from the baseline (empty disabled set when unconfigured).
- The one-time migration (_migrate_class_defaults_to_ui_config) seeds all six core classes into
  the UI config, EXCLUDING shell_cmd for system agents (Security/Compressor) so they keep
  read-only shell access via restricted_shell=True. User-configured keys are never overridden.
  A sentinel prevents re-runs and survives normal saves.
- System agents still get restricted_shell=True set by _create_system_agent (flag only — the full
  restricted-shell mechanism is covered by test_shell_cmd_restricted_mode.py).

No LLM or network connections required.
"""

import json
import threading
from pathlib import Path

import pytest

from agent_cascade.constants import (
    DEFAULT_COMPRESSOR_DISABLED_TOOLS,
    DEFAULT_GENERALIST_DISABLED_TOOLS,
    DEFAULT_NEW_AGENT_DISABLED_TOOLS,
    DEFAULT_ORCHESTRATOR_DISABLED_TOOLS,
    DEFAULT_REVIEWER_DISABLED_TOOLS,
    DEFAULT_SECURITY_DISABLED_TOOLS,
    DEFAULT_WRITER_DISABLED_TOOLS,
)
from agent_cascade.pool.config_persist import ConfigPersistMixin
from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent


SENTINEL = '_class_defaults_migrated'


def _make_pool(path: Path):
    """Minimal pool stub exposing only what the migration + save/load paths need.

    Mirrors the real AgentPool attribute names (pool/core.py) so the mixin methods run unmodified.
    """
    class _Pool(ConfigPersistMixin):
        def __init__(self, path: Path):
            self.llm_cfg = {}
            self.settings = None
            self._pool_settings_path = path
            self._settings_save_lock = threading.Lock()
            self._ui_disabled_tools = {}
            self._ui_disabled_tools_lock = threading.RLock()

    return _Pool(path)


# ============================================================================
# Resolver — Layer 3 removed
# ============================================================================


class TestResolverNoLayer3:
    """The resolver must no longer union per-class hardcoded defaults (old Layer 3)."""

    def test_security_no_config_gets_empty_not_old_layer3(self):
        """Security with no config → NOT the old 14-tool Layer-3 set.

        With Layer 4's exclusion list kept as-is (per task instruction), Security is excluded from
        the safe baseline too, so an unconfigured Security resolves to an empty disabled set. In
        practice the migration seeds its config (non-empty) on first run, which suppresses this path.
        """
        result = resolve_disabled_tools_for_agent(
            instance_override=None, template_cfg=None, agent_name='Security', agent_type='Security')
        # The old Layer-3 Security set (14 tools incl. call_agent/edit_file/shell_cmd) must NOT apply.
        assert result != DEFAULT_SECURITY_DISABLED_TOOLS
        assert 'call_agent' not in result  # was in the old Security defaults
        assert 'edit_file' not in result   # was in the old Security defaults
        assert result == set()             # excluded from baseline → empty (migration seeds config at runtime)

    def test_security_with_ui_config_only_user_tools(self):
        """Security with UI config → only the user's tools disabled; shell_cmd stays available."""
        result = resolve_disabled_tools_for_agent(
            instance_override={'disabled_tools': {'Security': ['write_file']}},
            template_cfg=None, agent_name='Security', agent_type='Security')
        assert 'write_file' in result
        # has_explicit_config=True → Layer 4 baseline is suppressed (no phantom tools).
        assert result == {'write_file'}
        # shell_cmd must NOT be disabled (system agents keep it available in restricted mode).
        assert 'shell_cmd' not in result

    def test_security_empty_dict_config_suppresses_baseline(self):
        """Present-but-empty config → has_explicit_config=True, so ALL tools enabled (disabled == ∅)."""
        result = resolve_disabled_tools_for_agent(
            instance_override={'disabled_tools': {'Security': []}},
            template_cfg=None, agent_name='Security', agent_type='Security')
        assert result == set()

    def test_compressor_no_config_gets_empty_not_old_layer3(self):
        """Compressor with no config → NOT the old 26-tool Layer-3 set (excluded from baseline)."""
        result = resolve_disabled_tools_for_agent(
            instance_override=None, template_cfg=None, agent_name='Compressor', agent_type='Compressor')
        # The old Layer-3 Compressor set (26 tools) must NOT apply.
        assert result != DEFAULT_COMPRESSOR_DISABLED_TOOLS
        assert 'read_file' not in result  # was in the old Compressor defaults
        assert result == set()            # excluded from baseline → empty (migration seeds config at runtime)

    def test_orchestrator_no_config_empty(self):
        """Orchestrator with no config → excluded from Layer 4 baseline (disabled == ∅)."""
        result = resolve_disabled_tools_for_agent(
            instance_override=None, template_cfg=None, agent_name='Main', agent_type='orchestrator')
        assert result == set()

    def test_unconfigured_coder_gets_layer4_baseline(self):
        """A random unconfigured agent (Coder) with no config → Layer 4 baseline applies."""
        result = resolve_disabled_tools_for_agent(
            instance_override=None, template_cfg=None, agent_name='coder', agent_type='coder')
        assert result == DEFAULT_NEW_AGENT_DISABLED_TOOLS

    def test_template_config_layer2_suppresses_baseline(self):
        """A non-empty Layer 2 (template) config suppresses the Layer 4 baseline."""
        result = resolve_disabled_tools_for_agent(
            instance_override=None,
            template_cfg={'disabled_tools': {'Security': ['delete_file']}},
            agent_name='Security', agent_type='Security')
        assert result == {'delete_file'}


# ============================================================================
# Migration — seeding UI config with class defaults (minus shell_cmd for system agents)
# ============================================================================


class TestMigrationSeeding:
    """_migrate_class_defaults_to_ui_config seeds the UI config once, idempotently."""

    def test_fresh_install_seeds_all_six_classes(self, tmp_path):
        pool = _make_pool(tmp_path / 'pool_settings.json')  # file does not exist yet
        pool._migrate_class_defaults_to_ui_config()

        ui = pool._ui_disabled_tools
        assert set(ui.keys()) == {'Security', 'Compressor', 'Generalist', 'Orchestrator', 'Reviewer', 'Writer'}
        # System agents keep shell_cmd available (excluded from the seeded disabled list).
        assert 'shell_cmd' not in ui['Security']
        assert 'shell_cmd' not in ui['Compressor']
        # Writer keeps shell_cmd disabled (unchanged behavior).
        assert 'shell_cmd' in ui['Writer']

    def test_seed_matches_constants_minus_shellcmd(self, tmp_path):
        pool = _make_pool(tmp_path / 'pool_settings.json')
        pool._migrate_class_defaults_to_ui_config()
        ui = pool._ui_disabled_tools
        assert set(ui['Security']) == set(DEFAULT_SECURITY_DISABLED_TOOLS - {'shell_cmd'})
        assert set(ui['Compressor']) == set(DEFAULT_COMPRESSOR_DISABLED_TOOLS - {'shell_cmd'})
        assert set(ui['Generalist']) == set(DEFAULT_GENERALIST_DISABLED_TOOLS)
        assert set(ui['Orchestrator']) == set(DEFAULT_ORCHESTRATOR_DISABLED_TOOLS)
        assert set(ui['Reviewer']) == set(DEFAULT_REVIEWER_DISABLED_TOOLS)
        assert set(ui['Writer']) == set(DEFAULT_WRITER_DISABLED_TOOLS)

    def test_existing_user_config_not_overridden(self, tmp_path):
        """A key the user already configured is NOT overridden by the migration."""
        path = tmp_path / 'pool_settings.json'
        # User explicitly disabled shell_cmd for Security (they want it gone entirely).
        path.write_text(json.dumps({'disabled_tools': {'Security': ['shell_cmd']}}), encoding='utf-8')

        pool = _make_pool(path)
        pool._apply_loaded_disabled_tools({'Security': ['shell_cmd']})  # load user config first
        pool._migrate_class_defaults_to_ui_config()

        ui = pool._ui_disabled_tools
        assert ui['Security'] == ['shell_cmd']  # user's explicit entry wins — NOT re-seeded
        # Other classes still get seeded.
        assert 'Writer' in ui and 'shell_cmd' in ui['Writer']

    def test_empty_dict_config_on_disk_not_clobbered(self, tmp_path):
        """A user who saved an empty per-agent config ({} or no keys) is not harmed by the migration.

        Documents the interaction with _load_pool_settings: an empty `disabled_tools` value is not
        applied to the live cache (pre-existing truthiness check), so the migration seeds defaults.
        Because setdefault never overrides a key the user actually set, and {} has no keys, nothing
        the user configured is lost — the seeded defaults are exactly what they had before the change.
        """
        path = tmp_path / 'pool_settings.json'
        path.write_text(json.dumps({'disabled_tools': {}}), encoding='utf-8')

        pool = _make_pool(path)
        # Reproduce the real load path: empty dict is skipped by `if disabled_tools_raw:`.
        disabled_tools_raw = json.loads(path.read_text(encoding='utf-8')).get('disabled_tools')
        if disabled_tools_raw:  # mirrors _load_pool_settings (empty → not applied)
            pool._apply_loaded_disabled_tools(disabled_tools_raw)
        pool._migrate_class_defaults_to_ui_config()

        ui = pool._ui_disabled_tools
        # All six classes get seeded defaults (the user had no per-agent keys to lose).
        assert set(ui.keys()) == {'Security', 'Compressor', 'Generalist', 'Orchestrator', 'Reviewer', 'Writer'}
        # System agents keep shell_cmd available.
        assert 'shell_cmd' not in ui['Security'] and 'shell_cmd' not in ui['Compressor']

    def test_sentinel_prevents_rerun(self, tmp_path):
        """With the sentinel present, the migration is a no-op (does not touch user config)."""
        path = tmp_path / 'pool_settings.json'
        # Sentinel present, but NO seeded keys — proves the early-return skips seeding.
        path.write_text(json.dumps({SENTINEL: True}), encoding='utf-8')

        pool = _make_pool(path)
        pool._migrate_class_defaults_to_ui_config()

        assert pool._ui_disabled_tools == {}  # nothing seeded

    def test_sentinel_written_to_file(self, tmp_path):
        path = tmp_path / 'pool_settings.json'
        pool = _make_pool(path)
        pool._migrate_class_defaults_to_ui_config()

        on_disk = json.loads(path.read_text(encoding='utf-8'))
        assert on_disk.get(SENTINEL) is True

    def test_save_preserves_sentinel(self, tmp_path):
        """_save_pool_settings must carry the sentinel across a normal save (it rebuilds `data`)."""
        path = tmp_path / 'pool_settings.json'
        # Simulate an already-migrated file with a user-configured agent.
        path.write_text(json.dumps({SENTINEL: True, 'disabled_tools': {'Writer': ['shell_cmd']}}),
                        encoding='utf-8')

        pool = _make_pool(path)
        pool._ui_disabled_tools = {'Writer': ['shell_cmd']}
        # PoolSettings.to_dict() does not know about the sentinel — it must be preserved from disk.
        pool._save_pool_settings()

        on_disk = json.loads(path.read_text(encoding='utf-8'))
        assert on_disk.get(SENTINEL) is True, 'sentinel was dropped by _save_pool_settings'


# ============================================================================
# System agents still get restricted_shell=True (flag only)
# ============================================================================


class TestSystemAgentRestrictedShellFlag:
    """_create_system_agent must still set restricted_shell=True (mechanism unchanged)."""

    def test_create_system_agent_sets_restricted_flag(self):
        """The flag is set unconditionally for system-invoked agents."""
        from agent_cascade.engine.core import ExecutionEngine

        inst = object.__new__(ExecutionEngine)  # no __init__ — we only exercise the flag assignment

        class _Lifecycle:
            def find_or_create_instance(self, *a, **k):
                fake = type('FakeInst', (), {'agent_class': 'Security'})()
                return fake, False, False

            def build_system_message(self, *a, **k):
                return 'sys'

            def initialize_conversation(self, *a, **k):
                return []

            def propagate_settings(self, *a, **k):
                return None

        inst.lifecycle = _Lifecycle()
        inst.pool = type('FakePool', (), {'active_stack_append': staticmethod(lambda *a, **k: None)})()
        inst.stream_publisher = type('FakeSP', (), {'push_initial_state': staticmethod(lambda *a, **k: None)})()

        created = inst._create_system_agent(
            agent_class='Security', instance_name='Security_test', task='t', caller='Main')

        assert created.restricted_shell is True
