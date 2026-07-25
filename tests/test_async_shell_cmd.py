"""Regression tests for async shell_cmd changes.

Tests cover:
1. Heartbeat uses async result buffer (not user message queue)
2. __wait command behavior (all variants)
3. Optional justification for control commands
4. __wait in _CONTROL_COMMANDS
5. Auto-async mode (timeout > 60s)

No LLM or network connections required.
"""

import sys
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, r'N:\work\WD\AgentCascade_unified')

import pytest


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_tracker():
    """Create a minimal AsyncShellTracker mock."""
    from agent_cascade.async_shell import AsyncShellTracker
    tracker = AsyncShellTracker(pool=None)
    return tracker


@pytest.fixture
def mock_pool_with_async_results():
    """Create a mock AgentPool with _async_results buffer."""
    pool = MagicMock()
    pool._async_results = MagicMock()
    return pool


@pytest.fixture
def mock_pool_without_async_results():
    """Create a mock AgentPool without _async_results buffer."""
    pool = MagicMock()
    del pool._async_results
    return pool


@pytest.fixture
def mock_task_running(mock_tracker):
    """Create a mock AsyncShellTask that appears running."""
    from agent_cascade.async_shell import AsyncShellTask
    task = AsyncShellTask(
        tool_id=1,
        agent_name='test_agent',
        command='echo test',
        pid=12345,
        completed=False,
        heartbeat_interval=10.0,
    )
    # Inject into tracker
    with mock_tracker._lock:
        if 'test_agent' not in mock_tracker._tasks:
            mock_tracker._tasks['test_agent'] = {}
        mock_tracker._tasks['test_agent'][1] = task
    return task


@pytest.fixture
def mock_task_completed(mock_tracker):
    """Create a mock AsyncShellTask that appears completed."""
    from agent_cascade.async_shell import AsyncShellTask
    task = AsyncShellTask(
        tool_id=2,
        agent_name='test_agent',
        command='echo done',
        pid=12346,
        completed=True,
        return_code=0,
        stdout_lines=['done'],
    )
    with mock_tracker._lock:
        if 'test_agent' not in mock_tracker._tasks:
            mock_tracker._tasks['test_agent'] = {}
        mock_tracker._tasks['test_agent'][2] = task
    return task


@pytest.fixture
def shell_cmd_tool():
    """Create a ShellCmd tool instance with minimal dependencies."""
    from agent_cascade.tools.custom.shell_cmd import ShellCmd
    tool = ShellCmd()
    return tool


# ============================================================================
# 1. Heartbeat uses async result buffer
# ============================================================================

