"""BUG_0362: clean root identity vs. user-facing display name.

Covers the slice contract:
  * flag OFF keeps plain root identities (byte-identical legacy behavior),
  * flag ON keeps a clean root identity and appends a numeric suffix (``Maine_2``)
    only when the name is already live; sub-agents are never suffixed,
  * restores reuse the persisted identity verbatim (a legacy birth stamp is never
    re-minted; a clean identity stays clean),
  * session listings show display names,
  * two roots sharing one endpoint/model get distinct KV labels.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

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
    def test_root_identity_is_clean_display_name(self, lifecycle_pool, monkeypatch):
        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        inst = lifecycle_pool.create_instance('Maine', 'orchestrator')
        assert inst.instance_name == 'Maine'
        assert inst.display_name == 'Maine'

    def test_two_roots_same_display_have_distinct_identities_and_kv_labels(
            self, lifecycle_pool, monkeypatch):
        import agent_cascade.state_ops as state_ops

        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        labels = []

        def fake_save_state(api_base, model, instance_name, session_digest=None, label=None):
            labels.append(label if label is not None else instance_name)
            return label if label is not None else instance_name

        monkeypatch.setattr(state_ops, 'save_state', fake_save_state)

        first = lifecycle_pool.create_instance('Maine', 'orchestrator')
        second = lifecycle_pool.create_instance('Maine', 'orchestrator')
        first.kv_birth_ts = '20260101_000000'
        second.kv_birth_ts = '20260101_000000'
        assert {first.instance_name, second.instance_name} == {'Maine', 'Maine_2'}
        assert first.display_name == second.display_name == 'Maine'

        cfg = {'api_base': 'http://127.0.0.1:1234/v1', 'model': 'shared-model',
               'state_save_enabled': True}
        for inst in (first, second):
            inst._last_endpoint_config = cfg
            assert state_ops.save_instance_state(inst) is True

        assert len(labels) == 2
        assert labels[0] != labels[1]
        assert set(labels) == {'Maine_20260101_000000', 'Maine_2_20260101_000000'}

    def test_subagent_is_never_minted(self, lifecycle_pool, monkeypatch):
        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        root = lifecycle_pool.create_instance('Maine', 'orchestrator')
        child = lifecycle_pool.create_instance('Coder1', 'coder', parent_instance=root.instance_name)
        assert child.instance_name == 'Coder1'
        assert child.display_name == 'Coder1'

    def test_second_root_gets_numeric_suffix(self, lifecycle_pool, monkeypatch):
        """A second (and third) live root sharing a display name gets ``_2`` / ``_3``."""
        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        first = lifecycle_pool.create_instance('Maine', 'orchestrator')
        second = lifecycle_pool.create_instance('Maine', 'orchestrator')
        third = lifecycle_pool.create_instance('Maine', 'orchestrator')
        assert [i.instance_name for i in (first, second, third)] == ['Maine', 'Maine_2', 'Maine_3']
        assert all(i.display_name == 'Maine' for i in (first, second, third))

    def test_add_mode_clone_gets_numeric_suffix(self, tmp_path, monkeypatch):
        """Add-mode clone always takes a fresh ``_N`` suffix (force_suffix) so it never
        clobbers the stack it was cloned from — even when the clean name is free."""
        from agent_cascade import ws_handlers as wh
        from tests.test_stack_sessions_backend import _make_handler

        log = tmp_path / 'orchestrator_Maine_20261115_150512.jsonl'
        log.write_text(json.dumps({'metadata': {'instance_name': 'Maine',
                                               'agent_class': 'Orchestrator'}}) + '\n',
                       encoding='utf-8')

        calls = []

        class _AddPool:
            stopped = False

            def load_session_from_log(self, path, *, target_instance,
                                      clear_sub_agents_before_load, caller_name):
                calls.append(target_instance)
                return f'Loaded session into {target_instance}'

            def get_instance(self, name):
                return self.instances.get(name)

        pool = _AddPool()
        pool.instances = {'Maine': object()}
        monkeypatch.setattr(wh, '_clear_caches_safely', lambda: None)
        h, _ = _make_handler(pool)

        # First clone of a live 'Maine' -> 'Maine_2'.
        asyncio.run(h.handle_load_session({'path': str(log), 'mode': 'add'}))
        assert calls == ['Maine_2']

        # A second clone stacks on the first -> 'Maine_3'.
        pool.instances['Maine_2'] = object()
        asyncio.run(h.handle_load_session({'path': str(log), 'mode': 'add'}))
        assert calls == ['Maine_2', 'Maine_3']

    def test_restore_preserves_legacy_stamped_identity(self, no_real_logger, monkeypatch):
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

    def test_restore_preserves_clean_identity(self, no_real_logger, monkeypatch):
        """A clean (un-stamped) persisted identity is restored verbatim — no suffix minted."""
        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        persisted = 'Maine'
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
        assert restored.instance_name == 'Maine'
        assert restored.display_name == 'Maine'

    def test_session_scan_shows_display_name(self, tmp_path, monkeypatch):
        import agent_cascade.api_server as api_server

        monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
        # Clean-identity log (post-BUG_0362): one trailing timestamp.
        clean_log = tmp_path / 'orchestrator_Maine_20261115_150512.jsonl'
        clean_log.write_text(json.dumps({'metadata': {'instance_name': 'Maine'}}) + '\n',
                             encoding='utf-8')
        # Legacy stamped log (pre-BUG_0362): identity keeps the birth stamp.
        legacy_identity = 'Maine_20261115_143022'
        legacy_log = tmp_path / f'orchestrator_{legacy_identity}_20261115_150512.jsonl'
        legacy_log.write_text(json.dumps({'metadata': {'instance_name': legacy_identity}}) + '\n',
                              encoding='utf-8')

        sessions = {s['instance_name']: s for s in api_server._scan_sessions_sync(tmp_path)}
        assert sessions['Maine']['name'] == 'Maine'
        assert sessions['Maine']['display_name'] == 'Maine'
        assert sessions[legacy_identity]['name'] == 'Maine'
        assert sessions[legacy_identity]['display_name'] == 'Maine'


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
            is_paused=lambda: False,
            has_messages=lambda name: False,
            get_queue_messages=lambda name: [],
        )
        serialized = _serialize_instance(root, fake_pool, all_instances=snapshot)
        assert serialized['instance_name'] == root.instance_name
        assert serialized['display_name'] == 'Maine'
