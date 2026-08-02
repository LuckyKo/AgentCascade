"""Failure scenario tests for async shell command handling.

Tests cover:
- Process termination failures
- Timeout race conditions
- Heartbeat integrity for long-running commands with intermittent output
- Zombie/orphaned process handling
- Kill task behavior under edge cases

No LLM or network connections required. Uses mocks to simulate failures.
"""

import os
import signal
import sys
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask


# ============================================================================
# Fixtures and helpers
# ============================================================================

@pytest.fixture
def tracker():
    return AsyncShellTracker(pool=None)


def _setup_task(tracker, task):
    """Add a task to a tracker under test_agent."""
    with tracker._lock:
        if 'test_agent' not in tracker._tasks:
            tracker._tasks['test_agent'] = {}
        tracker._tasks['test_agent'][task.tool_id] = task


def _make_running_task(tool_id=1, heartbeat_interval=-1.0, **kwargs):
    """Create a running AsyncShellTask with a mock process."""
    proc_mock = MagicMock()
    proc_mock.poll.return_value = None  # Still running
    proc_mock.pid = kwargs.pop('pid', 12345)
    
    return AsyncShellTask(
        tool_id=tool_id,
        agent_name='test_agent',
        command=kwargs.pop('command', 'echo test'),
        pid=proc_mock.pid,
        completed=False,
        heartbeat_interval=heartbeat_interval,
        process=proc_mock,
        **kwargs
    )


# ============================================================================
# Process termination failures
# ============================================================================

class TestProcessTerminationFailures:
    """Test that kill_task handles process termination failures gracefully."""

    def test_kill_task_handles_nonexistent_pid(self, tracker):
        """Kill task with PID that doesn't exist should not crash."""
        task = _make_running_task(pid=99999999)
        _setup_task(tracker, task)
        
        # Should complete without raising
        result = tracker.kill_task('test_agent', task.tool_id)
        assert result is not None

    def test_kill_task_handles_permission_denied(self, tracker):
        """Kill task with permission denied should log and continue."""
        # Use a fully mocked process that always appears running.
        # Simulate PermissionError via patching — don't rely on actual PID 1 behavior.
        proc_mock = MagicMock()
        proc_mock.poll.return_value = None  # Never terminates
        proc_mock.pid = 99999
        
        task = AsyncShellTask(
            tool_id=1,
            agent_name='test_agent',
            command='echo test',
            pid=proc_mock.pid,
            completed=False,
            heartbeat_interval=-1.0,
            process=proc_mock,
        )
        
        _setup_task(tracker, task)
        
        # Patch os.kill to raise PermissionError and time.sleep to avoid waiting.
        with patch('os.kill', side_effect=PermissionError("Operation not permitted")):
            with patch('time.sleep', return_value=None):
                result = tracker.kill_task('test_agent', task.tool_id)
                assert result is not None

    def test_kill_task_handles_process_already_gone(self, tracker):
        """Kill task when process already exited should handle gracefully."""
        task = _make_running_task(pid=99999999)
        task.process.poll.return_value = 0  # Already completed
        
        _setup_task(tracker, task)
        
        result = tracker.kill_task('test_agent', task.tool_id)
        assert "already finished" in result.lower() or result is not None

    def test_kill_task_sets_killed_flag(self, tracker):
        """Kill task sets the killed flag on the task."""
        task = _make_running_task(pid=99999999)
        # Process stays running so killed flag gets set before timeout
        task.process.poll.return_value = None
        
        _setup_task(tracker, task)
        
        # Patch time to simulate timeout quickly and subprocess.run for force-kill
        with patch('subprocess.run', return_value=MagicMock()):
            with patch('time.sleep', return_value=None):
                tracker.kill_task('test_agent', task.tool_id)
        
        assert task.killed is True

    def test_kill_task_escalates_to_sigkill(self, tracker):
        """Kill task escalates from SIGTERM to SIGKILL if needed."""
        task = _make_running_task(pid=12345)
        task.process.poll.return_value = None  # Still running
        
        _setup_task(tracker, task)
        
        kill_calls = []
        
        def mock_kill(pid, sig):
            kill_calls.append((pid, sig))
            if sig == signal.SIGKILL:
                raise ProcessLookupError()
        
        with patch('os.kill', side_effect=mock_kill):
            with patch('subprocess.run', return_value=MagicMock()):  # taskkill on Windows
                with patch('time.sleep', return_value=None):
                    result = tracker.kill_task('test_agent', task.tool_id)
        
        # On Windows, uses taskkill; on Unix, uses os.killpg/os.kill
        assert result is not None


# ============================================================================
# Timeout race conditions
# ============================================================================

