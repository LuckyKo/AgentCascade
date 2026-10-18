"""Tests for the manual Save/Load KV state buttons (todo #109, BUILD phase).

The KV save/restore machinery already exists (``agent_cascade/state_ops.py``); this
feature is a NEW user-initiated CALLER of it:

- ``state_ops.save_instance_state(..., set_label=False)`` — a manual save must NOT
  arm the automatic next-turn restore (the label is the arming signal).
- ``ws_handlers.WsMessageHandler.do_save_kv`` / ``do_load_kv`` — shared core logic
  for the WS 'kv_state' frame and the REST /api/state/save|load endpoints.
- ``do_load_kv`` refuses (``busy``) while ANY OTHER instance currently HOLDS the
  target endpoint, detected from real slot-ownership signals (``other._slot_release``
  and the router's acquire-time held map), never from agent STATE.

Mock style mirrors tests/test_state_restore_slot_gate.py (patch the state_ops HTTP
layer, MagicMock instances carrying ``_last_endpoint_config``) and tests/test_shell_cmd_safe_mode.py
(patch ``state_ops`` functions, assert call ARGUMENTS). Per the
reuse-mechanism-gated-on-default-off-toggle lesson, the global ``state_kv_save_enabled``
toggle is irrelevant to this path — it is ``false`` in the live config and the buttons
gate on per-endpoint CAPABILITY only.
"""

import asyncio
import threading
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

import agent_cascade.state_ops as state_ops
from agent_cascade.agent_instance import AgentState
from agent_cascade.api_router_pkg.normalization import normalize_api_base
from agent_cascade.ws_handlers import WsMessageHandler

# A KV-capable endpoint config (matches the live autoloader shape).
CAPABLE_EP = {
    'state_save_enabled': True,
    'api_base': 'http://127.0.0.1:1234/v1',
    'model': 'test-model',
}
# A second model on the SAME base — used to prove tuple comparison (F2).
OTHER_MODEL_EP = {
    'state_save_enabled': True,
    'api_base': 'http://127.0.0.1:1234/v1',
    'model': 'other-model',
}
NON_CAPABLE_EP = {
    'state_save_enabled': False,
    'api_base': 'http://127.0.0.1:1234/v1',
    'model': 'test-model',
}


def _map_key(endpoint_cfg):
    """Key the ROUTER writes into ``_instance_held_endpoint`` (producer side).

    Mirrors ``router.py:651``: ``api_router_pkg.normalization.normalize_api_base`` KEEPS
    ``/v1`` and rewrites ``localhost`` — it is NOT ``state_ops._normalize_api_base`` (which
    strips ``/v1`` and is only right for KV-file addressing). A test that forges the map
    with the consumer's normalizer is a wrong-shaped double: it passes against a guard that
    can never match in production.
    """
    return (normalize_api_base(endpoint_cfg['api_base']), endpoint_cfg['model'])


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _make_instance(name='agent-1', *, endpoint=CAPABLE_EP, holds_slot=False):
    """A stand-in AgentInstance carrying exactly the attributes the seam reads."""
    inst = MagicMock()
    inst.instance_name = name
    inst.agent_class = 'assistant'
    # Real AgentInstance uses threading.RLock; the save path re-acquires under an
    # outer lock, so a non-reentrant Lock would deadlock in real-save tests.
    inst._state_lock = threading.RLock()
    inst._state_label = None
    inst._slot_release = (lambda: None) if holds_slot else None
    inst._last_endpoint_config = endpoint
    return inst


def _make_pool(*, target, siblings=None, api_router=None):
    """A pool whose ``get_instance``/``instances`` are real (not MagicMock chains).

    ``siblings`` is a dict of name -> instance that ``do_load_kv`` iterates for the
    ownership guard. ``api_router`` is wired only for the router-held-map path.
    """
    pool = MagicMock()
    instances = {target.instance_name: target}
    if siblings:
        instances.update(siblings)
    pool.instances = instances
    pool.get_instance = lambda name: instances.get(str(name).strip())
    if api_router is not None:
        pool.api_router = api_router
    return pool


