import pytest
import os
import tempfile
import time
from pathlib import Path
from operation_manager import OperationManager

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
        assert "APPROVED" in res
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
        assert "APPROVED" in res
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
        assert "APPROVED" in res
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
        
        assert "APPROVED" in res
        assert elapsed_ms < 120.0, f"Performance test failed: elapsed time was {elapsed_ms:.2f}ms (expected < 120ms)"
        print(f"\nLarge file search on 50,000 lines took {elapsed_ms:.2f}ms")

@pytest.mark.skip(reason="Comment stripping is not part of heuristic matching per unified branch design — see test_heuristic_comment_fix.py")
def test_heuristic_refinements():
    with tempfile.TemporaryDirectory() as tmpdir:
        op_mgr = OperationManager(base_dir=tmpdir)
        op_mgr.file_ownership = {}
        
        # Test Case A: Python comment stripping and blank line resiliency
        file_path_py = Path(tmpdir) / "script.py"
        file_path_py.write_text(
            "def test_func():\n"
            "    x = 1  # inline comment in file\n"
            "\n"
            "\n"
            "    y = 2  # another comment\n"
            "    return x + y\n",
            encoding='utf-8'
        )
        op_mgr.file_ownership[str(file_path_py.resolve())] = "test_agent"
        
        # Old content: no comments, fewer blank lines
        old_content_py = (
            "def test_func():\n"
            "    x = 1\n"
            "    y = 2\n"
            "    return x + y\n"
        )
        new_content_py = (
            "def test_func():\n"
            "    x = 10\n"
            "    y = 20\n"
            "    return x + y\n"
        )
        
        res = op_mgr.edit_file(
            path="script.py",
            agent_name="test_agent",
            old_content=old_content_py,
            new_content=new_content_py,
            match_mode="heuristic"
        )
        assert "APPROVED" in res
        # Intermediate blank lines and comments should be replaced
        assert file_path_py.read_text(encoding='utf-8') == new_content_py

        # Test Case B: JS/C single-line and multi-line comment stripping
        file_path_js = Path(tmpdir) / "script.js"
        file_path_js.write_text(
            "function add(a, b) {\n"
            "    // single-line comment\n"
            "    /* multi-line \n"
            "       comment */\n"
            "    return a + b; /* inline comment */\n"
            "}\n",
            encoding='utf-8'
        )
        op_mgr.file_ownership[str(file_path_js.resolve())] = "test_agent"
        
        # Old content has no comments
        old_content_js = (
            "function add(a, b) {\n"
            "    return a + b;\n"
            "}\n"
        )
        new_content_js = (
            "function add(a, b) {\n"
            "    return a + b + 10;\n"
            "}\n"
        )
        res = op_mgr.edit_file(
            path="script.js",
            agent_name="test_agent",
            old_content=old_content_js,
            new_content=new_content_js,
            match_mode="heuristic"
        )
        assert "APPROVED" in res
        assert file_path_js.read_text(encoding='utf-8') == new_content_js

        # Test Case C: HTML comment stripping
        file_path_html = Path(tmpdir) / "index.html"
        file_path_html.write_text(
            "<div>\n"
            "  <!-- HTML comment -->\n"
            "  <h1>Title</h1>\n"
            "</div>\n",
            encoding='utf-8'
        )
        op_mgr.file_ownership[str(file_path_html.resolve())] = "test_agent"
        
        old_content_html = (
            "<div>\n"
            "  <h1>Title</h1>\n"
            "</div>\n"
        )
        new_content_html = (
            "<div>\n"
            "  <h2>New Title</h2>\n"
            "</div>\n"
        )
        res = op_mgr.edit_file(
            path="index.html",
            agent_name="test_agent",
            old_content=old_content_html,
            new_content=new_content_html,
            match_mode="heuristic"
        )
        assert "APPROVED" in res
        assert file_path_html.read_text(encoding='utf-8') == new_content_html

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
        assert "APPROVED" in res
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
        assert "APPROVED" in res
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
        assert "APPROVED" in res
        assert file_path_tabs.read_text(encoding='utf-8') == "class Foo:\n\tdef bar(self):\n\t\tx = 2\n"

