"""Regression tests for shell command cwd resolution using resolve_tool_path.

Tests cover the integration of resolve_tool_path into shell_cmd's cwd handling,
ensuring that working directory resolution behaves consistently with file tools.
No LLM or network connections required.
"""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
# tool_path_resolver lives in AgentCascade (refactor target), add it for imports
sys.path.insert(0, r'N:\work\WD\AgentCascade')

import pytest

from agent_cascade.tools.custom.shell_cmd import ShellCmd
from agent_cascade.utils.tool_path_resolver import resolve_tool_path


@pytest.fixture
def shell_cmd_tool():
    return ShellCmd()


@pytest.fixture
def tool_with_tracker(shell_cmd_tool):
    """Set up a shell_cmd tool with mocked async tracker and fallback resolution mode.

    operation_manager is None so resolve_tool_path uses fallback mode (DEFAULT_WORKSPACE)
    instead of delegating to a mock that would silently accept all paths.
    """
    from agent_cascade.async_shell import AsyncShellTracker
    tracker = AsyncShellTracker(pool=None)
    tracker.launch = MagicMock(return_value=(1, 12345, None, False, None))

    mock_pool = MagicMock()
    mock_pool._async_shell_tracker = tracker
    mock_pool.llm_cfg = {}  # Real dict so .get() works properly
    mock_pool.operation_manager = None  # Force resolve_tool_path fallback mode
    shell_cmd_tool.agent_pool = mock_pool
    shell_cmd_tool.agent_name = 'test_agent'
    return tracker


# ============================================================================
# Test resolve_tool_path behavior (fallback mode, no operation_manager)
# ============================================================================