def _make_router(held_map=None):
    """A fake api_router exposing a real ``_instance_held_endpoint`` dict + ``_lock``."""
    router = MagicMock()
    router._instance_held_endpoint = dict(held_map or {})
    router._lock = threading.Lock()
    return router


# ────────────────────────────────────────────────────────────────────────────
# 4.1 — set_label kwarg on save_instance_state
# ────────────────────────────────────────────────────────────────────────────


class TestSetLabelKwarg:

    def test_default_sets_label(self):
        """save_instance_state(inst) (no kwarg) → _state_label set (regression guard)."""
        inst = _make_instance()
        with patch('agent_cascade.state_ops.save_state', return_value='agent-1'):
            ok = state_ops.save_instance_state(inst)
        assert ok is True
        with inst._state_lock:
            assert inst._state_label == 'agent-1'

    def test_set_label_false_issues_save_but_keeps_label_none(self):
        """set_label=False → a real save HTTP is issued, but _state_label stays None."""
        inst = _make_instance()
        with patch.object(state_ops.httpx, 'post') as post_mock:
            post_mock.return_value.status_code = 200
            ok = state_ops.save_instance_state(inst, set_label=False)
        assert ok is True
        save_urls = [c.args[0] for c in post_mock.call_args_list
                     if isinstance(c.args[0], str) and '/state/save' in c.args[0]]
        assert len(save_urls) == 1, 'a state/save request must actually be issued'
        with inst._state_lock:
            assert inst._state_label is None

    def test_non_capable_config_returns_false_both_modes(self):
        """Capability-false config → False with NO HTTP, in both label modes."""
        for set_label in (True, False):
            inst = _make_instance(endpoint=NON_CAPABLE_EP)
            with patch.object(state_ops.httpx, 'post') as post_mock:
                ok = state_ops.save_instance_state(inst, set_label=set_label)
            assert ok is False
            post_mock.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# 4.2 — do_save_kv / do_load_kv seam tests
# ────────────────────────────────────────────────────────────────────────────


class TestKvSeamGuards:

    def test_no_pool(self):
        result = WsMessageHandler.do_save_kv(None, 'agent-1')
        assert result['status'] == 'failed'
        assert result['reason'] == 'no_pool'
        assert result['action'] == 'save'
        assert result['instance_name'] == 'agent-1'

    def test_unknown_instance(self):
        pool = _make_pool(target=_make_instance(), siblings={})
        result = WsMessageHandler.do_save_kv(pool, 'ghost')
        assert result['status'] == 'failed'
        assert result['reason'] == 'no_instance'

    def test_not_capable_gate_is_capability_only(self):
        """Capability gate only: a non-capable config fails with ``not_capable``.

        This test pins the failure half of the gate — it never touches the global
        ``state_kv_save_enabled`` toggle, because this path does not read it. The
        production-value half (a capable config still firing with the toggle at its
        live ``False``) is ``test_capable_config_fires_despite_toggle_false``.
        """
        inst = _make_instance(endpoint=NON_CAPABLE_EP)
        pool = _make_pool(target=inst)
        result = WsMessageHandler.do_save_kv(pool, 'agent-1')
        assert result['status'] == 'failed'
        assert result['reason'] == 'not_capable'

    def test_capable_config_fires_despite_toggle_false(self):
        """Production-value check: state_kv_save_enabled is irrelevant to this path."""
        inst = _make_instance(endpoint=CAPABLE_EP)
        pool = _make_pool(target=inst)
        # Force the toggle to its live-config value; the manual save must still fire.
        pool.settings = MagicMock(state_kv_save_enabled=False)
        with patch('agent_cascade.state_ops.save_instance_state', return_value=True) as m_save:
            result = WsMessageHandler.do_save_kv(pool, 'agent-1')
        assert result['status'] == 'ok'
        m_save.assert_called_once()

    def test_save_uses_set_label_false(self):
        """Save path calls save_instance_state with set_label=False (argument assert)."""
        inst = _make_instance(endpoint=CAPABLE_EP)
        pool = _make_pool(target=inst)
        with patch('agent_cascade.state_ops.save_instance_state', return_value=True) as m_save:
            WsMessageHandler.do_save_kv(pool, 'agent-1')
        m_save.assert_called_once()
        args, kwargs = m_save.call_args
        assert kwargs.get('set_label') is False, \
            f"manual save must pass set_label=False: {args}, {kwargs}"

    def test_load_uses_stable_label(self):
        """Load path calls restore_held_slot_state with label=instance_name."""
        inst = _make_instance(endpoint=CAPABLE_EP)
        pool = _make_pool(target=inst)
        with patch('agent_cascade.state_ops.restore_held_slot_state', return_value=True) as m_restore:
            WsMessageHandler.do_load_kv(pool, 'agent-1')
        m_restore.assert_called_once()
        args, kwargs = m_restore.call_args
        assert kwargs.get('label') == 'agent-1', \
            f"manual load must pass stable label: {args}, {kwargs}"


