import pytest
import os
import tempfile
import time
from pathlib import Path
from agent_cascade.operation_manager import OperationManager

def test_edit_file_modes():
    with tempfile.TemporaryDirectory() as tmpdir:
        op_mgr = OperationManager(base_dir=tmpdir)
        op_mgr.file_ownership = {}
        
        file_path = Path(tmpdir) / "test_file.txt"
        
        # Test Case 1: Exact Match (Default)
        file_path.write_text("line 1\nline 2\nline 3\n", encoding='utf-8')
        op_mgr.file_ownership[str(file_path.resolve())] = "test_agent"
        
        # Exact match edit should succeed
        res = op_mgr.edit_file(
            path="test_file.txt",
            agent_name="test_agent",
            old_content="line 2\n",
            new_content="line 2 modified\n",
            match_mode="exact"
        )
        assert "OK:" in res
        assert file_path.read_text(encoding='utf-8') == "line 1\nline 2 modified\nline 3\n"
        
        # Exact match with whitespace differences should fail
        res = op_mgr.edit_file(
            path="test_file.txt",
            agent_name="test_agent",
            old_content="line   2   modified\n",
            new_content="line 2 again\n",
            match_mode="exact"
        )
        assert "ERROR" in res
        
        # Test Case 2: Heuristic Match
        # Match with spaces / tabs / line endings
        res = op_mgr.edit_file(
            path="test_file.txt",
            agent_name="test_agent",
            old_content="line \t 2 \t modified",  # spaces & tabs, no trailing newline
            new_content="line 2 heuristic ok",  # no trailing newline
            match_mode="heuristic"
        )
        assert "OK:" in res
        # It should preserve the trailing newline since the matched block had one
        assert file_path.read_text(encoding='utf-8') == "line 1\nline 2 heuristic ok\nline 3\n"
        
        # Test Case 3: Line ending normalization
        # Write with CRLF bytes directly to avoid double translation on write_text
        file_path.write_bytes(b"first\r\nsecond\r\nthird\r\n")
        res = op_mgr.edit_file(
            path="test_file.txt",
            agent_name="test_agent",
            old_content="second\n",  # LF line ending in old_content
            new_content="second modified", # no trailing newline
            match_mode="heuristic"
        )
        assert "OK:" in res
        # Should preserve CRLF ending of the matched block on Windows / LF on Unix
        expected_bytes = b"first\r\nsecond modified\r\nthird\r\n" if os.name == 'nt' else b"first\nsecond modified\nthird\n"
        assert file_path.read_bytes() == expected_bytes
        
        # Test Case 4: Non-unique heuristic matches should fail
        file_path.write_text("dup\ndup\nother\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt",
            agent_name="test_agent",
            old_content="dup",
            new_content="replaced",
            match_mode="heuristic"
        )
        assert "ERROR" in res
        assert "found 2 times" in res or "found 2 times" in res.lower()

        # Test Case 5: Empty pattern check
        res = op_mgr.edit_file(
            path="test_file.txt",
            agent_name="test_agent",
            old_content="   \n   \n",
            new_content="something",
            match_mode="heuristic"
        )
        assert "ERROR" in res
        assert "whitespace" in res.lower()

