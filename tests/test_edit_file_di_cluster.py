#!/usr/bin/env python3
"""Regression cluster for the edit_file delete_and_insert bug set (0095/0105/0109/0110).

Two test surfaces, matching the repo's existing patterns:

  * Tool level (BUG_0110) — `EditFile.call()` argument validation lives in
    `agent_cascade/tools/custom/file_ops.py`. Uses the recording-operation-manager
    pattern from `test_fence_preservation_fix.py` (no LLM, no filesystem): assert the
    leaked-ChatML-tag payload is rejected and never forwarded to the operation manager.

  * Operation-manager level (BUG_0095 / 0105 / 0109) — the range/no-op/indent logic
    lives in `OperationManager.edit_file`. Uses a REAL OperationManager over a temp
    workspace (the `test_delete_file.py` pattern), owning the file so the edit is
    auto-approved. Asserts the guard's return string AND the on-disk effect.

Run: python -m pytest tests/test_edit_file_di_cluster.py -v -p no:xdist -o addopts=""
"""

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Tool-level helpers (recording operation manager) ─────────────────────────


class _RecordingOM:
    """Captures the exact args forwarded to the operation manager."""

    def __init__(self):
        self.edit = None    # (path, old_content, new_content, match_mode)

    def edit_file(self, path, agent_name=None, old_content=None, new_content=None,
                  match_mode='exact', range_param=None, occurrence=None, justification=''):
        self.edit = (path, old_content, new_content, match_mode)
        return 'edited'


class _FakePool:
    def __init__(self, om):
        self.operation_manager = om


def _tool(tool_cls, om):
    """Construct a real tool wired to a recording operation manager."""
    return tool_cls(agent_pool=_FakePool(om), agent_name='tester')


# ── Operation-manager helpers (real OM over a temp workspace) ───────────────


def _make_om(tmpdir):
    from agent_cascade.operation_manager import OperationManager
    return OperationManager(base_dir=str(tmpdir))


def _owned_file(om, tmpdir, name, content, agent='coder'):
    """Write a file in the workspace and mark it owned so edits auto-approve."""
    f = Path(tmpdir, name)
    f.write_text(content, encoding='utf-8')
    om._own(f.resolve(), agent)
    return f


def _d_and_i(om, f, new_content, range_param, agent='coder'):
    """Invoke the real d&i edit path (old_content is unused in d&i mode)."""
    return om.edit_file(str(f), agent, old_content='', new_content=new_content,
                        match_mode='delete_and_insert', range_param=range_param)


# ── BUG_0110: leaked ChatML parameter tag absorbed new_content ──────────────


def test_edit_file_missing_new_content_names_leaked_tag():
    """A justification carrying `</parameter=new_content>` (the field the model folded
    new_content into) must error loudly, name the tag, echo the received keys, and
    never reach the filesystem. Mirrors the real grammar-constrained reproduction."""
    from agent_cascade.tools.custom.file_ops import EditFile
    om = _RecordingOM()
    t = _tool(EditFile, om)
    # Faithful reproduction: keys are path/old_content/justification; no new_content.
    payload = {
        'path': 'doc.py',
        'old_content': 'OLD',
        'justification': 'set the value</parameter=new_content>\nvalue = 42\n</function>',
    }
    result = t.call(payload)
    assert om.edit is None, f"leaked-tag payload was forwarded to the filesystem: {om.edit!r}"
    assert 'ERROR' in result, f"expected an error, got: {result!r}"
    assert '</parameter' in result, f"error must name the leaked tag: {result!r}"
    assert 'justification' in result, f"error must echo the received keys: {result!r}"


def test_edit_file_clean_payload_still_passes():
    """Positive control: a normal edit (new_content present, no tags) is forwarded
    untouched — the self-diagnosing branch must not fire on valid calls."""
    from agent_cascade.tools.custom.file_ops import EditFile
    om = _RecordingOM()
    t = _tool(EditFile, om)
    t.call({'path': 'doc.py', 'old_content': 'OLD', 'new_content': 'NEW', 'match_mode': 'exact'})
    assert om.edit is not None, 'a clean payload was wrongly rejected'
    assert om.edit[2] == 'NEW'


# ── BUG_0095: net-zero (byte-identical) edit reported as success ───────────


