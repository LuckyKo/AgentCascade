"""BUG_0232 regression tests: the grep fast-path header must report the
PRE-TRUNCATION match count, not the post-truncation inline count.

Incident: grepping a directory of multi-KB single-line ``.jsonl`` records
returned "Found 0 matches for 'X' ... [TRUNCATED — showing 0 of 7 lines ...]"
and pointed the caller at a spillover file that did contain all 7 matches. The
header read ``count`` (tuple element 1), which ``_finish`` recomputes from the
surviving *inline* entries, so when the byte budget is exhausted before any
line fits it collapses to 0. The header now reads the new element-10
``total_matches_before``; element 1 stays the post-truncation count (tests and
the ``>>>``-legend decision depend on that meaning). See
``plans/grep-0232-header-count.md`` and [[grep-header-count-semantics-fast-path-vs-fallback]].
"""

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager import grep as grep_mod


def _make_om(root: Path) -> OperationManager:
    return OperationManager(base_dir=str(root))


def _match_event(path: str, line: int, text: str) -> str:
    """One ripgrep ``--json`` match event, matching the shape ``_parse_rg_json`` accepts."""
    return json.dumps({'type': 'match', 'data': {
        'path': {'text': path}, 'line_number': line, 'lines': {'text': text + '\n'}}})


def _fake_run(stdout_text: str, rc: int = 0):
    """A ``subprocess.run`` replacement returning fixed rg-JSON stdout (no real rg).

    Closure names (``stdout_text``/``rc``) are intentionally distinct from the fake result's
    attribute names (``stdout``/``returncode``): a class body cannot read a closure variable
    that shares its own attribute name, which would raise NameError.
    """
    def f(cmd, *args, **kwargs):
        class _R:
            returncode = rc
            stdout = stdout_text
            stderr = ''
        return _R()
    return f


def _force_fast_path():
    """Patch tool availability so ``_try_subprocess_grep`` runs (else it falls back to Python)."""
    return patch.object(grep_mod, '_check_tool_availability', return_value=(True, False))


# 7 entries of ~410 chars (GREP_MAX_LINE_CHARS default 500 ⇒ not abbreviated). With
# char_limit=200 the byte budget breaks on the first line, so NOTHING shows inline —
# the exact all-spilled case where the buggy header printed "Found 0 matches".
_HUGE_EVENTS = '\n'.join(_match_event(f'f{i}.txt', 1, 'x' * 400) for i in range(7)) + '\n'


def test_all_spilled_header_is_total_not_zero(tmp_path):
    om = _make_om(tmp_path)
    with _force_fast_path(), patch('subprocess.run', side_effect=_fake_run(_HUGE_EVENTS)):
        out = om.grep('x', '.', char_limit=200)

    # Headline reports the full pre-truncation match count, never the collapsed 0.
    assert out.startswith('Found 7 matches'), out
    assert 'Found 0' not in out, out
    # The (correct) notice still reports the line counts, and the pointer is in the body.
    assert 'showing 0 of 7 lines' in out, out
    assert 'Full output saved to:' in out, out
    assert 'were larger than the inline budget' in out, out

    # Ticket verification: the header number equals the count_only total for the same
    # search (the two views of "how many matches" must agree).
    with _force_fast_path(), patch('subprocess.run', side_effect=_fake_run(_HUGE_EVENTS)):
        out2 = om.grep('x', '.', char_limit=200, count_only=True)
    assert '[total: 7 matching lines in 7 file(s)]' in out2, out2
    # The two independent views of "how many matches" must agree (ticket §Verification):
    # the rendered header count and the count_only total are parsed separately and compared.
    m_hdr = re.search(r'Found (\d+) matches', out)
    m_cnt = re.search(r'\[total: (\d+) matching lines', out2)
    assert m_hdr and m_cnt, f"{out}\n{out2}"
    assert int(m_hdr.group(1)) == int(m_cnt.group(1)) == 7, f"{out}\n{out2}"


