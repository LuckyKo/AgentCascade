"""BUG_0039 — justification provenance on the approval path.

The reported defect ("framework auto-fills an empty justification with generated
prose") misattributes the mechanism: the injected prose is authored by the
security-advisor LLM, and the real defect is that on the *approval* path the
approver's reason OVERWRITES the caller's justification in five operations
(write_file, edit_file, re_indent, copy_file, move_file). A second, silent
defect (D2): on the auto-approve fast path the caller's justification was
discarded entirely, so a justified fast-pathed op logged no justification.

These tests exercise OperationManager directly with ``request_user_approval``
monkeypatched — no LLM, no API server, no real approval round-trip.

delete_file is deliberately NOT parametrized: its approval path composes and
APPENDS (file_operations.py:1852-1853) rather than overwrites; a separate guard
below pins that append behaviour through this change.
"""

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

APPROVER_LABEL = 'Auto-Approval Reason (approver-supplied, not caller-authored):'


def _make_om(tmpdir):
    """A real OperationManager over a temp workspace (no agent_pool)."""
    from agent_cascade.operation_manager import OperationManager
    om = OperationManager(base_dir=str(tmpdir))
    om.enable_timeout = True
    om.approval_timeout_seconds = 30
    return om


def _stub_approval(om, approved=True, reason=''):
    """Replace request_user_approval with an immediate stub (no blocking)."""
    om.request_user_approval = lambda **kwargs: (approved, reason)


# ── D1: approver reason must not replace the caller's justification ──────────

import pytest

FIVE_OPS = ['write_file', 'edit_file', 're_indent', 'copy_file', 'move_file']


@pytest.mark.parametrize('op', FIVE_OPS)
def test_approver_reason_does_not_replace_caller_justification(op):
    """D1 / BUG_0039: both texts survive; caller text under Security Justification,
    approver text under the Auto-Approval Reason label."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'target.txt')
        f.write_text('line one\nline two\n', encoding='utf-8')
        # Non-owned path → approval path is taken.
        caller_j = 'because I say so'
        advisor_r = 'advisor said this'
        _stub_approval(om, approved=True, reason=advisor_r)

        if op == 'write_file':
            res = om.write_file(str(f), 'new content\n', 'coder', justification=caller_j)
        elif op == 'edit_file':
            res = om.edit_file(str(f), 'coder', 'line one', 'line one edited',
                               match_mode='exact', justification=caller_j)
        elif op == 're_indent':
            res = om.re_indent(str(f), 'coder', lines='1:2', indent=4,
                               indent_type='space', mode='flat', justification=caller_j)
        elif op == 'copy_file':
            dest = Path(d, 'target_copy.txt')
            dest.write_text('old\n', encoding='utf-8')  # existing dest → approval path (new dest is auto-approved)
            res = om.copy_file(str(f), str(dest), 'coder', justification=caller_j)
        else:  # move_file
            dest = Path(d, 'target_moved.txt')
            res = om.move_file(str(f), str(dest), 'coder', justification=caller_j)

        assert res.startswith('OK'), f'{op}: expected OK result, got: {res}'
        # Both texts must be present.
        assert caller_j in res, f'{op}: caller justification lost: {res}'
        assert advisor_r in res, f'{op}: approver reason missing: {res}'
        # Provenance ordering: caller text immediately after the Security Justification label.
        sj_idx = res.index('Security Justification:')
        assert res[sj_idx:sj_idx + len('Security Justification: ') + 1] == \
            'Security Justification: b', f'{op}: caller text must follow the label: {res}'
        # Approver text must appear after its own label.
        ar_idx = res.index(APPROVER_LABEL)
        assert advisor_r in res[ar_idx:], f'{op}: approver text must follow its label: {res}'


@pytest.mark.parametrize('op', FIVE_OPS)
def test_auto_approved_preserves_caller_justification(op):
    """D2 (fails pre-fix): the fast path must keep the caller's justification."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'owned.txt')
        f.write_text('x\n', encoding='utf-8')
        om._own(f.resolve(), 'coder')  # owned → auto-approve fast path
        caller_j = 'because'

        if op == 'write_file':
            res = om.write_file(str(f), 'y\n', 'coder', justification=caller_j)
        elif op == 'edit_file':
            res = om.edit_file(str(f), 'coder', 'x', 'y', match_mode='exact',
                               justification=caller_j)
        elif op == 're_indent':
            res = om.re_indent(str(f), 'coder', lines='1:1', indent=4,
                               indent_type='space', mode='flat', justification=caller_j)
        elif op == 'copy_file':
            dest = Path(d, 'owned_copy.txt')  # new destination → fast path
            res = om.copy_file(str(f), str(dest), 'coder', justification=caller_j)
        else:  # move_file — owned source → fast path
            dest = Path(d, 'owned_moved.txt')
            res = om.move_file(str(f), str(dest), 'coder', justification=caller_j)

        assert res.startswith('OK'), f'{op}: expected OK result, got: {res}'
        assert f'Security Justification: {caller_j}' in res, \
            f'{op}: fast path must preserve caller justification: {res}'
        assert APPROVER_LABEL not in res, f'{op}: no approver reason on fast path: {res}'


@pytest.mark.parametrize('op', FIVE_OPS)
def test_approver_reason_absent_when_approver_silent(op):
    """Approver approves with an empty reason → exactly one justification line,
    no Auto-Approval Reason label."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'target.txt')
        f.write_text('a\nb\n', encoding='utf-8')
        caller_j = 'silent approver case'
        _stub_approval(om, approved=True, reason='')

        if op == 'write_file':
            res = om.write_file(str(f), 'c\n', 'coder', justification=caller_j)
        elif op == 'edit_file':
            res = om.edit_file(str(f), 'coder', 'a', 'b', match_mode='exact',
                               justification=caller_j)
        elif op == 're_indent':
            res = om.re_indent(str(f), 'coder', lines='1:2', indent=4,
                               indent_type='space', mode='flat', justification=caller_j)
        elif op == 'copy_file':
            dest = Path(d, 'target_copy.txt')
            res = om.copy_file(str(f), str(dest), 'coder', justification=caller_j)
        else:  # move_file
            dest = Path(d, 'target_moved.txt')
            res = om.move_file(str(f), str(dest), 'coder', justification=caller_j)

        assert res.startswith('OK'), f'{op}: expected OK result, got: {res}'
        assert res.count('Security Justification:') == 1, \
            f'{op}: exactly one justification line expected: {res}'
        assert APPROVER_LABEL not in res, f'{op}: silent approver must not add a label: {res}'


def test_delete_file_append_behaviour_unchanged():
    """Guard: delete_file appends the approver reason to the composed justification
    (file_operations.py:1852-1853) — it does NOT share the five-site overwrite shape,
    and this change must not convert it."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'victim.txt')
        f.write_text('x\n', encoding='utf-8')
        caller_j = 'caller delete reason'
        advisor_r = 'advisor delete verdict'
        _stub_approval(om, approved=True, reason=advisor_r)

        res = om.delete_file(str(f), 'coder', justification=caller_j)
        assert res.startswith('OK: Deleted'), f'expected OK delete, got: {res}'
        # Append semantics: caller text first, approver text after it (joined).
        sj_idx = res.index('Security Justification:')
        tail = res[sj_idx:]
        assert caller_j in tail and advisor_r in tail, f'both texts expected: {res}'
        assert tail.index(caller_j) < tail.index(advisor_r), \
            f'appender must come after caller text: {res}'
        # The five-site label must NOT appear on the delete path.
        assert APPROVER_LABEL not in res, f'delete_file must keep append shape: {res}'