def test_edit_file_noop_range_returns_warning_and_leaves_file():
    """range '3:3' + new_content '\\n' deletes a blank line and re-inserts an identical
    one → byte-identical result. Must return a WARNING and NOT write."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'l1\nl2\n\nl4\n'  # line 3 is a blank line
        f = _owned_file(om, d, 'f.txt', original)
        res = _d_and_i(om, f, new_content='\n', range_param='3:3')
        assert res.startswith('WARNING'), f"expected a no-op WARNING, got: {res!r}"
        assert f.read_text(encoding='utf-8') == original, 'no-op must leave the file unchanged'


def test_edit_file_blank_line_insert_succeeds():
    """A genuine insert that ADDS a blank line changes bytes → must succeed and write."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'g.txt', 'l1\nl2\nl3\n')
        res = _d_and_i(om, f, new_content='\n', range_param='3')  # insert before line 3
        assert res.startswith('OK'), f"expected success, got: {res!r}"
        assert f.read_text(encoding='utf-8') == 'l1\nl2\n\nl3\n', 'blank line should be inserted'


# ── BUG_0109: range ':' deletes the whole file (data-loss footgun) ──────────


def test_edit_file_whole_file_range_errors_and_leaves_file():
    """range ':' spans the entire file; with non-empty new_content it would delete
    and rewrite everything. Must error and NOT write."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'l1\nl2\nl3\n'
        f = _owned_file(om, d, 'h.txt', original)
        res = _d_and_i(om, f, new_content='replacement\n', range_param=':')
        assert res.startswith('ERROR'), f"expected whole-file-range ERROR, got: {res!r}"
        assert 'entire file' in res, f"error should explain the whole-file span: {res!r}"
        assert f.read_text(encoding='utf-8') == original, 'whole-file guard must not write'


# ── BUG_0175: suffix range 'N:' silently deletes the tail (message-only fix) ─


def test_edit_file_suffix_range_notes_tail_deletion():
    """range '3:' on a 3-line file deletes through EOF. The edit still performs, but the
    success message must surface the deleted tail (name the range, quote the last deleted
    line, point to range '0' / match_mode='exact'). This is the red/green discriminator:
    pre-fix the message carries no such note."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'l1\nl2\nl3\n'
        f = _owned_file(om, d, 'k.txt', original)
        res = _d_and_i(om, f, new_content='X\n', range_param='3:')
        assert res.startswith('OK'), f"suffix-range edit should still succeed, got: {res!r}"
        assert 'also deleted' in res, f"message must surface the tail deletion: {res!r}"
        assert 'to append without deleting' in res, f"message must point to the safe alternatives: {res!r}"
        assert 'l3' in res, f"message must quote the last deleted line: {res!r}"
        # Data loss really happened — now surfaced rather than silent.
        assert f.read_text(encoding='utf-8') == 'l1\nl2\nX\n', 'line 3 must in fact be removed'


def test_edit_file_suffix_range_note_absent_on_mid_file():
    """A mid-file delete that does NOT reach EOF must get NO tail-deletion note (no false
    positive on a normal d&i edit)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'l1\nl2\nl3\nl4\nl5\n'
        f = _owned_file(om, d, 'm.txt', original)
        res = _d_and_i(om, f, new_content='X\n', range_param='2:3')
        assert res.startswith('OK'), f"mid-file edit should succeed, got: {res!r}"
        assert 'also deleted' not in res, f"mid-file edit must not carry the tail note: {res!r}"
        assert f.read_text(encoding='utf-8') == 'l1\nX\nl4\nl5\n', 'mid-file delete+insert must be exact'


def test_edit_file_suffix_range_whole_file_still_errors():
    """Regression guard: the whole-file form ':' keeps ERRORing (BUG_0109 unchanged) —
    the new note lives only on the accepted suffix-range path, not the ERROR path."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'l1\nl2\nl3\n'
        f = _owned_file(om, d, 'n.txt', original)
        res = _d_and_i(om, f, new_content='replacement\n', range_param=':')
        assert res.startswith('ERROR'), f"whole-file range must still ERROR, got: {res!r}"
        assert 'also deleted' not in res, f"the note must not appear on the ERROR path: {res!r}"
        assert f.read_text(encoding='utf-8') == original, 'whole-file guard must not write'


