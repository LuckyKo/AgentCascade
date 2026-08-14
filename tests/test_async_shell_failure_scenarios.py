"""Failure scenario tests for async shell command handling.

Tests cover:
- Process termination and cleanup of killed/zombie processes
- Timeout behavior with real long-running processes
- Stderr capture on real process failures
- Edge cases in kill and poll behavior

Uses real process execution where possible, mocks only for internal state checks.
"""

import os
import sys
import subprocess
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock

import pytest

from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask


# ============================================================================
# Helpers
# ============================================================================

def _make_pool():
    """Create a mock pool that collects messages."""
    pool = MagicMock()
    pool.messages = []
    pool.enqueue_message = lambda agent, msg: pool.messages.append((agent, msg))
    pool.llm_cfg = {}
    return pool


def _platform_long_running_cmd(duration=60):
    """Return a long-running command appropriate for this platform."""
    if os.name == 'nt':
        return f'ping -n {duration + 1} 127.0.0.1 >nul 2>&1'
    else:
        return f'sleep {duration}'


def _platform_fail_cmd():
    """Return a command that fails with non-zero exit and writes to stderr."""
    if os.name == 'nt':
        return 'cmd /c "echo error message >&2 && exit /b 42"'
    else:
        return 'sh -c "echo error message >&2; exit 42"'


# ============================================================================
# Killed/zombie process cleanup (real processes)
# ============================================================================

class TestKilledProcessCleanup:
    """Verify killed/zombie processes are properly cleaned up using real execution."""

    def test_killed_process_is_removed_from_active_tasks(self):
        """After kill_task, the task is no longer reported as active."""
        pool = _make_pool()
        tracker = AsyncShellTracker(pool=pool)

        tool_id, _, _, completed_early, _ = tracker.launch(
            agent_name='test_agent',
            command=_platform_long_running_cmd(),
            heartbeat_interval=-1,
            timeout=3600,
        )
        assert not completed_early

        # Let tracking thread initialize the task with PID
        time.sleep(0.5)

        # Process should be active before kill
        assert tracker.has_active_tasks('test_agent') is True

        result = tracker.kill_task('test_agent', tool_id)
        assert 'Shell killed' in result, f"Kill failed: {result}"

        # After kill returns, task should no longer be active.
        # Use a poll loop instead of a bare sleep — the tracking thread needs time
        # to detect process death and remove the task from _tasks dict (finally block).
        # On loaded systems this can take >0.3s, so we retry up to 2 seconds.
        deadline = time.time() + 2.0
        while tracker.has_active_tasks('test_agent'):
            if time.time() >= deadline:
                break
            time.sleep(0.1)
        assert not tracker.has_active_tasks('test_agent'), \
            "Task still reported as active after kill_task returned"

    def test_killed_process_no_longer_sends_heartbeats(self):
        """A killed process stops sending heartbeats immediately."""
        pool = _make_pool()
        tracker = AsyncShellTracker(pool=pool)

        tool_id, _, _, completed_early, _ = tracker.launch(
            agent_name='test_agent',
            command=_platform_long_running_cmd(),
            heartbeat_interval=0.5,  # Fast heartbeats for test
            timeout=3600,
        )
        assert not completed_early

        time.sleep(1.0)  # Allow some heartbeats

        hb_before = sum(1 for m in pool.messages if 'heartbeat' in m[1].lower())
        assert hb_before > 0, "Should have received heartbeats before kill"

        tracker.kill_task('test_agent', tool_id)

        # Wait long enough that we'd see more heartbeats if process were alive
        time.sleep(2.0)

        hb_after = sum(1 for m in pool.messages if 'heartbeat' in m[1].lower())
        assert hb_after == hb_before, \
            f"Expected no more heartbeats after kill, got {hb_after - hb_before}"

    def test_killed_process_actually_terminated_on_os(self):
        """Verify the killed process is actually gone from the OS process table."""
        if os.name != 'nt':
            pytest.skip("OS-level verification is Windows-specific")

        pool = _make_pool()
        tracker = AsyncShellTracker(pool=pool)

        tool_id, _, _, completed_early, _ = tracker.launch(
            agent_name='test_agent',
            command='ping -t 127.0.0.1 >nul 2>&1',
            heartbeat_interval=-1,
            timeout=3600,
        )
        assert not completed_early

        time.sleep(0.5)

        task = tracker._get_task('test_agent', tool_id)
        pid = task.pid if task else None
        assert pid is not None and pid > 0, f"PID not set: {pid}"

        # Kill the task
        tracker.kill_task('test_agent', tool_id)

        time.sleep(0.5)

        # Verify PID is gone via tasklist
        proc_info = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
            capture_output=True, text=True, timeout=5
        )
        assert str(pid) not in proc_info.stdout.strip(), \
            f"Process {pid} still running after kill. Output: {proc_info.stdout[:200]}"


# ============================================================================
# Timeout behavior (real processes)
# ============================================================================

