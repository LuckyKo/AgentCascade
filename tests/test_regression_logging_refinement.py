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
                [{"role": "user", "content": "single match"}],
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
                f.write(json.dumps({"role": "user", "content": "via fallback"}) + "\n")

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


class TestReadLogsFormatParameter:
    """Regression tests for read_logs format parameter (raw/simple modes)."""

    def _create_mock_agent_pool(self, log_dir: str | None = None):
        """Create a mock agent_pool with configurable log_dir."""
        pool = MagicMock()
        if log_dir is not None:
            pool._logger.log_dir = log_dir
        else:
            del pool._logger.log_dir
        return pool

    def _write_test_log(self, tmp_path: Path, name: str, entries: list) -> Path:
        """Write a JSONL log file for testing."""
        p = tmp_path / name
        with open(p, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return p

    def test_raw_format_produces_json_lines_with_number_prefixes(self):
        """raw format produces JSON lines with line number prefixes (current behavior)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_test_log(
                tmp_path,
                "test.jsonl",
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi there"},
                ],
            )

            pool = self._create_mock_agent_pool(str(tmp_path))
            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            result = tool.call({"log_file": "test.jsonl", "format": "raw"})

            assert "Error" not in result, f"Unexpected error: {result}"
            lines = result.strip().split("\n")
            assert len(lines) == 2

            # Each line starts with a number prefix followed by ": " and valid JSON
            for line in lines:
                assert ": " in line, f"Line missing ': ' separator: {line}"
                num_prefix, json_part = line.split(": ", 1)
                assert num_prefix.isdigit(), f"Prefix not numeric: {num_prefix}"
                parsed = json.loads(json_part)
                assert isinstance(parsed, dict), f"Not a JSON object: {parsed}"

            # Verify content is preserved in raw output
            assert "hello" in result
            assert "hi there" in result

    def test_simple_format_produces_human_readable_output(self):
        """simple format produces human-readable output with role labels, timestamps."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_test_log(
                tmp_path,
                "test.jsonl",
                [
                    {"role": "user", "content": "hello world"},
                    {
                        "role": "assistant",
                        "timestamp": "2026-08-14T10:30:00Z",
                        "content": "hi there",
                    },
                ],
            )

            pool = self._create_mock_agent_pool(str(tmp_path))
            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            result = tool.call({"log_file": "test.jsonl", "format": "simple"})

            assert "Error" not in result, f"Unexpected error: {result}"

            # Should contain role labels
            assert "USER" in result
            assert "ASSISTANT" in result

            # Should contain content previews (indented)
            assert "hello world" in result
            assert "hi there" in result

            # Should NOT be raw JSON lines with ": {" pattern
            for line in result.split("\n"):
                stripped = line.strip()
                if stripped.startswith("[") and "] USER" in stripped:
                    continue  # header line, OK
                if stripped.startswith("    "):
                    continue  # content preview, OK
                # Check it's not raw JSON format (number prefix followed by JSON object)
                if ": {" in stripped or ":[" in stripped:
                    parts = stripped.split(": ", 1)
                    if len(parts) == 2 and parts[0].isdigit():
                        pytest.fail(f"Found raw-format line in simple output: {stripped}")

    def test_format_defaults_to_simple(self):
        """format parameter defaults to 'simple' when not specified."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_test_log(
                tmp_path,
                "test.jsonl",
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
            )

            pool = self._create_mock_agent_pool(str(tmp_path))
            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)
            # Call WITHOUT specifying format parameter
            result = tool.call({"log_file": "test.jsonl"})

            assert "Error" not in result, f"Unexpected error: {result}"

            # Should produce simple format output (role labels, no raw JSON lines)
            assert "USER" in result or "ASSISTANT" in result, \
                "Default format should be 'simple' with role labels"

    def test_truncation_modes_work_with_raw_format(self):
        """trim_tools truncates tool OUTPUTS but keeps tool CALLS intact; trim_all/none behave as expected."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            long_args = "x" * 2000
            long_output = "y" * 2000
            self._write_test_log(
                tmp_path,
                "test.jsonl",
                [
                    {
                        "role": "assistant",
                        "content": "calling tool",
                        "function_call": {"name": "big_tool", "arguments": long_args},
                    },
                    {
                        "role": "function",
                        "name": "big_tool",
                        "content": long_output,
                    },
                ],
            )

            pool = self._create_mock_agent_pool(str(tmp_path))
            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)

            # trim_tools: assistant tool-call arguments must be INTACT; tool-output content truncated
            result_trim_tools = tool.call(
                {"log_file": "test.jsonl", "format": "raw", "mode": "trim_tools", "max_chars_per_message": 100}
            )
            assert "Error" not in result_trim_tools, f"Unexpected error: {result_trim_tools}"
            # Tool call arguments are preserved (no TRUNCATED marker on them)
            assert long_args in result_trim_tools
            # Tool output content IS truncated (TRUNCATED marker present)
            assert "TRUNCATED" in result_trim_tools

            # trim_all: should truncate all long strings (both args and output)
            result_trim_all = tool.call(
                {"log_file": "test.jsonl", "format": "raw", "mode": "trim_all", "max_chars_per_message": 100}
            )
            assert "Error" not in result_trim_all, f"Unexpected error: {result_trim_all}"
            assert long_args not in result_trim_all
            assert long_output not in result_trim_all
            assert "TRUNCATED" in result_trim_all

            # none: should NOT truncate anything
            result_none = tool.call(
                {"log_file": "test.jsonl", "format": "raw", "mode": "none"}
            )
            assert "Error" not in result_none, f"Unexpected error: {result_none}"
            assert long_args in result_none
            assert long_output in result_none
            assert "TRUNCATED" not in result_none

    def test_truncation_modes_work_with_simple_format(self):
        """Existing truncation modes still work with simple format."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            long_content = "y" * 2000
            self._write_test_log(
                tmp_path,
                "test.jsonl",
                [
                    {"role": "user", "content": long_content},
                ],
            )

            pool = self._create_mock_agent_pool(str(tmp_path))
            from agent_cascade.tools.custom.read_logs import ReadLogs

            tool = ReadLogs(agent_pool=pool)

            # trim_all: content preview in simple mode should be truncated
            result_trim_all = tool.call(
                {"log_file": "test.jsonl", "format": "simple", "mode": "trim_all", "max_chars_per_message": 100}
            )
            assert "Error" not in result_trim_all, f"Unexpected error: {result_trim_all}"
            # Simple mode content preview is capped at ~200 chars regardless of max_chars,
            # but trim_all should still truncate the underlying data before formatting
            assert "USER" in result_trim_all

            # none: no truncation applied
            result_none = tool.call(
                {"log_file": "test.jsonl", "format": "simple", "mode": "none"}
            )
            assert "Error" not in result_none, f"Unexpected error: {result_none}"
            assert "USER" in result_none

    def test_invalid_format_returns_error(self):
        """Invalid format value returns a clear error message (via jsonschema validation)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_test_log(
                tmp_path,
                "test.jsonl",
                [{"role": "user", "content": "hello"}],
            )

            pool = self._create_mock_agent_pool(str(tmp_path))
            from agent_cascade.tools.custom.read_logs import ReadLogs
            import jsonschema

            tool = ReadLogs(agent_pool=pool)
            # Invalid enum value is caught by jsonschema validation before custom error handling
            with pytest.raises(jsonschema.ValidationError, match="invalid.*not one of"):
                tool.call({"log_file": "test.jsonl", "format": "invalid"})


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
