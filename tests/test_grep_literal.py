"""
Tests for the `literal` parameter of the grep tool (BUG_0171).

When literal=True, the pattern is treated as a fixed string (no regex interpretation),
so backslashes and other regex metacharacters are matched literally.

Run with:  python tests/test_grep_literal.py
"""
import tempfile
from pathlib import Path

import pytest


def _make_om(tmp_path: Path):
    """Create an OperationManager rooted at tmp_path."""
    from agent_cascade.operation_manager import OperationManager
    return OperationManager(base_dir=str(tmp_path))


class TestGrepLiteral:
    """Tests for the literal parameter in the grep operation manager."""

    def test_literal_finds_backslash_n(self, tmp_path):
        """A file containing literal backslash-n should be found with literal=True."""
        # The file contains: tests\ninfer_tests\.exe  (backslash-n, backslash-dot)
        file = tmp_path / 'searchable.txt'
        file.write_text('prefix tests\\ninfer_tests\\.exe suffix\n')

        om = _make_om(tmp_path)
        # In Python source, 'tests\\ninfer_tests\\.exe' is the string: tests\ninfer_tests\.exe
        result = om.grep(pattern='tests\\ninfer_tests\\.exe', path='.', literal=True)
        assert 'searchable.txt' in result, f"Should find the file; got: {result[:300]}"
        assert 'Found 1 match' in result or 'Found 1 match(es)' in result, f"Should report 1 match; got: {result[:300]}"

    def test_non_literal_does_not_find_backslash_n(self, tmp_path):
        """With literal=False (default), \\n is a regex newline and won't match backslash-n."""
        file = tmp_path / 'searchable.txt'
        file.write_text('prefix tests\\ninfer_tests\\.exe suffix\n')

        om = _make_om(tmp_path)
        # '\n' in regex = newline character; the file has backslash-n, not a newline
        result = om.grep(pattern='tests\\ninfer_tests\\.exe', path='.', literal=False)
        assert 'No matches' in result, f"Should find no matches with regex interpretation; got: {result[:300]}"

    def test_backward_compat_regex_still_works(self, tmp_path):
        """A normal regex pattern with literal=False must still work as before."""
        file = tmp_path / 'code.py'
        file.write_text("def hello_world():\n    return 'hi'\n")

        om = _make_om(tmp_path)
        result = om.grep(pattern=r'hello_\w+', path='.', literal=False)
        assert 'code.py' in result, f"Should find code.py; got: {result[:300]}"

    def test_literal_with_special_regex_chars(self, tmp_path):
        """Literal mode should match regex metacharacters as themselves."""
        file = tmp_path / 'special.txt'
        file.write_text('price is $5.00 (tax included)\n')

        om = _make_om(tmp_path)
        # In regex, $ . ( ) are metacharacters; literal mode should match them literally
        result = om.grep(pattern='$5.00 (tax', path='.', literal=True)
        assert 'special.txt' in result, f"Should find the file; got: {result[:300]}"

    def test_literal_single_file_path(self, tmp_path):
        """literal=True should work when path is a single file (not a directory)."""
        file = tmp_path / 'target.txt'
        file.write_text('path: N:\\work\\WD\\project\n')

        om = _make_om(tmp_path)
        result = om.grep(pattern='N:\\work\\WD\\project', path='target.txt', literal=True)
        assert 'target.txt' in result, f"Should find the file; got: {result[:300]}"

    def test_literal_with_smart_case(self, tmp_path):
        """literal=True + smart_case should be case-insensitive when pattern has no uppercase."""
        file = tmp_path / 'case.txt'
        file.write_text('Hello World\n')

        om = _make_om(tmp_path)
        # 'hello world' (no uppercase) with smart_case=True (default) → case-insensitive
        result = om.grep(pattern='hello world', path='.', literal=True)
        assert 'case.txt' in result, f"Should find case-insensitive match; got: {result[:300]}"

    def test_literal_with_smart_case_false(self, tmp_path):
        """literal=True + smart_case=False should be case-sensitive."""
        file = tmp_path / 'case2.txt'
        file.write_text('Hello World\n')

        om = _make_om(tmp_path)
        # smart_case=False → case-sensitive: 'Hello World' matches exactly
        result = om.grep(pattern='Hello World', path='.', literal=True, smart_case=False)
        assert 'case2.txt' in result, f"Should find exact case match; got: {result[:300]}"
        # 'hello world' (lowercase) should NOT match 'Hello World'
        result2 = om.grep(pattern='hello world', path='.', literal=True, smart_case=False)
        assert 'No matches' in result2, f"Should not find case-mismatched match; got: {result2[:300]}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
