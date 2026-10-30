"""Tests for the BUG_0182 "prefer built-in tool" hint reachability fix.

BUG_0182: the "prefer built-in tool" hint fired for a read-only shell command
even when the command targeted a path the built-in file tools cannot reach
(outside the allowed RO roots), leaving the agent stuck in an out-of-workspace
catch-22. The fix suppresses the hint when a concrete absolute path in the
command is outside every allowed root.

The hint block in ``SecurityAdvisorHandler._execute_check`` combines two
module-level primitives:
  - ``_builtin_tool_hint`` (existing) -> the built-in tool to prefer, or None
  - ``_shell_cmd_targets_out_of_ro`` (new) -> True if any absolute path is out-of-RO
These tests exercise both primitives and a 4-line mirror of the production
decision (same two primitives, same combination) so the 8 cases from the fix
plan are asserted directly.
"""
from pathlib import Path

from agent_cascade.security_handler import (
    _builtin_tool_hint,
    _get_session_extra_roots,
    _shell_cmd_targets_out_of_ro,
)
from agent_cascade.settings import DEFAULT_WORKSPACE


def _effective_hint(command, extra_roots=None):
    """Mirror of the production hint-block decision (same two primitives)."""
    tool = _builtin_tool_hint(command)
    if tool is not None and _shell_cmd_targets_out_of_ro(command, extra_roots):
        tool = None
    return tool


# A path guaranteed OUTSIDE the allowed RO roots: a sibling of the live workspace
# (the workspace root is RO root [1]; a sibling is not under it).
_OUT = str(Path(DEFAULT_WORKSPACE).parent / '__bug0182_outside_ro__')
# A path guaranteed INSIDE the workspace root (RO root [1]).
_IN = str(Path(DEFAULT_WORKSPACE) / 'subdir')


class TestHintReachability:
    """The 8 cases from the BUG_0182 fix plan."""

    def test_out_of_workspace_dir_suppressed(self):
        cmd = f'dir /b {_OUT}'
        assert _builtin_tool_hint(cmd) == 'list_dir'
        assert _shell_cmd_targets_out_of_ro(cmd, None) is True
        assert _effective_hint(cmd) is None

    def test_in_workspace_dir_fires(self):
        cmd = f'dir /b {_IN}'
        assert _builtin_tool_hint(cmd) == 'list_dir'
        assert _shell_cmd_targets_out_of_ro(cmd, None) is False
        assert _effective_hint(cmd) == 'list_dir'

    def test_dir_no_arg_fires(self):
        cmd = 'dir'
        assert _builtin_tool_hint(cmd) == 'list_dir'
        assert _shell_cmd_targets_out_of_ro(cmd, None) is False
        assert _effective_hint(cmd) == 'list_dir'

    def test_python_open_out_of_workspace_path_extracted(self):
        # The absolute path embedded in the -c code is extracted and is out-of-RO.
        cmd = f"python -c \"print(open(r'{_OUT}\\f.txt').read())\""
        assert _shell_cmd_targets_out_of_ro(cmd, None) is True

    def test_write_command_falls_to_guard(self):
        cmd = 'dir > out.txt'
        # A redirect to a real file is a write -> not strictly read-only -> no hint.
        assert _builtin_tool_hint(cmd) is None
        assert _effective_hint(cmd) is None

    def test_select_string_out_of_workspace_suppressed(self):
        cmd = f'select-string -Pattern x {_OUT}\\file.txt'
        assert _builtin_tool_hint(cmd) == 'grep'
        assert _shell_cmd_targets_out_of_ro(cmd, None) is True
        assert _effective_hint(cmd) is None

    def test_in_workspace_grep_fires(self):
        cmd = f'grep -r foo {_IN}'
        assert _builtin_tool_hint(cmd) == 'grep'
        assert _shell_cmd_targets_out_of_ro(cmd, None) is False
        assert _effective_hint(cmd) == 'grep'

    def test_malformed_command_no_crash(self):
        # No absolute path token -> conservative -> the hint still fires.
        cmd = 'dir --bogus-flag'
        assert _shell_cmd_targets_out_of_ro(cmd, None) is False
        assert _effective_hint(cmd) == 'list_dir'