class TestLoadOwnershipGuard:

    def test_sibling_holds_slot_busy(self):
        """(7a) Sibling holding the SAME endpoint → busy, NO restore call."""
        target = _make_instance('agent-A', endpoint=CAPABLE_EP)
        sibling = _make_instance('agent-B', endpoint=CAPABLE_EP, holds_slot=True)
        pool = _make_pool(target=target, siblings={'agent-B': sibling})
        with patch('agent_cascade.state_ops.restore_held_slot_state') as m_restore:
            result = WsMessageHandler.do_load_kv(pool, 'agent-A')
        assert result['status'] == 'failed'
        assert result['reason'] == 'busy'
        m_restore.assert_not_called()

    def test_router_held_map_busy(self):
        """(7b) Sibling _slot_release None but a router held-map entry → busy.

        The map value is built with the ROUTER's normalizer (see _map_key) — mirroring
        what writes it, not what reads it.
        """
        target = _make_instance('agent-A', endpoint=CAPABLE_EP)
        sibling = _make_instance('agent-B', endpoint=CAPABLE_EP, holds_slot=False)
        router = _make_router(held_map={'agent-B': _map_key(CAPABLE_EP)})
        pool = _make_pool(target=target, siblings={'agent-B': sibling}, api_router=router)
        with patch('agent_cascade.state_ops.restore_held_slot_state') as m_restore:
            result = WsMessageHandler.do_load_kv(pool, 'agent-A')
        assert result['status'] == 'failed'
        assert result['reason'] == 'busy'
        m_restore.assert_not_called()

    def test_transition_lag_control_proceeds(self):
        """(7c) Sibling _slot_release None, no map entry, but state RUNNING → PROCEEDS.

        The rejected state-proxy design would false-refuse here; the ownership guard
        must not (E8b: a released permit can coexist with a RUNNING state).
        """
        target = _make_instance('agent-A', endpoint=CAPABLE_EP)
        sibling = _make_instance('agent-B', endpoint=CAPABLE_EP, holds_slot=False)
        sibling.state = AgentState.RUNNING  # agent STATE must NOT be the discriminator
        pool = _make_pool(target=target, siblings={'agent-B': sibling})
        with patch('agent_cascade.state_ops.restore_held_slot_state', return_value=True) as m_restore:
            result = WsMessageHandler.do_load_kv(pool, 'agent-A')
        assert result['status'] == 'ok'
        m_restore.assert_called_once()

    def test_different_model_control_proceeds(self):
        """(7d) Sibling holding a DIFFERENT model on the same base → PROCEEDS (tuple)."""
        target = _make_instance('agent-A', endpoint=CAPABLE_EP)
        sibling = _make_instance('agent-B', endpoint=OTHER_MODEL_EP, holds_slot=True)
        pool = _make_pool(target=target, siblings={'agent-B': sibling})
        with patch('agent_cascade.state_ops.restore_held_slot_state', return_value=True) as m_restore:
            result = WsMessageHandler.do_load_kv(pool, 'agent-A')
        assert result['status'] == 'ok'
        m_restore.assert_called_once()

    def test_held_map_only_for_unregistered_name_busy(self):
        """(7f) Review finding 7: the router map is the ONLY signal for a name that
        is not in ``pool.instances`` (the real orphan class: dismissed/crashed
        instances popped from pool.instances by remove_instance whose slot release
        at router.py:962 has not run yet — sub-agents DO appear in pool.instances).

        Graph shape matters: ``do_load_kv`` reads ``getattr(agent_pool, 'api_router',
        None)`` — the router hangs off the POOL, so the double must wire it there,
        not anywhere else. Witness is observable: the refusal message must name the
        sibling that only the held map knows about, and no restore may be attempted.
        """
        target = _make_instance('agent-A', endpoint=CAPABLE_EP)
        router = _make_router(held_map={'agent-B': _map_key(CAPABLE_EP)})
        # NOTE: no sibling in pool.instances — 'agent-B' exists ONLY in the held map.
        pool = _make_pool(target=target, api_router=router)
        with patch('agent_cascade.state_ops.restore_held_slot_state', return_value=True) as m_restore:
            result = WsMessageHandler.do_load_kv(pool, 'agent-A')
        assert result['status'] == 'failed'
        assert result['reason'] == 'busy'
        assert 'agent-B' in result['message'], \
            'the refusal must come from the held-map entry, not a pool sibling'
        m_restore.assert_not_called()

    def test_normalization_trailing_v1_busy(self):
        """(7e) target /v1 vs sibling bare → same normalized key → busy.

        Proves _normalize_api_base is used, not rstrip('/').
        """
        target = _make_instance('agent-A', endpoint={
            'state_save_enabled': True, 'api_base': 'http://127.0.0.1:1234/v1', 'model': 'test-model'})
        sibling = _make_instance('agent-B', endpoint={
            'state_save_enabled': True, 'api_base': 'http://127.0.0.1:1234', 'model': 'test-model'},
            holds_slot=True)
        pool = _make_pool(target=target, siblings={'agent-B': sibling})
        with patch('agent_cascade.state_ops.restore_held_slot_state') as m_restore:
            result = WsMessageHandler.do_load_kv(pool, 'agent-A')
        assert result['status'] == 'failed'
        assert result['reason'] == 'busy'
        m_restore.assert_not_called()


