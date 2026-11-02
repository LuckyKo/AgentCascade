"""Backend-authoritative settings delta pipeline tests (Batch B, Phase 3).

Covers the ``update_settings`` / ``PATCH /api/settings`` pipeline:
  * only changed keys are applied and persisted;
  * exactly ONE disk write per batch (per touched bucket);
  * unknown key / type / enum violations are rejected per-key (batch not poisoned);
  * the shared settings_revision increments and the broadcast frames are emitted;
  * a stale base_revision does NOT clobber untouched keys (last-write-wins, no modal).

The pipeline is exercised at the unit level (mock pool, real ConfigUpdateRouter) and at the
REST level (TestClient against a real create_app).
"""
import asyncio
import threading
from pathlib import Path

import pytest

# Ensure top-level imports work.
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
import sys
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.agent_instance import PoolSettings
from agent_cascade.runtime_state import state as _runtime_state


class _MockRouter:
    """Minimal api_router stand-in: records update_default_llm_cfg calls."""

    def __init__(self):
        self.default_llm_cfg = {'model': 'initial-model', 'api_base': 'http://x', 'api_key': 'sk-0'}
        self.llm_updates = []

    def update_default_llm_cfg(self, cfg):
        self.default_llm_cfg.update(cfg)
        self.llm_updates.append(dict(cfg))


class _MockPool:
    """Pool stand-in for the delta pipeline: real PoolSettings, recording persist calls."""

    def __init__(self, tmp_path):
        self.settings = PoolSettings()
        self.instances = {}
        self.templates = {}
        self.telemetry = None
        self.stopped = False
        self.llm_cfg = {'tool_result_max_chars': 25000}
        self.api_router = _MockRouter()
        self._ui_disabled_tools = {}
        self._ui_disabled_tools_lock = threading.Lock()
        self.operation_manager = None
        self._enable_async_shell_console_window = False
        self.auto_security = False
        self._pool_settings_path = tmp_path / 'pool_settings.json'
        self._settings_save_lock = threading.Lock()
        self.save_calls = 0

    def _save_pool_settings(self):
        self.save_calls += 1
        _runtime_state.bump_settings_revision()

    def set_auto_security(self, enabled):
        self.auto_security = bool(enabled)

    def get_instance(self, name):
        return None

    def has_messages(self, name):
        return False

    def get_queue_messages(self, name):
        return []

    def has_pending(self, name):
        return False

    def is_paused(self):
        return False

    def is_instance_halted(self, name):
        return False


def _run(coro):
    return asyncio.run(coro)


# ── Unit: the apply_settings_changes pipeline ─────────────────────────────────

def test_only_changed_keys_applied_and_persisted_once(tmp_path):
    from agent_cascade.settings_delta import apply_settings_changes

    pool = _MockPool(tmp_path)
    base_rev = _runtime_state.settings_revision

    applied, rejected, changes_out, rev = _run(
        apply_settings_changes(pool, [], {'max_turns': 42, 'model': 'gpt-new'}))

    # Both accepted (one pool key, one llm key); no rejections.
    assert applied == ['max_turns', 'model']
    assert rejected == {}
    assert changes_out == {'max_turns': 42, 'model': 'gpt-new'}
    # Pool bucket persisted exactly once; llm persisted via the router (no extra pool save).
    assert pool.save_calls == 1
    assert pool.settings.max_turns == 42
    assert pool.api_router.default_llm_cfg['model'] == 'gpt-new'
    # Revision advanced past the base.
    assert rev > base_rev
    assert rev == _runtime_state.settings_revision


def test_unknown_key_rejected_per_key(tmp_path):
    from agent_cascade.settings_delta import apply_settings_changes

    pool = _MockPool(tmp_path)
    applied, rejected, changes_out, _rev = _run(
        apply_settings_changes(pool, [], {'max_turns': 10, 'totally_bogus_key': 1}))
    assert applied == ['max_turns']
    assert rejected == {'totally_bogus_key': 'unknown key'}
    # The bad key did not poison the good one, and no pool save was triggered by it.
    assert pool.save_calls == 1


def test_type_violation_rejected_per_key(tmp_path):
    from agent_cascade.settings_delta import apply_settings_changes

    pool = _MockPool(tmp_path)
    # max_turns expects a number; a string is a type mismatch.
    applied, rejected, _co, _rev = _run(
        apply_settings_changes(pool, [], {'max_turns': 'not-a-number', 'max_auto_rollbacks': 3}))
    assert applied == ['max_auto_rollbacks']
    assert 'max_turns' in rejected and 'type' in rejected['max_turns']


def test_empty_changes_no_persist_no_revision_bump(tmp_path):
    from agent_cascade.settings_delta import apply_settings_changes

    pool = _MockPool(tmp_path)
    base_rev = _runtime_state.settings_revision
    applied, rejected, _co, rev = _run(apply_settings_changes(pool, [], {}))
    assert applied == [] and rejected == {}
    assert pool.save_calls == 0
    assert rev == base_rev


