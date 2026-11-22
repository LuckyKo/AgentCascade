"""BUG_0385 — async shell_cmd stall (wedge) watchdog.

The async path already closed a child on real exit (the ``_poll_loop`` liveness break) and
hard-tree-killed at the declared ``timeout`` (via ``_track_task`` -> ``_kill_process_tree``).
The ONLY gap was a *wedged-but-alive* child (alive, zero new stdout/stderr, internally
crashed) sat "running" until the deadline. This suite locks in the fix: enabling the
timeout-derived idle floor so a detectably-silent run closes BEFORE the deadline via the
already-built stall branch, while the timeout stays the unconditional top-down tree-kill
backstop.

All mocked tests run under the normal ``-n auto`` invocation (they touch no real PIDs). The
one real-process test is pinned ``@pytest.mark.serial`` (EXCLUDED from the default run by
pytest.ini; run alone with ``-m serial -o addopts="-n 0"``).
"""

import os
import subprocess
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent_cascade.async_shell import AsyncShellTask, AsyncShellTracker
from agent_cascade.async_shell_pkg import tracker as tracker_mod


def _setup_task(tracker, task):
    """Register a task under test_agent (mirrors test_async_shell_kill._setup_task)."""
    with tracker._lock:
        tracker._tasks.setdefault('test_agent', {})[task.tool_id] = task


class _FakeProc:
    """Minimal Popen stand-in: poll() driven by ``_rc`` (None = alive)."""

    def __init__(self, *, rc=None, pid=99999):
        self._rc = rc
        self.pid = pid
        self.returncode = rc

    def poll(self):
        return self._rc

    def set_finished(self, rc=0):
        self._rc = rc
        self.returncode = rc


def _make_task(tool_id=1, *, timeout=60, command='sleep 9999'):
    return AsyncShellTask(tool_id=tool_id, agent_name='test_agent', command=command,
                          pid=99999, timeout=timeout)


def _make_fake_spawn(task, proc):
    """Build a _spawn_process stub that assigns a fake proc + no drain threads under the lock."""
    def _fake_spawn(agent_name, tool_id, command, cwd):
        with task._lock:
            task.process = proc
            task._drain_t_out = None
            task._drain_t_err = None
    return _fake_spawn


# ──────────────────────────────────────────────────────────────────────────────
# Threshold derivation (pure logic — the policy knob). Locks the min(KILL_DEFAULT,
# FRACTION*timeout) math: the kill floor sits below timeout for realistic timeouts, the
# default caps it, and a non-positive default disables the watchdog entirely.
# ──────────────────────────────────────────────────────────────────────────────

def test_stall_kill_floor_is_below_timeout():
    tracker = AsyncShellTracker(pool=None)
    for tmo in (2, 60, 750, 1800, 3600):
        task = _make_task(timeout=tmo)
        kill = tracker._stall_kill_seconds(task)
        assert 0 < kill < tmo, f"kill={kill} not strictly below timeout={tmo}"
        warn = tracker._stall_warn_seconds(task)
        assert 0 < warn < kill  # warn always precedes kill


def test_stall_watchdog_disabled_when_default_le_zero():
    """Owner re-disables the watchdog by setting ASYNC_SHELL_STALL_KILL_DEFAULT <= 0 (legacy)."""
    tracker = AsyncShellTracker(pool=None)
    with patch.object(tracker_mod, 'ASYNC_SHELL_STALL_KILL_DEFAULT', 0.0):
        assert tracker._stall_kill_seconds(_make_task(timeout=600)) == 0.0
        assert tracker._stall_warn_seconds(_make_task(timeout=600)) == 0.0


def test_stall_kill_floor_capped_at_default():
    """For a large timeout the idle floor is capped at KILL_DEFAULT, still << timeout."""
    tracker = AsyncShellTracker(pool=None)
    kill = tracker._stall_kill_seconds(_make_task(timeout=3600))
    assert kill == 600.0  # KILL_DEFAULT cap dominates (min(KILL_DEFAULT, FRACTION*timeout))


# ──────────────────────────────────────────────────────────────────────────────
# (b) wedge close: the stall detector sets stall_kill_requested and _track_task routes it
#     to the tree-killer EXACTLY ONCE (alive-root mode), before the deadline. The poll loop
#     itself must never kill.
# ──────────────────────────────────────────────────────────────────────────────