def test_large_file_performance():
    with tempfile.TemporaryDirectory() as tmpdir:
        op_mgr = OperationManager(base_dir=tmpdir)
        op_mgr.file_ownership = {}
        
        file_path = Path(tmpdir) / "large_file.txt"
        
        # Generate a large file with 50,000 lines
        lines = []
        for i in range(50000):
            if i == 25000:
                lines.append("def unique_target_function(x, y):\n    return x + y\n")
            else:
                # Add duplicate generic lines to create candidate noise
                lines.append(f"generic_line_{i % 1000}\n")
        
        file_path.write_text("".join(lines), encoding='utf-8')
        op_mgr.file_ownership[str(file_path.resolve())] = "test_agent"
        
        # Edit the unique target function with some whitespace discrepancies
        old_content = "def   unique_target_function  (x,  y):\n    return  x  +  y"
        new_content = "def unique_target_function(x, y):\n    # Added some documentation\n    return x + y"
        
        start_time = time.perf_counter()
        res = op_mgr.edit_file(
            path="large_file.txt",
            agent_name="test_agent",
            old_content=old_content,
            new_content=new_content,
            match_mode="heuristic"
        )
        end_time = time.perf_counter()
        elapsed_ms = (end_time - start_time) * 1000
        
        assert "OK:" in res
        assert elapsed_ms < 120.0, f"Performance test failed: elapsed time was {elapsed_ms:.2f}ms (expected < 120ms)"
        print(f"\nLarge file search on 50,000 lines took {elapsed_ms:.2f}ms")

def test_heuristic_indentation_alignment():
    with tempfile.TemporaryDirectory() as tmpdir:
        op_mgr = OperationManager(base_dir=tmpdir)
        op_mgr.file_ownership = {}

        # Case 1: Surrounding block has higher indentation (8 spaces) but old/new are 4 spaces
        file_path_py = Path(tmpdir) / "nested.py"
        file_path_py.write_text(
            "class MyClass:\n"
            "    def my_func():\n"
            "        x = 1\n"
            "        y = 2\n",
            encoding='utf-8'
        )
        op_mgr.file_ownership[str(file_path_py.resolve())] = "test_agent"

        old_content = "def my_func():\n    x = 1\n    y = 2"
        new_content = "def my_func():\n    x = 10\n    y = 20"

        res = op_mgr.edit_file(
            path="nested.py",
            agent_name="test_agent",
            old_content=old_content,
            new_content=new_content,
            match_mode="heuristic"
        )
        assert "OK:" in res
        expected = (
            "class MyClass:\n"
            "    def my_func():\n"
            "        x = 10\n"
            "        y = 20\n"
        )
        assert file_path_py.read_text(encoding='utf-8') == expected

        # Case 2: Single line replacement with unindented query
        file_path_single = Path(tmpdir) / "single.py"
        file_path_single.write_text(
            "            x = 1\n",
            encoding='utf-8'
        )
        op_mgr.file_ownership[str(file_path_single.resolve())] = "test_agent"

        res = op_mgr.edit_file(
            path="single.py",
            agent_name="test_agent",
            old_content="x = 1",
            new_content="x = 2",
            match_mode="heuristic"
        )
        assert "OK:" in res
        assert file_path_single.read_text(encoding='utf-8') == "            x = 2\n"

        # Case 3: Tabs indentation conversion and adjustment
        file_path_tabs = Path(tmpdir) / "tabs.py"
        file_path_tabs.write_text(
            "class Foo:\n"
            "\tdef bar(self):\n"
            "\t\tx = 1\n",
            encoding='utf-8'
        )
        op_mgr.file_ownership[str(file_path_tabs.resolve())] = "test_agent"

        res = op_mgr.edit_file(
            path="tabs.py",
            agent_name="test_agent",
            old_content="x = 1",
            new_content="x = 2",
            match_mode="heuristic"
        )
        assert "OK:" in res
        assert file_path_tabs.read_text(encoding='utf-8') == "class Foo:\n\tdef bar(self):\n\t\tx = 2\n"


