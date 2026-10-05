"""Wiring + handler tests for the ``allow_parallel_agents`` UI toggle.

The toggle (default ON = current behavior) forces every call_agent dispatch onto the SYNC
path when off. This file mirrors the shape of test_skill_scoring_settings.py: it promotes the
manual grep checklist to automated coverage across every seam that carries the key, plus a
handler unit test.

TRAP (plan §7): the handler guard is ``hasattr(agent_pool, 'settings')`` — a bare MagicMock
pool would auto-create ``settings``, making the guard vacuous. The fake pool here is a plain
object whose absence of ``settings`` is asserted explicitly so the guard stays meaningful.
"""

from pathlib import Path
from types import SimpleNamespace


class TestPoolSettingsField:
    """The new PoolSettings field exists with the correct default."""

    def test_default_is_true(self):
        from agent_cascade.agent_instance import PoolSettings

        assert PoolSettings().allow_parallel_agents is True


class TestWiringSeams:
    """Smoke test: the key is present in every Python + frontend seam that carries it."""

    KEY = 'allow_parallel_agents'

    def test_in_pool_settings_keys(self):
        from agent_cascade.config_handlers import POOL_SETTINGS_KEYS

        assert self.KEY in POOL_SETTINGS_KEYS, \
            f"{self.KEY} missing from POOL_SETTINGS_KEYS (ws_handlers save trigger)"

    def test_in_non_llm_keys(self):
        from agent_cascade.constants import NON_LLM_KEYS

        assert self.KEY in NON_LLM_KEYS, \
            f"{self.KEY} missing from NON_LLM_KEYS (would be forwarded as an LLM API param)"

    def test_handler_registered(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        assert self.KEY in CONFIG_HANDLERS, f"missing handler for {self.KEY}"

    def test_in_both_state_builder_blocks(self):
        """state_builder.py has TWO pool_settings serialization blocks (full state + stream
        update). Missing one = silent revert in half the UI sync paths."""
        src = Path('agent_cascade/api_integration_pkg/state_builder.py').read_text(encoding='utf-8')
        count = src.count(f"'{self.KEY}'")
        assert count >= 2, f"{self.KEY}: state_builder.py references it {count}× (want ≥2 — both blocks)"

    def test_frontend_seams_reference_key(self):
        """app.js must reference the key in POOL_SETTINGS_MAP, getGenerateCfg(), saveSettings()
        localStorage mirror, and loadSettings() restore (4 sites); index.html must carry the
        checkbox input id."""
        app_js = Path('web_ui/app.js').read_text(encoding='utf-8')
        index_html = Path('web_ui/index.html').read_text(encoding='utf-8')

        count = app_js.count(f"'{self.KEY}'")
        assert count >= 4, f"{self.KEY}: app.js references it {count}× (want ≥4)"
        assert "id: '#setting-allow-parallel'" in app_js, \
            f"{self.KEY}: missing POOL_SETTINGS_MAP entry for #setting-allow-parallel"

        dom_id = 'setting-allow-parallel'
        assert f'id="{dom_id}"' in index_html, f"missing input id {dom_id} in index.html"


class TestHandler:
    """Unit test for _handle_allow_parallel_agents — live mutation without restart (plan §3)."""

    def test_handler_sets_false(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        # Plain-object double: a real AgentPool always has .settings, but the handler's
        # hasattr guard must be tested against a pool that genuinely LACKS it (a bare
        # MagicMock would auto-create it and make the guard vacuous).
        fake_pool = SimpleNamespace(settings=SimpleNamespace(allow_parallel_agents=True))

        CONFIG_HANDLERS['allow_parallel_agents']({'allow_parallel_agents': False}, fake_pool, [])
        assert fake_pool.settings.allow_parallel_agents is False

    def test_handler_sets_true(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        fake_pool = SimpleNamespace(settings=SimpleNamespace(allow_parallel_agents=False))

        CONFIG_HANDLERS['allow_parallel_agents']({'allow_parallel_agents': True}, fake_pool, [])
        assert fake_pool.settings.allow_parallel_agents is True

    def test_handler_noop_without_settings(self):
        """A pool object without .settings must be skipped cleanly (no AttributeError)."""
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        bare_pool = SimpleNamespace()  # no .settings attribute at all

        CONFIG_HANDLERS['allow_parallel_agents']({'allow_parallel_agents': False}, bare_pool, [])
        assert not hasattr(bare_pool, 'settings')

    def test_handler_none_pool_is_noop(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        # Must not raise when agent_pool is None (ws_handlers passes it through as-is).
        CONFIG_HANDLERS['allow_parallel_agents']({'allow_parallel_agents': False}, None, [])

    def test_router_apply_live_mutation(self):
        """ConfigUpdateRouter.apply() mutates the live pool.settings without restart —
        the hot-reload guarantee from plan §3."""
        import asyncio
        from agent_cascade.config_handlers import ConfigUpdateRouter

        fake_pool = SimpleNamespace(settings=SimpleNamespace(allow_parallel_agents=True))
        router = ConfigUpdateRouter(fake_pool, [])

        asyncio.new_event_loop().run_until_complete(router.apply({'allow_parallel_agents': False}))
        assert fake_pool.settings.allow_parallel_agents is False
