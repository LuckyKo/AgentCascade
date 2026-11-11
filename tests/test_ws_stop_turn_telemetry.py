"""BUG_0287: the WS Stop path must close the in-flight telemetry turn.

Pressing Stop transitions active agents to IDLE directly in the WS handler
(``ws_handlers.WsMessageHandler.do_stop``) and never runs the engine's
``_run_finalize`` — the only other ``record_turn_end`` call site
(``engine/run_loop.py``). Before the fix, the stopped turn stayed open in
``TelemetryCollector._active_turns`` and the NEXT ``record_turn_start`` warned
and silently discarded it (losing its duration and LLM/tool attribution).

These tests drive the real ``do_stop`` against a minimal pool double (same
shape as ``test_api_endpoints._FakePool``) carrying a real ``TelemetryCollector``
as ``pool.telemetry``, mirroring ``test_telemetry_turn_lifecycle``.

The "no discard warning" assertion is made by monkeypatching the telemetry
module's ``_logger`` rather than ``caplog``: ``_logger`` is an ``_AppLoggerProxy``
that may not propagate to the root logger, and a silent ``caplog`` would make a
"warning was not emitted" check pass vacuously.
"""

import logging
import threading
from unittest.mock import MagicMock

import pytest

from agent_cascade.agent_instance import ACTIVE_STATES, AgentState, InvalidStateTransition
from agent_cascade.ws_handlers import WsMessageHandler
from agent_cascade.telemetry import TelemetryCollector
import agent_cascade.telemetry as telemetry_mod


class _FakeInstance:
    """Minimal AgentInstance double exposing the state attrs do_stop touches."""

    def __init__(self, state):
        self.state = state
        self._state_lock = threading.RLock()
        self._compression_lock = threading.RLock()
        self._continue_saved_msg = None

    def _transition(self, new_state):
        # Faithful to the real transition: legal only from an active state.
        if self.state not in ACTIVE_STATES:
            raise InvalidStateTransition(self.state, new_state)
        self.state = new_state


class _FakePool:
    """Minimal AgentPool double for the Stop handler, with telemetry attached."""

    def __init__(self, instances=None, telemetry=None):
        self.instances = dict(instances or {})
        self.telemetry = telemetry
        self._run_generation = 0
        self.terminated_instances = set()
        self.stopped = False
        self.stop_session_calls = 0

    def _mark_activity(self, name):
        pass

    def stop_session(self, release_slots=True):
        self.stop_session_calls += 1


def _session():
    return {'stop_requested': False, 'generating': True, 'generation_id': 5}


def _turn_end_events(collector, instance_name):
    return [e for e in collector.events
            if e.get('type') == 'turn_end' and e.get('instance') == instance_name]


def test_ws_stop_closes_open_turn_and_next_start_does_not_discard(tmp_path, monkeypatch):
    """Stop must record_turn_end for stopped instances so the next turn is clean."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    # Open a turn for the instance that will be stopped (mirrors the Maine 288 s case).
    collector.record_turn_start('Maine', config_fingerprint='fp1', agent_class='orchestrator')
    assert 'Maine' in collector._active_turns

    pool = _FakePool(
        instances={'Maine': _FakeInstance(AgentState.RUNNING),
                   'idle': _FakeInstance(AgentState.IDLE)},
        telemetry=collector,
    )

    # Capture every WARNING the telemetry module would emit.
    mock_logger = MagicMock()
    monkeypatch.setattr(telemetry_mod, '_logger', mock_logger)

    WsMessageHandler.do_stop(_session(), threading.Lock(), pool)

    # The turn was closed by the handler (not left open), so it is no longer active.
    assert 'Maine' not in collector._active_turns
    # ...and it was recorded, not discarded: exactly one turn_end advanced the count.
    summary = collector.get_session_summary()
    assert summary['total_runs'] == 1
    assert len(_turn_end_events(collector, 'Maine')) == 1
    # The IDLE instance had no turn; it must not have been counted.
    assert 'idle' not in collector._active_turns

    mock_logger.reset_mock()
    # The next turn must NOT find a stale open turn → no discard warning.
    collector.record_turn_start('Maine', config_fingerprint='fp2', agent_class='orchestrator')
    warn_text = [' '.join(str(a) for a in c.args) for c in mock_logger.warning.call_args_list]
    assert not any('still open' in m or 'will not be recorded' in m for m in warn_text), (
        f"expected no discard warning after Stop, got {warn_text}")


def test_ws_stop_turn_close_is_idempotent_with_engine_finalize(tmp_path):
    """Handler close + engine finalize both run: the turn is recorded exactly once."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    collector.record_turn_start('Maine', config_fingerprint='fp1', agent_class='coder')

    pool = _FakePool(instances={'Maine': _FakeInstance(AgentState.RUNNING)}, telemetry=collector)
    WsMessageHandler.do_stop(_session(), threading.Lock(), pool)

    # Simulate the engine's _run_finalize also running afterwards: record_turn_end
    # pops, so a second call is a no-op and does NOT double-count the turn.
    collector.record_turn_end('Maine')

    assert collector.get_session_summary()['total_runs'] == 1
    assert len(_turn_end_events(collector, 'Maine')) == 1


def test_ws_stop_with_none_telemetry_is_noop(tmp_path):
    """pool.telemetry may be None (pool/core.py); the Stop path must not crash."""
    pool = _FakePool(instances={'Maine': _FakeInstance(AgentState.RUNNING)}, telemetry=None)
    session = _session()

    WsMessageHandler.do_stop(session, threading.Lock(), pool)

    # Stop still did its core work despite the missing telemetry handle.
    assert session['stop_requested'] is True
    assert session['generating'] is False
    assert pool.instances['Maine'].state == AgentState.IDLE
