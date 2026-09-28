"""TG-STREAM: stream-time condition-based Telegram push tests.

Plan: ``plans/tg-stream-time-push_PLAN.md`` §5. Replaces the reverted end-of-run
history-extraction model with pushes that fire at the moment their condition is met in
the engine loop, operating only on committed turn output (no conversation lookback):

  * first-text push — ``engine/core.py`` _process_response commit point
    (``_tg_stream_push``), once per run, root-gated, has-text check;
  * final push — Phase 5 genuine completion (``_tg_push_final``), phase 'pre' before an
    auto-skill reflection and 'post' on the reflection's own final turn.

Tests drive the REAL ``ExecutionEngine.run()`` with a stubbed LLM (same harness shape as
tests/test_tg_dup_delivery.py) plus a fake supervisor recording ``notify_user`` calls.
caplog targets ``agent_cascade_logger`` (the shared module logger).

Non-vacuity: tests 1 and 3 FAIL against the pre-fix tree (no stream-time hooks exist;
the old model only pushed at end-of-run via run_agent_unified.py P2, which these
engine-level runs never reach) — red/green evidence reported in the delivery.
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def fresh_manager(tmp_path):
    """Fresh SkillManager isolated to a per-test tmp dir (mirrors test_tg_push_model)."""
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

    ``AgentInstance.__new__`` bypasses dataclass defaults, so every attribute run()'s hot
    path reads must be set explicitly. TG-STREAM flags default to their per-run reset
    values (what _reset_run_scoped_tg_state / lifecycle reuse establish at run start).
    """
    from agent_cascade.agent_instance import AgentInstance, AgentState
    from agent_cascade.llm.schema import Message, USER

    # Seed the conversation with a SYSTEM head so the real _setup_turn's P7 injection
    # branch (insert_message_at_head) is skipped — it would otherwise call
    # instance.insert_message_at_head, which our minimal harness does not provide.
    inst = AgentInstance.__new__(AgentInstance)
    inst.instance_name = 'w'
    inst.agent_class = 'test_agent'
    inst.parent_instance = parent_instance          # None == root (the push root gate)
    inst.conversation = [Message(role='system', content='sys'), Message(role=USER, content='task')]
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
    inst._system_prompt_frozen = False
    inst._continue_saved_msg = None
    inst._auto_skill_proposed = False
    inst._auto_skill_dirty_stop = False
    # TG-STREAM F4: per-run push flags, reset at every run start.
    inst._tg_first_pushed = False
    inst._tg_final_pushed_phase = None
    inst._streaming_responses = []
    return inst