def test_partial_truncation_header_is_total_not_shown(tmp_path):
    om = _make_om(tmp_path)
    # 10 short matches; char_limit=80 lets ~5 fit ⇒ shown < total, header must stay 10.
    events = '\n'.join(_match_event(f'f{i}.txt', 1, f'hit{i}') for i in range(10)) + '\n'
    with _force_fast_path(), patch('subprocess.run', side_effect=_fake_run(events)):
        out = om.grep('hit', '.', char_limit=80)

    assert out.startswith('Found 10 matches'), out
    assert 'Found 5 matches for' not in out, out  # header is the total, not the shown count
    assert 'showing 5 of 10 lines' in out, out
    # A partially-shown result keeps the generic end-of-body marker (the all-spilled
    # branch must NOT fire here; this pins the ``_sub_shown_lines == 0`` guard).
    assert 'more line(s) omitted' in out, out


def test_legitimate_zero_match_stays_zero(tmp_path):
    """Control against over-correction: a genuine no-match (rc=1) must stay byte-identical."""
    om = _make_om(tmp_path)
    with _force_fast_path(), patch('subprocess.run', side_effect=_fake_run('', rc=1)):
        out = om.grep('nomatch', '.')

    assert out.startswith('No matches found'), out
    assert 'Found 0 matches' not in out, out


def test_timeout_partial_header_is_partial_count_not_zero(tmp_path):
    """A killed rg with inline partials shows the (lower-bound) match count + INCOMPLETE, never 0.

    Deliberately sized so SOME lines fit (results non-empty): a timeout whose every line
    spilled was, pre-BUG_0243, a distinct case that fell through to the ``elif was_timed_out``
    "too broad" ERROR and never reached the header. BUG_0243 fixed that (now routed to the same
    header via ``_sub_spill_rel`` — see
    test_timeout_all_spilled_reports_partial_count_not_too_broad). This test pins the
    PARTIAL-inline shape and its assertions must not be weakened.
    """
    om = _make_om(tmp_path)
    # 7 matches ~100 chars each; char_limit=360 ⇒ ~3 fit inline, 7 total partial matches.
    events = '\n'.join(_match_event(f'f{i}.txt', 1, 'y' * 90) for i in range(7)) + '\n'

    def fake_timeout(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5.0, output=events)

    with _force_fast_path(), patch('subprocess.run', side_effect=fake_timeout):
        out = om.grep('y', '.', char_limit=360, timeout=5.0)

    assert out.startswith('Found 7 matches'), out
    assert 'search INCOMPLETE' in out, out
    assert 'showing 3 of 7 lines' in out, out
    assert 'Found 0' not in out, out


def test_timeout_all_spilled_reports_partial_count_not_too_broad(tmp_path):
    """BUG_0243: a killed rg whose every match spills must report the partial count
    with the search-INCOMPLETE marker + spillover headline, never the "too broad" ERROR."""
    om = _make_om(tmp_path)

    def fake_timeout(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5.0, output=_HUGE_EVENTS)

    with _force_fast_path(), patch('subprocess.run', side_effect=fake_timeout):
        out = om.grep('x', '.', char_limit=200, timeout=5.0)

    assert out.startswith('Found 7 matches'), out           # partial count, not 0
    assert 'too broad' not in out, out                      # THE discriminating assertion
    assert 'search INCOMPLETE' in out, out                  # lower-bound semantics
    assert 'TIMED OUT after 5s' in out, out
    assert 'showing 0 of 7 lines' in out, out               # all spilled (inline shows none)
    assert 'Full output saved to:' in out, out              # BUG_0232 spillover notice
    assert 'were larger than the inline budget' in out, out  # BUG_0232 all-spilled headline
    assert 'Found 0' not in out, out


def test_all_spilled_writes_spillover_file(tmp_path):
    """The body's 'read the spillover file' pointer must be actionable (file exists, full)."""
    om = _make_om(tmp_path)
    with _force_fast_path(), patch('subprocess.run', side_effect=_fake_run(_HUGE_EVENTS)):
        out = om.grep('x', '.', char_limit=200)

    m = re.search(r'Full output saved to: (.*?)\]', out)
    assert m, out
    spill_path = tmp_path / m.group(1)
    assert spill_path.exists(), f"referenced spillover missing: {spill_path}"
    content = spill_path.read_text(encoding='utf-8')
    # The spillover holds the FULL (untruncated) output — every huge match is present.
    assert 'x' * 400 in content, content[:200]
