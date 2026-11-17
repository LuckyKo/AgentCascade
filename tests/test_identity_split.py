"""BUG_0323 S1: birth-stamped root identity vs. user-facing display name.

Covers the slice contract:
  * flag OFF keeps plain root identities (byte-identical legacy behavior),
  * flag ON stamps roots, leaves sub-agents unstamped,
  * restores reuse the persisted stamp (never mint a new one),
  * session listings show display names,
  * two roots sharing one endpoint/model get distinct KV labels.
"""

import json
import threading
from types import SimpleNamespace

import pytest

from agent_cascade.agent_identity import strip_birth_stamp
from agent_cascade.pool.lifecycle import LifecycleMixin
from tests.test_session_restore_pool_ref import _FakeInstanceLogger, _PoolDouble


class _LifecyclePool(LifecycleMixin):
    """Just enough pool surface for the real create_instance path."""

    def __init__(self):
        self.instances = {}
        self._instances_version = 0
        self._pool_lock = threading.RLock()
        self.children = {}
        self._children_lock = threading.RLock()

    def _update_child_relationship(self, parent, child, add=True):
        return None


@pytest.fixture
def lifecycle_pool():
    return _LifecyclePool()


@pytest.fixture
def no_real_logger(monkeypatch):
    import agent_cascade.logger.agent_instance_logger as ail
    monkeypatch.setattr(ail, 'AgentInstanceLogger', _FakeInstanceLogger)


class TestFlagOff:
    def test_root_identity_unchanged(self, lifecycle_pool, monkeypatch):
        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
        inst = lifecycle_pool.create_instance('Maine', 'orchestrator')
        assert inst.instance_name == 'Maine'
        assert inst.display_name == 'Maine'

    def test_session_scan_has_no_new_keys(self, tmp_path, monkeypatch):
        import agent_cascade.api_server as api_server

        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
        log = tmp_path / 'orchestrator_Maine_20261115_143022_20261115_150512.jsonl'
        log.write_text(json.dumps({'metadata': {'instance_name': 'Maine_20261115_143022'}}) + '\n',
                       encoding='utf-8')

        [session] = api_server._scan_sessions_sync(tmp_path)
        assert session['name'] == 'Maine_20261115_143022'
        assert 'display_name' not in session
        assert 'instance_name' not in session


class TestFlagOn:
    def test_root_is_stamped_and_display_is_clean(self, lifecycle_pool, monkeypatch):
        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        inst = lifecycle_pool.create_instance('Maine', 'orchestrator')
        assert inst.instance_name.startswith('Maine_')
        assert strip_birth_stamp(inst.instance_name) == 'Maine'
        assert inst.display_name == 'Maine'

    def test_two_roots_same_display_have_distinct_identities_and_kv_labels(
            self, lifecycle_pool, monkeypatch):
        import agent_cascade.state_ops as state_ops

        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        labels = []

        def fake_save_state(api_base, model, instance_name, session_digest=None):
            labels.append(instance_name)
            return f'label:{instance_name}'

        monkeypatch.setattr(state_ops, 'save_state', fake_save_state)

        first = lifecycle_pool.create_instance('Maine', 'orchestrator')
        second = lifecycle_pool.create_instance('Maine', 'orchestrator')
        assert first.instance_name != second.instance_name
        assert first.display_name == second.display_name == 'Maine'

        cfg = {'api_base': 'http://127.0.0.1:1234/v1', 'model': 'shared-model',
               'state_save_enabled': True}
        for inst in (first, second):
            inst._last_endpoint_config = cfg
            assert state_ops.save_instance_state(inst) is True

        assert len(labels) == 2
        assert labels[0] != labels[1]
        assert {strip_birth_stamp(name) for name in labels} == {'Maine'}

    def test_subagent_is_never_minted(self, lifecycle_pool, monkeypatch):
        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        root = lifecycle_pool.create_instance('Maine', 'orchestrator')
        child = lifecycle_pool.create_instance('Coder1', 'coder', parent_instance=root.instance_name)
        assert child.instance_name == 'Coder1'
        assert child.display_name == 'Coder1'

    def test_restore_reuses_persisted_stamp(self, no_real_logger, monkeypatch):
        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        persisted = 'Maine_20261115_143022'
        log = '\n'.join([
            json.dumps({'metadata': {'instance_name': persisted, 'agent_class': 'Orchestrator'}}),
            json.dumps({'role': 'system', 'content': 'You are a test orchestrator.'}),
            json.dumps({'role': 'user', 'content': 'Do the thing.'}),
        ])
        pool = _PoolDouble()

        status = pool.load_session_from_log(
            log, target_instance='Maine', clear_sub_agents_before_load=False)

        assert not status.startswith('Error'), status
        restored = pool.instances[persisted]
        assert restored.instance_name == persisted
        assert restored.display_name == 'Maine'

    def test_session_scan_shows_display_name(self, tmp_path, monkeypatch):
        import agent_cascade.api_server as api_server

        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        identity = 'Maine_20261115_143022'
        log = tmp_path / f'orchestrator_{identity}_20261115_150512.jsonl'
        log.write_text(json.dumps({'metadata': {'instance_name': identity}}) + '\n',
                       encoding='utf-8')

        [session] = api_server._scan_sessions_sync(tmp_path)
        assert session['name'] == 'Maine'
        assert session['display_name'] == 'Maine'
        assert session['instance_name'] == identity


class TestStateBuilderDisplay:
    def test_session_name_uses_display_and_serializes_it(self, lifecycle_pool, monkeypatch):
        from agent_cascade.api_integration_pkg.state_builder import (_get_session_name,
                                                                     _serialize_instance)

        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        root = lifecycle_pool.create_instance('Maine', 'orchestrator')
        snapshot = {root.instance_name: root}
        assert _get_session_name(snapshot, 'fallback') == 'Maine'

        fake_pool = SimpleNamespace(
            instances=snapshot,
            is_instance_halted=lambda name: False,
            has_messages=lambda name: False,
            get_queue_messages=lambda name: [],
        )
        serialized = _serialize_instance(root, fake_pool, all_instances=snapshot)
        assert serialized['instance_name'] == root.instance_name
        assert serialized['display_name'] == 'Maine'
