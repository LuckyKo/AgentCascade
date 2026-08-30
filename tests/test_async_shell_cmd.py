"""Regression tests for async shell_cmd changes.

Tests cover: heartbeat routing, __wait behavior, control command justification,
auto-async mode, and edge cases. No LLM or network connections required.
"""

import sys
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import importlib.util as _util
import os as _os

import jsonschema
import pytest

from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask


def _load_message_queue_mixin():
    """Load MessageQueueMixin WITHOUT triggering agent_cascade/pool/__init__.py.

    ⚠️ DO NOT "simplify" this away — it is load-bearing for two independent reasons:

    1. A plain `from agent_cascade.pool.message_queue import MessageQueueMixin` at module
       top FAILS collection under pytest-xdist: `agent_cascade/pool/__init__.py` eagerly
       imports the full app chain (core → agents → tools → config.secrets_loader), which is
       unavailable to xdist workers. The mixin module itself has no such dependencies, so we
       load it directly from its file, registering a minimal `agent_cascade.pool` parent that
       does NOT run the heavy __init__.

    2. It must be loaded under its CANONICAL dotted name ('agent_cascade.pool.message_queue'),
       not an ad-hoc one. An ad-hoc name creates a DISTINCT class object, so the fake pool in
       this file would no longer pass `_has_real_wait_for_message`'s isinstance check (which
       compares against shell_cmd.py's own import) and every queue-driven test would silently
       fall to the polling fallback — passing for the wrong reason.

    See project memory: .agent_lessons/async-shell-wait-queue-fix-impl.md (gotchas 1 & 2).
    """
    if 'agent_cascade.pool.message_queue' in sys.modules:
        return sys.modules['agent_cascade.pool.message_queue'].MessageQueueMixin

    _mixin_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        'agent_cascade', 'pool', 'message_queue.py',
    )
    # Register a lightweight parent package so the canonical submodule name resolves,
    # WITHOUT executing agent_cascade/pool/__init__.py (which pulls in the whole app).
    if 'agent_cascade.pool' not in sys.modules:
        import types as _types
        _parent = _types.ModuleType('agent_cascade.pool')
        _parent.__path__ = [_os.path.dirname(_mixin_path)]
        sys.modules['agent_cascade.pool'] = _parent

    spec = _util.spec_from_file_location('agent_cascade.pool.message_queue', _mixin_path)
    mod = _util.module_from_spec(spec)
    sys.modules['agent_cascade.pool.message_queue'] = mod
    spec.loader.exec_module(mod)
    return mod.MessageQueueMixin


MessageQueueMixin = _load_message_queue_mixin()


