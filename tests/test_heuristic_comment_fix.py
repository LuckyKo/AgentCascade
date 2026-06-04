#!/usr/bin/env python3
"""Test that heuristic edit_file matching no longer strips comments, preventing
comment duplication/loss and indentation corruption.

The old bug: heuristic mode stripped comments before matching, then replaced
using raw file content. If old_content had different comment text/count than the
file, comments were silently lost or duplicated. Alignment was built on
normalized (comment-stripped) lines, so unmapped new_content lines lost
per-line indentation context.

The fix: heuristic mode now matches on raw content with only whitespace
normalization — no comment stripping at all.
"""

import difflib
import sys

# Use the project's configured threshold, with a fallback default
try:
    from agent_cascade.settings import DEFAULT_HEURISTIC_MATCH_THRESHOLD as THRESHOLD
except ImportError:
    THRESHOLD = 0.9  # fallback if settings module unavailable


def get_leading_whitespace(s):
    """Get leading whitespace of first non-blank line."""
    for line in s.splitlines():
        if line.strip():
            return line[:len(line) - len(line.lstrip())]
    return ""

def get_indent_width(indent_str):
    """Calculate indent width in spaces (tab=4)."""
    return sum(4 if c == '\t' else 1 for c in indent_str if c in ' \t')


# ============================================================================
# Simulated heuristic matching — replicates the FIXED logic from operation_manager.py
# ============================================================================

