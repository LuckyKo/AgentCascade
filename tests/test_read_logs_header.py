"""Tests for the read_logs operation-status header.

These cover the additive change that mirrors read_file's output style:
  - a first-line status header with entry window, total count, format, humanized size
  - an empty-log message mirroring read_file's empty-file style

The [TRUNCATED] marker, pagination footer and "showing last N of M" hint were
intentionally dropped in 10b3266: the header window `lines first-last/total`
already conveys which entries are shown and how to derive the next range. The
tests below pin that the header encodes the window (and that no stale footer
remains) instead of the removed affordances.

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
    with open(p, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry) + '\n')
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
    return tool.call({'log_file': path.name, **params})


class TestReadLogsHeader:
    def test_header_present_with_total_and_humanized_size(self):
        """(a) Header is the first line, has correct total count and a humanized size."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': f"m{i}"} for i in range(5)])
            result = _read(p, format='raw')

            lines = result.strip().split('\n')
            assert lines[0].startswith('OK: Read '), f"Header missing: {lines[0]}"
            # 5 entries total, full window (no explicit range -> default last-20 covers all 5)
            assert 'lines 1-5/5' in lines[0]
            assert '(raw,' in lines[0]
            # Small file (<1KB) -> "N B" humanized size, e.g. "... (raw, 175 B)"
            import re
            assert re.search(r'\(\w+, \d+ B\)$', lines[0]), f"No 'N B' size in header: {lines[0]}"

    def test_header_window_conveys_partial_range(self):
        """(b) The header window encodes a partial explicit range (10b3266 dropped the [TRUNCATED] tag).

        For a 10-entry log, range='3:7' must show exactly `lines 3-7/10`, which tells
        the model entries 1-2 and 8-10 are absent; a full range or default shows `1-10/10`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': f"m{i}"} for i in range(10)])

            # Partial range -> header pins the exact window
            partial = _read(p, format='raw', range='3:7')
            assert 'lines 3-7/10' in partial.split('\n')[0]

            # Full range -> full window
            full = _read(p, format='raw', range='1:10')
            assert 'lines 1-10/10' in full.split('\n')[0]

            # Default (no range) covering all entries -> full window
            default = _read(p, format='raw')
            assert 'lines 1-10/10' in default.split('\n')[0]

    def test_header_encodes_range_no_stale_footer(self):
        """(c) The header encodes the window so the next range is derivable (10b3266 dropped the pagination footer).

        For a 10-entry log, range='3:7' yields `lines 3-7/10` — from which the model
        derives both the head (`1:2`) and the next range (`8:10`). The old
        `→ continue at range=...` footer must no longer appear anywhere in the output.
        """
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': f"m{i}"} for i in range(10)])

            result = _read(p, format='raw', range='3:7')
            # Header encodes first/last/total -> next range (8:10) is derivable.
            assert 'lines 3-7/10' in result.split('\n')[0]

            # The dropped pagination footer must not resurface, for any range.
            assert 'continue at range' not in result

            to_end = _read(p, format='raw', range='5:10')
            assert 'lines 5-10/10' in to_end.split('\n')[0]
            assert 'continue at range' not in to_end

    def test_default_last20_header_shows_tail_window(self):
        """(d) Implicit last-20 default shows the tail window in the header when log > 20 (10b3266 dropped the hint).

        For a 35-entry log with no range, the default reads entries 16-35; the header
        `lines 16-35/35` conveys that entries 1-15 are not shown. The old
        'showing last N of M' hint must no longer appear.
        """
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': f"m{i}"} for i in range(35)])

            result = _read(p, format='raw')  # no range -> default last 20
            header = result.split('\n')[0]
            assert 'lines 16-35/35' in header
            # The dropped tail hint must not resurface.
            assert 'showing last' not in result

    def test_default_last20_no_hint_when_log_small(self):
        """No tail hint when the log has <= 20 entries (default already shows everything)."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': f"m{i}"} for i in range(3)])

            result = _read(p, format='raw')
            assert 'showing last' not in result
            assert 'continue at range' not in result

    def test_empty_log_message(self):
        """(e) Empty log returns a read_file-style empty message."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'empty.jsonl'
            p.write_text('', encoding='utf-8')

            result = _read(p, format='raw')
            assert result.startswith('OK: Read ')
            assert 'lines 0/0' in result
            # 0-byte file -> "0 B"
            assert '0 B' in result

    def test_single_entry_full_range(self):
        """A 1-entry log with an explicit full range shows 'lines 1-1/1', no [TRUNCATED], no footer."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': 'only'}])

            result = _read(p, format='raw', range='1')
            header = result.split('\n')[0]
            assert 'lines 1-1/1' in header
            assert '[TRUNCATED]' not in header
            assert 'continue at range' not in result


class TestReadLogsHeaderSimpleFormat:
    def test_header_first_line_in_simple_format(self):
        """The header is also the first line for simple format, and entries follow it."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(
                Path(tmp), 't.jsonl',
                [
                    {'role': 'user', 'content': 'hello'},
                    {'role': 'assistant', 'timestamp': '2026-08-14T10:30:00Z', 'content': 'hi there'},
                ],
            )
            result = _read(p, format='simple')

            lines = result.strip().split('\n')
            assert lines[0].startswith('OK: Read ')
            assert 'lines 1-2/2' in lines[0]
            assert '(simple,' in lines[0]
            # Entries still carry their role labels after the header
            assert 'USER' in result
            assert 'ASSISTANT' in result
