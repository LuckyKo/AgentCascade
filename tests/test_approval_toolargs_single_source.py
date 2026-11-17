#!/usr/bin/env python3
"""Tests for BUG_0335: the security-approver ``tool_args`` is the single source
of truth (the full validated executor params), not a hand-picked per-tool subset.

Covers:
  - the pure ``build_approval_args`` helper (exclude / drop_none / preview),
  - the structural guarantee that a param added to a tool's executor signature
    reaches the approver WITHOUT touching the approval call site (probe tool),
  - ``edit_file`` superset + the BUG_0334 ``occurrence`` contract (present when
    supplied, absent when unsupplied),
  - ``delete_file``'s DELIBERATE compact count/paths_preview transform (not the
    full resolved list),
  - ``write_file`` / ``copy_file`` / ``move_file`` payloads equal the full params
    minus the internal routing params (never leak ``agent_name``),
  - the fail-loud frame-capture guard rejects a malformed call frame.

Run: python -m pytest tests/test_approval_toolargs_single_source.py -v -p no:xdist -o addopts=""
"""

import tempfile
from pathlib import Path

import pytest

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_om(tmpdir):
    from agent_cascade.operation_manager import OperationManager
    return OperationManager(base_dir=str(tmpdir))


def _capture(om):
    """Force every call onto the approval path and record the ``tool_args`` it
    hands the approver. Patches ``_is_auto_approved`` to False and stubs
    ``request_user_approval`` to capture + approve. Returns the capture list."""
    captured = []

    def _never_auto(*args, **kwargs):
        return False

    def _record_and_approve(*args, **kwargs):
        captured.append(kwargs.get('tool_args', {}))
        return (True, 'approved by test spy')

    om._is_auto_approved = _never_auto
    om.request_user_approval = _record_and_approve
    return captured


def _owned_file(om, tmpdir, name, content, agent='coder'):
    f = Path(tmpdir, name)
    f.write_text(content, encoding='utf-8')
    om._own(f.resolve(), agent)
    return f


# ── Pure helper ───────────────────────────────────────────────────────────────


def test_build_approval_args_transforms():
    from agent_cascade.operation_manager.file_operations import build_approval_args

    # exclude drops internal/routing params only; everything else survives.
    out = build_approval_args(
        {'path': 'a', 'agent_name': 'coder', 'pre_approved': True},
        exclude=frozenset({'self', 'agent_name', 'pre_approved'}))
    assert out == {'path': 'a'}, f"only path should remain, got {out!r}"

    # drop_none omits unsupplied options but keeps falsy-but-present values.
    out = build_approval_args(
        {'a': 1, 'b': None, 'justification': ''}, drop_none=True)
    assert out == {'a': 1, 'justification': ''}, f"only None dropped, got {out!r}"

    # preview receives the derived full-params dict and returns the final view.
    out = build_approval_args(
        {'a': 1, 'b': None}, drop_none=True,
        preview=lambda full: {'count': 1, 'echo': full.get('a')})
    assert out == {'count': 1, 'echo': 1}, f"preview not applied, got {out!r}"


# ── The class fix: a new executor param reaches the approver automatically ────


def test_new_executor_param_reaches_approver_without_call_site_change():
    """BUG_0335 acceptance: a param added to a tool's signature appears in the
    approver's tool_args even though the tool BODY never mentions it in the
    approval payload. ``_approval_args`` derives it from the live signature, so
    the approval call site needs no change. ``agent_name`` (internal) is excluded;
    a not-supplied option (None) is dropped."""
    from agent_cascade.operation_manager import OperationManager

    class _ProbeOM(OperationManager):
        def hypothetical_file_tool(self, path, agent_name, justification='',
                                   force_as=None, new_fancy_field=42):
            # NOTE: the body does NOT hand-pick any keys — it just derives from the
            # live signature. Add/remove signature params and the payload follows.
            return self.request_user_approval(
                agent_name=agent_name,
                tool_name='hypothetical_file_tool',
                tool_args=self._approval_args(drop_none=True),
                description='probe',
            )

    with tempfile.TemporaryDirectory() as d:
        om = _ProbeOM(base_dir=str(d))
        captured = _capture(om)

        # Both new params supplied → both appear.
        om.hypothetical_file_tool('/x', 'coder', justification='why',
                                  force_as='json', new_fancy_field=7)
        assert captured, 'the approval path must have been reached'
        ta = captured[0]
        assert ta.get('force_as') == 'json', f"new param missing: {ta!r}"
        assert ta.get('new_fancy_field') == 7, f"new param missing: {ta!r}"
        assert ta.get('path') == '/x', f"path missing: {ta!r}"
        assert 'agent_name' not in ta, f"internal param leaked: {ta!r}"
        assert 'self' not in ta, f"self leaked: {ta!r}"

        # New param NOT supplied (None) → dropped by drop_none, like occurrence.
        captured.clear()
        om.hypothetical_file_tool('/x', 'coder')
        ta = captured[0]
        assert 'force_as' not in ta, f"unsupplied option should be dropped: {ta!r}"


