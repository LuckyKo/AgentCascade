"""Frontend-seam guard for the ``idle_timeout_seconds`` / ``system_agent_idle_timeout_seconds``
settings (the "resets on restart" hydration bug).

Root cause was frontend-only: both keys are server-backed and persisted to
``pool_settings.json`` correctly, but were absent from ``web_ui/app.js`` ``POOL_SETTINGS_MAP`` —
the ONLY server->DOM hydration path (``syncPoolSettings``). So the inputs kept their HTML
default and the next ``saveSettings()`` overwrote the persisted value.

This file mirrors the wiring-seam shape of
``test_allow_parallel_agents_config.py::TestWiringSeams`` for BOTH keys: it asserts the key
survives every Python + frontend seam that carries it. The JS behavioural test
(``test_settings_work_folders_hydrate.js``) is node-only; this guard exists so CI/pytest
also fails if the ``POOL_SETTINGS_MAP`` entry is dropped.

NOTE (deliberate divergence from the ``allow_parallel_agents`` template): that test also
asserts the key is in ``constants.NON_LLM_KEYS``. That list is a curated belt — its membership
does NOT follow ``value_source`` (e.g. ``max_parallel_agents`` and ``auto_continue`` are
``value_source='settings'`` yet present in it). The idle-timeout pair is an execution-side
inactivity timer, not an LLM sampling param, so it is intentionally NOT in ``NON_LLM_KEYS`` —
adding that assertion would be a false red. The real guard against LLM-param leakage is the
strict request-body allowlist ``ALLOWED_LLM_PARAMS`` (``agent_cascade/oai.py``), which strips
any key not on it before the HTTP call. The registry / handler / snapshot seams below are the
real, load-bearing ones here.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest


# (registry key, DOM input id) — parametrized over the idle-timeout pair.
IDLE_TIMEOUT_SEAMS = [
    ('idle_timeout_seconds', 'setting-idle-timeout'),
    ('system_agent_idle_timeout_seconds', 'setting-system-idle-timeout'),
]


class TestWiringSeams:
    """Smoke test: each key is present in every Python + frontend seam that carries it."""

    @pytest.mark.parametrize('key,dom_id', IDLE_TIMEOUT_SEAMS)
    def test_in_pool_settings_keys(self, key, dom_id):
        from agent_cascade.config_handlers import POOL_SETTINGS_KEYS

        assert key in POOL_SETTINGS_KEYS, \
            f"{key} missing from POOL_SETTINGS_KEYS (ws_handlers save trigger)"

    @pytest.mark.parametrize('key,dom_id', IDLE_TIMEOUT_SEAMS)
    def test_handler_registered(self, key, dom_id):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        assert key in CONFIG_HANDLERS, f"missing handler for {key}"

    @pytest.mark.parametrize('key,dom_id', IDLE_TIMEOUT_SEAMS)
    def test_in_state_builder_snapshot(self, key, dom_id):
        """pool_settings is built by ONE shared function (build_pool_settings_snapshot),
        called by both build_state_from_pool and build_stream_update_from_pool, so the two
        payload paths cannot drift. Assert the shared seam and that the key is really emitted."""
        src = Path('agent_cascade/api_integration_pkg/state_builder.py').read_text(encoding='utf-8')
        assert src.count('pool_settings = build_pool_settings_snapshot(pool)') >= 2, \
            f"{key}: both state_builder paths must share build_pool_settings_snapshot"

        from agent_cascade.agent_instance import PoolSettings
        from agent_cascade.api_integration_pkg.state_builder import build_pool_settings_snapshot

        class _StubPool:
            settings = PoolSettings()
            llm_cfg = {}

        assert key in build_pool_settings_snapshot(_StubPool()), \
            f"{key} not emitted by the registry-driven snapshot"

    @pytest.mark.parametrize('key,dom_id', IDLE_TIMEOUT_SEAMS)
    def test_frontend_seams_reference_key(self, key, dom_id):
        """app.js must carry the POOL_SETTINGS_MAP entry (the hydration seam that was the
        bug) and index.html must carry the input id the map targets."""
        from agent_cascade.settings_registry import POOL_SETTINGS_KEYS

        app_js = Path('web_ui/app.js').read_text(encoding='utf-8')
        index_html = Path('web_ui/index.html').read_text(encoding='utf-8')

        assert key in POOL_SETTINGS_KEYS, f"{key} must be in the registry (POOL_SETTINGS_KEYS)"
        # The map entry form is: { id: '#<dom_id>', prop: 'value', key: '<key>' }.
        assert f"id: '#{dom_id}'" in app_js, \
            f"{key}: missing POOL_SETTINGS_MAP entry for #{dom_id}"
        assert f"key: '{key}'" in app_js, \
            f"{key}: missing POOL_SETTINGS_MAP key for #{dom_id}"
        assert f'id="{dom_id}"' in index_html, f"missing input id {dom_id} in index.html"


class TestDeadCodeRemoved:
    """The dead hyphenated localStorage mirror (s['idle-timeout'] / s['system-idle-timeout'])
    was removed from saveSettings(); those keys are not in UI_PREFS_ALLOWLIST so they were
    silently dropped. Guard that they stay gone (the misleading 'key mismatch' the bug report
    hinted at)."""

    # The dead writes used hyphenated keys WITHOUT the `setting-` prefix; only the
    # DOM ids carry that prefix. Assert the exact strings that were the dead code.
    DEAD_MIRROR_KEYS = ['idle-timeout', 'system-idle-timeout']

    @pytest.mark.parametrize('dead_key', DEAD_MIRROR_KEYS)
    def test_hyphenated_writes_gone(self, dead_key):
        app_js = Path('web_ui/app.js').read_text(encoding='utf-8')
        assert f"s['{dead_key}']" not in app_js, \
            f"dead mirror write s['{dead_key}'] reappeared in saveSettings()"
