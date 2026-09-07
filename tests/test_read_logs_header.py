"""Tests for the read_logs operation-status header and pagination footer.

These cover the additive change that mirrors read_file's output style:
  - a first-line status header with entry window, total count, format, humanized size
  - a [TRUNCATED] marker for partial explicit ranges (not for full ranges or the default)
  - a pagination footer pointing at the next range / the "showing last N of M" hint
  - an empty-log message mirroring read_file's empty-file style

Per-entry rendering (truncation modes, raw vs simple) is NOT re-tested here — see
test_regression_logging_refinement.py for that.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _write_jsonl(tmp_path: Path, name: str, entries: list) -> Path:
    p = tmp_path / name
    with open(p, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return p


def _make_pool(log_dir: str | None):
    pool = MagicMock()
    if log_dir is not None:
        pool._logger.log_dir = log_dir
    else:
        del pool._logger.log_dir
    return pool


def _read(path: Path, **params) -> str:
    from agent_cascade.tools.custom.read_logs import ReadLogs

    tool = ReadLogs(agent_pool=_make_pool(str(path.parent)))
    return tool.call({"log_file": path.name, **params})


class TestReadLogsHeader:
    def test_header_present_with_total_and_humanized_size(self):
        """(a) Header is the first line, has correct total count and a humanized size."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), "t.jsonl", [{"role": "user", "content": f"m{i}"} for i in range(5)])
            result = _read(p, format="raw")

            lines = result.strip().split("\n")
            assert lines[0].startswith("OK: Read "), f"Header missing: {lines[0]}"
            # 5 entries total, full window (no explicit range -> default last-20 covers all 5)
            assert "lines 1-5/5" in lines[0]
            assert "(raw," in lines[0]
            # Small file (<1KB) -> "N B" humanized size, e.g. "... (raw, 175 B)"
            import re
            assert re.search(r"\(\w+, \d+ B\)$", lines[0]), f"No 'N B' size in header: {lines[0]}"

    def test_truncated_marker_for_partial_range_not_full(self):
        """(b) [TRUNCATED] appears for a partial explicit range, not for a full range."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), "t.jsonl", [{"role": "user", "content": f"m{i}"} for i in range(10)])

            # Partial range -> marker present
            partial = _read(p, format="raw", range="3:7")
            assert "[TRUNCATED]" in partial.split("\n")[0]

            # Full range -> no marker
            full = _read(p, format="raw", range="1:10")
            assert "[TRUNCATED]" not in full.split("\n")[0]

            # Default (no range) covering all entries -> no marker
            default = _read(p, format="raw")
            assert "[TRUNCATED]" not in default.split("\n")[0]

    def test_pagination_footer_points_to_next_range(self):
        """(c) Footer points to the correct next range for a partial explicit range."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), "t.jsonl", [{"role": "user", "content": f"m{i}"} for i in range(10)])

            result = _read(p, format="raw", range="3:7")
            # last=7, total=10 -> continue at 8:10
            assert '→ continue at range="8:10"' in result

            # A range that already reaches the end has no footer
            to_end = _read(p, format="raw", range="5:10")
            assert "continue at range" not in to_end

    def test_default_last20_shows_tail_hint_when_log_larger(self):
        """(d) Implicit last-20 default surfaces a 'showing last N of M' hint when log > 20."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), "t.jsonl", [{"role": "user", "content": f"m{i}"} for i in range(35)])

            result = _read(p, format="raw")  # no range -> default last 20
            header = result.split("\n")[0]
            # Default is NOT marked truncated, but the footer conveys it's only the tail
            assert "[TRUNCATED]" not in header
            assert "showing last 20 of 35" in result
            assert 'range="1:35"' in result

    def test_default_last20_no_hint_when_log_small(self):
        """No tail hint when the log has <= 20 entries (default already shows everything)."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), "t.jsonl", [{"role": "user", "content": f"m{i}"} for i in range(3)])

            result = _read(p, format="raw")
            assert "showing last" not in result
            assert "continue at range" not in result

    def test_empty_log_message(self):
        """(e) Empty log returns a read_file-style empty message."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "empty.jsonl"
            p.write_text("", encoding="utf-8")

            result = _read(p, format="raw")
            assert result.startswith("OK: Read ")
            assert "lines 0/0" in result
            # 0-byte file -> "0 B"
            assert "0 B" in result

    def test_single_entry_full_range(self):
        """A 1-entry log with an explicit full range shows 'lines 1-1/1', no [TRUNCATED], no footer."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), "t.jsonl", [{"role": "user", "content": "only"}])

            result = _read(p, format="raw", range="1")
            header = result.split("\n")[0]
            assert "lines 1-1/1" in header
            assert "[TRUNCATED]" not in header
            assert "continue at range" not in result


class TestReadLogsHeaderSimpleFormat:
    def test_header_first_line_in_simple_format(self):
        """The header is also the first line for simple format, and entries follow it."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(
                Path(tmp), "t.jsonl",
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "timestamp": "2026-08-14T10:30:00Z", "content": "hi there"},
                ],
            )
            result = _read(p, format="simple")

            lines = result.strip().split("\n")
            assert lines[0].startswith("OK: Read ")
            assert "lines 1-2/2" in lines[0]
            assert "(simple," in lines[0]
            # Entries still carry their role labels after the header
            assert "USER" in result
            assert "ASSISTANT" in result
