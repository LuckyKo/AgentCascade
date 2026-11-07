"""read_file no-limit char default + global net — regression tests.

A NO-LIMIT read (no explicit ``limit``) is truncated to ``read_file_max_chars``
(default ``DEFAULT_READ_FILE_MAX_CHARS`` = 4000) rendered chars at a WHOLE-LINE
boundary (last line kept complete, never chopped mid-line), with a plain
[TRUNCATED] marker — never the unbound-read warning. This REPLACES the legacy
120-line (DEFAULT_READ_FILE_MAX_LINES) default. A BOUNDED read (explicit limit /
range) keeps its requested range and is NEVER char-truncated. Reads stream only
to ~the window so memory is O(window), not O(file).

Global net: any tool result over ``DEFAULT_TOOL_RESULT_MAX_CHARS`` (25000) is cut
to ``DEFAULT_WILD_READ_TRUNCATION_CHARS`` (4000) with a notice.

Env + llm_cfg key: ``AGENT_CASCADE_READ_FILE_MAX_CHARS`` / ``read_file_max_chars``
(-1/0 disables the no-limit default).

Every "truncates" assertion is paired with a counterfactual that returns the body
FULL, so the suite FAILS under the old behavior.
"""
import re
import tempfile
from pathlib import Path

from agent_cascade.tool_utils import truncate_with_spillover
from agent_cascade.tools.custom.file_ops import ReadFile
from agent_cascade.settings import (
    DEFAULT_READ_FILE_MAX_CHARS,
    DEFAULT_TOOL_RESULT_MAX_CHARS,
    DEFAULT_WILD_READ_TRUNCATION_CHARS,
)


class _FakeOM:
    def _resolve_path(self, path, mode='ro'):
        return Path(path).resolve()


class _FakePool:
    """Minimal pool so tmp_path files resolve and ``llm_cfg`` can override the
    ``read_file_max_chars`` default for the disabled-counterfactual cases."""

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


def _mk(n, ch='y', pad=60):
    """n rendered lines, each ~70 chars, so 60 lines ≈ 4.2k, 40 lines ≈ 2.8k."""
    return '\n'.join(f"line {i} " + ch * pad for i in range(1, n + 1)) + '\n'


# Bodies sized against _read_text_file's ``NNN:\t`` rendering (~70 chars/line):
_OVER = _mk(70)   # ~4900 rendered -> exceeds the 4000 no-limit default
_UNDER = _mk(40)  # ~2800 rendered -> under 4000, stays intact
_HUGE = _mk(430)  # ~30k rendered -> exceeds the 25k global net / 4000 window


