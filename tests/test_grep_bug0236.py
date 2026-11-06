"""BUG_0236 regression tests: grep must bound per-line render length and keep partial
matches when ripgrep is killed at the deadline.

Incident: grepping a log directory full of multi-KB single-line `.jsonl` records returned
"Found 448 matches ... [TIMED OUT after 5s] [TRUNCATED — showing 3 of 2667 lines
(10299785 chars total)]" and wrote a multi-MB spillover. Each match is a whole JSONL
document, so a line-count-based budget was defeated by the lines themselves.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager import grep as grep_mod
from agent_cascade.operation_manager.grep import _abbreviate_grep_line, _format_grep_entry


def _make_om(root: Path) -> OperationManager:
    return OperationManager(base_dir=str(root))


def _jsonl_line(i: int, blob_chars: int = 2000) -> str:
    return json.dumps({'seq': i, 'msg': f'materialization test {i}', 'blob': 'x' * blob_chars})


class TestLineAbbreviation:
    def test_short_line_is_byte_identical(self):
        line = 'ordinary code line'
        assert _abbreviate_grep_line(line, 500) == line

    def test_exactly_at_cap_is_unchanged(self):
        """Boundary: a line of exactly ``cap`` chars is NOT abbreviated (len <= cap)."""
        assert _abbreviate_grep_line('q' * 500, 500) == 'q' * 500
        assert 'line abbreviated' in _abbreviate_grep_line('q' * 501, 500)

    def test_long_line_discloses_omitted_length(self):
        line = 'y' * 1000
        out = _abbreviate_grep_line(line, 100)
        assert out.startswith('y' * 100), out
        assert 'line abbreviated: +900 chars' in out, out
        assert len(out) < 200, out

    def test_cap_zero_disables(self):
        line = 'z' * 1000
        assert _abbreviate_grep_line(line, 0) == line

    def test_renderer_applies_cap_to_every_path(self):
        """``_format_grep_entry`` is the shared renderer — cap it there and all paths follow."""
        with patch('agent_cascade.settings.GREP_MAX_LINE_CHARS', 50):
            entry = _format_grep_entry('logs/a.jsonl', 1, 'm' * 500, '>>>')
        assert entry.startswith('logs/a.jsonl:1: >>>' + 'm' * 50), entry
        assert '+450 chars' in entry, entry


class TestJsonlDirectoryGrep:
    def _build(self, root: Path, files: int = 3, records: int = 20) -> None:
        logs = root / 'logs'
        logs.mkdir()
        for f in range(files):
            (logs / f'run{f}.jsonl').write_text(
                '\n'.join(_jsonl_line(i) for i in range(records)) + '\n', encoding='utf-8')

    def test_spillover_is_bounded_not_multimegabyte(self, tmp_path):
        self._build(tmp_path)
        om = _make_om(tmp_path)

        with patch('agent_cascade.settings.GREP_MAX_LINE_CHARS', 120):
            out = om.grep('materialization test', 'logs', char_limit=2000)

        # Every rendered entry is capped, so the full spillover is a few KB, not MBs.
        spill = next((tmp_path / 'logs' / 'spillover').glob('*.txt'), None)
        assert spill is not None, out
        assert spill.stat().st_size < 200_000, spill.stat().st_size
        assert 'line abbreviated:' in spill.read_text(encoding='utf-8')

        # The visible response stays small and explains why/how to re-target.
        assert len(out) < 4000, len(out)
        assert 'line abbreviated:' in out, out
        assert 'count_only=true' in out, out
        assert 'read_logs' in out or 'read_file' in out, out
        # A per-line raw remainder is reported (fixture lines are ~2 KB).
        assert 'chars]' in out, out

    def test_count_only_is_compact_and_counts_records(self, tmp_path):
        self._build(tmp_path)
        om = _make_om(tmp_path)

        out = om.grep('materialization test', 'logs', count_only=True)

        assert out.startswith('Count-only results'), out
        assert '[total: 60 matching lines in 3 file(s)]' in out, out
        assert len(out) < 1000, out

    def test_python_fallback_also_caps_lines(self, tmp_path):
        self._build(tmp_path, files=1, records=5)
        om = _make_om(tmp_path)

        with patch('agent_cascade.settings.GREP_MAX_LINE_CHARS', 100), \
                patch.object(grep_mod, '_check_tool_availability', return_value=(False, False)):
            out = om.grep('materialization test', 'logs')

        assert 'line abbreviated:' in out, out
        assert 'count_only=true' in out, out


class TestPartialResultsOnTimeout:
    @staticmethod
    def _match_event(path: str, line: int, text: str) -> str:
        return json.dumps({'type': 'match', 'data': {
            'path': {'text': path}, 'line_number': line, 'lines': {'text': text + '\n'}}})

    def test_killed_rg_partial_stdout_is_rendered_and_flagged(self, tmp_path):
        """A timeout must not discard matches rg already emitted (and must not re-scan)."""
        (tmp_path / 'a.txt').write_text('needle\n', encoding='utf-8')
        om = _make_om(tmp_path)
        partial = '\n'.join([
            self._match_event('a.txt', 1, 'needle alpha'),
            self._match_event('b.txt', 7, 'needle bravo'),
        ]) + '\n'

        def fake_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5.0, output=partial)

        with patch.object(grep_mod, '_check_tool_availability', return_value=(True, False)), \
                patch('subprocess.run', side_effect=fake_run):
            out = om.grep('needle', '.', timeout=5.0)

        assert out.startswith('Found 2 matches'), out
        assert 'a.txt:1:' in out and 'b.txt:7:' in out, out
        assert 'TIMED OUT after 5s' in out, out
        assert 'search INCOMPLETE' in out, out
        # No second scan: the Python fallback would report the on-disk single match instead.
        assert 'Found 2 matches' in out, out

    def test_timeout_without_partial_stdout_keeps_old_fallback(self, tmp_path):
        (tmp_path / 'a.txt').write_text('nothing\n', encoding='utf-8')
        om = _make_om(tmp_path)

        def fake_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5.0)

        with patch.object(grep_mod, '_check_tool_availability', return_value=(True, False)), \
                patch('subprocess.run', side_effect=fake_run):
            out = om.grep('needle', '.', timeout=5.0)

        # Nothing was recovered, so the diagnostic/fallback path still owns the answer.
        assert 'Found 2 matches' not in out, out
        assert ('too broad' in out) or ('No matches found' in out) or ('timed out' in out.lower()), out


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
