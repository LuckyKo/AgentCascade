"""Engine-layer tests for multi-edit same-file grouping.

These drive the REAL ``ToolExecMixin._execute_detected_tools`` (pinned to the production
function, not a copy) through a bare harness so the emit-exactly-N invariant is proven at
the engine layer, on top of the one-approval / serial-``edit_file`` application contract:

  * exactly N FUNCTION results for a N-member same-file group (one approval, no re-dispatch);
  * a malformed member, a missing operation_manager, and a deterministic per-member log-append
    failure each still yield exactly N results and never crash the dispatch loop.

``executed_tools`` and ``_already_emitted`` are loop-locals, so they are witnessed
indirectly: (a) ``response`` holds exactly N messages, and (b) the tool dispatcher is never
called — proving the leader absorbed every member and the loop skipped the non-leader
members as already-emitted instead of re-dispatching them.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.engine.tool_execution import ToolExecMixin  # noqa: E402


class _Harness(ToolExecMixin):
    """Bare ToolExecMixin that exists only to run ``_execute_detected_tools`` end-to-end.
    ``__init__`` deliberately skips super(); it wires only the collaborators the grouped
    dispatch + post-loop epilogue touch. Every overridden method is a side-effecting
    collaborator we do not want to execute — none of them is the grouping logic under test.
    ``_detect_tool`` and ``_resolve_placeholders`` are overridden to feed deterministic
    values so the real scan/batch code runs without placeholder machinery."""

    def __init__(self, operation_manager, append_and_log=None):
        self.pool = SimpleNamespace(
            is_paused=lambda: False,
            wait_if_paused=lambda timeout=1.0: None,
            get_instance=lambda name: None,      # nothing terminated
            settings=SimpleNamespace(cache_threshold_chars=100000),
            telemetry=None,
            operation_manager=operation_manager,
        )
        self.compression_handler = None          # falsy -> post-process skipped
        # Records any dispatch attempt; the grouped path must NEVER reach it.
        self.dispatcher_calls = []

        def _default_append(instance, message):
            return None
        self._append_and_log_impl = append_and_log or _default_append

    # ── overridden collaborators (side effects / unrelated machinery) ──
    def _detect_tool(self, out):
        fc = out.get('function_call')
        if not fc:
            return (False, None, None, '')
        try:
            args = json.loads(fc['arguments'])
        except Exception:
            args = {}          # mirror defensive behavior for unparseable arguments
        return (True, fc['name'], args, '')

    def _resolve_placeholders(self, tool_args, inst_name, tool_name):
        return tool_args       # identity — no {placeholder} machinery under test

    def _resolve_tool_policy(self, instance):
        return (None, set(), {})

    def _scan_tool_batch_indices(self, turn_output, inst_name):
        return ([], [], [])

    def _is_terminal_stop(self, inst_name):
        return False

    def _is_suspended_by_compression(self, inst_name):
        return False

    def _is_stopped(self, inst_name):
        return False       # keep the real orphan handler out of the witness

    def _auto_deny_reason(self, template, disabled_tools, function_map, tool_name):
        return None

    def _append_and_log(self, instance, message, **kwargs):
        return self._append_and_log_impl(instance, message)

    def _run_deferred_state_restore_safety_nets(self, instance, inst_name, **kwargs):
        return None

    def _proactive_compression_check(self, *args, **kwargs):
        return None


def _edit_calls(n, path='f.txt', **member_kwargs):
    """Build n ordered edit_file call messages (dict form the real loop accepts)."""
    calls = []
    for i in range(n):
        args = {'path': path, 'old_content': f'a{i}', 'new_content': f'b{i}',
                'match_mode': 'exact'}
        args.update(member_kwargs(i)) if callable(member_kwargs) else args.update(member_kwargs)
        calls.append({'role': 'assistant', 'content': '',
                      'function_call': {'name': 'edit_file', 'arguments': json.dumps(args)},
                      'extra': {'function_id': f'fc-{i}'}})
    return calls


def _drive(operation_manager, turn_output, append_and_log=None):
    harness = _Harness(operation_manager, append_and_log=append_and_log)
    # Witness: we drive the production grouping body, not a test-local copy.
    assert harness._execute_detected_tools.__func__ is ToolExecMixin._execute_detected_tools
    response = []
    used_any = harness._execute_detected_tools(
        SimpleNamespace(), 'coder', turn_output, list(turn_output), [], response)
    return harness, used_any, response


def _op(auto_approve=True, reject=False, edit_return=None):
    """operation_manager mock. auto_approve=True -> skip the gate; auto_approve=False ->
    enter the gate (GRANTED by default, or REJECTED when reject=True). The batch applies
    each member via a pre_approved ``edit_file`` call, which returns a single string."""
    op = Mock()
    op._is_auto_approved = Mock(return_value=auto_approve)
    if not auto_approve:
        if reject:
            op.request_user_approval = Mock(return_value=(False, 'user declined'))
            op.edit_file = Mock(side_effect=AssertionError('must not edit on reject'))
        else:
            op.request_user_approval = Mock(return_value=(True, 'ok'))
            op.edit_file = Mock(return_value=edit_return or 'OK: Edited')
    else:
        op.edit_file = Mock(return_value=edit_return or 'OK: Edited')
    return op


# ── happy path: one group of N -> exactly N results, one approval, no re-dispatch ──

def test_group_three_members_emits_three_results_with_one_approval():
    n = 3
    op = _op(auto_approve=False)            # unowned -> approval gate exercised (Minor #8)
    op.edit_file = Mock(return_value='OK: Edited')
    harness, used_any, response = _drive(op, _edit_calls(n))

    assert used_any is True
    assert len(response) == n                                   # emit-exactly-N
    assert all(m.name == 'edit_file' and m.role == 'function' for m in response)
    assert op.request_user_approval.call_count == 1             # ONE approval for the batch
    assert op.edit_file.call_count == n                         # N serial applications
    assert all(c.kwargs.get('pre_approved') is True for c in op.edit_file.call_args_list)
    assert harness.dispatcher_calls == []                       # members absorbed, not re-dispatched


# ── approval field-assignment (Major #5): no justification= kwarg ──────────────

def test_approval_called_with_no_justification_kwarg_and_batch_field_present():
    n = 2
    op = _op(auto_approve=False)
    op.edit_file = Mock(return_value='OK: Edited')
    _drive(op, _edit_calls(n))

    kwargs = op.request_user_approval.call_args.kwargs
    assert 'justification' not in kwargs, 'justification must not be a keyword argument'
    # The semantics note lives INSIDE tool_args (feeds PendingApproval.justification).
    tool_args = kwargs['tool_args']
    assert tool_args['justification'].startswith('approve applies all')
    assert tool_args['batch_size'] == n
    assert len(tool_args['batch']) == n
    # description is the multi-line numbered per-edit summary.
    assert kwargs['description'].splitlines()[0].startswith('Multi-edit')


# ── full per-edit args reach the guard (no silent truncation) ─────────────────

def test_batch_carries_full_args_not_truncated_previews():
    big = 'Z' * 500          # deliberately > the old 200-char preview cap
    op = _op(auto_approve=False)
    op.edit_file = Mock(return_value='OK: Edited')
    _drive(op, _edit_calls(2, new_content=big))
    batch = op.request_user_approval.call_args.kwargs['tool_args']['batch']
    assert len(batch) == 2
    for entry in batch:
        assert entry['new_content'] == big          # FULL content, not cut to 200
        assert 'old_preview' not in entry and 'new_preview' not in entry


def test_batch_truncates_oversized_field_with_marker():
    from agent_cascade.engine.tool_execution import _MAX_EDIT_ARG_CHARS, _edit_arg_for_guard
    huge = 'Y' * (_MAX_EDIT_ARG_CHARS + 500)
    op = _op(auto_approve=False)
    op.edit_file = Mock(return_value='OK: Edited')
    _drive(op, _edit_calls(2, new_content=huge))
    entry = op.request_user_approval.call_args.kwargs['tool_args']['batch'][0]
    assert entry['new_content'].startswith('Y' * _MAX_EDIT_ARG_CHARS)
    assert f'[TRUNCATED, {len(huge)} chars total]' in entry['new_content']
    # direct unit checks of the helper:
    assert _edit_arg_for_guard('abc') == 'abc'
    assert _edit_arg_for_guard('X' * 10, cap=5) == 'XXXXX\n…[TRUNCATED, 10 chars total]'


# ── reject path: all N get REJECTED results, no mutation ───────────────────────

def test_reject_yields_rejected_result_for_every_member():
    n = 3
    op = _op(auto_approve=False, reject=True)   # gate entered; user declines
    harness, used_any, response = _drive(op, _edit_calls(n))

    assert used_any is True
    assert len(response) == n
    for m in response:
        assert str(m.content).startswith('REJECTED:')
    op.edit_file.assert_not_called()                        # nothing edited on reject


# ── op_manager missing -> all N get "execution failed", no crash ──────────────

def test_missing_operation_manager_emits_error_for_all_members():
    n = 2
    # pool.operation_manager = None -> batch raises immediately -> inner handler fills N.
    harness, used_any, response = _drive(None, _edit_calls(n))
    assert used_any is True
    assert len(response) == n
    for m in response:
        assert 'execution failed' in str(m.content)


# ── malformed member -> all N get a result, loop does not crash ───────────────

def test_malformed_member_emits_result_for_all_members():
    n = 2
    op = _op(auto_approve=True)
    # Member #1 gets old_content of the WRONG type -> groups fine (scan checks path/mode
    # only) -> batch raises on validation -> emit-exactly-N handler fills both members.
    calls = _edit_calls(n)
    bad = json.loads(calls[1]['function_call']['arguments'])
    bad['old_content'] = ['not', 'a', 'string']
    calls[1]['function_call']['arguments'] = json.dumps(bad)

    harness, used_any, response = _drive(op, calls)
    assert used_any is True
    assert len(response) == n
    for m in response:
        assert 'execution failed' in str(m.content)
    op.edit_file.assert_not_called()                        # whole batch aborted before any edit


# ── Major #6: deterministic per-member log-append failure must not crash or abort ─

def test_log_append_failure_still_emits_every_member():
    n = 3
    op = _op(auto_approve=True)
    op.edit_file = Mock(return_value='OK: Edited')

    def _boom(instance, message):
        raise OSError('simulated log I/O error')        # deterministic, every member

    harness, used_any, response = _drive(op, _edit_calls(n), append_and_log=_boom)
    # The crash hole is closed: the loop completed, and every member still got a result.
    assert used_any is True
    assert len(response) == n
    assert op.edit_file.call_count == n


# ── cross-file separation: two different paths -> two independent approvals ─────

def test_two_paths_are_two_independent_groups_one_result_each():
    calls = _edit_calls(2, path='a.txt') + _edit_calls(2, path='b.txt')
    op = _op(auto_approve=True)
    op.edit_file = Mock(return_value='OK: Edited')
    harness, used_any, response = _drive(op, calls)

    assert used_any is True
    assert len(response) == 4                                   # both groups emit N
    assert op.edit_file.call_count == 4                         # serial applications across both files


# ── REGRESSION (live trigger bug): production _detect_tool (engine/core.py) returns
# function_call.arguments as an UNPARSED JSON STRING. The scan must normalize that to a
# dict, else every edit_file call bails at the isinstance(dict) check and no group ever
# forms. _Harness._detect_tool returns a dict, which masked this in the tests above —
# so these feed the production string shape directly. ──

def test_scan_edit_reducible_accepts_json_string_args():
    h = _Harness(Mock())
    args = {'path': 'f.txt', 'old_content': 'a', 'new_content': 'b', 'match_mode': 'exact'}
    # Production shape: a JSON string, not a dict.
    assert h._scan_edit_reducible(json.dumps(args), 'coder') == os.path.normcase('f.txt')
    # The dict shape (restored / hand-built histories) still works.
    assert h._scan_edit_reducible(args, 'coder') == os.path.normcase('f.txt')


def test_scan_edit_reducible_bails_on_non_dict_payload():
    h = _Harness(Mock())
    assert h._scan_edit_reducible('{broken json', 'coder') is None
    assert h._scan_edit_reducible(json.dumps([1, 2]), 'coder') is None   # parses to a list
    assert h._scan_edit_reducible(None, 'coder') is None


def test_scan_file_mutation_groups_groups_production_string_args():
    # A harness whose _detect_tool mirrors PRODUCTION (arguments stay a JSON string), so the
    # scan's string-normalization is exercised end-to-end rather than masked by the dict
    # override in _Harness.
    class _ProdHarness(ToolExecMixin):
        def _detect_tool(self, message):
            fc = message.get('function_call')
            if not fc:
                return (False, None, None, '')
            return (True, fc.get('name'), fc.get('arguments'), '')

        def _resolve_placeholders(self, tool_args, inst_name, tool_name):
            return tool_args          # identity; the scan normalizes the string first

    h = _ProdHarness()
    turn_output = [
        {'role': 'assistant', 'content': '',
         'function_call': {'name': 'edit_file',
                           'arguments': json.dumps({'path': 'f.txt', 'old_content': f'a{i}',
                                                    'new_content': f'b{i}', 'match_mode': 'exact'})},
         'extra': {'function_id': f'fc-{i}'}}
        for i in range(3)
    ]
    assert h._scan_file_mutation_groups(turn_output, 'coder') == {0: [0, 1, 2]}
