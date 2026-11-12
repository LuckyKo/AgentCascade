#!/usr/bin/env python3
"""P3 edit_file matching-quality tests.

Covers the two-stage unicode locating tier and the net-new non-literal-match
disclosure:

  * P3a — when raw-exact fails, stage 1 (tab→4 spaces) is tried unchanged; only
    if that also misses does stage 2 (tab + conservative unicode char-map) run.
    The tier is locate-only: it broadens WHERE a span is found, never WHAT is
    written (new_content verbatim, raw file bytes replaced).
  * P3b — a heuristic or normalized-fallback edit carries a prominent
    non-literal warning; a fallback match is no longer mislabeled bare (exact, …).

Run: python -m pytest tests/test_edit_file_p3_matching.py -q -p no:xdist
"""

import re
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


def _lf(s):
    """Normalize CRLF/CR to LF for content comparison — edit_file re-renders
    to the file's native line endings, which on a Windows host is CRLF."""
    return s.replace('\r\n', '\n').replace('\r', '\n')


# Typographic characters the locating tier maps to ASCII (punctuation/space only).
LDQ = '\u201c'   # left double quote → "
RDQ = '\u201d'   # right double quote → "
EN = '\u2013'    # en dash → -
EM = '\u2014'    # em dash → -
ELL = '\u2026'   # horizontal ellipsis → ...
NBSP = '\u00a0'  # no-break space → space
NNBSP = '\u202f'  # narrow no-break space → space
THIN = '\u2009'  # thin space → space


# ── P3a — two-stage locating tier (locate-only) ──────────────────────────────


def test_p3a_smart_quote_locates_verbatim():
    """A smart-quote old_content that fails raw-exact succeeds via stage 2,
    writes new_content content-verbatim, and reports kind=unicode (not bare
    (exact, …))."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'q.py', 'a = 1\nx = "hi"\nz = 2\n')
        old_content = f'x = {LDQ}hi{RDQ}\n'   # typographic → no raw match
        new_content = 'x = 99\n'
        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='exact')
        assert result.startswith('OK'), result
        assert _lf(f.read_text(encoding='utf-8')) == 'a = 1\nx = 99\nz = 2\n'
        assert 'matched after unicode normalization' in result, result
        assert 'unicode-norm' in result, result
        assert '(exact,' not in result, result


def test_p3a_new_content_smart_quote_preserved():
    """new_content containing a smart quote is written UNCHANGED — the tier
    normalizes only the locating inputs, never the replacement."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'q2.py', 'a = 1\nx = "hi"\nz = 2\n')
        old_content = f'x = {LDQ}hi{RDQ}\n'   # resolves via unicode tier
        new_content = f'x = {LDQ}yo{RDQ}\n'   # replacement carries a smart quote
        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='exact')
        assert result.startswith('OK'), result
        assert f'x = {LDQ}yo{RDQ}' in f.read_text(encoding='utf-8'), \
            'new_content must be written verbatim (smart quote preserved)'


def test_p3a_stage1_tab_space_byte_identical():
    """A tab/space-only miss still resolves via stage 1 exactly as before P3
    (correct replacement, kind=tab/space — the unicode stage is not involved)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'tabs.py', 'def foo():\n\treturn 1\n')
        old_content = 'def foo():\n    return 1\n'   # spaces where the file has tabs
        new_content = 'def foo():\n    return 42\n'
        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='exact')
        assert result.startswith('OK'), result
        assert _lf(f.read_text(encoding='utf-8')) == 'def foo():\n    return 42\n'
        assert 'matched after tab/space normalization' in result, result
        assert 'tab/space-norm' in result, result
        assert '(exact,' not in result, result


def test_p3a_unicode_noop_on_raw_exact():
    """A file that CONTAINS unicode but whose old_content raw-exact-matches
    never enters the tier: the response stays a plain (exact, …) with no
    normalization warning."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        line = f'x = {LDQ}hi{RDQ}\n'           # file itself has smart quotes
        f = _owned_file(om, d, 'u.py', f'z = 0\n{line}')
        old_content = line                     # byte-identical → raw exact match
        new_content = 'x = 0\n'
        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='exact')
        assert result.startswith('OK'), result
        assert _lf(f.read_text(encoding='utf-8')) == 'z = 0\nx = 0\n'
        assert '(exact,' in result, result      # raw-exact label retained
        assert 'matched after' not in result, result


