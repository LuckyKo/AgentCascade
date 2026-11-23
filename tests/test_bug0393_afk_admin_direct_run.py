"""BUG_0393 — AFK Admin direct-run dispatch tests.

Hermetic: the REAL nested ``_afk_admin_nudge`` closure in create_app is driven via
TestClient with a self-contained pool double; the Admin is stubbed (``run_child_core``)
so no LLM work happens. The generation-supersede gate is real (real ``ExecutionEngine``
construction), so the supersede-resistance test exercises the actual gate code.
"""

import threading
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from agent_cascade.agent_instance import AgentState  # noqa: F401 (kept for parity with BUG_0359)


class _AfkInstance:
    def __init__(self):
        self.conversation = []
        self._compression_lock = threading.Lock()
        self.instance_name = 'w1'


class _AfkOm:
    def __init__(self, afk_mode: str = 'admin'):
        self.afk_enabled = True
        self.afk_message = 'AFK basic nudge text'
        self.afk_mode = afk_mode
        self.base_dir = Path(tempfile.gettempdir())


class _AfkPool:
    def __init__(self, afk_mode: str = 'admin'):
        self.stopped = False
        self._instance = _AfkInstance()
        self.operation_manager = _AfkOm(afk_mode)
        self._enqueue_calls = []
        self._auto_security = None
        self._run_generation = 0
        self.instances = {}
        self._has_messages = False
        self._afk_nudge_lock = threading.Lock()
        self._afk_last_nudge_ts = 0.0
        self._afk_admin_in_flight = False
        self._acquire_slot_calls = 0

    def set_auto_security(self, value):
        self._auto_security = bool(value)

    def get_instance(self, name):
        return self._instance

    def has_pending(self, name):
        return False

    def has_messages(self, name):
        return self._has_messages

    def get_template(self, name):
        return name == 'Admin'  # Admin template available

    def enqueue_message(self, name, content):
        self._enqueue_calls.append((name, content))

    def _acquire_slot(self, *a, **k):
        self._acquire_slot_calls += 1
        return MagicMock()

    def is_instance_terminated(self, name):
        return False

    def _mark_activity(self, name):
        pass


def _drive_dispatch(monkeypatch, pool, wait_for='enqueue'):
    """Start the app; return after the dispatch has fired (or the timeout elapses)."""
    from agent_cascade import api_server
    import agent_cascade.utils.wakeup_helpers as wh
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api_server, 'AFK_NUDGE_TICK_SECS', 0.02)
    monkeypatch.setattr(api_server, 'AFK_NUDGE_COOLDOWN', 1.0)
    monkeypatch.setattr(wh, 'relaunch_idle_agent', lambda p, n: False)

    app = api_server.create_app(agents=[], agent_pool=pool, config={'session_name': 'w1'})
    with TestClient(app):
        deadline = time.monotonic() + 3.0
        if wait_for == 'enqueue':
            while time.monotonic() < deadline and not pool._enqueue_calls:
                time.sleep(0.02)
        else:
            # wait for the dispatch to have fired (the spy flips the flag)
            while time.monotonic() < deadline and not getattr(pool, '_dispatch_fired', False):
                time.sleep(0.02)
            time.sleep(0.4)  # give the post-run gate a beat to (or not) enqueue
    return pool._enqueue_calls


def _enqueue_text(content):
    return content if isinstance(content, str) else str(content)


def test_dispatch_calls_run_child_core_with_pinned_kwargs(monkeypatch):
    """Direct-run dispatch: fresh engine, _is_advisor_run=True, force_fresh=False,
    caller_name=root, child_depth=1, extract_task=True."""
    pool = _AfkPool()
    seen = {}

    def spy(*, engine, pool, agent_class, instance_name, args, caller_name,
           child_depth, force_fresh, prefix, extract_task):
        seen.update(engine=engine, agent_class=agent_class, instance_name=instance_name,
                   args=args, caller_name=caller_name, child_depth=child_depth,
                   force_fresh=force_fresh, extract_task=extract_task, prefix=prefix)
        pool._dispatch_fired = True
        return 'Fix the X bug'

    monkeypatch.setattr('agent_cascade.child_runner.run_child_core', spy)

    calls = _drive_dispatch(monkeypatch, pool)

    assert seen, 'run_child_core was not called'
    assert seen['force_fresh'] is False
    assert seen['caller_name'] == 'w1'
    assert seen['child_depth'] == 1
    assert seen['extract_task'] is True
    assert seen['agent_class'] == 'Admin'
    assert seen['instance_name'] == 'AFK_Admin'
    assert seen['prefix'] == 'Agent'
    assert seen['args']['task'].startswith('You are the fleet overseer')
    assert seen['engine']._is_advisor_run is True
    text = _enqueue_text(calls[0][1])
    assert text.strip().startswith('Context: This is a message from AFK_Admin.')
    assert 'Task: Fix the X bug' in text


def test_dispatch_posts_wrapped_task(monkeypatch):
    """The enqueued text is the task wrapped as a normal call_agent task
    (Context: ... / Task: ... / Please help with this task.), not a bare line,
    and carries no STATUS/FINDINGS preamble."""
    pool = _AfkPool()

    def spy(*, engine, pool, agent_class, instance_name, args, caller_name,
           child_depth, force_fresh, prefix, extract_task):
        pool._dispatch_fired = True
        return 'Run the regression suite'

    monkeypatch.setattr('agent_cascade.child_runner.run_child_core', spy)

    calls = _drive_dispatch(monkeypatch, pool)
    text = _enqueue_text(calls[0][1])

    assert text.strip() == ('Context: This is a message from AFK_Admin.\n\n'
                           'Task: Run the regression suite\n\n'
                           'Please help with this task.')
    assert 'STATUS:' not in text
    assert 'FINDINGS:' not in text


