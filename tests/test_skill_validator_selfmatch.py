"""Regression tests for BUG_0183: Tier-2 self-match zero-overlap is advisory, not a hard-reject.

Covers the 5 cases from the BUG_0183 fix plan:
  1. Domain-X skill, domain-Y task_text → registers (True) with an advisory warning.
  2. Skill shares vocabulary with task_text → (True, []) no warning.
  3. Coherent-domain skill with zero overlap → (True, [warning]) registers with advisory.
  4. Structural failure (missing required field) → (False, ...) Tier-1 unchanged.
  5. score < threshold with some overlap → (True, [advisory warning]) unchanged behavior.
"""

import sys
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_cascade.skills.validator import validate_skill


def _make_skill_content(
    name: str = 'test-skill',
    description: str = 'A skill for testing purposes with enough characters',
    triggers: list = None,
    body: str = None,
    source: str = 'auto-generated',
    generated_by: str = 'coder',
    generated_from_task: str = 'Write a test skill',
):
    """Build a valid SKILL.md content string."""
    if triggers is None:
        triggers = ['test', 'skill']
    if body is None:
        body = ('## Instructions\n\n'
                'Follow these steps carefully to complete the task. '
                'This body has enough characters to pass validation.\n\n'
                '1. Step one\n2. Step two\n3. Step three\n')
    fm = {
        'name': name,
        'description': description,
        'source': source,
        'triggers': triggers,
        'generated_by': generated_by,
        'generated_from_task': generated_from_task,
    }
    yaml_block = yaml.dump(fm, default_flow_style=False)
    return f"---\n{yaml_block}---\n\n{body}"


class TestSelfMatchZeroOverlapAdvisory:
    """BUG_0183: zero-overlap self-match is advisory (warning), not a hard-reject (error)."""

    def test_domain_x_skill_domain_y_task_registers(self):
        """Case 1: a domain-X skill proposed with a domain-Y task_text must register,
        not 0.000-reject. (This is the core BUG_0183 case.)"""
        content = _make_skill_content(
            name='docker-containers',
            description='Managing Docker containers and orchestration',
            triggers=['docker', 'container', 'orchestration'],
            generated_from_task='Deploy a Docker service',
        )
        passed, messages = validate_skill(
            content, 'docker-containers', set(),
            task_text='Write pytest unit tests for the parser module')
        assert passed, f"Domain-X skill in domain-Y context must register, got: {messages}"
        # The advisory warning must be present.
        assert any('shares NO vocabulary' in m for m in messages), \
            f"expected zero-overlap advisory warning, got: {messages}"
        assert any('advisory, non-blocking' in m for m in messages), \
            f"warning must be marked advisory/non-blocking, got: {messages}"

    def test_matching_skill_no_warning(self):
        """Case 2: a skill that shares vocabulary with the task_text passes cleanly."""
        content = _make_skill_content(
            name='pytest-testing',
            description='Writing pytest unit tests with fixtures and mocking',
            triggers=['pytest', 'unit test', 'fixtures'],
            generated_from_task='Write pytest unit tests',
        )
        passed, messages = validate_skill(
            content, 'pytest-testing', set(),
            task_text='Write pytest unit tests for the parser module')
        assert passed, f"Matching skill must pass, got: {messages}"
        # No self-match warning expected (score should be above threshold).
        assert not any('Self-match' in m for m in messages), \
            f"unexpected self-match warning for matching skill: {messages}"

    def test_zero_overlap_cohereent_domain_registers_with_warning(self):
        """Case 3: a coherent-domain skill with zero overlap registers with an advisory."""
        content = _make_skill_content(
            name='kubernetes-deploy',
            description='Deploying applications to Kubernetes clusters',
            triggers=['kubernetes', 'k8s', 'helm chart'],
            generated_from_task='Deploy a kubernetes service',
        )
        passed, messages = validate_skill(
            content, 'kubernetes-deploy', set(),
            task_text='Fix the SQL query parser regex')
        assert passed, f"Coherent zero-overlap skill must register, got: {messages}"
        assert any('shares NO vocabulary' in m for m in messages), \
            f"expected zero-overlap advisory, got: {messages}"

    def test_structural_failure_still_rejected(self):
        """Case 4: a structural (Tier-1) failure must still hard-reject."""
        # Missing the required 'description' field.
        fm = {
            'name': 'broken-skill',
            'source': 'auto-generated',
            'triggers': ['broken'],
        }
        yaml_block = yaml.dump(fm, default_flow_style=False)
        content = f"---\n{yaml_block}---\n\n## Body\nEnough text here.\n"
        passed, messages = validate_skill(content, 'broken-skill', set())
        assert not passed, f"Structural failure must reject, got: {messages}"

    def test_partial_overlap_advisory_unchanged(self):
        """Case 5: score < threshold with some overlap → advisory warning (unchanged)."""
        content = _make_skill_content(
            name='dedup-flag',
            description='Deliver push notifications once per run using an idempotent delivery flag',
            triggers=['dedup flag', 'push once per run', 'idempotent delivery'],
            generated_from_task='Implement dedup push flag',
        )
        task_text = ('Finish the Telegram bridge push-model implementation (todo.md:125): '
                     'engine/core.py pre-reflection push + run_agent_unified.py post-run push '
                     'deduped by instance._tg_pushed, via TelegramBridgeSupervisor.notify_user.')
        passed, messages = validate_skill(content, 'dedup-flag', set(), task_text=task_text)
        assert passed, f"Partial-overlap skill must pass with advisory, got: {messages}"
        advisory = [m for m in messages if 'advisory, non-blocking' in m]
        assert advisory, f"expected an advisory self-match warning, got: {messages}"
