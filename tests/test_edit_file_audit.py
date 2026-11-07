#!/usr/bin/env python3
"""Regression cluster for the edit_file audit (BUG_0250 + latent sibling bugs).

Covers:

  * BUG_0250 — a `delete_and_insert` call whose `new_content` carries the anchor
    line inserts it verbatim, duplicating the surviving line above the range. The
    fix is one advisory for ALL modes: compare adjacent identical-line pair counts
    before/after the edit and note newly introduced duplicates (advisory only — the
    written bytes are never changed).
  * `_parse_range` negative underflow (e.g. '-100:-100') used to clamp a 1-indexed
    start to 0, then return start-1 = -1, which Python interprets as "from the end"
    and silently DUPLICATES the file tail.
  * `range` sent as a JSON number (2 / 0) used to raise AttributeError / be treated
    as "missing"; both are valid specs.
  * `delete_and_insert` used to prepend the inherited indent to blank inserted lines,
    creating whitespace-only lines.
  * Heuristic mode's sibling-append path (BUG_0069) is only valid when new_content
    actually reproduces the matched block; it used to silently discard a plain
    replacement and to turn an indented delete-only edit into a no-op.
  * The BUG_0095 no-op guard now covers every mode (heuristic identity replacement).

Run: python -m pytest tests/test_edit_file_audit.py -q
"""

import tempfile
from pathlib import Path

from agent_cascade.operation_manager import OperationManager


def _make_om(tmpdir):
    om = OperationManager(base_dir=str(tmpdir))
    om.file_ownership = {}
    return om


def _owned_file(om, tmpdir, name, content):
    f = Path(tmpdir, name)
    f.write_text(content, encoding='utf-8')
    om._own(f.resolve(), 'coder')
    return f


def _d_and_i(om, f, new_content, range_param):
    return om.edit_file(str(f), 'coder', old_content='', new_content=new_content,
                        match_mode='delete_and_insert', range_param=range_param)


# ── BUG_0250: anchor text left in new_content creates adjacent duplicates ────


def test_di_append_notes_anchor_duplicate():
    """The range deletes one line; new_content starts with the surviving anchor line.
    The edit still applies verbatim (no silent dedup), but the message must flag the
    newly created adjacent duplicate. Red/green: pre-fix there is no note."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = ('# Doc\n'
                    '\n'
                    'Report the lattice as a table.\n'
                    'old line to delete\n'
                    '\n'
                    '## Tail\n'
                    'body\n')
        f = _owned_file(om, d, 'skill.md', original)
        res = _d_and_i(om, f, new_content='Report the lattice as a table.\n\nAlso: new\n',
                       range_param='4:4')
        assert res.startswith('OK'), res
        assert 'two identical adjacent lines' in res, f"duplicate advisory missing: {res!r}"
        assert 'anchor' in res, f"advisory must name the likely cause: {res!r}"
        assert 'Re-read the file' in res, f"advisory must point at verification: {res!r}"
        # Advisory-only: the duplicate is still written (the tool never dedups).
        assert f.read_text(encoding='utf-8').count('Report the lattice as a table.') == 2


def test_di_normal_append_has_no_duplicate_note():
    """Negative: an append whose first line is new must not emit the advisory."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'plain.md', '# Doc\n\nbody\nold line\n\n## Tail\n')
        res = _d_and_i(om, f, new_content='brand new line\n', range_param='4:4')
        assert res.startswith('OK'), res
        assert 'two identical adjacent lines' not in res, f"false positive: {res!r}"


def test_exact_duplicate_anchor_also_notes():
    """The same mistake is possible in exact mode (anchor repeated twice in
    new_content); the check is mode-agnostic."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'doc.md', 'anchor line here\ntail\n')
        res = om.edit_file(str(f), 'coder',
                           old_content='anchor line here\n',
                           new_content='anchor line here\nanchor line here\n',
                           match_mode='exact')
        assert res.startswith('OK'), res
        assert 'two identical adjacent lines' in res, f"advisory missing: {res!r}"


# ── _parse_range: negative underflow must never wrap to a negative index ──────


def test_di_negative_range_underflow_does_not_duplicate_file():
    """'-100:-100' on a 5-line file clamps to the FIRST line, not to a Python
    negative slice that re-appends the whole file."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'r.txt', 'l1\nl2\nl3\nl4\nl5\n')
        res = _d_and_i(om, f, new_content='X\n', range_param='-100:-100')
        assert res.startswith('OK'), res
        assert f.read_text(encoding='utf-8') == 'X\nl2\nl3\nl4\nl5\n', \
            f"negative underflow corrupted the file: {f.read_text(encoding='utf-8')!r}"