class TestHeartbeatUsesAsyncResultBuffer:
    """Verify heartbeats go into _async_results, not user message queue."""

    def test_heartbeat_goes_to_async_results_not_enqueue(self, mock_task_running):
        """Heartbeat should use _async_results.put, not pool.enqueue_message."""
        from agent_cascade.async_shell import AsyncShellTracker, ASYNC_SHELL_HEARTBEAT_TRUNCATE_CHARS

        # Create tracker with pool that has _async_results
        pool = MagicMock()
        pool._async_results = MagicMock()
        pool.enqueue_message = MagicMock()

        tracker = AsyncShellTracker(pool=pool)

        # Inject the running task
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = mock_task_running

        # Add output lines to the task (so heartbeat has something to send)
        with mock_task_running._lock:
            mock_task_running.stdout_lines = [f'line{i}' for i in range(5)]
            mock_task_running.last_heartbeat_sent_pos = 0

        # Call heartbeat
        tracker._send_heartbeat('test_agent', 1)

        # Verify _async_results.put was called, NOT enqueue_message
        pool._async_results.put.assert_called_once()
        pool.enqueue_message.assert_not_called()

        # Verify the message is a heartbeat
        call_args = pool._async_results.put.call_args
        msg = call_args[0][1]  # second positional arg is the message
        assert '⟨shell_cmd heartbeat⟩' in msg
        assert 'Tool ID: 1' in msg

    def test_heartbeat_does_not_double_wrap(self, mock_task_running):
        """Heartbeat message should NOT be wrapped by _make_async_result_message."""
        from agent_cascade.async_shell import AsyncShellTracker

        pool = MagicMock()
        pool._async_results = MagicMock()

        tracker = AsyncShellTracker(pool=pool)

        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = mock_task_running

        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['output line']
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)

        msg = pool._async_results.put.call_args[0][1]

        # The message should be a clean heartbeat, not double-wrapped
        # Double-wrapping would look like nested JSON or tool_call_result wrappers
        assert msg.startswith('⟨shell_cmd heartbeat⟩')
        # Should NOT contain typical double-wrap markers
        assert '"function_id":' not in msg
        assert 'tool_call_result' not in msg.lower()

    def test_heartbeat_fallback_to_enqueue_when_no_async_results(self, mock_task_running):
        """If pool lacks _async_results, heartbeat falls back to enqueue_message."""
        from agent_cascade.async_shell import AsyncShellTracker

        pool = MagicMock()
        del pool._async_results  # Remove the attribute
        pool.enqueue_message = MagicMock()

        tracker = AsyncShellTracker(pool=pool)

        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = mock_task_running

        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['fallback test']
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)

        pool.enqueue_message.assert_called_once()
        msg = pool.enqueue_message.call_args[0][1]
        assert '⟨shell_cmd heartbeat⟩' in msg

    def test_heartbeat_function_id_includes_tool_id(self, mock_task_running):
        """Heartbeat should include function_id with tool_id for proper routing."""
        from agent_cascade.async_shell import AsyncShellTracker

        pool = MagicMock()
        pool._async_results = MagicMock()

        tracker = AsyncShellTracker(pool=pool)

        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = mock_task_running

        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['data']
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)

        # Check kwargs for function_id
        call_kwargs = pool._async_results.put.call_args.kwargs if hasattr(pool._async_results.put.call_args, 'kwargs') else pool._async_results.put.call_args[1]
        func_id = call_kwargs.get('function_id', '')
        assert 'heartbeat_1' in str(func_id)


# ============================================================================
# 2. __wait command behavior
# ============================================================================

