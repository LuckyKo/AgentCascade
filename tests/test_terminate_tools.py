"""Tests for the UI "Terminate tool" button (``terminate_tools`` WS command).

The terminate_tools path REUSES the existing ``kill_all`` primitives — the same
calls the dismissal path already makes (``pool/lifecycle.py``) — to kill
in-flight background tool runners (async shell + CLI-agent) for an instance
while leaving the instance ALIVE and resumable. No new kill machinery is
introduced.

Layers (mirror the WS-seam style of tests/test_manual_kv_buttons.py):
- **Wiring:** drive the real ``WsMessageHandler.dispatch`` with a mocked pool;
  assert BOTH trackers' ``kill_all`` fire for the instance (``assert_any_call``,
  non-vacuous), the feedback is enqueued into the (alive) instance, and the
  sticky-slot permit is NOT released (the instance stays resumable).
- **Effect:** drive a REAL tracker with a stubbed running process; assert the
  kill flag is set (deactivation is gated on completion delivery, modelled).
- **Contract:** the pre-existing CLI-agent permit-release path releases the
  container-concurrency semaphore exactly once (the D4 leak-free precondition
  terminate_tools relies on) — exercised, not re-implemented.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

from agent_cascade.ws_handlers import WsMessageHandler, WsEnvelope, MsgType
from agent_cascade.async_shell_pkg.tracker import AsyncShellTracker
from agent_cascade.async_shell_pkg.task import AsyncShellTask
from agent_cascade.async_shell_pkg.cli_agent_tracker import CliAgentTracker


class _FakePopen:
    """Stand-in for a running subprocess: ``poll()`` returns None (still alive)."""

    def __init__(self):
        self._dead = False

    def poll(self):
        return None if not self._dead else 0

    def kill(self):
        self._dead = True


def _make_handler(pool):
    """A WsMessageHandler wired to a (mocked) pool with a recording broadcast_fn."""
    frames = []

    async def _broadcast(frame):
        frames.append(frame)

    session = {'session_name': 'X', 'generating': False, 'generation_id': 0}
    handler = WsMessageHandler(
        session=session,
        agent_pool=pool,
        agents=[],
        send_queue=None,
        broadcast_fn=_broadcast,
        build_state_fn=lambda **kw: {},
        start_gen_fn=lambda: None,
        session_lock=threading.Lock(),
        app=None,
    )
    return handler, frames


def _make_pool(*, parent_instance, shell_count=0, cli_count=0):
    """A mocked pool exposing real (inspectable) trackers + a fixed get_instance.

    ``parent_instance`` is the value of the fake instance's ``parent_instance``
    (None => root orchestrator)."""
    inst = MagicMock()
    inst.parent_instance = parent_instance
    shell = MagicMock()
    shell.kill_all.return_value = shell_count
    cli = MagicMock()
    cli.kill_all.return_value = cli_count
    pool = MagicMock()
    pool.get_instance.return_value = inst
    pool._async_shell_tracker = shell
    pool._cli_agent_tracker = cli
    return pool, shell, cli


# ────────────────────────────────────────────────────────────────────────────
# Wiring layer — the handler routes to both trackers and keeps the instance alive
# ────────────────────────────────────────────────────────────────────────────


class TestWiring:

    def test_calls_both_trackers_for_instance(self):
        """Both kill_all calls fire for the named instance; call lists non-empty first."""
        pool, shell, cli = _make_pool(parent_instance='X-parent', shell_count=2, cli_count=1)
        handler, frames = _make_handler(pool)
        asyncio.run(handler.dispatch({'type': 'terminate_tools', 'instance_name': 'X'}))

        # Both trackers' kill_all must have been invoked with the instance name.
        # assert_any_call is non-vacuous: it fails if NO recorded call used 'X'.
        shell.kill_all.assert_any_call('X')
        cli.kill_all.assert_any_call('X')
        # Feedback is enqueued into the (alive) instance, not a dismissal.
        pool.enqueue_message.assert_called_once()
        assert pool.enqueue_message.call_args.args[0] == 'X'
        # BUG_0352 Finding 3: the transient INFO frame is broadcast before the
        # state refresh so the clicked-button result is visible immediately.
        assert frames, 'expected at least one broadcast frame'
        assert frames[0][WsEnvelope.TYPE] == MsgType.INFO

    def test_unknown_instance_is_noop(self):
        """get_instance -> None: no kill, no enqueue, no raise (idle/unknown twin)."""
        pool, shell, cli = _make_pool(parent_instance='X-parent')
        pool.get_instance.return_value = None
        handler, _ = _make_handler(pool)
        asyncio.run(handler.dispatch({'type': 'terminate_tools', 'instance_name': 'ghost'}))
        shell.kill_all.assert_not_called()
        cli.kill_all.assert_not_called()
        pool.enqueue_message.assert_not_called()

    def test_root_orchestrator_kill_all_is_called(self):
        """BUG_0352 Finding 1: the root orchestrator IS a valid target for
        terminate_tools (it is non-destructive; the dominant killable runners
        are keyed by the caller, which is the root). kill_all fires for root."""
        pool, shell, cli = _make_pool(parent_instance=None, shell_count=1, cli_count=1)
        handler, _ = _make_handler(pool)
        asyncio.run(handler.dispatch({'type': 'terminate_tools', 'instance_name': 'root'}))
        shell.kill_all.assert_any_call('root')
        cli.kill_all.assert_any_call('root')
        pool.enqueue_message.assert_called_once()

    def test_does_not_release_slot_permit(self):
        """The instance stays resumable: release_slot_permit is NOT invoked.

        BUG_0352 Finding 4: this is a forward-regression guard against a future
        local-import of release_slot_permit inside the terminate_tools handler.
        It has zero active-path discriminating power today (no code path in
        terminate_tools calls release_slot_permit) — kept as documentation of
        the invariant that the instance remains alive and resumable.
        """
        pool, _, _ = _make_pool(parent_instance='X-parent', shell_count=1, cli_count=1)
        handler, _ = _make_handler(pool)
        with patch('agent_cascade.slot_queue.release_slot_permit') as m_release:
            asyncio.run(handler.dispatch({'type': 'terminate_tools', 'instance_name': 'X'}))
        m_release.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# Effect layer — the kill is real (the flag is set), not just "called"
# ────────────────────────────────────────────────────────────────────────────


class TestShellEffect:

    def test_kill_all_sets_kill_flag(self):
        """A REAL tracker: kill_all sets the kill flag. Deactivation is gated on the
        tracking thread delivering the completion (modelled below), not caused by the
        kill itself — kill_all is fire-and-forget."""
        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(tool_id=1, agent_name='X', command='sleep 100')
        proc = _FakePopen()
        with task._lock:
            task.process = proc
        with tracker._lock:
            tracker._tasks['X'] = {1: task}

        with patch.object(tracker, '_kill_process_tree') as kill_pt:
            count = tracker.kill_all('X')

        assert count == 1
        with task._lock:
            assert task.killed is True
        # kill_all is fire-and-forget: it does NOT itself tear down the process —
        # the tracking thread does — so the tree-kill is not invoked synchronously.
        kill_pt.assert_not_called()
        # Model the tracking-thread delivery: once the completion is enqueued the
        # task is no longer active.
        with task._lock:
            task.completion_delivered = True
        assert tracker.has_active_tasks('X') is False


# ────────────────────────────────────────────────────────────────────────────
# Contract layer — the pre-existing CLI permit-release path (D4 precondition)
# ────────────────────────────────────────────────────────────────────────────


class TestCliAgentPermitRelease:

    def test_sem_released_exactly_once_and_nullified(self):
        """Existing-behavior contract (D4): the CLI container permit is released exactly
        once when the run finalizes, then the guard is nullified so a second release
        cannot happen. This is the precondition that makes terminate_tools leak-free.
        It exercises the pre-existing _record_completed_run, NOT the new terminate path
        (the wiring test above already proves cli.kill_all fires for the instance, and
        CliAgentTracker.kill_all is the same inherited method proven by TestShellEffect)."""
        tracker = CliAgentTracker(pool=None)
        task = AsyncShellTask(tool_id=1, agent_name='X', command='run-123')
        sem = threading.Semaphore(1)
        sem.acquire()  # model the run holding the container permit (now 0 available)
        task._cli_sem = sem
        task.return_code = 0

        tracker._record_completed_run('X', 1, task)

        # The single permit was released exactly once, then the guard nullified it.
        assert sem.acquire(blocking=False) is True    # the one release
        assert sem.acquire(blocking=False) is False   # no second release
        assert task._cli_sem is None