class TestOutOfRoPredicate:
    """Direct behaviour of _shell_cmd_targets_out_of_ro."""

    def test_no_absolute_path_is_conservative(self):
        assert _shell_cmd_targets_out_of_ro('dir', None) is False
        assert _shell_cmd_targets_out_of_ro('', None) is False

    def test_multiple_args_any_out_of_ro_suppresses(self):
        cmd = f'grep -r foo {_IN} {_OUT}'
        assert _shell_cmd_targets_out_of_ro(cmd, None) is True

    def test_error_inducing_extra_roots_is_conservative(self):
        # A non-iterable extra_roots makes _get_allowed_file_roots raise; the
        # helper must swallow it and return False (do not suppress the hint).
        cmd = f'dir /b {_OUT}'
        assert _shell_cmd_targets_out_of_ro(cmd, 42) is False

    def test_extra_roots_widen_the_allowed_set(self):
        # When the out-of-RO path is added as an extra root, it is no longer out-of-RO.
        cmd = f'dir /b {_OUT}'
        assert _shell_cmd_targets_out_of_ro(cmd, None) is True
        assert _shell_cmd_targets_out_of_ro(cmd, [_OUT]) is False


class TestGetSessionExtraRoots:
    """Best-effort _get_session_extra_roots behaviour."""

    def test_none_pool_returns_none(self):
        assert _get_session_extra_roots(None) is None

    def test_pool_without_operation_manager_returns_none(self):
        class _Pool:
            pass
        assert _get_session_extra_roots(_Pool()) is None

    def test_collects_ro_and_rw_folders(self):
        class _Om:
            extra_work_folders_ro = ['r1']
            extra_work_folders_rw = ['w1']

        class _Pool:
            operation_manager = _Om()
        assert _get_session_extra_roots(_Pool()) == ['r1', 'w1']


# ── Integration: guard the real _execute_check wiring (the BUG_0182 fix site) ──
# Mirrors the fake-pool + fake-engine harness from tests/test_builtin_tool_hint.py
# so the observable is the LLM guard run count: 0 when the hint auto-approves,
# 1 when the hint is suppressed and the command falls through to the LLM guard.

import threading
import types
from unittest.mock import MagicMock

import pytest

from agent_cascade.security_handler import SecurityAdvisorHandler

_PROMPT = ('PROMPT tool={tool_name} desc={description} '
           'args={arguments} os={os_info} ws={workspace_info}')


class _FakeOperationManager:
    """Records the verdicts the guard delivers (the observable effect)."""

    def __init__(self, base_dir=None):
        self.base_dir = base_dir if base_dir is not None else 'N:\\work\\WD\\AgentWorkspace'
        self.extra_work_folders_ro = []
        self.extra_work_folders_rw = []
        self.approvals = []
        self.rejections = []

    def user_approve(self, rid, reason=''):
        self.approvals.append((rid, reason))

    def user_reject(self, rid, message=''):
        self.rejections.append((rid, message))


class _FakePool:
    def __init__(self, base_dir=None):
        self.operation_manager = _FakeOperationManager(base_dir)
        self.instance_state = {}
        self._execution = MagicMock()
        self._execution._state_lock = threading.Lock()
        self.stopped = False
        self.active_stack_removed = []

    def get_instance(self, name):
        return None

    def get_template(self, name):
        return None

    def active_stack_remove(self, name):
        self.active_stack_removed.append(name)