def test_failure_posts_basic_nudge_fallback(monkeypatch):
    """Failure (exception from the run) -> deterministic basic-nudge text."""
    pool = _AfkPool()

    def spy(*, engine, pool, **kwargs):
        pool._dispatch_fired = True
        raise RuntimeError('boom')

    monkeypatch.setattr('agent_cascade.child_runner.run_child_core', spy)

    calls = _drive_dispatch(monkeypatch, pool)

    assert _enqueue_text(calls[0][1]).strip() == 'AFK basic nudge text'


def test_no_block_result_posted_verbatim_as_wrapped_task(monkeypatch):
    """A non-empty result with no [TASK]…[END] block is posted verbatim as the task
    (the whole message), wrapped in the call_agent task frame — NOT a nudge.
    (This exercises the dispatch-wrap layer; the parser's no-block fallback itself is
    covered by test_extract_task_block_unit.)"""
    pool = _AfkPool()

    def spy(*, engine, pool, **kwargs):
        pool._dispatch_fired = True
        return "[Agent 'AFK_Admin' Rejected]: advisor denied the delegation"

    monkeypatch.setattr('agent_cascade.child_runner.run_child_core', spy)

    calls = _drive_dispatch(monkeypatch, pool)
    text = _enqueue_text(calls[0][1])

    assert text.strip().startswith('Context: This is a message from AFK_Admin.')
    assert 'advisor denied the delegation' in text


def test_empty_result_posts_basic_nudge_fallback(monkeypatch):
    """An empty result -> basic-nudge fallback."""
    pool = _AfkPool()

    def spy(*, engine, pool, **kwargs):
        pool._dispatch_fired = True
        return '   '

    monkeypatch.setattr('agent_cascade.child_runner.run_child_core', spy)

    calls = _drive_dispatch(monkeypatch, pool)

    assert _enqueue_text(calls[0][1]).strip() == 'AFK basic nudge text'


def test_daemon_never_preacquires_slot(monkeypatch):
    """No endpoint-slot pre-acquisition in the daemon thread."""
    pool = _AfkPool()

    def spy(*, engine, pool, **kwargs):
        pool._dispatch_fired = True
        return 'Continue the migration'

    monkeypatch.setattr('agent_cascade.child_runner.run_child_core', spy)

    _drive_dispatch(monkeypatch, pool)

    assert pool._acquire_slot_calls == 0


def test_supersede_resistance_survives_mid_run_gen_bump(monkeypatch):
    """A real root run bumping pool._run_generation mid-Admin must NOT abort the run —
    the fresh engine is tagged _is_advisor_run=True so _is_terminal_stop ignores gen."""
    pool = _AfkPool()

    def spy(*, engine, pool, **kwargs):
        assert engine._is_advisor_run is True
        pool._run_generation = 1  # a real root run starts mid-Admin
        engine._my_generation = 0  # captured at gen 0
        assert engine._is_terminal_stop('AFK_Admin') is False
        pool._dispatch_fired = True
        return 'Fix the flaky test'

    monkeypatch.setattr('agent_cascade.child_runner.run_child_core', spy)

    calls = _drive_dispatch(monkeypatch, pool)
    text = _enqueue_text(calls[0][1])
    assert text.strip().startswith('Context: This is a message from AFK_Admin.')
    assert 'Task: Fix the flaky test' in text


def test_is_generating_recheck_gates_enqueue(monkeypatch):
    """If a real run starts while the Admin runs, the result must NOT be enqueued."""
    pool = _AfkPool()
    import agent_cascade.api_server as api

    flipped = {'value': False}

    def spy(*, engine, pool, **kwargs):
        flipped['value'] = True
        pool._dispatch_fired = True
        return 'Do the next step'

    monkeypatch.setattr('agent_cascade.child_runner.run_child_core', spy)

    real_is_generating = api._is_generating

    def fake_is_generating(session):
        return flipped['value']

    monkeypatch.setattr(api, '_is_generating', fake_is_generating)

    calls = _drive_dispatch(monkeypatch, pool, wait_for='fired')

    assert calls == []


def test_extract_task_block_unit():
    """_extract_task_block: extracts the LAST [TASK]…[END] block; falls back to the whole
    message when no block; returns '' for empty input."""
    from agent_cascade.child_runner import _extract_task_block

    # block present -> extracts the task
    assert _extract_task_block('analysis\n[TASK] Fix bug BUG_0389 [END]') == 'Fix bug BUG_0389'
    # multi-line task inside the block
    assert _extract_task_block('[TASK]\nRun the full suite\n[END]') == 'Run the full suite'
    # no block -> whole message
    assert _extract_task_block('Just a plain message.') == 'Just a plain message.'
    # empty -> ''
    assert _extract_task_block('') == ''
    # LAST block wins (the real task is always the final element of the message)
    assert _extract_task_block('[TASK] a [END] x [TASK] b [END]') == 'b'
    # false-positive prose: literal [TASK]…[END] mentions in the prose are ignored; the
    # real task block (the last one) wins. This is the exact bug where the Admin's prose
    # described the format and the old first-match grabbed a bare '…'.
    assert _extract_task_block(
        'I emitted a [TASK]…[END] block. Then more prose [TASK]…[END] again. '
        '[TASK] Close out the loose ends [END]'
    ) == 'Close out the loose ends'
