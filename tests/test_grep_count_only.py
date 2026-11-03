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
        """Case 5 (BUG_0202 F3/F4): zero in-scope matches + an ignored (untracked) match.

        The match is ignored AND untracked (the fixture uses a bare .git dir, not a committed
        repo), so F3 recovers nothing and the F4 advisory fires — stating what was consulted
        (ignore rules), never asserting git status. The old "gitignored path(s)" wording is gone.
        """
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
        # F4 (BUG_0202/RC-C): the advisory names the consulted rules, never asserts git status.
        assert 'skipped by ignore rules' in out, out
        assert 'gitignored' not in out, out

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

    def test_tracked_file_found_from_both_roots(self, tmp_path):
        """Case 10 (BUG_0202 F1/F2/F3): a git-TRACKED file that .gitignore would prune is
        recovered from BOTH the repo root (child) and a non-repo parent root — the root-cause
        demonstration. The fixture git-inits a real repo in tmp_path (never the AgentCascade
        tree) so the F2 index query (``git ls-files``) sees the committed file.
        """
        import shutil
        import subprocess

        if not shutil.which('git'):
            pytest.skip('git not available')

        repo = tmp_path / 'repo'
        repo.mkdir()
        (repo / 'tracked.txt').write_text('needle\n', encoding='utf-8')
        (repo / '.gitignore').write_text('tracked.txt\n', encoding='utf-8')

        def git(*args):
            subprocess.run(['git', *args], cwd=str(repo), capture_output=True, text=True,
                           encoding='utf-8', errors='replace')

        git('init')
        # -f: tracked.txt is already matched by the .gitignore rule, so a plain `git add`
        # refuses it; force-add so it lands in the index (the F2 query needs it committed).
        git('add', '-f', 'tracked.txt', '.gitignore')
        git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-m', 'init')
        # A sibling file in the (non-repo) parent so the parent search has an in-scope root.
        (tmp_path / 'parent_marker.txt').write_text('parent\n', encoding='utf-8')
        om = _make_om(tmp_path)

        # Child (repo root): .gitignore prunes the tracked file, F3 recovers it (F2 exemption).
        child_out = om.grep('needle', str(repo), count_only=True)
        assert 'tracked.txt: 1' in child_out, child_out

        # Parent (non-repo): the nested repo's tracked file is recovered (nested-repo parity).
        parent_out = om.grep('needle', str(tmp_path), count_only=True)
        assert 'repo/tracked.txt: 1' in parent_out, parent_out

    def test_truncated_zero_match_discloses_timeout(self, tmp_path):
        """Case 11 (BUG_0202 F5/RC-B): a truncated search that finds nothing must disclose the
        timeout, never read as a conclusive "0 matching lines"."""
        (tmp_path / 'a.txt').write_text('nothing\n', encoding='utf-8')
        om = _make_om(tmp_path)
        # A genuinely partial total is the FALLBACK walk hitting its deadline (fallback_truncated);
        # a fast-path-only timeout whose fallback completed is NOT partial (see case 12).
        out = om._format_count_result(
            pattern='needle', searched_note='', per_file={}, per_pattern={}, total=0,
            alts=[], disclosure=grep_mod._CountDisclosure(fallback_truncated=True),
            skipped_count=0, skipped_paths=[],
            max_mb=100, timeout=5.0)
        assert out.startswith('0 matching lines'), out
        assert 'TIMED OUT' in out, out

    def test_pipeline_truncated_count_only_discloses_timeout(self, tmp_path):
        """Case 12 (BUG_0202 F5): a PIPELINE-level truncated count-only search (rg times out)
        must disclose the caller-deadline timeout through grep(). Case 11 pins the rendering
        via a direct _format_count_result call; this pins the F5 wiring (timed_out propagates
        from the rg path into the zero-match branch)."""
        import subprocess as _sp
        (tmp_path / 'a.txt').write_text('nothing\n', encoding='utf-8')
        om = _make_om(tmp_path)

        def _raise_timeout(*a, **k):
            raise _sp.TimeoutExpired(cmd='rg', timeout=5.0)

        # grep.py imports `subprocess` locally (not module-level), so patch the global
        # subprocess.run that its local `import subprocess` binds to. The rg fast path times out;
        # the (pure-Python) fallback completes the tiny scope, so this is a COMPLETE total — the
        # note must name the fast-path timeout and must NOT claim "partial" (guards finding 1).
        with patch('subprocess.run', side_effect=_raise_timeout):
            out = om.grep('needle', '.', count_only=True, timeout=5.0)

        assert out.startswith('0 matching lines'), out
        assert 'the ripgrep fast path timed out after 5s' in out, out
        assert 'partial' not in out, out  # the fallback finished the scope — not a partial total

    def test_over_cap_truncated_not_reported_as_caller_timeout(self, tmp_path):
        """Case 13 (BUG_0202 F5): a zero-match count-only grep whose over-cap DIAGNOSTIC walk is
        truncated must NOT emit 'TIMED OUT after {timeout}s' — the walk's 0.75 s budget is not the
        caller's deadline. It gets the over-cap partial note instead (guards finding 1)."""
        (tmp_path / 'a.txt').write_text('nothing\n', encoding='utf-8')
        om = _make_om(tmp_path)

        # Force the over-cap diagnostic walk to report itself truncated (its budget ran out)
        # while the caller's deadline was NOT hit.
        def _truncated_walk(self, *a, **k):
            return 1, ['big.bin'], True

        with patch.object(grep_mod.GrepMixin, '_over_cap_rel_paths', _truncated_walk):
            out = om.grep('needle', '.', count_only=True, timeout=30.0)

        assert out.startswith('0 matching lines'), out
        assert 'TIMED OUT after' not in out, out            # caller deadline was not hit
        assert 'over-cap file list is partial' in out, out  # the walk's own-budget note

    def test_whole_repo_ls_files_queried_once(self, tmp_path):
        """Case 14 (BUG_0202 F2): the git index is queried ONCE per repo (whole-repo
        ``git ls-files -z``), not once per tracked-but-ignored file — pins the whole-repo
        caching deviation against a per-directory regression."""
        import shutil
        import subprocess as _sp
        from agent_cascade.operation_manager import grep_ignore as gi_mod

        if not shutil.which('git'):
            pytest.skip('git not available')

        repo = tmp_path / 'repo'
        repo.mkdir()
        n = 5
        for i in range(n):
            (repo / f't{i}.txt').write_text(f'needle {i}\n', encoding='utf-8')
        # A nested subdir with its own tracked file: a (repo_root, matched_dir) keying regression
        # would query the index once per matched directory (repo/ AND repo/sub), so pinning the
        # count to exactly 1 proves whole-repo (repo_root-only) keying.
        (repo / 'sub').mkdir()
        (repo / 'sub' / 'nested.txt').write_text('needle nested\n', encoding='utf-8')
        (repo / '.gitignore').write_text('t*.txt\nsub/\n', encoding='utf-8')

        def git(*args):
            _sp.run(['git', *args], cwd=str(repo), capture_output=True, text=True)

        git('init')
        git('add', '-f', *[f't{i}.txt' for i in range(n)], 'sub/nested.txt', '.gitignore')
        git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-m', 'init')

        om = _make_om(repo)
        real_run = gi_mod.subprocess.run
        ls_calls: list = []

        def _spy_run(cmd, *a, **k):
            if cmd[:3] == ['git', 'ls-files', '-z']:
                ls_calls.append(cmd)
            return real_run(cmd, *a, **k)

        # BUG_0221 (#7): the over-cap walk and the recovery now share ONE resolver, so this
        # spies on the real path with no patch-out. A second resolver would query the index a
        # second time and fail the exactly-once assertion below.
        with patch.object(gi_mod.subprocess, 'run', _spy_run):
            out = om.grep('needle', str(repo), count_only=True)

        # F2: the whole-repo index is queried EXACTLY ONCE (keyed by repo_root only). A
        # (repo_root, matched_dir) keying regression would query once per matched directory
        # (repo/ for t*.txt, repo/sub for sub/) = 2 here, so this pins whole-repo keying.
        assert len(ls_calls) == 1, \
            f'whole-repo caching: expected exactly 1 query (repo_root key), got {len(ls_calls)}'
        assert ls_calls[0] == ['git', 'ls-files', '-z'], \
            f'whole-repo query must carry NO dir argument; got {ls_calls[0]}'
        assert 't0.txt: 1' in out, out            # root-level tracked files recovered (F2)
        assert 'sub/nested.txt: 1' in out, out    # nested tracked file recovered (F2)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
