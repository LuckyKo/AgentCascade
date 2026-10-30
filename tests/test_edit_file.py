#!/usr/bin/env python3
"""BUG_0060 regression tests: echo deleted first/last content in the
`delete_and_insert` confirmation message.

When several ranged edits target one file in a parallel block, a later edit's
range (computed against the pre-earlier-edit file) can land on shifted content,
and the old confirmation was silent about *which* lines were actually deleted.
The fix echoes the deleted first/last ~80 chars so the drift is visible without
reading the diff.

Two surfaces, matching the repo's existing d&i cluster pattern
(`tests/test_edit_file_di_cluster.py`): a real OperationManager over a temp
workspace, owning the file so the edit auto-approves. We assert the
confirmation string (the returned message), not the on-disk effect.

Run: python -m pytest tests/test_edit_file.py -v -p no:xdist -o addopts=""
"""

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


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


def _thirty_lines():
    return '\n'.join(f'line {n}' for n in range(1, 31)) + '\n'


# ── BUG_0060: range drift is now visible in the confirmation ────────────────


def test_dandi_drift_echo_shows_actual_deleted_content():
    """Two ranged edits where the second's range was computed against the
    pre-first-edit file. The first edit is a true insert-only (+5 lines) that
    shifts the later lines down; the second edit's range '20:22' (which the
    caller believed covered 'line 20/21/22') now covers 'line 15/16/17'. The
    confirmation must echo the ACTUAL deleted content (first 'line 15',
    last 'line 17'), making the drift visible.

    Before the fix the message reported only line arithmetic and was silent
    about which lines were deleted, so this mislanding was invisible."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'drift.txt', _thirty_lines())

        # First edit: insert-only at line 5 (+5 lines, no deletion) shifts
        # 'line 5'..('line 30') down by 5 → 'line 15' now sits at index 19.
        r1 = _d_and_i(om, f, new_content='X\nX\nX\nX\nX', range_param='5')
        assert r1.startswith('OK'), f"insert-only first edit should succeed: {r1!r}"

        # Second edit: range '20:22' (caller's belief: lines 20-22) now lands on
        # the shifted content 'line 15/16/17'. The echo must reveal that.
        r2 = _d_and_i(om, f, new_content='Y', range_param='20:22')
        assert r2.startswith('OK'), f"drifted second edit should succeed: {r2!r}"
        assert 'deleted first line: "line 15"' in r2, \
            f"echo must show the actual first deleted line (drift), got: {r2!r}"
        assert 'deleted last line:  "line 17"' in r2, \
            f"echo must show the actual last deleted line (drift), got: {r2!r}"
        # The caller's intended content is NOT what was deleted — the drift is real.
        assert '"line 20"' not in r2, f"caller-intended content must not be echoed: {r2!r}"


def test_dandi_single_line_delete_echo():
    """A single-line delete+insert (range '10:10') echoes the one deleted line
    as a single `deleted: "line 10"` entry (no first/last split)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'single.txt', _thirty_lines())
        r = _d_and_i(om, f, new_content='Y', range_param='10:10')
        assert r.startswith('OK'), f"single-line delete+insert should succeed: {r!r}"
        assert 'deleted: "line 10"' in r, \
            f"single-line echo must read `deleted: \"line 10\"`, got: {r!r}"
        # Single-line form must not carry the multi-line first/last split.
        assert 'deleted first line' not in r, f"single-line must not use first/last split: {r!r}"
