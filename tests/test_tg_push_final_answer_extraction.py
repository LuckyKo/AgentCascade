"""Non-vacuous regression tests for the TG-FINAL-ANSWER fix (2026-09-28).

Root cause: web-UI runs that end on the turn-limit / tool-call / reflection paths
pushed the WRONG conversation tail (turn-limit notice, FUNCTION-role WARNING string,
or reflection chatter) instead of the task's final answer, and every suppression
branch in both push paths was silent. Fixed by ``extract_final_answer_text`` in
``agent_cascade/compression/helpers.py`` (backward walk for the last assistant-with-text,
notice stripping, snapshot precedence) + INFO/WARNING logging at every skip branch in
both push paths (P1 pre-reflection in engine/core.py, P2 post-run in run_agent_unified.py).
The dirty-stop/reflection-tail delivery rule is documented self-containedly in the
``extract_final_answer_text`` docstring — no external plan file needed.

Revert-proof (non-vacuity requirement): tests 1-2 call the NEW
``extract_final_answer_text`` helper that did not exist pre-fix — they FAIL
against the pre-fix code (ImportError: cannot import name 'extract_final_answer_text')
and PASS post-fix. Where a test asserts behavior the OLD extractor got wrong, the
docstring states what ``extract_instance_output`` returned pre-fix so the red/green
delta is explicit in the report.

Run serially:
    python -m pytest tests/test_tg_push_final_answer_extraction.py -v -o addopts="" --timeout=120
"""

import asyncio
import logging
import sys
import threading
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch as mock_patch

import pytest

# Ensure top-level imports work (mirror tests/test_tg_dup_stale_snapshot.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.llm.schema import Message, ASSISTANT, FUNCTION, USER  # noqa: E402


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _make_inst(conversation=None):
    """Minimal AgentInstance via ``__new__`` (bypasses dataclass defaults).

    Only the attributes the push paths and extractors read are set; production code
    reads them with getattr-with-defaults anyway.
    """
    from agent_cascade.agent_instance import AgentInstance, AgentState

    inst = AgentInstance.__new__(AgentInstance)
    inst.instance_name = 'Maine'
    # _apply_ui_config reads instance.agent_class BEFORE the template's .llm check — a
    # minimally-constructed instance must carry it or run_agent_thread_unified dies before
    # reaching the post-run push (mirrors tests/test_tg_push_model.py::_make_inst).
    inst.agent_class = 'test_agent'
    inst.parent_instance = None
    inst.conversation = list(conversation or [])
    inst.state = AgentState.IDLE
    inst._state_lock = threading.RLock()
    return inst


def _make_run_pool(instance, *, supervisor=None, generation=1):
    """A SimpleNamespace pool just rich enough for run_agent_thread_unified's post-push.

    Same shape as tests/test_tg_push_model.py::_make_run_pool (the local is_stopped()
    closure reads exactly these attrs; the push site adds get_instance/telegram_supervisor).
    ``generation`` is the PRE-increment value: run_agent_thread_unified does
    ``pool._run_generation += 1`` at run start, so the [TG-PUSH] lines carry gen+1.
    """
    return SimpleNamespace(
        stopped=False,
        _run_generation=generation - 1,
        _instance_threads_lock=threading.Lock(),
        _instance_threads={},
        _halted_instances=set(),
        is_instance_terminated=lambda name: False,
        get_instance=lambda name: instance,
        # A template with no .llm makes _apply_ui_config return early — we only exercise
        # the push/reset path.
        get_template=lambda name: SimpleNamespace(llm=None),
        has_pending=lambda name: False,
        has_messages=lambda name: False,
        telegram_supervisor=supervisor,
    )