# ── edit_file: superset + the BUG_0334 occurrence contract ────────────────────


def test_edit_file_exact_payload_is_superset_and_keeps_occurrence():
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'f.txt', 'foo\nbar\nfoo\n')
        captured = _capture(om)
        om.edit_file(str(f), 'coder', old_content='foo', new_content='FOO',
                     match_mode='exact', occurrence=-1)
        assert captured, 'edit_file must reach the approver'
        ta = captured[0]
        # The approver must see a superset of the validated params.
        for k in ('path', 'old_content', 'new_content', 'match_mode', 'justification'):
            assert k in ta, f"superset missing {k}: {ta!r}"
        # BUG_0334 contract preserved: occurrence shown when supplied.
        assert ta.get('occurrence') == -1, f"occurrence must be -1: {ta!r}"
        assert 'agent_name' not in ta, f"internal param leaked: {ta!r}"


def test_edit_file_delete_and_insert_drops_unsupplied_occurrence():
    """Mirrors test_delete_and_insert_tool_args_has_no_occurrence: a defaulted
    option (occurrence=None) stays out of the approver's view via drop_none."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'f.txt', 'a\nb\nc\nd\ne\n')
        captured = _capture(om)
        om.edit_file(str(f), 'coder', old_content='', new_content='X\nY\n',
                     match_mode='delete_and_insert', range_param='2:3')
        assert captured, 'delete_and_insert must reach the approver'
        ta = captured[0]
        assert 'occurrence' not in ta, f"unsupplied occurrence must be dropped: {ta!r}"
        # range_param is now surfaced under its own param name (single source of truth).
        assert ta.get('range_param') == '2:3', f"range_param missing: {ta!r}"


# ── delete_file: deliberate compact preview is preserved ──────────────────────


def test_delete_file_compact_preview_preserved():
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        a = Path(d, 'a.txt'); a.write_text('1', encoding='utf-8')
        b = Path(d, 'b.txt'); b.write_text('2', encoding='utf-8')
        captured = _capture(om)  # all non-owned → one aggregate approval
        om.delete_file(str(Path(d)), 'coder', include='*.txt', justification='cleanup')
        assert captured, 'bulk delete must reach the approver'
        ta = captured[0]
        assert 'paths' not in ta, f"full paths list must not leak: keys {list(ta)}"
        assert 'path' not in ta, f"raw path must be replaced by the compact view: keys {list(ta)}"
        assert ta.get('count') == 2, f"count should be 2, got {ta.get('count')!r}"
        assert 'paths_preview' in ta, f"compact paths_preview missing: {list(ta)}"
        assert len(ta['paths_preview']) <= 5, f"preview must be bounded: {ta['paths_preview']}"
        assert 'cleanup' in (ta.get('justification') or ''), 'agent reason must be kept'


# ── write / copy / move: full params minus internal, never leak agent_name ────


def test_write_file_payload_is_full_params_minus_internal():
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'f.txt', 'old\n')
        captured = _capture(om)
        om.write_file(str(f), 'new\n', 'coder', justification='rewrite')
        assert captured, 'overwrite must reach the approver'
        assert captured[0] == {'path': str(f), 'content': 'new\n',
                               'justification': 'rewrite'}, f"got {captured[0]!r}"


def test_copy_and_move_payloads_are_full_params_minus_internal():
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        src = _owned_file(om, d, 'src.txt', 'data\n')
        dst = Path(d, 'dst.txt')

        captured = _capture(om)
        om.copy_file(str(src), str(dst), 'coder', justification='copy it')
        assert captured, 'copy must reach the approver'
        assert captured[0] == {'source': str(src), 'destination': str(dst),
                               'justification': 'copy it'}, f"copy got {captured[0]!r}"

        # Move needs a fresh source (the copy left dst behind; use src again).
        captured = _capture(om)
        dst.unlink()
        om.move_file(str(src), str(dst), 'coder', justification='move it')
        assert captured, 'move must reach the approver'
        assert captured[0] == {'source': str(src), 'destination': str(dst),
                               'justification': 'move it'}, f"move got {captured[0]!r}"


def test_approval_args_fails_loud_on_malformed_frame():
    """BUG_0335: the frame-capture guard must fail LOUD, not silently build a
    bogus payload, when called from a frame whose ``self`` is not this instance.

    Negative control: ``_approval_args`` is normally invoked ONLY inside a file tool
    method (the happy path is exercised by every other test here). Calling it
    directly from this test frame means the caller has no ``self`` equal to ``om``,
    so the guard must raise — proving the guard is live and not dead code.
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        with pytest.raises(RuntimeError):
            om._approval_args()  # not invoked by a tool method → invariant broken


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-v']))
