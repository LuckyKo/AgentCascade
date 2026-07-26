"""Regression tests for async shell_cmd changes.

Tests cover: heartbeat routing, __wait behavior, control command justification,
auto-async mode, and edge cases. No LLM or network connections required.
"""

import sys
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, r'N:\work\WD\AgentCascade_unified')

import pytest

from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask


def _setup_task(tracker, task):
    """Add a task to a tracker under test_agent."""
    with tracker._lock:
        if 'test_agent' not in tracker._tasks:
            tracker._tasks['test_agent'] = {}
        tracker._tasks['test_agent'][task.tool_id] = task


def _fake_time_module(initial=1000.0):
    """Create a fake time module for mocking polls without real delays."""
    state = {'time': initial, 'elapsed': 0.0}

    mod = MagicMock()
    mod.time.side_effect = lambda: state['time']
    mod.sleep.side_effect = lambda secs: state.__setitem__('time', state['time'] + secs) or state.__setitem__('elapsed', state['elapsed'] + secs)
    return mod, state


def _make_running_task(tool_id=1, heartbeat_interval=10.0, **kwargs):
    """Create a running AsyncShellTask."""
    return AsyncShellTask(
        tool_id=tool_id,
        agent_name='test_agent',
        command=kwargs.pop('command', 'echo test'),
        pid=kwargs.pop('pid', 12345),
        completed=False,
        heartbeat_interval=heartbeat_interval,
        **kwargs
    )


def _make_tool_with_tracker(shell_cmd_tool, tracker):
    """Wire a ShellCmd tool to use a tracker."""
    mock_pool = MagicMock()
    mock_pool._async_shell_tracker = tracker
    shell_cmd_tool.agent_pool = mock_pool
    shell_cmd_tool.agent_name = 'test_agent'


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_tracker():
    return AsyncShellTracker(pool=None)


@pytest.fixture
def mock_task_running(mock_tracker):
    task = _make_running_task()
    _setup_task(mock_tracker, task)
    return task


@pytest.fixture
def mock_task_completed(mock_tracker):
    task = AsyncShellTask(
        tool_id=2, agent_name='test_agent', command='echo done', pid=12346,
        completed=True, return_code=0, stdout_lines=['done']
    )
    _setup_task(mock_tracker, task)
    return task


@pytest.fixture
def shell_cmd_tool():
    from agent_cascade.tools.custom.shell_cmd import ShellCmd
    return ShellCmd()


# ============================================================================
# Heartbeat routing
# ============================================================================

class TestHeartbeatUsesAsyncResultBuffer:

    def test_heartbeat_goes_to_async_results_not_enqueue(self, mock_task_running):
        pool = MagicMock()
        pool._async_results = MagicMock()
        pool.enqueue_message = MagicMock()

        tracker = AsyncShellTracker(pool=pool)
        _setup_task(tracker, mock_task_running)

        with mock_task_running._lock:
            mock_task_running.stdout_lines = [f'line{i}' for i in range(5)]
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)

        pool._async_results.put.assert_called_once()
        pool.enqueue_message.assert_not_called()

        msg = pool._async_results.put.call_args[0][1]
        assert '⟨shell_cmd heartbeat⟩' in msg
        assert 'Tool ID: 1' in msg

    def test_heartbeat_does_not_double_wrap(self, mock_task_running):
        pool = MagicMock()
        pool._async_results = MagicMock()
        tracker = AsyncShellTracker(pool=pool)
        _setup_task(tracker, mock_task_running)

        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['output line']
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)
        msg = pool._async_results.put.call_args[0][1]

        assert msg.startswith('⟨shell_cmd heartbeat⟩')
        assert '"function_id":' not in msg
        assert 'tool_call_result' not in msg.lower()

    def test_heartbeat_fallback_to_enqueue_when_no_async_results(self, mock_task_running):
        pool = MagicMock()
        del pool._async_results
        pool.enqueue_message = MagicMock()

        tracker = AsyncShellTracker(pool=pool)
        _setup_task(tracker, mock_task_running)

        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['fallback test']
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)

        pool.enqueue_message.assert_called_once()
        msg = pool.enqueue_message.call_args[0][1]
        assert '⟨shell_cmd heartbeat⟩' in msg

    def test_heartbeat_function_id_includes_tool_id(self, mock_task_running):
        pool = MagicMock()
        pool._async_results = MagicMock()
        tracker = AsyncShellTracker(pool=pool)
        _setup_task(tracker, mock_task_running)

        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['data']
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)
        call_kwargs = pool._async_results.put.call_args.kwargs if hasattr(pool._async_results.put.call_args, 'kwargs') else pool._async_results.put.call_args[1]
        func_id = call_kwargs.get('function_id', '')
        assert 'heartbeat_1' in str(func_id)


