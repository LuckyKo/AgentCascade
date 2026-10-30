"""BUG_0056 regression tests: the '>>>' context-mode marker is documented via a one-line legend.

The grep tool injects a '>>>' marker in front of matched lines when ``context > 0``. That marker
was undocumented and unlabelled, so a reader seeing ``>>>    async def ...`` hypothesised the
'>>>' was in the file content. The fix prepends a one-line legend to the result body whenever at
least one match line carries the marker. These tests pin:

  * the legend IS present when a context-mode grep has a match (marker present),
  * the legend is ABSENT on a clean (non-context, or no-match) grep (no marker),
  * the legend is the first line of the body,
  * grep behaviour is unchanged (same matches, same line numbers).

The Python fallback is forced (no subprocess dependency) so the assertions are deterministic; a
separate fast-path test runs only when a subprocess grep tool is available.
"""
import tempfile
from pathlib import Path

import unittest.mock as mock

import agent_cascade.operation_manager.grep as grep_module
from agent_cascade.operation_manager import OperationManager

# Must match _GREP_MARKER_LEGEND in agent_cascade/operation_manager/grep.py.
_LEGEND = "# note: '>>>' marks matched lines in context mode (unmarked lines are surrounding context)"


def _body(result: str) -> str:
    """Return the result body (everything after the ``summary:\\n\\n`` header)."""
    return result.split('\n\n', 1)[1]


def _force_fallback():
    """Context manager that forces the pure-Python fallback (no ripgrep/grep dependency)."""
    return mock.patch.object(grep_module, '_check_tool_availability', return_value=(False, False))


def test_legend_present_in_context_mode():
    """A context-mode grep with a match must carry the legend (marker present)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 't.txt').write_text('alpha\nZEBRA one\nbravo\n', encoding='utf-8')
        om = OperationManager(base_dir=tmpdir)
        with _force_fallback():
            result = om.grep(pattern='ZEBRA', path='.', context=1)
        assert 'Found 1 matches' in result, f"Expected a match: {result}"
        assert _LEGEND in result, f"Legend missing from context-mode output: {result}"
        assert _body(result).splitlines()[0] == _LEGEND, f"Legend not first body line: {_body(result)!r}"
        assert '>>>' in result, f"Marker should still be present on the match: {result}"
    print('[PASS] test_legend_present_in_context_mode')


def test_legend_absent_in_non_context_mode():
    """A non-context grep must NOT carry the legend (no marker on clean greps)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 't.txt').write_text('alpha\nZEBRA one\nbravo\n', encoding='utf-8')
        om = OperationManager(base_dir=tmpdir)
        with _force_fallback():
            result = om.grep(pattern='ZEBRA', path='.', context=0)
        assert 'Found 1 matches' in result, f"Expected a match: {result}"
        assert _LEGEND not in result, f"Legend must be absent on a clean (non-context) grep: {result}"
        assert '>>>' not in result, f"No marker expected without context: {result}"
    print('[PASS] test_legend_absent_in_non_context_mode')


def test_legend_absent_when_no_matches():
    """A context-mode grep with zero matches must NOT carry the legend (no marker line)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 't.txt').write_text('alpha\nbravo\n', encoding='utf-8')
        om = OperationManager(base_dir=tmpdir)
        with _force_fallback():
            result = om.grep(pattern='ZEBRA', path='.', context=1)
        assert 'No matches found' in result, f"Expected no matches: {result}"
        assert _LEGEND not in result, f"Legend must be absent with no matches: {result}"
    print('[PASS] test_legend_absent_when_no_matches')


def test_single_file_context_mode_legend():
    """The single-file renderer must carry the legend too (context mode with a match)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 'doc.md').write_text('header\nZEBRA target\nfooter\n', encoding='utf-8')
        om = OperationManager(base_dir=tmpdir)
        with _force_fallback():
            result = om.grep(pattern='ZEBRA', path='doc.md', context=1)
        assert 'Found 1 matches' in result, f"Expected a match: {result}"
        assert _LEGEND in result, f"Legend missing from single-file context output: {result}"
        assert _body(result).splitlines()[0] == _LEGEND, f"Legend not first body line: {_body(result)!r}"
    print('[PASS] test_single_file_context_mode_legend')


def test_context_mode_numbering_unchanged():
    """The legend must not alter which lines match or how they are numbered."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 't.txt').write_text('alpha\nZEBRA one\nbravo\nZEBRA two\ndelta\n', encoding='utf-8')
        om = OperationManager(base_dir=tmpdir)
        with _force_fallback():
            result = om.grep(pattern='ZEBRA', path='.', context=1)
        lines = _body(result).splitlines()
        assert lines[0] == _LEGEND, f"Legend not first: {lines[0]!r}"
        # Both matches are present with their correct line numbers (2 and 4).
        assert 't.txt:2:' in result and 't.txt:4:' in result, f"Line numbers missing/changed: {result}"
        # Exactly two marker lines remain (the legend is excluded by content, not by the marker).
        marker_lines = [ln for ln in lines[1:] if '>>>' in ln and not ln.startswith('# note')]
        assert len(marker_lines) == 2, f"Expected 2 marker lines, got {len(marker_lines)}: {lines!r}"
    print('[PASS] test_context_mode_numbering_unchanged')


def test_fast_path_legend_when_available():
    """When a subprocess grep tool is available, the fast path must also carry the legend."""
    rg, grepc = grep_module._check_tool_availability()
    if not (rg or grepc):
        print('[SKIP] no subprocess grep tool available')
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, 't.txt').write_text('alpha\nZEBRA one\nbravo\n', encoding='utf-8')
        om = OperationManager(base_dir=tmpdir)
        result = om.grep(pattern='ZEBRA', path='.', context=1)
        assert 'Found 1 matches' in result, f"Expected a match: {result}"
        assert _LEGEND in result, f"Legend missing from fast-path context output: {result}"
    print('[PASS] test_fast_path_legend_when_available')


if __name__ == '__main__':
    test_legend_present_in_context_mode()
    test_legend_absent_in_non_context_mode()
    test_legend_absent_when_no_matches()
    test_single_file_context_mode_legend()
    test_context_mode_numbering_unchanged()
    test_fast_path_legend_when_available()
    print('\nAll BUG_0056 regression tests passed.')
