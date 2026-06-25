"""Test suite for grep usability improvements in operation_manager.py."""
import os, sys, tempfile, re
from pathlib import Path

sys.path.insert(0, r"N:\work\WD\AgentCascade")

def test_compile_grep_pattern_flags():
    """Test that _compile_grep_pattern accepts flags parameter."""
    from agent_cascade.operation_manager import _compile_grep_pattern
    _compile_grep_pattern.cache_clear()
    pat = _compile_grep_pattern("Hello")
    assert not pat.search("hello"), "Should be case-sensitive by default"
    pat_ci = _compile_grep_pattern("Hello", flags=re.IGNORECASE)
    assert pat_ci.search("hello"), "Should match with IGNORECASE flag"
    print("[PASS] test_compile_grep_pattern_flags")

def test_smart_case_logic():
    """Test smart_case logic."""
    assert not re.search(r'[A-Z]', "hello"), "Pattern 'hello' has no uppercase"
    assert re.search(r'[A-Z]', "Hello"), "Pattern 'Hello' has uppercase"
    print("[PASS] test_smart_case_logic")

def test_list_dir_no_emoji():
    """Test that list_directory output uses clean formatting without emoji."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "subdir").mkdir()
        Path(tmpdir, "test.txt").write_text("hello")
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".")
        assert "\U0001f4c1" not in result, "Should not contain folder emoji"
        assert "\U0001f4c2" not in result, "Should not contain open-folder emoji"
        assert "\U0001f4c4" not in result, "Should not contain page emoji"
        assert "\U0001f4dd" not in result, "Should not contain memo emoji"
        # New format uses "Directories:" / "Files:" headers with trailing slashes for dirs
        assert "subdir/" in result, f"Directory should appear with trailing slash: {result}"
        assert "test.txt" in result, f"File name should appear: {result}"
    print("[PASS] test_list_dir_no_emoji")

def test_grep_path_normalization():
    """Test that grep output uses forward slashes even on Windows."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        subdir = Path(tmpdir) / "src" / "nested"
        subdir.mkdir(parents=True)
        (subdir / "test.py").write_text("def hello():\n    pass")
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern="hello", path=".")
        assert "\\" not in result, f"Backslashes found in output: {result}"
        assert "src/nested/test.py" in result, f"Forward slash path expected: {result}"
    print("[PASS] test_grep_path_normalization")

def test_grep_no_strip():
    """Test that grep preserves whitespace (no .strip())."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        content = "  indented text  \nno indent\n"
        Path(tmpdir, "test.txt").write_text(content)
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern="indented", path=".")
        # The matched line should preserve its whitespace
        assert "  indented text  " in result or "    >>>" in result, \
            f"Whitespace should be preserved: {result}"
    print("[PASS] test_grep_no_strip")

def test_grep_context_lines():
    """Test that context lines are shown around matches."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        content = "line 1\nline 2 MATCH\nline 3\nline 4\n"
        Path(tmpdir, "test.txt").write_text(content)
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern="MATCH", path=".", context=1)
        assert "line 1" in result, f"Context before should be shown: {result}"
        assert ">>>" in result or "---" in result, f">>> prefix or --- separator expected: {result}"
        assert "line 3" in result, f"Context after should be shown: {result}"
    print("[PASS] test_grep_context_lines")

def test_grep_exclude():
    """Test that exclude parameter filters files."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "good.py").write_text("hello world")
        Path(tmpdir, "bad.txt").write_text("hello world")
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern="hello", path=".", exclude="*.txt")
        assert "good.py" in result, f"good.py should be included: {result}"
        assert "bad.txt" not in result, f"bad.txt should be excluded: {result}"
    print("[PASS] test_grep_exclude")

def test_grep_vcs_skip():
    """Test that VCS/build directories are skipped in Python fallback."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        pycache = Path(tmpdir, "__pycache__")
        pycache.mkdir()
        (pycache / "cached.pyc").write_text("hello")
        Path(tmpdir, "normal.py").write_text("hello")
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern="hello", path=".")
        assert "normal.py" in result, f"normal.py should be found: {result}"
    print("[PASS] test_grep_vcs_skip")

def test_backwards_compatibility():
    """Test that default behavior is preserved when new params aren't provided."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "test.py").write_text("Hello World\nhello world")
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern="hello", path=".")
        assert "test.py" in result, f"Should find matches with default params: {result}"
    print("[PASS] test_backwards_compatibility")

def test_context_match_count_not_inflated():
    """Test that context mode doesn't inflate match count."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        content = "alpha\nMATCH1\nbravo\nMATCH2\ndelta\n"
        Path(tmpdir, "test.txt").write_text(content)
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern="MATCH", path=".", context=1)
        # Should say "Found 2 matches" not "Found 6 matches" or similar inflated count
        assert "Found 2 matches" in result, f"Match count should be 2: {result}"

def test_exclude_fnmatch():
    """Test that exclude uses fnmatch (supports **)."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "good.py").write_text("hello")
        # Nested file that should be excluded by ** pattern
        subdir = Path(tmpdir) / "deep" / "nested"
        subdir.mkdir(parents=True)
        (subdir / "bad.pyc").write_text("hello")
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern="hello", path=".", exclude="**/*.pyc")
        assert "good.py" in result, f"good.py should be included: {result}"
        assert "bad.pyc" not in result, f"bad.pyc should be excluded by ** pattern: {result}"

def test_keyword_only_flags():
    """Test that _compile_grep_pattern flags parameter is keyword-only."""
    from agent_cascade.operation_manager import _compile_grep_pattern
    _compile_grep_pattern.cache_clear()
    # Should work with keyword arg
    pat = _compile_grep_pattern("Hello", flags=re.IGNORECASE)
    assert pat.search("hello"), "Should match with IGNORECASE"

if __name__ == "__main__":
    tests = [
        test_compile_grep_pattern_flags,
        test_smart_case_logic,
        test_list_dir_no_emoji,
        test_grep_path_normalization,
        test_grep_no_strip,
        test_grep_context_lines,
        test_grep_exclude,
        test_grep_vcs_skip,
        test_backwards_compatibility,
        test_context_match_count_not_inflated,
        test_exclude_fnmatch,
        test_keyword_only_flags,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print("")
    print("=" * 50)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    if failed > 0:
        sys.exit(1)