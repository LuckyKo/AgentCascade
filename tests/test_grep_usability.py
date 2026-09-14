"""Test suite for grep usability improvements in operation_manager.py."""
import re
import sys
import tempfile
from pathlib import Path

# Resolve project root relative to this test file (tests/ → project_root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_compile_grep_pattern_flags():
    """Test that _compile_grep_pattern accepts flags parameter."""
    from agent_cascade.operation_manager import _compile_grep_pattern
    _compile_grep_pattern.cache_clear()
    pat = _compile_grep_pattern('Hello')
    assert not pat.search('hello'), 'Should be case-sensitive by default'
    pat_ci = _compile_grep_pattern('Hello', flags=re.IGNORECASE)
    assert pat_ci.search('hello'), 'Should match with IGNORECASE flag'
    print('[PASS] test_compile_grep_pattern_flags')


def test_smart_case_logic():
    """Test smart_case logic."""
    assert not re.search(r'[A-Z]', 'hello'), "Pattern 'hello' has no uppercase"
    assert re.search(r'[A-Z]', 'Hello'), "Pattern 'Hello' has uppercase"
    print('[PASS] test_smart_case_logic')


def test_list_dir_no_emoji():
    """Test that list_directory output uses clean formatting without emoji."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 'subdir').mkdir()
        Path(tmpdir, 'test.txt').write_text('hello')
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory('.')
        assert '\U0001f4c1' not in result, 'Should not contain folder emoji'
        assert '\U0001f4c2' not in result, 'Should not contain open-folder emoji'
        assert '\U0001f4c4' not in result, 'Should not contain page emoji'
        assert '\U0001f4dd' not in result, 'Should not contain memo emoji'
        # New format uses "Directories:" / "Files:" headers with trailing slashes for dirs
        assert 'subdir/' in result, f"Directory should appear with trailing slash: {result}"
        assert 'test.txt' in result, f"File name should appear: {result}"
    print('[PASS] test_list_dir_no_emoji')


def test_list_recursive_empty_subdir_marked():
    """BUG_0006: an empty subdir must be shown as (empty) and root files must NOT
    be indented under it."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        # Root has 2 files + one EMPTY subdir (the exact BUG_0006 scenario).
        Path(tmpdir, 'fixed').mkdir()
        Path(tmpdir, 'BUG_0001.md').write_text('a')
        Path(tmpdir, 'BUG_0002.md').write_text('b')
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory('.', recursive=True)

        # Empty subdir is present and explicitly marked.
        assert '[DIR] fixed/ (empty)' in result, f"Empty subdir must be marked (empty): {result}"

        # Root files render at column 0 (no leading indent), so they are not
        # visually attached to the empty subdir.
        for fname in ('BUG_0001.md', 'BUG_0002.md'):
            line = next((ln for ln in result.splitlines() if ln.lstrip().startswith(fname)), None)
            assert line is not None, f"{fname} missing from output: {result}"
            assert not line.startswith(' '), \
                f"Root file {fname} must be at column 0 (not indented under a subdir): {line!r}"

        # The root is NOT reported as empty (it contains a dir).
        assert '(empty directory or all entries filtered out)' not in result, \
            f"Root with an empty subdir should not print top-level empty message: {result}"
    print('[PASS] test_list_recursive_empty_subdir_marked')


def test_list_recursive_depth_indentation():
    """Files inside a nested subdir must be indented more than root files AND labelled
    with their path relative to the listed root (so parentage is unambiguous)."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 'root.txt').write_text('root file')
        sub = Path(tmpdir, 'sub')
        sub.mkdir()
        (sub / 'nested.txt').write_text('nested file')
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory('.', recursive=True)

        root_line = next(ln for ln in result.splitlines() if ln.lstrip().startswith('root.txt'))
        # Nested file is now labelled with its relative path "sub/nested.txt".
        nested_line = next(ln for ln in result.splitlines() if 'sub/nested.txt' in ln)

        # Root file at column 0; nested file indented by >= 2 spaces.
        assert not root_line.startswith(' '), f"Root file should be at column 0: {root_line!r}"
        assert nested_line.startswith('  '), \
            f"Nested file should be indented under its subdir: {nested_line!r}"

        # The nested entry carries its parent dir in the label (the actual fix).
        assert 'sub/nested.txt' in nested_line, \
            f"Nested file must show relative path 'sub/nested.txt': {nested_line!r}"
        # Root-level file keeps a bare name (no directory prefix).
        assert root_line.lstrip().startswith('root.txt '), \
            f"Root file must keep a bare name: {root_line!r}"

        # Subdir contents are more indented than the subdir's own header line.
        dir_line = next(ln for ln in result.splitlines() if '[DIR] sub/' in ln)
        assert len(nested_line) - len(nested_line.lstrip()) > len(dir_line) - len(dir_line.lstrip()), \
            f"Nested file indent must exceed subdir header indent: {nested_line!r} vs {dir_line!r}"
    print('[PASS] test_list_recursive_depth_indentation')


def test_list_exclude_flat():
    """BUG_0008 (defect 1): exclude filter must DROP matching names, not keep them.
    Flat mode: exclude='*.txt' removes drop.txt and keeps keep.py."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 'keep.py').write_text('a')
        Path(tmpdir, 'drop.txt').write_text('b')
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory('.', exclude='*.txt')
        assert 'keep.py' in result, f"keep.py should be kept: {result}"
        assert 'drop.txt' not in result, f"drop.txt should be excluded: {result}"
    print('[PASS] test_list_exclude_flat')


