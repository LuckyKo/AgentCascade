"""BUG_0156 regression tests — async shell timeout-kill must deliver AND wake.

Field bug: an async ``shell_cmd`` that timed out was killed, ``task.completed``
flipped to True, and the SLEEPING agent immediately saw "no pending tools"
(``has_active_tasks`` → False) even though the completion message had not been
enqueued yet (the drain-thread joins were blocked on surviving child processes
and taskkill had failed with rc=128). The agent went COMPLETING → IDLE, and the
late "Timed out" result sat unprocessed in its queue until the next user message.

The tests here pin both halves of the fix:

1. ``has_active_tasks()`` keeps reporting the task as active until the completion
   result is actually handed to the pool (``completion_delivered``).
2. ``_deliver_completion()`` enqueues the result and then relaunches an IDLE
   agent so a result that arrives after ``run()`` exited is still acted on.

Deterministic by design: the spawn/poll/kill path is stubbed and the drain
threads are held open by an Event, so no real processes are involved.
"""

import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_cascade.async_shell import AsyncShellTask, AsyncShellTracker


def _add_task(tracker, task, agent_name='Maine'):
    """Register a task in the tracker's live dict."""
    with tracker._lock:
        tracker._tasks.setdefault(agent_name, {})[task.tool_id] = task


def _make_recording_pool():
    """Minimal pool double that records enqueue calls in order with relaunches."""
    events = []
    pool = SimpleNamespace(
        events=events,
        enqueue_message=lambda agent, msg: events.append(('enqueue', agent, msg)),
    )
    return pool


def _wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# ============================================================================
# has_active_tasks(): undelivered results keep the task "pending"
# ============================================================================


class TestHasActiveTasksUntilResultDelivered:

    def test_completed_task_still_active_until_completion_delivered(self):
        """A finished-but-undelivered task is active; delivery releases it (BUG_0156)."""
        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(tool_id=1, agent_name='Maine', command='build')
        with task._lock:
            task.completed = True
            task.return_code = 1
        _add_task(tracker, task)

        # The process is dead but the result has not reached the queue yet — the
        # SLEEPING agent must NOT be told there is nothing pending.
        assert tracker.has_active_tasks('Maine') is True

        tracker._mark_completion_delivered(task)
        assert tracker.has_active_tasks('Maine') is False

    def test_running_task_still_active(self):
        """Sanity: an unfinished task remains active regardless of delivery flag."""
        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(tool_id=1, agent_name='Maine', command='build')
        _add_task(tracker, task)

        assert tracker.has_active_tasks('Maine') is True
        assert tracker.has_active_tasks('Nobody') is False

    def test_mark_completion_delivered_none_is_noop(self):
        """The helper tolerates a missing task (defensive)."""
        AsyncShellTracker._mark_completion_delivered(None)


# ============================================================================
# _deliver_completion(): enqueue first, then wake an IDLE agent
# ============================================================================


class TestDeliverCompletionWakesIdleAgent:

    def test_enqueues_result_then_relaunches_idle_agent(self):
        """Result is queued BEFORE the relaunch so the woken run() finds it."""
        pool = _make_recording_pool()
        tracker = AsyncShellTracker(pool=pool)

        with patch('agent_cascade.utils.wakeup_helpers.relaunch_idle_agent') as mock_relaunch:
            mock_relaunch.side_effect = lambda p, name: pool.events.append(('relaunch', name)) or True
            tracker._deliver_completion('Maine', '⟨shell_cmd completed⟩ Timed out')

        assert pool.events == [
            ('enqueue', 'Maine', '⟨shell_cmd completed⟩ Timed out'),
            ('relaunch', 'Maine'),
        ], f"Enqueue must precede relaunch, got {pool.events}"
        mock_relaunch.assert_called_once_with(pool, 'Maine')

    def test_relaunch_failure_does_not_break_delivery(self):
        """A relaunch exception is swallowed; the result is still enqueued."""
        pool = _make_recording_pool()
        tracker = AsyncShellTracker(pool=pool)

        with patch('agent_cascade.utils.wakeup_helpers.relaunch_idle_agent',
                   side_effect=RuntimeError('boom')):
            tracker._deliver_completion('Maine', 'result')

        assert pool.events == [('enqueue', 'Maine', 'result')]

    def test_no_pool_is_a_noop(self):
        """Without a pool there is nowhere to deliver and nothing to wake."""
        tracker = AsyncShellTracker(pool=None)
        tracker._deliver_completion('Maine', 'result')  # must not raise


