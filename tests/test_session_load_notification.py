"""Tests for the [SESSION LOADED] in-tool-result notification queued on session load.

When ``AgentPool.load_session_from_log`` succeeds, it queues a single-line
``[SESSION LOADED]`` entry on the restored instance's ``_tool_warnings`` (the in-tool
result channel, drained by compression/handler.py::_drain_tool_warnings / engine/core.py)
so the next turn knows its context was reloaded from disk.

The pool is a REAL AgentPool with mocked dependencies (mirrors the ``agent_pool`` fixture
in tests/test_agent_pool.py), so the restored AgentInstance carries a real
``_tool_warnings`` list and a real ``_compression_lock`` RLock — a bare MagicMock would
auto-create a truthy ``_tool_warnings`` and make the ``_queue_tool_warning`` append a
vacuous no-op (the footgun documented in tests/test_shell_cmd_auto_ram_floor.py::_make_instance).
"""

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def agent_pool():
    """Create a real AgentPool with mocked dependencies so it can be instantiated (no LLM)."""
    with patch('agent_cascade.operation_manager.OperationManager') as mock_op_mgr, \
         patch('agent_cascade.telemetry.TelemetryCollector'), \
         patch('agent_cascade.api_router.APIRouter') as mock_router:

        op_mgr = MagicMock()
        op_mgr.base_dir = MagicMock()
        op_mgr.base_dir.__str__ = lambda self: '/tmp/test_workspace'
        op_mgr.extra_work_folders_ro = []
        op_mgr.extra_work_folders_rw = []
        mock_op_mgr.return_value = op_mgr

        router = MagicMock()
        router.get_effective_concurrency.return_value = 3
        mock_router.return_value = router

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(
            llm_cfg={'max_parallel_agents': 2},
            agents_dir='/tmp/fake_agents',
            workspace_dir='/tmp/test_workspace',
        )
        if hasattr(pool, 'settings'):
            pool.settings.idle_timeout_seconds = 60.0
            pool.settings.idle_check_interval = 30.0
        pool.start()
        pool._idle.stop()
        return pool


def _session_json(num_extra_msgs: int = 3) -> str:
    """A single JSON block (list of message dicts) — _parse_json_input strategy 3."""
    msgs = [{'role': 'system', 'content': 'You are a test agent.'}]
    msgs += [
        {'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'msg {i}'}
        for i in range(num_extra_msgs)
    ]
    return json.dumps(msgs)


class TestSessionLoadNotification:
    """load_session_from_log success path queues [SESSION LOADED]; failure paths don't."""

    def test_successful_load_queues_session_loaded_warning(self, agent_pool):
        num_msgs = 4  # 1 system + 3 user/assistant
        result = agent_pool.load_session_from_log(_session_json(num_msgs - 1), target_instance='restored-1')
        assert result.startswith('Loaded'), f'expected a success status, got: {result!r}'

        inst = agent_pool.get_instance('restored-1')
        assert inst is not None, 'restored instance missing from the pool'
        loaded = [w for w in inst._tool_warnings if w.startswith('[SESSION LOADED]')]
        assert loaded, 'expected a [SESSION LOADED] entry on the restored instance _tool_warnings'
        # House style: single line, names the instance and the reloaded message count.
        w = loaded[0]
        assert '\n' not in w
        assert 'restored-1' in w
        assert f'{num_msgs} conversation messages' in w

    def test_failed_load_does_not_queue_warning(self, agent_pool):
        # Empty input → early 'Error: Empty log input.' return, before any instance exists.
        result = agent_pool.load_session_from_log('')
        assert result.startswith('Error'), f'expected an error status, got: {result!r}'

        # Unparseable input → 'Error: No valid messages found in log input.'
        result = agent_pool.load_session_from_log('not json at all')
        assert result.startswith('Error'), f'expected an error status, got: {result!r}'

        # Failed loads exit before any instance is created, so there is nowhere for a
        # warning to be queued — assert the invariant directly (a loop over instances
        # would be vacuous here since none exist).
        assert not agent_pool.instances, 'failed loads must not create an instance'
