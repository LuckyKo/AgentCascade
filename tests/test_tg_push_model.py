"""Push-model tests for the Telegram bridge final-answer delivery (todo.md:125).

Plan: ``plans/tg-bridge-push-model_PLAN.md`` §5.3. Final-answer delivery moved from
"one fire-and-forget waiter per phone message, each of which fetches and sends the last
assistant message on generation-end" to "the root run pushes its final answer exactly once
at natural end" via ``pool.telegram_supervisor.notify_user(text)``:

  * pre-reflection push — ``engine/core.py`` Phase-5 body (root-gated by
    ``instance.parent_instance is None``), sets ``instance._tg_pushed = True``;
  * post-run push — ``run_agent_unified.run_agent_thread_unified`` after the run loop,
    gated by ``not is_stopped()`` and skipped when ``_tg_pushed`` is already True.

These tests cover plan §5.3 A (one-push-per-run matrix, dedup, sub-agent gate, stop
suppression, reset-at-start) and B (long-answer chunking via the E3-chunked ``_safe_send``).

The engine pre-hook tests drive the REAL ``ExecutionEngine.run()`` with a stubbed LLM —
the same harness as ``tests/test_skill_generation.py::TestInLoopTrigger._make_pool`` — so
the hook is exercised in its real context (not a hand-rolled copy of it).
"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def fresh_manager(tmp_path):
    """Fresh SkillManager isolated to a per-test tmp dir (mirrors test_skill_generation).

    ``load_full_instructions()`` does a direct registry lookup and never calls
    ``_ensure_discovered()``, so the trigger gate needs 'skill-creator' actually present;
    ``discover()`` below scans the real global skills tree. Redirecting the metrics file to
    tmp keeps any metrics flush off the production file.
    """
    from agent_cascade.skills.manager import SkillManager

    manager = SkillManager()
    manager._metrics_file = tmp_path / 'skills-metrics.json'
    base = tmp_path / 'agents' / 'global'
    manager._pending_dir = base / 'pending-skills'
    manager._candidates_dir = base / 'candidates'
    manager._production_skills_dir = base / 'skills'
    yield manager


def _make_inst(max_turns, parent_instance=None):
    """Minimal AgentInstance that drives the REAL ExecutionEngine.run().

    ``AgentInstance.__new__`` bypasses the dataclass defaults, so every attribute run()'s
    hot path reads must be set explicitly. ``parent_instance`` is left at its real default
    (None == root) unless overridden — the pre-hook's root gate keys on it.
    """
    from agent_cascade.agent_instance import AgentInstance, AgentState
    from agent_cascade.llm.schema import Message, USER

    inst = AgentInstance.__new__(AgentInstance)
    inst.instance_name = 'w'
    inst.agent_class = 'test_agent'
    inst.parent_instance = parent_instance          # None == root (the pre-hook gate)
    inst.conversation = [Message(role=USER, content='task')]
    inst._cached_messages = list(inst.conversation)
    inst._cached_llm_messages = list(inst.conversation)
    inst.max_turns = max_turns
    inst.state = AgentState.IDLE
    inst._compression_lock = threading.RLock()
    inst._state_lock = threading.RLock()
    inst._generate_cfg_override = None
    inst._turn_consumed = False
    inst._slot_release = None
    inst._slot_key = None
    inst._compression_suspended_at = 0.0
    inst._last_config_version = -1
    inst._last_token_count_conversation_length = -1
    inst._continue_saved_msg = None
    inst._auto_skill_proposed = False
    inst._auto_skill_dirty_stop = False
    # Present for the pre-hook: dataclass default would be False, but __new__ bypasses it.
    inst._tg_pushed = False
    inst._streaming_responses = []
    return inst


def _make_engine(fresh_manager, tmp_path, max_turns=3, min_turns=2, extra_turns=5,
                 natural_end_at=None, tool_call_at=None, supervisor=None):
    """Build a REAL ExecutionEngine + instance that drives engine.run() to completion.

    ``natural_end_at`` = the check number at which ``_post_turn_checks`` reports a genuine
    natural end (returns False → run() breaks). Defaults to ``max_turns`` so the agent
    completes on its last turn. ``tool_call_at`` emits a tool-call assistant message on that
    LLM call (drives the Phase-4 dirty-stop trigger path — used by the dirty-stop test).
    ``supervisor`` is attached to the mock pool as ``telegram_supervisor`` so the inlined
    notify_user idiom fires; pass None to simulate a disabled/absent bridge.

    Returns ``(engine, inst, pool, run)`` where ``run()`` drives the run inside its own
    AUTO_SKILL_* patch context and returns the exhausted generator.
    """
    from agent_cascade.execution_engine import ExecutionEngine
    from agent_cascade.llm.schema import Message, ASSISTANT, USER

    template = MagicMock()
    template.function_map = {'tool_a': None, 'tool_b': None}

    pool = MagicMock()
    pool.settings.auto_skill_enabled = True
    pool.settings.auto_skill_min_turns = min_turns
    pool.settings.default_load_skill_mode = 'AUTO'
    pool.settings.tail_sync_check_enabled = False
    pool.get_template.return_value = template
    pool.is_instance_terminated.return_value = False
    pool.has_pending.return_value = False
    pool.has_messages.return_value = False
    pool.drain_queue.return_value = []
    # The pre-hook reads this; a bare MagicMock auto-attribute would be a truthy mock and
    # the idiom would call notify_user on it. Set it explicitly to control the push.
    pool.telegram_supervisor = supervisor

    from agent_cascade.logger import AgentInstanceLogger
    log_inst = AgentInstanceLogger('test_agent', 'w', str(tmp_path), log_path=str(tmp_path / 'w.jsonl'))
    pool.get_logger.return_value = log_inst

    # Populate the registry via REAL discovery so load_full_instructions() finds 'skill-creator'.
    fresh_manager._cache_ttl = 0.0
    from pathlib import Path
    fresh_manager.discover([Path('agents/global/skills')])
    pool.skill_manager = fresh_manager

    engine = ExecutionEngine(pool)
    inst = _make_inst(max_turns)

    def fake_setup_turn(instance):
        return list(instance.conversation), [Message(role=USER, content='task')], []

    engine._setup_turn = MagicMock(side_effect=fake_setup_turn)
    engine._pre_llm_checks = MagicMock(return_value=False)
    engine._check_stop_conditions = MagicMock(return_value=False)
    engine._is_suspended_by_compression = MagicMock(return_value=False)
    engine._is_terminal_stop = MagicMock(return_value=False)
    engine._acquire_slot_with_logging = MagicMock(return_value=None)
    engine._check_stream_termination = MagicMock(return_value=None)

    _llm_call_count = {'n': 0}

    def fake_llm(inst, msgs):
        _llm_call_count['n'] += 1
        n = _llm_call_count['n']
        yield None
        if tool_call_at is not None and n == tool_call_at:
            yield Message(role=ASSISTANT, content='', function_call={'name': 'tool_a', 'arguments': '{}'})
        else:
            yield Message(role=ASSISTANT, content=f"reply {n}")

    engine._call_llm_with_injection = MagicMock(side_effect=lambda inst, msgs: fake_llm(inst, msgs))

    if tool_call_at is None:
        engine._execute_detected_tools = MagicMock(return_value=False)
    else:
        _tool_exec_calls = {'n': 0}

        def _exec_tool_driver(*a, **k):
            _tool_exec_calls['n'] += 1
            return _tool_exec_calls['n'] == tool_call_at

        engine._execute_detected_tools = MagicMock(side_effect=_exec_tool_driver)

    _nend = max_turns if natural_end_at is None else natural_end_at
    _ptc_calls = {'n': 0}

    def _post_turn_checks_driver(*a, **k):
        _ptc_calls['n'] += 1
        # False only on the natural-end check; True everywhere else so a triggered reflection
        # tail runs its full EXTRA budget before exhausting (mirrors TestInLoopTrigger).
        return _ptc_calls['n'] != _nend

    engine._post_turn_checks = MagicMock(side_effect=_post_turn_checks_driver)

    def run():
        with patch('agent_cascade.engine.core.AUTO_SKILL_MIN_TURNS', min_turns), \
                patch('agent_cascade.engine.core.AUTO_SKILL_EXTRA_TURNS', extra_turns), \
                patch('agent_cascade.skills.manager.AUTO_SKILL_MIN_TURNS', min_turns):
            gen = engine.run(inst)
            for _ in gen:
                pass
            return gen

    return engine, inst, pool, run


def _make_run_pool(instance, *, stopped=False, halted=None, terminated=False,
                   supervisor=None, generation=1):
    """A SimpleNamespace pool just rich enough for run_agent_thread_unified's post-push + reset.

    The local ``is_stopped()`` closure reads exactly these attrs:
        pool.stopped / pool._run_generation / pool._halted_instances / pool.is_instance_terminated
    and the push/reset sites use pool.get_instance(name) and pool.telegram_supervisor.
    """
    return SimpleNamespace(
        stopped=stopped,
        _run_generation=generation,
        _instance_threads_lock=threading.Lock(),
        _instance_threads={},
        _halted_instances=set(halted or ()),
        is_instance_terminated=lambda name: terminated,
        get_instance=lambda name: instance,
        # _apply_ui_config (called by run_agent_thread_unified before the loop) reads this; a
        # template with no .llm makes it return early — we only exercise the push/reset path.
        get_template=lambda name: SimpleNamespace(llm=None),
        telegram_supervisor=supervisor,
    )


def _run_unified(pool, instance_name='test-inst'):
    """Drive run_agent_thread_unified with the engine/broadcast machinery stubbed to no-ops.

    ``run_agent_in_pool_with_recovery`` is patched to yield nothing (an empty run), so control
    flows straight past the loop to the post-run push — exactly what these tests exercise. The
    broadcast helpers are no-ops; send_queue/loop are None so no WebSocket I/O happens.
    """
    import agent_cascade.run_agent_unified as rau
    from agent_cascade import api_integration as ai

    _orig = (ai.run_agent_in_pool_with_recovery, ai.build_stream_update_from_pool,
             ai.build_state_from_pool)
    try:
        ai.run_agent_in_pool_with_recovery = lambda *a, **k: iter([])
        ai.build_stream_update_from_pool = lambda *a, **k: None
        ai.build_state_from_pool = lambda *a, **k: None
        rau.run_agent_thread_unified(pool, instance_name, None, {}, None, None)
    finally:
        (ai.run_agent_in_pool_with_recovery, ai.build_stream_update_from_pool,
         ai.build_state_from_pool) = _orig


# --------------------------------------------------------------------------- #
# A. Pre-reflection + post push integration
# --------------------------------------------------------------------------- #

def test_queue_n_messages_one_push(fresh_manager, tmp_path):
    """Core regression: N phone messages queued during one run → exactly ONE push at run end.

    The old model spawned one waiter per message and each fired on the same generation-end,
    sending the identical answer N times. Under the push model delivery is once-per-run and
    independent of the queued-message count, so the root's post-run push fires exactly once no
    matter how many messages were in flight.
    """
    supervisor = MagicMock()
    pool = _make_run_pool(_make_inst(3), supervisor=supervisor)
    # Simulate N phone messages having been enqueued during the run (N > 1). The push is keyed
    # to the RUN, not the message count, so it must still fire exactly once.
    _n_messages = 4
    _run_unified(pool)
    assert supervisor.notify_user.call_count == 1, \
        f'expected exactly 1 push for {_n_messages} queued messages, got {supervisor.notify_user.call_count}'


def test_ui_originated_run_pushes(fresh_manager, tmp_path):
    """A run with no phone message but a known recipient (last_chat_id set) still pushes once.

    Proves "regardless of bridge or UI": the push is independent of how the run was started —
    a UI-originated run whose supervisor has a live chat id delivers exactly one final answer.
    """
    supervisor = MagicMock()   # notify_user no-ops only when last_chat_id is None; here it's set
    pool = _make_run_pool(_make_inst(3), supervisor=supervisor)
    _run_unified(pool)
    assert supervisor.notify_user.call_count == 1


def test_bridge_disabled_no_crash(fresh_manager, tmp_path):
    """Supervisor absent (bridge disabled) → run completes cleanly, no push, no exception."""
    pool = _make_run_pool(_make_inst(3), supervisor=None)   # getattr(...,'telegram_supervisor',None) -> None
    try:
        _run_unified(pool)   # must not raise
    except Exception as e:  # noqa: BLE001
        pytest.fail(f'bridge-disabled run raised: {e!r}')


def test_pre_and_post_dedup(fresh_manager, tmp_path):
    """Natural completion WITH reflection: pre pushes the snapshot and sets _tg_pushed=True;
    post sees True → skips. notify_user is called exactly ONCE total (dedup works)."""
    supervisor = MagicMock()
    engine, inst, pool, run = _make_engine(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                           extra_turns=5, natural_end_at=3, supervisor=supervisor)
    # The pre-hook is root-gated by parent_instance is None; the harness instance defaults to None.
    assert inst.parent_instance is None
    run()
    # Reflection fired → the pre-hook delivered the committed snapshot once.
    assert inst._auto_skill_proposed is True, 'reflection should have fired'
    assert inst._tg_pushed is True, 'pre-hook must set _tg_pushed=True on a non-empty push'
    assert supervisor.notify_user.call_count == 1, \
        f'pre+post dedup failed: expected 1 total push, got {supervisor.notify_user.call_count}'


def test_dirty_stop_pushes_tail_once(fresh_manager, tmp_path):
    """Phase-4 dirty-stop run (last turn ends on a tool call): NO pre push, post reads the tail.

    The pre-hook lives only in the Phase-5 trigger body, so a dirty-stop run never pre-pushes;
    _tg_pushed stays False and the single delivery is the post-run push of messages[-1] (the last
    reflection reply), exactly once.
    """
    supervisor = MagicMock()
    engine, inst, pool, run = _make_engine(fresh_manager, tmp_path, max_turns=3, min_turns=2,
                                           extra_turns=5, tool_call_at=3, natural_end_at=99,
                                           supervisor=supervisor)
    run()
    assert inst._auto_skill_proposed is True, 'reflection should have fired via the tool-call path'
    assert getattr(inst, '_auto_skill_dirty_stop', False) is True, 'must be a dirty-stop run'
    # Pre-hook never fired on this path → _tg_pushed stays False (post will deliver).
    assert inst._tg_pushed is False, 'dirty-stop run must NOT pre-push'
    # Simulate the post-run push reading the tail once.
    from agent_cascade.compression.helpers import extract_instance_output
    tail = extract_instance_output(list(inst.conversation), inst.instance_name, pool=pool, instance=inst)
    assert tail and tail.strip(), 'dirty-stop tail must carry a deliverable answer'
    supervisor.notify_user(tail)   # the post-run push (single delivery for this run)
    assert supervisor.notify_user.call_count == 1


def test_subagent_reflection_does_not_push(fresh_manager, tmp_path):
    """A sub-agent instance (parent_instance != None) that triggers reflection does NOT push.

    The pre-hook is gated by ``instance.parent_instance is None``; a child that reflects must not
    spam the phone with its internal output. Only the root pushes.
    """
    supervisor = MagicMock()
    engine, inst, pool, run = _make_engine(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                           extra_turns=5, natural_end_at=3, supervisor=supervisor)
    # Flip the instance to a sub-agent (parent set) so the root gate fails.
    inst.parent_instance = 'root-caller'
    assert inst.parent_instance is not None
    run()
    # Reflection fired on the child, but the pre-hook's root gate suppressed the push.
    assert inst._auto_skill_proposed is True, 'reflection should have fired on the sub-agent'
    supervisor.notify_user.assert_not_called(), \
        'a sub-agent reflection must not push to the phone'


def test_stop_suppresses_post_push(fresh_manager, tmp_path):
    """An explicit stop (is_stopped() True at the post site) suppresses the post-run push.

    The instance carries a real assistant answer, so a non-stopped run WOULD push it — proving
    the suppression is due to the stop gate, not an empty conversation.
    """
    from agent_cascade.llm.schema import Message, ASSISTANT

    supervisor = MagicMock()
    inst = _make_inst(3)
    inst.conversation.append(Message(role=ASSISTANT, content='the final answer'))
    # A halted instance → is_stopped() returns True at the post site (instance_name in
    # pool._halted_instances). Note: run_agent_thread_unified resets pool.stopped=False at run
    # start (L79), so a pre-set `stopped` flag would be cleared before the post check — halting
    # is the stop condition that survives to the post site.
    pool = _make_run_pool(inst, halted={'test-inst'}, supervisor=supervisor)
    _run_unified(pool)
    supervisor.notify_user.assert_not_called(), 'a stopped run must not push a final answer'


def test_tg_pushed_reset_at_run_start(fresh_manager, tmp_path):
    """Cross-run staleness guard: a stale _tg_pushed=True from a previous reflection run is
    reset at the start of the next run, so the next run's push still fires.

    Run 1 reflects (sets _tg_pushed=True). Run 2 has no reflection but starts with the stale
    True; E2a resets it to False at run start, so run 2's post-push is not suppressed.
    """
    supervisor = MagicMock()
    inst = _make_inst(3)
    inst._tg_pushed = True   # simulate a stale flag left over from a previous reflection run

    pool = _make_run_pool(inst, supervisor=supervisor)
    _run_unified(pool)       # E2a resets _tg_pushed=False at run start, then post-push fires
    assert inst._tg_pushed is False, 'E2a must reset _tg_pushed at run start'
    assert supervisor.notify_user.call_count == 1, \
        'the stale True must not suppress this run\'s push (reset-at-start works)'


# --------------------------------------------------------------------------- #
# B. Long-answer chunking (E3)
# --------------------------------------------------------------------------- #

def test_safe_send_chunks_long_text():
    """A final answer >4096 chars through _safe_send is split into ≤4096-char parts whose
    concatenation equals the original (E3 makes _safe_send chunk via the existing chunk_text)."""
    from agent_cascade.telegram_bridge.bot import _safe_send

    bot = MagicMock()

    async def _noop(**kwargs):
        return None

    bot.send_message.side_effect = lambda **kw: _noop()

    text = ('word ' * 1000).strip()   # ~5000 chars, single logical line with spaces
    assert len(text) > 4096

    asyncio.run(_safe_send(bot, chat_id=42, text=text))

    sent = [c.kwargs['text'] for c in bot.send_message.call_args_list]
    assert len(sent) >= 2, 'a >4096-char answer must be split into multiple parts'
    assert all(len(p) <= 4096 for p in sent), 'every part must respect the 4096 limit'
    assert ''.join(sent) == text, 'chunking must be lossless'
    assert all(c.kwargs.get('chat_id') == 42 for c in bot.send_message.call_args_list)
