"""Tests for the UI "Terminate tool" button (``terminate_tools`` WS command).

The terminate_tools path REUSES the existing ``kill_all`` primitives — the same
calls the dismissal path already makes (``pool/lifecycle.py``) — to kill
in-flight background tool runners (async shell + CLI-agent) for an instance
while leaving the instance ALIVE and resumable. No new kill machinery is
introduced.

Layers (mirror the WS-seam style of tests/test_manual_kv_buttons.py):
- **Wiring:** drive the real ``WsMessageHandler.dispatch`` with a mocked pool;
  assert BOTH trackers' ``kill_all`` fire for the instance (``assert_any_call``,
  non-vacuous), a one-shot terminate signal is requested (NO message is enqueued
  into the alive instance), and the sticky-slot permit is NOT released (the
  instance stays resumable).
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
        # Corrected spec: NO user/system message is enqueued — instead the one-shot
        # terminate signal is requested so the engine crashes out the batch with
        # [TOOL TERMINATED] markers while the instance stays alive.
        pool.enqueue_message.assert_not_called()
        pool.request_terminate_tools.assert_called_once_with('X')
        # The transient INFO frame is broadcast before the state refresh so the
        # clicked-button result is visible immediately.
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
        pool.request_terminate_tools.assert_not_called()

    def test_root_orchestrator_kill_all_is_called(self):
        """The root orchestrator IS a valid target for terminate_tools (it is
        non-destructive; the dominant killable runners are keyed by the caller,
        which is the root). kill_all fires for root."""
        pool, shell, cli = _make_pool(parent_instance=None, shell_count=1, cli_count=1)
        handler, _ = _make_handler(pool)
        asyncio.run(handler.dispatch({'type': 'terminate_tools', 'instance_name': 'root'}))
        shell.kill_all.assert_any_call('root')
        cli.kill_all.assert_any_call('root')
        # Corrected spec: signal requested, no message enqueued.
        pool.enqueue_message.assert_not_called()
        pool.request_terminate_tools.assert_called_once_with('root')

    def test_does_not_release_slot_permit(self):
        """The instance stays resumable: release_slot_permit is NOT invoked.

        This is a forward-regression guard against a future local-import of
        release_slot_permit inside the terminate_tools handler.
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


# ────────────────────────────────────────────────────────────────────────────
# Pool signal layer — one-shot signal + best-effort foreground tree-kill
# ────────────────────────────────────────────────────────────────────────────

import json
from types import SimpleNamespace

from agent_cascade.pool.slots import SlotsMixin
from agent_cascade.engine.tool_execution import ToolExecMixin
from agent_cascade.llm.schema import FUNCTION


class _SignalPool(SlotsMixin):
    """Minimal host for SlotsMixin's terminate-tools methods (no full AgentPool ctor).

    ``operation_manager._terminate_shell_process`` is a recording stub so the foreground
    kill path is exercised without touching real processes. ``kill_ok=False`` makes the
    tree-kill raise, to prove a kill failure never breaks the signal.
    """

    def __init__(self, kill_ok=True):
        self._terminate_tools_requested = set()
        self._terminate_tools_lock = threading.Lock()
        self._foreground_processes = {}
        self._foreground_lock = threading.Lock()
        self.kill_calls = []
        self._kill_ok = kill_ok
        self.operation_manager = SimpleNamespace(_terminate_shell_process=self._fake_term)

    def _fake_term(self, proc):
        self.kill_calls.append(proc)
        if not self._kill_ok:
            raise RuntimeError('tree-kill failed')


