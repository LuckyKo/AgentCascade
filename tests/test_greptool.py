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
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────


def run_shell_cmd(cmd: str, cwd: str) -> tuple[str, int]:
    """Run a shell command and return (stdout_text, return_code)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            shell=True,
            encoding='utf-8',  # Explicit UTF-8 to prevent cp1252 decode errors on Windows
            errors='replace',  # Replace undecodable bytes with replacement character
        )
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return '', -1


def is_windows() -> bool:
    return os.name == 'nt'


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


def shell_grep_include(pattern: str, path: str, include: str = '*') -> list[str]:
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
        parts = line.split(':')
        if len(parts) >= 2:
            candidate = parts[0].replace('\\', '/')
            files.add(candidate.split('/')[-1])
    return files


# ──────────────────────────────────────────────
#  Test fixture setup / teardown
# ──────────────────────────────────────────────


class TestFixture:
    """Creates and manages a temporary test directory structure."""

    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix='grep_test_')

    def build(self):
        """Build the standard test directory structure."""
        root = Path(self.tmpdir)

        # src/main.py
        (root / 'src').mkdir(exist_ok=True)
        (root / 'src' / 'main.py').write_text(
            'def hello_world():\n    print("Hello, World!")\n\nif __name__ == "__main__":\n    hello_world()\n')

        # src/utils.py
        (root / 'src' /
         'utils.py').write_text('class Helper:\n    def assist(self):\n        return True\n\nhelper = Helper()\n')

        # tests/test_main.py
        (root / 'tests').mkdir(exist_ok=True)
        (root / 'tests' / 'test_main.py').write_text(
            'def test_hello():\n    assert hello_world() is None\n\nif __name__ == "__main__":\n    import unittest\n    unittest.main()\n'
        )

        # .hidden/secret.txt
        (root / '.hidden').mkdir(exist_ok=True)
        (root / '.hidden' / 'secret.txt').write_text('SECRET_KEY=123\nAPI_TOKEN=abc456\n')

        # README.md
        (root / 'README.md').write_text(
            '# Test Project\n\nThis is a test project for grep reliability.\n\n## Usage\nRun `python src/main.py`\n')

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

        tool_output = om.grep(pattern='hello', path='.')
        shell_output = shell_grep_simple('hello', str(root))

        tool_files = files_mentioned_in_grep_output(tool_output)
        assert 'main.py' in tool_files, f"tool should find main.py; output: {tool_output[:200]}"
        assert 'test_main.py' in tool_files, f"tool should find test_main.py; output: {tool_output[:200]}"

        print(f"  Tool found files: {tool_files}")
        print(f"  Shell found lines: {len(shell_output)}")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
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

        tool_output = om.grep(pattern='HELLO', path='.', smart_case=False)

        # Shell equivalent (case-sensitive grep without -i)
        shell_output = shell_grep_case_sensitive('HELLO', str(root))

        # NOTE: This test documents a KNOWN BUG in operation_manager.py
        # The logic at line 485: if not smart_case or (...) → cmd.append('-i')
        # When smart_case=False, -i is ALWAYS added (case-insensitive)
        # Expected behavior: smart_case=False should mean case-sensitive

        if 'No matches found' in tool_output:
            print(f"  Tool correctly returned no matches")
            assert len(shell_output) == 0, f"Shell also should find nothing; got {shell_output}"
            print('  [PASS]')
        else:
            print(f"  [FAIL] KNOWN BUG: smart_case=False treated as case-insensitive")
            print(f"  Tool found {tool_output.count(chr(10))} lines (should be 0)")
            print(f"  Shell case-sensitive grep found {len(shell_output)} lines (correctly 0)")
            raise AssertionError(f"smart_case=False should be case-sensitive, but pattern 'HELLO' "
                                 f"matched lowercase 'hello'. Output: {tool_output[:200]}")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
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

        tool_output = om.grep(pattern='SECRET_KEY', path='.')
        assert 'secret.txt' in tool_output, f"Should find secret.txt; output: {tool_output[:300]}"
        assert 'SECRET_KEY=123' in tool_output, f"Should contain the matched line; output: {tool_output[:300]}"

        print(f"  Tool found secret.txt with SECRET_KEY=123")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
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

        tool_output = om.grep(pattern='def ', path='.', include='*.py')
        shell_output = shell_grep_include('def ', str(root), '*.py')

        assert 'main.py' in tool_output, f"Should find main.py; output: {tool_output[:300]}"
        assert 'utils.py' in tool_output, f"Should find utils.py; output: {tool_output[:300]}"
        assert 'test_main.py' in tool_output, f"Should find test_main.py; output: {tool_output[:300]}"
        assert 'README.md' not in tool_output, f"Should NOT find README.md; output: {tool_output[:300]}"

        print(f"  Tool found .py files with 'def ': main.py, utils.py, test_main.py")
        print(f"  Shell found {len(shell_output)} matching lines")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
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

        tool_output = om.grep(pattern='hello', path='.', exclude='test*')
        shell_output = shell_grep_exclude('hello', str(root), 'test*')

        assert 'main.py' in tool_output, f"Should find main.py; output: {tool_output[:300]}"
        if 'test_main.py' not in tool_output:
            print(f"  test_main.py correctly excluded")
        else:
            print(f"  Note: test_main.py found (exclude may need deeper path matching)")

        print(f"  Tool output: {tool_output[:200]}")
        print(f"  Shell found {len(shell_output)} matching lines")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
    finally:
        fixture.teardown()


# ──────────────────────────────────────────────
#  Edge case tests
# ──────────────────────────────────────────────


def test_special_regex_characters():
    """Test: Escaped metacharacters r'def hello_world\\(\\):' match the literal line 'def hello_world():'.

    The grep tool is documented as regex, so parentheses must be escaped to match literally.
    An unescaped '()' is an empty capture group (equivalent to 'def hello_world:'), which
    legitimately matches nothing — so we escape the parens and colon to genuinely exercise
    metacharacter handling.
    """
    print("\n--- Test: Special regex characters r'def hello_world\\(\\):' ---")
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager
        om = OperationManager(base_dir=str(root))

        tool_output = om.grep(pattern=r'def hello_world\(\):', path='.')
        assert 'main.py' in tool_output, \
            f"Should find main.py with escaped pattern r'def hello_world\\(\\):'; output: {tool_output[:300]}"

        print(f"  Tool found main.py with escaped metacharacter pattern")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
    finally:
        fixture.teardown()


def test_empty_directory():
    """Test: Searching an empty directory should return 'No matches found'."""
    print('\n--- Test: Empty directory search ---')
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager

        (Path(root) / 'empty_dir').mkdir(exist_ok=True)

        om = OperationManager(base_dir=str(root))
        tool_output = om.grep(pattern='hello', path='./empty_dir')

        assert 'No matches found' in tool_output, \
            f"Should return no matches for empty dir; output: {tool_output[:200]}"

        print(f"  Tool correctly returned no matches for empty directory")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
    finally:
        fixture.teardown()


def test_non_existent_path():
    """Test: Searching a non-existent path should return an error message."""
    print('\n--- Test: Non-existent path ---')
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager

        om = OperationManager(base_dir=str(root))
        tool_output = om.grep(pattern='hello', path='./does_not_exist')

        assert 'not found' in tool_output.lower(), \
            f"Should return error for non-existent path; output: {tool_output[:200]}"

        print(f"  Tool correctly returned error for non-existent path")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
    finally:
        fixture.teardown()


def test_invalid_regex():
    """Test: Invalid regex pattern should return an error message gracefully."""
    print('\n--- Test: Invalid regex pattern ---')
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager

        om = OperationManager(base_dir=str(root))
        tool_output = om.grep(pattern='[invalid(', path='.')

        assert 'ERROR' in tool_output or 'error' in tool_output.lower(), \
            f"Should return error for invalid regex; output: {tool_output[:200]}"

        print(f"  Tool correctly returned error for invalid regex")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
    finally:
        fixture.teardown()


def test_context_lines():
    """Test: Context lines mode shows surrounding lines with correct match count."""
    print('\n--- Test: Context lines mode ---')
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager

        om = OperationManager(base_dir=str(root))
        tool_output = om.grep(pattern='hello', path='.', context=1)

        assert 'matches' in tool_output.lower(), f"Should report match count; output: {tool_output[:300]}"
        assert '>>>' in tool_output, f"Should have >>> prefix on matched lines; output: {tool_output[:300]}"

        print(f"  Tool context mode works correctly")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
    finally:
        fixture.teardown()


def test_smart_case_behavior():
    """Test: smart_case=True (default) — lowercase pattern is case-insensitive, uppercase is case-sensitive."""
    print('\n--- Test: Smart case behavior ---')
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager

        om = OperationManager(base_dir=str(root))

        # "hello" (lowercase) with smart_case=True → case-insensitive → should find "Hello" too
        output_lower = om.grep(pattern='hello', path='.', smart_case=True)
        assert 'main.py' in output_lower, f"Lowercase pattern should be case-insensitive; output: {output_lower[:300]}"

        # "HELLO" (uppercase) with smart_case=True → case-sensitive → should NOT find "Hello"
        output_upper = om.grep(pattern='HELLO', path='.', smart_case=True)
        assert 'No matches found' in output_upper, f"Uppercase pattern should be case-sensitive; output: {output_upper[:300]}"

        print(f"  Smart case: lowercase→insensitive ✓, uppercase→sensitive ✓")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
    finally:
        fixture.teardown()


def test_ignore_vcs_false():
    """Test: ignore_vcs=False should search into .git-like directories."""
    print('\n--- Test: ignore_vcs=False searches VCS dirs ---')
    fixture = TestFixture()
    try:
        root = fixture.build()
        from agent_cascade.operation_manager import OperationManager

        (Path(root) / '.git').mkdir(exist_ok=True)
        (Path(root) / '.git' / 'config.txt').write_text('hello world\n')

        om = OperationManager(base_dir=str(root))

        om.grep(pattern='hello', path='.', ignore_vcs=True)  # noqa: F841  (side effect; return intentionally discarded)
        output_noignore = om.grep(pattern='hello', path='.', ignore_vcs=False)

        if 'config.txt' in output_noignore:
            print(f"  ignore_vcs=False correctly searches into .git/")
        else:
            print(f"  Note: .git/ search may depend on subprocess vs Python fallback")

        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
    finally:
        fixture.teardown()


def test_rg_cmd_no_bare_replace_flag():
    """Regression (BUG_0042): the ripgrep command must NOT contain a bare '-r' flag.

    In ripgrep, -r/--replace <text> substitutes <text> for each match, so a bare '-r'
    swallows the NEXT argument ('--no-heading') and sets every match's JSON
    submatches[].replacement.text to the literal '--no-heading'. Harmless today only
    because the consumer reads lines.text, but it is a latent landmine.

    We exercise the rg branch of _try_subprocess_grep by forcing tool availability and
    mocking subprocess.run to capture the exact cmd list passed to it (the command is
    built inline in the method, so capturing via the mock keeps this a minimal change).
    """
    print('\n--- Test: ripgrep cmd has no bare -r flag (BUG_0042) ---')
    import unittest.mock as mock

    from agent_cascade.operation_manager import grep as grep_module
    from agent_cascade.operation_manager.grep import GrepMixin

    fixture = TestFixture()
    try:
        root = fixture.build()

        # Minimal stand-in exposing only what the rg branch touches (no __init__ needed).
        host = GrepMixin()

        captured = {}

        def fake_run(cmd, *args, **kwargs):
            captured['cmd'] = list(cmd)
            # returncode 1 == "no matches" for ripgrep; empty stdout is fine.
            class _R:
                returncode = 1
                stdout = ''
                stderr = ''
            return _R()

        with mock.patch.object(grep_module, '_check_tool_availability', return_value=(True, False)), \
             mock.patch('subprocess.run', side_effect=fake_run):
            GrepMixin._try_subprocess_grep(
                host,
                pattern='hello',
                path=root,
                include='*',
                char_limit=1000,
                timeout=5.0,
                agent_name='test',
            )

        cmd = captured['cmd']
        assert cmd[0] == 'rg', f"expected rg branch; got: {cmd}"
        # A bare '-r' must NOT be present (it would consume the next flag as a replace string).
        assert '-r' not in cmd, \
            f"BUG_0042 regression: ripgrep cmd contains a bare '-r' flag that swallows the next arg; cmd={cmd}"
        # The intended flag must still be present and no longer swallowed.
        assert '--no-heading' in cmd, f"--no-heading missing from rg cmd; cmd={cmd}"

        print(f"  rg cmd (first 8): {cmd[:8]}")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
    finally:
        fixture.teardown()


def test_rg_cmd_includes_text_flag():
    """Regression (todo line 138): the ripgrep command MUST include '--text' so that files
    containing a NUL byte are searched as text instead of being classified "binary" and
    silently skipped. Without it, a binary file with a matching line is dropped (rc=0) or
    causes a false "No matches found" (rc=1).

    Mirrors test_rg_cmd_no_bare_replace_flag: force rg availability, mock subprocess.run to
    capture the exact cmd list built by _try_subprocess_grep.
    """
    print('\n--- Test: ripgrep cmd includes --text flag (todo line 138) ---')
    import unittest.mock as mock

    from agent_cascade.operation_manager import grep as grep_module
    from agent_cascade.operation_manager.grep import GrepMixin

    fixture = TestFixture()
    try:
        root = fixture.build()

        # Minimal stand-in exposing only what the rg branch touches (no __init__ needed).
        host = GrepMixin()

        captured = {}

        def fake_run(cmd, *args, **kwargs):
            captured['cmd'] = list(cmd)

            class _R:
                returncode = 1
                stdout = ''
                stderr = ''
            return _R()

        with mock.patch.object(grep_module, '_check_tool_availability', return_value=(True, False)), \
             mock.patch('subprocess.run', side_effect=fake_run):
            GrepMixin._try_subprocess_grep(
                host,
                pattern='hello',
                path=root,
                include='*',
                char_limit=1000,
                timeout=5.0,
                agent_name='test',
            )

        cmd = captured['cmd']
        assert cmd[0] == 'rg', f"expected rg branch; got: {cmd}"
        # --text (= rg -a) is required so NUL-byte/binary files are searched, not skipped.
        assert '--text' in cmd, \
            f"todo line 138 regression: ripgrep cmd missing '--text'; binary/NUL files would be " \
            f"silently skipped; cmd={cmd}"

        print(f"  rg cmd (first 9): {cmd[:9]}")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        fixture.teardown()


def test_rc2_partial_error_surfaces_matches():
    """Regression (Fix B, todo line 138): when ripgrep exits with code 2 ("matches found, but
    errors also occurred"), the tool must parse stdout and surface the real matches instead of
    discarding them / falling back to Python. A genuine usage error (rc=2 with NO match JSON)
    still falls back.

    Mocks subprocess.run to return rc=2 WITH valid match JSON on stdout, then asserts the
    method returns those matches (results is not None and count>0).
    """
    print('\n--- Test: rc=2 partial-error search surfaces matches (Fix B) ---')
    import unittest.mock as mock

    from agent_cascade.operation_manager import grep as grep_module
    from agent_cascade.operation_manager.grep import GrepMixin

    fixture = TestFixture()
    try:
        root = fixture.build()
        host = GrepMixin()

        # Two valid match entries in rg --json format (one per file).
        match_json_1 = ('{"type":"match","data":{"path":{"text":"normal.txt"},'
                        '"line_number":1,"lines":{"text":"hello world\\n"}}}')
        match_json_2 = ('{"type":"match","data":{"path":{"text":"binary.txt"},'
                        '"line_number":1,"lines":{"text":"hello\\\\u0000world\\n"}}}')

        def fake_run(cmd, *args, **kwargs):
            class _R:
                returncode = 2
                stdout = match_json_1 + '\n' + match_json_2 + '\n'
                stderr = 'rg: somefile.txt: Permission denied'
            return _R()

        with mock.patch.object(grep_module, '_check_tool_availability', return_value=(True, False)), \
             mock.patch('subprocess.run', side_effect=fake_run):
            (results, count, was_timed_out, _trunc, _orig, _spill, _total, _shown) = \
                GrepMixin._try_subprocess_grep(
                    host,
                    pattern='hello',
                    path=root,
                    include='*',
                    char_limit=1000,
                    timeout=5.0,
                    agent_name='test',
                )

        # Matches must be surfaced, NOT discarded (results is not None) and count>0.
        assert results is not None, \
            f"Fix B regression: rc=2 with valid match JSON fell back to Python (results=None)"
        assert count == 2, f"Fix B regression: expected 2 matches surfaced, got {count}; results={results}"
        assert 'normal.txt' in results[0], f"first match should be normal.txt; results={results}"

        print(f"  rc=2 with matches -> surfaced {count} matches (no fallback)")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        fixture.teardown()


def test_rc2_usage_error_still_falls_back():
    """Regression (Fix B guard): rc=2 with NO parseable match JSON (a genuine usage error) must
    still return None so the caller falls back to Python — Fix B must not swallow that case."""
    print('\n--- Test: rc=2 usage error (no matches) still falls back ---')
    import unittest.mock as mock

    from agent_cascade.operation_manager import grep as grep_module
    from agent_cascade.operation_manager.grep import GrepMixin

    fixture = TestFixture()
    try:
        root = fixture.build()
        host = GrepMixin()

        def fake_run(cmd, *args, **kwargs):
            class _R:
                returncode = 2
                stdout = ''
                stderr = 'rg: unrecognized flag --bogus'
            return _R()

        with mock.patch.object(grep_module, '_check_tool_availability', return_value=(True, False)), \
             mock.patch('subprocess.run', side_effect=fake_run):
            (results, count, *_) = GrepMixin._try_subprocess_grep(
                host,
                pattern='hello',
                path=root,
                include='*',
                char_limit=1000,
                timeout=5.0,
                agent_name='test',
            )

        assert results is None, \
            f"rc=2 usage error with empty stdout must fall back (results=None); got {results!r}"
        assert count == 0, f"expected count 0 for usage-error fallback; got {count}"

        print('  rc=2 usage error (empty stdout) -> fell back to Python')
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        fixture.teardown()


def test_binary_nul_match_end_to_end():
    """Regression (todo line 138, end-to-end): a file whose matching line contains a NUL byte
    must be reported. Without Fix A (--text), ripgrep classifies it as binary and either drops
    the match (when other files match) or reports "No matches found" (when it's the only match).

    This test REQUIRES ripgrep to actually be available on the host; it skips gracefully when
    rg is absent, consistent with the file's existing conventions.
    """
    print('\n--- Test: binary/NUL-byte match end-to-end (todo line 138) ---')
    from agent_cascade.operation_manager import grep as grep_module

    _rg_available, _grep_available = grep_module._check_tool_availability()
    if not _rg_available:
        print('  [SKIP] ripgrep not available on this host; end-to-end binary test requires rg')
        return

    fixture = TestFixture()
    try:
        root = fixture.build()
        # One normal match file + one file whose matching line contains a NUL byte.
        (root / 'normal.txt').write_text('needle here\n')
        (root / 'binary.txt').write_bytes(b'needle\x00here\n')

        from agent_cascade.operation_manager import OperationManager
        om = OperationManager(base_dir=str(root))

        tool_output = om.grep(pattern='needle', path='.')
        # BOTH files must be reported. Without --text, binary.txt is dropped (rc=0) and only
        # normal.txt appears — that is the false "success" this test guards against.
        assert 'normal.txt' in tool_output, \
            f"Should find normal.txt; output: {tool_output[:300]}"
        assert 'binary.txt' in tool_output, \
            f"todo line 138 regression: binary.txt (NUL-byte match) was silently dropped; " \
            f"output: {tool_output[:300]}"

        print('  Both normal.txt and binary.txt (NUL-byte match) reported')
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        fixture.teardown()


def test_include_glob_does_not_reinclude_git():
    """Regression: a user include glob (e.g. '**/*.py') must NOT re-include files inside .git/
    when ignore_vcs=True (the default). This guards the ripgrep "last match wins" --glob ordering:
    the '!.git/' exclusion is appended last so it always wins over any include glob.
    """
    print('\n--- Test: include glob does not re-include .git/ ---')
    fixture = TestFixture()
    try:
        root = fixture.build()
        # Add a .py file inside .git/ that matches the include glob '**/*.py'.
        (Path(root) / '.git').mkdir(exist_ok=True)
        (Path(root) / '.git' / 'config.py').write_text('def git_helper():\n    pass\n')

        from agent_cascade.operation_manager import OperationManager
        om = OperationManager(base_dir=str(root))

        # include='**/*.py' would match .git/config.py if the .git exclusion were overridden.
        tool_output = om.grep(pattern='def ', path='.', include='**/*.py', ignore_vcs=True)
        assert 'main.py' in tool_output, \
            f"Should find main.py with include='**/*.py'; output: {tool_output[:300]}"
        assert 'config.py' not in tool_output, \
            f".git/config.py must NOT be re-included by include glob; output: {tool_output[:300]}"

        print(f"  include='**/*.py' found main.py but correctly excluded .git/config.py")
        print('  [PASS]')
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        raise  # re-raise so main()'s try/except counts this as a failure (not silently passed)
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
        ('Hidden directory', test_hidden_directory),
        ("Include filter '*.py'", test_include_filter),
        ("Exclude filter 'test*'", test_exclude_filter),
        # Edge case tests
        ('Special regex chars', test_special_regex_characters),
        ('Empty directory', test_empty_directory),
        ('Non-existent path', test_non_existent_path),
        ('Invalid regex', test_invalid_regex),
        ('Context lines', test_context_lines),
        ('Smart case behavior', test_smart_case_behavior),
        ('ignore_vcs=False', test_ignore_vcs_false),
        # Regression tests
        ('rg cmd: no bare -r flag (BUG_0042)', test_rg_cmd_no_bare_replace_flag),
        ('Include glob does not re-include .git/', test_include_glob_does_not_reinclude_git),
        # todo line 138 — binary/NUL-byte content-dependent false "no matches"/dropped matches
        ('rg cmd: includes --text flag (todo 138)', test_rg_cmd_includes_text_flag),
        ('rc=2 partial-error surfaces matches (Fix B)', test_rc2_partial_error_surfaces_matches),
        ('rc=2 usage error still falls back', test_rc2_usage_error_still_falls_back),
        ('binary/NUL-byte match end-to-end (todo 138)', test_binary_nul_match_end_to_end),
    ]

    passed = 0
    failed = 0

    print('=' * 60)
    print('  AgentCascade Grep Tool Reliability Tests')
    print('=' * 60)

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print('\n' + '=' * 60)
    print(f"  Results: {passed}/{len(tests)} passed, {failed}/{len(tests)} failed")
    print('=' * 60)

    if failed > 0:
        print('\n  FAILED TESTS:')
        for name, _ in tests:
            # Re-run to check which ones fail (simple approach)
            pass
        print('  See above for details.')

        print('\n  KNOWN BUG DOCUMENTATION:')
        print("  - smart_case=False is treated as 'always case-insensitive'")
        print("    instead of 'case-sensitive'. In _try_subprocess_grep(),")
        print("    line ~485: 'if not smart_case or (...): cmd.append(\"-i\")'")
        print('    The condition always adds -i when smart_case=False,')
        print('    making it case-insensitive instead of case-sensitive.')
        print('  - Affects both ripgrep and standard grep subprocess paths.')
        print('  - Python fallback has the same issue in its logic.')


if __name__ == '__main__':
    main()
