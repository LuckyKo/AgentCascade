"""Regression tests for shell_cmd auto-denial of `| head` / `| tail` pipe stages.

Covers:
- The `_detect_head_tail_pipe` helper directly (must-deny / must-not-deny cases).
- End-to-end proof that both `_execute_sync` and `_launch_async` return the denial
  string and NEVER call `operation_manager.execute_shell_command` / `tracker.launch`.

No LLM or network connections required.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def shell_cmd_tool():
    from agent_cascade.tools.custom.shell_cmd import ShellCmd
    return ShellCmd()


class TestDetectHeadTailPipe:
    """Direct unit tests of the detection helper."""

    @pytest.mark.parametrize("command", [
        "ls -R | head -20",
        "dir /s | tail -5",
        "git log --oneline | head -10",
        "cd /workspace && git log --oneline | head -10",  # cd prefix
        "somecmd | HEAD -n 5",                            # case-insensitive
        "foo | tail -f",                                  # flag after name
        "bar | tail --lines=10",                          # long-flag form
        "a | b | head -3",                                # multi-stage, head not first stage
    ])
    def test_must_deny(self, command):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        result = ShellCmd._detect_head_tail_pipe(command)
        assert result is not None, f"expected denial for {command!r}"
        assert result.startswith("DENIED"), f"denial should be actionable: {result!r}"

    @pytest.mark.parametrize("command", [
        "ls -la",                                        # no pipe
        "git show-ref --head",                           # git subcommand, no pipe
        "find . -name '*.py' | grep 'test'",             # other safe pipe
        "find . -type f | wc -l",
        "git diff --stat | sort",
        "dir /s | findstr 'python'",
    ])
    def test_must_not_deny(self, command):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        result = ShellCmd._detect_head_tail_pipe(command)
        assert result is None, f"expected no denial for {command!r}, got: {result!r}"

    def test_empty_and_none(self):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        assert ShellCmd._detect_head_tail_pipe("") is None


class TestSyncDenialEndToEnd:
    """_execute_sync must return the denial and never call execute_shell_command."""

    @pytest.mark.parametrize("command", [
        "ls -R | head -20",
        "cd /workspace && git log --oneline | tail -5",
    ])
    def test_sync_denies_and_does_not_execute(self, shell_cmd_tool, command):
        mock_pool = MagicMock()
        mock_pool.llm_cfg = {}  # dict so .get('shell_char_limit', default) returns default
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool._execute_sync(
            agent_name='test_agent', command=command,
            justification='test', cwd=None, timeout=None,
        )

        assert result.startswith("DENIED"), f"expected denial string, got: {result!r}"
        mock_pool.operation_manager.execute_shell_command.assert_not_called()


class TestAsyncDenialEndToEnd:
    """_launch_async must return the denial and never call tracker.launch."""

    @pytest.mark.parametrize("command", [
        "ls -R | head -20",
        "git log --oneline | tail -5",
    ])
    def test_async_denies_and_does_not_launch(self, shell_cmd_tool, command):
        mock_pool = MagicMock()
        mock_pool.llm_cfg = {}  # dict so .get('shell_char_limit', default) returns default
        tracker = MagicMock()
        mock_pool._async_shell_tracker = tracker
        shell_cmd_tool.agent_pool = mock_pool
        shell_cmd_tool.agent_name = 'test_agent'

        result = shell_cmd_tool._launch_async(
            agent_name='test_agent', command=command,
            justification='test', cwd=None, timeout=None, heartbeat_interval=-1,
        )

        assert result.startswith("DENIED"), f"expected denial string, got: {result!r}"
        tracker.launch.assert_not_called()
