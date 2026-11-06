"""BUG_0246 regression tests — read_file-specific char trip + lowered line cap.

The outer 6k safety net (``tool_result_max_chars``, BUG_0189) trims the 2k-6k
pass-through band ONLY for raw bodies >6000. A read_file whose rendered body sits in
~3000-6000 was therefore returned FULL. read_file is the single largest per-call input
driver (~26.9%), so the incremental lever is a read_file-only char trip (default 3000)
that fires for BOTH wild and bounded reads and cuts to the 2k wild window with a plain
[TRUNCATED] marker (never the unbound-read warning). Env-only:
``AGENT_CASCADE_READ_FILE_MAX_CHARS`` (-1/0 disables). Secondary lever:
``DEFAULT_READ_FILE_MAX_LINES`` 150 -> 120.

Every "truncates" assertion is paired with a disabled-counterfactual that returns the body
FULL, so the suite FAILS under the old behavior (no trip / cap 150).
"""
import re
import tempfile
from pathlib import Path

from agent_cascade.tools.custom.file_ops import ReadFile


class _FakeOM:
    def _resolve_path(self, path, mode='ro'):
        return Path(path).resolve()


class _FakePool:
    """Minimal pool so tmp_path files resolve, and so ``llm_cfg`` can override the
    ``read_file_max_chars`` trip for the disabled-counterfactual cases."""

    def __init__(self, llm_cfg=None):
        self.operation_manager = _FakeOM()
        self.llm_cfg = llm_cfg or {}


def _read(content, cfg=None, **params):
    """Write *content* to a temp file and run the real ReadFile.call() against it."""
    tmp = tempfile.mkdtemp()
    p = Path(tmp) / 'probe.txt'
    p.write_text(content, encoding='utf-8')
    return ReadFile(agent_pool=_FakePool(cfg)).call({'path': str(p), **params})


def _body(out):
    """The rendered content between the code fences (what the model actually sees)."""
    assert '```' in out, out
    return out.split('```')[1]


def _shown_count(out):
    """Parse ``lines A-B/total`` from the header and return the shown line count B-A+1."""
    m = re.search(r'lines (\d+)-(\d+)/', out.split('\n')[0])
    assert m, f"no line range in header: {out.split(chr(10))[0]!r}"
    return int(m.group(2)) - int(m.group(1)) + 1


# Body sizes (formatted ``NNN:\t...``) computed against _read_text_file's rendering:
_BAND = '\n'.join(f"line {i} " + 'y' * 60 for i in range(1, 53)) + '\n'   # ~3778 (>3000, <6000, 52 lines)
_SMALL = '\n'.join(f"line {i} " + 'z' * 30 for i in range(1, 61)) + '\n'  # ~2562 (<3000)
_PROBE_5K = '\n'.join(f"line {i} " + 'q' * 90 for i in range(1, 52)) + '\n'  # ~5235 (>3000)
_PROBE_8K = '\n'.join(f"line {i} " + 'w' * 150 for i in range(1, 41)) + '\n'  # ~6502 (>6000)


class TestReadFileCharTrip:
    def test_wild_read_trip_truncates_with_plain_marker(self):
        """A wild read whose body >3000 is cut to ~2k with [TRUNCATED], not the warning."""
        out = _read(_BAND)  # no limit -> wild
        assert out.startswith('OK:'), out
        assert '[TRUNCATED]' in out
        assert 'Unbound read detected!' not in out, 'read_file trip must not emit the wild warning'
        assert '→ continue at start_line=' in out
        assert len(_body(out)) < 2005, f"body not cut: {len(_body(out))}"

    def test_bounded_read_trip_truncates(self):
        """A bounded read (explicit limit) >3000 is ALSO cut — this is the new lever."""
        out = _read(_BAND, limit=52)  # explicit limit, not wild
        assert out.startswith('OK:'), out
        assert '[TRUNCATED]' in out
        assert 'Unbound read detected!' not in out
        assert '→ continue at start_line=' in out
        assert len(_body(out)) < 2005, f"bounded body not cut: {len(_body(out))}"

    def test_trip_disabled_returns_full_body(self):
        """Counterfactual: with the trip disabled (read_file_max_chars=-1) the same reads
        return the FULL >3000 body with no marker — the truncation above FAILS without the lever."""
        for kwargs in ({}, {'limit': 52}):
            out = _read(_BAND, cfg={'read_file_max_chars': -1}, **kwargs)
            assert out.startswith('OK:'), out
            assert '[TRUNCATED]' not in out
            assert 'Unbound read detected!' not in out
            body = _body(out)
            assert 3000 < len(body) < 6000, f"expected full 3778 body, got {len(body)}"
            assert 'line 52' in body, 'last line must be present when trip disabled'

    def test_body_below_trip_passes_untouched(self):
        """The median read (~2.8k) must stay fully intact: no marker, reaches EOF."""
        out = _read(_SMALL)  # ~2562 < 3000, 60 lines < 120
        assert out.startswith('OK:'), out
        assert '[TRUNCATED]' not in out
        assert 'Unbound read detected!' not in out
        assert '(EOF)' in out.split('\n')[0]
        assert 'line 60' in _body(out)

    def test_probe_5kb_cuts_to_window(self):
        """~5KB read (in the old pass-through band) -> ~2k + [TRUNCATED] under the 3000 trip."""
        out = _read(_PROBE_5K)
        assert '[TRUNCATED]' in out
        assert 'Unbound read detected!' not in out
        assert len(_body(out)) < 2005

    def test_probe_8kb_cuts_to_window_monotone_safe(self):
        """8KB (>6k) read -> ~2k + [TRUNCATED]; the trip fires first, so no double-warn,
        and the cut is never larger than the outer net's 2k target (monotone-safe)."""
        out = _read(_PROBE_8K)
        assert '[TRUNCATED]' in out
        assert 'Unbound read detected!' not in out
        assert len(_body(out)) < 2005


class TestReadFileLineCap:
    def test_default_cap_shows_120_of_200_lines(self):
        """Counterfactual vs the old 150: 200 short lines -> exactly 120 shown + [TRUNCATED].

        Lines are short so the body of the 120 shown lines stays under 3000 and the LINE cap,
        not the char trip, is what bounds the read."""
        content = '\n'.join(f"L{i}" for i in range(1, 201)) + '\n'
        out = _read(content)  # wild, default line cap
        assert '[TRUNCATED]' in out
        shown = _shown_count(out)
        assert shown == 120, f"expected 120 shown lines (new cap), got {shown}"

    def test_typical_read_under_cap_is_whole(self):
        """110 short lines is below the 120 cap -> returned whole, EOF, no marker.

        Proves the cap is not over-cutting the common read (median ~48 lines)."""
        content = '\n'.join(f"L{i}" for i in range(1, 111)) + '\n'
        out = _read(content)
        assert '[TRUNCATED]' not in out
        assert '(EOF)' in out.split('\n')[0]
        assert _shown_count(out) == 110