class TestResolveToolPathFallback:
    """Tests for resolve_tool_path in fallback mode (no agent_pool/operation_manager)."""

    def test_valid_relative_path_resolves_to_workspace(self):
        """Relative path '.' should resolve to the workspace directory."""
        result = resolve_tool_path('.', mode="rw")
        assert result.is_absolute()
        from agent_cascade.settings import DEFAULT_WORKSPACE
        base = Path(DEFAULT_WORKSPACE).resolve()
        assert result == base

    def test_workspace_prefix_stripped_and_resolved(self):
        """Path with /workspace/ prefix should be stripped and resolved correctly."""
        result = resolve_tool_path('/workspace/src/main.py', mode="ro")
        from agent_cascade.settings import DEFAULT_WORKSPACE
        base = Path(DEFAULT_WORKSPACE).resolve()
        expected = (base / 'src/main.py').resolve()
        assert result == expected

    def test_workspace_root_resolves_to_base(self):
        """Path '/workspace' should resolve to the workspace directory."""
        result = resolve_tool_path('/workspace', mode="rw")
        from agent_cascade.settings import DEFAULT_WORKSPACE
        base = Path(DEFAULT_WORKSPACE).resolve()
        assert result == base

    def test_workspace_prefix_without_leading_slash(self):
        """Path 'workspace/src' should also be handled."""
        result = resolve_tool_path('workspace/src', mode="ro")
        from agent_cascade.settings import DEFAULT_WORKSPACE
        base = Path(DEFAULT_WORKSPACE).resolve()
        expected = (base / 'src').resolve()
        assert result == expected

    def test_invalid_path_outside_workspace_raises_value_error(self):
        """Path outside allowed directories should raise ValueError with clear message."""
        # Use a path that's clearly outside workspace
        outside = Path('/tmp/../../../etc/passwd').resolve() if os.name != 'nt' else Path('C:\\Windows\\System32').resolve()

        with pytest.raises(ValueError) as exc_info:
            resolve_tool_path(str(outside), mode="rw")

        assert 'outside the allowed' in str(exc_info.value).lower()

    def test_traversal_escape_raises_value_error(self):
        """Path traversal attempts should raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            resolve_tool_path('../../../../../../etc/passwd', mode="rw")

        assert 'outside the allowed' in str(exc_info.value).lower()

    def test_absolute_path_within_workspace_accepted(self):
        """Absolute path within workspace directory should be accepted."""
        from agent_cascade.settings import DEFAULT_WORKSPACE
        base = Path(DEFAULT_WORKSPACE).resolve()
        abs_path = base / 'some_file.txt'

        result = resolve_tool_path(str(abs_path), mode="ro")
        assert result == abs_path.resolve()


# ============================================================================
# Test shell_cmd integration with resolve_tool_path for cwd handling
# ============================================================================

class TestShellCmdCwdResolution:
    """Tests that shell_cmd uses resolve_tool_path correctly for cwd parameter."""

    def test_valid_relative_cwd_resolves_correctly(self, shell_cmd_tool, tool_with_tracker):
        """Relative cwd '.' should resolve to workspace dir and be passed as resolved path."""
        tracker = tool_with_tracker
        from agent_cascade.settings import DEFAULT_WORKSPACE
        expected_cwd = Path(DEFAULT_WORKSPACE).resolve()

        result = shell_cmd_tool.call(
            '{"command": "pwd", "cwd": ".", "execution_mode": "async", "justification": "test"}'
        )

        assert 'ERROR' not in result
        tracker.launch.assert_called_once()
        launched_cwd = tracker.launch.call_args.kwargs.get('cwd')
        assert launched_cwd == expected_cwd

    def test_workspace_prefix_cwd_resolved_to_absolute(self, shell_cmd_tool, tool_with_tracker):
        """Cwd with /workspace/ prefix should be stripped and resolved to absolute path."""
        tracker = tool_with_tracker
        from agent_cascade.settings import DEFAULT_WORKSPACE
        base = Path(DEFAULT_WORKSPACE).resolve()
        expected_cwd = (base / 'tests').resolve()

        result = shell_cmd_tool.call(
            '{"command": "ls", "cwd": "/workspace/tests", "execution_mode": "async", "justification": "test"}'
        )

        assert 'ERROR' not in result
        tracker.launch.assert_called_once()
        launched_cwd = tracker.launch.call_args.kwargs.get('cwd')
        assert launched_cwd == expected_cwd

    def test_invalid_cwd_outside_workspace_returns_error(self, shell_cmd_tool, tool_with_tracker):
        """Invalid cwd outside allowed directories should return error message."""
        # Use a path on a different drive (Windows) or clearly outside workspace
        if os.name == 'nt':
            outside_path = 'Z:\\some\\path'  # Non-existent drive, definitely outside workspace
        else:
            outside_path = '/etc/passwd'

        result = shell_cmd_tool.call(
            f'{{"command": "ls", "cwd": "{outside_path}", "execution_mode": "async", "justification": "test"}}'
        )

        assert 'ERROR' in result
        assert 'Invalid working directory' in result

    def test_cwd_traversal_escape_returns_error(self, shell_cmd_tool, tool_with_tracker):
        """Cwd with path traversal should return error."""
        result = shell_cmd_tool.call(
            '{"command": "ls", "cwd": "../../../../../../../../../../../etc/passwd", "execution_mode": "async", "justification": "test"}'
        )

        assert 'ERROR' in result
        assert 'Invalid working directory' in result

    def test_absolute_cwd_within_workspace_accepted(self, shell_cmd_tool, tool_with_tracker):
        """Absolute cwd within workspace should be accepted and passed as resolved path."""
        tracker = tool_with_tracker
        from agent_cascade.settings import DEFAULT_WORKSPACE
        base = Path(DEFAULT_WORKSPACE).resolve()
        abs_cwd = base / 'tests'
        # Use forward slashes to avoid JSON backslash escaping issues on Windows
        abs_cwd_str = str(abs_cwd).replace('\\', '/')

        result = shell_cmd_tool.call(
            f'{{"command": "ls", "cwd": "{abs_cwd_str}", "execution_mode": "async", "justification": "test"}}'
        )

        assert 'ERROR' not in result
        tracker.launch.assert_called_once()
        launched_cwd = tracker.launch.call_args.kwargs.get('cwd')
        # Compare as normalized strings to handle path separator differences
        assert str(launched_cwd).replace('\\', '/').lower() == str(abs_cwd.resolve()).replace('\\', '/').lower()

    def test_error_message_includes_path_info(self, shell_cmd_tool, tool_with_tracker):
        """Error message for invalid cwd should include the problematic path."""
        if os.name == 'nt':
            bad_cwd = 'Z:\\outside\\workspace'
        else:
            bad_cwd = '/etc/shadow'

        result = shell_cmd_tool.call(
            f'{{"command": "ls", "cwd": "{bad_cwd}", "execution_mode": "async", "justification": "test"}}'
        )

        assert 'ERROR' in result
        # Error should reference the path so user knows what was invalid
        assert bad_cwd.replace('\\', '/') in result or 'outside' in result.lower()


# ============================================================================
# Test consistency between shell_cmd cwd and file tool path resolution
# ============================================================================

class TestCwdResolutionConsistency:
    """Verify shell_cmd cwd resolution matches file tool behavior."""

    def test_rejection_consistent_for_cwd_and_files(self):
        """Paths rejected for files should be rejected for shell cwd too.

        Both use resolve_tool_path, so rejection behavior must be identical.
        """
        bad_path = '../../../../../../etc/passwd'

        with pytest.raises(ValueError):
            resolve_tool_path(bad_path, mode="rw")

        with pytest.raises(ValueError):
            resolve_tool_path(bad_path, mode="ro")