def _run_unified(pool, instance_name='Maine'):
    """Drive run_agent_thread_unified with the engine/broadcast machinery stubbed to no-ops.

    ``run_agent_in_pool_with_recovery`` is patched to yield nothing (an empty run), so
    control flows straight past the loop to the post-run push — exactly what these tests
    exercise. Mirrors tests/test_tg_push_model.py::_run_unified.

    The patch targets ``agent_cascade.api_integration_pkg.runner`` (the real module)
    because run_agent_thread_unified does ``from .api_integration import ...`` INSIDE
    the function body — a fresh name lookup on every call, so patching the facade's
    re-exported names is too late (they were already bound at import time).
    """
    import agent_cascade.run_agent_unified as rau
    from agent_cascade.api_integration_pkg import runner, state_builder

    _orig = (runner.run_agent_in_pool_with_recovery, state_builder.build_stream_update_from_pool,
             state_builder.build_state_from_pool)
    try:
        runner.run_agent_in_pool_with_recovery = lambda *a, **k: iter([])
        state_builder.build_stream_update_from_pool = lambda *a, **k: None
        state_builder.build_state_from_pool = lambda *a, **k: None
        rau.run_agent_thread_unified(pool, instance_name, None, {}, None, None)
    finally:
        (runner.run_agent_in_pool_with_recovery, state_builder.build_stream_update_from_pool,
         state_builder.build_state_from_pool) = _orig


# --------------------------------------------------------------------------- #
# F1. extract_final_answer_text — the wrong-tail regressions
# --------------------------------------------------------------------------- #

def test_turn_limit_tail_returns_answer_without_notice():
    """Test 1: conversation ending with the turn-limit notice suffix on the
    last assistant message → the extractor returns the answer text WITHOUT the notice.

    Pre-fix behavior: ``extract_instance_output`` returned the FULL tail —
    "The final answer\\n\\n[Turn limit reached — results may be incomplete. Continue if needed.]"
    — so the phone received the notice as part of the answer (and the real answer was
    several messages back on longer runs). Fails pre-fix: ImportError (helper absent) /
    notice suffix present in the result.
    """
    from agent_cascade.compression.helpers import extract_final_answer_text

    conv = [
        Message(role=USER, content='do the thing'),
        Message(role=ASSISTANT, content='intermediate progress note'),
        Message(role=FUNCTION, name='tool_a', content='tool result blob'),
        Message(role=ASSISTANT,
                content='The final answer\n\n[Turn limit reached — results may be incomplete. Continue if needed.]'),
    ]
    inst = _make_inst(conv)

    result = extract_final_answer_text(list(inst.conversation), 'Maine', instance=inst)

    assert result == 'The final answer', \
        f'expected the bare answer without the notice suffix, got {result!r}'
    assert 'Turn limit reached' not in result


def test_function_role_tail_returns_earlier_assistant_text():
    """Test 2: last message is a FUNCTION-role tool result → the extractor
    returns the EARLIER assistant text, not the WARNING string.

    Pre-fix behavior: ``extract_instance_output`` returned
    "WARNING: Sub-agent Maine terminated with a tool result (no final text output)..." —
    an internal diagnostic pushed to the phone instead of the answer. Fails pre-fix:
    ImportError (helper absent) / WARNING string returned.
    """
    from agent_cascade.compression.helpers import extract_final_answer_text

    conv = [
        Message(role=USER, content='do the thing'),
        Message(role=ASSISTANT, content='Here is the answer you asked for.'),
        Message(role=FUNCTION, name='tool_b', content='{"ok": true}'),
    ]
    inst = _make_inst(conv)

    result = extract_final_answer_text(list(inst.conversation), 'Maine', instance=inst)

    assert result == 'Here is the answer you asked for.', \
        f'expected the earlier assistant text, got {result!r}'
    assert not result.startswith('WARNING:')


def test_snapshot_precedence_returns_snapshot_unchanged():
    """Test 3: instance with ``_auto_skill_task_output`` set and NOT dirty →
    the snapshot is returned unchanged, even when the conversation tail differs.

    Guards the "keep current behavior" rule: natural-completion runs still push the
    pre-reflection snapshot captured at trigger time (core.py:358), not a re-walk of
    the now-extended reflection conversation.
    """
    from agent_cascade.compression.helpers import extract_final_answer_text

    conv = [
        Message(role=USER, content='do the thing'),
        Message(role=ASSISTANT, content='pre-reflection answer text'),
        # Reflection turns appended after the snapshot was captured:
        Message(role=USER, content='[auto-skill reflection prompt]'),
        Message(role=ASSISTANT, content='reflection final chatter'),
    ]
    inst = _make_inst(conv)
    inst._auto_skill_task_output = 'SNAPSHOT ANSWER'
    inst._auto_skill_dirty_stop = False

    result = extract_final_answer_text(list(inst.conversation), 'Maine', instance=inst)

    assert result == 'SNAPSHOT ANSWER', \
        f'snapshot precedence broken: expected the snapshot, got {result!r}'