# ============================================================================
# End-to-end tracking path: timeout-kill + stuck drains + failed tree kill
# ============================================================================


class TestTimeoutKillWithStuckDrains:

    def test_message_delivered_and_agent_active_while_drains_block(self):
        """Timeout-kill with surviving children: result still delivered, agent not idle.

        Mirrors the field timeline: taskkill fails, orphaned children keep the pipe
        drain threads blocked, so the completion message is delayed. Throughout that
        window has_active_tasks() must remain True (no premature COMPLETING→IDLE),
        and the completion must still be enqueued + the agent woken afterwards.
        """
        pool = _make_recording_pool()
        tracker = AsyncShellTracker(pool=pool)

        task = AsyncShellTask(tool_id=3, agent_name='Maine', command='ninja build', timeout=0)
        _add_task(tracker, task)
        # launch() is not involved here — release the tracking thread's gate.
        task.launch_check_done.set()

        proc = MagicMock()
        proc.pid = 24288
        proc.returncode = 1
        proc.poll.return_value = None  # alive until the (simulated) kill

        # Drain threads that stay blocked as long as orphaned children hold the pipes.
        drain_release = threading.Event()

        def stuck_drain():
            drain_release.wait(timeout=30)

        t_out = threading.Thread(target=stuck_drain, daemon=True)
        t_err = threading.Thread(target=stuck_drain, daemon=True)
        t_out.start()
        t_err.start()

        def fake_spawn(agent_name, tool_id, command, cwd):
            with task._lock:
                task.process = proc
                task._drain_t_out = t_out
                task._drain_t_err = t_err
            return task

        tracker_thread = threading.Thread(
            target=tracker._track_task,
            args=('Maine', 3, 'ninja build', None),
            daemon=True,
        )

        # Long join timeout so the "message not yet queued" window stays open while
        # the test asserts. The real fix is what we assert on, not the duration.
        with patch.object(tracker, '_spawn_process', side_effect=fake_spawn), \
                patch.object(tracker, '_kill_process_tree') as mock_kill, \
                patch('agent_cascade.async_shell_pkg.tracker.DRAIN_THREAD_JOIN_TIMEOUT', 30), \
                patch('agent_cascade.utils.wakeup_helpers.relaunch_idle_agent') as mock_relaunch:
            mock_relaunch.side_effect = lambda p, name: pool.events.append(('relaunch', name)) or True

            tracker_thread.start()

            # The tracker marks the task completed BEFORE joining the drain threads.
            assert _wait_for(lambda: _task_completed(task)), \
                'tracking thread never marked the timed-out task completed'
            mock_kill.assert_called_once()

            # We are now inside the drain-join window: process dead, no message yet.
            assert not any(e[0] == 'enqueue' for e in pool.events), \
                f'completion message was enqueued before the drain join (test premise broken): {pool.events}'
            assert tracker.has_active_tasks('Maine') is True, \
                'BUG_0156: agent was told "no pending tools" before the result was delivered'

            # Release the stuck drains -> tracking thread delivers and exits.
            drain_release.set()
            tracker_thread.join(timeout=10)

        assert not tracker_thread.is_alive(), 'tracking thread did not finish after drain release'

        enqueued = [e for e in pool.events if e[0] == 'enqueue']
        assert len(enqueued) == 1, f'expected exactly one completion message, got {pool.events}'
        msg = enqueued[0][2]
        assert '⟨shell_cmd completed⟩' in msg
        assert 'Timed out after' in msg, f'expected timeout status, got {msg!r}'

        relaunches = [e for e in pool.events if e[0] == 'relaunch']
        assert relaunches == [('relaunch', 'Maine')], \
            f'IDLE-wakeup relaunch missing/misordered: {pool.events}'

        assert task.completion_delivered is True
        assert tracker.has_active_tasks('Maine') is False
        assert tracker._get_task('Maine', 3) is None, 'task should be reaped after delivery'


def _task_completed(task):
    with task._lock:
        return task.completed


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
