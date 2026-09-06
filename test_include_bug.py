#!/usr/bin/env python3
"""Test include filter behavior with nested files."""
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from agent_cascade.operation_manager import OperationManager

def test_include_nested_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        sub = Path(tmpdir) / "a"
        sub.mkdir()
        (sub / "a.txt").write_text("1")
        (Path(tmpdir) / "root.txt").write_text("2")
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory(".", recursive=True, include="*.txt")
        print(result)
        if "root.txt" not in result:
            print("[BUG] root.txt should be included")
        if "a.txt" not in result:
            print("[BUG] a.txt should be included (nested file)")
        # Also check that subdir "a" is not listed (since it doesn't match *.txt)
        if "[DIR] a/" in result:
            print("[BUG] subdir 'a' should not be listed")
        print()

if __name__ == "__main__":
    test_include_nested_files()