class _FakePoolWithWait(MessageQueueMixin):
    """Lightweight real pool for exercising the queue-driven __wait path.

    MessageQueueMixin has no __init__, so we provide exactly what wait_for_message +
    enqueue_message touch: _message_condition, message_queues, is_instance_terminated,
    and _mark_activity (called by enqueue_message). A bare MagicMock would NOT be an
    instance of MessageQueueMixin, so it correctly falls to the polling fallback.
    """

    def __init__(self):
        self.message_queues = {}
        self._queue_lock = threading.Lock()
        self._message_condition = threading.Condition(self._queue_lock)
        # Set True to exercise the termination branch of wait_for_message (returns None promptly).
        self.terminated = False

    def is_instance_terminated(self, instance_name):
        return self.terminated

    def _mark_activity(self, instance_name):  # called by enqueue_message (message_queue.py:67)
        pass


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
    mock_pool.llm_cfg = {}  # Must be dict so .get('shell_char_limit', default) returns default
    mock_pool.operation_manager.request_user_approval.return_value = (True, '')  # Auto-approve for tests
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

    def test_heartbeat_goes_to_enqueue_message(self, mock_task_running):
        pool = MagicMock()
        pool.enqueue_message = MagicMock()
        pool.llm_cfg = {}

        tracker = AsyncShellTracker(pool=pool)
        _setup_task(tracker, mock_task_running)

        with mock_task_running._lock:
            mock_task_running.stdout_lines = [f'line{i}' for i in range(5)]
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)

        pool.enqueue_message.assert_called_once()

        msg = pool.enqueue_message.call_args[0][1]
        assert '⟨shell_cmd heartbeat⟩' in msg
        assert 'Tool ID: 1' in msg

    def test_heartbeat_does_not_double_wrap(self, mock_task_running):
        pool = MagicMock()
        pool.enqueue_message = MagicMock()
        pool.llm_cfg = {}
        tracker = AsyncShellTracker(pool=pool)
        _setup_task(tracker, mock_task_running)

        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['output line']
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)
        msg = pool.enqueue_message.call_args[0][1]

        assert msg.startswith('⟨shell_cmd heartbeat⟩')
        assert '"function_id":' not in msg
        assert 'tool_call_result' not in msg.lower()

    def test_heartbeat_fallback_to_enqueue_when_no_async_results(self, mock_task_running):
        pool = MagicMock()
        del pool._async_results
        pool.enqueue_message = MagicMock()
        pool.llm_cfg = {}

        tracker = AsyncShellTracker(pool=pool)
        _setup_task(tracker, mock_task_running)

        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['fallback test']
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)

        pool.enqueue_message.assert_called_once()
        msg = pool.enqueue_message.call_args[0][1]
        assert '⟨shell_cmd heartbeat⟩' in msg

    def test_heartbeat_no_longer_uses_async_results_buffer(self, mock_task_running):
        """Verify heartbeats now go directly to enqueue_message (single-queue migration)."""
        pool = MagicMock()
        pool.enqueue_message = MagicMock()
        pool.llm_cfg = {}
        # Ensure _async_results is NOT used
        if hasattr(pool, '_async_results'):
            delattr(pool, '_async_results')

        tracker = AsyncShellTracker(pool=pool)
        _setup_task(tracker, mock_task_running)

        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['data']
            mock_task_running.last_heartbeat_sent_pos = 0

        tracker._send_heartbeat('test_agent', 1)
        pool.enqueue_message.assert_called_once()
        msg = pool.enqueue_message.call_args[0][1]
        assert '⟨shell_cmd heartbeat⟩' in msg


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

    def _wait_env_queue(self, shell_cmd_tool, task):
        """Set up tracker + a REAL MessageQueueMixin pool for the queue-driven __wait path."""
        tracker = AsyncShellTracker(pool=None)
        _setup_task(tracker, task)
        pool = _FakePoolWithWait()
        pool._async_shell_tracker = tracker
        pool.llm_cfg = {}  # dict so .get('shell_char_limit', default) returns default
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'test_agent'
        return tracker, pool

    def test_wait_no_running_shell(self, shell_cmd_tool, mock_tracker):
        _make_tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 999, "execution_mode": "async"}')
        assert 'No running shell found' in result
        assert 'Tool ID: 999' in result

    def test_wait_already_completed(self, shell_cmd_tool):
        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(tool_id=2, agent_name='test_agent', command='echo done', pid=12346,
                              completed=True, return_code=0, stdout_lines=['done'])
        _setup_task(tracker, task)
        _make_tool_with_tracker(shell_cmd_tool, tracker)

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 2, "execution_mode": "async"}')
        assert 'already completed' in result.lower() or 'Process already completed' in result
        assert 'Tool ID: 2' in result
        assert 'elapsed' in result, f"__wait (already completed) missing elapsed time: {result!r}"

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

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')
        thread.join(timeout=2)

        assert '⟨shell_cmd wait⟩' in result
        assert 'Process completed' in result
        assert 'exit code 0' in result
        assert 'elapsed' in result, f"__wait (completed) missing elapsed time: {result!r}"

    def test_wait_returns_timeout_when_no_output(self, shell_cmd_tool, mock_task_running):
        _, fake_time_mod, _ = self._wait_env(shell_cmd_tool, mock_task_running)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        assert '⟨shell_cmd wait⟩' in result
        assert 'No new output' in result
        assert 'timeout' in result.lower()
        assert 'elapsed' in result, f"__wait (timeout) missing elapsed time: {result!r}"

    def test_wait_respects_timeout_cap_at_180s(self, shell_cmd_tool):
        task = _make_running_task(heartbeat_interval=3600.0, command='sleep 1000', pid=99999)
        _, fake_time_mod, state = self._wait_env(shell_cmd_tool, task)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        assert state['elapsed'] <= 180.0, f"__wait waited {state['elapsed']:.1f}s, should cap at 180s (3 min)"
        assert state['elapsed'] >= 179.0, f"__wait waited {state['elapsed']:.1f}s, should be ~180s"
        assert 'No new output' in result
        assert '180s' in result
        assert 'elapsed' in result, f"__wait (timeout cap) missing elapsed time: {result!r}"

    def test_wait_uses_heartbeat_interval_below_cap(self, shell_cmd_tool):
        """When heartbeat_interval is below the 180s cap, __wait waits that interval."""
        task = _make_running_task(heartbeat_interval=90.0, command='sleep 1000', pid=99999)
        _, fake_time_mod, state = self._wait_env(shell_cmd_tool, task)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        assert state['elapsed'] <= 90.0, f"__wait waited {state['elapsed']:.1f}s, should be heartbeat interval 90s (below cap)"
        assert state['elapsed'] >= 89.0, f"__wait waited {state['elapsed']:.1f}s, should be ~90s"
        assert 'No new output' in result
        assert '90s' in result
        assert 'elapsed' in result, f"__wait (heartbeat below cap) missing elapsed time: {result!r}"

    def test_wait_heartbeat_at_exact_cap(self, shell_cmd_tool):
        """When heartbeat_interval equals the 180s cap, __wait waits ~180s (boundary)."""
        task = _make_running_task(heartbeat_interval=180.0, command='sleep 1000', pid=99999)
        _, fake_time_mod, state = self._wait_env(shell_cmd_tool, task)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        # min(180.0, 180.0) == 180.0; allow a small overshoot from the 0.5s poll step
        assert state['elapsed'] <= 181.0, f"__wait waited {state['elapsed']:.1f}s, should be ~180s at exact cap"
        assert state['elapsed'] >= 179.0, f"__wait waited {state['elapsed']:.1f}s, should be ~180s at exact cap"
        assert 'No new output' in result
        assert '180s' in result
        assert 'elapsed' in result, f"__wait (exact cap) missing elapsed time: {result!r}"

    def test_wait_no_deadlock_on_sequential_access(self, shell_cmd_tool, mock_task_running):
        _, fake_time_mod, _ = self._wait_env(shell_cmd_tool, mock_task_running)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            for _ in range(3):
                result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')
                assert '⟨shell_cmd wait⟩' in result

    def test_wait_proper_lock_handling(self, shell_cmd_tool):
        task = _make_running_task(heartbeat_interval=5.0, command='sleep 100', pid=99999)
        _, fake_time_mod, _ = self._wait_env(shell_cmd_tool, task)

        with patch.dict('sys.modules', {'time': fake_time_mod}):
            shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        with task._lock:
            assert task.completed is False

    # ── Queue-driven __wait tests (real MessageQueueMixin path) ──────────────

    def test_wait_consumes_already_queued_heartbeat(self, shell_cmd_tool, mock_task_running):
        """RC2 core fix: an ALREADY-queued heartbeat is consumed immediately (no timeout wait)."""
        self._wait_env_queue(shell_cmd_tool, mock_task_running)
        msg = "⟨shell_cmd heartbeat⟩ Beat 1 (30s), Tool ID: 1 | No new output (still running)"
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', msg)

        start = time.time()
        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')
        elapsed = time.time() - start

        assert result == msg
        # Must return immediately — NOT wait out the heartbeat interval (default 10s here).
        assert elapsed < 2.0, f"__wait did not consume pre-queued heartbeat immediately ({elapsed:.1f}s)"

    def test_wait_does_not_swallow_non_shell_messages(self, shell_cmd_tool, mock_task_running):
        """v2 wake-up contract: a NON-shell (user) message at the FRONT of the queue → __wait
        returns the DEFAULT wake-up string (NOT the shell msg) and leaves BOTH messages queued
        in original order for the normal drain. Would FAIL if the code still consumed/skipped."""
        self._wait_env_queue(shell_cmd_tool, mock_task_running)
        user_msg = "hello from user"
        shell_msg = "⟨shell_cmd heartbeat⟩ Beat 1 (5s), Tool ID: 1 | 1 line since last tick\nout"
        # Enqueue the non-shell message FIRST so it sits at the FRONT of the shared queue.
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', user_msg)
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', shell_msg)

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        # Front is the user msg (not this shell) → default wake-up string, NOT the shell msg.
        assert 'Woken by queued message (not this shell)' in result, f"expected default wake-up: {result!r}"
        assert result != shell_msg, "default wake-up case must not return the shell msg verbatim"
        # BOTH messages remain queued, in original order (user first), for the normal drain.
        remaining = shell_cmd_tool.agent_pool.get_queue_messages('test_agent')
        assert remaining == [user_msg, shell_msg], \
            f"queue was mutated / reordered: {remaining!r}"

    def test_wait_user_and_heartbeat_drain_in_sequence(self, shell_cmd_tool, mock_task_running):
        """v2 wake-up contract: user msg then this tool's heartbeat → __wait returns the default
        wake-up (user is at front), and BOTH remain queued in original order so the normal drain
        delivers them one after another as usual."""
        self._wait_env_queue(shell_cmd_tool, mock_task_running)
        user_msg = "supervisor note"
        heartbeat = "⟨shell_cmd heartbeat⟩ Beat 1 (5s), Tool ID: 1 | 1 line since last tick\nout"
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', user_msg)
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', heartbeat)

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        assert 'Woken by queued message (not this shell)' in result, f"expected default wake-up: {result!r}"
        # Both still queued in original order — drain will deliver user then heartbeat.
        remaining = shell_cmd_tool.agent_pool.get_queue_messages('test_agent')
        assert remaining == [user_msg, heartbeat], \
            f"queue must be left intact in order for the drain: {remaining!r}"

    def test_wait_predicate_leaves_other_tool_id_queued(self, shell_cmd_tool, mock_task_running):
        """v2 wake-up contract: a DIFFERENT tool's shell message at the FRONT → __wait(tool_id=1)
        returns the default wake-up (not ours) and leaves BOTH queued in order; tool 2's own
        __wait will later consume it when it reaches the front."""
        self._wait_env_queue(shell_cmd_tool, mock_task_running)
        msg1 = "⟨shell_cmd heartbeat⟩ Beat 1 (5s), Tool ID: 1 | 1 line since last tick\na"
        msg2 = "⟨shell_cmd heartbeat⟩ Beat 1 (5s), Tool ID: 2 | 1 line since last tick\nb"
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', msg2)  # other tool at FRONT
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', msg1)

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        # Front is tool 2's msg (not ours) → default wake-up, NOT consumed.
        assert 'Woken by queued message (not this shell)' in result, f"expected default wake-up: {result!r}"
        remaining = shell_cmd_tool.agent_pool.get_queue_messages('test_agent')
        # Both remain queued in original order for the normal drain.
        assert remaining == [msg2, msg1], f"queue was mutated / reordered: {remaining!r}"

    def test_wait_no_output_duplication(self, shell_cmd_tool, mock_task_running):
        """RC3: __wait consumes the tracker's heartbeat (which advanced last_heartbeat_sent_pos);
        a subsequent heartbeat must NOT re-send lines already delivered by __wait."""
        # Real tracker wired to the real pool so _send_heartbeat enqueues + advances position.
        pool = _FakePoolWithWait()
        pool.llm_cfg = {}
        tracker = AsyncShellTracker(pool=pool)  # tracker must see the pool or _enqueue is a no-op
        _setup_task(tracker, mock_task_running)
        pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'test_agent'

        # Seed output the tracker has not yet sent.
        with mock_task_running._lock:
            mock_task_running.stdout_lines = ['dup line A', 'dup line B']
            mock_task_running.last_heartbeat_sent_pos = 0

        # Tracker produces + enqueues a heartbeat (advances last_heartbeat_sent_pos to end).
        tracker._send_heartbeat('test_agent', 1)
        queued = pool.get_queue_messages('test_agent')
        assert len(queued) == 1
        first_msg = queued[0]
        assert 'dup line A' in first_msg and 'dup line B' in first_msg

        # __wait consumes that heartbeat verbatim.
        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')
        assert result == first_msg

        # Now the tracker sends a SECOND heartbeat with NO new output. Because
        # last_heartbeat_sent_pos was already advanced by the first heartbeat (not by __wait),
        # this second heartbeat must be the "no new output" variant — it must NOT re-send
        # 'dup line A'/'dup line B'.
        tracker._send_heartbeat('test_agent', 1)
        queued2 = pool.get_queue_messages('test_agent')
        assert len(queued2) == 1
        second_msg = queued2[0]
        assert 'No new output (still running)' in second_msg
        assert 'dup line A' not in second_msg, "RC3 regression: lines re-sent after __wait consumed them"
        assert 'dup line B' not in second_msg

    def test_wait_fallback_uses_polling_for_mock_pool(self, shell_cmd_tool, mock_task_running):
        """Guards the MagicMock guard: a bare MagicMock pool is NOT a MessageQueueMixin, so
        __wait must fall to _polling_wait (proving _has_real_wait_for_message rejects mocks)."""
        from agent_cascade.tools.custom.shell_cmd import _has_real_wait_for_message
        tracker = AsyncShellTracker(pool=None)
        _setup_task(tracker, mock_task_running)
        _make_tool_with_tracker(shell_cmd_tool, tracker)  # wires a bare MagicMock pool

        assert not _has_real_wait_for_message(shell_cmd_tool.agent_pool), \
            "MagicMock pool must be rejected by _has_real_wait_for_message"
        # And the fake real pool must be accepted (proves the new path is reachable).
        assert _has_real_wait_for_message(_FakePoolWithWait())

    def test_wait_predicate_does_not_match_longer_tool_id(self, shell_cmd_tool, mock_task_running):
        """REGRESSION (exact-id boundary under v2 front-of-queue semantics): tool_id=1 must NOT
        treat a Tool ID: 12 message as its own.

        The old predicate `f'Tool ID: {tool_id}' in m` matched "Tool ID: 1" as a substring of
        "Tool ID: 12". With v2 front-of-queue semantics, if the 12-message is at the FRONT and
        we __wait(1), the boundary regex must say it is NOT ours → default wake-up, both stay
        queued. (If the old substring predicate were used, the 12-msg would be wrongly consumed.)
        """
        self._wait_env_queue(shell_cmd_tool, mock_task_running)
        msg_12 = "⟨shell_cmd heartbeat⟩ Beat 1 (5s), Tool ID: 12 | 1 line since last tick\nb"
        msg_1 = "⟨shell_cmd heartbeat⟩ Beat 1 (5s), Tool ID: 1 | 1 line since last tick\na"
        # Enqueue the LONGER id first so it sits at the FRONT of the shared queue.
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', msg_12)
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', msg_1)

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        # Front is the Tool ID: 12 message (NOT ours — boundary regex rejects it).
        assert 'Woken by queued message (not this shell)' in result, \
            f"__wait(1) wrongly consumed tool 12's front message: {result!r}"
        # Both remain queued in original order; tool 12's __wait will consume msg_12 later.
        remaining = shell_cmd_tool.agent_pool.get_queue_messages('test_agent')
        assert remaining == [msg_12, msg_1], f"queue was mutated / reordered: {remaining!r}"

    def test_wait_consumes_own_id_when_at_front(self, shell_cmd_tool, mock_task_running):
        """Positive boundary check (v2 consume path): when THIS tool's exact-id message is at the
        FRONT it IS consumed verbatim — and a longer-id message queued behind it stays put. Pairs
        with test_wait_predicate_does_not_match_longer_tool_id to pin the 1-vs-12 boundary both ways."""
        self._wait_env_queue(shell_cmd_tool, mock_task_running)
        msg_1 = "⟨shell_cmd heartbeat⟩ Beat 1 (5s), Tool ID: 1 | 1 line since last tick\na"
        msg_12 = "⟨shell_cmd heartbeat⟩ Beat 1 (5s), Tool ID: 12 | 1 line since last tick\nb"
        # Ours at the FRONT, longer id behind it.
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', msg_1)
        shell_cmd_tool.agent_pool.enqueue_message('test_agent', msg_12)

        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        assert result == msg_1, f"__wait(1) should consume its own front message verbatim: {result!r}"
        remaining = shell_cmd_tool.agent_pool.get_queue_messages('test_agent')
        # Only our own message was consumed; the longer-id one stays queued.
        assert remaining == [msg_12], f"longer-id message must stay queued: {remaining!r}"

    def test_wait_timeout_on_empty_new_path(self, shell_cmd_tool):
        """Queue-driven path None-return branch: with NO messages enqueued and a short real
        timeout, __wait returns the exact 'No new output (timeout after ...)' string promptly.
        Covers the queue path's timeout branch (previously only exercised via the MagicMock
        polling fallback). Uses a short heartbeat_interval to keep the real wait ~0.5s."""
        task = _make_running_task(heartbeat_interval=0.5, command='sleep 100', pid=99999)
        self._wait_env_queue(shell_cmd_tool, task)

        start = time.time()
        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')
        elapsed = time.time() - start

        assert '⟨shell_cmd wait⟩' in result
        assert 'No new output' in result
        assert 'timeout after 0s' in result  # timeout = min(0.5, 180) → {0.5:.0f} == '0'
        assert 'elapsed' in result
        assert elapsed < 1.5, f"__wait did not return promptly on empty queue ({elapsed:.1f}s)"

    def test_wait_terminated_instance_returns_promptly(self, shell_cmd_tool):
        """Termination branch (message_queue.py ~170-171): when is_instance_terminated is True,
        wait_for_message returns None immediately (even with a long timeout), so __wait returns
        the existing 'No new output (timeout after ...)' string promptly without waiting it out."""
        # Long heartbeat_interval → normally a ~3600s-capped wait; termination must short-circuit it.
        task = _make_running_task(heartbeat_interval=3600.0, command='sleep 100', pid=99999)
        self._wait_env_queue(shell_cmd_tool, task)
        shell_cmd_tool.agent_pool.terminated = True  # make is_instance_terminated return True

        start = time.time()
        result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')
        elapsed = time.time() - start

        assert '⟨shell_cmd wait⟩' in result
        assert 'No new output' in result
        assert 'timeout after' in result
        # Must NOT wait out the (capped) heartbeat interval — termination returns promptly.
        assert elapsed < 1.5, f"__wait did not return promptly on terminated instance ({elapsed:.1f}s)"


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
        result = shell_cmd_tool.call('{"command": "__status", "tool_id": 1, "execution_mode": "async"}')
        assert 'ValueError' not in result
        assert '⟨shell_cmd status⟩' in result
        assert 'Tool ID: 1' in result

    def test_kill_command_without_justification(self, shell_cmd_tool):
        tracker = self._tracker_with_task()
        _make_tool_with_tracker(shell_cmd_tool, tracker)
        result = shell_cmd_tool.call('{"command": "__kill", "tool_id": 1, "execution_mode": "async"}')
        assert 'ValueError' not in result

    def test_ctrl_c_without_justification(self, shell_cmd_tool):
        tracker = self._tracker_with_task(tool_id=3, heartbeat_interval=5.0)
        _make_tool_with_tracker(shell_cmd_tool, tracker)
        result = shell_cmd_tool.call('{"command": "__ctrl_c", "tool_id": 3, "execution_mode": "async"}')
        assert 'ValueError' not in result
        assert 'Tool ID: 3' in result or 'Ctrl+C sent' in result or 'Failed' in result

    def test_heartbeat_update_without_justification(self, shell_cmd_tool):
        tracker = self._tracker_with_task(tool_id=4, heartbeat_interval=10.0)
        _make_tool_with_tracker(shell_cmd_tool, tracker)
        result = shell_cmd_tool.call('{"command": "__heartbeat=5", "tool_id": 4, "execution_mode": "async"}')
        assert 'ValueError' not in result
        assert 'updated' in result.lower() or 'Tool ID: 4' in result

    def test_status_command_works_without_justification(self, shell_cmd_tool):
        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(tool_id=5, agent_name='test_agent', command='long running task',
                              pid=55555, completed=False, heartbeat_interval=5.0,
                              stdout_lines=['line1', 'line2'])
        _setup_task(tracker, task)
        _make_tool_with_tracker(shell_cmd_tool, tracker)

        result = shell_cmd_tool.call('{"command": "__status", "tool_id": 5, "execution_mode": "async"}')
        assert '⟨shell_cmd status⟩' in result
        assert 'Tool ID: 5' in result
        assert 'line1' in result or 'line2' in result or 'running' in result.lower()

    def test_status_completed_variant_includes_elapsed(self, shell_cmd_tool):
        """__status for a completed task includes elapsed time in the status label."""
        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(tool_id=6, agent_name='test_agent', command='echo done',
                              pid=66666, completed=True, return_code=0,
                              stdout_lines=['done'])
        _setup_task(tracker, task)
        _make_tool_with_tracker(shell_cmd_tool, tracker)

        result = shell_cmd_tool.call('{"command": "__status", "tool_id": 6, "execution_mode": "async"}')
        assert '⟨shell_cmd status⟩' in result
        assert 'completed' in result.lower()
        assert 'elapsed' in result or 's)' in result, \
            f"__status (completed) missing elapsed time: {result!r}"

    def test_status_running_variant_includes_elapsed(self, shell_cmd_tool):
        """__status for a running task includes elapsed time in the status label."""
        tracker = AsyncShellTracker(pool=None)
        task = _make_running_task(tool_id=7, command='sleep 100', pid=77777)
        _setup_task(tracker, task)
        _make_tool_with_tracker(shell_cmd_tool, tracker)

        result = shell_cmd_tool.call('{"command": "__status", "tool_id": 7, "execution_mode": "async"}')
        assert '⟨shell_cmd status⟩' in result
        assert 'running' in result.lower()
        assert 'elapsed' in result, f"__status (running) missing elapsed time: {result!r}"

    def test_regular_command_without_justification_raises(self, shell_cmd_tool, mock_tracker):
        _make_tool_with_tracker(shell_cmd_tool, mock_tracker)
        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call('{"command": "ls -la"}')
        assert 'justification' in str(exc_info.value).lower()

    def test_tool_id_with_non_control_command_sends_as_stdin(self, shell_cmd_tool):
        """Non-control commands with tool_id are sent as stdin input (no justification needed).

        This is intentional: tool_id means interacting with an already-approved running shell.
        See _handle_control_command else branch: 'Send as stdin input to the running process'.
        """
        tracker = AsyncShellTracker(pool=None)
        task = _make_running_task(tool_id=1, command='cat')
        _setup_task(tracker, task)

        # Mock send_input to verify it's called with correct args
        tracker.send_input = MagicMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        mock_pool.llm_cfg = {}
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        # Non-control command with tool_id should NOT raise — it attempts stdin input
        result = shell_cmd_tool.call('{"command": "echo test", "tool_id": 1}')

        # Verify tracker.send_input was actually called with correct args (agent_name, tool_id, command)
        tracker.send_input.assert_called_once_with('test_agent', 1, 'echo test')

    def test_control_command_with_non_numeric_tool_id_raises(self, shell_cmd_tool):
        """Control commands with non-numeric tool_id should raise a clear error."""
        _make_tool_with_tracker(shell_cmd_tool, MagicMock())
        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call('{"command": "__status", "tool_id": "abc"}')
        assert 'tool_id' in str(exc_info.value).lower() and 'numeric' in str(exc_info.value).lower()

    def test_control_command_without_tool_id_raises(self, shell_cmd_tool):
        """Control commands without tool_id should raise a clear error."""
        _make_tool_with_tracker(shell_cmd_tool, MagicMock())
        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call('{"command": "__status"}')
        assert 'tool_id' in str(exc_info.value).lower()

    def test_regular_command_with_justification_works(self, shell_cmd_tool):
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='file1\nfile2\n') as mock_exec:
            result = shell_cmd_tool.call('{"command": "ls -la", "justification": "listing files"}')
            mock_exec.assert_called_once()
            assert 'file1' in result

    def test_async_launch_without_justification_raises(self, shell_cmd_tool, mock_tracker):
        _make_tool_with_tracker(shell_cmd_tool, mock_tracker)
        with pytest.raises(ValueError) as exc_info:
            shell_cmd_tool.call('{"command": "echo hello", "execution_mode": "async"}')
        assert 'justification' in str(exc_info.value).lower()

    def test_async_launch_with_justification_works(self, shell_cmd_tool, mock_tracker):
        tracker = AsyncShellTracker(pool=None)
        # New launch() signature: (tool_id, pid, early_output, completed_early, return_code)
        tracker.launch = MagicMock(return_value=(1, 12345, None, False, None))

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        mock_pool.llm_cfg = {}
        mock_pool.operation_manager.request_user_approval.return_value = (True, '')
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call('{"command": "echo hello", "execution_mode": "async", "justification": "test"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result
        assert 'Tool ID: 1' in result

    def test_async_launch_early_completion_returns_completed_message(self, shell_cmd_tool, mock_tracker):
        """When launch() detects early completion, return completed message instead of launched."""
        tracker = AsyncShellTracker(pool=None)
        # Simulate early completion with output captured during launch wait
        tracker.launch = MagicMock(return_value=(1, 0, ['hello world'], True, 0))

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        mock_pool.llm_cfg = {}
        mock_pool.operation_manager.request_user_approval.return_value = (True, '')
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call('{"command": "echo hello", "execution_mode": "async", "justification": "test"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd completed⟩' in result
        assert 'Tool ID: 1' in result
        assert 'success' in result
        assert 'hello world' in result

    def test_async_launch_early_output_appended_to_launched_message(self, shell_cmd_tool, mock_tracker):
        """When launch() detects early output but process still running, append to launched message."""
        tracker = AsyncShellTracker(pool=None)
        # Simulate early output with process still running
        tracker.launch = MagicMock(return_value=(1, 0, ['starting...', 'loading...'], False, None))

        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        mock_pool.llm_cfg = {}
        mock_pool.operation_manager.request_user_approval.return_value = (True, '')
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool.call('{"command": "long_running_task", "execution_mode": "async", "justification": "test"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result
        assert 'Tool ID: 1' in result
        assert 'Initial output:' in result
        assert 'starting...' in result
        assert 'loading...' in result


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
        # New launch() signature: (tool_id, pid, early_output, completed_early, return_code)
        tracker.launch = MagicMock(return_value=(1, 12345, None, False, None))
        mock_pool = MagicMock()
        mock_pool._async_shell_tracker = tracker
        mock_pool.llm_cfg = {}  # Must be dict so .get('shell_char_limit', default) returns default
        mock_pool.operation_manager.request_user_approval.return_value = (True, '')  # Auto-approve for tests
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'
        return tracker

    def test_timeout_gt_60_auto_switches_to_async(self, shell_cmd_tool, mock_tracker):
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 120, "justification": "test"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result

    def test_execution_mode_sync_with_large_timeout_stays_sync(self, shell_cmd_tool):
        """Headline regression: execution_mode='sync' with a large timeout (300) must stay sync, never auto-async."""
        tracker = self._tool_with_tracker(shell_cmd_tool)
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='output') as mock_exec:
            result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 300, "execution_mode": "sync", "justification": "sync"}')
            mock_exec.assert_called_once()
            tracker.launch.assert_not_called()
            assert 'output' in result

    @pytest.mark.parametrize('timeout', [30, 60])
    def test_timeout_at_or_below_60_stays_sync(self, shell_cmd_tool, timeout):
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='output') as mock_exec:
            shell_cmd_tool.agent_pool = MagicMock()
            shell_cmd_tool.agent_name = 'test_agent'
            result = shell_cmd_tool.call(f'{{"command": "echo hello", "timeout": {timeout}, "justification": "sync"}}')
            mock_exec.assert_called_once()
            assert 'output' in result

    def test_explicit_async_ignores_timeout_threshold(self, shell_cmd_tool, mock_tracker):
        """execution_mode='async' with a small timeout forces background (forced-async regression)."""
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "echo hello", "execution_mode": "async", "timeout": 1, "justification": "async"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result

    def test_omitted_execution_mode_with_large_timeout_auto_async(self, shell_cmd_tool, mock_tracker):
        """Omitted execution_mode + timeout>60 → AUTO-ASYNC (auto rule regression)."""
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 120, "justification": "test"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result

    def test_null_execution_mode_with_large_timeout_auto_async(self, shell_cmd_tool, mock_tracker):
        """Explicit null execution_mode + timeout>60 behaves as AUTO (same as omission)."""
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 120, "execution_mode": null, "justification": "test"}')
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

    def test_explicit_auto_with_large_timeout_auto_async(self, shell_cmd_tool, mock_tracker):
        """Explicit execution_mode='auto' + timeout>60 behaves identically to omission → AUTO-ASYNC."""
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 120, "execution_mode": "auto", "justification": "test"}')
        tracker.launch.assert_called_once()
        assert '⟨shell_cmd launched⟩' in result

    def test_explicit_auto_defaults_heartbeat_to_30(self, shell_cmd_tool, mock_tracker):
        """Explicit execution_mode='auto' auto-async should default heartbeat_interval to 30s when not set."""
        tracker = self._tool_with_tracker(shell_cmd_tool, mock_tracker)
        # No heartbeat_interval specified; explicit 'auto' + timeout>60 → auto-async
        shell_cmd_tool.call('{"command": "echo hello", "timeout": 120, "execution_mode": "auto", "justification": "test"}')
        tracker.launch.assert_called_once()
        call_kwargs = tracker.launch.call_args
        assert call_kwargs.kwargs.get('heartbeat_interval') == 30, \
            f"Expected heartbeat_interval=30 for explicit-auto auto-async, got {call_kwargs.kwargs.get('heartbeat_interval')}"

    def test_explicit_auto_with_small_timeout_stays_sync(self, shell_cmd_tool):
        """Explicit execution_mode='auto' + timeout<=60 must stay sync (no forced async)."""
        tracker = self._tool_with_tracker(shell_cmd_tool)
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='output') as mock_exec:
            result = shell_cmd_tool.call('{"command": "echo hello", "timeout": 30, "execution_mode": "auto", "justification": "sync"}')
            mock_exec.assert_called_once()
            tracker.launch.assert_not_called()
            assert 'output' in result

    def test_explicit_auto_without_timeout_stays_sync(self, shell_cmd_tool):
        """Explicit execution_mode='auto' with no timeout must stay sync (guards the `timeout is not None` condition).

        The schema types 'timeout' as integer (no null), so a JSON string or dict carrying "timeout": null is
        rejected by jsonschema before dispatch — an explicit null can never reach line 213. The only way
        `params.get('timeout')` yields None through the real tool path is by OMITTING the key, which is what
        this test exercises: auto mode + absent timeout → run_async stays False (the `timeout is not None`
        clause at line 223 short-circuits) → sync.
        """
        tracker = self._tool_with_tracker(shell_cmd_tool)
        with patch.object(shell_cmd_tool, '_execute_sync', return_value='output') as mock_exec:
            result = shell_cmd_tool.call('{"command": "echo hello", "execution_mode": "auto", "justification": "test"}')
            mock_exec.assert_called_once()
            tracker.launch.assert_not_called()
            assert 'output' in result

    def test_invalid_execution_mode_rejected_by_schema(self, shell_cmd_tool):
        """An execution_mode value outside the enum ('auto'/'sync'/'async'/null) must be rejected by schema validation."""
        self._tool_with_tracker(shell_cmd_tool)
        with pytest.raises(jsonschema.exceptions.ValidationError):
            shell_cmd_tool.call('{"command": "echo hello", "execution_mode": "bogus", "justification": "test"}')