class TestWaitCommand:
    """Verify __wait command handles all scenarios correctly."""

    def test_wait_no_running_shell(self, shell_cmd_tool, mock_tracker):
        """__wait with nonexistent tool_id returns 'no running shell found'."""
        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = mock_tracker

        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "__wait", "tool_id": 999, "async_mode": true}'
        )

        assert 'No running shell found' in result
        assert 'Tool ID: 999' in result

    def test_wait_already_completed(self, shell_cmd_tool):
        """__wait on completed task returns 'already completed'."""
        from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask

        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(
            tool_id=2,
            agent_name='test_agent',
            command='echo done',
            pid=12346,
            completed=True,
            return_code=0,
            stdout_lines=['done'],
        )
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][2] = task

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "__wait", "tool_id": 2, "async_mode": true}'
        )

        assert 'already completed' in result.lower() or 'Process already completed' in result
        assert 'Tool ID: 2' in result

    def test_wait_returns_new_output(self, shell_cmd_tool, mock_task_running):
        """__wait blocks briefly and returns new output when shell produces it."""
        from agent_cascade.async_shell import AsyncShellTracker

        tracker = AsyncShellTracker(pool=None)
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = mock_task_running

        # Simulate output appearing after a short delay
        def delayed_output():
            time.sleep(0.1)
            with mock_task_running._lock:
                mock_task_running.stdout_lines.append('new output line')

        thread = threading.Thread(target=delayed_output, daemon=True)
        thread.start()

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "__wait", "tool_id": 1, "async_mode": true}'
        )

        thread.join(timeout=2)

        assert '⟨shell_cmd wait⟩' in result
        assert 'Tool ID: 1' in result
        assert 'new output line' in result

    def test_wait_returns_completion_status(self, shell_cmd_tool, mock_task_running):
        """__wait returns completion status when shell finishes."""
        from agent_cascade.async_shell import AsyncShellTracker

        tracker = AsyncShellTracker(pool=None)
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = mock_task_running

        # Simulate completion after a short delay
        def delayed_completion():
            time.sleep(0.1)
            with mock_task_running._lock:
                mock_task_running.completed = True
                mock_task_running.return_code = 0

        thread = threading.Thread(target=delayed_completion, daemon=True)
        thread.start()

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "__wait", "tool_id": 1, "async_mode": true}'
        )

        thread.join(timeout=2)

        assert '⟨shell_cmd wait⟩' in result
        assert 'Process completed' in result
        assert 'exit code 0' in result

    def test_wait_returns_timeout_when_no_output(self, shell_cmd_tool, mock_task_running):
        """__wait returns timeout message when no output within timeout period."""
        from agent_cascade.async_shell import AsyncShellTracker

        tracker = AsyncShellTracker(pool=None)
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = mock_task_running

        # No output will be produced
        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        # Mock time to fast-forward through the poll loop
        current_time = [1000.0]

        def fake_time():
            return current_time[0]

        def fake_sleep(seconds):
            current_time[0] += seconds

        # time is imported inside the __wait block, so patch at module level
        fake_time_module = MagicMock()
        fake_time_module.time.side_effect = fake_time
        fake_time_module.sleep.side_effect = fake_sleep

        with patch.dict('sys.modules', {'time': fake_time_module}):
            result = shell_cmd_tool.call(
                '{"command": "__wait", "tool_id": 1, "async_mode": true}'
            )

        assert '⟨shell_cmd wait⟩' in result
        assert 'No new output' in result
        assert 'timeout' in result.lower()

    def test_wait_respects_timeout_cap_at_60s(self, shell_cmd_tool):
        """__wait caps timeout at 60s even if heartbeat_interval is huge."""
        from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask

        tracker = AsyncShellTracker(pool=None)

        # Create task with huge heartbeat interval (3600s)
        task = AsyncShellTask(
            tool_id=1,
            agent_name='test_agent',
            command='sleep 1000',
            pid=99999,
            completed=False,
            heartbeat_interval=3600.0,  # 1 hour!
        )
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = task

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        # Mock time to fast-forward through the poll loop
        current_time = [1000.0]
        elapsed = [0.0]

        def fake_time():
            return current_time[0]

        def fake_sleep(seconds):
            current_time[0] += seconds
            elapsed[0] += seconds

        fake_time_module = MagicMock()
        fake_time_module.time.side_effect = fake_time
        fake_time_module.sleep.side_effect = fake_sleep

        with patch.dict('sys.modules', {'time': fake_time_module}):
            result = shell_cmd_tool.call(
                '{"command": "__wait", "tool_id": 1, "async_mode": true}'
            )

        # Should timeout at 60s cap, NOT wait for 3600s
        assert elapsed[0] <= 60.0, f"__wait waited {elapsed[0]:.1f}s, should cap at 60s"
        assert elapsed[0] >= 59.0, f"__wait waited {elapsed[0]:.1f}s, should be ~60s"
        assert 'No new output' in result
        # The timeout message should show ~60s
        assert '60s' in result

    def test_wait_no_deadlock_on_sequential_access(self, shell_cmd_tool, mock_task_running):
        """__wait should not deadlock when called multiple times sequentially."""
        from agent_cascade.async_shell import AsyncShellTracker

        tracker = AsyncShellTracker(pool=None)
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = mock_task_running

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        # Mock time to fast-forward through the poll loop
        current_time = [1000.0]

        def fake_time():
            return current_time[0]

        def fake_sleep(seconds):
            current_time[0] += seconds

        fake_time_module = MagicMock()
        fake_time_module.time.side_effect = fake_time
        fake_time_module.sleep.side_effect = fake_sleep

        # Run __wait sequentially multiple times to check no lock issues
        with patch.dict('sys.modules', {'time': fake_time_module}):
            for i in range(3):
                result = shell_cmd_tool.call(
                    '{"command": "__wait", "tool_id": 1, "async_mode": true}'
                )
                assert '⟨shell_cmd wait⟩' in result

    def test_wait_proper_lock_handling(self, shell_cmd_tool):
        """__wait properly acquires/releases task lock without holding it across sleeps."""
        from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask

        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(
            tool_id=1,
            agent_name='test_agent',
            command='sleep 100',
            pid=99999,
            completed=False,
            heartbeat_interval=5.0,
        )
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = task

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        current_time = [1000.0]

        def fake_time():
            return current_time[0]

        def fake_sleep(seconds):
            current_time[0] += seconds

        fake_time_module = MagicMock()
        fake_time_module.time.side_effect = fake_time
        fake_time_module.sleep.side_effect = fake_sleep

        with patch.dict('sys.modules', {'time': fake_time_module}):
            result = shell_cmd_tool.call(
                '{"command": "__wait", "tool_id": 1, "async_mode": true}'
            )

        # Lock should be released after __wait completes
        with task._lock:
            # This should not deadlock — lock was properly released
            assert task.completed == False