def _make_engine(fresh_manager, tmp_path, *, max_turns=3, min_turns=99, extra_turns=5,
                 natural_end_at=None, tool_call_ats=(), supervisor=None,
                 stop_after_llm_call=None):
    """Build a REAL ExecutionEngine + instance that drives engine.run() to completion.

    Scripted LLM: on call ``n`` yields an assistant message with text ``f'reply {n}'``;
    when ``n in tool_call_ats`` it ALSO carries a function_call (valid JSON args, so the
    incomplete-state detector does not flag it). The FUNCTION result appended by the real
    ``_execute_detected_tools`` has USER role and never counts as push text.

    * ``natural_end_at`` = the _post_turn_checks call number that reports a genuine end
      (returns False → Phase 5). NOTE: tool-call turns `continue` in Phase 4 and NEVER
      reach _post_turn_checks, so this counts only non-tool-call (Phase-5-reached) turns,
      NOT total LLM calls. Defaults to ``max_turns`` (correct when there are no tool calls).
    * ``min_turns`` defaults high so the auto-skill gates stay closed unless a test opts
      in (test 3) — keeps the other runs reflection-free.
    * ``stop_after_llm_call``: terminal stop becomes true after this many LLM calls
      (drives the D3 stop-mid-run path).
    """
    from agent_cascade.execution_engine import ExecutionEngine
    from agent_cascade.llm.schema import Message, ASSISTANT, USER

    template = MagicMock()
    template.function_map = {'tool_a': None}

    pool = MagicMock()
    # A bare MagicMock auto-generates a truthy `telemetry` whose record_user_turn(...)
    # call can raise inside the real _consume_turn — swallowed by run()'s broad except,
    # which kills the loop mid-run. Pin it to None (house _FakePool does the same).
    pool.telemetry = None
    pool.settings.auto_skill_enabled = True
    pool.settings.auto_skill_min_turns = min_turns
    pool.settings.default_load_skill_mode = 'AUTO'
    pool.settings.tail_sync_check_enabled = False
    # _is_terminal_stop reads these off the pool: stopped=False and a stable generation
    # keep the run alive (the stop-mid-run test drives its own terminal-stop mock).
    pool.stopped = False
    pool._run_generation = 0
    pool.get_template.return_value = template
    pool.is_instance_terminated.return_value = False
    pool.has_pending.return_value = False
    pool.has_messages.return_value = False
    pool.drain_queue.return_value = []
    # A bare MagicMock auto-attribute would be a truthy mock and the push helpers would
    # call notify_user on it. Set explicitly to control the push (None == bridge absent).
    pool.telegram_supervisor = supervisor

    from agent_cascade.logger import AgentInstanceLogger
    log_inst = AgentInstanceLogger('test_agent', 'w', str(tmp_path), log_path=str(tmp_path / 'w.jsonl'))
    pool.get_logger.return_value = log_inst

    # Populate the registry via REAL discovery so auto_skill_qualifies() can find
    # 'skill-creator' when a test opts into reflection.
    fresh_manager._cache_ttl = 0.0
    fresh_manager.discover([Path('agents/global/skills')])
    pool.skill_manager = fresh_manager

    engine = ExecutionEngine(pool)
    inst = _make_inst(max_turns)
    # run() sets self._my_generation from pool._run_generation at entry — pin it so the
    # real _is_terminal_stop's generation-mismatch check stays False (the stop test
    # overrides _is_terminal_stop with its own mock anyway).
    engine._my_generation = 0

    def fake_setup_turn(instance):
        return list(instance.conversation), [Message(role=USER, content='task')], []

    engine._setup_turn = MagicMock(side_effect=fake_setup_turn)
    engine._pre_llm_checks = MagicMock(return_value=False)
    engine._check_stop_conditions = MagicMock(return_value=False)
    engine._is_suspended_by_compression = MagicMock(return_value=False)
    engine._acquire_slot_with_logging = MagicMock(return_value=None)
    engine._check_stream_termination = MagicMock(return_value=None)
    # Phase 5's genuine-completion gate: the real _detect_pure_thinking_turn does
    # msg.get('role') on Message objects (dict-only access) and would raise AttributeError
    # — swallowed by run()'s broad except, which exits before any final push. Our scripted
    # turns never carry thinking content, so stub it False.
    engine._detect_pure_thinking_turn = MagicMock(return_value=False)

    _llm_call_count = {'n': 0}

    def fake_llm(inst, msgs):
        _llm_call_count['n'] += 1
        n = _llm_call_count['n']
        yield None
        if n in tool_call_ats:
            # text + tool call on the same turn (turn-1 shape of test 1) — the has-text
            # check must pick the text, not skip the whole turn.
            yield Message(role=ASSISTANT, content=f'reply {n}',
                          function_call={'name': 'tool_a', 'arguments': '{}'})
        else:
            yield Message(role=ASSISTANT, content=f'reply {n}')

    engine._call_llm_with_injection = MagicMock(side_effect=lambda inst, msgs: fake_llm(inst, msgs))

    if tool_call_ats:
        _tool_exec_calls = {'n': 0}

        def _exec_tool_driver(*a, **k):
            _tool_exec_calls['n'] += 1
            return _tool_exec_calls['n'] in tool_call_ats

        engine._execute_detected_tools = MagicMock(side_effect=_exec_tool_driver)
    else:
        engine._execute_detected_tools = MagicMock(return_value=False)

    # natural_end_at may be an int (single natural-end PTC call number) or a set/frozenset
    # of call numbers (e.g. test 3 needs False at BOTH the pre-extension completion and
    # the reflection's own final turn). Defaults to max_turns (correct when no tool calls).
    _nend = max_turns if natural_end_at is None else natural_end_at
    _nend_set = {_nend} if isinstance(_nend, int) else set(_nend)
    _ptc_calls = {'n': 0}

    def _post_turn_checks_driver(*a, **k):
        _ptc_calls['n'] += 1
        # False on the natural-end check(s); True everywhere else.
        return _ptc_calls['n'] not in _nend_set

    engine._post_turn_checks = MagicMock(side_effect=_post_turn_checks_driver)

    if stop_after_llm_call is not None:
        def _terminal_stop_driver(*a, **k):
            return _llm_call_count['n'] > stop_after_llm_call

        engine._is_terminal_stop = MagicMock(side_effect=_terminal_stop_driver)
    else:
        engine._is_terminal_stop = MagicMock(return_value=False)

    def run():
        with patch('agent_cascade.engine.core.AUTO_SKILL_MIN_TURNS', min_turns), \
                patch('agent_cascade.engine.core.AUTO_SKILL_EXTRA_TURNS', extra_turns), \
                patch('agent_cascade.skills.manager.AUTO_SKILL_MIN_TURNS', min_turns):
            gen = engine.run(inst)
            for _ in gen:
                pass
            return gen

    return engine, inst, pool, run


