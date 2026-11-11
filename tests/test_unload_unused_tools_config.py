"""Wiring + handler tests for the ``unload_unused_tools_post_compression`` UI toggle.

Mirrors tests/test_allow_parallel_agents_config.py. This knob (off by default; the PoolSettings
default is seeded from the AGENT_CASCADE_UNLOAD_UNUSED_TOOLS_POST_COMPRESSION env var) controls
whether the compression handler unloads MCP tools that were not used in the kept context, after a
successful compression (compression/handler.py: _maybe_unload_unused_mcp_tools).

Two things this file pins that go beyond the allow_parallel_agents template:
  * the CONSUMER SEAM must read pool.settings LIVE, not the import-time module constant
    (settings.UNLOAD_UNUSED_TOOLS_POST_COMPRESSION) — otherwise the UI toggle persists/broadcasts
    but is a silent no-op until a restart;
  * validate_registry() must still pass (the hardcoded POOL_SETTINGS_KEYS count drift-guard).

TRAP (same as the template): the handler guard is ``hasattr(agent_pool, 'settings')`` — a bare
MagicMock pool would auto-create ``settings`` and make the guard vacuous, so the fake pool is a
plain SimpleNamespace whose absence of ``settings`` is asserted explicitly.
"""

from pathlib import Path
from types import SimpleNamespace


class TestPoolSettingsField:
    """The new PoolSettings field exists with the correct (off) default."""

    def test_default_matches_env_constant(self):
        from agent_cascade.agent_instance import PoolSettings
        from agent_cascade import settings

        # The default is seeded from the AGENT_CASCADE_UNLOAD_UNUSED_TOOLS_POST_COMPRESSION env
        # constant (False when unset — the CI/default case), so documented env opt-in survives
        # pool-backed startup while the UI toggle still overrides it live.
        assert PoolSettings().unload_unused_tools_post_compression is settings.UNLOAD_UNUSED_TOOLS_POST_COMPRESSION
        if not settings.UNLOAD_UNUSED_TOOLS_POST_COMPRESSION:  # default env: feature ships OFF
            assert PoolSettings().unload_unused_tools_post_compression is False


