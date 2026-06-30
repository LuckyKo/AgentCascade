"""Focused verification of ONLY the 3 OOB fixes."""
import sys, os, tempfile, logging
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

    def check(name, condition):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {name}")
            return True
        else:
            failed += 1
            print(f"  [FAIL] {name}")
            return False

    # ════════════════════════════════════════
    # FIX 3: read_file out-of-bounds check
    # ════════════════════════════════════════
    print("\n" + "=" * 60)
    print("FIX 3: read_file - start_line beyond EOF check")
    print("=" * 60)

    test_file = Path(tmpdir) / "small.txt"
    test_file.write_text("line1\nline2\nline3\n", encoding='utf-8')

    # F3.1: start_line > total_lines → ERROR (not silent truncation or weird output)
    result = op_mgr.read_file("small.txt", start_line=5, limit=10)
    check("start_line=5 on 3-line file returns clear ERROR message",
          "ERROR" in result and "beyond the end of file" in result)

    # F3.2: start_line = total_lines + 1 → ERROR
    result = op_mgr.read_file("small.txt", start_line=4, limit=5)
    check("start_line=total_lines+1 returns clear ERROR message",
          "ERROR" in result and "beyond the end of file" in result)

    # F3.3: Empty file (0 lines), start_line=1 → ERROR
    empty_file = Path(tmpdir) / "empty.txt"
    empty_file.write_text("", encoding='utf-8')
    result = op_mgr.read_file("empty.txt", start_line=1, limit=10)
    check("Empty file (0 lines), start_line=1 returns clear ERROR message",
          "ERROR" in result and "beyond the end of file" in result)

    # F3.4: Normal reads still work (regression)
    result = op_mgr.read_file("small.txt", start_line=1, limit=2)
    check("Normal read within bounds returns content (not error)",
          "ERROR" not in result and "line1" in result and "line2" in result)

    # F3.5: Reading from last line works
    result = op_mgr.read_file("small.txt", start_line=3, limit=5)
    check("Reading from last line returns content (not error)",
          "ERROR" not in result and "line3" in result)

    # ════════════════════════════════════════
    # FIX 1: re_indent dead code removed
    # ════════════════════════════════════════
    print("\n" + "=" * 60)
    print("FIX 1: re_indent - Dead negative-index code removed")
    print("=" * 60)

    test_file2 = Path(tmpdir) / "indent_test.py"
    test_file2.write_text("def foo():\n    pass\nbar()\nbaz()\n", encoding='utf-8')  # 4 lines

    # F1.1: Negative start_line → ERROR (not converted via old dead code to positive)
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="-3:-1", indent=4, indent_type="space", mode="min"
    )
    check("Negative start_line=-3 returns >= 1 error (not silently converted)",
          "ERROR" in result and ">= 1" in result)

    # F1.2: Negative end_line → ERROR
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="1:-5", indent=4, indent_type="space", mode="min"
    )
    check("Negative end_line=-5 returns >= 1 error",
          "ERROR" in result and ">= 1" in result)

    # F1.3: Verify no residual negative-index adjustment exists
    # Old code would have converted -3 to (4-3)=1 on a 4-line file. Now it errors.
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="-2:-1", indent=4, indent_type="space", mode="min"
    )
    check("-2:-1 returns >= 1 error (dead code fully removed)",
          "ERROR" in result and ">= 1" in result)

    # ════════════════════════════════════════
    # FIX 2: re_indent bounds checking BEFORE clamping
    # ════════════════════════════════════════
    print("\n" + "=" * 60)
    print("FIX 2: re_indent - Bounds checked BEFORE clamping")
    print("=" * 60)

    test_file2 = Path(tmpdir) / "indent_test.py"
    test_file2.write_text("def foo():\n    pass\nbar()\nbaz()\n", encoding='utf-8')  # 4 lines

    # F2.1: start_line > total_lines → ERROR (not silently clamped to total_lines)
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="5:10", indent=4, indent_type="space", mode="min"
    )
    check("start_line=5 on 4-line file returns bounds error (not clamped)",
          "ERROR" in result and "exceeds file length" in result)

    # F2.2: end_line > total_lines → ERROR (not silently clamped to total_lines)
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="1:10", indent=4, indent_type="space", mode="min"
    )
    check("end_line=10 on 4-line file returns bounds error (not clamped)",
          "ERROR" in result and "exceeds file length" in result)

    # F2.3: end = total_lines + 1 → ERROR (boundary case, not silently accepted)
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="4:5", indent=4, indent_type="space", mode="min"
    )
    check("end=total_lines+1 returns bounds error (not silently clamped)",
          "ERROR" in result and "exceeds file length" in result)

    # F2.4: start = total_lines → OK (valid boundary, not rejected)
    test_file3 = Path(tmpdir) / "boundary_test.py"
    test_file3.write_text("a\nb\nc\n", encoding='utf-8')  # 3 lines
    result = op_mgr.re_indent(
        path="boundary_test.py", agent_name="test_agent",
        lines="1:3", indent=2, indent_type="space", mode="min"
    )
    check("Valid range start=end=total_lines works (not over-rejected)",
          "APPROVED" in result)

    # F2.5: Normal large range beyond file → ERROR with clear message
    result = op_mgr.re_indent(
        path="indent_test.py", agent_name="test_agent",
        lines="100:200", indent=4, indent_type="space", mode="min"
    )
    check("Very large range returns bounds error with file length info",
          "ERROR" in result and "exceeds file length" and "4 lines" in result)

    # ════════════════════════════════════════
    # REGRESSION: Normal operations still work
    # ════════════════════════════════════════
    print("\n" + "=" * 60)
    print("REGRESSION: Normal operations unchanged")
    print("=" * 60)

    test_file4 = Path(tmpdir) / "normal_test.py"
    test_file4.write_text("def foo():\n    pass\nbar()\nbaz()\n", encoding='utf-8')  # 4 lines
    
    result = op_mgr.re_indent(
        path="normal_test.py", agent_name="test_agent",
        lines="1:4", indent=4, indent_type="space", mode="min"
    )
    check("Normal re_indent (valid range) returns APPROVED",
          "APPROVED" in result)

    # ════════════════════════════════════════
    print("\n" + "=" * 60)
    total = passed + failed
    print(f"RESULTS: {passed}/{total} tests passed, {failed} failed")
    if failed == 0:
        verdict = "PASS - All OOB fixes verified correctly"
    else:
        verdict = f"FAIL - {failed} test(s) need attention"
    print(f"VERDICT: {verdict}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())