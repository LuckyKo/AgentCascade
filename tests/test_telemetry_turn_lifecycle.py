"""Turn-end telemetry must fire on every ``ExecutionEngine.run()`` exit path.

BUG_0127: ``record_turn_end`` used to be called only on the empty-conversation
early exit, normal completion and the unhandled-``Exception`` path. It was
skipped for ``AgentTerminatedError`` (dismissal/stop during tool execution) and
for ``GeneratorExit`` (consumer abandoned the generator). Config/agent-class
attribution of every LLM/tool event created during a turn is gated on the active
turn, so an unclosed turn made the whole final turn invisible in both tables.

These tests drive the real ``run()`` with a MagicMock pool (the same minimal
harness used by ``test_bug1_bug6_bug4_8_slot_deadlock.py``) and a real
``TelemetryCollector`` attached as ``pool.telemetry``.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.llm.schema import Message
from agent_cascade.telemetry import TelemetryCollector


def _make_instance(name='A'):
    return AgentInstance(
        instance_name=name,
        agent_class='coder',
        conversation=[Message(role='system', content='sys')],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=0,
        parent_instance='Main',
    )


def _make_engine(collector):
    inst = _make_instance()
    pool = MagicMock()
    pool.stopped = False
    pool._run_generation = 1
    pool.is_instance_terminated.return_value = False
    pool.get_instance.return_value = inst
    pool.has_pending.return_value = False
    pool.has_messages.return_value = False
    pool._compression_halted = set()
    pool.telemetry = collector
    # A concrete template keeps record_turn_start's fingerprint/description
    # computation on the real code path (MagicMock attributes are not oracle
    # enough for hashlib).
    pool.get_template.return_value = SimpleNamespace(
        llm=SimpleNamespace(model='unit-model', generate_cfg={}, cfg={}),
        function_map={},
    )

    engine = ExecutionEngine(pool)
    engine._my_generation = 1
    return engine, inst, pool


def _turn_events(collector):
    return [e for e in list(collector.events) if e.get('type') == 'turn_end']


def test_terminated_run_records_turn_end(tmp_path):
    """AgentTerminatedError mid-run must still close the telemetry turn."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, inst, _pool = _make_engine(collector)
    engine._setup_turn = MagicMock(side_effect=AgentTerminatedError('stopped'))

    list(engine.run(inst))

    summary = collector.get_session_summary()
    assert summary['total_turns'] == 1
    assert len(_turn_events(collector)) == 1

    # Time and attribution made it into both tables, not just the session total.
    configs = collector.get_config_comparison()
    classes = collector.get_agent_class_summary()
    assert configs and configs[0]['turns'] == 1
    assert collector._config_stats[configs[0]['config_fingerprint']]['total_duration_ms'] > 0
    assert classes and classes[0]['agent_class'] == 'coder'
    assert collector._agent_class_stats['coder']['total_time_ms'] > 0


def test_unhandled_exception_records_exactly_one_turn_end(tmp_path):
    """The generic exception path must not double-record now that the finally does."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, inst, _pool = _make_engine(collector)
    engine._setup_turn = MagicMock(side_effect=RuntimeError('boom'))

    list(engine.run(inst))

    assert collector.get_session_summary()['total_turns'] == 1
    assert len(_turn_events(collector)) == 1


def test_early_exit_records_exactly_one_turn_end(tmp_path):
    """Empty-conversation early exit keeps exactly one turn_end (no double count)."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, inst, _pool = _make_engine(collector)
    engine._setup_turn = MagicMock(return_value=(None, None, None))

    list(engine.run(inst))

    assert collector.get_session_summary()['total_turns'] == 1
    assert len(_turn_events(collector)) == 1


def test_abandoned_generator_records_exactly_one_turn_end(tmp_path):
    """Consumer abandoning the run() generator mid-turn (GeneratorExit) closes the turn exactly once.

    The subtlest BUG_0127 exit path: drive one yield, then close() so GeneratorExit is
    raised inside run() at the yield. The exit finally must record_turn_end exactly once
    (record_turn_end is idempotent via pop), making the abandoned turn visible in the
    session total rather than silently dropping it.
    """
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, inst, _pool = _make_engine(collector)
    # Non-empty conversation so the turn loop is entered; _pre_llm_checks short-circuits
    # to a `yield response` (core.py:1238), so the generator is suspended at a yield when closed.
    engine._setup_turn = MagicMock(return_value=([Message(role='user', content='hi')], [], []))
    engine._pre_llm_checks = MagicMock(return_value=True)

    gen = engine.run(inst)
    next(gen)  # drive to the first yield; record_turn_start has already run
    gen.close()  # raises GeneratorExit at the yield -> run()'s exit finally

    assert collector.get_session_summary()['total_turns'] == 1
    assert len(_turn_events(collector)) == 1


def test_no_active_turn_is_a_silent_noop(tmp_path):
    """A terminal stop before turn_start must not fabricate a turn_end event."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, inst, pool = _make_engine(collector)
    pool.is_instance_terminated.return_value = True

    list(engine.run(inst))

    assert collector.get_session_summary()['total_turns'] == 0
    assert _turn_events(collector) == []
    assert inst.state != AgentState.RUNNING
