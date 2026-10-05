"""Wiring + handler + gate tests for the ``state_kv_save_enabled`` UI toggle.

The toggle (default ON = current behavior) gates the KV state save/restore pair that
wraps every "caller yields its endpoint slot to a system agent" site (Security Guard /
Compressor / sync child). The gate lives inside the two shared engine helpers
(``ExecutionEngine.save_before_slot_yield`` / ``reacquire_after_slot_yield``) — the single
funnel for all three yield sites. When OFF:

- ``save_before_slot_yield`` returns False WITHOUT touching state_ops (no ``state/save``);
- ``reacquire_after_slot_yield`` STILL re-acquires the slot (never conditional) but skips
  the KV restore (no ``state/load``) and returns True.

This file mirrors tests/test_allow_parallel_agents_config.py: wiring seams across every
Python + frontend touch point, a handler unit test, plus gate-behavior tests against the
real engine helpers.

TRAP (same as allow_parallel): the handler guard is ``hasattr(agent_pool, 'settings')`` —
a bare MagicMock pool would auto-create ``settings``, making the guard vacuous. The fake
pool here is a plain object whose absence of ``settings`` is asserted explicitly.
"""

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_cascade.state_ops as state_ops
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import Message

AUTOLOADER_X = 'http://localhost:1234/v1/'  # the shared conc=0 autoloader (held)


# ============================================================================
# Helpers
# ============================================================================


def make_instance(name='B', label='B'):
    """Real AgentInstance with a saved state label on an autoloader endpoint."""
    inst = AgentInstance(
        instance_name=name,
        agent_class='coder',
        conversation=[Message(role='system', content='sys')],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=0,
        parent_instance='Main',
    )
    with inst._state_lock:
        inst._state_label = label
        inst._last_endpoint_config = {
            'api_base': AUTOLOADER_X,
            'model': 'model-A',
            'state_save_enabled': True,
        }
    return inst


def make_engine(instance, state_kv_save_enabled=None):
    """ExecutionEngine over a MagicMock pool.

    The real engine's ``_get_pool_setting`` reads ``getattr(pool.settings, name, default)``;
    a bare MagicMock auto-creates the attribute chain and returns a truthy MagicMock for
    ANY setting — which would defeat the gate under test. So we attach a REAL settings
    object (or none at all, to exercise the True-default path).
    """
    pool = SimpleNamespace()
    if state_kv_save_enabled is not None:
        pool.settings = SimpleNamespace(state_kv_save_enabled=state_kv_save_enabled)
    engine = ExecutionEngine(pool)

    # _restore_held_slot_state resolves the held endpoint via pool.api_router; wire it
    # so the ON-path restore test can assert the load URL targets the held autoloader.
    router = SimpleNamespace()
    router.get_effective_slot_info = lambda *a, **kw: {
        'slot_key': 'pool-x', 'api_base': AUTOLOADER_X, 'needs_slot': True}
    router.get_endpoint_chain = lambda *a, **kw: [
        {'api_base': AUTOLOADER_X, 'model': 'model-A', 'state_save_enabled': True}]
    pool.api_router = router
    return engine


def _recorded_state_urls(post_mock):
    """Extract the /state/save and /state/load URLs from recorded httpx.post calls."""
    urls = []
    for call in post_mock.call_args_list:
        url = call.args[0] if call.args else call.kwargs.get('url', '')
        if isinstance(url, str) and ('/state/save' in url or '/state/load' in url):
            urls.append(url)
    return urls


# ============================================================================
# PoolSettings field
# ============================================================================


class TestPoolSettingsField:
    """The PoolSettings field exists with the correct default (default ON = today's behavior)."""

    def test_default_is_true(self):
        from agent_cascade.agent_instance import PoolSettings

        assert PoolSettings().state_kv_save_enabled is True


# ============================================================================
# Wiring seams
# ============================================================================


class TestWiringSeams:
    """Smoke test: the key is present in every Python + frontend seam that carries it."""

    KEY = 'state_kv_save_enabled'

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
        assert "id: '#setting-state-kv-save'" in app_js, \
            f"{self.KEY}: missing POOL_SETTINGS_MAP entry for #setting-state-kv-save"

        dom_id = 'setting-state-kv-save'
        assert f'id="{dom_id}"' in index_html, f"missing input id {dom_id} in index.html"


# ============================================================================
# Handler
# ============================================================================


