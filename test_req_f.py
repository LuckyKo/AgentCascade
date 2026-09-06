#!/usr/bin/env python3
"""Test requirement (f) specifically."""
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from agent_cascade.operation_manager import OperationManager

def test_root_only_empty_subdir_no_empty_msg():
    """Root with only an empty subdir should NOT show top-level empty message."""
    print("=== Test (f): Root with only empty subdir ===")
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

def test_root_only_empty_subdir_with_max_entries():
    """Root with only an empty subdir, but max_entries truncates it.
    Does empty message appear? This is ambiguous."""
    print("=== Test (f) with max_entries=0 ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "empty_sub").mkdir()
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True, max_entries=0)
        print(result)
        # With max_entries=0, no entries shown. Should we show empty message?
        # Since no entries are shown, the output is empty, and is_empty will be True.
        # This might be acceptable for max_entries=0.
        if "(empty directory or all entries filtered out)" in result:
            print("[INFO] Empty message appears because max_entries=0 shows nothing")
        else:
            print("[INFO] No empty message (maybe total_dirs>0 even with max_entries=0?)")
        print()

def test_root_only_subdir_filter_excluded():
    """Root with a subdir that is entirely filter-excluded.
    Ambiguous: should we show empty message?"""
    print("=== Test (f) with filter excluding subdir ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        sub = Path(tmpdir, "sub")
        sub.mkdir()
        (sub / "file.txt").write_text("hello")
        om = OperationManager(base_dir=tmpdir)
        # Exclude the subdir itself by matching its name?
        result = om.list_directory(".", recursive=True, exclude="sub")
        print(result)
        # The subdir is excluded, so it won't appear. Root has no other entries.
        # This should show empty message because no entries are visible.
        if "(empty directory or all entries filtered out)" in result:
            print("[INFO] Empty message appears as expected")
        else:
            print("[INFO] No empty message - unexpected?")
        print()

if __name__ == "__main__":
    test_root_only_empty_subdir_no_empty_msg()
    test_root_only_empty_subdir_with_max_entries()
    test_root_only_subdir_filter_excluded()
