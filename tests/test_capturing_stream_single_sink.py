"""D-8 regression: _CapturingStream emits each record exactly once.

The logging tree (console handler + rotating file handler) is the ONLY sink.
The raw stream write is a fallback for when logging itself fails.
"""
import io
import logging
from unittest.mock import patch, MagicMock

import pytest


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _make_capturing_stream(stream_type='stderr'):
    from agent_cascade.log import _CapturingStream
    original = io.StringIO()
    return _CapturingStream(stream_type, original), original


def test_warning_emitted_once():
    """A stderr write produces exactly 1 log record and NO raw stream output."""
    cs, original = _make_capturing_stream('stderr')
    handler = _ListHandler()

    with patch('agent_cascade.log.logger') as mock_logger:
        mock_logger.log = MagicMock(side_effect=lambda level, msg: handler.emit(
            logging.LogRecord('test', level, '', 0, msg, None, None)))
        cs.write('boom\n')

    assert len(handler.records) == 1
    assert original.getvalue() == ''


def test_stdout_print_still_logged():
    """A stdout write at INFO reaches the handler (console.log guarantee)."""
    cs, original = _make_capturing_stream('stdout')
    handler = _ListHandler()

    with patch('agent_cascade.log.logger') as mock_logger:
        mock_logger.log = MagicMock(side_effect=lambda level, msg: handler.emit(
            logging.LogRecord('test', level, '', 0, msg, None, None)))
        cs.write('hello\n')

    assert len(handler.records) == 1
    assert handler.records[0].msg == 'hello'
    # No raw duplicate on the original stream
    assert original.getvalue() == ''


def test_falls_back_to_original_when_logging_fails():
    """When logger.log raises, the message reaches the original stream."""
    cs, original = _make_capturing_stream('stderr')

    with patch('agent_cascade.log.logger') as mock_logger:
        mock_logger.log = MagicMock(side_effect=RuntimeError('logging broken'))
        cs.write('fallback_msg\n')

    assert 'fallback_msg' in original.getvalue()


def test_empty_and_whitespace_writes_are_noops():
    """write('') and write('   \\n') produce no record and no raw write."""
    cs, original = _make_capturing_stream('stderr')
    handler = _ListHandler()

    with patch('agent_cascade.log.logger') as mock_logger:
        mock_logger.log = MagicMock(side_effect=lambda level, msg: handler.emit(
            logging.LogRecord('test', level, '', 0, msg, None, None)))
        cs.write('')
        cs.write('   \n')

    assert len(handler.records) == 0
    assert original.getvalue() == ''
