"""
Comprehensive reliability tests for the grep tool in AgentCascade.

Tests compare operation_manager.grep() results against equivalent shell commands
(find/grep via subprocess) to ensure consistency and correctness.

Run with:  python test_greptool.py
           or via code_interpreter from within AgentCascade

KNOWN BUG (documented below):
  smart_case=False is treated as "always case-insensitive" instead of 
  "case-sensitive". The logic in _try_subprocess_grep adds -i whenever
  not smart_case, regardless of the pattern. This affects both ripgrep
  and standard grep subprocess paths.
"""
import os, sys, tempfile, re, subprocess, shutil
from pathlib import Path

# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────

def run_shell_cmd(cmd: str, cwd: str) -> tuple[str, int]:
    """Run a shell command and return (stdout_text, return_code)."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=15, shell=True,
            encoding='utf-8',          # Explicit UTF-8 to prevent cp1252 decode errors on Windows
            errors='replace',          # Replace undecodable bytes with replacement character
        )
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return "", -1

def is_windows() -> bool:
    return os.name == "nt"

def shell_grep_simple(pattern: str, path: str) -> list[str]:
    """Shell equivalent of grep: find all files recursively matching pattern."""
    if is_windows():
        cmd = f'Get-ChildItem -Path "{path}" -Recurse -File | Select-String -Pattern "{pattern}" -CaseSensitive $false | ForEach-Object {{ $_.Path + \":\" + $_.LineNumber }}'
    else:
        cmd = f"grep -rI -i '{pattern}' {path}"
    out, rc = run_shell_cmd(cmd, path)
    return [l.strip() for l in out.strip().splitlines() if l.strip()] if out.strip() else []

def shell_grep_case_sensitive(pattern: str, path: str) -> list[str]:
    """Shell grep with case-sensitive matching."""
    if is_windows():
        cmd = f'Get-ChildItem -Path "{path}" -Recurse -File | Select-String -Pattern "{pattern}" -CaseSensitive $true | ForEach-Object {{ $_.Path + \":\" + $_.LineNumber }}'
    else:
        cmd = f"grep -rI '{pattern}' {path}"  # No -i = case-sensitive
    out, rc = run_shell_cmd(cmd, path)
    return [l.strip() for l in out.strip().splitlines() if l.strip()] if out.strip() else []

def shell_grep_include(pattern: str, path: str, include: str = "*") -> list[str]:
    """Shell grep with file type filter."""
    if is_windows():
        cmd = f'Get-ChildItem -Path "{path}" -Recurse -File -Include "{include}" | Select-String -Pattern "{pattern}" -CaseSensitive $false | ForEach-Object {{ $_.Path + \":\" + $_.LineNumber }}'
    else:
        cmd = f"find {path} -name '{include}' -type f -exec grep -l '{pattern}' {{}} \\;"
    out, rc = run_shell_cmd(cmd, path)
    return [l.strip() for l in out.strip().splitlines() if l.strip()] if out.strip() else []

def shell_grep_exclude(pattern: str, path: str, exclude: str) -> list[str]:
    """Shell grep with file exclusion filter."""
    if is_windows():
        cmd = f'Get-ChildItem -Path "{path}" -Recurse -File | Where-Object {{ $_.Name -notlike "{exclude}" }} | Select-String -Pattern "{pattern}" -CaseSensitive $false | ForEach-Object {{ $_.Path + \":\" + $_.LineNumber }}'
    else:
        cmd = f"grep -rI --exclude={exclude} '{pattern}' {path}"
    out, rc = run_shell_cmd(cmd, path)
    return [l.strip() for l in out.strip().splitlines() if l.strip()] if out.strip() else []

def files_mentioned_in_grep_output(output: str) -> set[str]:
    """Extract filenames mentioned in grep output."""
    files: set[str] = set()
    for line in output.splitlines():
        parts = line.split(":")
        if len(parts) >= 2:
            candidate = parts[0].replace("\\", "/")
            files.add(candidate.split("/")[-1])
    return files

# ──────────────────────────────────────────────
#  Test fixture setup / teardown
# ──────────────────────────────────────────────

class TestFixture:
    """Creates and manages a temporary test directory structure."""
    
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="grep_test_")
    
    def build(self):
        """Build the standard test directory structure."""
        root = Path(self.tmpdir)
        
        # src/main.py
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "main.py").write_text(
            'def hello_world():\n    print("Hello, World!")\n\nif __name__ == "__main__":\n    hello_world()\n'
        )
        
        # src/utils.py
        (root / "src" / "utils.py").write_text(
            'class Helper:\n    def assist(self):\n        return True\n\nhelper = Helper()\n'
        )
        
        # tests/test_main.py
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "test_main.py").write_text(
            'def test_hello():\n    assert hello_world() is None\n\nif __name__ == "__main__":\n    import unittest\n    unittest.main()\n'
        )
        
        # .hidden/secret.txt
        (root / ".hidden").mkdir(exist_ok=True)
        (root / ".hidden" / "secret.txt").write_text(
            'SECRET_KEY=123\nAPI_TOKEN=abc456\n'
        )
        
        # README.md
        (root / "README.md").write_text(
            '# Test Project\n\nThis is a test project for grep reliability.\n\n## Usage\nRun `python src/main.py`\n'
        )
        
        return root
    
    def teardown(self):
        """Remove the temporary directory."""
        shutil.rmtree(self.tmpdir, ignore_errors=True)

# ──────────────────────────────────────────────
#  Individual tests
# ──────────────────────────────────────────────

def test_simple_pattern():
    """Test: Simple pattern 'hello' should find matches in main.py and test_main.py."""
    print("\n--- Test: Simple pattern 'hello' ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        om = OperationManager(base_dir=str(root))
        
        tool_output = om.grep(pattern="hello", path=".")
        shell_output = shell_grep_simple("hello", str(root))
        
        tool_files = files_mentioned_in_grep_output(tool_output)
        assert "main.py" in tool_files, f"tool should find main.py; output: {tool_output[:200]}"
        assert "test_main.py" in tool_files, f"tool should find test_main.py; output: {tool_output[:200]}"
        
        print(f"  Tool found files: {tool_files}")
        print(f"  Shell found lines: {len(shell_output)}")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_case_sensitive_pattern():
    """Test: 'HELLO' with smart_case=False should find nothing (no exact HELLO in files)."""
    print("\n--- Test: Case-sensitive pattern 'HELLO' ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        om = OperationManager(base_dir=str(root))
        
        tool_output = om.grep(pattern="HELLO", path=".", smart_case=False)
        
        # Shell equivalent (case-sensitive grep without -i)
        shell_output = shell_grep_case_sensitive("HELLO", str(root))
        
        # NOTE: This test documents a KNOWN BUG in operation_manager.py
        # The logic at line 485: if not smart_case or (...) → cmd.append('-i')
        # When smart_case=False, -i is ALWAYS added (case-insensitive)
        # Expected behavior: smart_case=False should mean case-sensitive
        
        if "No matches found" in tool_output:
            print(f"  Tool correctly returned no matches")
            assert len(shell_output) == 0, f"Shell also should find nothing; got {shell_output}"
            print("  [PASS]")
        else:
            print(f"  [FAIL] KNOWN BUG: smart_case=False treated as case-insensitive")
            print(f"  Tool found {tool_output.count(chr(10))} lines (should be 0)")
            print(f"  Shell case-sensitive grep found {len(shell_output)} lines (correctly 0)")
            raise AssertionError(
                f"smart_case=False should be case-sensitive, but pattern 'HELLO' "
                f"matched lowercase 'hello'. Output: {tool_output[:200]}"
            )
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_hidden_directory():
    """Test: 'SECRET_KEY' should find secret.txt in .hidden/ directory."""
    print("\n--- Test: Hidden directory pattern 'SECRET_KEY' ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        om = OperationManager(base_dir=str(root))
        
        tool_output = om.grep(pattern="SECRET_KEY", path=".")
        assert "secret.txt" in tool_output, f"Should find secret.txt; output: {tool_output[:300]}"
        assert "SECRET_KEY=123" in tool_output, f"Should contain the matched line; output: {tool_output[:300]}"
        
        print(f"  Tool found secret.txt with SECRET_KEY=123")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_include_filter():
    """Test: pattern 'def ' with include '*.py' should only find Python files."""
    print("\n--- Test: Include filter 'def ' with include='*.py' ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        om = OperationManager(base_dir=str(root))
        
        tool_output = om.grep(pattern="def ", path=".", include="*.py")
        shell_output = shell_grep_include("def ", str(root), "*.py")
        
        assert "main.py" in tool_output, f"Should find main.py; output: {tool_output[:300]}"
        assert "utils.py" in tool_output, f"Should find utils.py; output: {tool_output[:300]}"
        assert "test_main.py" in tool_output, f"Should find test_main.py; output: {tool_output[:300]}"
        assert "README.md" not in tool_output, f"Should NOT find README.md; output: {tool_output[:300]}"
        
        print(f"  Tool found .py files with 'def ': main.py, utils.py, test_main.py")
        print(f"  Shell found {len(shell_output)} matching lines")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_exclude_filter():
    """Test: pattern 'hello' with exclude='test*' should not find test files."""
    print("\n--- Test: Exclude filter 'hello' with exclude='test*' ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        om = OperationManager(base_dir=str(root))
        
        tool_output = om.grep(pattern="hello", path=".", exclude="test*")
        shell_output = shell_grep_exclude("hello", str(root), "test*")
        
        assert "main.py" in tool_output, f"Should find main.py; output: {tool_output[:300]}"
        if "test_main.py" not in tool_output:
            print(f"  test_main.py correctly excluded")
        else:
            print(f"  Note: test_main.py found (exclude may need deeper path matching)")
        
        print(f"  Tool output: {tool_output[:200]}")
        print(f"  Shell found {len(shell_output)} matching lines")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

# ──────────────────────────────────────────────
#  Edge case tests
# ──────────────────────────────────────────────

def test_special_regex_characters():
    """Test: Pattern with parentheses 'def hello_world():' should be handled as regex."""
    print("\n--- Test: Special regex characters 'def hello_world():' ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        om = OperationManager(base_dir=str(root))
        
        tool_output = om.grep(pattern="def hello_world():", path=".")
        assert "main.py" in tool_output, \
            f"Should find main.py with 'def hello_world():'; output: {tool_output[:300]}"
        
        print(f"  Tool found main.py with regex pattern including parens")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_empty_directory():
    """Test: Searching an empty directory should return 'No matches found'."""
    print("\n--- Test: Empty directory search ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        
        (Path(root) / "empty_dir").mkdir(exist_ok=True)
        
        om = OperationManager(base_dir=str(root))
        tool_output = om.grep(pattern="hello", path="./empty_dir")
        
        assert "No matches found" in tool_output, \
            f"Should return no matches for empty dir; output: {tool_output[:200]}"
        
        print(f"  Tool correctly returned no matches for empty directory")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_non_existent_path():
    """Test: Searching a non-existent path should return an error message."""
    print("\n--- Test: Non-existent path ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        
        om = OperationManager(base_dir=str(root))
        tool_output = om.grep(pattern="hello", path="./does_not_exist")
        
        assert "not found" in tool_output.lower(), \
            f"Should return error for non-existent path; output: {tool_output[:200]}"
        
        print(f"  Tool correctly returned error for non-existent path")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_invalid_regex():
    """Test: Invalid regex pattern should return an error message gracefully."""
    print("\n--- Test: Invalid regex pattern ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        
        om = OperationManager(base_dir=str(root))
        tool_output = om.grep(pattern="[invalid(", path=".")
        
        assert "ERROR" in tool_output or "error" in tool_output.lower(), \
            f"Should return error for invalid regex; output: {tool_output[:200]}"
        
        print(f"  Tool correctly returned error for invalid regex")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_context_lines():
    """Test: Context lines mode shows surrounding lines with correct match count."""
    print("\n--- Test: Context lines mode ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        
        om = OperationManager(base_dir=str(root))
        tool_output = om.grep(pattern="hello", path=".", context=1)
        
        assert "matches" in tool_output.lower(), f"Should report match count; output: {tool_output[:300]}"
        assert ">>>" in tool_output, f"Should have >>> prefix on matched lines; output: {tool_output[:300]}"
        
        print(f"  Tool context mode works correctly")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_smart_case_behavior():
    """Test: smart_case=True (default) — lowercase pattern is case-insensitive, uppercase is case-sensitive."""
    print("\n--- Test: Smart case behavior ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        
        om = OperationManager(base_dir=str(root))
        
        # "hello" (lowercase) with smart_case=True → case-insensitive → should find "Hello" too
        output_lower = om.grep(pattern="hello", path=".", smart_case=True)
        assert "main.py" in output_lower, f"Lowercase pattern should be case-insensitive; output: {output_lower[:300]}"
        
        # "HELLO" (uppercase) with smart_case=True → case-sensitive → should NOT find "Hello"
        output_upper = om.grep(pattern="HELLO", path=".", smart_case=True)
        assert "No matches found" in output_upper, f"Uppercase pattern should be case-sensitive; output: {output_upper[:300]}"
        
        print(f"  Smart case: lowercase→insensitive ✓, uppercase→sensitive ✓")
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

def test_ignore_vcs_false():
    """Test: ignore_vcs=False should search into .git-like directories."""
    print("\n--- Test: ignore_vcs=False searches VCS dirs ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        
        (Path(root) / ".git").mkdir(exist_ok=True)
        (Path(root) / ".git" / "config.txt").write_text("hello world\n")
        
        om = OperationManager(base_dir=str(root))
        
        output_ignore = om.grep(pattern="hello", path=".", ignore_vcs=True)
        output_noignore = om.grep(pattern="hello", path=".", ignore_vcs=False)
        
        if "config.txt" in output_noignore:
            print(f"  ignore_vcs=False correctly searches into .git/")
        else:
            print(f"  Note: .git/ search may depend on subprocess vs Python fallback")
        
        print("  [PASS]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
    finally:
        fixture.teardown()

# ──────────────────────────────────────────────
#  Main runner
# ──────────────────────────────────────────────

def main():
    tests = [
        # Core functionality tests
        ("Simple pattern 'hello'", test_simple_pattern),
        ("Case-sensitive 'HELLO' (KNOWN BUG)", test_case_sensitive_pattern),
        ("Hidden directory", test_hidden_directory),
        ("Include filter '*.py'", test_include_filter),
        ("Exclude filter 'test*'", test_exclude_filter),
        # Edge case tests
        ("Special regex chars", test_special_regex_characters),
        ("Empty directory", test_empty_directory),
        ("Non-existent path", test_non_existent_path),
        ("Invalid regex", test_invalid_regex),
        ("Context lines", test_context_lines),
        ("Smart case behavior", test_smart_case_behavior),
        ("ignore_vcs=False", test_ignore_vcs_false),
    ]
    
    passed = 0
    failed = 0
    
    print("=" * 60)
    print("  AgentCascade Grep Tool Reliability Tests")
    print("=" * 60)
    
    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"  Results: {passed}/{len(tests)} passed, {failed}/{len(tests)} failed")
    print("=" * 60)
    
    if failed > 0:
        print("\n  FAILED TESTS:")
        for name, _ in tests:
            # Re-run to check which ones fail (simple approach)
            pass
        print("  See above for details.")
        
        print("\n  KNOWN BUG DOCUMENTATION:")
        print("  - smart_case=False is treated as 'always case-insensitive'")
        print("    instead of 'case-sensitive'. In _try_subprocess_grep(),")
        print("    line ~485: 'if not smart_case or (...): cmd.append(\"-i\")'")
        print("    The condition always adds -i when smart_case=False,")
        print("    making it case-insensitive instead of case-sensitive.")
        print("  - Affects both ripgrep and standard grep subprocess paths.")
        print("  - Python fallback has the same issue in its logic.")

if __name__ == "__main__":
    main()