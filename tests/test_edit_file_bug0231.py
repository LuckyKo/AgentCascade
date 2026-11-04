#!/usr/bin/env python3
"""BUG_0231 regression tests: exact-match miss must describe the nearest
candidate, not compare old_content line 1 against file line 1.

The BUG_0186 hint walked both line lists at the same index, so any failing
block below line 1 reported "first diff at line 1" against the file header.
These tests pin the three cases that matter:

  (a) a block deep in the file, differing by wording;
  (b) a block deep in the file, differing only by a transcribed tab prefix;
  (c) no shared line at all → "not found anywhere", no line-1 comparison.

Run: python -m pytest tests/test_edit_file_bug0231.py -v -p no:xdist -o addopts=""
"""

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _make_om(tmpdir):
    from agent_cascade.operation_manager import OperationManager
    return OperationManager(base_dir=str(tmpdir))


def _owned_file(om, tmpdir, name, content, agent='coder'):
    f = Path(tmpdir, name)
    f.write_text(content, encoding='utf-8')
    om._own(f.resolve(), agent)
    return f


def _long_file(target_line, first_line, second_line, total=52):
    """A file whose interesting block starts at 1-based ``target_line``."""
    lines = [f'filler line {i}' for i in range(1, total + 1)]
    lines[target_line - 1] = first_line
    lines[target_line] = second_line
    return '\n'.join(lines) + '\n'


def test_wording_diff_below_line_one_names_candidate_region():
    """The block is at line 30; the hint must say line 30, not line 1."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        file_content = _long_file(
            30,
            '- (c) save covers 67 (to_dict) + 12 explicit',
            '  continued',
        )
        f = _owned_file(om, d, 'audit.md', file_content)
        # Differ only in wording on the block's first line.
        old_content = ('- (c) save covers 67 (to_dict) + 12 explicit '
                       '`data[...]` writes\n  continued\n')

        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content='X', match_mode='exact')

        assert result.startswith('ERROR'), result
        assert 'first diff at line 30' in result, result
        assert 'first diff at line 1:' not in result, result
        assert 'text differs' in result, result
        assert 'nearest candidate' in result, result


def test_tab_prefix_below_line_one_reports_whitespace():
    """A copied read_file tab prefix must be named as whitespace, not line 1."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        file_content = _long_file(
            12,
            '  - item',       # file indents with 2 spaces
            'next line',
        )
        f = _owned_file(om, d, 'notes.md', file_content)
        # Simulates copying read_file's "NNN:\t" prefix plus the content's
        # own indentation: tab + 2 spaces → 6 spaces after normalization.
        old_content = '\t  - item\nnext line\n'

        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content='X', match_mode='exact')

        assert result.startswith('ERROR'), result
        assert 'first diff at line 1:' not in result, result
        assert 'line 12' in result, result
        assert 'whitespace' in result, result


def test_no_shared_line_says_not_found_anywhere():
    """With zero line overlap, do not fabricate a line-1 comparison."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'other.md', 'alpha\nbeta\ngamma\n')

        result = om.edit_file(str(f), 'coder',
                              old_content='zzz unrelated\nyyy nothing\n',
                              new_content='X', match_mode='exact')

        assert result.startswith('ERROR'), result
        assert 'not found anywhere' in result, result
        assert 'first diff' not in result, result


def test_old_content_past_end_of_file_is_reported():
    """A match region that runs past EOF must say so, with a candidate line."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'short.md', 'a\nb\n')

        result = om.edit_file(str(f), 'coder',
                              old_content='a\nb\nc\n',
                              new_content='X', match_mode='exact')

        assert result.startswith('ERROR'), result
        assert 'runs past the end' in result, result
        assert 'first diff at line 1:' not in result, result


def test_aligned_content_still_reports_the_differing_line():
    """Backwards compatibility: an aligned block still reports its own line."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'aligned.txt', 'alpha\nbeta\ngamma\n')

        result = om.edit_file(str(f), 'coder',
                              old_content='alpha\nBETA\ngamma\n',
                              new_content='X', match_mode='exact')

        assert result.startswith('ERROR'), result
        assert 'first diff at line 2' in result, result
        assert 'text differs' in result, result


def test_helper_classifies_whitespace_and_text_directly():
    """The helper's classifier distinguishes the incident causes."""
    from agent_cascade.operation_manager.file_operations import (
        _classify_line_difference,
    )
    assert 'whitespace' in _classify_line_difference('\tX', '    X')
    assert 'whitespace' in _classify_line_difference('\t  - item', '  - item')
    assert 'indentation' in _classify_line_difference('    X', '  X')
    assert _classify_line_difference('old text', 'new text') == 'text differs'