def _pushed_texts(supervisor):
    """All texts delivered via notify_user, in delivery order.

    NOTE: this returns ALL calls (including failed ones). For tests where the first
    call fails (notify_user → False), filter manually or use a wrapper that records
    only successful deliveries. Most tests use return_value=True so all calls succeed.
    """
    return [c.args[0] for c in supervisor.notify_user.call_args_list]


# --------------------------------------------------------------------------- #
# 1. First + final, no reflection
# --------------------------------------------------------------------------- #

def test_first_and_final_no_reflection(fresh_manager, tmp_path):
    """2-turn run (turn 1 text+tool, turn 2 final text) → exactly 2 pushes, in order:
    first = turn-1 text, final[pre] = turn-2 text. No reflection (min_turns high)."""
    supervisor = MagicMock()
    supervisor.notify_user.return_value = True
    engine, inst, pool, run = _make_engine(
        fresh_manager, tmp_path, max_turns=2, natural_end_at=1,
        tool_call_ats={1}, supervisor=supervisor)

    run()

    texts = _pushed_texts(supervisor)
    assert texts == ['reply 1', 'reply 2'], \
        f'expected [first=turn-1 text, final[pre]=turn-2 text] in order, got {texts!r}'
    assert inst._tg_first_pushed is True
    assert inst._tg_final_pushed_phase == 'pre'
    # Both pushes carry the run-generation correlation keyword (F3 observability).
    for c in supervisor.notify_user.call_args_list:
        assert c.kwargs.get('instance_name') == 'w'


# --------------------------------------------------------------------------- #
# 2. Tool-call-only first turn skipped
# --------------------------------------------------------------------------- #

def test_tool_call_only_first_turn_skipped(fresh_manager, tmp_path):
    """Turn 1 is tool-only (empty content) → the first push must be turn-2's text, not
    the empty one. The has-text check is what makes this true."""
    supervisor = MagicMock()
    supervisor.notify_user.return_value = True
    engine, inst, pool, run = _make_engine(
        fresh_manager, tmp_path, max_turns=3, natural_end_at=2,
        tool_call_ats={1}, supervisor=supervisor)

    # Make turn 1 truly tool-only (no text alongside the call).
    from agent_cascade.llm.schema import ASSISTANT, Message

    orig_fake_llm = engine._call_llm_with_injection.side_effect

    def fake_llm_tool_only(inst, msgs):
        _n = {'v': 0}

        def gen():
            for item in orig_fake_llm(inst, msgs):
                if item is not None and getattr(item, 'role', '') == ASSISTANT \
                        and getattr(item, 'function_call', None) is not None:
                    _n['v'] += 1
                    if _n['v'] == 1:
                        yield Message(role=ASSISTANT, content='',
                                      function_call={'name': 'tool_a', 'arguments': '{}'})
                        return
                yield item
        return gen()

    engine._call_llm_with_injection = MagicMock(side_effect=fake_llm_tool_only)

    run()

    texts = _pushed_texts(supervisor)
    assert texts[0] == 'reply 2', \
        f'first push must be turn-2 text (turn 1 was tool-only/empty), got {texts!r}'
    assert texts[-1] == 'reply 3', f'final[pre] must be the last turn, got {texts!r}'
    assert len(texts) == 2, f'expected exactly 2 pushes (first + final), got {len(texts)}: {texts!r}'


# --------------------------------------------------------------------------- #
# 3. Reflection run → 3 pushes
# --------------------------------------------------------------------------- #