class TestTerminateSignal:

    def test_request_then_consume_is_one_shot(self):
        """consume returns True exactly once; the signal clears afterward (batch scope)."""
        p = _SignalPool()
        p.request_terminate_tools('X')
        assert p.has_terminate_tools_requested('X') is True
        assert p.consume_terminate_tools('X') is True
        assert p.consume_terminate_tools('X') is False
        assert p.has_terminate_tools_requested('X') is False

    def test_foreground_handle_killed_on_request(self):
        """A registered foreground handle is tree-killed when terminate is requested;
        the signal is still set (kill is best-effort, signal is authoritative)."""
        p = _SignalPool()
        proc = _FakePopen()
        p.register_foreground_process('X', proc)
        p.request_terminate_tools('X')
        assert proc in p.kill_calls
        assert p.has_terminate_tools_requested('X') is True
        p.unregister_foreground_process('X', proc)
        assert 'X' not in p._foreground_processes

    def test_kill_failure_still_sets_signal(self):
        """A failing OS tree-kill must NOT raise nor clear the signal — the engine-side
        result-marker path still stands regardless of kill outcome."""
        p = _SignalPool(kill_ok=False)
        p.register_foreground_process('X', _FakePopen())
        p.request_terminate_tools('X')  # must not raise
        assert p.has_terminate_tools_requested('X') is True


# ────────────────────────────────────────────────────────────────────────────
# Engine marker layer — schema-valid [TOOL TERMINATED] FUNCTION results
# ────────────────────────────────────────────────────────────────────────────


def _tool_calls(n, name='noop'):
    """n ordered generic tool-call messages (dict form the real loop accepts)."""
    return [{
        'role': 'assistant', 'content': '',
        'function_call': {'name': name, 'arguments': json.dumps({'x': i})},
        'extra': {'function_id': f'fc-{i}'},
    } for i in range(n)]


class _ScriptedPool:
    """Pool stand-in: consume_terminate_tools replays a scripted bool sequence (the real
    bool singletons, so the engine's ``is True`` guard is exercised faithfully)."""

    def __init__(self, consume_seq):
        self._seq = list(consume_seq)
        # cache_threshold_chars is read by the dispatch path (tool_execution.py) as the
        # argument to _cache_tool_output, so the dispatch-path tests need it modeled.
        self.settings = SimpleNamespace(shell_char_limit=2048, cache_threshold_chars=8192)
        # pool.instances must hold the live instance so the stack-swap identity guard
        # (_swap_identity_lost) sees identity intact and lets the normal FUNCTION append
        # run — exactly as it does in production. Without it the guard mis-reads the
        # instance as parked and silently drops the dispatched result.
        self.instances = {}

    def consume_terminate_tools(self, inst_name):
        return self._seq.pop(0) if self._seq else False

    def get_instance(self, name):
        return None

    def is_paused(self):
        return False

    def wait_if_paused(self, timeout=1.0):
        return None

    def request_terminate(self):
        """Simulate a terminate signal arriving DURING a blocking call: forces the next
        ``consume_terminate_tools`` to return the ``True`` singleton, so a test can drive
        the engine's post-dispatch overwrite."""
        self._seq.insert(0, True)


class _RecordingDispatcher:
    """Dispatcher stand-in for the dispatch-path tests. Records the dispatched tool,
    calls ``harness.on_execute`` (which may simulate a terminate signal landing DURING the
    blocking call) and returns its result, so the engine's post-dispatch overwrite is
    exercised with a real-looking tool output in hand."""

    def __init__(self, harness):
        self._harness = harness

    def execute_tool(self, instance, tool_name, tool_args, llm_messages, **kwargs):
        self._harness.dispatched.append(tool_name)
        return self._harness.on_execute(tool_name)


