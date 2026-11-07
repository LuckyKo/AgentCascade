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
  * Line-ending preservation: writes use the file's own dominant style (no whole-file
    CRLF rewrite on Windows, no LF→CRLF translation on Unix) and CRLF `new_content`
    is normalized instead of producing mixed endings.
  * A concurrent-modification guard aborts an edit when the file's bytes changed
    between the initial read and the write (e.g. during approval).
  * Heuristic tail absorption (BUG_0168) now discloses a dropped suffix, and no longer
    claims absorption when BUG_0186 trailing-line preservation cancels it.

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


# ── BUG_0262: exact mode must not assert an impossible anchor/range cause ─────


def _advisory_line(res):
    """Pull the single 'two identical adjacent lines' note out of the full result
    message so tests assert on the advisory itself, not incidental text elsewhere."""
    notes = [ln for ln in res.splitlines() if 'two identical adjacent lines' in ln]
    assert notes, f"advisory line missing from: {res!r}"
    return notes[0]


def test_exact_adjacent_seed_advisory_is_neutral():
    """BUG_0260/0262: an exact-mode edit whose new_content legitimately seeds two
    adjacent identical lines that are NOT the matched anchor (a repeated test seed).
    The note is advisory-only (not an error) and must use the neutral wording that
    does not blame an anchor-repeat or a range boundary — neither can occur here."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'telemetry.py',
                        'def test_telemetry():\n    placeholder\n')
        seed = "    classified('instA', fpA, 80)\n"
        res = om.edit_file(str(f), 'coder',
                           old_content='    placeholder\n',
                           new_content=seed + seed,
                           match_mode='exact')
        assert res.startswith('OK'), f"must be a success message, not a defect flag: {res!r}"
        advisory = _advisory_line(res)
        assert 'verify this is intended' in advisory, \
            f"exact-mode advisory must use neutral wording: {advisory!r}"
        for cause in ('anchor', 'range boundary'):
            assert cause not in advisory, \
                f"exact-mode advisory must not assert {cause!r} cause: {advisory!r}"
        # Advisory-only: the intended seed is still written verbatim (no dedup).
        assert f.read_text(encoding='utf-8').count("classified('instA', fpA, 80)") == 2


def test_delete_and_insert_duplicate_keeps_diagnostic_wording():
    """Scoping the other way: the delete_and_insert path CAN repeat its anchor or
    leave it at a range boundary, so its advisory must still name those causes."""
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
        advisory = _advisory_line(res)
        assert 'anchor' in advisory, \
            f"delete_and_insert advisory must still name the anchor cause: {advisory!r}"
        assert 'range boundary' in advisory, \
            f"delete_and_insert advisory must still name the range-boundary cause: {advisory!r}"


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


# ── line-ending preservation (write in the file's own style) ────────────────


def test_lf_file_is_not_rewritten_to_crlf():
    """An LF file must stay LF even on Windows. read_text()/write_text() used to
    translate every ending to the OS native style, silently rewriting the whole
    file to CRLF after a one-line edit."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'lf.txt')
        f.write_bytes(b'a\nb\nc\n')
        om._own(f.resolve(), 'coder')
        res = om.edit_file(str(f), 'coder', old_content='b\n', new_content='B\n',
                           match_mode='exact')
        assert res.startswith('OK'), res
        assert f.read_bytes() == b'a\nB\nc\n', \
            f"LF file rewritten: {f.read_bytes()!r}"


def test_crlf_file_keeps_crlf_on_every_platform():
    """A CRLF file must stay CRLF on Unix too (the inverse of the Windows bug)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'crlf.txt')
        f.write_bytes(b'a\r\nb\r\nc\r\n')
        om._own(f.resolve(), 'coder')
        res = om.edit_file(str(f), 'coder', old_content='b\n', new_content='B\n',
                           match_mode='exact')
        assert res.startswith('OK'), res
        assert f.read_bytes() == b'a\r\nB\r\nc\r\n', \
            f"CRLF file was rewritten: {f.read_bytes()!r}"


def test_crlf_new_content_is_normalized_into_lf_file():
    """new_content carrying CRLF (e.g. pasted from a Windows file) must not create
    mixed endings or '\\r\\r\\n' when the target file is LF."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'mixed.txt')
        f.write_bytes(b'a\nb\nc\n')
        om._own(f.resolve(), 'coder')
        res = om.edit_file(str(f), 'coder', old_content='b\n', new_content='X\r\nY\r\n',
                           match_mode='exact')
        assert res.startswith('OK'), res
        assert f.read_bytes() == b'a\nX\nY\nc\n', \
            f"CRLF leaked into an LF file: {f.read_bytes()!r}"


# ── concurrent-modification guard ───────────────────────────────────────────


def test_concurrent_modification_is_rejected(monkeypatch):
    """Approval can take arbitrary time. If the file changed on disk after the read,
    the stale edit must be aborted (no write, no backup, external work preserved)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'conc.txt')
        f.write_text('alpha\nbeta\n', encoding='utf-8')
        # Not agent-owned → goes through approval; mutate the file while "waiting".
        def racy_approval(**_kwargs):
            f.write_text('external work\n', encoding='utf-8')
            return True, 'approved'

        monkeypatch.setattr(om, 'request_user_approval', racy_approval)
        res = om.edit_file(str(f), 'coder', old_content='beta\n',
                           new_content='beta2\n', match_mode='exact')
        assert res.startswith('ERROR') and 'concurrent' in res.lower(), res
        assert f.read_text(encoding='utf-8') == 'external work\n', \
            f"concurrent writer's content was clobbered: {f.read_text(encoding='utf-8')!r}"
        assert not list(Path(d).rglob('*.bak')), 'no backup should be created on abort'


# ── heuristic absorption visibility (BUG_0260) ──────────────────────────────


def test_absorbed_suffix_is_reported():
    """When absorption drops text after the replaced tail, the success message must
    say so — the file change itself is intentional BUG_0168 behaviour."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'abs.txt',
                        'line one\nline two\n'
                        '   from 129 columns. So "micro 512 sits exactly on the floor" ...\n'
                        'line five\n')
        res = om.edit_file(str(f), 'coder',
                           old_content='line one\nline two\n',
                           new_content='new one\nnew two\nnew three\n   from 129 columns.\n',
                           match_mode='heuristic')
        assert res.startswith('OK'), res
        assert 'absorbed' in res.lower(), res
        assert 'So "micro 512 sits exactly on the floor"' in res, \
            f"dropped suffix not disclosed: {res!r}"
        assert f.read_text(encoding='utf-8') == (
            'new one\nnew two\nnew three\n   from 129 columns.\nline five\n')


def test_cancelled_absorption_is_not_reported():
    """BUG_0186 preservation can cancel absorption; the message must not claim a
    line was absorbed when the following line actually survives untouched."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = ('line one\nline two\n   from 129 columns.\n'
                    '   from 129 columns. So "micro 512" ...\n')
        f = _owned_file(om, d, 'cancel.txt', original)
        res = om.edit_file(str(f), 'coder',
                           old_content='line one\nline two\n   from 129 columns.\n',
                           new_content='new line one\nnew line two\n   from 129 columns.\n',
                           match_mode='heuristic')
        assert res.startswith('OK'), res
        assert 'absorbed' not in res.lower(), \
            f"message claims an absorption that was cancelled: {res!r}"
        assert f.read_text(encoding='utf-8') == (
            'new line one\nnew line two\n   from 129 columns.\n'
            '   from 129 columns. So "micro 512" ...\n')
