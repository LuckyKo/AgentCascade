#!/usr/bin/env python3
"""BUG_0394 — security-layer / prompt-layer pins for the delete_and_insert EOF append.

The BUG_0394 false reject comes from the LLM Security advisor (not the in-tree code
guard): it sees only the raw ``range_param="0"`` string and misreads it as a whole-file
wipe. The fix has two prompt/surface layers, pinned here (the behaviour of an LLM cannot
be unit-tested, but what we can pin is that the advisor is *given* the correct facts and
*instructed* to name only real parameters):

  * test_security_prompt_carries_empty_span_note — mirrors security_handler.py:642's
    ``prompt_template.format(...)`` and asserts the enriched description's empty-span
    marker survives into the prompt the advisor receives.
  * test_advisor_prompt_pins_real_parameter_rule — pins the SECURITY_ADVISOR_PROMPT
    edit_file guidance: name only real parameters and only valid match_mode values.
  * test_schema_range_clause_is_consistent — pins the dna.py ``edit_file.range`` schema
    tightening: EOF append is ``'0'`` (or ``'0:'``); a single line number is INSERT-BEFORE.

Run: python -m pytest tests/test_edit_file_di_advisory_span.py -q -p no:cacheprovider -o addopts=""
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.operation_manager.file_operations import (  # noqa: E402
    _di_span_summary,
    _edit_parse_range,
)
from agent_cascade.prompts.dna import (  # noqa: E402
    SECURITY_ADVISOR_PROMPT,
    TOOL_METADATA,
)


# ── §b + security_handler.py:642 — the description reaches the advisor's prompt ──


def _di_edit_description(path, range_param, total_lines):
    """Rebuild the edit_file approval description exactly as file_operations.edit_file
    does for delete_and_insert (parse the range, then enrich with the span summary)."""
    start_idx, end_idx = _edit_parse_range(range_param, total_lines)
    span = _di_span_summary(start_idx, end_idx, total_lines)
    return f"Surgical edit to: {path} (mode: delete_and_insert) — {span}"


def test_security_prompt_carries_empty_span_note():
    """The assembled advisor prompt for an edit_file d&i range '0' append must contain
    the empty-span classification. Mirrors the prompt-format step at
    security_handler.py:642-648 (same template, same slot kwargs) — deterministic, no LLM."""
    assert '{description}' in SECURITY_ADVISOR_PROMPT, \
        'advisor template must keep the {description} slot the span note rides through'
    path = '/w/reports/doc.md'
    description = _di_edit_description(path, '0', total_lines=25)
    prompt = SECURITY_ADVISOR_PROMPT.format(
        tool_name='edit_file',
        description=description,
        arguments=json.dumps({
            'path': path,
            'match_mode': 'delete_and_insert',
            'range_param': '0',
            'new_content': 'APPEND_A\n',
        }),
        os_info='Linux 5.0',
        workspace_info='Main workspace: /w\n',
    )
    assert 'delete span EMPTY' in prompt, \
        f"advisor prompt must state the empty delete span: {prompt!r}"
    assert 'removes NOTHING' in prompt, \
        f"advisor prompt must state nothing is removed: {prompt!r}"
    # The raw '0' is present too, but the span note must override the misread.
    assert '"range_param": "0"' in prompt, 'raw range must still be visible to the advisor'


# ── §d(3) — SECURITY_ADVISOR_PROMPT rulebook: name only real parameters / valid modes ──


def test_advisor_prompt_pins_real_parameter_rule():
    """The edit_file guidance block must name only real parameters and only valid
    match_mode values, and warn against the invented tokens from BUG_0191."""
    assert 'ONLY real parameters' in SECURITY_ADVISOR_PROMPT, \
        'advisor must be told to name only real edit_file parameters'
    assert 'ONLY valid match_mode values' in SECURITY_ADVISOR_PROMPT, \
        'advisor must be told to name only valid match_mode values'
    # All four valid match_mode values spelled out (so a bad one is recognizable as wrong).
    for mode in ('exact', 'heuristic', 'heuristic_agnostic', 'delete_and_insert'):
        assert mode in SECURITY_ADVISOR_PROMPT, f"valid match_mode {mode!r} must be listed"
    # The bogus tokens the model invented must be explicitly called out as non-existent.
    for bogus in ('"mode:"', '"append"', '"range_param"'):
        assert bogus in SECURITY_ADVISOR_PROMPT, \
            f"advisor rule must name invented token {bogus!r} as invalid"


# ── §(e) — edit_file.range schema text is internally consistent about EOF append ──


def test_schema_range_clause_is_consistent():
    """The range schema must keep the correct EOF-append clause and must NOT claim a
    single line number is an EOF append (a single number is INSERT-BEFORE)."""
    rng = TOOL_METADATA['edit_file']['parameters']['range']
    # The capability the code already supports is still advertised.
    assert "Use '0' to append at end of file." in rng, \
        f"EOF-append capability must remain in schema: {rng!r}"
    # The corrected, consistent clause.
    assert "to append at EOF use '0' (or '0:')" in rng, \
        f"tightened EOF-append clause missing: {rng!r}"
    assert 'a single line number inserts before that line, not at EOF' in rng, \
        f"single-line-number INSERT-BEFORE clarification missing: {rng!r}"
    # The old, internally-inconsistent clause must be gone.
    assert "to append use '0' or a single line number" not in rng, \
        f"inconsistent single-line-number-append clause must be removed: {rng!r}"