def test_di_negative_range_underflow_start_clamps_to_first_line():
    """'-100:-2' clamps the start to line 1, keeps the end at line 4 (not a wrapped
    negative index), and is not mistaken for the whole-file range."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'r2.txt', 'l1\nl2\nl3\nl4\nl5\n')
        res = _d_and_i(om, f, new_content='X\n', range_param='-100:-2')
        assert res.startswith('OK'), res
        assert f.read_text(encoding='utf-8') == 'X\nl5\n', \
            f"expected lines 1-4 replaced: {f.read_text(encoding='utf-8')!r}"


# ── range sent as a JSON number ──────────────────────────────────────────────


def test_di_range_as_int_inserts():
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'i.txt', 'a\nb\nc\n')
        res = _d_and_i(om, f, new_content='X\n', range_param=2)
        assert res.startswith('OK'), f"int range must be accepted: {res!r}"
        assert f.read_text(encoding='utf-8') == 'a\nX\nb\nc\n'


def test_di_range_zero_as_int_appends():
    """int 0 is the append spec; it used to be rejected as a missing range."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'z.txt', 'a\nb\n')
        res = _d_and_i(om, f, new_content='X\n', range_param=0)
        assert res.startswith('OK'), f"int 0 must mean append: {res!r}"
        assert f.read_text(encoding='utf-8') == 'a\nb\nX\n'


# ── blank inserted lines must not inherit the range's indent ────────────────


def test_di_blank_inserted_line_has_no_inherited_indent():
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'blank.py', 'def f():\n    pass\n')
        res = _d_and_i(om, f, new_content='x = 1\n\nx = 2\n', range_param='2')
        assert res.startswith('OK'), res
        assert f.read_text(encoding='utf-8') == 'def f():\n    x = 1\n\n    x = 2\n    pass\n', \
            f"blank line gained trailing whitespace: {f.read_text(encoding='utf-8')!r}"


# ── heuristic sibling-append path requires a reproduced block ───────────────


def test_heuristic_delete_only_indented_block_actually_deletes():
    """A delete-only heuristic edit on an INDENTED block used to be diverted into
    the sibling-append path (new_base 0 < file_base) and written back unchanged
    while reporting OK."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'del.py',
                        'def f():\n    x = 1\n    y = 2\ndef g():\n    return 3\n')
        res = om.edit_file(str(f), 'coder',
                           old_content='x = 1\n    y = 2', new_content='',
                           match_mode='heuristic')
        assert res.startswith('OK') and 'WARNING' not in res, res
        assert f.read_text(encoding='utf-8') == 'def f():\ndef g():\n    return 3\n', \
            f"delete-only edit did not delete: {f.read_text(encoding='utf-8')!r}"


def test_heuristic_sibling_guard_ignores_non_reproduced_block():
    """A plain re-indenting replacement does not reproduce the matched block, so the
    sibling path must not slice it away (pre-fix: silent no-op reported as OK)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'sib.py',
                        'def f():\n    a = 1\n    b = 2\ndef g():\n    pass\n')
        res = om.edit_file(str(f), 'coder',
                           old_content='a = 1\n    b = 2',
                           new_content='a = 10\n  b = 20',
                           match_mode='heuristic')
        assert res.startswith('OK'), res
        assert 'a = 10' in f.read_text(encoding='utf-8'), \
            f"replacement was silently discarded: {f.read_text(encoding='utf-8')!r}"


def test_heuristic_identity_replacement_is_a_noop_warning():
    """The shared BUG_0095 guard now covers heuristic mode: a byte-identical result
    returns WARNING and creates no backup."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'same.txt', 'a\nb\nc\n')
        res = om.edit_file(str(f), 'coder',
                           old_content='b\n', new_content='b\n',
                           match_mode='heuristic')
        assert res.startswith('WARNING: No-op'), f"expected a no-op warning: {res!r}"
        assert f.read_text(encoding='utf-8') == 'a\nb\nc\n'
        assert not list(Path(d).rglob('*.bak')), 'no backup should be created for a no-op'


# ── d&i message: past-EOF colon range is insert-only, not "lines 4-3" ───────


def test_di_past_eof_colon_range_reports_insert_only():
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'past.txt', 'l1\nl2\nl3\n')
        res = _d_and_i(om, f, new_content='X\n', range_param='4:')
        assert res.startswith('OK'), res
        assert 'INSERT-ONLY' in res, f"zero-line delete must be labelled insert-only: {res!r}"
        assert 'lines 4-3' not in res, f"nonsensical range leaked into the message: {res!r}"
