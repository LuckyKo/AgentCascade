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
