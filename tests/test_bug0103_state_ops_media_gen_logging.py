"""BUG_0103 regression: state_ops / media_gen records must reach the app log sink.

Root cause of the bug: both modules used ``logging.getLogger(__name__)``. Their dotted
names ('agent_cascade.state_ops', 'agent_cascade.tools.media_gen') are SIBLINGS of
'agent_cascade_logger' — the only logger the console/file handlers attach to (log.py:setup_logger)
— so they propagate to an unconfigured root and their DEBUG/INFO records fall to
``logging.lastResort`` (fixed at WARNING) and never reach console.log. The fix routes
both modules through the late-bound ``_AppLoggerProxy`` (single source of truth, now
defined once in agent_cascade.log), which resolves the live ``log.logger`` on every
attribute access.

This test is deterministic and does NOT depend on a live LLM or init_logging(): it
attaches a handler to whatever object ``agent_cascade.log.logger`` currently is (exactly
what the proxy delegates to), sets that logger's level so the records are created, and
asserts a record emitted THROUGH each module proxy lands on the handler. Level and
handlers are restored in ``finally`` so a failure cannot leak state into later tests.
"""

import logging

import pytest

import agent_cascade.log as log_mod
import agent_cascade.state_ops as state_ops
import agent_cascade.tools.media_gen as media_gen
from agent_cascade.log import _AppLoggerProxy


class _ListHandler(logging.Handler):
    """In-memory handler that keeps emitted records for assertion."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_module_loggers_are_the_canonical_proxy():
    """Regression guard: the deviation (logging.getLogger(__name__)) must not return."""
    assert isinstance(state_ops.logger, _AppLoggerProxy), \
        'state_ops.logger must be the late-bound proxy'
    assert isinstance(media_gen.logger, _AppLoggerProxy), \
        'media_gen.logger must be the late-bound proxy'


def test_records_emitted_through_the_module_loggers_reach_the_sink():
    """A DEBUG/INFO record emitted via state_ops.logger / media_gen.logger must reach
    the handler-bearing sink logger — the exact guarantee the bug was missing."""
    # The proxy resolves log.logger on every call; attach to that same live object.
    sink = log_mod.logger
    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)

    prev_level = sink.level
    sink.addHandler(handler)
    sink.setLevel(logging.DEBUG)
    try:
        state_ops.logger.debug('Saved state %s', 'TEST_MARKER_state_ops')
        media_gen.logger.info('[MediaGen] TEST_MARKER_media_gen VRAM release')
    finally:
        sink.setLevel(prev_level)
        sink.removeHandler(handler)

    msgs = [r.getMessage() for r in handler.records]
    assert any('TEST_MARKER_state_ops' in m for m in msgs), \
        f'state_ops record must reach the sink, got {msgs!r}'
    assert any('[MediaGen] TEST_MARKER_media_gen' in m for m in msgs), \
        f'media_gen record must reach the sink, got {msgs!r}'
