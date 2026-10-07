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
        """state_builder.py has TWO pool_settings serialization blocks (full state + stream
        update). Missing one = the UI reverts the value on every stream tick."""
        src = Path('agent_cascade/api_integration_pkg/state_builder.py').read_text(encoding='utf-8')
        count = src.count(f"'{KEY}'")
        assert count >= 2, f"{KEY}: state_builder.py references it {count}× (want ≥2 — both blocks)"

    def test_frontend_seams_reference_key(self):
        """app.js must reference the key in POOL_SETTINGS_MAP, getGenerateCfg(), saveSettings()
        localStorage mirror, and loadSettings() restore (4 sites); index.html must carry the
        input id."""
        app_js = Path('web_ui/app.js').read_text(encoding='utf-8')
        index_html = Path('web_ui/index.html').read_text(encoding='utf-8')

        count = app_js.count(KEY)
        assert count >= 4, f"{KEY}: app.js references it {count}× (want ≥4)"
        assert f"id: '#setting-slot-queue-timeout'" in app_js, \
            f"{KEY}: missing POOL_SETTINGS_MAP entry for #{DOM_ID}"

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