class TestReadFileCharTrip:
    def test_no_limit_read_over_default_truncates_whole_line(self):
        """A no-limit read whose body >4000 is cut to whole lines <4000 + [TRUNCATED],
        NOT the unbound-read warning, with a continue footer."""
        full = set(
            f"{i}:\tline {i} " + 'y' * 60 for i in range(1, 71)
        )
        out = _read(_OVER)  # no limit -> no-limit read
        assert out.startswith('OK:'), out
        assert '[TRUNCATED]' in out
        assert 'Unbound read detected!' not in out, 'the 4k default must not emit the unbound warning'
        assert '→ continue at start_line=' in out
        body = _body(out)
        assert len(body) < DEFAULT_READ_FILE_MAX_CHARS, f"body not cut: {len(body)}"
        # Whole-line boundary: every rendered line shown must exist intact in the source
        # (a chopped line would not). Proves the cut never splits a line mid-word.
        for ln in body.split('\n'):
            if ln:
                assert ln in full, f"chopped/absent line leaked: {ln!r}"
        assert 'line 1 ' in _OVER, 'sanity: source has the expected first line'

    def test_no_limit_read_under_default_is_intact(self):
        """A small no-limit read stays fully intact (EOF, no marker, no warning)."""
        out = _read(_UNDER)  # ~2800 < 4000
        assert out.startswith('OK:'), out
        assert '[TRUNCATED]' not in out
        assert 'Unbound read detected!' not in out
        assert '(EOF)' in out.split('\n')[0]
        assert 'line 40' in _body(out)
        assert len(_body(out)) < DEFAULT_READ_FILE_MAX_CHARS

    def test_bounded_read_not_char_truncated(self):
        """A BOUNDED read (explicit limit) whose body >4000 keeps its whole requested
        range — no char truncation, no marker. The range wins over the no-limit default."""
        out = _read(_OVER, limit=200)  # 70 lines, explicit generous limit -> bounded
        assert out.startswith('OK:'), out
        assert '[TRUNCATED]' not in out
        assert 'Unbound read detected!' not in out
        body = _body(out)
        assert len(body) > DEFAULT_READ_FILE_MAX_CHARS, f"bounded body should stay intact, got {len(body)}"
        assert 'line 70' in body, 'requested last line must be present'

    def test_wild_vs_bounded_counterfactual(self):
        """Same body: the NO-LIMIT read is cut to <4000 while the BOUNDED read is not —
        proves the is_wild_read gate on the char default is load-bearing."""
        wild = _read(_OVER)                # no limit -> cut
        bounded = _read(_OVER, limit=200)   # explicit limit -> intact
        assert '[TRUNCATED]' in wild and len(_body(wild)) < DEFAULT_READ_FILE_MAX_CHARS, wild
        assert '[TRUNCATED]' not in bounded and len(_body(bounded)) > DEFAULT_READ_FILE_MAX_CHARS, bounded

    def test_default_disabled_returns_full_body(self):
        """Counterfactual: with the default disabled (read_file_max_chars=-1) a ~4900
        body returns FULL (no marker, no warning, since it is under the 25k net)."""
        for kwargs in ({}, {'limit': 200}):
            out = _read(_OVER, cfg={'read_file_max_chars': -1}, **kwargs)
            assert out.startswith('OK:'), out
            assert '[TRUNCATED]' not in out
            assert 'Unbound read detected!' not in out
            body = _body(out)
            assert DEFAULT_READ_FILE_MAX_CHARS < len(body) < DEFAULT_TOOL_RESULT_MAX_CHARS, \
                f"expected full ~4900 body, got {len(body)}"
            assert 'line 70' in body, 'last line present when the default is disabled'

    def test_huge_file_default_cuts_to_window_not_warning(self):
        """A ~30k no-limit file under the DEFAULT 4k window is cut to whole lines <4000
        with [TRUNCATED] — the read_file default fires first, so no unbound warning."""
        out = _read(_HUGE)
        assert '[TRUNCATED]' in out
        assert 'Unbound read detected!' not in out
        assert len(_body(out)) < DEFAULT_READ_FILE_MAX_CHARS

    def test_huge_file_default_disabled_falls_to_net_warning(self):
        """With the 4k default disabled, a >25k no-limit read reaches the unbound-read
        WARNING path (HWM) and is cut to wild_truncation (4000)."""
        out = _read(_HUGE, cfg={'read_file_max_chars': -1})
        assert 'Unbound read detected!' in out, out
        assert len(_body(out)) < DEFAULT_WILD_READ_TRUNCATION_CHARS + 1000, len(_body(out))


class TestGlobalNet:
    def test_global_net_thresholds(self):
        """Global net constants are 25k trigger / 4k cut (single source of truth: settings)."""
        assert DEFAULT_TOOL_RESULT_MAX_CHARS == 25000
        assert DEFAULT_WILD_READ_TRUNCATION_CHARS == 4000

    def test_global_net_cuts_to_wild_window(self):
        """The global net's cut keeps ~4k (DEFAULT_WILD_READ_TRUNCATION_CHARS) and appends
        a notice. (The 25k TRIGGER gate lives in handler._assemble_tool_result, which calls
        this with char_threshold=25000/char_limit=4000 for any tool result over 25k.)"""
        text = 'x' * (DEFAULT_TOOL_RESULT_MAX_CHARS + 5000)  # >25k
        out = truncate_with_spillover(
            text,
            DEFAULT_WILD_READ_TRUNCATION_CHARS,
            instance_name='gn_test',
            tool_name='test',
            base_dir=Path(tempfile.mkdtemp()),
            skip_spillover=True,
        )
        assert out.startswith(text[:DEFAULT_WILD_READ_TRUNCATION_CHARS])
        assert len(out) < len(text), 'a >25k result must be cut'
        assert len(out) <= DEFAULT_WILD_READ_TRUNCATION_CHARS + 1000, len(out)
