#!/usr/bin/env python3
"""Test to confirm _matches_filters exclude bug."""
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from agent_cascade.operation_manager import OperationManager

def test_exclude_inverted():
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "include.txt").write_text("1")
        Path(tmpdir, "exclude.txt").write_text("2")
        Path(tmpdir, "keep.log").write_text("3")
        om = OperationManager(base_dir=tmpdir)
        # Exclude *.txt - should only keep keep.log
        result = om.list_directory(".", exclude="*.txt")
        print(result)
        if "include.txt" in result:
            print("[BUG] include.txt should be excluded")
        if "exclude.txt" in result:
            print("[BUG] exclude.txt should be excluded")
        if "keep.log" not in result:
            print("[BUG] keep.log should be included")

if __name__ == "__main__":
    test_exclude_inverted()