class _MarkerHarness(ToolExecMixin):
    """Bare ToolExecMixin host for the terminate marker helpers + a loop-head-terminate
    run of the REAL ``_execute_detected_tools``. The loop-head path fires before any
    dispatch, so none of the dispatch/approval machinery is exercised — this isolates the
    marker batch. Collaborators the helpers touch are stubbed; helpers themselves are
    production code (pinned via ``__func__`` below)."""

    def __init__(self, consume_seq):
        self.pool = _ScriptedPool(consume_seq)
        self.compression_handler = None
        self.appended = []
        # Dispatch-path support (post-dispatch overwrite / partial batch). on_execute is
        # the stubbed tool body; override it to simulate a terminate arriving mid-call.
        self.dispatched = []
        self.tool_dispatcher = _RecordingDispatcher(self)
        self.on_execute = lambda tool_name: 'REAL_RESULT_OK'

    # ── collaborators unrelated to the marker batch ──
    def _telemetry(self):
        return None

    def _detect_tool(self, out):
        fc = out.get('function_call')
        if not fc:
            return (False, None, None, '')
        return (True, fc['name'], {}, '')

    def _resolve_tool_policy(self, instance):
        return (None, set(), {})

    def _scan_tool_batch_indices(self, turn_output, inst_name):
        return ([], [], [])

    def _is_terminal_stop(self, inst_name):
        return False

    def _is_suspended_by_compression(self, inst_name):
        return False

    def _is_stopped(self, inst_name):
        return False

    def _auto_deny_reason(self, template, disabled_tools, function_map, tool_name):
        return None

    def _cache_tool_output(self, *a, **k):
        return None

    def _detect_tool_result_failure(self, tool_result):
        # Stubbed so a dispatched result is treated as successful; the post-dispatch
        # overwrite then decides the final tool_success regardless of this.
        return (True, '')

    def _append_and_log(self, instance, message, **kwargs):
        self.appended.append(message)

    def _run_deferred_state_restore_safety_nets(self, instance, inst_name, **kwargs):
        return None

    def _proactive_compression_check(self, *a, **k):
        return None


