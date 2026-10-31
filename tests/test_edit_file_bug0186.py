#!/usr/bin/env python3
"""BUG_0186 regression tests: edit_file exact match tab/space mismatch
and heuristic trailing-context preservation.

Two sub-issues:
  (a) Exact match fails when old_content uses spaces and the file uses tabs
      (or vice versa). Fix: normalize whitespace before comparing.
  (b) Heuristic match drops trailing lines when the replacement is shorter
      than the matched region. Fix: preserve uncovered trailing lines.

Run: python -m pytest tests/test_edit_file_bug0186.py -v -p no:xdist -o addopts=""
"""

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _make_om(tmpdir):
    from agent_cascade.operation_manager import OperationManager
    return OperationManager(base_dir=str(tmpdir))


def _owned_file(om, tmpdir, name, content, agent='coder'):
    """Write a file in the workspace and mark it owned so edits auto-approve."""
    f = Path(tmpdir, name)
    f.write_text(content, encoding='utf-8')
    om._own(f.resolve(), agent)
    return f


# ── (a) Exact match: tab-indented file, space-indented old_content ─────────


def test_exact_match_tab_space_normalization():
    """A file with tab indentation; old_content uses space indentation.
    The normalized match should succeed and replace the tab-indented content."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        # File uses tabs for indentation
        file_content = 'def foo():\n\treturn 1\n\t\n\t# end\n'
        f = _owned_file(om, d, 'tabs.py', file_content)

        # old_content uses spaces (transcribed from read_file output)
        old_content = 'def foo():\n    return 1\n    \n    # end\n'
        new_content = 'def foo():\n    return 42\n    \n    # end\n'

        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='exact')
        assert result.startswith('OK'), f"tab/space normalized match should succeed: {result!r}"

        # The file should now contain the replacement
        content = f.read_text(encoding='utf-8')
        assert 'return 42' in content, f"replacement should be in the file: {content!r}"
        # The original tab-indented content is gone
        assert '\treturn 1' not in content, f"old content should be replaced: {content!r}"


def test_exact_match_no_match_helpful_error():
    """When neither raw nor normalized match succeeds, the error should
    include a helpful diff hint showing the first differing line."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        file_content = 'alpha\nbeta\ngamma\n'
        f = _owned_file(om, d, 'nomatch.txt', file_content)

        # old_content has a line that differs from the file
        old_content = 'alpha\nBETA\ngamma\n'
        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content='X', match_mode='exact')
        assert result.startswith('ERROR'), f"should be an error: {result!r}"
        assert 'first diff' in result, f"error should include diff hint: {result!r}"
        assert 'line 2' in result, f"error should name the differing line: {result!r}"


def test_exact_match_raw_still_works():
    """Positive control: a raw exact match (no tab/space mismatch) still works."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        file_content = 'hello\nworld\n'
        f = _owned_file(om, d, 'raw.txt', file_content)

        result = om.edit_file(str(f), 'coder', old_content='hello\n',
                              new_content='goodbye\n', match_mode='exact')
        assert result.startswith('OK'), f"raw exact match should succeed: {result!r}"
        assert f.read_text(encoding='utf-8') == 'goodbye\nworld\n'


# ── (b) Heuristic: trailing context preservation ───────────────────────────


def test_heuristic_trailing_context_preserved():
    """A multi-line function declaration where the replacement is shorter.
    The trailing lines (parameters, body, closing brace) should be preserved."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        file_content = (
            'struct Report { int v; };\n'
            '\n'
            'Context make_context(std::uint64_t id, std::string protocol,\n'
            '                    const std::string& extra) {\n'
            '    return {};\n'
            '}\n'
        )
        f = _owned_file(om, d, 'events.h', file_content)

        # old_content includes the full function signature (2 lines)
        old_content = (
            'Context make_context(std::uint64_t id, std::string protocol,\n'
            '                    const std::string& extra) {'
        )
        # new_content is shorter — just the function name
        new_content = 'Context make_context_v2'

        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='heuristic')
        assert result.startswith('OK'), f"heuristic edit should succeed: {result!r}"

        content = f.read_text(encoding='utf-8')
        # The new name should be present
        assert 'make_context_v2' in content, f"new name should be in file: {content!r}"
        # The trailing lines (parameters, body) should be preserved exactly once
        assert content.count('const std::string& extra') == 1, \
            f"parameter should appear exactly once (no duplication): {content!r}"
        assert content.count('return {};') == 1, \
            f"function body should appear exactly once: {content!r}"
        # The struct should still be there
        assert 'struct Report' in content, f"struct should be preserved: {content!r}"