# ============================================================================
# 3. Optional justification for control commands
# ============================================================================

class TestOptionalJustification:
    """Verify justification is optional for control commands but required for regular commands."""

    def test_control_command_without_justification(self, shell_cmd_tool):
        """Control command (__status) with tool_id works WITHOUT justification."""
        from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask

        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(
            tool_id=1,
            agent_name='test_agent',
            command='echo test',
            pid=12345,
            completed=False,
        )
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = task

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        # No justification provided, should still work
        result = shell_cmd_tool.call(
            '{"command": "__status", "tool_id": 1, "async_mode": true}'
        )

        assert '⟨shell_cmd status⟩' in result
        assert 'Tool ID: 1' in result
        assert 'ValueError' not in result

    def test_kill_command_without_justification(self, shell_cmd_tool):
        """__kill with tool_id works WITHOUT justification."""
        from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask

        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(
            tool_id=1,
            agent_name='test_agent',
            command='echo test',
            pid=12345,
            completed=False,
        )
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = task

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "__kill", "tool_id": 1, "async_mode": true}'
        )

        # Should succeed without ValueError about justification
        assert 'ValueError' not in result
        # Will say "Shell already finished" since we don't have a real process
        # but key point is no justification error

    def test_regular_command_without_justification_raises(self, shell_cmd_tool, mock_tracker):
        """Regular sync command WITHOUT justification raises ValueError."""
        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = mock_tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call(
                '{"command": "ls -la"}'
            )

        assert 'justification' in str(exc_info.value).lower()

    def test_regular_command_with_justification_works(self, shell_cmd_tool):
        """Regular command WITH justification works (no error)."""
        # Mock out the sync execution to avoid actual shell call
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='file1\nfile2\n') as mock_exec:
            result = shell_cmd_tool.call(
                '{"command": "ls -la", "justification": "listing files in workspace"}'
            )

            mock_exec.assert_called_once()
            assert 'file1' in result

    def test_async_launch_without_justification_raises(self, shell_cmd_tool, mock_tracker):
        """Async launch (async_mode=true, no tool_id) WITHOUT justification raises ValueError."""
        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = mock_tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call(
                '{"command": "echo hello", "async_mode": true}'
            )

        assert 'justification' in str(exc_info.value).lower()

    def test_async_launch_with_justification_works(self, shell_cmd_tool, mock_tracker):
        """Async launch WITH justification works."""
        from agent_cascade.async_shell import AsyncShellTracker

        tracker = AsyncShellTracker(pool=None)

        # Mock launch to avoid spawning real processes
        original_launch = tracker.launch
        tracker.launch = MagicMock(return_value=(1, 12345))

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "echo hello", "async_mode": true, "justification": "test launch"}'
        )

        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result
        assert 'Tool ID: 1' in result


