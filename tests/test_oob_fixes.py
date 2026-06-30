"""Comprehensive verification of OOB fixes in file_operations.py."""
import sys, os, tempfile, logging

# Suppress logging noise
logging.disable(logging.WARNING)

from pathlib import Path
sys.path.insert(0, r"N:\work\WD\AgentCascade_unified")

from agent_cascade.operation_manager.file_operations import FileOpsMixin


class MockOpMgr(FileOpsMixin):
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.file_ownership = {}
    
    def _resolve_path(self, path, mode="ro"):
        resolved = (self.base_dir / path).resolve()
        if mode == "rw" and not resolved.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return resolved
    
    def _is_auto_approved(self, path, agent_name, creating_new=False):
        return True


def main():
    tmpdir = tempfile.mkdtemp()
    op_mgr = MockOpMgr(tmpdir)
    
    passed = 0
    failed = 0

    def check(name, condition, result_str=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {name}")
            return True
        else:
            failed += 1
            print(f"  [FAIL] {name}")
            if result_str:
                print(f"         Result: {result_str[:200]}")
            return False

    # FIX 3: read_file out-of-bounds check
    print("\n" + "=" * 60)
    print("FIX 3: read_file out-of-bounds check")
    print("=" * 60)

    test_file = Path(tmpdir) / "small.txt"
    test_file.write_text("line1\nline2\nline3\n", encoding='utf-8')

    # T1: start_line beyond EOF
    result = op_mgr.read_file("small.txt", start_line=5, limit=10)
    check("start_line=5 on 3-line file returns error",
          "ERROR" in result and "beyond the end of file" in result, result)

    # T2: Normal read within bounds
    result = op_mgr.read_file("small.txt", start_line=1, limit=3)
    check("Normal read lines 1-3 returns content",
          "line1:" in result and "line3:" in result and "ERROR" not in result, result)

    # T3: start_line exactly at last line
    result = op_mgr.read_file("small.txt", start_line=3, limit=10)
    check("start_line=3 (last line) returns content",
          "line3:" in result and "ERROR" not in result, result)

    # T4: Empty file
    empty_file = Path(tmpdir) / "empty.txt"
    empty_file.write_text("", encoding='utf-8')
    result = op_mgr.read_file("empty.txt", start_line=1, limit=10)
    check("Empty file (0 lines), start_line=1 returns error",
          "ERROR" in result and "beyond the end of file" in result, result)

    # T5: Single-line file
    single_file = Path(tmpdir) / "single.txt"
    single_file.write_text("only line\n", encoding='utf-8')
    result = op_mgr.read_file("single.txt", start_line=1, limit=10)
    check("Single-line file read works",
          "only line:" in result and "ERROR" not in result, result)

    # T6: start_line = total_lines + 1 (just beyond)
    result = op_mgr.read_file("small.txt", start_line=4, limit=5)
    check("start_line=4 on 3-line file returns error",
          "ERROR" in result and "beyond the end of file" in result, result)

    # T7: Reading from near-last with limit beyond EOF (no truncation marker needed)
    result = op_mgr.read_file("small.txt", start_line=2, limit=5)
    check("Read hits real EOF without [TRUNCATED]",
          "line2:" in result and "line3:" in result and "[TRUNCATED]" not in result, result)

    # FIX 1 & 2: re_indent dead code removed + bounds check before clamp
    print("\n" + "=" * 60)
    print("FIX 1 & 2: re_indent - dead code removal + bounds checking")
    print("=" * 60)

    test_file2 = Path(tmpdir) / "indent_test.py"
    test_file2.write_text("def foo():\n    pass\nbar()\nbaz()\n", encoding='utf-8')  # 4 lines

    # T9: start_line exceeds file length
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="5:10", indent=4, indent_type="space", mode="min"
    )
    check("start_line=5 on 4-line file returns bounds error",
          "ERROR" in result and "exceeds file length" in result, result)

    # T10: end_line exceeds file length
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="1:10", indent=4, indent_type="space", mode="min"
    )
    check("end_line=10 on 4-line file returns bounds error",
          "ERROR" in result and "exceeds file length" in result, result)

    # T11: Negative start_line (caught by >= 1 check, NOT negative index conversion)
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="-3:-1", indent=4, indent_type="space", mode="min"
    )
    check("Negative start_line=-3 returns >= 1 error (dead code removed)",
          "ERROR" in result and ">= 1" in result, result)

    # T12: Negative end_line
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="1:-5", indent=4, indent_type="space", mode="min"
    )
    check("Negative end_line=-5 returns >= 1 error",
          "ERROR" in result and ">= 1" in result, result)

    # T13: Normal re_indent still works (regression check)
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="1:4", indent=4, indent_type="space", mode="min"
    )
    check("Normal re_indent returns APPROVED",
          "APPROVED" in result, result)

    # T14: start == end (invalid range)
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="4:4", indent=4, indent_type="space", mode="min"
    )
    check("start==end returns 'Start must be less than end' error",
          "ERROR" in result and "Start must be less than end" in result, result)

    # T15: Boundary - end = total_lines + 1 (bounds error, NOT silent clamp)
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="4:5", indent=4, indent_type="space", mode="min"
    )
    check("end=total_lines+1 returns bounds error (not silent clamp)",
          "ERROR" in result and "exceeds file length" in result, result)

    # T16: Dead code removal verification - -2:-1 should NOT convert to valid range
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="-2:-1", indent=4, indent_type="space", mode="min"
    )
    check("Dead code removed: -2:-1 returns >= 1 error (not converted)",
          "ERROR" in result and ">= 1" in result, result)

    # T17: Empty file re_indent edge case
    empty_file2 = Path(tmpdir) / "empty_indent.py"
    empty_file2.write_text("", encoding='utf-8')
    result = op_mgr.re_indent(
        path="empty_indent.py", agent_name="test_agent",
        lines="1:5", indent=4, indent_type="space", mode="min"
    )
    check("Empty file (0 lines) returns bounds error for any range",
          "ERROR" in result and "exceeds file length" in result, result)

    # T18: Valid boundary case still works
    test_file3 = Path(tmpdir) / "boundary_test.py"
    test_file3.write_text("a\nb\nc\n", encoding='utf-8')  # 3 lines
    result = op_mgr.re_indent(
        path="boundary_test.py", agent_name="test_agent",
        lines="1:3", indent=2, indent_type="space", mode="min"
    )
    check("Valid range 1:3 on 3-line file works correctly",
          "APPROVED" in result, result)

    # T19: Large start_line returns error immediately (no iteration)
    result = op_mgr.read_file("small.txt", start_line=100, limit=1)
    check("Large start_line=100 returns error immediately",
          "ERROR" in result and "beyond the end of file" in result, result)

    # SUMMARY
    total = passed + failed
    print("\n" + "=" * 60)
    print(f"RESULTS: {passed}/{total} tests passed, {failed} failed")
    if failed == 0:
        verdict = "PASS - All fixes verified correctly"
    else:
        verdict = f"FAIL - {failed} test(s) need attention"
    print(f"VERDICT: {verdict}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())