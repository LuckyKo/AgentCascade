"""Wiring round-trip test for the ``slot_queue_timeout_seconds`` UI setting.

Clones the shape of tests/test_allow_parallel_agents_config.py: it promotes the
manual grep checklist to automated coverage across every seam that carries the
key — POOL_SETTINGS_KEYS, the registered handler, BOTH state_builder blocks, and
the four app.js sites + the index.html input id.

The dead-man's switch reads this setting live at call time (gotcha #10); the
behavioral live-read is covered in test_slot_queue_deadmans_switch.py
(TestQueueLimitsLiveRead). This file only proves the key is wired through every
seam so the UI value is not silently dropped or reverted.
"""

from pathlib import Path


KEY = 'slot_queue_timeout_seconds'
DOM_ID = 'setting-slot-queue-timeout'


class TestWiringSeams:
    """Smoke test: the key is present in every Python + frontend seam that carries it."""

    def test_in_pool_settings_keys(self):
        from agent_cascade.config_handlers import POOL_SETTINGS_KEYS

        assert KEY in POOL_SETTINGS_KEYS, \
            f"{KEY} missing from POOL_SETTINGS_KEYS (ws_handlers save trigger)"

    def test_handler_registered(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        assert KEY in CONFIG_HANDLERS, f"missing handler for {KEY}"

    def test_in_both_state_builder_blocks(self):
        """pool_settings comes from ONE shared function (build_pool_settings_snapshot) used by
        both build_state_from_pool and build_stream_update_from_pool, so the UI cannot revert
        the value on stream ticks. Assert the shared seam and that the key is really emitted."""
        src = Path('agent_cascade/api_integration_pkg/state_builder.py').read_text(encoding='utf-8')
        assert src.count('pool_settings = build_pool_settings_snapshot(pool)') >= 2, \
            f"{KEY}: both state_builder paths must share build_pool_settings_snapshot"

        from agent_cascade.agent_instance import PoolSettings
        from agent_cascade.api_integration_pkg.state_builder import build_pool_settings_snapshot

        class _StubPool:
            settings = PoolSettings()
            llm_cfg = {}

        assert KEY in build_pool_settings_snapshot(_StubPool()), \
            f"{KEY} not emitted by the registry-driven snapshot"

    def test_frontend_seams_reference_key(self):
        """app.js must reference the key in the three real seams (POOL_SETTINGS_MAP entry,
        saveSettings() localStorage mirror, getGenerateCfg() write); index.html must carry the
        input id. (The old ≥4 raw count included the dead localKey duplicate.)"""
        app_js = Path('web_ui/app.js').read_text(encoding='utf-8')
        index_html = Path('web_ui/index.html').read_text(encoding='utf-8')

        assert f"key: '{KEY}'" in app_js, f"{KEY}: missing POOL_SETTINGS_MAP entry for #{DOM_ID}"
        assert f"s['{KEY}']" in app_js, f"{KEY}: missing saveSettings localStorage mirror"
        assert f"cfg.{KEY}" in app_js, f"{KEY}: missing getGenerateCfg write"

        assert f'id="{DOM_ID}"' in index_html, f"missing input id {DOM_ID} in index.html"


class TestHandlerRoundTrip:
    """The handler writes the value into pool.settings (live, no restart)."""

    def test_handler_sets_value(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        class _P:
            def __init__(self):
                from agent_cascade.agent_instance import PoolSettings
                self.settings = PoolSettings()

        pool = _P()
        CONFIG_HANDLERS[KEY]({'slot_queue_timeout_seconds': 450}, pool, [])
        assert pool.settings.slot_queue_timeout_seconds == 450