# ============================================================================
# __wait command behavior
# ============================================================================

class TestWaitCommand:

    def _wait_env(self, shell_cmd_tool, task):
        """Set up tracker+tool for __wait tests with fake time."""
        tracker = AsyncShellTracker(pool=None)
        _setup_task(tracker, task)
        _make_tool_with_tracker(shell_cmd_tool, tracker)
        fake_time_mod, state = _fake_time_module()
        return tracker, fake_time_mod, state

    def test_wait_no_running_shell(self, shell_cmd_tool, mock_tracker):
        _make_tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 999, "async_mode": true}')
        assert 'No running shell found' in result
        assert 'Tool ID: 999' in result

    def test_wait_already_completed(self, shell_cmd_tool):
        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(tool_id=2, agent_name='test_agent', command='echo done', pid=12346,
                              completed=True, return_code=0, stdout_lines=['done'])
        _setup_task(tracker, task)
        _make_tool_with_tracker(shell_cmd_tool, tracker)

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 2, "async_mode": true}')
        assert 'already completed' in result.lower() or 'Process already completed' in result
        assert 'Tool ID: 2' in result

    def test_wait_returns_new_output(self, shell_cmd_tool, mock_task_running):
        tracker = AsyncShellTracker(pool=None)
        _setup_task(tracker, mock_task_running)
        _make_tool_with_tracker(shell_cmd_tool, tracker)

        def delayed_output():
            time.sleep(0.1)
            with mock_task_running._lock:
                mock_task_running.stdout_lines.append('new output line')

        thread = threading.Thread(target=delayed_output, daemon=True)
        thread.start()

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "async_mode": true}')
        thread.join(timeout=2)

        assert '⟨shell_cmd wait⟩' in result
        assert 'Tool ID: 1' in result
        assert 'new output line' in result

    def test_wait_returns_completion_status(self, shell_cmd_tool, mock_task_running):
        tracker = AsyncShellTracker(pool=None)
        _setup_task(tracker, mock_task_running)
        _make_tool_with_tracker(shell_cmd_tool, tracker)

        def delayed_completion():
            time.sleep(0.1)
            with mock_task_running._lock:
                mock_task_running.completed = True
                mock_task_running.return_code = 0

        thread = threading.Thread(target=delayed_completion, daemon=True)
        thread.start()

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "async_mode": true}')
        thread.join(timeout=2)

        assert '⟨shell_cmd wait⟩' in result
        assert 'Process completed' in result
        assert 'exit code 0' in result

    def test_wait_returns_timeout_when_no_output(self, shell_cmd_tool, mock_task_running):
        _, fake_time_mod, _ = self._wait_env(shell_cmd_tool, mock_task_running)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "async_mode": true}')

        assert '⟨shell_cmd wait⟩' in result
        assert 'No new output' in result
        assert 'timeout' in result.lower()

    def test_wait_respects_timeout_cap_at_60s(self, shell_cmd_tool):
        task = _make_running_task(heartbeat_interval=3600.0, command='sleep 1000', pid=99999)
        _, fake_time_mod, state = self._wait_env(shell_cmd_tool, task)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "async_mode": true}')

        assert state['elapsed'] <= 60.0, f"__wait waited {state['elapsed']:.1f}s, should cap at 60s"
        assert state['elapsed'] >= 59.0, f"__wait waited {state['elapsed']:.1f}s, should be ~60s"
        assert 'No new output' in result
        assert '60s' in result

    def test_wait_no_deadlock_on_sequential_access(self, shell_cmd_tool, mock_task_running):
        _, fake_time_mod, _ = self._wait_env(shell_cmd_tool, mock_task_running)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            for _ in range(3):
                result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "async_mode": true}')
                assert '⟨shell_cmd wait⟩' in result

    def test_wait_proper_lock_handling(self, shell_cmd_tool):
        task = _make_running_task(heartbeat_interval=5.0, command='sleep 100', pid=99999)
        _, fake_time_mod, _ = self._wait_env(shell_cmd_tool, task)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "async_mode": true}')

        with task._lock:
            assert task.completed is False


# ============================================================================
# Justification rules
# ============================================================================

