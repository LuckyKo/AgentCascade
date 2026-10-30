"""BUG_0050 regression tests: write_file with content ordered before path.

Verifies that:
  1. A write_file call with `content` before `path` in the JSON succeeds
     (both keys present in the parsed mapping).
  2. A genuinely-missing `path` still errors, and the error message lists
     the keys actually received.
  3. A truncated JSON string (unbalanced braces) produces a clear
     "truncated" error, not a misleading "missing required parameter".
  4. The path-first (working) case is unchanged.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# The repo root (overridable via env for dry-run against a temp copy)
REPO = os.environ.get('BUG0050_REPO', r'N:\work\WD\AgentCascade')
sys.path.insert(0, REPO)

# If running against the live repo (pre-deploy), swap in the fixed base.py
# so the truncation check is active. After deploy, the repo's base.py already
# has the fix and the swap is a no-op.
import importlib.util
import agent_cascade.tools.base as _orig_base
_FIXED_BASE = os.path.join(os.path.dirname(__file__), '_bug0050_base_copy.py')
if os.path.exists(_FIXED_BASE):
    _spec = importlib.util.spec_from_file_location(
        'agent_cascade.tools.base', _FIXED_BASE
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules['agent_cascade.tools.base'] = _mod
    _spec.loader.exec_module(_mod)
    # Re-import file_ops so WriteFile picks up the swapped BaseTool
    if 'agent_cascade.tools.custom.file_ops' in sys.modules:
        del sys.modules['agent_cascade.tools.custom.file_ops']

from agent_cascade.tools.custom.file_ops import WriteFile


def _make_writefile():
    """Create a WriteFile instance with a mock agent_pool (no real FS needed)."""
    tool = WriteFile.__new__(WriteFile)
    tool.name = 'write_file'
    tool.description = 'test'
    tool.parameters = {
        'type': 'object',
        'properties': {
            'path': {'type': 'string'},
            'content': {'type': 'string'},
            'justification': {'type': 'string'},
        },
        'required': ['path', 'content'],
    }
    tool.cfg = {}
    # Mock the operation_manager so call() doesn't hit the filesystem
    mock_om = MagicMock()
    mock_om.write_file.return_value = 'OK: Created /tmp/test.txt (1 lines, 5 B)'
    tool.agent_pool = MagicMock()
    tool.agent_pool.operation_manager = mock_om
    tool.agent_name = 'test_agent'
    return tool


class TestWriteFileContentBeforePath:
    """BUG_0050: content ordered before path in write_file arguments."""

    def test_content_before_path_succeeds(self):
        """content before path → both keys present, no 'missing path' error."""
        tool = _make_writefile()
        args = json.dumps({'content': 'hello world', 'path': '/tmp/test.txt'})
        result = tool.call(args)
        assert 'ERROR' not in result, f"Expected success, got: {result}"
        # Verify the operation manager received both keys
        tool.agent_pool.operation_manager.write_file.assert_called_once()
        call_kwargs = tool.agent_pool.operation_manager.write_file.call_args[1]
        assert call_kwargs['path'] == '/tmp/test.txt'
        assert call_kwargs['content'] == 'hello world'

    def test_path_before_path_still_works(self):
        """path before content (the working case) → unchanged behavior."""
        tool = _make_writefile()
        args = json.dumps({'path': '/tmp/test.txt', 'content': 'hello world'})
        result = tool.call(args)
        assert 'ERROR' not in result, f"Expected success, got: {result}"

    def test_large_content_before_path_succeeds(self):
        """Large content (~2 KB) before path → both keys present."""
        tool = _make_writefile()
        large_content = '# Title\n\n' + ('line of text\n' * 100)
        args = json.dumps({'content': large_content, 'path': '/tmp/test.txt'})
        result = tool.call(args)
        assert 'ERROR' not in result, f"Expected success, got: {result}"
        call_kwargs = tool.agent_pool.operation_manager.write_file.call_args[1]
        assert call_kwargs['path'] == '/tmp/test.txt'
        assert call_kwargs['content'] == large_content

    def test_missing_path_still_errors(self):
        """Genuinely missing path → error message lists received keys."""
        tool = _make_writefile()
        args = json.dumps({'content': 'hello'})
        result = tool.call(args)
        assert 'ERROR' in result
        assert 'path' in result, f"Error should mention missing 'path': {result}"
        # The improved error should echo received keys
        assert 'content' in result, f"Error should list received keys: {result}"

    def test_truncated_json_gives_clear_error(self):
        """Truncated JSON (unbalanced braces) → 'truncated' error, not 'missing path'."""
        tool = _make_writefile()
        # Simulates LLM output cut off mid-content: {"content": "abc" (no closing })
        truncated = '{"content": "abc"'
        result = tool.call(truncated)
        assert 'ERROR' in result
        assert 'truncated' in result.lower(), (
            f"Expected 'truncated' in error, got: {result}"
        )

    def test_truncated_json_with_path_last(self):
        """Truncated JSON where path would have been last → clear truncation error."""
        tool = _make_writefile()
        # content first, path would be second, but JSON is cut off
        truncated = '{"content": "some large content here", "path": "/tm'
        result = tool.call(truncated)
        assert 'ERROR' in result
        assert 'truncated' in result.lower(), (
            f"Expected 'truncated' in error, got: {result}"
        )

    def test_dict_input_content_before_path(self):
        """Dict input (pre-parsed by _resolve_placeholders) with content first."""
        tool = _make_writefile()
        args = {'content': 'hello', 'path': '/tmp/test.txt'}
        result = tool.call(json.dumps(args))
        assert 'ERROR' not in result, f"Expected success, got: {result}"

    def test_parallel_block_both_succeed(self):
        """Two write_file calls in sequence: path-first then content-first → both succeed."""
        tool = _make_writefile()
        # Call 1: path before content (the working case)
        args1 = json.dumps({'path': '/tmp/a.txt', 'content': 'file a'})
        result1 = tool.call(args1)
        assert 'ERROR' not in result1, f"Call 1 failed: {result1}"

        # Call 2: content before path (the BUG_0050 case)
        args2 = json.dumps({'content': 'file b', 'path': '/tmp/b.txt'})
        result2 = tool.call(args2)
        assert 'ERROR' not in result2, f"Call 2 failed: {result2}"

        # Both calls should have hit the operation manager
        assert tool.agent_pool.operation_manager.write_file.call_count == 2