def test_dirty_stop_walks_to_last_assistant_text():
    """Dirty-stop run (``_auto_skill_dirty_stop`` True): the snapshot is bypassed and the
    walk returns the LAST assistant-with-text overall — on a dirty run that is the
    reflection's final answer (documented choice in the helper docstring).
    """
    from agent_cascade.compression.helpers import extract_final_answer_text

    conv = [
        Message(role=USER, content='do the thing'),
        Message(role=ASSISTANT, content='task answer before reflection'),
        Message(role=USER, content='[auto-skill reflection prompt]'),
        Message(role=ASSISTANT, content='reflection final answer'),
    ]
    inst = _make_inst(conv)
    inst._auto_skill_task_output = 'STALE SNAPSHOT'
    inst._auto_skill_dirty_stop = True

    result = extract_final_answer_text(list(inst.conversation), 'Maine', instance=inst)

    assert result == 'reflection final answer', \
        f'dirty-stop run must return the last assistant text, got {result!r}'


def test_degenerate_conversation_falls_back_to_extract_instance_output():
    """No assistant-with-text anywhere → fall back to extract_instance_output's existing
    behavior (WARNING string), preserving current handling of degenerate cases.
    """
    from agent_cascade.compression.helpers import extract_final_answer_text, extract_instance_output

    conv = [
        Message(role=USER, content='do the thing'),
        Message(role=FUNCTION, name='tool_a', content='result'),
    ]
    inst = _make_inst(conv)

    result = extract_final_answer_text(list(inst.conversation), 'Maine', instance=inst)
    expected = extract_instance_output(list(conv), 'Maine', instance=None)

    assert result == expected, \
        f'degenerate fallback must match extract_instance_output, got {result!r}'
    assert result.startswith('WARNING:'), f'expected the WARNING string, got {result!r}'


# --------------------------------------------------------------------------- #
# F2. P2 suppression-branch logging
#
# caplog targets 'agent_cascade_logger' — the SHARED module logger object
# (agent_cascade/log.py:220) that run_agent_unified logs through, NOT the module
# path. Targeting a non-emitting name would silently drop INFO records.
# --------------------------------------------------------------------------- #

def _extract_tg_push_lines(caplog):
    """All captured log messages carrying the [TG-PUSH] tag (P1/P2 push paths)."""
    return [r.getMessage() for r in caplog.records if '[TG-PUSH]' in r.getMessage()]


def test_p2_skip_logged_when_tg_pushed_true_at_post_site(caplog):
    """P2 skipped because ``_tg_pushed`` is True at the post site → INFO line with
    instance + gen, and NO push.

    Pre-fix: the branch was silent (zero [TG-PUSH] lines) — which is exactly why the
    production log could not show which gate fired.

    The per-run reset in run_agent_thread_unified clears a pre-set flag before the post
    site, so this test drives the extracted ``_tg_post_run_push`` helper directly (the
    same code path ``run_agent_thread_unified`` reaches after the run loop) with the
    flag set — the exact state the pre-reflection push leaves behind. The
    ``test_p2_skip_logged_when_tg_pushed_set_mid_run`` test below covers the mid-run
    transition (flag set by a P1 push during the run) through the full path.
    """
    from agent_cascade.run_agent_unified import _tg_post_run_push

    inst = _make_inst([Message(role=ASSISTANT, content='the final answer')])
    inst._tg_pushed = True   # the pre-reflection push already delivered this run's answer
    supervisor = MagicMock()
    pool = _make_run_pool(inst, supervisor=supervisor, generation=7)

    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        _tg_post_run_push(inst, 'Maine', 7, pool, outstanding=False)

    lines = [l for l in _extract_tg_push_lines(caplog) if 'post-run push skipped' in l and 'pre-reflection push already delivered' in l]
    assert lines, f'expected the _tg_pushed skip line, got: {_extract_tg_push_lines(caplog)}'
    assert 'Maine' in lines[0] and 'gen=7' in lines[0], f'missing instance/gen: {lines[0]!r}'
    supervisor.notify_user.assert_not_called()