def test_reflection_run_three_pushes(fresh_manager, tmp_path):
    """Natural completion at turn N + auto-skill extension + reflection final turn →
    exactly 3 pushes: first (turn-1 text), final[pre] (pre-extension answer), final[post]
    (reflection answer). Marker ends at 'post'.

    The trigger itself is stubbed to fire on the natural-completion turn (the real
    _try_auto_skill_extension would run its full skill-registry qualification — the test
    exercises the push/phase machinery, not the auto-skill gates)."""
    supervisor = MagicMock()
    supervisor.notify_user.return_value = True
    # natural_end_at={1, 2}: PTC #1 (turn 2) = pre-extension completion → final[pre] +
    # extension fires. PTC #2 (turn 3, first reflection turn) = reflection's genuine
    # completion → final[post]. (extra_turns=5 gives the reflection budget; the driver
    # ends it after one reflection turn.)
    engine, inst, pool, run = _make_engine(
        fresh_manager, tmp_path, max_turns=3, min_turns=1, extra_turns=5,
        natural_end_at={1, 2}, tool_call_ats={1}, supervisor=supervisor)

    # Fire the extension exactly once — on Phase 5's genuine-completion turn (turn 2).
    _ext_calls = {'n': 0}

    def _try_ext_driver(*a, **k):
        _ext_calls['n'] += 1
        if _ext_calls['n'] == 1:
            inst._auto_skill_proposed = True   # the one-shot marker the phase keying reads
            return True
        return False

    engine._try_auto_skill_extension = MagicMock(side_effect=_try_ext_driver)

    run()

    assert inst._auto_skill_proposed is True, 'reflection should have fired'
    texts = _pushed_texts(supervisor)
    # turn 1: text+tool → first push. turn 2: final text (natural end) → final[pre].
    # Reflection turns run on the extended budget; their final text → final[post].
    assert len(texts) == 3, f'expected exactly 3 pushes (first, final[pre], final[post]), got {len(texts)}: {texts!r}'
    assert texts[0] == 'reply 1', f'first push must be turn-1 text, got {texts!r}'
    assert texts[1] == 'reply 2', f'final[pre] must be the pre-extension answer (turn 2), got {texts!r}'
    assert texts[2].startswith('reply ') and texts[2] != 'reply 2', \
        f'final[post] must be a reflection answer (a later turn), got {texts!r}'
    assert inst._tg_final_pushed_phase == 'post', \
        f'marker must end at post after the reflection final push, got {inst._tg_final_pushed_phase!r}'


# --------------------------------------------------------------------------- #
# 4. notify_user False → retry on next text turn
# --------------------------------------------------------------------------- #

def test_notify_false_retries_on_next_text_turn(fresh_manager, tmp_path):
    """Bridge down (notify_user False) on turn 1 must NOT set the first-push flag — the
    next text turn retries and succeeds; no crash either way."""
    supervisor = MagicMock()
    # First notify_user call returns False (bridge down); all subsequent calls return True.
    # Track successful deliveries separately — call_args_list includes failed calls too.
    _notify_calls = {'n': 0}
    _successful_texts = []

    def _notify_driver(text, **kw):
        _notify_calls['n'] += 1
        ok = _notify_calls['n'] > 1  # False on call #1, True thereafter
        if ok:
            _successful_texts.append(text)
        return ok

    supervisor.notify_user.side_effect = _notify_driver
    engine, inst, pool, run = _make_engine(
        fresh_manager, tmp_path, max_turns=3, natural_end_at=2,
        tool_call_ats={1}, supervisor=supervisor)

    run()

    # Turn 1 (text+tool): notify_user → False → flag stays unset. Turn 2: the retry of
    # the first push succeeds (True), delivering turn-2's text ('reply 2') — NOT a replay
    # of turn-1's text, because _tg_stream_push always pushes the CURRENT turn_output.
    # Turn 3: final[pre] delivers 'reply 3'.
    assert _successful_texts == ['reply 2', 'reply 3'], \
        f'first push must be RETRIED on turn 2 (delivering current text), then final[pre]; got {_successful_texts!r}'
    assert inst._tg_first_pushed is True, 'flag must be set once the retry succeeds'
    assert inst._tg_final_pushed_phase == 'pre'


# --------------------------------------------------------------------------- #
# 5. Sub-agent never pushes
# --------------------------------------------------------------------------- #

