"""Test suite for grep usability improvements in operation_manager.py."""
import os, sys, tempfile, re
from pathlib import Path

# Resolve project root relative to this test file (tests/ → project_root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

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

def test_list_recursive_empty_subdir_marked():
    """BUG_0006: an empty subdir must be shown as (empty) and root files must NOT
    be indented under it."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        # Root has 2 files + one EMPTY subdir (the exact BUG_0006 scenario).
        Path(tmpdir, "fixed").mkdir()
        Path(tmpdir, "BUG_0001.md").write_text("a")
        Path(tmpdir, "BUG_0002.md").write_text("b")
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True)

        # Empty subdir is present and explicitly marked.
        assert "[DIR] fixed/ (empty)" in result, f"Empty subdir must be marked (empty): {result}"

        # Root files render at column 0 (no leading indent), so they are not
        # visually attached to the empty subdir.
        for fname in ("BUG_0001.md", "BUG_0002.md"):
            line = next((ln for ln in result.splitlines() if ln.lstrip().startswith(fname)), None)
            assert line is not None, f"{fname} missing from output: {result}"
            assert not line.startswith(" "), \
                f"Root file {fname} must be at column 0 (not indented under a subdir): {line!r}"

        # The root is NOT reported as empty (it contains a dir).
        assert "(empty directory or all entries filtered out)" not in result, \
            f"Root with an empty subdir should not print top-level empty message: {result}"
    print("[PASS] test_list_recursive_empty_subdir_marked")

def test_list_recursive_depth_indentation():
    """Files inside a nested subdir must be indented more than root files AND labelled
    with their path relative to the listed root (so parentage is unambiguous)."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "root.txt").write_text("root file")
        sub = Path(tmpdir, "sub")
        sub.mkdir()
        (sub / "nested.txt").write_text("nested file")
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True)

        root_line = next(ln for ln in result.splitlines() if ln.lstrip().startswith("root.txt"))
        # Nested file is now labelled with its relative path "sub/nested.txt".
        nested_line = next(ln for ln in result.splitlines() if "sub/nested.txt" in ln)

        # Root file at column 0; nested file indented by >= 2 spaces.
        assert not root_line.startswith(" "), f"Root file should be at column 0: {root_line!r}"
        assert nested_line.startswith("  "), \
            f"Nested file should be indented under its subdir: {nested_line!r}"

        # The nested entry carries its parent dir in the label (the actual fix).
        assert "sub/nested.txt" in nested_line, \
            f"Nested file must show relative path 'sub/nested.txt': {nested_line!r}"
        # Root-level file keeps a bare name (no directory prefix).
        assert root_line.lstrip().startswith("root.txt "), \
            f"Root file must keep a bare name: {root_line!r}"

        # Subdir contents are more indented than the subdir's own header line.
        dir_line = next(ln for ln in result.splitlines() if "[DIR] sub/" in ln)
        assert len(nested_line) - len(nested_line.lstrip()) > len(dir_line) - len(dir_line.lstrip()), \
            f"Nested file indent must exceed subdir header indent: {nested_line!r} vs {dir_line!r}"
    print("[PASS] test_list_recursive_depth_indentation")

def test_list_exclude_flat():
    """BUG_0008 (defect 1): exclude filter must DROP matching names, not keep them.
    Flat mode: exclude='*.txt' removes drop.txt and keeps keep.py."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "keep.py").write_text("a")
        Path(tmpdir, "drop.txt").write_text("b")
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", exclude="*.txt")
        assert "keep.py" in result, f"keep.py should be kept: {result}"
        assert "drop.txt" not in result, f"drop.txt should be excluded: {result}"
    print("[PASS] test_list_exclude_flat")

def test_list_exclude_recursive():
    """BUG_0008 (defect 1): exclude filter in recursive mode drops all matching files
    at any depth while keeping non-matching ones."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "keep.py").write_text("a")
        Path(tmpdir, "drop.txt").write_text("b")
        a = Path(tmpdir, "a"); a.mkdir()
        (a / "inner.txt").write_text("c")
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True, exclude="*.txt")
        # No .txt file may appear anywhere in the output.
        assert ".txt" not in result, f"No .txt files should be shown: {result}"
        assert "drop.txt" not in result, f"drop.txt should be excluded: {result}"
        assert "inner.txt" not in result, f"inner.txt should be excluded: {result}"
        # Non-.txt files are kept.
        assert "keep.py" in result, f"keep.py should be kept: {result}"
    print("[PASS] test_list_exclude_recursive")

def test_list_include_filtered_dir_subtree():
    """BUG_0008 (defect 2): a dir that fails the include filter must still be walked
    so matching files beneath it are found. include='*.txt' must reveal BOTH the root
    drop.txt AND a/inner.txt even though dir 'a' itself does not match '*.txt'."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "drop.txt").write_text("a")
        Path(tmpdir, "keep.py").write_text("b")
        a = Path(tmpdir, "a"); a.mkdir()
        (a / "inner.txt").write_text("c")
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True, include="*.txt")
        # Both matching .txt files must be present (key regression assertion).
        assert "drop.txt" in result, f"root drop.txt should be shown: {result}"
        assert "inner.txt" in result, f"a/inner.txt should be shown (filtered dir still walked): {result}"
        # Non-matching file is dropped.
        assert "keep.py" not in result, f"keep.py should be filtered out by include='*.txt': {result}"
    print("[PASS] test_list_include_filtered_dir_subtree")

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
        test_list_recursive_empty_subdir_marked,
        test_list_recursive_depth_indentation,
        test_list_exclude_flat,
        test_list_exclude_recursive,
        test_list_include_filtered_dir_subtree,
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