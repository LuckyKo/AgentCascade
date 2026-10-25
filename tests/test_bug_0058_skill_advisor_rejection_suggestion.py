"""Regression tests for BUG_0058 — skill-advisor denials must carry an actionable suggestion.

Report: a DENY returned prose only ("handle it yourself"), the caller had to re-parse it,
and nothing structured distinguished "do it yourself" from "rephrase the task". Three
consecutive trivial delegations were rejected and retried by hand.

Fix:
  * the advisor contract gains ``[SUGGESTION] <action> — <hint>`` and
    ``[RETRY_TASK] <paste-ready task or none>`` (DENY only), parsed into
    ``SkillAdvisorResult.suggestion_action / suggestion_hint / retry_task``;
  * the action is a fixed vocabulary (``SUGGESTION_ACTIONS``) so callers can branch on it;
  * ``format_denial_message()`` renders the gate's denial with
    ``[SKILL-ADVISOR SUGGESTION] action=<token>``, the hint, the retry task, and the
    action's next-step instruction — while keeping ``[SKILL-ADVISOR DENIED]`` leading;
  * the compression prompt keeps one line per denial so the knowledge survives summarization.

No LLM, no Docker, no network.
"""

import pytest

from agent_cascade.prompts.dna import COMPRESSION_PROMPT
from agent_cascade.skills.advisor import (SUGGESTION_ACTIONS, SkillAdvisorResult, build_skill_advisor_prompt,
                                          format_denial_message, parse_advisor_output)


class FakeSkillManager:
    """Minimal advisor-facing surface (no ratings, one deterministic match)."""

    def __init__(self, names=('docker-best-practices', 'httpx-connection-pooling')):
        self._names = list(names)

    def _ensure_discovered(self):
        pass

    def get_skill_names(self):
        return list(self._names)

    def get_all_metadata(self, include_active_only=False):
        return [{'name': n, 'description': f"desc for {n}"} for n in self._names]

    def get_rating_average(self, name):
        return None

    def match_skills(self, query, include_inactive=False, cap=None):
        return []


def _parse(lines: str, skills=('docker-best-practices',)) -> SkillAdvisorResult:
    return parse_advisor_output(lines, FakeSkillManager(skills))


# ── Parsing the structured suggestion ────────────────────────────────────────

def test_deny_parses_action_hint_and_retry_task():
    out = ('[SKILLS] none\n'
           '[NOTES] none\n'
           '[SUGGESTION] handle_directly — Run grep + read_file yourself; no delegation needed\n'
           '[RETRY_TASK] none\n'
           '[VERDICT] DENY — task is trivially handleable by the parent')
    r = _parse(out)

    assert r.verdict == 'deny'
    assert r.suggestion_action == 'handle_directly'
    assert r.suggestion_hint == 'Run grep + read_file yourself; no delegation needed'
    assert r.retry_task == ''


def test_deny_parses_a_paste_ready_retry_task():
    out = ('[SUGGESTION] rephrase — the task is too vague to delegate\n'
           '[RETRY_TASK] Audit every docker-compose file under config/ for unpinned image tags and\n'
           'produce a table of file → service → tag.\n'
           '[VERDICT] DENY — insufficient scope')
    r = _parse(out)

    assert r.suggestion_action == 'rephrase'
    # Newlines from a wrapped advisor response collapse into ONE paste-ready line.
    assert r.retry_task == ('Audit every docker-compose file under config/ for unpinned image tags and '
                            'produce a table of file → service → tag.')


@pytest.mark.parametrize('line,expected_action,expected_hint', [
    ('[SUGGESTION] add_context: Include the failing test name', 'add_context', 'Include the failing test name'),
    ('[SUGGESTION] `split_task` | parse first, delegate the report', 'split_task', 'parse first, delegate the report'),
    ('[SUGGESTION] handle-directly - do it yourself', 'handle_directly', 'do it yourself'),
    ('[SUGGESTION] HANDLE_DIRECTLY', 'handle_directly', ''),
])
def test_suggestion_separator_and_case_tolerance(line, expected_action, expected_hint):
    r = _parse(f'{line}\n[VERDICT] DENY — trivial')
    assert r.suggestion_action == expected_action
    assert r.suggestion_hint == expected_hint