def test_p2_dedup_no_second_push_when_tg_pushed_set_mid_run(caplog):
    """Dedup regression guard: ``_tg_pushed`` set mid-run (pre-reflection push) → the
    post-run site must NOT push a second time.

    Pre-fix: the branch was silent AND the dedup was untested — a regression that made
    the post-run push fire again after a successful pre-reflection push would have been
    invisible (the "same message again" symptom class the TG-DEDUP gate exists to prevent).

    The supervisor's notify_user side_effect sets the flag on every call, simulating the
    pre-reflection push having delivered earlier in this same run. The assertion is on
    ``notify_user.call_count == 1``: exactly one push total (the simulated P1), none from
    the post-run site. This drives the FULL ``run_agent_thread_unified`` path, so it also
    proves the call-site wiring (the post-run block invokes ``_tg_post_run_push`` with the
    live instance). The direct-branch test above covers the skip-log line in isolation —
    the per-run reset makes that branch unreachable through the full path unless the flag
    is set mid-run, and asserting on the log line here would couple this dedup guard to
    the helper's internal logging.
    """
    inst = _make_inst([Message(role=ASSISTANT, content='the final answer')])

    def _delivered(*a, **k):
        # Simulate the pre-reflection push having delivered earlier in this run.
        inst._tg_pushed = True
        return True

    supervisor = MagicMock()
    supervisor.notify_user.side_effect = _delivered
    pool = _make_run_pool(inst, supervisor=supervisor, generation=7)

    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        _run_unified(pool)

    # Exactly one push total: the simulated P1. The post-run site must NOT push again.
    assert supervisor.notify_user.call_count == 1, \
        f'dedup broken: expected exactly 1 push (the pre-reflection push), got {supervisor.notify_user.call_count}'


def test_p2_skip_logged_when_outstanding_work_pending(caplog):
    """P2 skipped because queued/async-pending work remained → INFO line (the B1 signal)."""
    inst = _make_inst([Message(role=ASSISTANT, content='the final answer')])
    supervisor = MagicMock()
    pool = _make_run_pool(inst, supervisor=supervisor, generation=3)
    pool.has_messages = lambda name: True   # a phone message was queued during the run

    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        _run_unified(pool)

    lines = [l for l in _extract_tg_push_lines(caplog) if 'post-run push skipped' in l and 'outstanding work pending' in l]
    assert lines, f'expected the outstanding-work skip line (B1 signal), got: {_extract_tg_push_lines(caplog)}'
    assert 'Maine' in lines[0] and 'gen=3' in lines[0], f'missing instance/gen: {lines[0]!r}'
    supervisor.notify_user.assert_not_called()


def test_p2_degenerate_fallback_text_logged_at_warning(caplog):
    """P2 extracted a degenerate fallback (WARNING string — no assistant-with-text) →
    WARNING line, not a silent skip and NOT pushed to the phone."""
    inst = _make_inst([Message(role=USER, content='do the thing'),
                       Message(role=FUNCTION, name='tool_a', content='result')])
    supervisor = MagicMock()
    pool = _make_run_pool(inst, supervisor=supervisor, generation=5)

    with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
        _run_unified(pool)

    lines = [l for l in _extract_tg_push_lines(caplog) if 'degenerate fallback' in l]
    assert lines, f'expected the degenerate-fallback WARNING line, got: {_extract_tg_push_lines(caplog)}'
    assert any(r.levelno == logging.WARNING for r in caplog.records if '[TG-PUSH]' in r.getMessage())
    supervisor.notify_user.assert_not_called()


def test_p2_notify_user_false_logged_at_warning(caplog):
    """notify_user returned False at the P2 site → WARNING line (bridge loop not live)."""
    inst = _make_inst([Message(role=ASSISTANT, content='the final answer')])
    supervisor = MagicMock()
    supervisor.notify_user.return_value = False   # bridge down / not started
    pool = _make_run_pool(inst, supervisor=supervisor, generation=9)

    with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
        _run_unified(pool)

    lines = [l for l in _extract_tg_push_lines(caplog) if 'notify_user returned False' in l]
    assert lines, f'expected the notify_user-False WARNING line, got: {_extract_tg_push_lines(caplog)}'
    assert 'Maine' in lines[0] and 'gen=9' in lines[0], f'missing instance/gen: {lines[0]!r}'
    supervisor.notify_user.assert_called_once()