def _drive_track_with_poll_result(tracker, task, *, timed_out=False, stall=False, proc):
    """Run _track_task end-to-end, stubbing spawn + poll loop to force a close branch.

    Returns the patched _deliver_completion mock so callers can assert on the DELIVERED payload
    (locking the close-branch -> header WIRING, not just a header-format call).
    """
    def _fake_poll(agent_name, tool_id, p, t, tout, terr):
        if stall:
            with t._lock:
                t.stall_kill_requested = True
                t.stall_seconds = 12.0
        return timed_out

    with patch.object(tracker, '_spawn_process', side_effect=_make_fake_spawn(task, proc)), \
         patch.object(tracker, '_poll_loop', side_effect=_fake_poll), \
         patch.object(tracker, '_deliver_completion') as deliver:
        tracker._track_task('test_agent', task.tool_id, task.command, None)
    return deliver


def test_wedged_child_closes_via_stall_branch_single_tree_kill():
    """(b) wedge → exactly one ALIVE tree-kill, rc=1, elapsed < timeout, AND the completion
    message _track_task ACTUALLY delivered carries the stall header — locking the
    stall_kill_requested -> stalled=True wiring, not just the header format."""
    tracker = AsyncShellTracker(pool=None)
    proc = _FakeProc(rc=None)               # wedged: alive, never exits
    task = _make_task(timeout=60)
    task.launch_check_done.set()
    _setup_task(tracker, task)

    start = time.time()
    with patch.object(tracker, '_kill_process_tree') as kill_tree, \
         patch.object(tracker, '_reap_completed_tree') as reap:
        deliver = _drive_track_with_poll_result(tracker, task, stall=True, proc=proc)
    elapsed = time.time() - start

    assert elapsed < task.timeout
    # Single tree-kill on the stall branch — alive-root mode (expect_root_exited not True).
    kill_tree.assert_called_once()
    assert kill_tree.call_args.kwargs.get('expect_root_exited') is not True
    # Alive-root kill does NOT go through the dead-root reaper.
    reap.assert_not_called()
    assert task.completed and task.return_code == 1

    # Load-bearing on the WIRING: assert on the payload _track_task actually delivered. The body
    # is built internally via _build_completion_message(stalled=task.stall_kill_requested), so
    # 'Stalled' here can ONLY appear if the stall flag propagated to stalled=True. A direct
    # _build_completion_header(stalled=True) call would lock the format but NOT this wiring.
    deliver.assert_called_once()
    delivered_body = deliver.call_args.args[1]
    assert 'Stalled:' in delivered_body and 'stall watchdog' in delivered_body
    # ...and it took the STALL path, not the timeout backstop.
    assert 'Timed out after' not in delivered_body


def test_undetectable_hang_still_uses_timeout_backstop_single_tree_kill():
    """(c) no stall trip → the timeout branch still hard-tree-kills exactly once."""
    tracker = AsyncShellTracker(pool=None)
    proc = _FakeProc(rc=None)
    task = _make_task(timeout=60)
    task.launch_check_done.set()
    _setup_task(tracker, task)

    with patch.object(tracker, '_kill_process_tree') as kill_tree:
        deliver = _drive_track_with_poll_result(tracker, task, timed_out=True, stall=False, proc=proc)

    kill_tree.assert_called_once()
    assert kill_tree.call_args.kwargs.get('expect_root_exited') is not True
    assert task.completed and task.return_code == 1
    # Assert on the DELIVERED payload (consistent with test b): locks the timeout-branch wiring,
    # not just the header format.
    delivered_body = deliver.call_args.args[1]
    assert 'Timed out after' in delivered_body and 'Stalled' not in delivered_body


def test_normal_completion_does_not_alive_kill():
    """(a) child exited → no ALIVE tree-kill; only the dead-root reaper runs."""
    tracker = AsyncShellTracker(pool=None)
    proc = _FakeProc(rc=0)                  # already dead
    proc.returncode = 7
    task = _make_task(timeout=60)
    task.launch_check_done.set()
    _setup_task(tracker, task)

    with patch.object(tracker, '_spawn_process', side_effect=_make_fake_spawn(task, proc)), \
         patch.object(tracker, '_poll_loop', return_value=False), \
         patch.object(tracker, '_kill_process_tree') as kill_tree, \
         patch.object(tracker, '_deliver_completion'):
        tracker._track_task('test_agent', task.tool_id, task.command, None)

    # Normal completion: only the dead-root reap (expect_root_exited=True), never an alive kill.
    alive_kills = [c for c in kill_tree.call_args_list
                   if c.kwargs.get('expect_root_exited') is not True]
    assert not alive_kills
    dead_reaps = [c for c in kill_tree.call_args_list
                  if c.kwargs.get('expect_root_exited') is True]
    assert dead_reaps
    assert task.completed and task.return_code == 7


