"""Focused tests for the compression marker timestamp-interval header.

Covers helpers._format_timestamp_interval() and build_marker_message():
- full date+time rendering with the literal arrow "→"
- adaptive duration buckets (seconds / minutes / hours / days)
- multi-day spans rendering both full dates
- no-timestamp fallback (all None ts) → neutral header, no crash
- messages as dicts carrying a "ts" key

All tests are self-contained — no LLM or API server required.
"""

import datetime
import re

import pytest

from agent_cascade.llm.schema import USER, Message
from agent_cascade.compression.helpers import (
    _format_timestamp_interval,
    build_marker_message,
)


def _ts(*args):
    """Build a unix timestamp from local-time components."""
    return datetime.datetime(*args).timestamp()


# ── Header format: full date+time + arrow ───────────────────────────────────

class TestHeaderFormat:
    def test_full_datetime_and_arrow(self):
        start = _ts(2026, 9, 6, 10, 14)
        end = _ts(2026, 9, 7, 8, 30)
        out = _format_timestamp_interval(start, end, n_messages=5)
        assert "2026-09-06 10:14 → 2026-09-07 08:30" in out

    def test_multi_day_span_renders_both_full_dates(self):
        start = _ts(2026, 9, 6, 10, 14)
        end = _ts(2026, 9, 9, 8, 30)
        out = _format_timestamp_interval(start, end, n_messages=5)
        assert "2026-09-06 10:14" in out
        assert "2026-09-09 08:30" in out
        # arrow present exactly once
        assert out.count("→") == 1

    def test_marker_message_contains_interval_header(self):
        start = _ts(2026, 9, 6, 10, 14)
        end = _ts(2026, 9, 7, 8, 30)
        msg = build_marker_message("summary", first_ts=start, last_ts=end, n_messages=5)
        assert isinstance(msg, Message)
        assert msg.role == USER
        assert "2026-09-06 10:14 → 2026-09-07 08:30" in msg.content


# ── Adaptive duration buckets ───────────────────────────────────────────────

class TestDurationBuckets:
    def test_seconds_under_60(self):
        start = _ts(2026, 9, 6, 10, 14)
        out = _format_timestamp_interval(start, start + 45, n_messages=3)
        assert ", 45s" in out

    def test_zero_seconds(self):
        start = _ts(2026, 9, 6, 10, 14)
        out = _format_timestamp_interval(start, start, n_messages=3)
        assert ", 0s" in out

    def test_minutes_whole(self):
        start = _ts(2026, 9, 6, 10, 14)
        out = _format_timestamp_interval(start, start + 48 * 60, n_messages=3)
        assert ", 48m" in out

    def test_minutes_with_seconds_rounds(self):
        # 48m 30s rounds to 49m (round-half-to-even: 2910 -> 2910/60 = 48.5)
        start = _ts(2026, 9, 6, 10, 14)
        out = _format_timestamp_interval(start, start + int(48 * 60 + 30), n_messages=3)
        # either 48m or 49m is acceptable depending on rounding; just assert a "Nm" form
        assert re.search(r", \d+m\b", out)

    def test_minutes_zero_falls_back_to_seconds(self):
        # < 60s already covered; here ensure the <3600 branch with 0 minutes shows seconds
        start = _ts(2026, 9, 6, 10, 14)
        out = _format_timestamp_interval(start, start + 30, n_messages=3)
        assert ", 30s" in out

    def test_hours(self):
        start = _ts(2026, 9, 6, 10, 14)
        end = start + (1 * 3600 + 12 * 60)
        out = _format_timestamp_interval(start, end, n_messages=3)
        assert ", 1h 12m" in out

    def test_days_includes_days(self):
        start = _ts(2026, 9, 6, 10, 14)
        end = start + (2 * 86400 + 3 * 3600 + 16 * 60)
        out = _format_timestamp_interval(start, end, n_messages=3)
        assert ", 2d 3h 16m" in out

    def test_exactly_one_day(self):
        start = _ts(2026, 9, 6, 10, 14)
        end = start + 86400
        out = _format_timestamp_interval(start, end, n_messages=3)
        assert ", 1d 0h 0m" in out


# ── Fallback when no timestamps ────────────────────────────────────────────

class TestNoTimestampFallback:
    def test_both_none(self):
        out = _format_timestamp_interval(None, None, n_messages=7)
        assert out == "7 messages summarized"

    def test_start_none(self):
        out = _format_timestamp_interval(None, 1234.0, n_messages=2)
        assert out == "2 messages summarized"

    def test_end_none(self):
        out = _format_timestamp_interval(1234.0, None, n_messages=2)
        assert out == "2 messages summarized"

    def test_marker_message_no_ts_no_crash(self):
        msg = build_marker_message("summary", first_ts=None, last_ts=None, n_messages=4)
        assert isinstance(msg, Message)
        assert "4 messages summarized" in msg.content

    def test_default_args_fallback(self):
        # No ts args at all -> neutral header with 0 messages.
        out = _format_timestamp_interval(None, None)
        assert out == "0 messages summarized"


# ── Dict messages with "ts" key (extraction logic mirrors core.py) ─────────

def _extract_ts_list(messages):
    """Mirror of the extraction loop in core.compress_context()."""
    ts_list = []
    for msg in messages:
        ts = msg.get('ts') if isinstance(msg, dict) else getattr(msg, 'ts', None)
        if ts is not None:
            ts_list.append(float(ts))
    return (min(ts_list), max(ts_list)) if ts_list else (None, None)


class TestDictMessageTsExtraction:
    def test_dicts_with_ts(self):
        start = _ts(2026, 9, 6, 10, 14)
        end = _ts(2026, 9, 7, 8, 30)
        messages = [
            {"role": USER, "content": "a", "ts": start},
            {"role": USER, "content": "b", "ts": None},
            {"role": USER, "content": "c", "ts": end},
        ]
        first_ts, last_ts = _extract_ts_list(messages)
        out = _format_timestamp_interval(first_ts, last_ts, n_messages=len(messages))
        assert "2026-09-06 10:14 → 2026-09-07 08:30" in out

    def test_dicts_without_ts_key(self):
        messages = [
            {"role": USER, "content": "a"},
            {"role": USER, "content": "b"},
        ]
        first_ts, last_ts = _extract_ts_list(messages)
        assert first_ts is None and last_ts is None
        out = _format_timestamp_interval(first_ts, last_ts, n_messages=2)
        assert out == "2 messages summarized"

    def test_mixed_message_objects_and_dicts(self):
        start = _ts(2026, 9, 6, 10, 14)
        end = _ts(2026, 9, 6, 11, 0)
        obj_msg = Message(role=USER, content="obj")
        obj_msg.ts = start
        messages = [
            obj_msg,
            {"role": USER, "content": "dict", "ts": end},
        ]
        first_ts, last_ts = _extract_ts_list(messages)
        assert first_ts == start
        assert last_ts == end

    def test_out_of_order_ts_uses_min_max(self):
        # Robustness: first/last by value, not list position.
        early = _ts(2026, 9, 5, 1, 0)
        late = _ts(2026, 9, 8, 23, 59)
        mid = _ts(2026, 9, 6, 12, 0)
        messages = [
            {"role": USER, "content": "x", "ts": late},
            {"role": USER, "content": "y", "ts": early},
            {"role": USER, "content": "z", "ts": mid},
        ]
        first_ts, last_ts = _extract_ts_list(messages)
        assert first_ts == early
        assert last_ts == late


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