class TestWiringSeams:
    """Smoke test: the key is present in every Python + frontend seam that carries it."""

    KEY = 'unload_unused_tools_post_compression'

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

    def test_validate_registry_passes(self):
        """The hardcoded schema/whitelist drift-guard in settings_registry must accept the new
        key (count was bumped to 105). A miss here breaks collection of every settings test."""
        from agent_cascade.settings_registry import validate_registry

        validate_registry()

    def test_emitted_by_state_builder_snapshot(self):
        """pool_settings is built by ONE shared function (build_pool_settings_snapshot), called by
        both build_state_from_pool and build_stream_update_from_pool, so the two payload paths
        cannot drift. Assert the shared seam and that the key is really emitted."""
        src = Path('agent_cascade/api_integration_pkg/state_builder.py').read_text(encoding='utf-8')
        assert src.count('pool_settings = build_pool_settings_snapshot(pool)') >= 2, \
            f"{self.KEY}: both state_builder paths must share build_pool_settings_snapshot"

        from agent_cascade.agent_instance import PoolSettings
        from agent_cascade.api_integration_pkg.state_builder import build_pool_settings_snapshot

        class _StubPool:
            settings = PoolSettings()
            llm_cfg = {}

        snap = build_pool_settings_snapshot(_StubPool())
        assert self.KEY in snap, f"{self.KEY} not emitted by the registry-driven snapshot"
        # Must round-trip as a real bool (not dropped/None), matching the env-seeded default.
        from agent_cascade import settings
        assert isinstance(snap[self.KEY], bool)
        assert snap[self.KEY] is settings.UNLOAD_UNUSED_TOOLS_POST_COMPRESSION

    def test_frontend_seams_reference_key(self):
        """app.js must reference the key in POOL_SETTINGS_MAP, getGenerateCfg() and saveSettings()
        (3 sites; hydration is purely map-driven — no loadSettings() restore). index.html must
        carry the checkbox input id, default-off (no `checked` on that control)."""
        from agent_cascade.settings_registry import POOL_SETTINGS_KEYS

        app_js = Path('web_ui/app.js').read_text(encoding='utf-8')
        index_html = Path('web_ui/index.html').read_text(encoding='utf-8')

        assert self.KEY in POOL_SETTINGS_KEYS, f"{self.KEY} must be in the registry (POOL_SETTINGS_KEYS)"
        assert "id: '#setting-unload-unused-mcp'" in app_js, \
            f"{self.KEY}: missing POOL_SETTINGS_MAP entry for #setting-unload-unused-mcp"
        assert f"cfg.{self.KEY} = $('#setting-unload-unused-mcp').checked;" in app_js, \
            f"{self.KEY}: missing getGenerateCfg() send"
        assert f"s['{self.KEY}'] = $('#setting-unload-unused-mcp').checked;" in app_js, \
            f"{self.KEY}: missing saveSettings() localStorage mirror"

        dom_id = 'setting-unload-unused-mcp'
        assert f'id="{dom_id}"' in index_html, f"missing input id {dom_id} in index.html"
        # Default OFF: the control must NOT ship with a `checked` attribute.
        assert f'id="{dom_id}" checked' not in index_html, \
            f"{dom_id} must default OFF (no `checked` attribute) to match the off-by-default knob"

    def test_consumer_seam_reads_pool_settings_live(self):
        """REGRESSION GUARD (cheap drift check; the authoritative behavioral coverage is in
        TestConsumerGateLiveRead below). The compression handler gate must read pool.settings LIVE
        — pool-safe so a pool-less handler falls back to the env constant — NOT the import-time
        module constant. Reverting to ``if not settings.UNLOAD_...`` makes the UI toggle a silent
        no-op until restart while all persistence/broadcast tests still pass."""
        src = Path('agent_cascade/compression/handler.py').read_text(encoding='utf-8')
        assert "getattr(getattr(self, 'pool', None), 'settings', None)" in src, \
            'consumer seam must live-read pool.settings (pool-safe) so the UI toggle applies without restart'
        assert "getattr(_pool_settings, 'unload_unused_tools_post_compression'" in src, \
            'consumer seam must read the new key off pool.settings (with env fallback), not env-only'
        # The bare module-constant gate (no pool read) must be gone.
        assert 'if not settings.UNLOAD_UNUSED_TOOLS_POST_COMPRESSION:' not in src, \
            'consumer seam reverted to the import-time constant — UI toggle would be a no-op until restart'


