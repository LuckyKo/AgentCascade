#!/usr/bin/env python3
"""BUG_0195 regression tests: read_logs must expose physical file lines.

read_logs historically numbered only the entries it parsed, so a citation
"line N" from its output silently drifted from the raw file line whenever
blank lines (or non-entry prose) existed. The fix:

  - every JSONL/mixed entry records its physical 1-based file line;
  - simple mode shows both coordinates (``⟨E <entry> | L <line>⟩``);
  - raw mode prefixes ``L<line>:`` (or ``E<entry>:`` when no mapping exists);
  - the header says ``entries`` and appends ``file lines A-B``;
  - ``by_line=true`` interprets ``range`` as physical file lines.

Run: python -m pytest tests/test_read_logs_bug0195.py -v -p no:xdist -o addopts=""
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


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


# A mixed prose + JSONL file: entry indices are offset from file lines by the
# two blank lines and the prose line. Physical layout:
#   1: "# Todo"
#   2: (blank)
#   3: {"role": "user", "content": "first"}
#   4: (blank)
#   5: {"role": "assistant", "content": "second"}
MIXED = (
    '# Todo\n'
    '\n'
    '{"role": "user", "content": "first"}\n'
    '\n'
    '{"role": "assistant", "content": "second"}\n'
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding='utf-8')
    return p


def test_simple_labels_show_entry_and_physical_line():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), 'mixed.md', MIXED)
        result = _read(p, format='simple')

        # Entry 1 is the prose line at file line 1; entry 3 is at file line 5.
        assert '⟨E 1 | L 1⟩' in result, result
        assert '⟨E 3 | L 5⟩' in result, result
        # Header uses entry coordinates and appends the physical window.
        header = result.split('\n')[0]
        assert 'entries 1-3/3' in header, header
        assert 'file lines 1-5' in header, header


def test_raw_prefix_uses_physical_file_line():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), 'mixed.md', MIXED)
        result = _read(p, format='raw')

        body = result.split('```')[1].strip().split('\n')
        assert body[0].startswith('L1: '), body
        assert body[1].startswith('L3: '), body
        assert body[2].startswith('L5: '), body
        # The physical prefix matches the actual source line.
        assert json.loads(body[1].split(': ', 1)[1])['content'] == 'first'


def test_by_line_selects_physical_lines_not_entry_indices():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), 'mixed.md', MIXED)
        # File line 5 holds entry 3 ("second"); it must not be read as entry 5
        # (which does not exist) or as entry 5's neighbourhood.
        result = _read(p, format='simple', range='5:5', by_line=True)

        assert 'second' in result, result
        assert 'first' not in result, result
        header = result.split('\n')[0]
        assert 'entries 3-3/3' in header, header
        assert 'file lines 5-5' in header, header


def test_by_line_range_covers_multiple_physical_lines():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), 'mixed.md', MIXED)
        result = _read(p, format='simple', range='3:5', by_line=True)

        assert 'first' in result, result
        assert 'second' in result, result
        assert 'Todo' not in result, result
        header = result.split('\n')[0]
        assert 'entries 2-3/3' in header, header


def test_by_line_requires_explicit_range():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), 'mixed.md', MIXED)
        result = _read(p, format='simple', by_line=True)
        assert result.startswith('Error'), result
        assert 'requires an explicit' in result, result


def test_by_line_rejected_for_json_array_without_line_mapping():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), 'array.json',
                   json.dumps([{'role': 'user', 'content': 'a'},
                               {'role': 'user', 'content': 'b'}]))
        result = _read(p, format='simple', range='1:2', by_line=True)
        assert result.startswith('Error'), result
        assert 'JSON array' in result, result


def test_json_array_header_omits_file_lines_and_uses_entry_prefix():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), 'array.json',
                   json.dumps([{'role': 'user', 'content': 'a'},
                               {'role': 'user', 'content': 'b'}]))
        result = _read(p, format='raw')

        header = result.split('\n')[0]
        assert 'entries 1-2/2' in header, header
        assert 'file lines' not in header, header
        body = result.split('```')[1].strip().split('\n')
        assert body[0].startswith('E1: '), body
        assert body[1].startswith('E2: '), body


def test_entry_range_remains_entry_based():
    """Positive control: without by_line, range is still entry-based."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), 'mixed.md', MIXED)
        result = _read(p, format='simple', range='2:2')

        assert 'first' in result, result
        assert 'second' not in result, result
        header = result.split('\n')[0]
        assert 'entries 2-2/3' in header, header
        assert 'file lines 3-3' in header, header


def test_single_json_object_maps_to_line_one():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), 'single.json', json.dumps({'role': 'user', 'content': 'only'}))
        result = _read(p, format='simple')

        assert '⟨E 1 | L 1⟩' in result, result
        header = result.split('\n')[0]
        assert 'entries 1-1/1' in header, header
        assert 'file lines 1-1' in header, header