def test_unknown_action_degrades_to_unspecified_but_keeps_the_hint():
    """An unrecognized token must not swallow the prose: the hint still reaches the caller."""
    r = _parse('[SUGGESTION] maybe_delegate — try a bigger task\n[VERDICT] DENY — trivial')
    assert r.suggestion_action == 'unspecified'
    assert r.suggestion_hint == 'try a bigger task'


def test_explicit_none_action_means_do_not_retry():
    r = _parse('[SUGGESTION] none\n[VERDICT] DENY — redundant duplication of work in progress')
    assert r.suggestion_action == 'none'
    assert r.suggestion_hint == ''
    assert 'Do not retry this delegation.' in format_denial_message(r)


def test_missing_suggestion_markers_leave_fields_empty():
    r = _parse('[SKILLS] none\n[VERDICT] DENY — trivial')
    assert r.suggestion_action == ''
    assert r.suggestion_hint == ''
    assert r.retry_task == ''


def test_approve_ignores_suggestion_markers():
    """A suggestion only means something on DENY; APPROVE must not surface one."""
    r = _parse('[SKILLS] docker-best-practices\n'
               '[SUGGESTION] handle_directly — nonsense on approve\n'
               '[RETRY_TASK] nonsense\n'
               '[VERDICT] APPROVE — fine')
    assert r.verdict == 'approve'
    assert r.suggestion_action == ''
    assert r.retry_task == ''


def test_retry_task_none_variants_are_empty():
    for value in ('none', 'NONE', 'n/a', '-'):
        r = _parse(f'[SUGGESTION] rephrase — x\n[RETRY_TASK] {value}\n[VERDICT] DENY — x')
        assert r.retry_task == '', value


def test_action_vocabulary_is_the_documented_one():
    assert SUGGESTION_ACTIONS == ('handle_directly', 'rephrase', 'add_context', 'split_task', 'none')


# ── The caller-facing denial payload ─────────────────────────────────────────

def test_denial_payload_is_machine_actionable_and_keeps_the_leading_token():
    r = _parse('[SUGGESTION] add_context — include the target file and expected output\n'
               '[RETRY_TASK] Review engine/tool_execution.py for unbound locals and report each site.\n'
               '[VERDICT] DENY — context is too thin to delegate')
    content = format_denial_message(r)
    lines = content.split('\n')

    assert lines[0].startswith('[SKILL-ADVISOR DENIED] ')
    assert 'context is too thin to delegate' in lines[0]
    assert lines[1] == '[SKILL-ADVISOR SUGGESTION] action=add_context'
    assert 'hint: include the target file and expected output' in lines
    assert 'retry_task: Review engine/tool_execution.py for unbound locals and report each site.' in lines
    assert lines[-1] == 'Retry call_agent with the missing context included.'


def test_denial_payload_for_unspecified_keeps_the_old_guidance():
    """No advisor suggestion → the pre-fix next-step prose, plus an explicit unknown token."""
    r = SkillAdvisorResult(verdict='deny', reason='trivial task')
    content = format_denial_message(r)

    assert '[SKILL-ADVISOR SUGGESTION] action=unspecified' in content
    assert 'hint:' not in content
    assert 'retry_task:' not in content
    assert content.endswith('Consider handling this task yourself or rephrasing with more specific context.')


def test_denial_payload_never_looks_like_a_system_error():
    """child_runner raises ChildAgentFailedError when the child text starts with [SYSTEM ERROR."""
    r = SkillAdvisorResult(verdict='deny', reason='trivial')
    assert not format_denial_message(r).strip().startswith('[SYSTEM ERROR')


# ── Prompt contract ──────────────────────────────────────────────────────────

def test_advisor_prompt_requests_the_new_markers_and_vocabulary():
    prompt = build_skill_advisor_prompt(FakeSkillManager(), 'Do x', 'ctx', 'coder', 'Maine')

    assert '[SUGGESTION]' in prompt
    assert '[RETRY_TASK]' in prompt
    for action in SUGGESTION_ACTIONS:
        assert action in prompt


def test_compression_prompt_preserves_denials_across_summarization():
    """BUG_0058 Option B: the summary (the compression-surviving context) must keep denials."""
    assert '[SKILL-ADVISOR DENIED]' in COMPRESSION_PROMPT
    assert 'not retried' in COMPRESSION_PROMPT
