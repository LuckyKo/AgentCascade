"""Wiring round-trip test for the ``compression_review_max_retries`` UI setting (BUG_0419).

Clones the shape of tests/test_slot_queue_timeout_ui_wiring.py: it promotes the manual
grep checklist to automated coverage across every seam that carries the key — POOL_SETTINGS_KEYS,
the registered handler, the shared state_builder snapshot, and the app.js sites + index.html
input id.

Adapted for the COMPRESSION key family (deliberate difference from the slot_queue template):
the sibling compression keys are purely server-backed and carry NO ``s['<key>']`` localStorage
mirror line in saveSettings() (it would be dropped by the ui-prefs allowlist, i.e. dead code),
so this file asserts the two REAL app.js write seams (POOL_SETTINGS_MAP entry + getGenerateCfg
write) rather than a third localStorage mirror. The live-apply behavior (both consumer module
namespaces + the 0 = gate-off no-op) is covered in
tests/test_compression_review_max_retries_setting.py.
"""

from pathlib import Path
from unittest.mock import MagicMock


KEY = 'compression_review_max_retries'
DOM_ID = 'setting-compression-review-max-retries'


class TestWiringSeams:
    """Smoke test: the key is present in every Python + frontend seam that carries it."""

    def test_in_pool_settings_keys(self):
        from agent_cascade.config_handlers import POOL_SETTINGS_KEYS

        assert KEY in POOL_SETTINGS_KEYS, \
            f"{KEY} missing from POOL_SETTINGS_KEYS (update_config save trigger)"

    def test_handler_registered(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        assert KEY in CONFIG_HANDLERS, f"missing handler for {KEY}"

    def test_in_shared_state_builder_snapshot(self):
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
        """app.js must reference the key in the two real compression-family write seams
        (POOL_SETTINGS_MAP hydrate entry + getGenerateCfg write); index.html must carry the
        input id. (The sibling compression keys deliberately have NO saveSettings localStorage
        mirror, so — unlike the slot_queue clone — we do not assert one here.)"""
        app_js = Path('web_ui/app.js').read_text(encoding='utf-8')
        index_html = Path('web_ui/index.html').read_text(encoding='utf-8')

        assert f"key: '{KEY}'" in app_js, f"{KEY}: missing POOL_SETTINGS_MAP entry for #{DOM_ID}"
        assert f"cfg.{KEY}" in app_js, f"{KEY}: missing getGenerateCfg write"

        # 0-must-survive (decision 3): the write must NOT coerce 0 to a nonzero default via
        # `parseInt(...) || <n>`. Assert the NaN-safe 0-tolerant form is present.
        assert f"cfg.{KEY} = Number.isNaN(_crrRaw) ? 0 :" in app_js, \
            f"{KEY}: getGenerateCfg write must be 0-tolerant (no `|| nonzero` coercion)"

        assert f'id="{DOM_ID}"' in index_html, f"missing input id {DOM_ID} in index.html"
        # The control must accept 0..5.
        assert 'id="setting-compression-review-max-retries" min="0" max="5"' in index_html, \
            f"{DOM_ID} must expose min=0 max=5"


class TestHandlerRoundTrip:
    """The handler writes the value into pool.settings (live, no restart).

    It also mirrors into the two consumer module namespaces (BUG_0419 strategy A) — this class
    saves/restores those module globals so the write does not leak into the rest of the suite.
    """

    def _run_handler(self, value):
        from unittest.mock import MagicMock

        from agent_cascade.agent_instance import PoolSettings
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        pool = MagicMock()
        pool.settings = PoolSettings()
        CONFIG_HANDLERS[KEY]({KEY: value}, pool, [])
        return pool

    def test_handler_sets_value(self):
        pool = self._run_handler(3)
        assert pool.settings.compression_review_max_retries == 3

    def test_handler_mirrors_into_both_consumer_namespaces(self):
        import agent_cascade.compression.agent_invoker as ai
        import agent_cascade.compression.core as core

        from agent_cascade.agent_instance import PoolSettings
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        saved_core, saved_ai = core.COMPRESSION_REVIEW_MAX_RETRIES, ai.COMPRESSION_REVIEW_MAX_RETRIES
        try:
            pool = MagicMock()
            pool.settings = PoolSettings()
            CONFIG_HANDLERS[KEY]({KEY: 4}, pool, [])
            assert core.COMPRESSION_REVIEW_MAX_RETRIES == 4
            assert ai.COMPRESSION_REVIEW_MAX_RETRIES == 4
        finally:
            core.COMPRESSION_REVIEW_MAX_RETRIES = saved_core
            ai.COMPRESSION_REVIEW_MAX_RETRIES = saved_ai