def test_edit_file_suffix_range_insert_only_at_eof_no_note():
    """range '4' (past the end) parses to an empty range → the INSERT-ONLY branch, a sibling
    of the note's `elif new_content:` branch, so the note code is structurally unreachable here.
    Structural guard only (confirms insert-only is untouched) — NOT a note-guard discriminator."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'l1\nl2\nl3\n'
        f = _owned_file(om, d, 'o.txt', original)
        res = _d_and_i(om, f, new_content='l4\n', range_param='4')
        assert res.startswith('OK'), f"insert-only-at-EOF should succeed, got: {res!r}"
        assert 'also deleted' not in res, f"insert-only edit must not carry the tail note: {res!r}"
        assert f.read_text(encoding='utf-8') == 'l1\nl2\nl3\nl4\n', 'insert-only must not delete anything'


def test_edit_file_suffix_range_past_eof_colon_deletes_nothing_no_note():
    """range '4:' on a 3-line file reaches the NORMAL d&i branch (start_idx != end_idx) but
    deletes ZERO lines (old_lc == 0), so the guard's `old_lc > 0` term must suppress the note.
    This is the ONLY input that exercises that term: without it the note would falsely print a
    nonsensical 'trailing lines 4-3'. The insert still happens."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'l1\nl2\nl3\n'
        f = _owned_file(om, d, 'p.txt', original)
        res = _d_and_i(om, f, new_content='X\n', range_param='4:')
        assert res.startswith('OK'), f"past-EOF colon edit should succeed, got: {res!r}"
        assert 'also deleted' not in res, f"zero-line delete must not carry the tail note: {res!r}"
        assert f.read_text(encoding='utf-8') == 'l1\nl2\nl3\nX\n', 'nothing deleted, X appended'


def test_edit_file_append_at_eof_succeeds():
    """range '0' is a legitimate append and must NOT be caught by the whole-file guard."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'i.txt', 'l1\nl2\nl3\n')
        res = _d_and_i(om, f, new_content='l4\n', range_param='0')
        assert res.startswith('OK'), f"append should succeed, got: {res!r}"
        assert f.read_text(encoding='utf-8') == 'l1\nl2\nl3\nl4\n', 'append should add the line'


# ── BUG_0213: range ends mid-block, leaving orphaned residue on the surviving side ──


def test_edit_file_mid_block_range_notes_continuation_residue():
    """The BUG_0213 repro: the section body spans lines 3-8 but the range stops at 5.
    The edit still performs, but the success message must surface the first surviving
    line (line 6, mid-sentence) so the residue is visible without a follow-up grep.
    Red/green discriminator: pre-fix the message carried only line arithmetic."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = ('# Doc\n'
                    '\n'
                    '## Section\n'
                    'First line of the\n'
                    'section body that\n'
                    'runs on to line eight\n'
                    'and ends here.\n'
                    '\n'
                    '## Next\n'
                    'body\n')
        f = _owned_file(om, d, 'residue.md', original)
        res = _d_and_i(om, f, new_content='X\n', range_param='3:5')
        assert res.startswith('OK'), f"mid-block edit should still succeed, got: {res!r}"
        assert 'note: line 6 after the range continues the deleted block' in res, \
            f"message must flag the surviving continuation at line 6: {res!r}"
        assert 'runs on to line eight' in res, \
            f"message must quote the first surviving line: {res!r}"
        assert 'extend the range' in res, f"message must point at the repair: {res!r}"
        # The residue is real — now surfaced rather than silent.
        assert 'X\nruns on to line eight\n' in f.read_text(encoding='utf-8'), \
            'the orphaned continuation must in fact survive the edit'