class TestTerminateMarkers:

    def test_helpers_are_production_code(self):
        h = _MarkerHarness([])
        assert h._terminate_remaining_tools.__func__ is ToolExecMixin._terminate_remaining_tools
        assert h._emit_terminated_fn_msg.__func__ is ToolExecMixin._emit_terminated_fn_msg

    def test_terminate_remaining_tools_emits_one_function_per_open_call(self):
        """Each still-open tool call gets exactly one FUNCTION marker with the correct
        function_id and tool_success=False; already-closed indices are skipped."""
        h = _MarkerHarness([])
        calls = _tool_calls(3)
        response, executed, closed = [], [], {0}  # index 0 already closed
        n = h._terminate_remaining_tools(
            SimpleNamespace(), 'coder', calls, response, executed, closed)
        assert n == 2  # indices 1,2 only
        fn = [m for m in response if m.role == FUNCTION]
        assert len(fn) == 2
        assert {m.extra['function_id'] for m in fn} == {'fc-1', 'fc-2'}
        assert all(m.extra['tool_success'] is False for m in fn)
        assert all('TOOL TERMINATED' in m.content for m in fn)
        assert {1, 2}.issubset(closed)
        # idempotent: a second call closes nothing further
        assert h._terminate_remaining_tools(
            SimpleNamespace(), 'coder', calls, response, executed, closed) == 0

    def test_emit_terminated_fn_msg_schema(self):
        """A single marker is a proper FUNCTION message carrying the call's function_id
        so no tool_call is orphaned on replay (no API 400)."""
        h = _MarkerHarness([])
        out = {'role': 'assistant', 'content': '',
               'function_call': {'name': 'noop', 'arguments': '{}'},
               'extra': {'function_id': 'fc-7'}}
        response = []
        h._emit_terminated_fn_msg(SimpleNamespace(), out, 'noop', 0, 'coder', response)
        assert len(response) == 1
        msg = response[0]
        assert msg.role == FUNCTION
        assert msg.extra == {'function_id': 'fc-7', 'tool_success': False}
        assert 'TOOL TERMINATED' in msg.content

    def test_loop_head_terminate_closes_batch_and_completes_turn(self):
        """Terminate observed at the loop head: every tool is replaced with a marker, the
        batch closes cleanly (N calls -> N FUNCTION results, no orphan), the turn
        completes (used_any True) with NO dispatch and NO run abort."""
        assert _MarkerHarness([])._execute_detected_tools.__func__ is ToolExecMixin._execute_detected_tools
        h = _MarkerHarness([True])  # consume_seq [True] == [head(0)]: terminate at tool 0's loop head.
        calls = _tool_calls(3)
        response = []
        used = h._execute_detected_tools(SimpleNamespace(), 'coder', calls, list(calls), [], response)
        assert used is True
        fn = [m for m in response if m.role == FUNCTION]
        assert len(fn) == 3
        assert {m.extra['function_id'] for m in fn} == {'fc-0', 'fc-1', 'fc-2'}
        assert all(m.extra['tool_success'] is False for m in fn)
        assert all('TOOL TERMINATED' in m.content for m in fn)
        # appended == response length: every marker is persisted too (no in-flight gaps)
        assert len(h.appended) == 3

    def test_post_dispatch_overwrite_marks_blocking_call(self):
        """The 60s-sync repro at engine level (the headline case): ONE tool is dispatched
        (loop head sees no signal), the terminate signal is set DURING the blocking call,
        and the engine's post-dispatch overwrite replaces the real output with the marker.
        Asserts the emitted fn_msg has tool_success=False, carries TOOL_TERMINATED (and NOT
        the real output), and the turn completes (used_any True) with no run abort."""
        # consume_seq [False] == [head(0)]; request_terminate() injects the True the post-dispatch consume reads.
        h = _MarkerHarness([False])  # loop-head consume -> False -> we actually dispatch
        # Simulate the user pressing Terminate while the blocking call is still running.
        h.on_execute = lambda tool_name: (h.pool.request_terminate(), 'REAL_RESULT_OK')[1]
        calls = _tool_calls(1, name='shell_cmd')
        response = []
        inst = SimpleNamespace()
        h.pool.instances['coder'] = inst  # keep stack-swap identity intact (production shape)
        used = h._execute_detected_tools(inst, 'coder', calls, list(calls), [], response)
        assert used is True
        assert h.dispatched == ['shell_cmd']           # the dispatcher really ran
        fn = [m for m in response if m.role == FUNCTION]
        assert len(fn) == 1
        assert 'REAL_RESULT_OK' not in fn[0].content   # real blocking output discarded
        assert 'TOOL TERMINATED' in fn[0].content      # marker stands
        assert fn[0].extra['function_id'] == 'fc-0'
        assert fn[0].extra['tool_success'] is False
        assert len(h.appended) == 1

    def test_partial_batch_keeps_earlier_real_result(self):
        """A tool that completed with a real result BEFORE terminate is observed must NOT
        be replaced — only the not-yet-run tools get markers. consume_seq [False, False,
        True]: tool 0 dispatches + closes for real, then terminate latches at tool 1's loop
        head and closes it with a marker."""
        # consume_seq [False, False, True] == [head(0), post-dispatch(0), head(1)].
        h = _MarkerHarness([False, False, True])
        calls = _tool_calls(2, name='shell_cmd')
        response = []
        inst = SimpleNamespace()
        h.pool.instances['coder'] = inst  # keep stack-swap identity intact (production shape)
        used = h._execute_detected_tools(inst, 'coder', calls, list(calls), [], response)
        assert used is True
        assert h.dispatched == ['shell_cmd']           # only tool 0 ran; tool 1 never dispatched
        fn = [m for m in response if m.role == FUNCTION]
        assert len(fn) == 2
        first = next(m for m in fn if m.extra['function_id'] == 'fc-0')
        second = next(m for m in fn if m.extra['function_id'] == 'fc-1')
        # tool 0's real result is preserved untouched (not replaced by a marker).
        assert 'REAL_RESULT_OK' in first.content
        assert 'TOOL TERMINATED' not in first.content
        assert first.extra['tool_success'] is True
        # only tool 1 (not-yet-run) gets the marker.
        assert 'TOOL TERMINATED' in second.content
        assert second.extra['tool_success'] is False

    def test_consume_degrades_false_on_bare_and_mock_pools(self):
        """_consume_terminate_tools must never open the terminate path on a pool that
        cannot actually signal: a bare SimpleNamespace pool has no consume_terminate_tools
        (getattr -> None), and a MagicMock pool auto-fabricates a TRUTHY child mock that
        the ``is True`` identity guard rejects. Both must return the False singleton."""
        bare = SimpleNamespace(pool=SimpleNamespace())
        assert ToolExecMixin._consume_terminate_tools(bare, 'x') is False
        mocked = SimpleNamespace(pool=MagicMock())
        assert ToolExecMixin._consume_terminate_tools(mocked, 'x') is False
