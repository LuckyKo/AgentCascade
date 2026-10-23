"""P8-4 — a terminated tool must not be masked by an UnboundLocalError.

When a tool's ``execute_tool`` raises ``AgentTerminatedError`` (a clean stop-check
abort), the inner ``except AgentTerminatedError: raise`` re-raises into the per-tool
``finally``. That finally references ``tool_result`` at the post-processing call
(only when ``compression_handler`` is truthy). Pre-fix ``tool_result`` was never
bound on that path, so an ``UnboundLocalError`` REPLACED the ``AgentTerminatedError``
and the termination was lost. The fix binds ``tool_result = ''`` before the try and
records the termination in telemetry (success=False, error='terminated').

Reuses the batch harness from tests.test_call_agent_parallel_batch_state_deferral
(its _make_pool/_make_engine drive the real _execute_detected_tools loop).
"""

from unittest.mock import MagicMock

import pytest

from agent_cascade.exceptions import AgentTerminatedError
from tests.test_call_agent_parallel_batch_state_deferral import (
    CALLER_CLASS,
    CALLER_NAME,
    _SimpleInstance,
    _make_engine,
    _make_pool,
    _run_batch,
)


def _tool_msg(name='write_file'):
    """A turn_output assistant message carrying one non-call_agent tool call."""
    return {
        'role': 'assistant',
        'function_call': {'name': name, 'arguments': {'path': 'x', 'content': 'y'}},
        'extra': {'function_id': f'fc_{name}'},
    }


def _terminated_engine(*, compression_handler, telemetry=None):
    """Engine whose tool_dispatcher.execute_tool raises AgentTerminatedError."""
    caller = _SimpleInstance(CALLER_NAME, CALLER_CLASS)
    pool = _make_pool(caller)
    engine = _make_engine(pool, caller)
    # The real masking site (post-processing) only runs when a handler is configured.
    engine.compression_handler = compression_handler
    engine.tool_dispatcher = MagicMock()
    engine.tool_dispatcher.execute_tool.side_effect = AgentTerminatedError('stopped')
    # Stub the post-processing body so the test observes the call SITE, not its logic.
    engine._postprocess_tool_result = lambda *a, **k: ''
    if telemetry is not None:
        engine._telemetry = lambda: telemetry
    return engine, caller


def test_terminated_tool_with_compression_handler_raises_agent_terminated():
    """Discriminator: a terminated tool with a compression handler must surface
    AgentTerminatedError, not an UnboundLocalError from the finally's post-processing."""
    engine, caller = _terminated_engine(compression_handler=object())
    with pytest.raises(AgentTerminatedError):
        _run_batch(engine, caller, [_tool_msg()])


def test_terminated_tool_records_failed_tool_call_end():
    """Discriminator: the termination must be recorded as a FAILED tool call (success=False,
    error='terminated', result_chars=0), not silently skipped (pre-fix the result_chars
    computation itself raised and was swallowed by the telemetry try/except)."""
    tel = MagicMock()
    engine, caller = _terminated_engine(compression_handler=object(), telemetry=tel)
    with pytest.raises(AgentTerminatedError):
        _run_batch(engine, caller, [_tool_msg()])

    assert tel.record_tool_call_end.call_count == 1
    kwargs = tel.record_tool_call_end.call_args.kwargs
    assert kwargs['success'] is False, 'a terminated tool must not be recorded as a success'
    assert kwargs['error'] == 'terminated'
    assert kwargs['result_chars'] == 0


def test_mid_batch_termination_clears_thread_local():
    """GUARD (not a discriminator): a termination mid-batch must still clear the
    thread-local instance name (the finally always runs) and surface AgentTerminatedError."""
    from unittest.mock import patch
    engine, caller = _terminated_engine(compression_handler=object())
    with patch('agent_cascade.engine.tool_execution.clear_current_instance_name') as cleared:
        with pytest.raises(AgentTerminatedError):
            _run_batch(engine, caller, [_tool_msg('write_file'), _tool_msg('read_file')])
    assert cleared.call_count >= 1, 'the thread-local instance name must be cleared on termination'
