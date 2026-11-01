"""BUG_0190: grep count_only mode — per-file matching-line counts (not the matched lines).

``count_only=True`` returns a per-file count breakdown (plus a per-alternative breakdown for
top-level ``|`` patterns) instead of materializing the matched lines. Counts are matching
LINES (submatches are not counted) and totals are computed before any display cap. Over-cap
files are skipped and named with the shared ``[search INCOMPLETE ...]`` note (the BUG_0185
integrity link), so a count over a partially-searched scope is never read as a full-scope total.

Modelled on ``test_grep_large_files.py``: real ripgrep (a hard dependency of the fast path),
``tmp_path`` fixtures, ``_make_om`` / ``_patch_cap`` helpers.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager import grep as grep_mod

HEADER = b'{"num_hidden_layers": 48, "hidden_size": 5120}\n'
PAYLOAD = b'x' * (2 * 1024 * 1024)


def _make_om(root: Path) -> OperationManager:
    return OperationManager(base_dir=str(root))


def _patch_cap():
    """Shrink the cap to 1 MB / 64 KiB header so a 2 MB fixture is over-size."""
    return patch.multiple('agent_cascade.settings',
                          GREP_MAX_FILE_SIZE_MB=1,
                          GREP_LARGE_FILE_HEAD_KB=64,
                          GREP_LARGE_FILE_SCAN_LIMIT=50,
                          GREP_LARGE_FILE_SCAN_BUDGET_S=5.0)


def _line_mode_total(out: str) -> int:
    m = re.search(r'Found (\d+) matches', out)
    assert m, f'line-mode header missing in: {out!r}'
    return int(m.group(1))


class TestCounts:
    def test_per_file_counts_match_line_mode_totals(self, tmp_path):
        """Case 1: count-mode totals equal line-mode totals (char_limit=-1 avoids truncation)."""
        (tmp_path / 'a.txt').write_text('foo\nbar\nfoo\n', encoding='utf-8')   # 2 'foo'
        (tmp_path / 'b.txt').write_text('foo\nfoo\nfoo\n', encoding='utf-8')  # 3 'foo'
        om = _make_om(tmp_path)

        line_out = om.grep('foo', '.', include='*.txt', char_limit=-1)
        count_out = om.grep('foo', '.', include='*.txt', count_only=True)

        assert _line_mode_total(line_out) == 5
        assert count_out.startswith('Count-only results'), count_out
        assert 'a.txt: 2' in count_out
        assert 'b.txt: 3' in count_out
        assert '[total: 5 matching lines in 2 file(s)]' in count_out

    def test_per_alternative_breakdown(self, tmp_path):
        """Case 2: a line matching both alternatives is counted for both (sum >= total)."""
        (tmp_path / 'c.txt').write_text('foo bar\nfoo\nbar\n', encoding='utf-8')
        om = _make_om(tmp_path)
        out = om.grep('foo|bar', '.', include='*.txt', count_only=True)

        # 3 matching lines; foo hits lines 1,2 (2); bar hits lines 1,3 (2); sum 4 >= 3.
        assert '[total: 3 matching lines in 1 file(s)]' in out, out
        assert "[per-pattern: 'foo': 2, 'bar': 2 across 3 matching lines]" in out, out

    def test_include_exclude_honoured_in_count_mode(self, tmp_path):
        """Case 3: include/exclude filters apply to counts (an excluded file must not over-count)."""
        (tmp_path / 'keep.txt').write_text('needle\nneedle\n', encoding='utf-8')
        (tmp_path / 'skip.log').write_text('needle\nneedle\nneedle\n', encoding='utf-8')
        om = _make_om(tmp_path)
        out = om.grep('needle', '.', include='*.txt', count_only=True)

        assert 'keep.txt: 2' in out, out
        assert 'skip.log' not in out, out
        assert '[total: 2 matching lines in 1 file(s)]' in out, out

    def test_over_cap_file_reports_incomplete(self, tmp_path):
        """Case 4 (BUG_0185<->BUG_0190 link): an over-cap file is excluded and named."""
        (tmp_path / 'model.ninfer').write_bytes(HEADER + PAYLOAD)
        (tmp_path / 'small.txt').write_text('needle\n', encoding='utf-8')
        om = _make_om(tmp_path)

        with _patch_cap():
            out = om.grep('needle', '.', count_only=True)

        assert 'search INCOMPLETE' in out, out
        assert 'model.ninfer' in out, out
        assert '> 1 MB' in out, out

    def test_zero_match_with_gitignored_probe_note(self, tmp_path):
        """Case 5: zero in-scope matches + a gitignored match surfaces the BUG_0061 probe note."""
        # A .git dir (with HEAD) anchors the repo so rg applies the .gitignore rules.
        (tmp_path / '.git').mkdir()
        (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
        (tmp_path / '.gitignore').write_text('ignored/\n', encoding='utf-8')
        (tmp_path / 'ignored').mkdir()
        (tmp_path / 'ignored' / 'secret.txt').write_text('needle\n', encoding='utf-8')
        (tmp_path / 'visible.txt').write_text('nothing\n', encoding='utf-8')
        om = _make_om(tmp_path)

        out = om.grep('needle', '.', count_only=True)  # ignore_vcs=True by default

        assert out.startswith('0 matching lines'), out
        assert 'gitignored' in out, out

    def test_single_file_count_only(self, tmp_path):
        """Case 6: count_only on a single-file path goes through _grep_single_file."""
        f = tmp_path / 'one.txt'
        f.write_text('x\ny\nx\n', encoding='utf-8')
        om = _make_om(tmp_path)

        out = om.grep('x', str(f), count_only=True)

        assert 'one.txt: 2' in out, out
        assert '[total: 2 matching lines in 1 file(s)]' in out, out

    def test_python_fallback_parity(self, tmp_path):
        """Case 7: with rg unavailable the Python fallback yields the same counts."""
        (tmp_path / 'a.txt').write_text('foo\nfoo\n', encoding='utf-8')
        (tmp_path / 'b.txt').write_text('foo\n', encoding='utf-8')
        om = _make_om(tmp_path)

        rg_out = om.grep('foo', '.', include='*.txt', count_only=True)
        with patch.object(grep_mod, '_check_tool_availability', return_value=(False, False)):
            py_out = om.grep('foo', '.', include='*.txt', count_only=True)

        assert rg_out == py_out, f'\nrg={rg_out!r}\npy={py_out!r}'

    def test_tool_wrapper_accepts_count_only(self, tmp_path):
        """Case 8: the Grep tool schema accepts count_only and forwards it to operation_manager.grep."""
        class _FakeOM:
            def __init__(self):
                self.last_kwargs = None

            def grep(self, pattern, path, include, **kwargs):
                self.last_kwargs = kwargs
                return f"MOCK: count_only={kwargs.get('count_only')}"

        class _FakePool:
            def __init__(self):
                self.operation_manager = _FakeOM()
                self.llm_cfg = {}

        from agent_cascade.tools.custom.file_ops import Grep

        pool = _FakePool()
        tool = Grep(cfg={}, agent_pool=pool)
        out = tool.call(json.dumps({'pattern': 'x', 'count_only': True}))

        assert pool.operation_manager.last_kwargs.get('count_only') is True
        assert 'count_only=True' in out

    def test_count_mode_fallback_over_cap_incomplete(self, tmp_path):
        """Case 9 (BUG_0185 hardening link): a forced Python fallback over over-cap files
        still reports the shared [search INCOMPLETE ...] note, never a bare total."""
        (tmp_path / 'a.ninfer').write_bytes(b'header\n' + PAYLOAD)
        (tmp_path / 'b.ninfer').write_bytes(b'header\n' + PAYLOAD)
        om = _make_om(tmp_path)

        with _patch_cap(), patch.object(grep_mod, '_check_tool_availability',
                                        return_value=(False, False)):
            out = om.grep('needle', '.', include='*.ninfer', count_only=True)

        assert 'search INCOMPLETE' in out, out
        assert '2 file(s) > 1 MB' in out, out


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