def test_delete_and_insert_mode():
    """Test all 20 scenarios for the delete_and_insert match_mode."""
    with tempfile.TemporaryDirectory() as tmpdir:
        op_mgr = OperationManager(base_dir=tmpdir)
        op_mgr.file_ownership = {}

        file_path = Path(tmpdir) / "test_file.txt"

        # ── Test 1: Normal delete+insert ────────────────────────────────
        file_path.write_text("line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\n", encoding='utf-8')
        op_mgr.file_ownership[str(file_path.resolve())] = "test_agent"

        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="3:5", new_content="X\nY\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 1 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        assert content == "line1\nline2\nX\nY\nline6\nline7\nline8\n", f"Test 1 assertion: got [{content}]"

        # ── Test 2: Delete only (empty new_content) ─────────────────────
        file_path.write_text("a\nb\nc\nd\ne\nf\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="2:4", new_content="", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 2 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        assert content == "a\ne\nf\n", f"Test 2 assertion: got [{content}]"

        # ── Test 3: Insert only (single number) ────────────────────────
        file_path.write_text("l1\nl2\nl3\nl4\nl5\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="3", new_content="inserted\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 3 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        assert content == "l1\nl2\ninserted\nl3\nl4\nl5\n", f"Test 3 assertion: got [{content}]"

        # ── Test 4: Append at end (start=0) ────────────────────────────
        file_path.write_text("a\nb\nc\nd\ne\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="0", new_content="footer\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 4 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        assert content == "a\nb\nc\nd\ne\nfooter\n", f"Test 4 assertion: got [{content}]"

        # ── Test 5: Insert at start (start=1) ──────────────────────────
        file_path.write_text("l1\nl2\nl3\nl4\nl5\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="1", new_content="header\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 5 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        assert content == "header\nl1\nl2\nl3\nl4\nl5\n", f"Test 5 assertion: got [{content}]"

        # ── Test 6: Negative index insert ──────────────────────────────
        file_path.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="-1", new_content="near_end\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 6 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # -1 means insert before last line (between 7 and 8)
        assert content == "l1\nl2\nl3\nl4\nl5\nl6\nl7\nnear_end\nl8\n", f"Test 6 assertion: got [{content}]"

        # ── Test 7: Negative range delete+insert ───────────────────────
        file_path.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="-3:-1", new_content="replaced\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 7 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # -3=line 8, -1=line 10 → delete lines 8-9 (exclusive of 10), insert at line 8
        assert content == "l1\nl2\nl3\nl4\nl5\nl6\nl7\nreplaced\nl10\n", f"Test 7 assertion: got [{content}]"

        # ── Test 8: Delete entire file content ─────────────────────────
        lines = [f"line{i}\n" for i in range(1, 11)]
        file_path.write_text("".join(lines), encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="1:10", new_content="", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 8 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        assert content == "", f"Test 8 assertion: got [{content}]"

        # ── Test 9: Out-of-bounds clamping ─────────────────────────────
        file_path.write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="5:20", new_content="end\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 9 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # Lines 1-4 preserved, lines 5-10 deleted, "end" inserted at pos 5
        assert content == "line1\nline2\nline3\nline4\nend\n", f"Test 9 assertion: got [{content}]"

        # ── Test 10: Single line delete+replace ────────────────────────
        file_path.write_text("".join(f"line{i}\n" for i in range(1, 9)), encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="4:4", new_content="new_line\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 10 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        assert content == "line1\nline2\nline3\nnew_line\nline5\nline6\nline7\nline8\n", f"Test 10 assertion: got [{content}]"

        # ── Test 11: CRLF preservation ─────────────────────────────────
        file_path.write_bytes(b"l1\r\nl2\r\nl3\r\nl4\r\nl5\r\nl6\r\n")
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="3:3", new_content="mid", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 11 failed: {res}"
        content = file_path.read_bytes()
        # Inserted line should get CRLF ending to match surrounding context
        assert b"l1\r\nl2\r\nmid\r\nl4\r\nl5\r\nl6\r\n" == content, f"Test 11 assertion: got [{content}]"

        # ── Test 12: Empty range at middle (pure insert past end) ──────
        file_path.write_text("a\nb\nc\nd\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="5", new_content="x\ny\nz\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 12 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # Line 5 is past end → clamped to append at end
        assert content == "a\nb\nc\nd\nx\ny\nz\n", f"Test 12 assertion: got [{content}]"

        # ── Test 13: Invalid format error ──────────────────────────────
        file_path.write_text("a\nb\nc\nd\ne\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="abc:xyz", new_content="test\n", match_mode="delete_and_insert"
        )
        assert "ERROR" in res, f"Test 13 failed (expected error): {res}"

        # ── Test 13b: Empty start ":5" means delete all up to line 5 (including) ──
        file_path.write_text("a\nb\nc\nd\ne\nf\ng\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content=":5", new_content="", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 13b failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # Delete lines 1-5 → only f\ng remain
        assert content == "f\ng\n", f"Test 13b assertion: got [{content}]"

        # ── Test 13c: Empty end "5:" means delete all from line 5 onward ──
        file_path.write_text("a\nb\nc\nd\ne\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="3:", new_content="", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 13c failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # Delete from line 3 onward → only a\nb remain
        assert content == "a\nb\n", f"Test 13c assertion: got [{content}]"

        # ── Test 14: Negative out-of-bounds clamping ───────────────────
        file_path.write_text("a\nb\nc\nd\ne\n", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="-100", new_content="start\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 14 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # -100 clamped to beginning → insert at start
        assert content == "start\na\nb\nc\nd\ne\n", f"Test 14 assertion: got [{content}]"

        # ── Test 15: Multi-line insert into CRLF file ───────────────────
        file_path.write_bytes(b"l1\r\nl2\r\nl3\r\nl4\r\nl5\r\n")
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="3:3", new_content="X\nY", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 15 failed: {res}"
        content = file_path.read_bytes()
        # Both X and Y should get \r\n to match surrounding CRLF context
        assert b"l1\r\nl2\r\nX\r\nY\r\nl4\r\nl5\r\n" == content, f"Test 15 assertion: got [{content}]"

        # ── Test 16: Delete all but last line with ending preservation ───
        file_path.write_bytes(b"a\r\nb\r\nc\r\nd\r\nlast\r\n")
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="1:4", new_content="", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 16 failed: {res}"
        content = file_path.read_bytes()
        # Only "last\r\n" should remain — ending preserved from after[0]
        assert b"last\r\n" == content, f"Test 16 assertion: got [{content}]"

        # ── Test 17: Empty file with non-zero range ─────────────────────
        file_path.write_text("", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="1:5", new_content="content\n", match_mode="delete_and_insert"
        )
        assert "ERROR" in res, f"Test 17 failed (expected error): {res}"

        # ── Test 18: Empty file with append (range "0") ────────────────
        file_path.write_text("", encoding='utf-8')
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="0", new_content="appended\n", match_mode="delete_and_insert"
        )
        assert "OK:" in res, f"Test 18 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        assert content == "appended\n", f"Test 18 assertion: got [{content}]"


def test_re_indent_shift_mode():
    """Test all scenarios for the new shift mode of re_indent.

    Shift mode behavior:
      - Positive indent: strips existing leading whitespace, prepends N chars of specified type
      - Negative indent: removes up to N leading whitespace chars (type-agnostic)
      - Zero indent: no-op, lines unchanged
      - Blank lines always pass through unchanged regardless of sign
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        op_mgr = OperationManager(base_dir=tmpdir)
        op_mgr.file_ownership = {}

        file_path = Path(tmpdir) / "shift_test.py"

        # ── Test 1: Positive shift (add spaces) ────────────────────────
        # Existing whitespace is stripped; N new space chars are prepended.
        file_path.write_text(
            "    def foo():\n"
            "        bar()\n"
            "        baz()\n",
            encoding='utf-8'
        )
        op_mgr.file_ownership[str(file_path.resolve())] = "test_agent"

        res = op_mgr.re_indent(
            path="shift_test.py", agent_name="test_agent",
            lines="1:3", indent=2, indent_type="space", mode="shift"
        )
        assert "OK:" in res, f"Test 1 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # Each line's ws stripped → prefix "  " prepended to bare content
        expected = (
            "  def foo():\n"
            "  bar()\n"
            "  baz()\n"
        )
        assert content == expected, f"Test 1 assertion: got [{content}]"

        # ── Test 2: Negative shift (remove leading chars) ──────────────
        file_path.write_text(
            "        def foo():\n"     # 8 spaces
            "            bar()\n"      # 12 spaces
            "            baz()\n",     # 12 spaces
            encoding='utf-8'
        )

        res = op_mgr.re_indent(
            path="shift_test.py", agent_name="test_agent",
            lines="1:3", indent=-4, indent_type="space", mode="shift"
        )
        assert "OK:" in res, f"Test 2 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # Remove min(4, ws_count) leading chars from each line
        expected = (
            "    def foo():\n"      # 8 - 4 = 4 spaces remain
            "        bar()\n"       # 12 - 4 = 8 spaces remain
            "        baz()\n"
        )
        assert content == expected, f"Test 2 assertion: got [{content}]"

        # ── Test 3: Negative shift removes all (clamp to 0) ────────────
        file_path.write_text(
            "  def foo():\n"         # 2 spaces
            "    bar()\n",           # 4 spaces
            encoding='utf-8'
        )

        res = op_mgr.re_indent(
            path="shift_test.py", agent_name="test_agent",
            lines="1:2", indent=-5, indent_type="space", mode="shift"
        )
        assert "OK:" in res, f"Test 3 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # All leading whitespace removed (min(5,2)=2 and min(5,4)=4)
        expected = (
            "def foo():\n"
            "bar()\n"
        )
        assert content == expected, f"Test 3 assertion: got [{content}]"

        # ── Test 4: Mixed indentation preserved during positive shift ──
        file_path.write_text(
            "\tdef foo():\n"
            "\t\tbar()\n",
            encoding='utf-8'
        )

        res = op_mgr.re_indent(
            path="shift_test.py", agent_name="test_agent",
            lines="1:2", indent=2, indent_type="space", mode="shift"
        )
        assert "OK:" in res, f"Test 4 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        # Positive shift strips ws then prepends → tabs replaced by spaces
        expected = (
            "  def foo():\n"
            "  bar()\n"
        )
        assert content == expected, f"Test 4 assertion: got [{content}]"

        # ── Test 5: Blank lines preserved ──────────────────────────────
        file_path.write_text(
            "    def foo():\n"
            "\n"
            "        bar()\n",
            encoding='utf-8'
        )

        res = op_mgr.re_indent(
            path="shift_test.py", agent_name="test_agent",
            lines="1:3", indent=3, indent_type="space", mode="shift"
        )
        assert "OK:" in res, f"Test 5 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        expected = (
            "   def foo():\n"    # stripped + 3 spaces
            "\n"                 # blank line unchanged
            "   bar()\n"         # stripped + 3 spaces
        )
        assert content == expected, f"Test 5 assertion: got [{content}]"

        # ── Test 6: Zero indent (no-op) ────────────────────────────────
        file_path.write_text(
            "    def foo():\n"
            "\t\tbar()\n",
            encoding='utf-8'
        )

        res = op_mgr.re_indent(
            path="shift_test.py", agent_name="test_agent",
            lines="1:2", indent=0, indent_type="space", mode="shift"
        )
        assert "OK:" in res, f"Test 6 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        expected = (
            "    def foo():\n"   # unchanged
            "\t\tbar()\n"       # unchanged
        )
        assert content == expected, f"Test 6 assertion: got [{content}]"

        # ── Test 7: Tab addition ───────────────────────────────────────
        file_path.write_text(
            "def foo():\n"
            "bar()\n",
            encoding='utf-8'
        )

        res = op_mgr.re_indent(
            path="shift_test.py", agent_name="test_agent",
            lines="1:2", indent=2, indent_type="tab", mode="shift"
        )
        assert "OK:" in res, f"Test 7 failed: {res}"
        content = file_path.read_text(encoding='utf-8')
        expected = (
            "\t\tdef foo():\n"   # 2 tabs prepended + stripped content
            "\t\tbar()\n"       # same
        )
        assert content == expected, f"Test 7 assertion: got [{content}]"

