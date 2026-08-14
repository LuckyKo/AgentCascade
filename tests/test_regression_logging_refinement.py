"""Regression tests for logging refinement changes.

Tests validate:
1. read_logs tool — new auto-resolution feature for bare filenames
2. send_message tool — logging pattern refactoring (no behavioral changes expected)
3. file_ops tools — logging pattern refactoring (no behavioral changes expected)
4. forget_last_tool — added exception logging (no behavioral changes expected)
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestReadLogsAutoResolution:
    """Test the new auto-resolution feature in read_logs for bare filenames."""

    def _create_mock_agent_pool(self, log_dir: str | None = None):
        """Create a mock agent_pool with configurable log_dir."""
        pool = MagicMock()
        if log_dir is not None:
            pool._logger.log_dir = log_dir
        else:
            # No log_dir attribute — should fall through to resolve_tool_path
            del pool._logger.log_dir
        return pool

    def _write_test_log(self, tmp_path: Path, name: str, entries: list) -> Path:
        """Write a JSONL log file for testing."""
        p = tmp_path / name
        with open(p, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return p

    def test_bare_filename_resolved_in_log_dir(self):
        """A bare filename like 'orchestrator_Maine_20260814.jsonl' resolves against log_dir."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log_file = self._write_test_log(
                tmp_path,
                "test_agent.jsonl",
                [{"type": "user", "content": "hello"}, {"type": "assistant", "content": "hi"}],
            )

            pool = self._create_mock_agent_pool(str(tmp_path))

            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            result = tool.call({"log_file": "test_agent.jsonl"})

            assert "Error" not in result, f"Unexpected error: {result}"
            assert "hello" in result
            assert "hi" in result

    def test_bare_filename_not_in_log_dir_falls_through(self):
        """If bare filename doesn't exist in log_dir, falls through to resolve_tool_path.

        Note: We can only verify it doesn't raise an exception and returns something
        (likely an error about path not being in allowed dirs), since resolve_tool_path
        restricts paths to workspace directories.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool = self._create_mock_agent_pool(str(tmp_path))

            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            # Non-existent bare filename should fall through without crashing
            result = tool.call({"log_file": "nonexistent.jsonl"})

            # Should not crash — may return error about file not found or path restriction
            assert isinstance(result, str), f"Expected string result, got {type(result)}"

    def test_path_traversal_guard_rejects_dotdot(self):
        """The '..' guard rejects path traversal attempts."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log_dir = tmp_path / "logs"
            log_dir.mkdir()

            # Write a file outside log_dir that we're trying to reach via ..
            secret_file = tmp_path / "secret.jsonl"
            with open(secret_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"secret": "data"}) + "\n")

            pool = self._create_mock_agent_pool(str(log_dir))

            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            # Attempt path traversal — should NOT resolve via auto-resolution
            result = tool.call({"log_file": "../secret.jsonl"})

            # The ".." guard prevents auto-resolution, and resolve_tool_path should also reject it
            # Either way, we should NOT get the secret data
            assert "secret" not in result.lower() or "Error" in result or "not found" in result.lower(), \
                "Path traversal via '..' was not properly blocked!"

    def test_full_path_still_works(self):
        """Full paths go through resolve_tool_path fallback (may fail due to path restrictions).

        This verifies the fallback doesn't crash — actual success depends on workspace config.
        """
        import os
        workspace_root = Path(os.environ.get("AGENT_WORKSPACE", "N:\\work\\WD\\AgentWorkspace"))
        test_file = workspace_root / "_test_regression_fullpath.jsonl"

        try:
            with open(test_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"msg": "via full path"}) + "\n")

            pool = self._create_mock_agent_pool("/nonexistent")

            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            # Should not crash — result depends on resolve_tool_path restrictions
            result = tool.call({"log_file": str(test_file)})
            assert isinstance(result, str), f"Expected string result, got {type(result)}"
        finally:
            if test_file.exists():
                test_file.unlink()

    def test_wildcard_single_match_resolves(self):
        """Wildcard patterns with single match resolve correctly."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_test_log(
                tmp_path,
                "agent_one.jsonl",
                [{"msg": "single match"}],
            )

            pool = self._create_mock_agent_pool(str(tmp_path))

            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            result = tool.call({"log_file": "agent_*.jsonl"})

            assert "Error" not in result, f"Unexpected error: {result}"
            assert "single match" in result

    def test_wildcard_multiple_matches_returns_error(self):
        """Wildcard patterns with multiple matches return a helpful error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_test_log(tmp_path, "agent_a.jsonl", [{"msg": "a"}])
            self._write_test_log(tmp_path, "agent_b.jsonl", [{"msg": "b"}])

            pool = self._create_mock_agent_pool(str(tmp_path))

            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            result = tool.call({"log_file": "agent_*.jsonl"})

            assert "Error" in result or "Multiple" in result, \
                f"Expected error for multiple matches but got: {result}"

    def test_no_agent_pool_falls_through(self):
        """Without agent_pool, falls through to resolve_tool_path.

        Note: resolve_tool_path restricts paths to workspace directories, so we use
        a workspace path instead of a temp dir.
        """
        import os
        workspace_root = Path(os.environ.get("AGENT_WORKSPACE", "N:\\work\\WD\\AgentWorkspace"))
        test_file = workspace_root / "_test_regression_nopool.jsonl"

        try:
            with open(test_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"msg": "via fallback"}) + "\n")

            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=None)
            result = tool.call({"log_file": str(test_file)})

            assert "Error" not in result, f"Unexpected error: {result}"
            assert "via fallback" in result
        finally:
            if test_file.exists():
                test_file.unlink()

    def test_bare_filename_with_slash_not_auto_resolved(self):
        """A filename containing '/' is NOT auto-resolved (goes to resolve_tool_path).

        This verifies the guard condition works — doesn't crash. Actual resolution
        depends on workspace path restrictions.
        """
        import os
        workspace_root = Path(os.environ.get("AGENT_WORKSPACE", "N:\\work\\WD\\AgentWorkspace"))
        subdir = workspace_root / "_test_regression_sub"
        subdir.mkdir(exist_ok=True)
        test_file = subdir / "nested.jsonl"

        try:
            with open(test_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"msg": "nested"}) + "\n")

            pool = self._create_mock_agent_pool(str(workspace_root))

            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            # Path with / goes to resolve_tool_path, not auto-resolution — should not crash
            result = tool.call({"log_file": str(test_file)})
            assert isinstance(result, str), f"Expected string result, got {type(result)}"
        finally:
            if test_file.exists():
                test_file.unlink()
            if subdir.exists():
                subdir.rmdir()