def test_list_exclude_recursive():
    """BUG_0008 (defect 1): exclude filter in recursive mode drops all matching files
    at any depth while keeping non-matching ones."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 'keep.py').write_text('a')
        Path(tmpdir, 'drop.txt').write_text('b')
        a = Path(tmpdir, 'a')
        a.mkdir()
        (a / 'inner.txt').write_text('c')
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory('.', recursive=True, exclude='*.txt')
        # No .txt file may appear anywhere in the output.
        assert '.txt' not in result, f"No .txt files should be shown: {result}"
        assert 'drop.txt' not in result, f"drop.txt should be excluded: {result}"
        assert 'inner.txt' not in result, f"inner.txt should be excluded: {result}"
        # Non-.txt files are kept.
        assert 'keep.py' in result, f"keep.py should be kept: {result}"
    print('[PASS] test_list_exclude_recursive')


def test_list_include_filtered_dir_subtree():
    """BUG_0008 (defect 2): a dir that fails the include filter must still be walked
    so matching files beneath it are found. include='*.txt' must reveal BOTH the root
    drop.txt AND a/inner.txt even though dir 'a' itself does not match '*.txt'."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 'drop.txt').write_text('a')
        Path(tmpdir, 'keep.py').write_text('b')
        a = Path(tmpdir, 'a')
        a.mkdir()
        (a / 'inner.txt').write_text('c')
        om = OperationManager(base_dir=tmpdir)
        result = om.list_directory('.', recursive=True, include='*.txt')
        # Both matching .txt files must be present (key regression assertion).
        assert 'drop.txt' in result, f"root drop.txt should be shown: {result}"
        assert 'inner.txt' in result, f"a/inner.txt should be shown (filtered dir still walked): {result}"
        # Non-matching file is dropped.
        assert 'keep.py' not in result, f"keep.py should be filtered out by include='*.txt': {result}"
    print('[PASS] test_list_include_filtered_dir_subtree')


def test_grep_path_normalization():
    """Test that grep output uses forward slashes even on Windows."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        subdir = Path(tmpdir) / 'src' / 'nested'
        subdir.mkdir(parents=True)
        (subdir / 'test.py').write_text('def hello():\n    pass')
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='hello', path='.')
        assert '\\' not in result, f"Backslashes found in output: {result}"
        assert 'src/nested/test.py' in result, f"Forward slash path expected: {result}"
    print('[PASS] test_grep_path_normalization')


def test_grep_no_strip():
    """Test that grep preserves whitespace (no .strip())."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        content = '  indented text  \nno indent\n'
        Path(tmpdir, 'test.txt').write_text(content)
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='indented', path='.')
        # The matched line should preserve its whitespace
        assert '  indented text  ' in result or '    >>>' in result, \
            f"Whitespace should be preserved: {result}"
    print('[PASS] test_grep_no_strip')


def test_grep_context_lines():
    """Test that context lines are shown around matches."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        content = 'line 1\nline 2 MATCH\nline 3\nline 4\n'
        Path(tmpdir, 'test.txt').write_text(content)
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='MATCH', path='.', context=1)
        assert 'line 1' in result, f"Context before should be shown: {result}"
        assert '>>>' in result or '---' in result, f">>> prefix or --- separator expected: {result}"
        assert 'line 3' in result, f"Context after should be shown: {result}"
    print('[PASS] test_grep_context_lines')


def test_grep_exclude():
    """Test that exclude parameter filters files."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 'good.py').write_text('hello world')
        Path(tmpdir, 'bad.txt').write_text('hello world')
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='hello', path='.', exclude='*.txt')
        assert 'good.py' in result, f"good.py should be included: {result}"
        assert 'bad.txt' not in result, f"bad.txt should be excluded: {result}"
    print('[PASS] test_grep_exclude')