# ============================================================================
# 4. __wait in _CONTROL_COMMANDS
# ============================================================================

class TestWaitInControlCommands:
    """Verify __wait is listed in ShellMixin._CONTROL_COMMANDS."""

    def test_wait_is_in_control_commands(self):
        """__wait must be in _CONTROL_COMMANDS tuple."""
        from agent_cascade.operation_manager.shell import ShellMixin

        assert '__wait' in ShellMixin._CONTROL_COMMANDS

    def test_all_expected_control_commands_present(self):
        """All expected control commands should be present."""
        from agent_cascade.operation_manager.shell import ShellMixin

        expected = {'__status', '__kill', '__ctrl_c', '__wait'}
        assert expected.issubset(set(ShellMixin._CONTROL_COMMANDS))

    def test_control_commands_are_safe(self):
        """Control commands should be detected as safe read-only commands."""
        from agent_cascade.operation_manager.shell import ShellMixin

        mixin = ShellMixin()

        for cmd in ShellMixin._CONTROL_COMMANDS:
            assert mixin._is_safe_readonly_shell_command(cmd), f"{cmd} should be safe"

        # Also test heartbeat prefix
        assert mixin._is_safe_readonly_shell_command('__heartbeat=5')


# ============================================================================
# 5. Auto-async mode (timeout > 60s)
# ============================================================================

class TestAutoAsyncMode:
    """Verify auto-async mode triggers correctly based on timeout."""

    def test_timeout_gt_60_auto_switches_to_async(self, shell_cmd_tool, mock_tracker):
        """timeout > 60 without async_mode → auto-switches to async."""
        from agent_cascade.async_shell import AsyncShellTracker

        tracker = AsyncShellTracker(pool=None)
        tracker.launch = MagicMock(return_value=(1, 12345))

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "echo hello", "timeout": 120, "justification": "test auto-async"}'
        )

        # Should have called launch (async path), not execute_sync
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result

    def test_timeout_gt_60_with_explicit_async_mode_false_stays_sync(self, shell_cmd_tool):
        """timeout > 60 with async_mode=False → stays sync (explicit override)."""
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='output') as mock_exec:
            mock_tracker = MagicMock()
            mock_pool = MagicMock()
            mock_pool._async_shell_tracker = mock_tracker
            shell_cmd_tool.agent_pool = mock_pool
            shell_cmd_tool.agent_name = 'test_agent'

            result = shell_cmd_tool.call(
                '{"command": "echo hello", "timeout": 120, "async_mode": false, "justification": "force sync"}'
            )

            # Explicit async_mode=false should override auto-async
            mock_exec.assert_called_once()
            assert 'output' in result

    def test_timeout_lt_60_without_async_mode_stays_sync(self, shell_cmd_tool):
        """timeout <= 60 without async_mode → stays sync."""
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='output') as mock_exec:
            shell_cmd_tool.agent_pool = MagicMock()
            shell_cmd_tool.agent_name = 'test_agent'

            result = shell_cmd_tool.call(
                '{"command": "echo hello", "timeout": 30, "justification": "short timeout"}'
            )

            mock_exec.assert_called_once()
            assert 'output' in result

    def test_timeout_exactly_60_stays_sync(self, shell_cmd_tool):
        """timeout == 60 → stays sync (only > 60 triggers auto-async)."""
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='output') as mock_exec:
            shell_cmd_tool.agent_pool = MagicMock()
            shell_cmd_tool.agent_name = 'test_agent'

            result = shell_cmd_tool.call(
                '{"command": "echo hello", "timeout": 60, "justification": "boundary test"}'
            )

            mock_exec.assert_called_once()
            assert 'output' in result

    def test_explicit_async_mode_true_ignores_timeout_threshold(self, shell_cmd_tool, mock_tracker):
        """async_mode=true with any timeout → always async."""
        from agent_cascade.async_shell import AsyncShellTracker

        tracker = AsyncShellTracker(pool=None)
        tracker.launch = MagicMock(return_value=(1, 12345))

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        # Even with timeout=1, async_mode=true forces async path
        result = shell_cmd_tool.call(
            '{"command": "echo hello", "async_mode": true, "timeout": 1, "justification": "explicit async"}'
        )

        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result


