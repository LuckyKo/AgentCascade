#!/usr/bin/env python3
"""Manual verification script for BUG_0006 fix - tests edge cases."""
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from agent_cascade.operation_manager import OperationManager

def test_root_only_empty_subdir():
    """Test requirement (f): root with only empty subdir should NOT show top-level empty message."""
    print("=== Test: Root with only empty subdir ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "empty_sub").mkdir()
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True)
        print(result)
        assert "(empty directory or all entries filtered out)" not in result, \
            f"Root with empty subdir should NOT show empty message: {result}"
        assert "[DIR] empty_sub/ (empty)" in result, \
            f"Empty subdir must be marked: {result}"
        print("[PASS]\n")

def test_filter_excludes_all_in_subdir():
    """Test requirement (f) ambiguity: if root's only subdir is entirely filter-excluded,
    should the top-level empty message appear? This is ambiguous."""
    print("=== Test: Filter excludes all entries in subdir ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        sub = Path(tmpdir, "sub")
        sub.mkdir()
        (sub / "file.txt").write_text("hello")
        om = OperationManager(base_dir=tmpdir)
        # Exclude all files in sub
        result = om.list_directory(".", recursive=True, include="*.nonexistent")
        print(result)
        # Since the subdir itself might be included or not depends on include pattern
        # The subdir name "sub" doesn't match "*.nonexistent", so it won't be included as an entry.
        # This means total_dirs and total_files might be 0, triggering empty message.
        # This is a gray area - the directory exists but is completely filtered out.
        print("[INFO] Behavior depends on interpretation of 'empty' with filters\n")

def test_os_walk_ordering():
    """Verify that files yielded by os.walk for a given dir are rendered at that dir's indent,
    and that a dir's own entries are not misattributed when subdirs are visited in a particular order."""
    print("=== Test: os.walk ordering and attribution ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create structure: root/a/b/file.txt, root/c/file2.txt
        (Path(tmpdir) / "a" / "b").mkdir(parents=True)
        (Path(tmpdir) / "c").mkdir()
        (Path(tmpdir) / "a" / "b" / "file.txt").write_text("1")
        (Path(tmpdir) / "c" / "file2.txt").write_text("2")
        (Path(tmpdir) / "root.txt").write_text("0")
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True)
        print(result)
        # Check that root.txt is at indent 0
        lines = result.splitlines()
        root_line = next(l for l in lines if l.strip().startswith("root.txt"))
        assert not root_line.startswith(" "), f"root.txt should be at column 0: {root_line!r}"
        # Check that file.txt is indented more than a/
        a_line = next(l for l in lines if "[DIR] a/" in l)
        file_line = next(l for l in lines if l.strip().startswith("file.txt"))
        a_indent = len(a_line) - len(a_line.lstrip())
        file_indent = len(file_line) - len(file_line.lstrip())
        assert file_indent > a_indent, f"file.txt indent {file_indent} must > a/ indent {a_indent}"
        print("[PASS]\n")

def test_max_entries_totals():
    """Verify that returned tuple counts are computed consistently with new rendering and
    that max_entries truncation doesn't corrupt the totals."""
    print("=== Test: max_entries and totals consistency ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create many files and dirs
        for i in range(20):
            (Path(tmpdir) / f"file{i}.txt").write_text("x")
            (Path(tmpdir) / f"dir{i}").mkdir()
        om = OperationManager(base_dir=tmpdir)
        # Request only 10 entries
        output, total_dirs, total_files, total_size = om._list_recursive(
            ".", Path(tmpdir), None, None, "name", -1, 10
        )
        print(f"Totals: dirs={total_dirs}, files={total_files}, size={total_size}")
        print(output)
        # The totals should reflect ALL entries in the tree, not just those shown
        expected_dirs = 20
        expected_files = 20
        assert total_dirs == expected_dirs, f"Expected {expected_dirs} dirs, got {total_dirs}"
        assert total_files == expected_files, f"Expected {expected_files} files, got {total_files}"
        print("[PASS]\n")

def test_symlink_cycle_guard():
    """Test that symlink cycles are handled correctly."""
    print("=== Test: Symlink cycle guard ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a cycle: dir1 -> dir2 -> dir1 via symlink (on Windows need admin? maybe tricky)
        # Simplified: create a subdir and symlink it into itself
        sub = Path(tmpdir) / "sub"
        sub.mkdir()
        try:
            # Windows symlink creation requires admin or developer mode; might fail
            (sub / "link").symlink_to(sub)
        except OSError:
            print("[SKIP] Could not create symlink (needs admin/developer mode)")
            return
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True)
        print(result)
        # Should not infinite loop or crash
        assert "Error" not in result
        print("[PASS]\n")

def test_empty_dir_with_nested_empty():
    """Test nested empty directories are all marked."""
    print("=== Test: Nested empty directories ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "a" / "b" / "c").mkdir(parents=True)
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True)
        print(result)
        assert "[DIR] a/ (empty)" in result or "[DIR] a/" in result, \
            f"Empty dir a should be marked: {result}"
        # Check all nested empty dirs appear
        assert "[DIR] b/" in result, f"Dir b should appear: {result}"
        assert "[DIR] c/ (empty)" in result, f"Empty dir c should be marked: {result}"
        print("[PASS]\n")

def test_root_files_not_attached_to_last_subdir():
    """Verify root files are NOT visually attached to any subdir (requirement b)."""
    print("=== Test: Root files not attached to subdir ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "sub").mkdir()
        (Path(tmpdir) / "root1.txt").write_text("1")
        (Path(tmpdir) / "root2.txt").write_text("2")
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True)
        print(result)
        # Root files should be at column 0, not indented under any subdir
        for fname in ("root1.txt", "root2.txt"):
            line = next(l for l in result.splitlines() if l.strip().startswith(fname))
            assert not line.startswith(" "), f"{fname} must be at column 0: {line!r}"
        # Ensure no root file appears after a subdir line with same or greater indent
        lines = result.splitlines()
        subdir_indent = None
        for line in lines:
            if "[DIR]" in line:
                subdir_indent = len(line) - len(line.lstrip())
            elif line.strip() and not line.startswith("  " + (" " * (subdir_indent or 0))):
                # A file line that is not indented more than the last subdir
                if subdir_indent is not None and not line.startswith(" " * subdir_indent):
                    # This is a root-level file; it should have no leading spaces
                    assert not line.startswith(" "), f"Root file after subdir with wrong indent: {line!r}"
        print("[PASS]\n")

if __name__ == "__main__":
    tests = [
        test_root_only_empty_subdir,
        test_filter_excludes_all_in_subdir,
        test_os_walk_ordering,
        test_max_entries_totals,
        test_symlink_cycle_guard,
        test_empty_dir_with_nested_empty,
        test_root_files_not_attached_to_last_subdir,
    ]
    for test in tests:
        try:
            test()
        except AssertionError as e:
            print(f"[FAIL] {test.__name__}: {e}")
        except Exception as e:
            print(f"[ERROR] {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