class TestHandler:
    """Unit test for _handle_state_kv_save_enabled — live mutation without restart."""

    def test_handler_sets_false(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        # Plain-object double: a real AgentPool always has .settings, but the handler's
        # hasattr guard must be tested against a pool that genuinely LACKS it (a bare
        # MagicMock would auto-create it and make the guard vacuous).
        fake_pool = SimpleNamespace(settings=SimpleNamespace(state_kv_save_enabled=True))

        CONFIG_HANDLERS['state_kv_save_enabled']({'state_kv_save_enabled': False}, fake_pool, [])
        assert fake_pool.settings.state_kv_save_enabled is False

    def test_handler_sets_true(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        fake_pool = SimpleNamespace(settings=SimpleNamespace(state_kv_save_enabled=False))

        CONFIG_HANDLERS['state_kv_save_enabled']({'state_kv_save_enabled': True}, fake_pool, [])
        assert fake_pool.settings.state_kv_save_enabled is True

    def test_handler_noop_without_settings(self):
        """A pool object without .settings must be skipped cleanly (no AttributeError)."""
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        bare_pool = SimpleNamespace()  # no .settings attribute at all

        CONFIG_HANDLERS['state_kv_save_enabled']({'state_kv_save_enabled': False}, bare_pool, [])
        assert not hasattr(bare_pool, 'settings')

    def test_handler_none_pool_is_noop(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        # Must not raise when agent_pool is None (ws_handlers passes it through as-is).
        CONFIG_HANDLERS['state_kv_save_enabled']({'state_kv_save_enabled': False}, None, [])


# ============================================================================
# Gate behavior — engine helpers
# ============================================================================


class TestSaveGate:
    """save_before_slot_yield honors the toggle: OFF = no state_ops call at all."""

    def test_off_skips_save_without_state_ops(self):
        inst = make_instance()
        engine = make_engine(inst, state_kv_save_enabled=False)

        with patch.object(state_ops.httpx, 'post') as post_mock:
            result = engine.save_before_slot_yield(inst, inst.instance_name, 'before_security_check')

        assert result is False
        assert _recorded_state_urls(post_mock) == [], \
            'no state/save may fire when the toggle is OFF'
        with inst._state_lock:
            assert inst._state_label == 'B', 'label must be untouched when save is skipped'

    def test_on_saves_by_default(self):
        """Default ON (no settings object at all) preserves today's behavior: a real save fires."""
        inst = make_instance()
        engine = make_engine(inst, state_kv_save_enabled=None)  # no .settings → True default

        with patch.object(state_ops.httpx, 'post') as post_mock:
            post_mock.return_value.status_code = 200
            result = engine.save_before_slot_yield(inst, inst.instance_name, 'before_compression')

        assert result is True
        saves = [u for u in _recorded_state_urls(post_mock) if '/state/save' in u]
        assert len(saves) == 1, f"exactly one state/save expected: {saves}"


class TestReacquireGate:
    """reacquire_after_slot_yield: slot re-acquire is NEVER conditional; only the KV restore
    is skipped when the toggle is OFF."""

    def test_off_still_reacquires_but_skips_restore(self):
        inst = make_instance()  # label='B' pending — would normally trigger a restore
        engine = make_engine(inst, state_kv_save_enabled=False)

        def fake_reacquire(instance_arg, holder_name, context='reacquire'):
            instance_arg._slot_release = lambda: None
            instance_arg._slot_key = 'pool-x'
            return True

        with patch.object(state_ops.httpx, 'post') as post_mock:
            with patch.object(engine, 'reacquire_for', side_effect=fake_reacquire) as reacq:
                ok = engine.reacquire_after_slot_yield(inst, inst.instance_name, 'after_security_check')

        assert ok is True
        reacq.assert_called_once()  # slot re-acquire still happened — never conditional
        assert _recorded_state_urls(post_mock) == [], \
            'no state/load may fire when the toggle is OFF'
        with inst._state_lock:
            assert inst._state_label is None, \
                'stale label must be cleared on the OFF skip path (save occurred while ON)'

    def test_on_restores_to_held_endpoint(self):
        """Default ON: existing restore-to-held-endpoint behavior still holds."""
        inst = make_instance()  # label pending, autoloader endpoint config cached
        engine = make_engine(inst, state_kv_save_enabled=None)  # no .settings → True default

        def fake_reacquire(instance_arg, holder_name, context='reacquire'):
            instance_arg._slot_release = lambda: None
            instance_arg._slot_key = 'pool-x'
            return True

        with patch.object(state_ops.httpx, 'post') as post_mock:
            post_mock.return_value.status_code = 200
            with patch.object(engine, 'reacquire_for', side_effect=fake_reacquire):
                ok = engine.reacquire_after_slot_yield(inst, inst.instance_name, 'sync child')

        assert ok is True
        loads = [u for u in _recorded_state_urls(post_mock) if '/state/load' in u]
        assert len(loads) == 1, f"exactly one state/load expected: {loads}"
        assert AUTOLOADER_X.rstrip('/') in loads[0], f"restore must target the held endpoint: {loads[0]}"


class TestGeneralSaveGateUnchanged:
    """Regression: tool_dispatcher.py's general pre-delegation save gate is untouched — it
    still reads the same pool setting with the True default."""

    def test_guard_line_still_reads_setting(self):
        src = Path('agent_cascade/tool_dispatcher.py').read_text(encoding='utf-8')
        assert "_get_pool_setting('state_kv_save_enabled', True)" in src, \
            'tool_dispatcher general-save gate must still read state_kv_save_enabled (default True)'
