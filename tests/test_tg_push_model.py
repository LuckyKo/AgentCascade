"""Push-model tests for the Telegram bridge final-answer delivery (todo.md:125).

SUPERSEDED by the TG-STREAM stream-time push model — see
``plans/tg-stream-time-push_PLAN.md`` and ``tests/test_tg_stream_push.py``. The old
push model ("pre-reflection push in engine/core.py Phase 5 + post-run extraction push
in run_agent_unified, deduped by the one-shot ``instance._tg_pushed`` bool") is GONE:
the final answer is now pushed at stream time (first text output + pre/post-reflection
final answers) and the end-of-run history-extraction block was deleted (plan F5).

What survives in this file, adapted to the new model:
  * the engine-driven sub-agent gate (a child that reflects never pushes);
  * the long-answer chunking guard for ``_safe_send``.

The post-run-push tests (queue dedup, UI-originated push, bridge-disabled no-crash,
stop suppression, reset-at-start) and the pre/post dedup + dirty-stop tests asserted
behavior that no longer exists; they are removed rather than kept as dead code.
"""

import asyncio
import threading
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
    # TG-STREAM: the old _tg_pushed bool was replaced by phase-keyed
    # stream-time push flags; __new__ bypasses the dataclass defaults, so set them here.
    inst._tg_first_pushed = False
    inst._tg_final_pushed_phase = None
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


# --------------------------------------------------------------------------- #
# A. Sub-agent gate (engine-driven; the push itself lives in test_tg_stream_push)
# --------------------------------------------------------------------------- #

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
    # Reflection fired on the child, but the stream-time push's root gate suppressed it.
    assert inst._auto_skill_proposed is True, 'reflection should have fired on the sub-agent'
    supervisor.notify_user.assert_not_called(), \
        'a sub-agent reflection must not push to the phone'


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