def test_poll_loop_wedge_sets_flag_never_kills():
    """Direct poll-loop check: silence past the floor latches stall_kill_requested and the
    poll loop itself never calls the tree-killer."""
    tracker = AsyncShellTracker(pool=None)
    proc = _FakeProc(rc=None)
    task = _make_task(timeout=600)
    task.last_output_at = time.time() - 1000.0   # wedged: long silence from the start

    with patch.object(tracker_mod, 'ASYNC_SHELL_STALL_KILL_DEFAULT', 0.05), \
         patch.object(tracker_mod, 'ASYNC_SHELL_STALL_KILL_FRACTION', 1.0), \
         patch.object(tracker_mod, 'ASYNC_SHELL_STALL_WARN_FRACTION', 0.0), \
         patch.object(tracker_mod, 'HEARTBEAT_CHECK_INTERVAL', 0.01), \
         patch.object(tracker, '_kill_process_tree') as kill_tree:
        timed_out = tracker._poll_loop('test_agent', 1, proc, task, None, None)

    assert task.stall_kill_requested is True
    assert timed_out is False          # wedge close, NOT a timeout
    kill_tree.assert_not_called()       # poll loop must never kill


def test_real_tree_killer_invoked_exactly_once_on_stall_branch():
    """(d) command-signature mock: the REAL _kill_process_tree issues exactly one kill for the
    stall branch (no double-kill), proving the routing reaches the proven killer once."""
    tracker = AsyncShellTracker(pool=None)
    proc = _FakeProc(rc=None)
    proc.returncode = 1
    task = _make_task(timeout=60)
    task.launch_check_done.set()
    _setup_task(tracker, task)

    taskkill_calls = []

    def mock_run(cmd, **kwargs):
        s = str(cmd)
        if 'taskkill' in s:
            taskkill_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '', '')

    def _fake_poll(agent_name, tool_id, p, t, tout, terr):
        with t._lock:
            t.stall_kill_requested = True
            t.stall_seconds = 5.0
        return False

    with patch.object(tracker, '_spawn_process', side_effect=_make_fake_spawn(task, proc)), \
         patch.object(tracker, '_poll_loop', side_effect=_fake_poll), \
         patch.object(tracker, '_deliver_completion'), \
         patch.object(tracker_mod.subprocess, 'run', side_effect=mock_run):
        tracker._track_task('test_agent', task.tool_id, task.command, None)

    if os.name == 'nt':
        # Exactly one tree-kill pass reached the OS (a second survivor-recheck pass may follow
        # only if a survivor is reported; with empty CIM output there are none).
        assert 1 <= len(taskkill_calls) <= 2
    else:
        # POSIX path uses os.killpg, not subprocess.run — assert no spurious taskkill instead.
        assert not taskkill_calls


# ──────────────────────────────────────────────────────────────────────────────
# (e) Optional real-process proof (SERIAL — excluded from default -n auto run).
# Runs alone:  pytest -m serial tests/test_async_shell_stall_watchdog.py -o addopts="-n 0"
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.serial
def test_real_silent_child_reaped_before_deadline():
    """Async launch a child that prints one line then sleeps silently well past a tiny stall
    floor; assert the tool is closed via the stall branch well before the wall-clock timeout."""
    tracker = AsyncShellTracker(pool=None)
    # prints one line, then goes silent far past the stall floor.
    command = 'python -c "print(chr(72)+chr(105)); import time; time.sleep(60)"'
    with patch.object(tracker_mod, 'ASYNC_SHELL_STALL_KILL_DEFAULT', 2.0), \
         patch.object(tracker_mod, 'ASYNC_SHELL_STALL_KILL_FRACTION', 1.0), \
         patch.object(tracker_mod, 'ASYNC_SHELL_STALL_WARN_FRACTION', 0.0):
        task = AsyncShellTask(tool_id=1, agent_name='test_agent', command=command,
                              timeout=30, heartbeat_interval=-1.0)
        _setup_task(tracker, task)
        start = time.time()
        # Drive spawn+poll+track on THIS thread; the stall floor (~2s) must close it first.
        try:
            tracker._spawn_process('test_agent', 1, command, None)
            with task._lock:
                proc = task.process
            task.launch_check_done.set()
            timed_out = tracker._poll_loop('test_agent', 1, proc, task,
                                           task._drain_t_out, task._drain_t_err)
            elapsed = time.time() - start
            assert task.stall_kill_requested is True
            assert timed_out is False and elapsed < task.timeout
            # Reap for real (best-effort) so nothing leaks regardless of the asserts below.
            if proc is not None and proc.poll() is None:
                tracker._kill_process_tree(proc, 'test_agent', 1)
            assert proc.poll() is not None  # dead (or zombie awaiting init reap on POSIX)
        finally:
            if 'proc' in locals() and proc is not None:
                try:
                    tracker._kill_process_tree(proc, 'test_agent', 1, expect_root_exited=True)
                except Exception:
                    pass
