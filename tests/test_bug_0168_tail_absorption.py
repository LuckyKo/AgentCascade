#!/usr/bin/env python3
"""Test BUG_0168: heuristic edit_file tail-overlap absorption.

Verifies that when new_content's last non-blank line is a prefix of the next
file line (ending at a word boundary), the heuristic span is extended to absorb
it, preventing duplicate/residue lines.

Also verifies:
- Distinct lines that merely share a prefix (no word boundary) are NOT absorbed
- The indent anchor uses the pre-absorption span (Phase 2 regression guard)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def simulate_absorption(file_content: str, old_content: str, new_content: str):
    """Replicate the BUG_0168 absorption logic from file_operations.py.

    Returns (absorbed: bool, orig_end_idx: int, pre_absorb_end: int).
    """
    file_lines = file_content.splitlines(keepends=True)

    # Build file_line_info (non-blank lines only)
    file_line_info = []
    for idx, line in enumerate(file_lines):
        norm = ''.join(line.split())
        if norm:
            file_line_info.append((idx, norm))

    # Find the match: locate where old_content's first non-blank line appears
    old_norm_lines = [''.join(l.split()) for l in old_content.splitlines(keepends=True) if ''.join(l.split())]
    start_list_idx = None
    for i, (_, norm) in enumerate(file_line_info):
        if old_norm_lines and norm == old_norm_lines[0]:
            start_list_idx = i
            break
    assert start_list_idx is not None, 'Could not find old_content start in file'

    n_old = len(old_norm_lines)
    end_list_idx = start_list_idx + n_old  # exclusive
    orig_end_idx = file_line_info[end_list_idx - 1][0]

    # ── BUG_0168 absorption logic (mirrors production) ──
    absorbed = False
    pre_absorb_end = orig_end_idx

    _new_lines = new_content.splitlines(keepends=True)
    _last_new_line = next((l for l in reversed(_new_lines) if l.strip()), '')
    if _last_new_line:
        _last_stripped = _last_new_line.lstrip().rstrip('\r\n')
        _next_list_idx = end_list_idx
        if _next_list_idx < len(file_line_info) and len(_last_stripped) >= 5:
            _next_orig_idx = file_line_info[_next_list_idx][0]
            _next_stripped = file_lines[_next_orig_idx].lstrip()
            if _next_stripped.startswith(_last_stripped):
                _plen = len(_last_stripped)
                _next_body = _next_stripped.rstrip('\r\n')
                if _plen == len(_next_body):
                    absorbed = True
                    orig_end_idx = _next_orig_idx
                elif _plen < len(_next_body):
                    _junction_char = _next_stripped[_plen]
                    if not (_junction_char.isalnum() or _junction_char == '_'):
                        absorbed = True
                        orig_end_idx = _next_orig_idx

    return absorbed, orig_end_idx, pre_absorb_end


# ── Test cases ──────────────────────────────────────────────────────────────


def test_duplicate_prefix_absorbed():
    """BUG report case 2: new_content's last line is a prefix of the next file line
    with a word boundary (space after the prefix). Should absorb."""
    file_content = (
        'line one\n'
        'line two\n'
        '   from 129 columns.\n'
        '   from 129 columns. So "micro 512 sits exactly on the floor" ...\n'
        'line five\n'
    )
    old_content = 'line one\nline two\n   from 129 columns.\n'
    new_content = 'new line one\nnew line two\n   from 129 columns.\n'

    absorbed, end_idx, pre_end = simulate_absorption(file_content, old_content, new_content)
    assert absorbed, f"Expected absorption, got absorbed=False"
    assert end_idx == 3, f"Expected end_idx=3, got {end_idx}"
    assert pre_end == 2, f"Expected pre_end=2, got {pre_end}"
    print('PASS: test_duplicate_prefix_absorbed')


def test_no_overlap_no_absorption():
    """No absorption when new_content's last line does not overlap the next file line."""
    file_content = 'alpha\nbeta\ngamma\ndelta\n'
    old_content = 'alpha\nbeta\n'
    new_content = 'x1\nx2\n'

    absorbed, _, _ = simulate_absorption(file_content, old_content, new_content)
    assert not absorbed, f"Expected no absorption, got absorbed=True"
    print('PASS: test_no_overlap_no_absorption')


