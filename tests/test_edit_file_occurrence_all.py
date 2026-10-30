#!/usr/bin/env python3
"""Tests for BUG_0059: edit_file occurrence=-1 replaces ALL occurrences.

Covers:
  - replace-all on a 3× pattern (asserts content + "replaced all 3" message)
  - regression: occurrence=2 still replaces only the 2nd
  - occurrence=-1 on a unique pattern (count=1) → works, no error
  - occurrence=5 when count=3 → error mentioning "-1 to replace all"
  - occurrence="-1" as a STRING (LLM fidelity) → coerced to int, works
  - non-overlapping semantics: "aa" in "aaaa" → 2 matches replaced

Run: python -m pytest tests/test_edit_file_occurrence_all.py -v -p no:xdist -o addopts=""
"""

import tempfile
from pathlib import Path

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_om(tmpdir):
    from agent_cascade.operation_manager import OperationManager
    return OperationManager(base_dir=str(tmpdir))


def _owned_file(om, tmpdir, name, content, agent='coder'):
    """Write a file in the workspace and mark it owned so edits auto-approve."""
    f = Path(tmpdir, name)
    f.write_text(content, encoding='utf-8')
    om._own(f.resolve(), agent)
    return f


def _edit(om, f, old, new, occurrence, agent='coder'):
    """Invoke exact-mode edit_file with a given occurrence value."""
    return om.edit_file(str(f), agent, old_content=old, new_content=new,
                        match_mode='exact', occurrence=occurrence)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_occurrence_minus1_replaces_all_three():
    """Pattern appears 3×; occurrence=-1 replaces ALL 3 and message says so."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'foo\nbar\nfoo\nbaz\nfoo\nqux\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='foo', new='FOO', occurrence=-1)
        assert res.startswith('OK'), f"expected success, got: {res!r}"
        assert 'replaced all 3' in res, f"message must report 3 replacements: {res!r}"
        assert f.read_text(encoding='utf-8') == 'FOO\nbar\nFOO\nbaz\nFOO\nqux\n'


def test_occurrence_2_still_replaces_only_second():
    """Regression guard: occurrence=2 replaces ONLY the 2nd match."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'foo\nbar\nfoo\nbaz\nfoo\nqux\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='foo', new='FOO', occurrence=2)
        assert res.startswith('OK'), f"expected success, got: {res!r}"
        assert f.read_text(encoding='utf-8') == 'foo\nbar\nFOO\nbaz\nfoo\nqux\n'


def test_occurrence_minus1_unique_pattern():
    """occurrence=-1 on a pattern present once (count=1) → replaces it, no error."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'hello world\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='hello', new='HELLO', occurrence=-1)
        assert res.startswith('OK'), f"expected success, got: {res!r}"
        assert 'replaced all 1' in res, f"message must report 1 replacement: {res!r}"
        assert f.read_text(encoding='utf-8') == 'HELLO world\n'


def test_occurrence_out_of_range_mentions_minus1():
    """occurrence=5 when count=3 → error string mentions '-1 to replace all'."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'a\nb\na\nc\na\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='a', new='X', occurrence=5)
        assert res.startswith('ERROR'), f"expected an error, got: {res!r}"
        assert '-1 to replace all' in res, f"error must mention -1 sentinel: {res!r}"


def test_occurrence_minus1_as_string():
    """occurrence='-1' sent as a string (LLM fidelity) → coerced to int -1, works."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'x\ny\nx\nz\nx\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='x', new='X', occurrence='-1')
        assert res.startswith('OK'), f"expected success, got: {res!r}"
        assert f.read_text(encoding='utf-8') == 'X\ny\nX\nz\nX\n'


def test_non_overlapping_semantics():
    """'aa' in 'aaaa' has 2 non-overlapping matches (str.count semantics).
    occurrence=-1 replaces both → 'XXXX'."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'aaaa\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='aa', new='XX', occurrence=-1)
        assert res.startswith('OK'), f"expected success, got: {res!r}"
        assert 'replaced all 2' in res, f"message must report 2: {res!r}"
        assert f.read_text(encoding='utf-8') == 'XXXX\n'


def test_occurrence_zero_is_out_of_range():
    """occurrence=0 is below the valid 1..N range → error mentioning -1 sentinel."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'a\nb\na\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='a', new='X', occurrence=0)
        assert res.startswith('ERROR'), f"expected an error, got: {res!r}"
        assert '-1 to replace all' in res, f"error must mention -1 sentinel: {res!r}"


def test_occurrence_negative_two_is_out_of_range():
    """occurrence=-2 (not the -1 sentinel) → out-of-range error."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'a\nb\na\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='a', new='X', occurrence=-2)
        assert res.startswith('ERROR'), f"expected an error, got: {res!r}"
        assert 'out of range' in res.lower(), f"error must say out of range: {res!r}"


def test_occurrence_minus1_pattern_absent_errors():
    """occurrence=-1 on a file where the pattern is absent → 'ERROR: Pattern not found',
    file unchanged (no write, no backup)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'hello\nworld\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='missing', new='X', occurrence=-1)
        assert res.startswith('ERROR'), f"expected an error, got: {res!r}"
        assert 'Pattern not found' in res, f"error must name the cause: {res!r}"
        assert f.read_text(encoding='utf-8') == content, 'file must be unchanged on error'


def test_occurrence_minus1_noop_identical_content():
    """occurrence=-1 with new_content == old_content → NO-OP, file unchanged."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'foo\nbar\nfoo\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='foo', new='foo', occurrence=-1)
        assert res.startswith('NO-OP'), f"expected a no-op, got: {res!r}"
        assert f.read_text(encoding='utf-8') == content, 'file must be unchanged on no-op'


def test_occurrence_non_numeric_string_errors():
    """occurrence='abc' → coercion failure, error mentions 'positive integer or -1'."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        content = 'a\nb\na\n'
        f = _owned_file(om, d, 'f.txt', content)
        res = _edit(om, f, old='a', new='X', occurrence='abc')
        assert res.startswith('ERROR'), f"expected an error, got: {res!r}"
        assert 'positive integer or -1' in res, f"error must explain valid values: {res!r}"