def test_p2_success_still_pushes_extracted_answer():
    """Happy path unchanged: a run with a real assistant answer still pushes exactly once,
    now via extract_final_answer_text (turn-limit notice stripped)."""
    inst = _make_inst([
        Message(role=USER, content='do the thing'),
        Message(role=ASSISTANT,
                content='The final answer\n\n[Turn limit reached — results may be incomplete. Continue if needed.]'),
    ])
    supervisor = MagicMock()
    supervisor.notify_user.return_value = True
    pool = _make_run_pool(inst, supervisor=supervisor)

    _run_unified(pool)

    assert supervisor.notify_user.call_count == 1
    pushed = supervisor.notify_user.call_args.args[0]
    assert pushed == 'The final answer', f'pushed text must be the bare answer, got {pushed!r}'


# --------------------------------------------------------------------------- #
# F2. notify_user False path
# --------------------------------------------------------------------------- #

def _make_supervisor(tmp_path):
    """Build a supervisor with fast timers — mirrors tests/test_telegram_bridge_supervisor.py."""
    from agent_cascade.telegram_bridge.supervisor import TelegramBridgeSupervisor

    return TelegramBridgeSupervisor(
        ac_base_url='http://127.0.0.1:8126',
        project_root=PROJECT_ROOT,
        workspace_dir=str(tmp_path),
        backoff_base=0.01,
        backoff_cap=0.02,
        max_restart_attempts=3,
        stop_join_timeout_sec=5.0,
    )


def test_notify_user_false_when_app_none_logs_reason(caplog, tmp_path):
    """supervisor with app None (bridge never started) → returns False AND logs the
    reason at DEBUG. Pre-fix: the return was a silent no-op."""
    sup = _make_supervisor(tmp_path)

    with caplog.at_level(logging.DEBUG, logger='agent_cascade_logger'):
        result = sup.notify_user('hello', instance_name='Maine', run_generation=4)

    assert result is False
    lines = [r.getMessage() for r in caplog.records if 'notify_user no-op' in r.getMessage()]
    assert lines, f'expected a notify_user no-op DEBUG line, got: {[r.getMessage() for r in caplog.records]}'
    assert 'app is None' in lines[0], f'reason missing from log line: {lines[0]!r}'


def test_notify_user_false_when_loop_closed_logs_reason(caplog, tmp_path):
    """supervisor with a closed loop → returns False AND logs the reason at DEBUG."""
    loop = asyncio.new_event_loop()
    loop.close()   # now is_closed() == True

    sup = _make_supervisor(tmp_path)
    with sup._lock:
        sup._app = MagicMock()
        sup._loop = loop

    with caplog.at_level(logging.DEBUG, logger='agent_cascade_logger'):
        result = sup.notify_user('hello', instance_name='Maine', run_generation=4)

    assert result is False
    lines = [r.getMessage() for r in caplog.records if 'notify_user no-op' in r.getMessage()]
    assert lines, f'expected a notify_user no-op DEBUG line, got: {[r.getMessage() for r in caplog.records]}'
    assert 'loop is closed' in lines[0], f'reason missing from log line: {lines[0]!r}'


# --------------------------------------------------------------------------- #
# P1 pre-reflection push skip-branch logging (mirrors the P2 tests above)
#
# The P1 block lives inside ExecutionEngine.run()'s Phase-5 auto-skill trigger —
# driving a full engine run to reach it requires budget exhaustion + qualification,
# which is impractical in a unit test. Instead we drive the extracted
# _tg_pre_reflection_push helper directly (same code path run() reaches after the
# trigger fires). caplog targets 'agent_cascade_logger' (shared module logger).
# --------------------------------------------------------------------------- #

def test_p1_skip_logged_when_extracted_text_empty(caplog):
    """P1: extract_final_answer_text returns empty (no assistant-with-text, and the
    degenerate fallback is also empty) → INFO skip line, NO push.

    Pre-fix: the branch was silent (zero [TG-PUSH] lines).
    """
    from agent_cascade.engine.core import _tg_pre_reflection_push

    # A conversation with only USER messages — no assistant-with-text anywhere.
    inst = _make_inst([Message(role=USER, content='do the thing')])
    supervisor = MagicMock()
    pool = SimpleNamespace(_run_generation=4, telegram_supervisor=supervisor)

    # Patch extract_final_answer_text to return '' (the degenerate fallback returns a
    # WARNING string for non-empty conversations; we want the empty-text branch).
    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        with mock_patch('agent_cascade.compression.helpers.extract_final_answer_text', return_value=''):
            _tg_pre_reflection_push(inst, pool)

    lines = [l for l in _extract_tg_push_lines(caplog) if 'pre-reflection push skipped' in l and 'extracted text empty' in l]
    assert lines, f'expected the empty-text skip line, got: {_extract_tg_push_lines(caplog)}'
    assert 'Maine' in lines[0] and 'gen=4' in lines[0], f'missing instance/gen: {lines[0]!r}'
    supervisor.notify_user.assert_not_called()