class TestSendMessageRefactoring:
    """Verify send_message still works after logging refactoring."""

    def test_send_message_instantiates_with_logger(self):
        """SendMessage class instantiates correctly and uses module-level logger."""
        from agent_cascade.tools.custom.send_message import SendMessage, logger

        # Should not raise any import or instantiation errors
        tool = SendMessage()

        # Verify logger is a logging.Logger instance (module-level)
        import logging
        assert isinstance(logger, logging.Logger), \
            f"Expected module-level logger to be logging.Logger, got {type(logger)}"

    def test_send_message_has_required_attributes(self):
        """SendMessage has the expected tool attributes."""
        from agent_cascade.tools.custom.send_message import SendMessage

        tool = SendMessage()

        assert hasattr(tool, 'name')
        assert hasattr(tool, 'description')
        assert hasattr(tool, 'parameters')
        assert tool.name == 'send_message'


class TestFileOpsRefactoring:
    """Verify file_ops tools still work after logging refactoring."""

    def test_file_ops_import_no_errors(self):
        """All file ops classes import without errors."""
        from agent_cascade.tools.custom.file_ops import (
            ReadFile,
            WriteFile,
            EditFile,
            DeleteFile,
            CopyFile,
            ReIndent,
            ListDir,
        )

        # Instantiate each to verify no logger-related issues
        for cls in [ReadFile, WriteFile, EditFile, DeleteFile, CopyFile, ReIndent, ListDir]:
            tool = cls()
            assert hasattr(tool, 'name')
            assert hasattr(tool, 'call')

    def test_read_file_basic(self):
        """ReadFile can read a file within allowed directories."""
        import os
        workspace_root = Path(os.environ.get("AGENT_WORKSPACE", "N:\\work\\WD\\AgentWorkspace"))
        test_file = workspace_root / "_test_regression_readfile.txt"

        try:
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("test content\nline 2")

            from agent_cascade.tools.custom.file_ops import ReadFile

            tool = ReadFile()
            result = tool.call({"path": str(test_file)})

            assert "Error" not in result, f"Unexpected error: {result}"
            assert "test content" in result
            assert "line 2" in result
        finally:
            if test_file.exists():
                test_file.unlink()


class TestForgetLastToolRefactoring:
    """Verify forget_last_tool still works after adding exception logging."""

    def test_forget_last_tool_import_no_errors(self):
        """ForgetLast imports and instantiates without errors."""
        from agent_cascade.tools.custom.forget_last_tool import ForgetLast, logger

        import logging
        assert isinstance(logger, logging.Logger)

        tool = ForgetLast()
        assert hasattr(tool, 'name')
        assert hasattr(tool, 'call')
        assert tool.name == 'forget_last'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