class TestTimeoutBehavior:
    """Verify timeout handling with real long-running processes."""

    def test_timeout_kills_long_running_process(self):
        """A process exceeding its timeout is killed automatically."""
        pool = _make_pool()
        tracker = AsyncShellTracker(pool=pool)

        tool_id, _, _, completed_early, _ = tracker.launch(
            agent_name='test_agent',
            command=_platform_long_running_cmd(duration=60),
            heartbeat_interval=-1,
            timeout=2,  # Short timeout
        )
        assert not completed_early

        time.sleep(0.5)
        task = tracker._get_task('test_agent', tool_id)
        assert task is not None and not task.completed

        # Wait for timeout to trigger kill
        time.sleep(3.0)

        # Task should now be completed (by timeout kill).
        # Note: timeout sets completed=True but NOT killed=True — only kill_task sets killed.
        with task._lock:
            assert task.completed is True, "Task should be completed after timeout"
            # Return code should be non-zero since process was killed by timeout
            assert task.return_code is not None and task.return_code != 0, \
                f"Expected non-zero exit code after timeout kill, got {task.return_code}"

    def test_timeout_completion_message_sent(self):
        """After timeout kills a process, a completion message is sent to the pool."""
        pool = _make_pool()
        tracker = AsyncShellTracker(pool=pool)

        tracker.launch(
            agent_name='test_agent',
            command=_platform_long_running_cmd(duration=60),
            heartbeat_interval=-1,
            timeout=2,
        )

        # Wait for timeout and completion message
        time.sleep(4.0)

        completion_msgs = [m for m in pool.messages if 'completed' in m[1].lower() or 'killed' in m[1].lower()]
        assert len(completion_msgs) > 0, \
            f"Expected completion/killed message after timeout. Messages: {pool.messages}"


# ============================================================================
# Stderr capture on real process failures
# ============================================================================

class TestStderrCapture:
    """Verify stderr is captured correctly when real processes fail."""

    def test_stderr_captured_on_failure(self):
        """Stderr output from a failing command is captured in task output."""
        pool = _make_pool()
        tracker = AsyncShellTracker(pool=pool)

        cmd = _platform_fail_cmd()
        tool_id, _, early_output, completed_early, return_code = tracker.launch(
            agent_name='test_agent',
            command=cmd,
            heartbeat_interval=-1,
            timeout=10,
        )

        # Fast commands may complete during launch wait
        if completed_early:
            assert return_code == 42, f"Expected exit code 42, got {return_code}"
            assert early_output is not None and len(early_output) > 0, \
                "Early output should contain stderr for failed command"
            assert any('error message' in line.lower() for line in early_output), \
                f"Stderr 'error message' not found in early output: {early_output}"
        else:
            # Wait for completion
            time.sleep(1.0)
            task = tracker._get_task('test_agent', tool_id)
            with task._lock:
                assert task.completed is True, "Task should be completed"
                assert task.return_code == 42, f"Expected exit code 42, got {task.return_code}"
                all_output = task.stdout_lines + (task.stderr_lines if hasattr(task, 'stderr_lines') else [])
                assert any('error message' in line.lower() for line in all_output), \
                    f"Stderr 'error message' not found in output: {all_output}"

    def test_nonzero_exit_code_recorded(self):
        """A failing command's non-zero exit code is recorded on the task."""
        pool = _make_pool()
        tracker = AsyncShellTracker(pool=pool)

        tool_id, _, early_output, completed_early, return_code = tracker.launch(
            agent_name='test_agent',
            command=_platform_fail_cmd(),
            heartbeat_interval=-1,
            timeout=10,
        )

        if completed_early:
            assert return_code == 42, f"Expected exit code 42 from early completion, got {return_code}"
        else:
            time.sleep(1.0)
            task = tracker._get_task('test_agent', tool_id)
            with task._lock:
                assert task.completed is True
                assert task.return_code == 42, f"Expected exit code 42, got {task.return_code}"


# ============================================================================
# Edge cases in kill behavior (minimal mocks for internal state)
# ============================================================================

class TestKillEdgeCases:
    """Test edge cases in kill behavior that are hard to exercise with real processes."""

    def test_kill_nonexistent_task_returns_error(self):
        """kill_task returns an error message for a nonexistent task."""
        tracker = AsyncShellTracker(pool=None)

        result = tracker.kill_task('test_agent', 999)
        assert 'No running shell found' in result

    def test_kill_already_finished_task(self):
        """kill_task handles already-finished tasks gracefully.

        When a command completes very fast, the task may be cleaned up from _tasks
        before kill_task is called, resulting in 'No running shell found'. This is
        acceptable behavior — the important thing is no crash occurs.
        """
        tracker = AsyncShellTracker(pool=None)

        # Launch a command that completes immediately
        tool_id, _, early_output, completed_early, return_code = tracker.launch(
            agent_name='test_agent',
            command='echo done' if os.name != 'nt' else 'cmd /c echo done',
            heartbeat_interval=-1,
            timeout=5,
        )

        # Wait for task to be fully set up and cleaned up if completed early
        time.sleep(0.5)

        # kill_task should not crash — either finds the finished task or reports not found
        result = tracker.kill_task('test_agent', tool_id)
        assert result is not None, "kill_task should return a message"
        # Acceptable outcomes: already finished, or task cleaned up (not found)
        assert ('finished' in result.lower() or 'no running shell' in result.lower()), \
            f"Unexpected kill_task result for finished command: {result}"
