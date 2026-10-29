"""BUG_0169: grep must not stream multi-GB binary artifacts as text.

`--text` is required so NUL-byte files are not silently skipped (BUG_0041/BUG_0042), but
combined with a 19 GB model artifact it turns grep into an unbounded scan that times out and
then blames the caller's pattern/scope. The fix caps the streamed file size and recovers
matches from the over-size files' headers, reporting the partial coverage honestly.

These tests use REAL ripgrep (it is a hard dependency of the grep fast path) and a tiny
threshold so a few-MB fixture exercises the same code path as a 19 GB artifact.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager.grep import GrepMixin

HEADER = b'{"num_hidden_layers": 48, "hidden_size": 5120}\n'
TAIL_ONLY = b'needle only in the tail of an over-size file\n'
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


class TestRgArgvCap:
    def test_rg_argv_keeps_text_and_adds_a_size_cap(self):
        cmd = GrepMixin()._build_rg_argv('needle', '*', '', 0, True)
        assert '--text' in cmd, 'BUG_0041: --text must stay or NUL-byte files are skipped'
        assert '--max-filesize=100M' in cmd, \
            f'BUG_0169: the streaming search must cap file size; cmd={cmd}'

    def test_cap_can_be_disabled(self):
        with patch('agent_cascade.settings.GREP_MAX_FILE_SIZE_MB', 0):
            cmd = GrepMixin()._build_rg_argv('needle', '*', '', 0, True)
        assert not any(arg.startswith('--max-filesize') for arg in cmd), \
            f'GREP_MAX_FILE_SIZE_MB=0 must restore the old unbounded behaviour; cmd={cmd}'


class TestLargeFileHeaderScan:
    def test_header_match_in_over_size_artifact_is_found(self, tmp_path):
        # A 19 GB artifact's useful metadata lives in the first KiB, exactly like this.
        (tmp_path / 'model.ninfer').write_bytes(HEADER + PAYLOAD)
        (tmp_path / 'notes.txt').write_text('nothing here\n', encoding='utf-8')

        with _patch_cap():
            out = _make_om(tmp_path).grep('num_hidden_layers', '.', include='*.ninfer')

        assert out.startswith('Found 1 matches'), out
        assert 'model.ninfer' in out
        assert 'larger than 1 MB' in out, 'the result must say the file was not streamed in full'
        assert 'read_file' in out
        assert 'timed out' not in out.lower()

    def test_large_file_without_a_header_match_reports_partial_coverage(self, tmp_path):
        # The pattern exists only after the bounded header window → grep cannot see it, but it
        # must not claim a clean "No matches" over a scope it could not fully read.
        (tmp_path / 'model.ninfer').write_bytes(b'header line\n' + PAYLOAD + TAIL_ONLY)

        with _patch_cap():
            out = _make_om(tmp_path).grep('needle', '.', include='*.ninfer')

        assert out.startswith('No matches found'), out
        assert 'larger than 1 MB' in out
        assert 'use read_file' in out

    def test_include_filter_is_honoured_by_the_header_scan(self, tmp_path):
        # Only the .ninfer candidate is in scope; a same-size .txt must not be header-scanned.
        (tmp_path / 'model.ninfer').write_bytes(b'header\n' + PAYLOAD)
        (tmp_path / 'big.txt').write_bytes(HEADER + PAYLOAD)

        with _patch_cap():
            out = _make_om(tmp_path).grep('num_hidden_layers', '.', include='*.ninfer')

        assert out.startswith('No matches found'), out
        assert 'big.txt' not in out

    def test_small_nul_byte_file_still_matches(self, tmp_path):
        # BUG_0041/BUG_0042 regression guard: the size cap must not resurrect the
        # false-"No matches" behaviour --text was added to prevent.
        (tmp_path / 'small.bin').write_bytes(b'hello\x00world needle\x00tail\n')

        with _patch_cap():
            out = _make_om(tmp_path).grep('needle', '.', include='*.bin')

        assert out.startswith('Found 1 matches'), out
        assert 'small.bin' in out


class TestTimeoutMessage:
    def test_timeout_names_the_over_size_files_instead_of_the_pattern(self, tmp_path):
        (tmp_path / 'a.ninfer').write_bytes(b'header\n' + PAYLOAD)
        (tmp_path / 'b.ninfer').write_bytes(b'header\n' + PAYLOAD)
        om = _make_om(tmp_path)

        # Force the timeout branch deterministically (a real rg timeout needs a huge tree).
        om._try_subprocess_grep = lambda *a, **k: (None, 0, True, False, 0, None, 0, 0)

        with _patch_cap():
            out = om.grep('needle', '.', include='*.ninfer', timeout=5.0)

        assert out.startswith('ERROR: Search timed out'), out
        assert '2 file(s) larger than 1 MB' in out, out
        assert 'read_file' in out
        assert 'too broad' not in out


class TestPythonFallbackGuard:
    def test_fallback_scans_headers_instead_of_reading_the_whole_file(self, tmp_path):
        (tmp_path / 'model.ninfer').write_bytes(HEADER + PAYLOAD)
        om = _make_om(tmp_path)

        with _patch_cap(), patch(
                'agent_cascade.operation_manager.grep._check_tool_availability',
                return_value=(False, False)):
            out = om.grep('num_hidden_layers', '.', include='*.ninfer')

        assert out.startswith('Found 1 matches'), out
        assert 'model.ninfer' in out
        assert 'larger than 1 MB' in out


class TestLargeFileScanHelper:
    def test_helper_counts_over_size_files_without_head_scan(self, tmp_path):
        (tmp_path / 'a.ninfer').write_bytes(PAYLOAD)
        (tmp_path / 'small.txt').write_text('x\n', encoding='utf-8')

        with _patch_cap():
            report = _make_om(tmp_path)._scan_large_files(
                tmp_path, '*.ninfer', '', 'needle', head_scan=False)

        assert report['large_files'] == 1
        assert report['scanned'] == 0
        assert report['total_bytes'] >= 2 * 1024 * 1024

    def test_helper_respects_exclude(self, tmp_path):
        (tmp_path / 'keep.ninfer').write_bytes(b'header\n' + PAYLOAD)
        (tmp_path / 'skip.ninfer').write_bytes(HEADER + PAYLOAD)

        with _patch_cap():
            report = _make_om(tmp_path)._scan_large_files(
                tmp_path, '*.ninfer', 'skip*', 'num_hidden_layers', head_scan=True)

        assert report['count'] == 0
        assert report['large_files'] == 1

    def test_budget_expiry_marks_the_report_partial(self, tmp_path):
        (tmp_path / 'model.ninfer').write_bytes(HEADER + PAYLOAD)

        with _patch_cap(), patch('agent_cascade.settings.GREP_LARGE_FILE_SCAN_BUDGET_S', -1.0):
            report = _make_om(tmp_path)._scan_large_files(
                tmp_path, '*.ninfer', '', 'num_hidden_layers', head_scan=True)

        assert report['partial'] is True
        assert report['count'] == 0


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