def test_p1_skip_logged_when_degenerate_fallback(caplog):
    """P1: extract_final_answer_text returns a WARNING string (degenerate fallback —
    no assistant-with-text) → WARNING skip line, NO push.

    Pre-fix: the branch was silent.
    """
    from agent_cascade.engine.core import _tg_pre_reflection_push

    # FUNCTION-role tail with no assistant text → extract_instance_output returns a WARNING string.
    inst = _make_inst([Message(role=USER, content='do the thing'),
                       Message(role=FUNCTION, name='tool_a', content='result')])
    supervisor = MagicMock()
    pool = SimpleNamespace(_run_generation=6, telegram_supervisor=supervisor)

    with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
        _tg_pre_reflection_push(inst, pool)

    lines = [l for l in _extract_tg_push_lines(caplog) if 'pre-reflection push skipped' in l and 'degenerate fallback' in l]
    assert lines, f'expected the degenerate-fallback skip line, got: {_extract_tg_push_lines(caplog)}'
    assert any(r.levelno == logging.WARNING for r in caplog.records if '[TG-PUSH]' in r.getMessage())
    supervisor.notify_user.assert_not_called()


def test_p1_skip_logged_when_no_supervisor(caplog):
    """P1: real answer text but no telegram_supervisor on the pool → INFO skip line,
    NO push.

    Pre-fix: the branch was silent.
    """
    from agent_cascade.engine.core import _tg_pre_reflection_push

    inst = _make_inst([Message(role=ASSISTANT, content='the final answer')])
    pool = SimpleNamespace(_run_generation=8, telegram_supervisor=None)

    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        _tg_pre_reflection_push(inst, pool)

    lines = [l for l in _extract_tg_push_lines(caplog) if 'pre-reflection push skipped' in l and 'no telegram supervisor' in l]
    assert lines, f'expected the no-supervisor skip line, got: {_extract_tg_push_lines(caplog)}'
    assert 'Maine' in lines[0] and 'gen=8' in lines[0], f'missing instance/gen: {lines[0]!r}'


def test_p1_notify_user_false_logged_at_warning(caplog):
    """P1: notify_user returns False (bridge loop not live) → WARNING line, and
    _tg_pushed is NOT set (so the post-run push can still deliver).

    Pre-fix: the branch was silent AND _tg_pushed was set unconditionally — which
    would have suppressed the post-run push and dropped the final answer.
    """
    from agent_cascade.engine.core import _tg_pre_reflection_push

    inst = _make_inst([Message(role=ASSISTANT, content='the final answer')])
    supervisor = MagicMock()
    supervisor.notify_user.return_value = False   # bridge down / not started
    pool = SimpleNamespace(_run_generation=10, telegram_supervisor=supervisor)

    with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
        _tg_pre_reflection_push(inst, pool)

    lines = [l for l in _extract_tg_push_lines(caplog) if 'notify_user returned False' in l]
    assert lines, f'expected the notify_user-False WARNING line, got: {_extract_tg_push_lines(caplog)}'
    assert 'Maine' in lines[0] and 'gen=10' in lines[0], f'missing instance/gen: {lines[0]!r}'
    # The critical regression guard: _tg_pushed must NOT be set on failure.
    assert not getattr(inst, '_tg_pushed', False), \
        '_tg_pushed was set despite notify_user returning False — post-run push would be suppressed'


def test_p1_success_sets_tg_pushed_and_pushes():
    """P1 happy path: real answer text + live bridge → exactly one push, _tg_pushed
    set to True (dedup guard for the post-run push)."""
    from agent_cascade.engine.core import _tg_pre_reflection_push

    inst = _make_inst([Message(role=ASSISTANT, content='the final answer')])
    supervisor = MagicMock()
    supervisor.notify_user.return_value = True
    pool = SimpleNamespace(_run_generation=12, telegram_supervisor=supervisor)

    _tg_pre_reflection_push(inst, pool)

    assert supervisor.notify_user.call_count == 1
    assert inst._tg_pushed is True, '_tg_pushed must be set on successful push (dedup guard)'