def test_subagent_never_pushes(fresh_manager, tmp_path):
    """An instance with parent_instance set must produce ZERO notify_user calls, even on
    a run that has text output and reaches natural completion."""
    supervisor = MagicMock()
    supervisor.notify_user.return_value = True
    engine, inst, pool, run = _make_engine(
        fresh_manager, tmp_path, max_turns=2, natural_end_at=1,
        tool_call_ats={1}, supervisor=supervisor)
    inst.parent_instance = 'root-caller'

    run()

    supervisor.notify_user.assert_not_called(), \
        'a sub-agent must never push to the phone (root gate)'


# --------------------------------------------------------------------------- #
# 6. Reset coverage (both entry paths clear both markers)
# --------------------------------------------------------------------------- #

def test_reset_paths_clear_both_markers():
    """_reset_run_scoped_tg_state (run_agent_unified) AND the lifecycle_manager reuse
    path must clear _tg_first_pushed and _tg_final_pushed_phase — a stale 'pushed' state
    from run N must not suppress run N+1's pushes."""
    from agent_cascade.run_agent_unified import _reset_run_scoped_tg_state

    # Path 1: main-agent reset (direct function call).
    inst = _make_inst(3)
    inst._tg_first_pushed = True
    inst._tg_final_pushed_phase = 'post'
    _reset_run_scoped_tg_state(inst)
    assert inst._tg_first_pushed is False, '_reset_run_scoped_tg_state must clear the first-push flag'
    assert inst._tg_final_pushed_phase is None, '_reset_run_scoped_tg_state must clear the final-phase marker'

    # Path 2: sub-agent reuse reset in lifecycle_manager — drive the REAL
    # find_or_create_instance reuse path (a minimal pool + a reused IDLE instance) and
    # assert the flags are cleared on the returned instance.
    import threading as _threading
    from unittest.mock import MagicMock as _MM

    from agent_cascade.agent_instance import AgentState as _AgentState
    from agent_cascade.lifecycle_manager import AgentLifecycleManager

    reused = _make_inst(3)
    reused.state = _AgentState.IDLE
    reused.parent_instance = 'old-caller'
    reused._state_lock = _threading.RLock()
    reused._child_instances = set()
    reused.last_activity = 0.0
    reused._nest_depth = 0
    # Stale push state from the previous run — the reuse path must clear it.
    reused._tg_first_pushed = True
    reused._tg_final_pushed_phase = 'post'

    pool = _MM()
    pool.instances = {'w': reused}
    pool._resolve_instance_name.side_effect = lambda n: n
    pool._children_lock = _threading.Lock()
    # caller is None → the child-relationship bookkeeping is skipped entirely.
    manager = AgentLifecycleManager.__new__(AgentLifecycleManager)
    manager.pool = pool

    inst, is_reuse, _loaded = manager.find_or_create_instance(
        'test_agent', 'w', caller=None, nest_depth=0)

    assert is_reuse and inst is reused, 'the harness must exercise the REUSE path'
    assert inst._tg_first_pushed is False, \
        'lifecycle reuse path must reset _tg_first_pushed (F4 second reset site)'
    assert inst._tg_final_pushed_phase is None, \
        'lifecycle reuse path must reset _tg_final_pushed_phase (F4 second reset site)'


# --------------------------------------------------------------------------- #
# 7. Stop mid-run → no final push, INFO logged (D3)
# --------------------------------------------------------------------------- #

def test_stop_mid_run_no_final_push_info_logged(fresh_manager, tmp_path, caplog):
    """Terminal stop after the first push: Phase 5 never runs, so no final push — and the
    D3 observability line is logged (first sent, final not delivered)."""
    supervisor = MagicMock()
    supervisor.notify_user.return_value = True
    engine, inst, pool, run = _make_engine(
        fresh_manager, tmp_path, max_turns=5, natural_end_at=99,
        tool_call_ats={1}, stop_after_llm_call=2, supervisor=supervisor)

    with caplog.at_level('INFO', logger='agent_cascade_logger'):
        run()

    texts = _pushed_texts(supervisor)
    assert texts == ['reply 1'], \
        f'stop after turn 2 must leave only the first push (no final), got {texts!r}'
    assert inst._tg_final_pushed_phase is None, 'no final push may have fired on a stopped run'
    d3_lines = [r.getMessage() for r in caplog.records
                if 'stopped before final push' in r.getMessage()]
    assert d3_lines, f'D3 INFO line missing from logs: {[r.getMessage() for r in caplog.records]}'