class TestKvFailureMapping:

    def test_save_while_target_running_proceeds(self):
        """(8) E8d: target instance RUNNING with a resident model → save proceeds."""
        target = _make_instance('agent-A', endpoint=CAPABLE_EP, holds_slot=True)
        pool = _make_pool(target=target)
        with patch('agent_cascade.state_ops.save_instance_state', return_value=True) as m_save:
            result = WsMessageHandler.do_save_kv(pool, 'agent-A')
        assert result['status'] == 'ok'
        m_save.assert_called_once()

    def test_save_false_maps_to_save_failed(self):
        """(9) patched save returns False → save_failed result dict."""
        target = _make_instance('agent-A', endpoint=CAPABLE_EP)
        pool = _make_pool(target=target)
        with patch('agent_cascade.state_ops.save_instance_state', return_value=False):
            result = WsMessageHandler.do_save_kv(pool, 'agent-A')
        assert result['status'] == 'failed'
        assert result['reason'] == 'save_failed'

    def test_load_false_maps_to_load_failed(self):
        """(9) patched restore returns False → load_failed result dict."""
        target = _make_instance('agent-A', endpoint=CAPABLE_EP)
        pool = _make_pool(target=target)
        with patch('agent_cascade.state_ops.restore_held_slot_state', return_value=False):
            result = WsMessageHandler.do_load_kv(pool, 'agent-A')
        assert result['status'] == 'failed'
        assert result['reason'] == 'load_failed'

    def test_helper_never_raises_when_pool_raises(self):
        """Helper never raises even when the pool's get_instance raises."""
        pool = MagicMock()
        pool.get_instance = MagicMock(side_effect=RuntimeError('boom'))
        result = WsMessageHandler.do_save_kv(pool, 'agent-1')
        assert result['status'] == 'failed'
        assert result['reason'] == 'no_instance'


