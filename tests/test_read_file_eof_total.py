"""# BUG_0059 regression tests"""
"""BUG_0059 regression tests — read_file header reports total line count at EOF.

Root cause: the tail-of-file branch in _read_text_file set
    total_lines = len(lines_read)
when the read reached EOF, so the header's /N slot showed the *returned* line
count (e.g. 14) instead of the file's total (e.g. 266).

Fix under test:
- total_lines is now set to line_num (the file's last line number) at EOF.
- An (EOF) marker is appended to the header when the read reaches end-of-file.

All tests drive the public ReadFile.call() so they exercise the real path.
"""
import re
from pathlib import Path

import pytest

from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.tools.custom.file_ops import ReadFile


def _ws_file(name: str, content: str):
    """Write *content* under DEFAULT_WORKSPACE (the only root the standalone resolver allows)."""
    p = Path(DEFAULT_WORKSPACE) / name
    p.write_text(content, encoding='utf-8')
    return p


def _extract_total(header: str) -> int:
    """Extract the /N total from a read_file header line."""
    m = re.search(r'/(\d+)', header)
    assert m, f"no /N found in header: {header!r}"
    return int(m.group(1))


@pytest.fixture(scope='module')
def file_266_lines():
    """A 266-line file for the BUG_0059 regression tests."""
    lines = [f"LINE_{i:04d}" for i in range(1, 267)]
    return _ws_file('bug0059_266lines.txt', '\n'.join(lines) + '\n')


def test_eof_header_reports_total(file_266_lines):
    """T1: reading to EOF (start_line=252, limit=14) must show /266, not /14."""
    out = ReadFile().call({'path': str(file_266_lines), 'start_line': 252, 'limit': 14})
    assert not out.startswith('ERROR'), out
    header = out.split('\n')[0]
    total = _extract_total(header)
    assert total == 266, f"header shows /{total}, expected /266"
    # Content must be correct: lines 252-265
    assert 'LINE_0252' in out
    assert 'LINE_0265' in out
    assert 'LINE_0266' not in out


def test_eof_header_has_eof_marker(file_266_lines):
    """T2: the EOF case must carry a distinct (EOF) marker in the header."""
    out = ReadFile().call({'path': str(file_266_lines), 'start_line': 252, 'limit': 14})
    assert not out.startswith('ERROR'), out
    header = out.split('\n')[0]
    assert '(EOF)' in header, f"missing (EOF) marker in header: {header!r}"


def test_mid_file_header_unchanged(file_266_lines):
    """T3: mid-file read (start_line=70, limit=30) must show /266 with NO (EOF) marker."""
    out = ReadFile().call({'path': str(file_266_lines), 'start_line': 70, 'limit': 30})
    assert not out.startswith('ERROR'), out
    header = out.split('\n')[0]
    total = _extract_total(header)
    assert total == 266, f"header shows /{total}, expected /266"
    assert '(EOF)' not in header, f"unexpected (EOF) in mid-file header: {header!r}"
    # Content check
    assert 'LINE_0070' in out
    assert 'LINE_0099' in out
    assert 'LINE_0100' not in out


def test_eof_wild_read_small_file(tmp_path):
    """T4: a wild read (no limit) of a small file reaches EOF naturally.

    The header must show the correct total and the (EOF) marker.
    Uses tmp_path with a fake agent_pool to avoid DEFAULT_WORKSPACE collisions.
    """
    p = tmp_path / 'bug0059_wild.txt'
    p.write_text('\n'.join(f"wild_line_{i}" for i in range(1, 11)) + '\n', encoding='utf-8')

    class _FakeOM:
        def _resolve_path(self, path, mode='ro'):
            return Path(path).resolve()

    class _FakePool:
        operation_manager = _FakeOM()

    out = ReadFile(agent_pool=_FakePool()).call({'path': str(p), 'start_line': 1})
    assert not out.startswith('ERROR'), out
    header = out.split('\n')[0]
    total = _extract_total(header)
    assert total == 10, f"wild-read header shows /{total}, expected /10"
    assert '(EOF)' in header, f"missing (EOF) marker in wild-read header: {header!r}"


def test_content_unchanged_at_eof(file_266_lines):
    """T5: the content returned at EOF is identical to a full read of the same range.

    The fix must not alter the content — only the header string.
    """
    out_eof = ReadFile().call({'path': str(file_266_lines), 'start_line': 252, 'limit': 14})
    # Extract content (everything after the first ``` fence)
    content_match = re.search(r'```\n(.*)```', out_eof, re.DOTALL)
    assert content_match, 'no content fence found'
    content = content_match.group(1)
    # Verify the 14 lines are correct
    lines = content.strip().split('\n')
    assert len(lines) == 14
    for i, line in enumerate(lines):
        expected_num = 252 + i
        assert f"{expected_num}:\t" in line, f"line {i} missing prefix {expected_num}:"
        assert f"LINE_{expected_num:04d}" in line
