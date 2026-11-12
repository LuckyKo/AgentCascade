"""Multi-edit same-file atomic grouping — primitive-level tests.

Covers the two layers the feature adds:
  1. ``batch_mutate_file`` (file_operations): N reducible edits applied atomically
     against the ORIGINAL file bytes — all-or-nothing, exactly N result strings,
     ownership established once.
  2. ``_scan_file_mutation_groups`` (engine): which same-file same-turn edit runs
     collapse into a group (reducibility + consecutiveness + cap).

The end-to-end leader/emit path is exercised indirectly: the invariant that makes it
API-safe is "batch_mutate_file always returns exactly N strings, on every exit path",
which is asserted here on the happy, no-match, and overlap paths.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.operation_manager import OperationManager  # noqa: E402
from agent_cascade.operation_manager.file_operations import EditSpec  # noqa: E402
from agent_cascade.engine.tool_execution import ToolExecMixin  # noqa: E402


def _make_om(tmpdir):
    return OperationManager(base_dir=str(tmpdir))


def _owned_file(om, tmpdir, name, content, agent='coder'):
    f = Path(tmpdir, name)
    f.write_text(content, encoding='utf-8')
    om._own(f.resolve(), agent)
    return f


# ── batch_mutate_file ──────────────────────────────────────────────────────────

def test_batch_applies_all_edits_atomically(tmpdir):
    om = _make_om(tmpdir)
    f = _owned_file(om, tmpdir, 'f.txt', 'a\nb\nc\nd\n')
    specs = [EditSpec('a', 'A', 'exact'), EditSpec('c', 'C', 'exact')]
    results = om.batch_mutate_file(str(f), specs, 'coder')
    assert len(results) == 2
    assert all(r.startswith('OK') for r in results), results
    assert f.read_text(encoding='utf-8') == 'A\nb\nC\nd\n'


def test_batch_rolls_back_whole_when_an_edit_anchors_on_another_output(tmpdir):
    """§7.2 causative case: edit #2 anchors on edit #1's not-yet-written output.
    Because every edit is matched against the ORIGINAL bytes, edit #2 finds nothing →
    the whole batch rolls back; file bytes AND mtime must be untouched."""
    om = _make_om(tmpdir)
    f = _owned_file(om, tmpdir, 'g.txt', 'a\nb\nc\n')
    before_bytes = f.read_bytes()
    before_mtime = f.stat().st_mtime_ns
    specs = [EditSpec('b', 'b\nb2', 'exact'),   # writes 'b2' only after the atomic write
             EditSpec('b2', 'b3', 'exact')]      # anchors on edit #1's output → no match
    results = om.batch_mutate_file(str(f), specs, 'coder')
    assert len(results) == 2
    assert all('REJECTED:' in r for r in results), results
    assert f.read_bytes() == before_bytes
    assert f.stat().st_mtime_ns == before_mtime


def test_batch_rejects_overlapping_edits_unchanged(tmpdir):
    om = _make_om(tmpdir)
    f = _owned_file(om, tmpdir, 'h.txt', 'hello world\n')
    before = f.read_bytes()
    specs = [EditSpec('hello', 'HI', 'exact'),
             EditSpec('hello world', 'GONE', 'exact')]  # overlaps edit #1
    results = om.batch_mutate_file(str(f), specs, 'coder')
    assert len(results) == 2
    assert all('REJECTED:' in r and 'overlap' in r for r in results), results
    assert f.read_bytes() == before


def test_batch_ownership_established_once(tmpdir):
    """Ownership is set once inside the atomic write; a later single edit_file on the
    same file must auto-approve (proves _own fired and persists)."""
    om = _make_om(tmpdir)
    f = _owned_file(om, tmpdir, 'i.txt', 'x\ny\n')
    specs = [EditSpec('x', 'X', 'exact'), EditSpec('y', 'Y', 'exact')]
    om.batch_mutate_file(str(f), specs, 'coder')
    assert f.read_text(encoding='utf-8') == 'X\nY\n'
    assert om._is_auto_approved(str(f), 'coder') is True

def test_batch_rejects_member_with_identical_old_new(tmpdir):
    """A member whose old_content==new_content is a no-op surfaced by _edit_compute_match
    as an error (identical to a standalone edit). The whole batch rolls back atomically and
    the file is left untouched — no partial write."""
    om = _make_om(tmpdir)
    f = _owned_file(om, tmpdir, 'j.txt', 'a\nb\n')
    before = f.read_bytes()
    results = om.batch_mutate_file(
        str(f), [EditSpec('b', 'B', 'exact'), EditSpec('a', 'a', 'exact')], 'coder')
    assert len(results) == 2
    assert all(r.startswith('REJECTED:') for r in results), results
    assert f.read_bytes() == before


def test_batch_failure_results_detectable_as_failure(tmpdir):
    """The engine classifies success/failure from the FIRST line, so rollback headers
    must start with a recognized error token — not a custom 'REJECTED BATCH:' label."""
    om = _make_om(tmpdir)
    f = _owned_file(om, tmpdir, 'k.txt', 'a\nb\n')
    results = om.batch_mutate_file(
        str(f), [EditSpec('b', 'B', 'exact'), EditSpec('ZZZ', 'X', 'exact')], 'coder')
    for r in results:
        first = next(ln.strip() for ln in r.split('\n') if ln.strip()).lower()
        assert first.startswith(('error:', 'rejected:', 'failed:', 'invalid:')), first


# ── engine scan (grouping) ─────────────────────────────────────────────────────

class _ScanShim(ToolExecMixin):
    """Minimal shim: supply only the two helpers the scan depends on. No __init__ —
    the scan touches no instance state beyond those methods."""
    def _detect_tool(self, out):
        return True, out['name'], out['args'], ''
    def _resolve_placeholders(self, args, inst_name, tool_name):
        return args


def _tg(name, **args):
    return {'name': name, 'args': args}


def test_scan_groups_same_path_reducible_run():
    shim = _ScanShim()
    tg = [_tg('edit_file', path='a.py', old_content='x', new_content='y'),
          _tg('edit_file', path='a.py', old_content='z', new_content='w'),
          _tg('edit_file', path='a.py', old_content='p', new_content='q')]
    assert shim._scan_file_mutation_groups(tg, 'coder') == {0: [0, 1, 2]}


def test_scan_is_per_path_and_breaks_on_other_tools():
    shim = _ScanShim()
    tg = [_tg('edit_file', path='a.py', old_content='x', new_content='y'),
          _tg('edit_file', path='b.py', old_content='x', new_content='y'),   # path split
          _tg('shell_cmd', command='ls'),                                   # non-edit split
          _tg('edit_file', path='a.py', old_content='x', new_content='y'),
          _tg('edit_file', path='a.py', old_content='z', new_content='w')]
    assert shim._scan_file_mutation_groups(tg, 'coder') == {3: [3, 4]}


@pytest.mark.parametrize('bad', [
    {'match_mode': 'delete_and_insert'},
    {'occurrence': -1},
    {'occurrence': 2},
])
def test_scan_excludes_non_reducible(bad):
    shim = _ScanShim()
    base = {'path': 'a.py', 'old_content': 'x', 'new_content': 'y'}
    tg = [_tg('edit_file', **{**base, **bad}),
          _tg('edit_file', **base)]
    # the non-reducible member breaks the run; the trailing reducible single is too short
    assert shim._scan_file_mutation_groups(tg, 'coder') == {}


def test_scan_caps_group_at_max_size():
    shim = _ScanShim()
    tg = [_tg('edit_file', path='a.py', old_content=str(i), new_content=str(i + 1))
          for i in range(15)]
    groups = shim._scan_file_mutation_groups(tg, 'coder')
    assert groups == {0: list(range(ToolExecMixin._MAX_BATCH_GROUP_SIZE))}
