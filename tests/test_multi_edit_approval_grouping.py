"""Multi-edit same-file grouping — primitive + batch-application tests.

The grouped-batch path does two things and nothing else:
  1. ONE security approval for a run of N same-file edit_file calls, so the guard
     sees every member (not just the first), and
  2. applies each member SERIALLY through the existing single-edit ``edit_file``
     path — ``pre_approved=True`` suppresses only the per-member approval gate.

There is no atomic batch primitive; application lives only in ``edit_file``. These
tests pin the contract ``edit_file`` gained (``pre_approved``) plus the two batch
guarantees the engine relies on — per-member feedback with no rollback of earlier
members, and the approver's reason surfaced on the lead result. The grouping scan
(which runs collapse into one group) is also pinned here.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.operation_manager import OperationManager  # noqa: E402
from agent_cascade.engine.tool_execution import ToolExecMixin  # noqa: E402


def _make_om(tmpdir):
    return OperationManager(base_dir=str(tmpdir))


def _owned_file(om, tmpdir, name, content, agent='coder'):
    f = Path(tmpdir, name)
    f.write_text(content, encoding='utf-8')
    om._own(f.resolve(), agent)
    return f


# ── edit_file(pre_approved=True) ───────────────────────────────────────────────

def test_pre_approved_skips_per_call_approval_still_writes(tmpdir):
    """pre_approved=True suppresses ONLY the approval gate: an UNowned file — which
    would otherwise prompt — is edited without any request_user_approval call, and a
    control (same manager, pre_approved=False) confirms the gate really does fire
    when the flag is absent."""
    om = _make_om(tmpdir)
    f = Path(tmpdir, 'pp.txt')
    f.write_text('a\nb\n', encoding='utf-8')   # NOT owned -> would normally prompt
    om.request_user_approval = Mock(return_value=(True, 'ok'))

    result = om.edit_file(str(f), 'coder', 'a', 'A', 'exact', pre_approved=True)
    om.request_user_approval.assert_not_called()   # the gate was skipped
    assert result.startswith('OK'), result
    assert f.read_text(encoding='utf-8') == 'A\nb\n'

    # Control: an unowned edit WITHOUT pre_approved must reach the gate.
    g = Path(tmpdir, 'pp2.txt')
    g.write_text('c\n', encoding='utf-8')
    om.edit_file(str(g), 'coder', 'c', 'C', 'exact')
    om.request_user_approval.assert_called_once()


def test_pre_approved_still_runs_vault_screen_and_concurrent_guard(tmpdir):
    """pre_approved must NOT skip the memory-vault screen or the concurrent-modification
    guard. We hook the vault screen to (a) prove it ran by observing its call, and
    (b) mutate the file on disk from inside it — after edit_file's initial read but
    before the guarded re-read. The guard must then abort the write: no backup, the
    intruder's bytes stand, the edit is not applied."""
    om = _make_om(tmpdir)
    f = _owned_file(om, tmpdir, 'cg.txt', 'a\nb\n')

    def _mutate_then_pass(*a, **k):
        f.write_bytes(b'INTRUDER\n')   # simulate a concurrent write between read and commit
        return None                     # vault screen: no objection to the content

    with patch('agent_cascade.operation_manager.file_operations._screen_vault_write',
               side_effect=_mutate_then_pass) as screen:
        result = om.edit_file(str(f), 'coder', 'a', 'A', 'exact', pre_approved=True)

    screen.assert_called_once()                    # vault screen still ran under pre_approved
    assert 'concurrent' in result.lower(), result  # guard aborted the write
    assert f.read_bytes() == b'INTRUDER\n'         # intruder's bytes stand; edit not applied


# ── serial batch application (engine _execute_file_mutation_batch) ─────────────

class _BatchHarness(ToolExecMixin):
    """Drives the REAL ``_execute_file_mutation_batch`` against a REAL OperationManager
    so the per-member ``edit_file`` calls hit real files and produce real results. Only
    the detect / placeholder / append collaborators are overridden; the batch body and
    emit path are production code."""

    def __init__(self, om):
        self.pool = SimpleNamespace(operation_manager=om)

    def _detect_tool(self, out):
        return (True, out['name'], out['args'], '')

    def _resolve_placeholders(self, tool_args, inst_name, tool_name):
        return tool_args           # identity — no {placeholder} machinery under test

    def _append_and_log(self, instance, message, **kwargs):
        return None                # keep the conversation-log side effect out of the witness

    def run(self, members, inst_name='coder'):
        turn_output = [{'name': 'edit_file', 'args': m} for m in members]
        response, executed_tools, already_emitted = [], [], set()
        self._execute_file_mutation_batch(
            SimpleNamespace(), inst_name, list(range(len(members))),
            turn_output, response, executed_tools, already_emitted)
        return response


def test_serial_batch_three_members_three_results_middle_failure_isolated(tmpdir):
    """3 same-file members -> 3 distinct per-edit results. The MIDDLE member does not
    match; only IT reports an error. Members 1 and 3 stay applied (asserted via file
    state) — a later member failing does not roll back earlier ones."""
    om = _make_om(tmpdir)
    f = _owned_file(om, tmpdir, 'b3.txt', 'a\nb\nc\n')
    h = _BatchHarness(om)
    members = [
        {'path': str(f), 'old_content': 'a', 'new_content': 'A', 'match_mode': 'exact'},
        {'path': str(f), 'old_content': 'ZZZ', 'new_content': 'X', 'match_mode': 'exact'},  # no match
        {'path': str(f), 'old_content': 'c', 'new_content': 'C', 'match_mode': 'exact'},
    ]
    contents = [m.content for m in h.run(members)]
    assert len(contents) == 3
    assert len(set(contents)) == 3, 'each member must keep its own result'
    assert 'OK' in contents[0] and 'OK' in contents[2]
    first = next(ln.strip() for ln in contents[1].splitlines() if ln.strip()).lower()
    assert first.startswith(('error:', 'rejected:', 'failed:', 'invalid:')), contents[1]
    # Members 1 and 3 applied; member 2 did not corrupt anything.
    assert f.read_text(encoding='utf-8') == 'A\nb\nC\n'


def test_batch_surfaces_guard_reason_on_lead_result(tmpdir):
    """When the (unowned) batch's single approval returns a non-empty reason, that
    reason is prefixed on result #0 only — the model sees WHY the guard approved —
    and every member is still applied."""
    om = _make_om(tmpdir)
    f = Path(tmpdir, 'b1.txt')
    f.write_text('a\nb\n', encoding='utf-8')   # unowned -> approval gate entered
    om._is_auto_approved = Mock(return_value=False)
    om.request_user_approval = Mock(return_value=(True, 'guard: looks safe'))

    h = _BatchHarness(om)
    members = [
        {'path': str(f), 'old_content': 'a', 'new_content': 'A', 'match_mode': 'exact'},
        {'path': str(f), 'old_content': 'b', 'new_content': 'B', 'match_mode': 'exact'},
    ]
    response = h.run(members, inst_name='coder')
    assert 'Approval (coder): guard: looks safe' in response[0].content
    assert all('Approval (' not in m.content for m in response[1:])
    assert f.read_text(encoding='utf-8') == 'A\nB\n'


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