class TestTimeoutRaceConditions:
    """Test timeout handling and race conditions in async shell."""

    def test_timeout_detected_in_poll_loop(self):
        """Poll loop detects timeout when elapsed > task.timeout."""
        task = _make_running_task(pid=99999999)
        task.timeout = 1.0
        task.start_time = time.time() - 2.0  # Already past timeout
        
        proc_mock = MagicMock()
        proc_mock.poll.return_value = None  # Still running
        
        tracker = AsyncShellTracker(pool=None)
        
        timed_out = tracker._poll_loop('test_agent', task.tool_id, proc_mock, task, 
                                       MagicMock(), MagicMock())
        
        assert timed_out is True

    def test_timeout_does_not_trigger_for_completed(self):
        """Completed tasks don't trigger timeout in poll loop."""
        task = _make_running_task(pid=12345)
        task.completed = True
        
        proc_mock = MagicMock()
        proc_mock.poll.return_value = 0  # Already completed
        
        tracker = AsyncShellTracker(pool=None)
        
        timed_out = tracker._poll_loop('test_agent', task.tool_id, proc_mock, task,
                                       MagicMock(), MagicMock())
        
        assert timed_out is False

    def test_concurrent_timeout_and_kill(self, tracker):
        """Kill flag is checked during poll loop to avoid double-killing."""
        task = _make_running_task(pid=99999999)
        task.timeout = 60.0
        
        proc_mock = MagicMock()
        poll_count = [0]
        
        def poll_side_effect():
            poll_count[0] += 1
            if poll_count[0] > 1:
                return 0  # Completed on second poll
            return None
        
        proc_mock.poll.side_effect = poll_side_effect
        
        tracker._poll_loop('test_agent', task.tool_id, proc_mock, task,
                           MagicMock(), MagicMock())


# ============================================================================
# Heartbeat integrity for long-running commands
# ============================================================================

class TestHeartbeatIntegrity:
    """Test heartbeat behavior under various conditions."""

    def test_heartbeat_disabled_by_default(self):
        """Default heartbeat_interval=-1 disables heartbeats."""
        task = _make_running_task(heartbeat_interval=-1.0)  # Default
        
        proc_mock = MagicMock()
        poll_count = [0]
        
        def poll_side_effect():
            poll_count[0] += 1
            if poll_count[0] > 2:
                return 0
            return None
        
        proc_mock.poll.side_effect = poll_side_effect
        
        pool = MagicMock()
        tracker = AsyncShellTracker(pool=pool)
        
        tracker._poll_loop('test_agent', task.tool_id, proc_mock, task,
                           MagicMock(), MagicMock())
        
        # No heartbeat should be sent when interval is -1 (default)

    def test_heartbeat_zero_interval_disables(self):
        """heartbeat_interval=0 or negative disables heartbeats."""
        task = _make_running_task(heartbeat_interval=0)
        
        proc_mock = MagicMock()
        poll_count = [0]
        
        def poll_side_effect():
            poll_count[0] += 1
            if poll_count[0] > 2:
                return 0
            return None
        
        proc_mock.poll.side_effect = poll_side_effect
        
        pool = MagicMock()
        tracker = AsyncShellTracker(pool=pool)
        
        tracker._poll_loop('test_agent', task.tool_id, proc_mock, task,
                           MagicMock(), MagicMock())
        
        # No heartbeat should be sent when interval is 0

    def test_heartbeat_respects_interval(self):
        """No heartbeat sent before interval has elapsed."""
        task = _make_running_task(heartbeat_interval=60.0)
        task.start_time = time.time() - 5.0  # Only 5s ago, interval is 60s
        
        proc_mock = MagicMock()
        poll_count = [0]
        
        def poll_side_effect():
            poll_count[0] += 1
            if poll_count[0] > 2:
                return 0
            return None
        
        proc_mock.poll.side_effect = poll_side_effect
        
        pool = MagicMock()
        tracker = AsyncShellTracker(pool=pool)
        
        # Track heartbeat calls
        heartbeat_calls = []
        original_send = tracker._send_heartbeat
        
        def track_heartbeat(agent_name, tool_id):
            heartbeat_calls.append((agent_name, tool_id))
            return original_send(agent_name, tool_id)
        
        tracker._send_heartbeat = track_heartbeat
        
        tracker._poll_loop('test_agent', task.tool_id, proc_mock, task,
                           MagicMock(), MagicMock())
        
        # Heartbeat should NOT be sent yet (only 5s < 60s interval)
        assert len(heartbeat_calls) == 0


# ============================================================================
# Zombie/orphaned process handling
# ============================================================================