class TestOptionalJustification:

    def _tracker_with_task(self, tool_id=1, **kwargs):
        tracker = AsyncShellTracker(pool=None)
        task = _make_running_task(tool_id=tool_id, **kwargs)
        _setup_task(tracker, task)
        return tracker

    def test_control_command_without_justification(self, shell_cmd_tool):
        tracker = self._tracker_with_task()
        _make_tool_with_tracker(shell_cmd_tool, tracker)
        result = shell_cmd_tool.call('{"command": "__status", "tool_id": 1, "async_mode": true}')
        assert 'ValueError' not in result
        assert '⟨shell_cmd status⟩' in result
        assert 'Tool ID: 1' in result

    def test_kill_command_without_justification(self, shell_cmd_tool):
        tracker = self._tracker_with_task()
        _make_tool_with_tracker(shell_cmd_tool, tracker)
        result = shell_cmd_tool.call('{"command": "__kill", "tool_id": 1, "async_mode": true}')
        assert 'ValueError' not in result

    def test_ctrl_c_without_justification(self, shell_cmd_tool):
        tracker = self._tracker_with_task(tool_id=3, heartbeat_interval=5.0)
        _make_tool_with_tracker(shell_cmd_tool, tracker)
        result = shell_cmd_tool.call('{"command": "__ctrl_c", "tool_id": 3, "async_mode": true}')
        assert 'ValueError' not in result
        assert 'Tool ID: 3' in result or 'Ctrl+C sent' in result or 'Failed' in result

    def test_heartbeat_update_without_justification(self, shell_cmd_tool):
        tracker = self._tracker_with_task(tool_id=4, heartbeat_interval=10.0)
        _make_tool_with_tracker(shell_cmd_tool, tracker)
        result = shell_cmd_tool.call('{"command": "__heartbeat=5", "tool_id": 4, "async_mode": true}')
        assert 'ValueError' not in result
        assert 'updated' in result.lower() or 'Tool ID: 4' in result

    def test_status_command_works_without_justification(self, shell_cmd_tool):
        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(tool_id=5, agent_name='test_agent', command='long running task',
                              pid=55555, completed=False, heartbeat_interval=5.0,
                              stdout_lines=['line1', 'line2'])
        _setup_task(tracker, task)
        _make_tool_with_tracker(shell_cmd_tool, tracker)

        result = shell_cmd_tool.call('{"command": "__status", "tool_id": 5, "async_mode": true}')
        assert '⟨shell_cmd status⟩' in result
        assert 'Tool ID: 5' in result
        assert 'line1' in result or 'line2' in result or 'running' in result.lower()

    def test_regular_command_without_justification_raises(self, shell_cmd_tool, mock_tracker):
        _make_tool_with_tracker(shell_cmd_tool, mock_tracker)
        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call('{"command": "ls -la"}')
        assert 'justification' in str(exc_info.value).lower()

    def test_tool_id_with_non_control_command_requires_justification(self, shell_cmd_tool):
        """Having tool_id alone doesn't exempt a regular command from needing justification."""
        _make_tool_with_tracker(shell_cmd_tool, MagicMock())
        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call('{"command": "echo test", "tool_id": 1}')
        assert 'justification' in str(exc_info.value).lower()

    def test_control_command_with_non_numeric_tool_id_raises(self, shell_cmd_tool):
        """Control commands with non-numeric tool_id should raise a clear error."""
        _make_tool_with_tracker(shell_cmd_tool, MagicMock())
        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call('{"command": "__status", "tool_id": "abc"}')
        assert 'tool_id' in str(exc_info.value).lower() and 'numeric' in str(exc_info.value).lower()

    def test_regular_command_with_justification_works(self, shell_cmd_tool):
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='file1\nfile2\n') as mock_exec:
            result = shell_cmd_tool.call('{"command": "ls -la", "justification": "listing files"}')
            mock_exec.assert_called_once()
            assert 'file1' in result

    def test_async_launch_without_justification_raises(self, shell_cmd_tool, mock_tracker):
        _make_tool_with_tracker(shell_cmd_tool, mock_tracker)
        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call('{"command": "echo hello", "async_mode": true}')
        assert 'justification' in str(exc_info.value).lower()

    def test_async_launch_with_justification_works(self, shell_cmd_tool, mock_tracker):
        tracker = AsyncShellTracker(pool=None)
        tracker.launch = MagicMock(return_value=(1, 12345))

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call('{"command": "echo hello", "async_mode": true, "justification": "test"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result
        assert 'Tool ID: 1' in result


# ============================================================================
# __wait in _CONTROL_COMMANDS
# ============================================================================

class TestWaitInControlCommands:

    def test_wait_is_in_control_commands(self):
        from agent_cascade.operation_manager.shell import ShellMixin
        assert '__wait' in ShellMixin._CONTROL_COMMANDS

    def test_all_expected_control_commands_present(self):
        from agent_cascade.operation_manager.shell import ShellMixin
        assert {'__status', '__kill', '__ctrl_c', '__wait'}.issubset(set(ShellMixin._CONTROL_COMMANDS))

    def test_control_commands_are_safe(self):
        from agent_cascade.operation_manager.shell import ShellMixin
        mixin = ShellMixin()
        for cmd in ShellMixin._CONTROL_COMMANDS:
            assert mixin._is_safe_readonly_shell_command(cmd), f"{cmd} should be safe"
        assert mixin._is_safe_readonly_shell_command('__heartbeat=5')


# ============================================================================
# Auto-async mode
# ============================================================================

class TestAutoAsyncMode:

    def _tool_with_tracker(self, shell_cmd_tool, tracker=None):
        if tracker is None:
            tracker = AsyncShellTracker(pool=None)
        tracker.launch = MagicMock(return_value=(1, 12345))
        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'
        return tracker

    def test_timeout_gt_60_auto_switches_to_async(self, shell_cmd_tool, mock_tracker):
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 120, "justification": "test"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result

    def test_timeout_gt_60_with_explicit_async_mode_false_stays_sync(self, shell_cmd_tool):
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='output') as mock_exec:
            shell_cmd_tool.agent_pool = MagicMock()
            shell_cmd_tool.agent_name = 'test_agent'
            result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 120, "async_mode": false, "justification": "sync"}')
            mock_exec.assert_called_once()
            assert 'output' in result

    @pytest.mark.parametrize('timeout', [30, 60])
    def test_timeout_at_or_below_60_stays_sync(self, shell_cmd_tool, timeout):
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='output') as mock_exec:
            shell_cmd_tool.agent_pool = MagicMock()
            shell_cmd_tool.agent_name = 'test_agent'
            result = shell_cmd_tool.call(f'{{"command": "echo hello", "timeout": {timeout}, "justification": "sync"}}')
            mock_exec.assert_called_once()
            assert 'output' in result

    def test_explicit_async_mode_true_ignores_timeout_threshold(self, shell_cmd_tool, mock_tracker):
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "echo hello", "async_mode": true, "timeout": 1, "justification": "async"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result

    def test_auto_async_defaults_heartbeat_to_30(self, shell_cmd_tool, mock_tracker):
        """Auto-async mode should default heartbeat_interval to 30s when not explicitly set."""
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        # No heartbeat_interval specified; auto-async should kick in (timeout > 60)
        result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 120, "justification": "test"}')
        tracker.launch.assert_called_once()
        call_kwargs = tracker.launch.call_args
        assert call_kwargs.kwargs.get('heartbeat_interval') == 30, \
            f"Expected heartbeat_interval=30 for auto-async, got {call_kwargs.kwargs.get('heartbeat_interval')}"

    def test_auto_async_respects_explicit_heartbeat(self, shell_cmd_tool, mock_tracker):
        """Auto-async mode should not override an explicitly set heartbeat_interval."""
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 120, "heartbeat_interval": 60, "justification": "test"}')
        tracker.launch.assert_called_once()
        call_kwargs = tracker.launch.call_args
        assert call_kwargs.kwargs.get('heartbeat_interval') == 60, \
            f"Expected heartbeat_interval=60 (explicit), got {call_kwargs.kwargs.get('heartbeat_interval')}"


# ============================================================================
# Edge cases
# ============================================================================

class TestEdgeCases:

    def test_wait_with_no_heartbeat_interval_uses_default_timeout(self, shell_cmd_tool):
        task = _make_running_task(heartbeat_interval=-1.0, command='sleep 100', pid=99999)
        tracker = AsyncShellTracker(pool=None)
        _setup_task(tracker, task)
        _make_tool_with_tracker(shell_cmd_tool, tracker)
        fake_time_mod, state = _fake_time_module()

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "async_mode": true}')

        assert state['elapsed'] >= 29.0, f"__wait waited {state['elapsed']:.1f}s, expected ~30s"
        assert state['elapsed'] <= 31.0, f"__wait waited {state['elapsed']:.1f}s, expected ~30s"
        assert 'No new output' in result