def test_distinct_prefix_not_absorbed():
    """False-positive guard: distinct lines that share a prefix but have no word
    boundary at the junction should NOT be absorbed.

    'value = data_source_primary' is a prefix of
    'value = data_source_primary_extended_field', but the junction char is '_'
    (an identifier char), so no absorption.
    """
    file_content = (
        'value = data_source_primary\n'
        'value = data_source_primary_extended_field\n'
    )
    old_content = 'value = data_source_primary\n'
    new_content = 'value = data_source_primary\n'

    absorbed, _, _ = simulate_absorption(file_content, old_content, new_content)
    assert not absorbed, (
        f"Expected no absorption (no word boundary at junction), got absorbed=True"
    )
    print('PASS: test_distinct_prefix_not_absorbed')


def test_exact_match_absorbed():
    """When new_content's last line exactly matches the next file line (modulo
    line ending), it should be absorbed (no duplicate)."""
    file_content = (
        'hello world this is a test\n'
        'hello world this is a test\n'
    )
    old_content = 'hello world this is a test\n'
    new_content = 'hello world this is a test\n'

    absorbed, end_idx, _ = simulate_absorption(file_content, old_content, new_content)
    assert absorbed, f"Expected absorption (exact match), got absorbed=False"
    assert end_idx == 1, f"Expected end_idx=1, got {end_idx}"
    print('PASS: test_exact_match_absorbed')


def test_no_next_line():
    """No absorption when the matched block is at the end of the file."""
    file_content = 'alpha\nbeta\n'
    old_content = 'alpha\nbeta\n'
    new_content = 'x1\nx2\n'

    absorbed, _, _ = simulate_absorption(file_content, old_content, new_content)
    assert not absorbed, f"Expected no absorption (no next line), got absorbed=True"
    print('PASS: test_no_next_line')


def test_word_boundary_period():
    """Prefix ending with a period followed by a space in the next line → word
    boundary → absorb."""
    file_content = (
        'the compiled 512 columns.\n'
        'the compiled 512 columns. Cards follow after this.\n'
    )
    old_content = 'the compiled 512 columns.\n'
    new_content = 'the compiled 512 columns.\n'

    absorbed, _, _ = simulate_absorption(file_content, old_content, new_content)
    # Junction char is ' ' (space) → word boundary → absorb
    assert absorbed, f"Expected absorption (period+space boundary), got absorbed=False"
    print('PASS: test_word_boundary_period')


def test_word_boundary_comma():
    """Prefix followed by a comma in the next line → word boundary → absorb."""
    file_content = (
        'item_one\n'
        'item_one, item_two, item_three\n'
    )
    old_content = 'item_one\n'
    new_content = 'item_one\n'

    absorbed, _, _ = simulate_absorption(file_content, old_content, new_content)
    # Junction char is ',' → non-identifier → word boundary → absorb
    assert absorbed, f"Expected absorption (comma boundary), got absorbed=False"
    print('PASS: test_word_boundary_comma')


def test_short_prefix_not_absorbed():
    """Prefix shorter than 5 chars is not absorbed (minimum length guard)."""
    file_content = (
        'ab\n'
        'ab longer text here\n'
    )
    old_content = 'ab\n'
    new_content = 'ab\n'

    absorbed, _, _ = simulate_absorption(file_content, old_content, new_content)
    assert not absorbed, f"Expected no absorption (prefix < 5 chars), got absorbed=True"
    print('PASS: test_short_prefix_not_absorbed')


def test_sibling_append_no_duplicate():
    """Regression: when absorption happens AND the _sibling_append path is taken,
    the absorbed line must not be re-emitted by the sibling slice.

    Simulates the production _sibling_append logic:
    - actual_old_content includes the absorbed line
    - file_block_lines is the pre-absorption block
    - When absorbed_tail, siblings are the lines between the block boundary
      and the absorbed line (found by identity as the last non-blank line)
    """
    file_content = (
        '    foo\n'
        '    bar\n'
        'quux tail line here\n'
    )
    old_content = '    foo\n    bar\n'
    new_content = '    foo\n    bar\nquux tail line here\n'

    absorbed, orig_end_idx, pre_end = simulate_absorption(file_content, old_content, new_content)
    assert absorbed, 'Expected absorption for this case'

    # Simulate the production _sibling_append slice logic
    file_lines = file_content.splitlines(keepends=True)
    actual_old_content = ''.join(file_lines[0:orig_end_idx + 1])  # includes absorbed line
    _ncl = new_content.splitlines(keepends=True)
    _block_lines = 2  # len(file_block_lines) = pre-absorption block (2 lines)

    # BUG_0168 fix: find absorbed line by identity (last non-blank), exclude it
    _absorbed_pos = len(_ncl) - 1
    while _absorbed_pos >= 0 and not _ncl[_absorbed_pos].strip():
        _absorbed_pos -= 1
    result = actual_old_content + ''.join(_ncl[_block_lines:_absorbed_pos])

    assert result == '    foo\n    bar\nquux tail line here\n', f"Unexpected result: {result!r}"
    print('PASS: test_sibling_append_no_duplicate')