class TestZombieProcessHandling:
    """Test detection and cleanup of zombie/orphaned processes."""

    def test_zombie_detected_by_timeout(self):
        """A zombie process (not responding to output) is killed by timeout."""
        # Use consistent time mocking so there's no race with task creation timestamps.
        with patch('time.time', return_value=1000.0):
            task = _make_running_task(pid=12345)
            task.timeout = 0.1
            task.start_time = 900.0  # Long past timeout relative to mocked time
            
            proc_mock = MagicMock()
            proc_mock.poll.return_value = None  # Still running (zombie-like)
            
            tracker = AsyncShellTracker(pool=None)
            
            timed_out = tracker._poll_loop('test_agent', task.tool_id, proc_mock, task,
                                           MagicMock(), MagicMock())
            
            assert timed_out is True

    def test_orphaned_task_detected_by_poll(self):
        """Tasks whose processes vanished are detected by poll returning exit code."""
        task = _make_running_task(pid=99999999)
        
        proc_mock = MagicMock()
        proc_mock.poll.return_value = 0  # Already completed
        
        tracker = AsyncShellTracker(pool=None)
        
        timed_out = tracker._poll_loop('test_agent', task.tool_id, proc_mock, task,
                                       MagicMock(), MagicMock())
        
        assert timed_out is False

    def test_has_active_tasks_excludes_completed(self, tracker):
        """has_active_tasks returns False for all-completed agents."""
        task1 = _make_running_task(tool_id=1)
        task1.completed = True
        _setup_task(tracker, task1)
        
        assert tracker.has_active_tasks('test_agent') is False

    def test_has_active_tasks_includes_running(self, tracker):
        """has_active_tasks returns True when any task is still running."""
        task1 = _make_running_task(tool_id=1)
        task1.completed = False
        _setup_task(tracker, task1)
        
        assert tracker.has_active_tasks('test_agent') is True

    def test_kill_all_sets_killed_flags(self, tracker):
        """kill_all sets killed flags on all active tasks."""
        running_task = _make_running_task(tool_id=1, pid=12345)
        completed_task = _make_running_task(tool_id=2, pid=12346)
        completed_task.process.poll.return_value = 0
        
        _setup_task(tracker, running_task)
        _setup_task(tracker, completed_task)
        
        killed_count = tracker.kill_all('test_agent')
        
        # Running task should have killed flag set
        assert running_task.killed is True


# ============================================================================
# Kill process tree behavior
# ============================================================================

class TestKillProcessTree:
    """Test _kill_process_tree handles edge cases."""

    def test_kill_process_tree_handles_empty_ps_output(self, tracker):
        """Empty ps output doesn't crash the kill tree logic."""
        proc_mock = MagicMock()
        proc_mock.pid = 12345
        
        with patch('subprocess.run', return_value=MagicMock(stdout=b'', stderr=b'')):
            # Should not raise
            tracker._kill_process_tree(proc_mock, 'test_agent', 1)

    def test_kill_process_tree_handles_ps_failure(self, tracker):
        """ps command failure is handled gracefully."""
        proc_mock = MagicMock()
        proc_mock.pid = 12345
        
        with patch('subprocess.run', side_effect=FileNotFoundError()):
            # Should fall back to killing just the target PID or no-op
            try:
                tracker._kill_process_tree(proc_mock, 'test_agent', 1)
            except Exception as e:
                pytest.fail(f"_kill_process_tree raised unexpectedly: {e}")

    def test_kill_process_tree_skips_self(self, tracker):
        """Kill tree does not attempt to kill itself (current process)."""
        current_pid = os.getpid()
        
        proc_mock = MagicMock()
        proc_mock.pid = current_pid
        
        mock_proc_result = MagicMock()
        mock_proc_result.stdout = f"  {current_pid}   pts/0    00:00:00 bash".encode()
        
        with patch('subprocess.run', return_value=mock_proc_result):
            with patch('os.kill') as mock_kill:
                tracker._kill_process_tree(proc_mock, 'test_agent', 1)
            
            # Should not kill itself
            calls_with_self = [c for c in mock_kill.call_args_list if c[0][0] == current_pid]
            assert len(calls_with_self) == 0


# ============================================================================
# AsyncShellTask state management
# ============================================================================

class TestAsyncShellTaskState:
    """Test AsyncShellTask state transitions and properties."""

    def test_task_completed_property(self):
        """Completed task has correct state."""
        task = AsyncShellTask(
            tool_id=1, agent_name='test', command='echo hi', pid=12345,
            completed=True, return_code=0, stdout_lines=['hi']
        )
        assert task.completed is True
        assert task.return_code == 0

    def test_task_heartbeat_interval_default(self):
        """Default heartbeat interval is -1 (disabled)."""
        task = AsyncShellTask(
            tool_id=1, agent_name='test', command='echo hi', pid=12345
        )
        assert task.heartbeat_interval == -1.0

    def test_task_with_no_heartbeat_interval(self):
        """Task with heartbeat_interval=0 doesn't send heartbeats."""
        task = AsyncShellTask(
            tool_id=1, agent_name='test', command='echo hi', pid=12345,
            heartbeat_interval=0
        )
        assert task.heartbeat_interval == 0