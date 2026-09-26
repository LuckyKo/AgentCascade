"""Tests for the additive `active_instances` field on GET /api/status.

Covers plan §6.1 (T1–T6): only ACTIVE_STATES instances are surfaced, with
correct name/state/turn/max_turns/tokens; idle + terminated excluded; malformed
conversations don't raise; existing response keys unchanged (no waiter regression).

Uses a lightweight fake pool (duck-typed) so the endpoint's real logic runs without
spinning up a full AgentPool / mock LLM server. The endpoint reads only:
  - agent_pool.instances        (dict name -> AgentInstance)
  - agent_pool.list_agents()    (template-name strings)
  - agent_pool.active_stack     (via get_active_stack, hasattr-guarded)
  - agent_pool.operation_manager (via get_approvals, getattr-guarded)
  - agent_pool.is_instance_halted(sess_name)  (hasattr-guarded)
A real AgentInstance object is used per entry so the `_state_lock` + `conversation`
snapshot path in the endpoint is exercised for real.
"""

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from agent_cascade.agent_instance import ACTIVE_STATES, AgentInstance, AgentState
from agent_cascade.llm.schema import Message, USER
from agent_cascade.settings import DEFAULT_MAX_TURNS


def _make_inst(name, state, agent_class='coder', conv=None, max_turns=None, turn=0):
    """Build a real AgentInstance in the given lifecycle state."""
    now = time.monotonic()
    inst = AgentInstance(
        instance_name=name,
        agent_class=agent_class,
        conversation=conv if conv is not None else [],
        state=state,
        max_turns=max_turns,
        parent_instance=None,
        created_at=now,
        last_activity=now,
        compression_summary=None,
        latest_marker_index=-1,
    )
    inst._current_turn = turn  # _current_turn is a private field (not a dataclass kwarg)
    return inst


def _fake_pool(instances):
    """Duck-typed pool exposing only what /api/status touches."""
    pool = MagicMock()
    pool.instances = instances  # real dict of name -> AgentInstance
    pool.list_agents.return_value = ['orchestrator', 'coder']
    pool.active_stack = []
    pool.operation_manager = None
    pool.is_instance_halted.return_value = False
    return pool


@pytest.fixture
def client():
    """TestClient bound to a fresh app per test (isolation between cases)."""
    from agent_cascade.api_server import create_app

    def _build(instances):
        app = create_app(agents=[], agent_pool=_fake_pool(instances), config={'session_name': 'T'})
        return TestClient(app, raise_server_exceptions=False)

    return _build


def _get_status(client):
    """Call /api/status with a valid token by injecting one into the app's session store."""
    # Find the running app + its api_sessions dict via the TestClient's ASGI app.
    app = client.app
    # create_app stores sessions in a closure; reach it through the endpoint's globals is
    # not possible, so we register a token by hitting /api/handshake instead.
    keys = client.get('/api/keys')
    assert keys.status_code == 200
    server_pub = keys.json()['public_key']

    from cryptography.hazmat.primitives.asymmetric import x25519
    import base64
    priv = x25519.X25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode('utf-8')
    hs = client.post('/api/handshake', json={'public_key': pub_b64})
    assert hs.status_code == 200
    token = hs.json()['session_token']
    resp = client.get(f'/api/status?token={token}')
    assert resp.status_code == 200
    return resp.json()


class TestActiveInstances:

    def test_t1_no_instances_empty_and_agents_unchanged(self, client):
        c = client({})
        data = _get_status(c)
        assert data['active_instances'] == []
        # `agents` stays a list of template-name strings (contract unchanged).
        assert isinstance(data['agents'], list)
        assert all(isinstance(a, str) for a in data['agents'])

    def test_t2_one_running_instance_fields(self, client):
        conv = [Message(role=USER, content='hello world this is a message')] * 3
        inst = _make_inst('coder', AgentState.RUNNING, agent_class='coder', conv=conv, turn=5)
        data = _get_status(client({'coder': inst}))
        assert len(data['active_instances']) == 1
        row = data['active_instances'][0]
        assert row['name'] == 'coder'
        assert row['state'] == 'RUNNING'
        assert row['turn'] >= 0
        assert row['turn'] == 5
        # max_turns unset -> DEFAULT_MAX_TURNS (250)
        assert row['max_turns'] == DEFAULT_MAX_TURNS
        assert row['tokens'] > 0

    def test_t3_idle_and_terminated_excluded(self, client):
        instances = {
            'idle_a': _make_inst('idle_a', AgentState.IDLE),
            'term_b': _make_inst('term_b', AgentState.TERMINATED),
            'run_c': _make_inst('run_c', AgentState.RUNNING, conv=[Message(role=USER, content='x')]),
        }
        data = _get_status(client(instances))
        names = {r['name'] for r in data['active_instances']}
        assert names == {'run_c'}
        # every surfaced state is in ACTIVE_STATES
        for r in data['active_instances']:
            assert AgentState[r['state']] in ACTIVE_STATES

    def test_t4_max_turns_resolution(self, client):
        # None -> default; explicit value respected.
        inst_none = _make_inst('a', AgentState.RUNNING, max_turns=None)
        inst_custom = _make_inst('b', AgentState.SLEEPING, max_turns=42)
        data = _get_status(client({'a': inst_none, 'b': inst_custom}))
        by_name = {r['name']: r for r in data['active_instances']}
        assert by_name['a']['max_turns'] == DEFAULT_MAX_TURNS
        assert by_name['b']['max_turns'] == 42

    def test_t5_malformed_conversation_does_not_raise(self, client):
        # None / dict / bool entries must be skipped gracefully (mirrors get_history_stats).
        conv = [None, {'role': 'user', 'content': 'hi'}, True, Message(role=USER, content='ok')]
        inst = _make_inst('weird', AgentState.RUNNING, conv=conv)
        data = _get_status(client({'weird': inst}))  # must not raise / 500
        row = data['active_instances'][0]
        assert row['name'] == 'weird'
        assert isinstance(row['tokens'], int)
        assert isinstance(row['words'], int)

    def test_t6_existing_keys_unchanged(self, client):
        inst = _make_inst('coder', AgentState.RUNNING, conv=[Message(role=USER, content='hi')])
        data = _get_status(client({'coder': inst}))
        for key in ('generating', 'agents', 'active_stack', 'instance_halted', 'pending_approvals'):
            assert key in data, f'missing existing key {key!r}'
        assert isinstance(data['generating'], bool)
        assert isinstance(data['active_agent'], str)
        assert isinstance(data['active_stack'], list)
        assert isinstance(data['instance_halted'], bool)
        assert isinstance(data['pending_approvals'], list)