def test_edit_file_block_boundary_range_has_no_residue_note():
    """A range that ends exactly where the block ends (next line is blank) must NOT
    carry the BUG_0213 note — no false positive on a clean section replacement."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = ('# Doc\n'
                    '\n'
                    '## Section\n'
                    'First line of the\n'
                    'section body that\n'
                    'runs on to line eight\n'
                    'and ends here.\n'
                    '\n'
                    '## Next\n'
                    'body\n')
        f = _owned_file(om, d, 'boundary.md', original)
        res = _d_and_i(om, f, new_content='X\n', range_param='3:7')
        assert res.startswith('OK'), f"clean section replace should succeed, got: {res!r}"
        assert 'after the range' not in res, \
            f"block-boundary edit must not carry a residue note: {res!r}"
        assert f.read_text(encoding='utf-8') == '# Doc\n\nX\n\n## Next\nbody\n', \
            'section replacement must be exact'


def test_edit_file_terminal_punctuation_range_has_no_residue_note():
    """A deleted block whose last line closes cleanly ('.') is presumed complete, so a
    non-blank surviving line must not be flagged as residue."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'para one ends.\ncontinues here\nlast\n'
        f = _owned_file(om, d, 'closed.txt', original)
        res = _d_and_i(om, f, new_content='replacement\n', range_param='1:1')
        assert res.startswith('OK'), f"edit should succeed, got: {res!r}"
        assert 'after the range' not in res, \
            f"closed block must not carry a residue note: {res!r}"
        assert f.read_text(encoding='utf-8') == 'replacement\ncontinues here\nlast\n', \
            'edit must be exact'


def test_edit_file_range_ending_on_heading_notes_orphan():
    """Deleting a heading while leaving its body behind orphans the body even though it
    is plain prose — the second BUG_0213 shape. Must be flagged."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'intro\n## Section title\nbody line one\n'
        f = _owned_file(om, d, 'heading.md', original)
        res = _d_and_i(om, f, new_content='replacement\n', range_param='2:2')
        assert res.startswith('OK'), f"edit should succeed, got: {res!r}"
        assert 'note: line 3 after the range may be orphaned' in res, \
            f"message must flag the orphaned body under a deleted heading: {res!r}"
        assert 'body line one' in res, f"message must quote the surviving line: {res!r}"


def test_edit_file_next_line_structural_start_has_no_residue_note():
    """A surviving list item / heading / fence starts its own block and must never be
    mistaken for residue."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'intro text that\n- item one\n- item two\n'
        f = _owned_file(om, d, 'list.md', original)
        res = _d_and_i(om, f, new_content='X\n', range_param='1:1')
        assert res.startswith('OK'), f"edit should succeed, got: {res!r}"
        assert 'after the range' not in res, \
            f"a structural next line must not be flagged as residue: {res!r}"


def test_edit_file_insert_only_has_no_residue_note():
    """Insert-only d&i (old_lc == 0) deletes nothing, so there is nothing to orphan."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'insert.md', 'line one\nline two\n')
        res = _d_and_i(om, f, new_content='inserted\n', range_param='2')
        assert res.startswith('OK'), f"insert-only should succeed, got: {res!r}"
        assert 'after the range' not in res, \
            f"insert-only edit must not carry a residue note: {res!r}"


# ── BUG_0224: range starts on a structural opener the replacement does not restore ──


def test_edit_file_range_starting_on_heading_notes_unrestored_opener():
    """The BUG_0224 repro: a range that begins on a '###' heading whose new_content is
    body-only deletes the heading and leaves the section without a title. The edit still
    succeeds (advisory only) but the message must now flag that the opener was dropped.
    Red/green discriminator: pre-fix the message carried no such note."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = ('# Doc\n'
                    '\n'
                    '### Section title\n'
                    'body line one\n'
                    'body line two\n'
                    '\n'
                    '### Next\n'
                    'more\n')
        f = _owned_file(om, d, 'opener.md', original)
        # Delete the heading + body (lines 3-5), replace with body text only.
        res = _d_and_i(om, f, new_content='new body only\n', range_param='3:5')
        assert res.startswith('OK'), f"edit should still succeed (advisory only), got: {res!r}"
        assert 'note: range started on a heading/structural opener' in res, \
            f"message must flag the dropped heading: {res!r}"
        assert 'Section title' in res, f"message must quote the dropped opener: {res!r}"
        # The heading is genuinely gone now.
        assert '### Section title' not in f.read_text(encoding='utf-8'), \
            'the heading must in fact be removed by the edit'