def test_p3a_coverage_dash_ellipsis_space():
    """En/em dash, ellipsis, and the three space variants each resolve via the
    unicode tier and write new_content verbatim."""
    cases = [
        ('a-b\n', f'a{EN}b\n'),      # en dash
        ('a-b\n', f'a{EM}b\n'),      # em dash
        ('a...b\n', f'a{ELL}b\n'),   # ellipsis → three dots
        ('a b\n', f'a{NBSP}b\n'),    # no-break space
        ('a b\n', f'a{NNBSP}b\n'),   # narrow no-break space
        ('a b\n', f'a{THIN}b\n'),    # thin space
    ]
    for i, (file_line, old_line) in enumerate(cases):
        with tempfile.TemporaryDirectory() as d:
            om = _make_om(d)
            f = _owned_file(om, d, f'cov{i}.txt', f'head\n{file_line}tail\n')
            result = om.edit_file(str(f), 'coder', old_content=old_line,
                                  new_content='REPLACED\n', match_mode='exact')
            assert result.startswith('OK'), (i, repr(old_line), result)
            assert _lf(f.read_text(encoding='utf-8')) == 'head\nREPLACED\ntail\n', (i, repr(old_line))
            assert 'matched after unicode normalization' in result, (i, result)


def test_p3a_ambiguous_unicode_rejected():
    """Two raw-differing lines that collapse to the same normalized string make
    the normalized match ambiguous (count>1) → rejected as a non-unique match."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        # line1 en dash, line2 em dash; both normalize to 'a-b'.
        f = _owned_file(om, d, 'amb.txt', f'a{EN}b\na{EM}b\n')
        old_content = 'a-b\n'   # ASCII hyphen matches BOTH after normalization
        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content='X\n', match_mode='exact')
        assert result.startswith('ERROR'), result
        assert 'found 2 times' in result, result


def test_p3a_raw_span_repeat_is_guarded():
    """A repeated raw line-snapped span cannot resolve to the wrong site: it
    forces normalized count>1, which the uniqueness guard rejects
    deterministically. This test proves that guard fires for a file whose snapped
    span would repeat."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        # The snapped raw span 'x"hi"\n' occurs twice (raw count 0 vs old_content).
        f = _owned_file(om, d, 'rep.txt', f'x{LDQ}hi{RDQ}\ny\nx{LDQ}hi{RDQ}\n')
        old_content = 'x"hi"\n'   # straight quotes → normalizes to match both lines
        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content='X\n', match_mode='exact')
        assert result.startswith('ERROR') and 'found 2 times' in result, result


# ── P3b — prominent non-literal-match disclosure ─────────────────────────────


def test_p3b_heuristic_warning_keeps_ratio_token():
    """A heuristic edit carries the prominent non-literal warning AND retains the
    existing {ratio:.0%} token (backward compatibility)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'h.cpp', 'void foo(int a) {\n    return;\n}\nint x = 1;\n')
        old_content = 'void foo(int a) {\n    return;\n}'
        new_content = 'void foo_v2(int a) {\n    return;\n}'
        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='heuristic')
        assert result.startswith('OK'), result
        # The new prominent warning is present...
        assert 'non-literal match (heuristic' in result, result
        # ...and the existing terse label token still survives independently
        # (this fails if the terse "(heuristic N%, ...)" is ever removed — the
        # ⚠ line also carries a ratio, so a bare r'\d+%' check is not enough).
        assert re.search(r'\(heur(istic|_ag) \d+%', result), result
        assert re.search(r'-\d+ \+\d+ = [+-]?\d+net\)', result), result


def test_p3b_normalized_fallback_not_labeled_exact():
    """A normalized-fallback exact edit is labeled by its normalization kind, not
    as a bare literal (exact, …). Focuses on the label change only — the applied
    replacement itself (and its disclosure line) is covered by the Stage-1 test."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'tab2.py', 'def foo():\n\treturn 1\n')
        old_content = 'def foo():\n    return 1\n'
        new_content = 'def foo():\n    return 2\n'
        result = om.edit_file(str(f), 'coder', old_content=old_content,
                              new_content=new_content, match_mode='exact')
        assert result.startswith('OK'), result
        assert 'tab/space-norm' in result, result   # label uses the normalization kind
        assert '(exact,' not in result, result      # no longer mislabeled bare exact
