"""Unit tests for backfilling Message.ts from a log dict's ISO 'timestamp' field.

On session load (``load_session_from_log``) reconstructed Messages have ``ts=None``
because the log stores a naive local-time ISO string, not the in-memory unix ``ts``.
``SessionIOMixin._backfill_ts_from_dict`` restores ``ts`` from that string so resumed
sessions' first compression window shows a correct timestamp range.

These tests target the small extracted helper directly (no LLM / filesystem needed).
"""

import datetime

from agent_cascade.llm.schema import USER, Message
from agent_cascade.pool.session_io import SessionIOMixin


def _msg(ts=None):
    """Build a minimal valid Message with an optional pre-set ts."""
    return Message(role=USER, content='hello', ts=ts)


class TestBackfillTsFromDict:

    def test_valid_iso_timestamp_sets_float_ts(self):
        iso = '2026-09-07T12:54:00.071151'
        msg = _msg(ts=None)
        SessionIOMixin._backfill_ts_from_dict(msg, {'timestamp': iso})

        expected = datetime.datetime.fromisoformat(iso).timestamp()
        assert msg.ts is not None
        assert isinstance(msg.ts, float)
        assert abs(msg.ts - expected) < 1e-6

    def test_existing_ts_is_not_overwritten(self):
        # A message that already carries a ts must keep it, even with a valid timestamp.
        original = 1234567890.5
        msg = _msg(ts=original)
        SessionIOMixin._backfill_ts_from_dict(msg, {'timestamp': '2026-09-07T12:54:00.071151'})

        assert msg.ts == original

    def test_missing_timestamp_leaves_ts_none(self):
        msg = _msg(ts=None)
        SessionIOMixin._backfill_ts_from_dict(msg, {'role': USER, 'content': 'x'})

        assert msg.ts is None

    def test_empty_string_timestamp_leaves_ts_none(self):
        msg = _msg(ts=None)
        SessionIOMixin._backfill_ts_from_dict(msg, {'timestamp': ''})

        assert msg.ts is None

    def test_malformed_timestamp_leaves_ts_none_and_does_not_raise(self):
        msg = _msg(ts=None)
        # Non-parseable string: must not raise and must leave ts unchanged.
        SessionIOMixin._backfill_ts_from_dict(msg, {'timestamp': 'not-a-real-date'})

        assert msg.ts is None

    def test_non_string_timestamp_leaves_ts_none(self):
        # Defensive: a non-string value (e.g. int/None) must not raise or set ts.
        for bad in (1234567890, None, 3.14):
            msg = _msg(ts=None)
            SessionIOMixin._backfill_ts_from_dict(msg, {'timestamp': bad})
            assert msg.ts is None