class TestHandler:
    """Unit test for _handle_unload_unused_tools_post_compression — live mutation without restart."""

    def test_handler_sets_true(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        fake_pool = SimpleNamespace(settings=SimpleNamespace(unload_unused_tools_post_compression=False))

        CONFIG_HANDLERS['unload_unused_tools_post_compression'](
            {'unload_unused_tools_post_compression': True}, fake_pool, [])
        assert fake_pool.settings.unload_unused_tools_post_compression is True

    def test_handler_sets_false(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        fake_pool = SimpleNamespace(settings=SimpleNamespace(unload_unused_tools_post_compression=True))

        CONFIG_HANDLERS['unload_unused_tools_post_compression'](
            {'unload_unused_tools_post_compression': False}, fake_pool, [])
        assert fake_pool.settings.unload_unused_tools_post_compression is False

    def test_handler_noop_without_settings(self):
        """A pool object without .settings must be skipped cleanly (no AttributeError)."""
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        bare_pool = SimpleNamespace()  # no .settings attribute at all
        CONFIG_HANDLERS['unload_unused_tools_post_compression'](
            {'unload_unused_tools_post_compression': False}, bare_pool, [])
        assert not hasattr(bare_pool, 'settings')

    def test_handler_none_pool_is_noop(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        # Must not raise when agent_pool is None (ws_handlers passes it through as-is).
        CONFIG_HANDLERS['unload_unused_tools_post_compression'](
            {'unload_unused_tools_post_compression': False}, None, [])

    def test_router_apply_live_mutation(self):
        """ConfigUpdateRouter.apply() mutates the live pool.settings without restart —
        the hot-reload guarantee."""
        import asyncio
        from agent_cascade.config_handlers import ConfigUpdateRouter

        fake_pool = SimpleNamespace(settings=SimpleNamespace(unload_unused_tools_post_compression=False))
        router = ConfigUpdateRouter(fake_pool, [])

        asyncio.new_event_loop().run_until_complete(
            router.apply({'unload_unused_tools_post_compression': True}))
        assert fake_pool.settings.unload_unused_tools_post_compression is True


class TestConsumerGateLiveRead:
    """Behavioral coverage of the compression-handler consumer seam (the no-op-until-restart guard).

    Drives ``_maybe_unload_unused_mcp_tools`` directly, replacing the gated body with a recorder so
    only the gate decision is observed. Proves the gate reads pool.settings LIVE (UI override wins
    over env in BOTH directions) and falls back to the env constant for pool-less doubles — the
    object.__new__ case that a source-text-only check cannot catch (this exact crash broke
    tests/test_mcp_tool_unload.py before the pool-safe getattr fix).
    """

    KEY = 'unload_unused_tools_post_compression'

    def _patched_handler_class(self, monkeypatch, calls):
        from agent_cascade.compression.handler import CompressionHandler
        # Replace the real unload body with a recorder; we observe only whether the gate let it run.
        monkeypatch.setattr(
            CompressionHandler, '_unload_unused_mcp_tools',
            lambda self, *a, **k: calls.append(a))
        return CompressionHandler

    def _run_wrapper(self, calls, monkeypatch, pool_settings=None):
        """Drive the wrapper for the given pool.settings value; return the recorder list so the
        caller can assert how many times the gated body ran. pool_settings=None => pool-less."""
        handler_cls = self._patched_handler_class(monkeypatch, calls)
        h = object.__new__(handler_cls)
        if pool_settings is not None:
            h.pool = SimpleNamespace(settings=SimpleNamespace(**{self.KEY: pool_settings}))
        h._maybe_unload_unused_mcp_tools(SimpleNamespace(instance_name='x'), 'x', 'summary')
        return calls

    def test_pool_settings_on_overrides_env_off(self, monkeypatch):
        import agent_cascade.settings as settings
        monkeypatch.setattr(settings, 'UNLOAD_UNUSED_TOOLS_POST_COMPRESSION', False)
        calls = []
        self._run_wrapper(calls, monkeypatch, pool_settings=True)
        assert len(calls) == 1, 'live pool.settings=True must unload even when env is OFF'

    def test_pool_settings_off_overrides_env_on(self, monkeypatch):
        import agent_cascade.settings as settings
        monkeypatch.setattr(settings, 'UNLOAD_UNUSED_TOOLS_POST_COMPRESSION', True)
        calls = []
        self._run_wrapper(calls, monkeypatch, pool_settings=False)
        assert len(calls) == 0, 'live pool.settings=False must skip even when env is ON'

    def test_poolless_falls_back_to_env_constant(self, monkeypatch):
        """Regression guard for the self.pool AttributeError: a pool-less handler (no .pool) must
        fall back cleanly to the env constant, not raise AttributeError."""
        import agent_cascade.settings as settings
        monkeypatch.setattr(settings, 'UNLOAD_UNUSED_TOOLS_POST_COMPRESSION', True)
        calls = []
        handler_cls = self._patched_handler_class(monkeypatch, calls)
        h = object.__new__(handler_cls)  # deliberately no .pool attribute
        assert not hasattr(h, 'pool')
        h._maybe_unload_unused_mcp_tools(SimpleNamespace(instance_name='x'), 'x', 'summary')
        assert len(calls) == 1, 'pool-less handler must fall back to env=True and run, not raise'