# ============================================================================
# Console window suppression (regression)
# ============================================================================

class TestConsoleWindowSuppression:
    """Guard against the env-var check in AsyncShellTracker.launch() regressing.

    conftest.py sets QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW=1 for all
    pytest runs, so even if a caller explicitly passes console_window=True,
    the tracker must override it to False.
    """

    def test_launch_forces_console_window_false_when_env_set(self):
        """console_window=True is overridden to False when the disable env var is set."""
        import os

        # conftest.py guarantees this is set for all tests, but assert defensively
        assert os.environ.get("QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW") == "1", \
            "Expected conftest to set QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW=1"

        tracker = AsyncShellTracker(pool=None)
        tool_id, _, early_output, completed_early, return_code = tracker.launch(
            agent_name='test_agent',
            command='echo suppressed',
            heartbeat_interval=-1,
            timeout=5,
            console_window=True,  # Simulate caller requesting a visible window
        )

        task = tracker._get_task('test_agent', tool_id)
        assert task.console_window is False, \
            "console_window should be forced to False when QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW=1"


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
            result = shell_cmd_tool.call('{"command": "__wait", "tool_id": 1, "execution_mode": "async"}')

        assert state['elapsed'] >= 29.0, f"__wait waited {state['elapsed']:.1f}s, expected ~30s"
        assert state['elapsed'] <= 31.0, f"__wait waited {state['elapsed']:.1f}s, expected ~30s"
        assert 'No new output' in result