# ────────────────────────────────────────────────────────────────────────────
# 4.3 — effect-level test: patches ONLY the HTTP transport, drives a real label
# ────────────────────────────────────────────────────────────────────────────


class TestEffectLevelLabelVsArtifact:
    """The seam tests patch the helpers, so they assert call ORDER, not EFFECT.

    This test patches only ``state_ops.save_state`` / ``restore_state`` (the HTTP
    layer) and drives a REAL instance through manual save → automatic consumer clears
    the label → manual load, asserting a load request is ACTUALLY issued after the
    automatic consumer cleared ``_state_label``. This is the test that catches the
    label-vs-artifact confusion the audit skill warns about.
    """

    def _record(self, post_mock, path):
        return [c.args[0] for c in post_mock.call_args_list
                if isinstance(c.args[0], str) and path in c.args[0]]

    def test_manual_load_issued_after_label_consumed(self):
        target = _make_instance('agent-1', endpoint=CAPABLE_EP)
        pool = _make_pool(target=target)
        norm = state_ops._normalize_api_base(CAPABLE_EP['api_base'])

        with patch.object(state_ops.httpx, 'post') as post_mock:
            post_mock.return_value.status_code = 200

            # 1. Manual save: set_label=False → file written, _state_label stays None.
            ok = WsMessageHandler.do_save_kv(pool, 'agent-1')
            assert ok['status'] == 'ok'
            assert len(self._record(post_mock, '/state/save')) == 1
            with target._state_lock:
                assert target._state_label is None

            # 2. An automatic caller sets the label and then CONSUMES it (clears it),
            #    modeling the engine's next-turn restore already having run.
            with target._state_lock:
                target._state_label = 'agent-1'
            with target._state_lock:
                target._state_label = None
            assert target._state_label is None

            # 3. Manual load by the STABLE label → a state/load request IS issued,
            #    even though _state_label is None (the old consume-once path no-ops).
            #    Wire the router so resolve_held_endpoint resolves to the SAME endpoint
            #    the save wrote to (the normal single-endpoint case, §2a).
            router = _make_router()
            router.get_effective_slot_info.return_value = {
                'api_base': CAPABLE_EP['api_base'], 'needs_slot': True}
            router.get_endpoint_chain.return_value = [{'model': CAPABLE_EP['model']}]
            pool.api_router = router
            res = WsMessageHandler.do_load_kv(pool, 'agent-1')

        loads = self._record(post_mock, '/state/load')
        assert len(loads) == 1, \
            'a state/load must be issued despite _state_label being None'
        assert norm in loads[0]
        assert res['status'] == 'ok'


# ────────────────────────────────────────────────────────────────────────────
# 4.5 — WS dispatch + frame-relay tests
# ────────────────────────────────────────────────────────────────────────────


def _make_handler():
    """A WsMessageHandler with a broadcast_fn that records frames."""
    frames = []

    async def _broadcast(frame):
        frames.append(frame)

    session = {'session_name': 'agent-1', 'generating': False, 'generation_id': 0}
    handler = WsMessageHandler(
        session=session,
        agent_pool=None,
        agents=[],
        send_queue=None,
        broadcast_fn=_broadcast,
        build_state_fn=lambda **kw: {},
        start_gen_fn=lambda: None,
        session_lock=threading.Lock(),
        app=None,
    )
    return handler, frames