def _drive_execute_check(monkeypatch, command, base_dir=None):
    """Drive the real _execute_check for a shell_cmd; return (guard_runs, op_manager).

    Installs the same fake engine + stubbed LLM-adjacent helpers as
    test_builtin_tool_hint.py so the LLM path runs exactly once (counted) and
    returns early without heavy work.
    """
    counters = {'run': 0}
    verdict_text = '[YES] safe [END]'

    class _FakeEngine:
        def __init__(self, pool):
            pass

        def run(self, instance):
            counters['run'] += 1

            def _gen():
                yield ({'role': 'assistant', 'content': verdict_text}, False)
            return _gen()

        def _telemetry(self):
            return None

        def save_before_slot_yield(self, *a, **k):
            pass

        @staticmethod
        def _discard_stale_permit(*a, **k):
            pass

    monkeypatch.setattr('agent_cascade.execution_engine.ExecutionEngine', _FakeEngine)
    monkeypatch.setattr('agent_cascade.security_handler.yield_caller_slot',
                        lambda *a, **k: False)
    monkeypatch.setattr(
        'agent_cascade.security_handler.security_reuse.acquire_security_agent',
        lambda engine, **kw: (types.SimpleNamespace(conversation=[]),
                              False, kw.get('fallback_name', 'Security_x')))
    monkeypatch.setattr('agent_cascade.compression.helpers.extract_instance_output',
                        lambda conv, key: verdict_text)
    monkeypatch.setattr('agent_cascade.api_integration.broadcast_stream_update',
                        lambda **kw: (0.0, 0))

    pool = _FakePool(base_dir)
    app = type('App', (), {})()
    handler = SecurityAdvisorHandler(pool, {'session_name': 'Maine'}, app,
                                     MagicMock(), lambda *a, **k: None)
    ap = {'request_id': 'rid', 'tool_name': 'shell_cmd',
          'description': 'probe', 'tool_args': {'command': command}}
    handler._execute_check(ap, None, 'rid', True, 'Maine', 'coder',
                           _PROMPT, 60.0, 3600.0)
    return counters['run'], pool.operation_manager


class TestExecuteCheckHintWiring:
    """Guard the real _execute_check wiring (the BUG_0182 fix site)."""

    @pytest.fixture(autouse=True)
    def _fresh_cache(self, monkeypatch):
        from agent_cascade.security_handler import _get_security_approval_cache
        cache = _get_security_approval_cache(None)
        cache.clear()
        yield cache
        cache.clear()

    def test_in_workspace_hint_fires_no_guard(self, monkeypatch):
        # In-workspace path: the hint auto-approves (no LLM guard run) and a
        # [SECURITY_HINT] approval is delivered to the operation manager.
        runs, om = _drive_execute_check(monkeypatch, f'Get-ChildItem {_IN}')
        assert runs == 0
        assert any(r.startswith('[SECURITY_HINT]') for (_, r) in om.approvals)

    def test_out_of_ro_hint_suppressed_guard_runs(self, monkeypatch):
        # Out-of-RO path: the hint is suppressed, so the command falls through
        # to the LLM guard (exactly one run) and NO [SECURITY_HINT] approval
        # is delivered.
        runs, om = _drive_execute_check(monkeypatch, f'Get-ChildItem {_OUT}')
        assert runs == 1
        assert not any(r.startswith('[SECURITY_HINT]') for (_, r) in om.approvals)


class TestGetSessionExtraRootsBaseDir:
    """The base_dir-differs-from-DEFAULT_WORKSPACE branch of _get_session_extra_roots."""

    def test_base_dir_sibling_is_prepended(self):
        sibling = str(Path(DEFAULT_WORKSPACE).parent / '__bug0182_sibling_ws__')

        class _Om:
            base_dir = sibling
            extra_work_folders_ro = []
            extra_work_folders_rw = []

        class _Pool:
            operation_manager = _Om()
        roots = _get_session_extra_roots(_Pool())
        assert roots is not None
        assert any(Path(r).resolve() == Path(sibling).resolve() for r in roots)

    def test_base_dir_equal_to_default_not_prepended(self):
        class _Om:
            base_dir = str(DEFAULT_WORKSPACE)
            extra_work_folders_ro = []
            extra_work_folders_rw = []

        class _Pool:
            operation_manager = _Om()
        # No ro/rw folders and base_dir == DEFAULT_WORKSPACE -> no extra roots.
        assert _get_session_extra_roots(_Pool()) is None