# ============================================================================
# Real execution tests (minimal set — verify actual command output and exit codes)
# ============================================================================

class TestRealExecution:
    """Tests that execute real commands via the async shell tracker.

    These verify end-to-end behavior: actual process launch, output capture,
    and exit code recording. Kept small to avoid slow tests.
    """

    def test_real_echo_output_captured(self):
        """A simple echo command's stdout is captured correctly."""
        import os as _os

        pool = MagicMock()
        pool.messages = []
        pool.enqueue_message = lambda agent, msg: pool.messages.append((agent, msg))
        pool.llm_cfg = {}

        tracker = AsyncShellTracker(pool=pool)

        expected = 'hello-from-real-process'
        if _os.name == 'nt':
            cmd = f'cmd /c echo {expected}'
        else:
            cmd = f'sh -c "echo {expected}"'

        tool_id, _, early_output, completed_early, return_code = tracker.launch(
            agent_name='test_agent',
            command=cmd,
            heartbeat_interval=-1,
            timeout=5,
        )

        # Handle both early completion and normal completion paths
        if completed_early:
            assert return_code == 0, f"Expected exit code 0, got {return_code}"
            assert early_output is not None and len(early_output) > 0
            assert any(expected in line for line in early_output), \
                f"Output '{expected}' not found in: {early_output}"
        else:
            time.sleep(1.0)
            task = tracker._get_task('test_agent', tool_id)
            with task._lock:
                assert task.completed is True, "Task should be completed"
                assert task.return_code == 0, f"Expected exit code 0, got {task.return_code}"
                all_output = task.stdout_lines + (task.stderr_lines if hasattr(task, 'stderr_lines') else [])
                assert any(expected in line for line in all_output), \
                    f"Output '{expected}' not found in: {all_output}"

    def test_real_python_exit_code_zero(self):
        """A Python one-liner that exits 0 is recorded correctly."""
        pool = MagicMock()
        pool.messages = []
        pool.enqueue_message = lambda agent, msg: pool.messages.append((agent, msg))
        pool.llm_cfg = {}

        tracker = AsyncShellTracker(pool=pool)

        tool_id, _, early_output, completed_early, return_code = tracker.launch(
            agent_name='test_agent',
            command='python -c "print(\'ok\')"',
            heartbeat_interval=-1,
            timeout=5,
        )

        if completed_early:
            assert return_code == 0, f"Expected exit code 0, got {return_code}"
        else:
            time.sleep(1.0)
            task = tracker._get_task('test_agent', tool_id)
            with task._lock:
                assert task.completed is True
                assert task.return_code == 0, f"Expected exit code 0, got {task.return_code}"

    def test_real_command_nonzero_exit(self):
        """A command that exits non-zero has its exit code recorded."""
        import os as _os

        pool = MagicMock()
        pool.messages = []
        pool.enqueue_message = lambda agent, msg: pool.messages.append((agent, msg))
        pool.llm_cfg = {}

        tracker = AsyncShellTracker(pool=pool)

        if _os.name == 'nt':
            cmd = 'cmd /c exit /b 7'
        else:
            cmd = 'sh -c "exit 7"'

        tool_id, _, early_output, completed_early, return_code = tracker.launch(
            agent_name='test_agent',
            command=cmd,
            heartbeat_interval=-1,
            timeout=5,
        )

        if completed_early:
            assert return_code == 7, f"Expected exit code 7, got {return_code}"
        else:
            time.sleep(1.0)
            task = tracker._get_task('test_agent', tool_id)
            with task._lock:
                assert task.completed is True
                assert task.return_code == 7, f"Expected exit code 7, got {task.return_code}"