def test_heuristic_full_replacement_no_trailing_drop():
    """Positive control: when new_content covers the full matched region,
    no trailing lines should be preserved (normal replacement)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        file_content = (
            'void foo(int a) {\n'
            '    return;\n'
            '}\n'
            'int x = 1;\n'
        )
        f = _owned_file(om, d, 'full.cpp', file_content)

        # old_content matches the full function (3 lines)
        old_content = 'void foo(int a) {\n    return;\n}'
        # new_content also covers 3 lines (full replacement)
        new_content = 'void foo_v2(int a) {\n    return;\n}'

        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='heuristic')
        assert result.startswith('OK'), f"heuristic edit should succeed: {result!r}"

        content = f.read_text(encoding='utf-8')
        assert 'foo_v2' in content, f"new name should be in file: {content!r}"
        assert 'int x = 1;' in content, f"subsequent code should be preserved: {content!r}"
        # The old function body should be gone (replaced)
        assert content.count('return;') == 1, \
            f"should have exactly one 'return;' (the new one): {content!r}"


def test_heuristic_modification_trailing_preserved_no_duplication(tmp_path):
    """When new_content modifies the first line of a multi-line match, the
    trailing lines are preserved exactly once (no duplication)."""
    om = _make_om(tmp_path)
    d = tmp_path / 'src'
    d.mkdir()
    file_content = (
        'Context make_context(std::uint64_t id) {\n'
        '    return Context(id);\n'
        '}\n'
        'int x = 1;\n'
    )
    f = _owned_file(om, d, 'modify.cpp', file_content)

    # old_content matches the 2-line signature + opening brace
    old_content = (
        'Context make_context(std::uint64_t id) {\n'
        '    return Context(id);'
    )
    # new_content is shorter — just the modified name
    new_content = 'Context make_context_v2(std::uint64_t id) {'

    result = om.edit_file(str(f), 'coder', old_content=old_content,
                          new_content=new_content, match_mode='heuristic')
    assert result.startswith('OK'), f"heuristic edit should succeed: {result!r}"

    content = f.read_text(encoding='utf-8')
    # The new name should be present
    assert 'make_context_v2' in content, f"new name should be in file: {content!r}"
    # The body line should appear exactly once
    assert content.count('return Context(id);') == 1, \
        f"body should appear exactly once: {content!r}"
    # The closing brace should appear exactly once
    assert content.count('}') == 1, \
        f"closing brace should appear exactly once: {content!r}"
    # Subsequent code preserved
    assert 'int x = 1;' in content, f"subsequent code should be preserved: {content!r}"


def test_heuristic_intentional_deletion_no_resurrection(tmp_path):
    """Intentional line deletion via heuristic mode must NOT resurrect trailing lines.

    If the user replaces a 3-line block with a 2-line subset (deleting the
    middle line), the trailing preservation must not fire — the deleted
    line should stay gone.
    """
    om = _make_om(tmp_path)
    d = tmp_path / 'src'
    d.mkdir()
    file_content = 'line1\nline2\nline3\nline4\n'
    f = _owned_file(om, d, 'deletion.txt', file_content)

    # Delete line2: replace the 3-line block with lines 1 and 3 only
    old_content = 'line1\nline2\nline3'
    new_content = 'line1\nline3'

    result = om.edit_file(str(f), 'coder', old_content=old_content,
                          new_content=new_content, match_mode='heuristic')
    assert result.startswith('OK'), f"heuristic edit should succeed: {result!r}"

    content = f.read_text(encoding='utf-8')
    # line2 should be gone
    assert 'line2' not in content, f"line2 should be deleted: {content!r}"
    # line3 should appear exactly once (not duplicated by trailing preservation)
    assert content.count('line3') == 1, \
        f"line3 should appear exactly once (no resurrection): {content!r}"
    # line4 should be preserved
    assert 'line4' in content, f"line4 should be preserved: {content!r}"


def test_exact_match_tab_space_with_trailing_content():
    """Tab-indented file with content AFTER the match region.
    The normalized match must succeed and preserve the trailing content.
    This exercises the end-of-match guard (fix #1) and the trailing-newline
    trim (fix #2)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        # File uses tabs; has a trailing comment line after the function
        file_content = 'def foo():\n\treturn 1\n# trailing\n'
        f = _owned_file(om, d, 'tabs_trailing.py', file_content)

        # old_content uses spaces and ends with a newline
        old_content = 'def foo():\n    return 1\n'
        new_content = 'def foo():\n    return 99\n'

        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='exact')
        assert result.startswith('OK'), f"normalized match with trailing content should succeed: {result!r}"

        content = f.read_text(encoding='utf-8')
        assert 'return 99' in content, f"replacement should be in the file: {content!r}"
        assert '# trailing' in content, f"trailing content must be preserved: {content!r}"
        assert 'return 1' not in content, f"old content should be replaced: {content!r}"


def test_exact_match_no_trailing_newline_no_glue():
    """When old_content has no trailing newline, the replacement must not
    glue to the next line (fix #2)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        file_content = 'def foo():\n\treturn 1\n# trailing\n'
        f = _owned_file(om, d, 'tabs_no_nl.py', file_content)

        # old_content has NO trailing newline
        old_content = 'def foo():\n    return 1'
        new_content = 'def foo():\n    return 42'

        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='exact')
        assert result.startswith('OK'), f"match without trailing newline should succeed: {result!r}"

        content = f.read_text(encoding='utf-8')
        assert 'return 42' in content, f"replacement should be in the file: {content!r}"
        # The trailing line must be on its own line (not glued)
        assert '# trailing' in content, f"trailing content must be on its own line: {content!r}"
        assert '\n# trailing' in content, f"newline before trailing content: {content!r}"