class TestWsDispatch:

    def test_routes_to_handler_and_emits_result(self):
        """dispatch({'type':'kv_state',...}) routes to handle_kv_state, emits kv_state_result."""
        target = _make_instance('agent-1', endpoint=CAPABLE_EP)
        pool = _make_pool(target=target)
        handler, frames = _make_handler()
        handler.agent_pool = pool

        with patch('agent_cascade.state_ops.save_instance_state', return_value=True):
            asyncio.run(handler.dispatch({'type': 'kv_state', 'action': 'save',
                                         'instance_name': 'agent-1'}))

        assert any(f.get('type') == 'kv_state_result' for f in frames)
        result = next(f for f in frames if f.get('type') == 'kv_state_result')
        assert result['status'] == 'ok'
        assert result['action'] == 'save'
        assert result['instance_name'] == 'agent-1'
        # The generating session flag must NOT be flipped by a manual KV op.
        assert handler.session['generating'] is False

    def test_bad_action_emits_bad_action_frame(self):
        """Unknown action → bad_action failure frame, all four fields present."""
        handler, frames = _make_handler()
        asyncio.run(handler.dispatch({'type': 'kv_state', 'action': 'frobnicate',
                                     'instance_name': 'agent-1'}))
        assert any(f.get('type') == 'kv_state_result' for f in frames)
        result = next(f for f in frames if f.get('type') == 'kv_state_result')
        assert result['status'] == 'failed'
        assert result['reason'] == 'bad_action'
        for key in ('status', 'action', 'instance_name', 'message'):
            assert key in result

    def test_frame_relay_shape_matches_frontend_contract(self):
        """(15) The frame broadcast_fn receives carries exactly the keys the frontend reads."""
        target = _make_instance('agent-1', endpoint=NON_CAPABLE_EP)
        pool = _make_pool(target=target)
        handler, frames = _make_handler()
        handler.agent_pool = pool

        asyncio.run(handler.dispatch({'type': 'kv_state', 'action': 'load',
                                     'instance_name': 'agent-1'}))
        assert any(f.get('type') == 'kv_state_result' for f in frames)
        result = next(f for f in frames if f.get('type') == 'kv_state_result')
        # Frontend case 'kv_state_result' reads: status, action, instance_name, message.
        for key in ('status', 'action', 'instance_name', 'message'):
            assert key in result, f"frame-relay contract: {key} must be present on failure paths"


# ────────────────────────────────────────────────────────────────────────────
# 19 — KV button label single source of truth (app.js must not hard-code
#      restore labels; index.html owns them, app.js captures live textContent)
# ────────────────────────────────────────────────────────────────────────────


class TestKvLabelSingleSource:

    def test_app_js_captures_live_label_and_restores_from_dataset(self):
        """Snapshot: sendKvState captures btn.dataset.kvLabel; BOTH restore paths read it."""
        app_js = (Path(__file__).resolve().parent.parent / 'web_ui' / 'app.js').read_text(
            encoding='utf-8')
        # Capture before overwrite — the live label is the source, not a literal.
        assert 'btn.dataset.kvLabel = btn.textContent;' in app_js
        # Result-frame restore path and safety-timeout restore path both read the dataset.
        assert 'saveKvBtn.textContent = saveKvBtn.dataset.kvLabel;' in app_js
        assert 'loadKvBtn.textContent = loadKvBtn.dataset.kvLabel;' in app_js
        assert 'btn.textContent = btn.dataset.kvLabel;' in app_js

    def test_app_js_has_no_hardcoded_kv_restore_labels(self):
        """The label strings live ONLY in index.html; app.js must not restate them."""
        app_js = (Path(__file__).resolve().parent.parent / 'web_ui' / 'app.js').read_text(
            encoding='utf-8')
        for hardcoded in ('💾 Save KV', '📂 Load KV'):
            assert hardcoded not in app_js, (
                f"app.js must not hard-code {hardcoded!r} — restore paths read dataset.kvLabel")