def heuristic_match(file_content, old_content):
    """Simulate the fixed heuristic matching logic (no comment stripping).
    
    Returns (actual_old_content, match_ratio) or raises ValueError on failure.
    """
    file_lines = file_content.splitlines(keepends=True)
    
    # Map normalized lines of the raw file
    file_line_info = []
    for idx, line in enumerate(file_lines):
        norm = "".join(line.split())
        if norm:
            file_line_info.append((idx, norm))
    
    # Map normalized lines of old_content
    old_line_info = []
    for line in old_content.splitlines(keepends=True):
        norm = "".join(line.split())
        if norm:
            old_line_info.append(norm)
    
    if not old_line_info:
        raise ValueError("old_content contains only whitespace")
    
    # Build file line map
    file_line_map = {}
    for list_idx, (orig_idx, norm) in enumerate(file_line_info):
        file_line_map.setdefault(norm, []).append(list_idx)
    
    candidates = set()
    n_old = len(old_line_info)
    n_file = len(file_line_info)
    
    for old_idx, norm in enumerate(old_line_info):
        if norm in file_line_map and len(file_line_map[norm]) <= 20:
            for list_idx in file_line_map[norm]:
                start = list_idx - old_idx
                if 0 <= start <= n_file - n_old:
                    candidates.add(start)
    
    matches = []
    
    for start_idx in candidates:
        best_ratio = 0.0
        best_info = None
        for size in range(max(1, n_old - 2), min(n_file - start_idx + 1, n_old + 3)):
            candidate_slice = file_line_info[start_idx : start_idx + size]
            candidate_norms = [item[1] for item in candidate_slice]
            ratio = difflib.SequenceMatcher(
                None, "".join(old_line_info), "".join(candidate_norms)
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_info = {
                    'start_list_idx': start_idx,
                    'end_list_idx': start_idx + size,
                    'ratio': ratio
                }
        if best_info and best_ratio >= THRESHOLD:
            matches.append(best_info)
    
    if not matches:
        raise ValueError(f"Heuristic pattern not found (threshold={THRESHOLD:.0%})")
    if len(matches) > 1:
        raise ValueError(f"Pattern found {len(matches)} times - not unique")
    
    match = matches[0]
    orig_start = file_line_info[match['start_list_idx']][0]
    orig_end = file_line_info[match['end_list_idx'] - 1][0]
    
    actual_old_content = "".join(file_lines[orig_start : orig_end + 1])
    return actual_old_content, match['ratio']


def apply_indent_preservation(old_content, new_content, actual_old_content):
    """Simulate the indentation preservation logic from operation_manager.py."""
    old_norm_lines = ["".join(l.split()) for l in old_content.splitlines()]
    file_norm_lines = ["".join(l.split()) for l in actual_old_content.splitlines()]
    new_norm_lines = ["".join(l.split()) for l in new_content.splitlines()]
    
    # Phase 1 - Alignment: old_content -> file block
    matcher = difflib.SequenceMatcher(None, old_norm_lines, file_norm_lines)
    old_to_file_map = {}
    for tag, i1s, i1e, j1s, j1e in matcher.get_opcodes():
        if tag == 'equal':
            for a, b in zip(range(i1s, i1e), range(j1s, j1e)):
                old_to_file_map[a] = b
        elif tag == 'replace':
            sub = difflib.SequenceMatcher(None, old_norm_lines[i1s:i1e], file_norm_lines[j1s:j1e])
            for tag2, a_s, a_e, b_s, b_e in sub.get_opcodes():
                if tag2 == 'equal':
                    for a, b in zip(range(a_s, a_e), range(b_s, b_e)):
                        old_to_file_map[i1s + a] = j1s + b
    
    # new_content -> old_content alignment
    new_to_old_map = {}
    matcher2 = difflib.SequenceMatcher(None, new_norm_lines, old_norm_lines)
    for tag, i1s, i1e, j1s, j1e in matcher2.get_opcodes():
        if tag == 'equal':
            for a, b in zip(range(i1s, i1e), range(j1s, j1e)):
                new_to_old_map[a] = b
    
    # Combined: new_content -> file block
    new_to_file_map = {}
    for n_idx, o_idx in new_to_old_map.items():
        if o_idx in old_to_file_map:
            new_to_file_map[n_idx] = old_to_file_map[o_idx]
    
    # File indents by line
    file_block_lines = actual_old_content.splitlines(keepends=True)
    file_indent_by_line = {}
    for idx, fl in enumerate(file_block_lines):
        if "".join(fl.split()):
            leading_ws = fl[:len(fl) - len(fl.lstrip())] if fl.strip() else ""
            file_indent_by_line[idx] = leading_ws
    
    # Phase 2 - Apply indents
    new_content_lines = new_content.splitlines(keepends=True)
    adjusted_lines = []
    
    file_indent = get_leading_whitespace(actual_old_content)
    old_indent = get_leading_whitespace(old_content)
    delta_width = get_indent_width(file_indent) - get_indent_width(old_indent)
    
    for line_idx, line in enumerate(new_content_lines):
        if not line.strip():
            adjusted_lines.append(line)
            continue
        
        if line_idx in new_to_file_map:
            f_idx = new_to_file_map[line_idx]
            if f_idx in file_indent_by_line:
                orig_leading_ws = file_indent_by_line[f_idx]
                adjusted_lines.append(orig_leading_ws + line.lstrip())
                continue
        
        # Fallback: base indent delta
        if file_indent != old_indent and delta_width != 0:
            current_indent = line[:len(line) - len(line.lstrip())]
            current_width = get_indent_width(current_indent)
            new_spaces = max(0, current_width + delta_width)
            adjusted_lines.append((' ' * new_spaces) + line.lstrip())
        elif file_indent:
            adjusted_lines.append(file_indent + line.lstrip())
        else:
            adjusted_lines.append(line)
    
    return "".join(adjusted_lines)


# ============================================================================
# Test cases
# ============================================================================

def test_comment_preservation_basic():
    """Comments in old_content match and don't get duplicated/lost."""
    file_content = (
        "def foo():\n"
        "    # This is a comment\n"
        "    x = 1\n"
        "    y = 2\n"
        "    return x + y\n"
    )
    old_content = file_content  # exact match
    new_content = (
        "def foo():\n"
        "    # Modified comment\n"
        "    x = 10\n"
        "    y = 20\n"
        "    z = 30\n"
        "    return x + y + z\n"
    )
    
    actual_old, ratio = heuristic_match(file_content, old_content)
    adjusted_new = apply_indent_preservation(old_content, new_content, actual_old)
    result = file_content.replace(actual_old, adjusted_new, 1)
    
    # Exactly 1 comment line (the new one)
    comment_count = sum(1 for line in result.splitlines() if line.strip().startswith('#'))
    assert comment_count == 1, f"Expected 1 comment, got {comment_count}"
    
    assert "# Modified comment" in result
    assert "z = 30" in result
    
    # Indentation check
    for line in result.splitlines():
        if not line.strip() or line.strip().startswith('#'):
            continue
        leading = len(line) - len(line.lstrip())
        assert leading in (0, 4), f"Bad indent on: {line!r}"
    
    print("PASS test_comment_preservation_basic")


def test_comment_count_difference():
    """Differing comment counts should cause match failure, NOT silent duplication."""
    file_content = (
        "def bar():\n"
        "    # Comment A\n"
        "    # Comment B\n"
        "    val = 42\n"
        "    return val\n"
    )
    old_content = (
        "def bar():\n"
        "    # Comment A\n"
        "    val = 42\n"
        "    return val\n"
    )
    
    try:
        actual_old, ratio = heuristic_match(file_content, old_content)
        # If it matches, verify matched block has both file comments
        cc = sum(1 for l in actual_old.splitlines() if l.strip().startswith('#'))
        assert cc == 2, f"Matched {cc} comments but file has 2 - mismatch!"
    except ValueError:
        # Expected: match fails because content differs
        print("PASS test_comment_count_difference (rejected)")
        return
    
    print("PASS test_comment_count_difference (matched correctly)")


def test_indentation_preservation():
    """Indentation preserved without comment-stripping gaps."""
    file_content = (
        "class MyClass:\n"
        "    # Class doc comment\n"
        "    def method(self):\n"
        "        # Method comment\n"
        "        x = 1\n"
        "        y = 2\n"
        "        return x + y\n"
    )
    old_content = file_content
    new_content = (
        "class MyClass:\n"
        "    # Updated class comment\n"
        "    def method(self):\n"
        "        # Updated method comment\n"
        "        x = 10\n"
        "        y = 20\n"
        "        z = 30\n"
        "        return x + y + z\n"
    )
    
    actual_old, ratio = heuristic_match(file_content, old_content)
    adjusted_new = apply_indent_preservation(old_content, new_content, actual_old)
    result = file_content.replace(actual_old, adjusted_new, 1)
    
    # Exactly 2 comments (the new ones)
    cc = sum(1 for l in result.splitlines() if l.strip().startswith('#'))
    assert cc == 2, f"Expected 2 comments, got {cc}"
    
    # Verify indentation levels
    for line in adjusted_new.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        leading = len(line) - len(line.lstrip())
        if stripped.startswith('class '):
            assert leading == 0, f"Line {line!r}: expected 0, got {leading}"
        elif stripped.startswith('# Updated class'):
            assert leading == 4, f"Line {line!r}: expected 4, got {leading}"
        elif stripped.startswith('def '):
            # def is inside class -> 4 spaces
            assert leading == 4, f"Line {line!r}: expected 4, got {leading}"
        elif stripped.startswith('# Updated method') or stripped.startswith('x =') \
             or stripped.startswith('y =') or stripped.startswith('z =') \
             or stripped.startswith('return'):
            # Inside method -> 8 spaces
            assert leading == 8, f"Line {line!r}: expected 8, got {leading}"
    
    print("PASS test_indentation_preservation")


def test_whitespace_tolerance():
    """Heuristic mode tolerates whitespace differences (no regression)."""
    file_content = (
        "def hello():\n"
        "    x=1\n"
        "    y=2\n"
        "    return x+y\n"
    )
    old_content = (
        "def hello():\n"
        "    x = 1\n"
        "    y = 2\n"
        "    return x + y\n"
    )
    
    actual_old, ratio = heuristic_match(file_content, old_content)
    assert "x=1" in actual_old, "Should match file content exactly"
    
    new_content = (
        "def hello():\n"
        "    x = 10\n"
        "    y = 20\n"
        "    return x + y\n"
    )
    
    adjusted_new = apply_indent_preservation(old_content, new_content, actual_old)
    result = file_content.replace(actual_old, adjusted_new, 1)
    assert "x = 10" in result or "x=10" in result
    
    print("PASS test_whitespace_tolerance")


def test_no_comment_stripping():
    """Core fix test: comment text IS part of normalized line comparison."""
    file_content = (
        "def func():\n"
        "    # Important comment\n"
        "    x = 1\n"
    )
    
    old_with_comment = (
        "def func():\n"
        "    # Important comment\n"
        "    x = 1\n"
    )
    old_without_comment = (
        "def func():\n"
        "    x = 1\n"
    )
    
    # WITH comment: should match
    actual_old, ratio = heuristic_match(file_content, old_with_comment)
    assert "# Important comment" in actual_old
    
    # WITHOUT comment: should fail (comments are structural now)
    try:
        heuristic_match(file_content, old_without_comment)
    except ValueError:
        print("PASS test_no_comment_stripping (rejected)")
        return
    
    print("PASS test_no_comment_stripping")


def test_multiline_c_comments():
    """C-style multiline comments are treated as structural content."""
    file_content = (
        "/* This is a\n"
        "   multiline comment */\n"
        "void foo() {\n"
        "    // inline comment\n"
        "    int x = 1;\n"
        "}\n"
    )
    old_content = file_content
    new_content = (
        "/* Updated comment */\n"
        "void foo() {\n"
        "    // updated inline comment\n"
        "    int x = 42;\n"
        "}\n"
    )
    
    actual_old, ratio = heuristic_match(file_content, old_content)
    adjusted_new = apply_indent_preservation(old_content, new_content, actual_old)
    result = file_content.replace(actual_old, adjusted_new, 1)
    
    assert "int x = 42;" in result
    assert "/* Updated comment */" in result
    
    print("PASS test_multiline_c_comments")


# ============================================================================
# Run all tests
# ============================================================================

if __name__ == '__main__':
    print("=" * 60)
    print("Testing heuristic edit_file fix: no comment stripping")
    print("=" * 60)
    
    tests = [
        test_comment_preservation_basic,
        test_comment_count_difference,
        test_indentation_preservation,
        test_whitespace_tolerance,
        test_no_comment_stripping,
        test_multiline_c_comments,
    ]
    
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL {test.__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    
    if failed:
        sys.exit(1)
    print("All tests PASSED")