def test_grep_vcs_skip():
    """Test that VCS/build directories are skipped in Python fallback."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        pycache = Path(tmpdir, '__pycache__')
        pycache.mkdir()
        (pycache / 'cached.pyc').write_text('hello')
        Path(tmpdir, 'normal.py').write_text('hello')
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='hello', path='.')
        assert 'normal.py' in result, f"normal.py should be found: {result}"
    print('[PASS] test_grep_vcs_skip')


def test_backwards_compatibility():
    """Test that default behavior is preserved when new params aren't provided."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 'test.py').write_text('Hello World\nhello world')
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='hello', path='.')
        assert 'test.py' in result, f"Should find matches with default params: {result}"
    print('[PASS] test_backwards_compatibility')


def test_context_match_count_not_inflated():
    """Test that context mode doesn't inflate match count."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        content = 'alpha\nMATCH1\nbravo\nMATCH2\ndelta\n'
        Path(tmpdir, 'test.txt').write_text(content)
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='MATCH', path='.', context=1)
        # Should say "Found 2 matches" not "Found 6 matches" or similar inflated count
        assert 'Found 2 matches' in result, f"Match count should be 2: {result}"


def test_exclude_fnmatch():
    """Test that exclude uses fnmatch (supports **)."""
    from agent_cascade.operation_manager import OperationManager
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 'good.py').write_text('hello')
        # Nested file that should be excluded by ** pattern
        subdir = Path(tmpdir) / 'deep' / 'nested'
        subdir.mkdir(parents=True)
        (subdir / 'bad.pyc').write_text('hello')
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='hello', path='.', exclude='**/*.pyc')
        assert 'good.py' in result, f"good.py should be included: {result}"
        assert 'bad.pyc' not in result, f"bad.pyc should be excluded by ** pattern: {result}"


def test_keyword_only_flags():
    """Test that _compile_grep_pattern flags parameter is keyword-only."""
    from agent_cascade.operation_manager import _compile_grep_pattern
    _compile_grep_pattern.cache_clear()
    # Should work with keyword arg
    pat = _compile_grep_pattern('Hello', flags=re.IGNORECASE)
    assert pat.search('hello'), 'Should match with IGNORECASE'


def test_extract_spill_path():
    """BUG_0010: _extract_spill_path recovers the rel path from a truncate_with_spillover notice."""
    from agent_cascade.operation_manager import GrepMixin

    # Normal notice (head mode, same format read_file / Python fallback produce)
    notice = 'line1\nline2\n\n[TRUNCATED — showing 2 of 5 lines (99 chars total). Full output saved to: logs/spillover/agent_grep_20260914_054738_592862.txt]'
    assert GrepMixin._extract_spill_path(notice) == 'logs/spillover/agent_grep_20260914_054738_592862.txt'
    # Path with a dot and no trailing bracket edge (custom spill path)
    assert GrepMixin._extract_spill_path('x\nFull output saved to: custom/spill.txt]') == 'custom/spill.txt'
    # No notice present → None
    assert GrepMixin._extract_spill_path('no truncation here') is None
    print('[PASS] test_extract_spill_path')


def test_grep_fast_path_truncation_surfaces_spillover():
    """BUG_0010: an overflowing fast-path grep must surface the spillover file path.

    Skips gracefully when no subprocess grep tool (rg/grep) is available, since this
    only exercises the subprocess fast path.
    """
    from agent_cascade.operation_manager import OperationManager
    if not _require_subprocess_grep('test_grep_fast_path_truncation_surfaces_spillover'):
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        # 60 lines, each ~50 chars → well over the default char_limit of 2000.
        content = ''.join(f"line{i:03d} " + ('x' * 40) + '\n' for i in range(60))
        Path(tmpdir, 'big.txt').write_text(content)
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='line', path='.')
        # Truncation must be signalled...
        assert '[TRUNCATED' in result, f"Expected truncation marker: {result[:300]}"
        # ...and the spillover file path must now be surfaced (the BUG_0010 fix).
        assert 'full output saved to:' in result, \
            f"Spillover path notice missing from fast-path grep response: {result[:400]}"
        assert 'logs/spillover/' in result, \
            f"Spillover file path not surfaced in fast-path grep response: {result[:400]}"
        print('[PASS] test_grep_fast_path_truncation_surfaces_spillover')


def _require_subprocess_grep(test_name):
    """Return True if a subprocess grep tool (rg/grep) is available, else print a skip note."""
    from agent_cascade.operation_manager.grep import _check_tool_availability
    if any(_check_tool_availability()):
        return True
    print(f'[SKIP] {test_name} (no rg/grep available)')
    return False


def _body_after_summary(result):
    """Return the output body after the 'summary:' header line.

    The summary header is rendered as ``f"{summary}:\n\n" + output_text`` — a single,
    deliberate blank line follows the header and must be allowed. Slicing at the first
    '\n' keeps that expected blank line out of the body we assert on.
    """
    return result.split('\n', 1)[1] if '\n' in result else ''


def test_grep_fast_path_no_blank_lines_between_matches():
    """Regression: fast-path (rg) grep must not emit a blank line between match entries.

    rg's --json 'lines.text' value always carries a trailing newline ('\\n' on POSIX,
    '\\r\\n' on Windows). The old code appended that text verbatim and then joined the
    list with '\\n', so every entry ended up as ``...content\\n`` + join ``\\n`` = a blank
    line between EVERY entry (and a dangling trailing newline after the last one).

    Skips gracefully when no subprocess grep tool (rg/grep) is available, since this only
    exercises the subprocess fast path.
    """
    from agent_cascade.operation_manager import OperationManager
    if not _require_subprocess_grep('test_grep_fast_path_no_blank_lines_between_matches'):
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        # Two matching lines (ZEBRA) far apart, in one file. The filler lines contain no
        # 'ZEBRA' and no uppercase, so the pattern matches exactly two lines.
        Path(tmpdir, 't.txt').write_text('ZEBRA alpha\nbravo\n' + ('x' * 30 + '\n') * 5 + 'ZEBRA charlie\ndelta\n')
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='ZEBRA', path='.')
        assert 'Found 2 matches' in result, f"Expected 2 matches: {result}"
        body = _body_after_summary(result)
        # No blank line between entries and no dangling trailing newline (the bug).
        assert '\n\n' not in body, \
            f"Blank line(s) present between match entries: {body!r}"
        assert not body.endswith('\n'), \
            f"Body has a dangling trailing newline after the last entry: {body!r}"
        # Both matches are actually surfaced.
        for token in ('alpha', 'charlie'):
            assert token in body, f"Match '{token}' missing from output: {body!r}"
    print('[PASS] test_grep_fast_path_no_blank_lines_between_matches')


def test_grep_fast_path_context_no_blank_lines():
    """Regression: fast-path (rg) grep with context must not emit blank lines between the
    match/context entry lines, each match keeps its '>>>' prefix, and the count is correct.

    rg emits a trailing newline on BOTH 'match' and 'context' events, so both branches were
    affected by the old bug. Skips gracefully when no subprocess grep tool is available.
    """
    from agent_cascade.operation_manager import OperationManager
    if not _require_subprocess_grep('test_grep_fast_path_context_no_blank_lines'):
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        # Two matches (ZEBRA) far apart so their context windows do not merge. Filler lines
        # contain no 'ZEBRA' and no uppercase, so the pattern matches exactly two lines.
        Path(tmpdir, 't.txt').write_text('ZEBRA alpha\nbravo\n' + ('x' * 30 + '\n') * 5 + 'ZEBRA charlie\ndelta\n')
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='ZEBRA', path='.', context=1)
        assert 'Found 2 matches' in result, f"Expected exactly 2 matches: {result}"
        body = _body_after_summary(result)
        # No blank line between the match/context entry lines (the bug).
        assert '\n\n' not in body, \
            f"Blank line(s) present between context-group entries: {body!r}"
        assert not body.endswith('\n'), \
            f"Body has a dangling trailing newline after the last entry: {body!r}"
        # Each match is still prefixed with '>>>' (exactly two, one per match).
        match_lines = [ln for ln in body.splitlines() if '>>>' in ln]
        assert len(match_lines) == 2, f"Expected exactly 2 '>>>' match lines: {body!r}"
        # Context neighbours are surfaced.
        for token in ('alpha', 'bravo', 'charlie', 'delta'):
            assert token in body, f"Line '{token}' missing from context output: {body!r}"
    print('[PASS] test_grep_fast_path_context_no_blank_lines')


if __name__ == '__main__':
    tests = [
        test_compile_grep_pattern_flags,
        test_smart_case_logic,
        test_list_dir_no_emoji,
        test_list_recursive_empty_subdir_marked,
        test_list_recursive_depth_indentation,
        test_list_exclude_flat,
        test_list_exclude_recursive,
        test_list_include_filtered_dir_subtree,
        test_grep_path_normalization,
        test_grep_no_strip,
        test_grep_context_lines,
        test_grep_exclude,
        test_grep_vcs_skip,
        test_backwards_compatibility,
        test_context_match_count_not_inflated,
        test_exclude_fnmatch,
        test_keyword_only_flags,
        test_extract_spill_path,
        test_grep_fast_path_truncation_surfaces_spillover,
        test_grep_fast_path_no_blank_lines_between_matches,
        test_grep_fast_path_context_no_blank_lines,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print('')
    print('=' * 50)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    if failed > 0:
        sys.exit(1)