def test_stale_base_revision_does_not_clobber_untouched_keys(tmp_path):
    from agent_cascade.settings_delta import apply_settings_changes

    pool = _MockPool(tmp_path)
    # First batch sets max_turns.
    _run(apply_settings_changes(pool, [], {'max_turns': 42}))
    assert pool.settings.max_turns == 42
    # Second batch touches a DIFFERENT key; the untouched key must survive.
    _run(apply_settings_changes(pool, [], {'max_auto_rollbacks': 7}))
    assert pool.settings.max_turns == 42
    assert pool.settings.max_auto_rollbacks == 7


def test_session_name_applies_and_persists(tmp_path, monkeypatch):
    import agent_cascade.session_state_store as ss
    from agent_cascade.settings_delta import apply_settings_changes

    monkeypatch.setattr(ss, '_session_state_path', lambda: tmp_path / 'session_state.json')
    pool = _MockPool(tmp_path)
    session = {'session_name': 'Maine'}
    applied, rejected, changes_out, _rev = _run(
        apply_settings_changes(pool, [], {'session_name': 'Rex'}, session=session))
    assert applied == ['session_name']
    assert session['session_name'] == 'Rex'
    assert changes_out['session_name'] == 'Rex'
    assert ss.load_session_name() == 'Rex'


# ── WS handler: broadcast frames + stale base_revision log ────────────────────

def test_ws_update_settings_broadcasts_applied_and_changed(tmp_path):
    from agent_cascade.ws_handlers import WsMessageHandler

    frames = []

    async def _broadcast(frame):
        frames.append(frame)

    pool = _MockPool(tmp_path)
    handler = WsMessageHandler(
        session={'session_name': 'Maine'}, agent_pool=pool, agents=[], send_queue=None,
        broadcast_fn=_broadcast, build_state_fn=lambda: {}, start_gen_fn=None,
        session_lock=threading.Lock(), app=None)

    _run(handler.handle_update_settings({'type': 'update_settings', 'base_revision': 0,
                                         'changes': {'max_turns': 33}}))
    types = [f['type'] for f in frames]
    assert 'settings_applied' in types
    assert 'settings_changed' in types
    applied_frame = next(f for f in frames if f['type'] == 'settings_applied')
    assert applied_frame['applied'] == ['max_turns']
    changed_frame = next(f for f in frames if f['type'] == 'settings_changed')
    assert changed_frame['changes'] == {'max_turns': 33}


def test_ws_update_settings_stale_revision_still_applies(tmp_path):
    from agent_cascade.ws_handlers import WsMessageHandler

    frames = []

    async def _broadcast(frame):
        frames.append(frame)

    pool = _MockPool(tmp_path)
    handler = WsMessageHandler(
        session={'session_name': 'Maine'}, agent_pool=pool, agents=[], send_queue=None,
        broadcast_fn=_broadcast, build_state_fn=lambda: {}, start_gen_fn=None,
        session_lock=threading.Lock(), app=None)

    # base_revision=0 is stale (the pool already bumped the revision via _MockPool saves).
    _run(handler.handle_update_settings({'type': 'update_settings', 'base_revision': 0,
                                         'changes': {'max_turns': 55}}))
    assert pool.settings.max_turns == 55
    applied_frame = next(f for f in frames if f['type'] == 'settings_applied')
    assert applied_frame['applied'] == ['max_turns']  # not rejected despite the stale revision


# ── REST: PATCH /api/settings ─────────────────────────────────────────────────

def test_patch_api_settings_applies_and_rejects(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_cascade.api_server import create_app

    # Point session_state at a temp dir so the REST path doesn't touch the real config.
    import agent_cascade.session_state_store as ss
    monkeypatch.setattr(ss, '_session_state_path', lambda: tmp_path / 'session_state.json')
    monkeypatch.setattr(_runtime_state, 'settings_revision', 0)

    pool = _MockPool(tmp_path)
    app = create_app(agents=[], agent_pool=pool, config={'session_name': 'TestSession'})
    client = TestClient(app)

    resp = client.patch('/api/settings', json={'base_revision': 0,
                                               'changes': {'max_turns': 21, 'bogus': 1}})
    assert resp.status_code == 200
    body = resp.json()
    assert body['applied'] == ['max_turns']
    assert 'bogus' in body['rejected']
    assert body['revision'] > 0
    assert pool.settings.max_turns == 21

class TestTypeValidationEdges:
    """`_type_ok` edge cases: numeric tuples, the bool-is-an-int trap, and collection types."""

    @pytest.mark.parametrize('key, value, expected', [
        ('max_turns', 250, True),            # py_type (int, float)
        ('max_turns', 2.5, True),
        ('max_turns', '250', False),
        ('max_turns', True, False),          # bool subclasses int but is not a number
        ('loop_two_phase_enabled', True, True),
        ('loop_two_phase_enabled', 1, False),  # a number is not a bool
        ('disabled_tools', {'orchestrator': []}, True),  # py_type (dict, list)
        ('disabled_tools', ['orchestrator'], True),
        ('disabled_tools', 'orchestrator', False),
        ('skill_always_protected', 'a,b', True),
        ('skill_always_protected', ['a'], False),
        ('totally_unknown_key', 1, True),    # unknown keys are rejected by the caller, not by _type_ok
    ])
    def test_type_ok_edges(self, key, value, expected):
        from agent_cascade.settings_delta import _type_ok
        assert _type_ok(key, value) is expected