def test_edit_file_range_starting_on_heading_restored_no_note():
    """A range that begins on a heading whose new_content RE-SUPPLIES a heading must NOT
    carry the note — no false positive when the opener is intentionally restored."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = ('### Old title\n'
                    'body line one\n'
                    'body line two\n'
                    '\n'
                    '## Next section\n')
        f = _owned_file(om, d, 'restored.md', original)
        # Replace the first section (lines 1-3), but re-supply a heading.
        res = _d_and_i(om, f, new_content='### New title\nnew body\n', range_param='1:3')
        assert res.startswith('OK'), f"edit should succeed, got: {res!r}"
        assert 'heading/structural opener' not in res, \
            f"a restored heading must not carry the note: {res!r}"
        assert f.read_text(encoding='utf-8') == '### New title\nnew body\n\n## Next section\n', \
            'replacement must be exact'


def test_edit_file_plain_body_range_has_no_opener_note():
    """A range whose first deleted line is plain prose (no structural opener) must NOT
    carry the note — no false positive on an ordinary body replacement."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = ('# Doc\n'
                    'plain body one\n'
                    'plain body two\n')
        f = _owned_file(om, d, 'plain.md', original)
        res = _d_and_i(om, f, new_content='replacement\n', range_param='2:3')
        assert res.startswith('OK'), f"edit should succeed, got: {res!r}"
        assert 'heading/structural opener' not in res, \
            f"a plain-body range must not carry the opener note: {res!r}"
        assert f.read_text(encoding='utf-8') == '# Doc\nreplacement\n', \
            'plain-body replacement must be exact'


def test_edit_file_fence_opener_unrestored_notes():
    """A range starting on a code fence whose replacement is not a fence must be flagged
    (fence is a recognised structural opener)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = ('intro\n'
                    '```\n'
                    'code here\n'
                    '```\n')
        f = _owned_file(om, d, 'fence.md', original)
        res = _d_and_i(om, f, new_content='plain text\n', range_param='2:4')
        assert res.startswith('OK'), f"edit should succeed, got: {res!r}"
        assert 'heading/structural opener' in res, \
            f"a dropped code fence must be flagged: {res!r}"


def test_edit_file_defclass_opener_unrestored_notes():
    """A range starting on a 'def' header whose replacement is not a def/class must be
    flagged (def/class is a recognised structural opener)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = ('def foo():\n'
                    '    return 1\n'
                    '\n'
                    'def bar():\n'
                    '    return 2\n')
        f = _owned_file(om, d, 'func.py', original)
        res = _d_and_i(om, f, new_content='x = 1\n', range_param='1:2')
        assert res.startswith('OK'), f"edit should succeed, got: {res!r}"
        assert 'heading/structural opener' in res, \
            f"a dropped def header must be flagged: {res!r}"


def test_edit_file_advisory_does_not_block_edit():
    """The note is advisory: even when it fires, the file is written with the new content
    (the edit is not rejected)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = '### Title\nbody one\nbody two\n\n## Next\n'
        f = _owned_file(om, d, 'block.md', original)
        res = _d_and_i(om, f, new_content='replaced body\n', range_param='1:3')
        assert res.startswith('OK'), f"advisory must not block the edit, got: {res!r}"
        assert 'heading/structural opener' in res, f"note should fire: {res!r}"
        assert f.read_text(encoding='utf-8') == 'replaced body\n\n## Next\n', \
            'the edit must be applied despite the advisory'


# ── BUG_0105: verbatim indentation of inserted lines (regression, no code change)


def test_edit_file_preserves_three_space_indent_verbatim():
    """A d&i insert whose new_content already has leading whitespace must be written
    verbatim (exactly 3 spaces) — NOT re-indented to the surrounding block's base.
    Locks in commit 536746ee9 (BUG_0063 verbatim-indent guard)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        # Insert point (line 2) has a 4-space base indent; new_content uses 3 spaces.
        f = _owned_file(om, d, 'j.py', 'def f():\n    pass\n')
        res = _d_and_i(om, f, new_content='   x = 1\n', range_param='2')
        assert res.startswith('OK'), f"expected success, got: {res!r}"
        written = f.read_text(encoding='utf-8')
        inserted_lines = [ln for ln in written.splitlines() if 'x = 1' in ln]
        assert len(inserted_lines) == 1, f"expected one inserted line, got: {written!r}"
        assert inserted_lines[0] == '   x = 1', \
            f"indent must stay exactly 3 spaces (verbatim), got: {inserted_lines[0]!r}"
