"""Tests for ReadLogsTool._parse_range — 1-indexed inclusive range parsing.

Verifies the negative-index convention: both bounds (and single indices) use
1-indexed inclusive semantics where -1 = last entry, -2 = second-to-last,
i.e. `total_entries + 1 + x`. Matches the convention in
agent_cascade/operation_manager/file_operations.py `_parse_range`.
"""

import pytest

from agent_cascade.tools.custom.read_logs import ReadLogs


TOTAL = 10


@pytest.mark.parametrize(
    "range_str,expected",
    [
        # Positive bounds
        ("1:10", (0, 10)),
        ("3:7", (2, 7)),
        ("5:", (4, 10)),
        (":4", (0, 4)),
        # Empty = all entries
        ("", (0, 10)),
        # Single entry (no colon)
        ("5", (4, 5)),
        # Negative single index
        ("-1", (9, 10)),
        ("-2", (8, 9)),
        # Negative bounds in ranges: -1 as an END bound = last entry (inclusive)
        ("-1:-1", (9, 10)),  # the key fix: was ValueError before
        ("-3:-1", (7, 10)),
        ("5:-1", (4, 10)),
        ("1:-1", (0, 10)),
        # Out-of-range clamping (no error)
        ("15:20", (9, 10)),  # both bounds clamp down to entry 10
        ("-99", (0, 1)),  # single index clamps to first entry
    ],
)
def test_parse_range_valid(range_str, expected):
    assert ReadLogs._parse_range(range_str, TOTAL) == expected


@pytest.mark.parametrize("range_str", ["7:3", "1:2:3"])
def test_parse_range_raises(range_str):
    with pytest.raises(ValueError):
        ReadLogs._parse_range(range_str, TOTAL)


def test_parse_range_empty_log():
    assert ReadLogs._parse_range("", 0) == (0, 0)


@pytest.mark.parametrize("range_str,expected", [("-1", (0, 1)), ("-1:-1", (0, 1)), ("1:1", (0, 1))])
def test_parse_range_single_entry_log(range_str, expected):
    assert ReadLogs._parse_range(range_str, 1) == expected