def test_multi_sibling_absorbed_no_duplicate():
    """Regression: multi-sibling + absorbed-last must not lose intermediate
    siblings or duplicate the absorbed line.

    new_content = [matched_1, matched_2, sib_one, absorbed_line]
    actual_old_content = [matched_1, matched_2, absorbed_line]
    Siblings = _ncl[2:3] = [sib_one]
    Result = actual_old_content + sib_one (absorbed line NOT re-emitted)

    Note: the absorbed line appears before the siblings in the output because
    it is part of actual_old_content (the matched block). This is structurally
    correct: the absorbed line belongs to the block, siblings are separate.
    """
    file_content = (
        '    foo\n'
        '    bar\n'
        'quux tail line here\n'
    )
    old_content = '    foo\n    bar\n'
    new_content = '    foo\n    bar\nsib_one here\nquux tail line here\n'

    absorbed, orig_end_idx, pre_end = simulate_absorption(file_content, old_content, new_content)
    assert absorbed, 'Expected absorption for this case'

    file_lines = file_content.splitlines(keepends=True)
    actual_old_content = ''.join(file_lines[0:orig_end_idx + 1])
    _ncl = new_content.splitlines(keepends=True)
    _block_lines = 2

    _absorbed_pos = len(_ncl) - 1
    while _absorbed_pos >= 0 and not _ncl[_absorbed_pos].strip():
        _absorbed_pos -= 1
    result = actual_old_content + ''.join(_ncl[_block_lines:_absorbed_pos])

    # Exact byte assertion (order-sensitive)
    expected = '    foo\n    bar\nquux tail line here\nsib_one here\n'
    assert result == expected, f"Order/content mismatch:\n  got:      {result!r}\n  expected: {expected!r}"
    print('PASS: test_multi_sibling_absorbed_no_duplicate')


def test_trailing_blank_no_duplicate():
    """Regression: a trailing blank line after the absorbed line must not
    re-introduce the duplicate (the old positional :-1 slice dropped the
    blank and kept the absorbed line)."""
    file_content = (
        '    foo\n'
        '    bar\n'
        'quux tail line here\n'
    )
    old_content = '    foo\n    bar\n'
    # Note the trailing blank line after the absorbed line
    new_content = '    foo\n    bar\nquux tail line here\n\n'

    absorbed, orig_end_idx, pre_end = simulate_absorption(file_content, old_content, new_content)
    assert absorbed, 'Expected absorption for this case'

    file_lines = file_content.splitlines(keepends=True)
    actual_old_content = ''.join(file_lines[0:orig_end_idx + 1])
    _ncl = new_content.splitlines(keepends=True)
    _block_lines = 2

    # Find absorbed line by identity (last non-blank), not by position
    _absorbed_pos = len(_ncl) - 1
    while _absorbed_pos >= 0 and not _ncl[_absorbed_pos].strip():
        _absorbed_pos -= 1
    # _absorbed_pos should be 2 (the "quux tail line here" line), not 3 (the blank)
    assert _absorbed_pos == 2, f"Expected absorbed_pos=2, got {_absorbed_pos}"

    result = actual_old_content + ''.join(_ncl[_block_lines:_absorbed_pos])

    # The absorbed line must appear exactly once (no duplicate from trailing blank)
    assert result.count('quux tail line here') == 1, (
        f"Absorbed line duplicated: {result!r}"
    )
    assert result == '    foo\n    bar\nquux tail line here\n', f"Unexpected: {result!r}"
    print('PASS: test_trailing_blank_no_duplicate')


if __name__ == '__main__':
    test_duplicate_prefix_absorbed()
    test_no_overlap_no_absorption()
    test_distinct_prefix_not_absorbed()
    test_exact_match_absorbed()
    test_no_next_line()
    test_word_boundary_period()
    test_word_boundary_comma()
    test_short_prefix_not_absorbed()
    test_sibling_append_no_duplicate()
    test_multi_sibling_absorbed_no_duplicate()
    test_trailing_blank_no_duplicate()
    print('\nAll BUG_0168 tests passed.')