# ============================================================================
# Edge cases / integration-style tests
# ============================================================================

class TestEdgeCases:
    """Additional edge case tests for async shell_cmd."""

    def test_wait_with_no_heartbeat_interval_uses_default_timeout(self, shell_cmd_tool):
        """__wait with heartbeat_interval=-1 uses default 30s timeout."""
        from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask

        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(
            tool_id=1,
            agent_name='test_agent',
            command='sleep 100',
            pid=99999,
            completed=False,
            heartbeat_interval=-1.0,  # No heartbeats
        )
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][1] = task

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        # Mock time to verify timeout value without waiting
        current_time = [1000.0]
        elapsed = [0.0]

        def fake_time():
            return current_time[0]

        def fake_sleep(seconds):
            current_time[0] += seconds
            elapsed[0] += seconds

        fake_time_module = MagicMock()
        fake_time_module.time.side_effect = fake_time
        fake_time_module.sleep.side_effect = fake_sleep

        with patch.dict('sys.modules', {'time': fake_time_module}):
            result = shell_cmd_tool.call(
                '{"command": "__wait", "tool_id": 1, "async_mode": true}'
            )

        # Should use default timeout (30s), not hang forever
        assert elapsed[0] >= 29.0, f"__wait waited {elapsed[0]:.1f}s, should be ~30s"
        assert elapsed[0] <= 31.0, f"__wait waited {elapsed[0]:.1f}s, should be ~30s"
        assert 'No new output' in result

    def test_status_command_works_without_justification(self, shell_cmd_tool):
        """__status is a control command that doesn't need justification."""
        from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask

        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(
            tool_id=5,
            agent_name='test_agent',
            command='long running task',
            pid=55555,
            completed=False,
            heartbeat_interval=5.0,
            stdout_lines=['line1', 'line2'],
        )
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][5] = task

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "__status", "tool_id": 5, "async_mode": true}'
        )

        assert '⟨shell_cmd status⟩' in result
        assert 'Tool ID: 5' in result
        assert 'line1' in result or 'line2' in result or 'running' in result.lower()

    def test_ctrl_c_without_justification(self, shell_cmd_tool):
        """__ctrl_c is a control command that doesn't need justification."""
        from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask

        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(
            tool_id=3,
            agent_name='test_agent',
            command='sleep 100',
            pid=33333,
            completed=False,
        )
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][3] = task

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "__ctrl_c", "tool_id": 3, "async_mode": true}'
        )

        # Should not raise ValueError about justification
        assert 'ValueError' not in result
        assert 'Tool ID: 3' in result or 'Ctrl+C sent' in result or 'Failed' in result

    def test_heartbeat_update_without_justification(self, shell_cmd_tool):
        """__heartbeat=N is a control command that doesn't need justification."""
        from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask

        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(
            tool_id=4,
            agent_name='test_agent',
            command='sleep 100',
            pid=44444,
            completed=False,
            heartbeat_interval=10.0,
        )
        with tracker._lock:
            if 'test_agent' not in tracker._tasks:
                tracker._tasks['test_agent'] = {}
            tracker._tasks['test_agent'][4] = task

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call(
            '{"command": "__heartbeat=5", "tool_id": 4, "async_mode": true}'
        )

        assert 'ValueError' not in result
        assert 'updated' in result.lower() or 'Tool ID: 4' in result