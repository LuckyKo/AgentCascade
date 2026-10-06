"""Regression tests for shell_cmd `| head` / `| tail` pipe-stage STRIPPING (BUG_0057).

Covers:
- The `_strip_head_tail_pipes` helper directly (must-strip / must-not-strip cases).
- End-to-end proof that both `_execute_sync` and `_launch_async` strip the stages,
  pass the rewritten command to execution, and emit the strip note.

No LLM or network connections required.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def shell_cmd_tool():
    from agent_cascade.tools.custom.shell_cmd import ShellCmd
    return ShellCmd()


class TestStripHeadTailPipes:
    """Direct unit tests of the stripping helper."""

    @pytest.mark.parametrize('command,expected_stripped', [
        ('git log --oneline | tail -5', 'git log --oneline'),
        ('cmd | head -n 5 | tail -2', 'cmd'),
        ('cd /tmp && git log | tail -3', 'cd /tmp && git log'),
        ('echo x | head -5', 'echo x'),
        ('ls -R | head -20', 'ls -R'),
        ('dir /s | tail -5', 'dir /s'),
        ('somecmd | HEAD -n 5', 'somecmd'),           # case-insensitive
        ('foo | tail --lines=10', 'foo'),             # long-flag form
        ('a | b | head -3', 'a | b'),                 # multi-stage, head not first stage
        ('git log | grep x | tail -2', 'git log | grep x'),  # middle non-head/tail preserved
    ])
    def test_must_strip(self, command, expected_stripped):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        result, was_stripped = ShellCmd._strip_head_tail_pipes(command)
        assert was_stripped is True, f"expected strip for {command!r}"
        assert result == expected_stripped, (
            f"expected {expected_stripped!r} for {command!r}, got {result!r}")

    @pytest.mark.parametrize('command', [
        'ls -la',                                      # no pipe
        'head -5 file',                                # first stage — never stripped
        'tail -f /var/log/syslog',                     # first stage
        'git show-ref --head',                         # git arg, not a pipe stage
        "find . -name '*.py' | grep 'test'",           # other safe pipe
        'find . -type f | wc -l',                      # wc is not head/tail
        'git diff --stat | sort',                      # sort is not head/tail
        "dir /s | findstr 'python'",                   # findstr is not head/tail
    ])
    def test_must_not_strip(self, command):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        result, was_stripped = ShellCmd._strip_head_tail_pipes(command)
        assert was_stripped is False, f"expected no strip for {command!r}"
        assert result == command, f"command should be unchanged: {result!r} != {command!r}"

    def test_empty_command(self):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        result, was_stripped = ShellCmd._strip_head_tail_pipes('')
        assert result == ''
        assert was_stripped is False

    def test_idempotency(self):
        """Stripping an already-stripped command is a no-op."""
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        first, stripped1 = ShellCmd._strip_head_tail_pipes('git log --oneline | tail -5')
        assert stripped1 is True
        second, stripped2 = ShellCmd._strip_head_tail_pipes(first)
        assert stripped2 is False
        assert second == first

    def test_malformed_leading_pipe(self):
        """A leading-pipe command like '| head' should not crash; returns unchanged."""
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        result, was_stripped = ShellCmd._strip_head_tail_pipes('| head')
        # stages[0] is empty string, stages[1:] has 'head' → stripped_body would be ''
        # → guard returns original unchanged
        assert was_stripped is False
        assert result == '| head'

    def test_all_pipe_stages_head_tail(self):
        """echo x | head -5 | tail -3 → both stripped, leaves just 'echo x'."""
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        result, was_stripped = ShellCmd._strip_head_tail_pipes('echo x | head -5 | tail -3')
        assert was_stripped is True
        assert result == 'echo x'


class TestStripHeadTailPipesQuotedPipes:
    """BUG_0060: a '|' inside a quoted string literal is argument data, not a pipeline
    separator. The naive cmd.split('|') used to split these in half and the rejoin then
    produced a corrupted command (e.g. a Python SyntaxError). These must be left untouched."""

    @pytest.mark.parametrize('command', [
        # double-quoted python -c with '| head' inside the string literal
        'python -c "s=\\"| head\\"; print(s)"',
        # single-quoted variant from the original bug report
        "python -c \"s='git log 2>&1 | head -5'; print(s)\"",
        # a real pipeline whose FIRST stage carries a quoted pipe must still strip the tail
        # only if there is an unquoted trailing head/tail; here none → unchanged
        "echo 'a|b' | grep 'c|d'",
    ])
    def test_quoted_pipe_not_stripped(self, command):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        result, was_stripped = ShellCmd._strip_head_tail_pipes(command)
        assert was_stripped is False, f"quoted pipe wrongly treated as a stage: {command!r}"
        assert result == command, f"command corrupted: {result!r} != {command!r}"

    def test_quoted_pipe_plus_real_trailing_tail(self):
        """A quoted pipe in an early stage coexists with a genuine trailing '| tail' —
        only the real (unquoted) tail is stripped; the quoted one survives intact."""
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        command = "python -c \"print('x|y')\" | tail -3"
        result, was_stripped = ShellCmd._strip_head_tail_pipes(command)
        assert was_stripped is True
        # the quoted 'x|y' must be preserved verbatim in the surviving command
        assert "print('x|y')" in result, f"quoted pipe lost: {result!r}"
        assert '| tail' not in result, f"trailing tail not stripped: {result!r}"


class TestSyncStripEndToEnd:
    """_execute_sync must strip head/tail, pass rewritten command to execute_shell_command."""

    @pytest.mark.parametrize('command,expected_cmd', [
        ('ls -R | head -20', 'ls -R'),
        ('cd /workspace && git log --oneline | tail -5', 'cd /workspace && git log --oneline'),
    ])
    def test_sync_strips_and_executes(self, shell_cmd_tool, command, expected_cmd):
        mock_pool = MagicMock()
        mock_pool.llm_cfg = {}
        mock_pool.operation_manager.execute_shell_command.return_value = 'AUTO-APPROVED: ok\n'
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool._execute_sync(
            agent_name='test_agent', command=command,
            justification='test', cwd=None, timeout=None,
        )

        # The rewritten command must be what reaches execute_shell_command
        call_kwargs = mock_pool.operation_manager.execute_shell_command.call_args.kwargs
        assert call_kwargs['command'] == expected_cmd, (
            f"expected {expected_cmd!r}, got {call_kwargs['command']!r}")
        # Note must be present in the result
        assert '[note] stripped' in result, f"strip note missing from: {result!r}"

    def test_sync_no_strip_no_note(self, shell_cmd_tool):
        """A command without head/tail pipes should not get the note."""
        mock_pool = MagicMock()
        mock_pool.llm_cfg = {}
        mock_pool.operation_manager.execute_shell_command.return_value = 'AUTO-APPROVED: ok\n'
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool._execute_sync(
            agent_name='test_agent', command='ls -la',
            justification='test', cwd=None, timeout=None,
        )

        call_kwargs = mock_pool.operation_manager.execute_shell_command.call_args.kwargs
        assert call_kwargs['command'] == 'ls -la'
        assert '[note] stripped' not in result


class TestAsyncStripEndToEnd:
    """_launch_async must strip head/tail and emit the note in the launched/completed message."""

    def test_async_strips_and_emits_note(self, shell_cmd_tool):
        mock_pool = MagicMock()
        mock_pool.llm_cfg = {}
        tracker = MagicMock()
        # Simulate early completion
        tracker.launch.return_value = (1, 12345, ['line1', 'line2'], True, 0)
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool._launch_async(
            agent_name='test_agent', command='git log --oneline | tail -5',
            justification='test', cwd=None, timeout=None, heartbeat_interval=-1,
        )

        # The stripped command must be what reaches tracker.launch
        call_kwargs = tracker.launch.call_args.kwargs
        assert call_kwargs['command'] == 'git log --oneline', (
            f"expected 'git log --oneline', got {call_kwargs['command']!r}")
        # Note must be present
        assert '[note] stripped' in result, f"strip note missing from: {result!r}"
        # AUTO-APPROVED should be emitted (git log is safe)
        assert 'AUTO-APPROVED' in result

    def test_async_no_strip_no_note(self, shell_cmd_tool):
        mock_pool = MagicMock()
        mock_pool.llm_cfg = {}
        tracker = MagicMock()
        tracker.launch.return_value = (1, 12345, None, False, None)
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool._launch_async(
            agent_name='test_agent', command='git log --oneline',
            justification='test', cwd=None, timeout=None, heartbeat_interval=-1,
        )

        call_kwargs = tracker.launch.call_args.kwargs
        assert call_kwargs['command'] == 'git log --oneline'
        assert '[note] stripped' not in result
