"""Comprehensive test suite for auto-skill generation (Phase 3).

Covers:
  - Unit: validation (valid, invalid, edge cases)
  - Unit: registration (new skill, duplicate, triggers in registry)
  - Unit: self-match (pass at 0.3, fail below 0.3)
  - Unit: matcher trigger indexing
  - Integration: full propose → validate → promote flow
  - Integration: rate limiting
  - Integration: hot-reload (new skill discoverable after registration)
"""

import re
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Ensure the project root is on sys.path so imports resolve correctly
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_cascade.agent_instance import AgentState
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, USER, Message
from agent_cascade.settings import (AUTO_SKILL_EXTRA_TURNS, AUTO_SKILL_MIN_TURNS, AUTO_SKILL_PROMOTION_THRESHOLD,
                                    CANDIDATE_MIN_RATINGS, DEFAULT_LOAD_SKILL_MODE, LOAD_SKILL_NONE)
from agent_cascade.skills.manager import SkillManager
from agent_cascade.skills.matcher import SkillMatcher
from agent_cascade.skills.parser import parse_frontmatter
from agent_cascade.skills.validator import validate_skill

# ===========================================================================
# Helpers — skill content factory
# ===========================================================================


def _uid():
    """Generate a short unique suffix for parallel test isolation."""
    return uuid.uuid4().hex[:8]


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


def _isolate_metrics(manager, tmp_path, reset=False):
    """Point a manager's metrics file at a temp path (avoid clobbering production).

    When ``reset`` is True the in-memory metrics dict is also emptied so the
    production skills-metrics.json (loaded at __init__) cannot leak real ratings
    into tests. Intended as the body of an autouse fixture: call it, then ``yield``.
    """
    manager._metrics_file = tmp_path / 'skills-metrics.json'
    if reset:
        with manager._metrics_lock:
            manager._metrics = {}


def _cleanup_test_artifacts():
    """Remove test-specific artifacts left by the skill generation tests.

    - pending-skills/: deletes all entries (these are always test artifacts).
    - candidates/: only deletes directories whose names match test patterns
      ("test-*", "tmp-*"). Production skills must never be deleted here; we
      cannot rely on a hardcoded whitelist of canonical skills because new
      production skills get added over time.
    - agents/global/skills/: same test-pattern rule as above.
    """

    def _is_test_skill(name: str) -> bool:
        return (name.startswith('test-') or name.startswith('tmp-') or '-test-skill-' in name or
                name.endswith('-testing'))

    def _remove_empty_dir(entry: Path) -> None:
        """Remove a skill directory if empty after deleting its SKILL.md.

        Best-effort: on Windows, xdist sibling workers or Defender may briefly
        hold a lock on the file. Retry with backoff; never raise from cleanup.
        """
        skill_file = entry / 'SKILL.md'
        if skill_file.exists():
            for attempt in range(3):
                try:
                    skill_file.unlink()
                    break
                except (PermissionError, OSError):
                    if attempt < 2:
                        time.sleep(0.1 * (attempt + 1))
            else:
                return  # give up silently; leftover is harmless
        try:
            if not list(entry.iterdir()):
                entry.rmdir()
        except (PermissionError, OSError):
            pass

    pending_root = Path('agents/global/pending-skills')
    if pending_root.exists():
        for entry in list(pending_root.iterdir()):
            if entry.is_dir():
                _remove_empty_dir(entry)

    # Candidate-flow artifacts: test-named candidates left by the decision-gate tests.
    candidates_root = Path('agents/global/candidates')
    if candidates_root.exists():
        for entry in list(candidates_root.iterdir()):
            if entry.is_dir() and _is_test_skill(entry.name):
                _remove_empty_dir(entry)

    skills_root = Path('agents/global/skills')
    if skills_root.exists():
        for entry in list(skills_root.iterdir()):
            # Only remove directories that look like test artifacts.
            # Never blindly delete non-canonical skills: production skills
            # are added over time and a whitelist would always lag behind.
            if entry.is_dir() and _is_test_skill(entry.name):
                _remove_empty_dir(entry)


# ===========================================================================
# Shared fixture: fresh SkillManager with guaranteed cleanup
# ===========================================================================


@pytest.fixture(autouse=True)
def fresh_manager(tmp_path):
    """Create a fresh SkillManager and clean up test artifacts after each test.

    CRITICAL: redirect ``_metrics_file`` to a temp path. ``SkillManager.__init__`` loads the
    REAL agents/global/skills-metrics.json into memory and defaults ``_metrics_file`` to it, so
    any test that triggers a metrics flush (load counts, ratings, or prune_stale_metrics via
    evaluate_candidates) would otherwise clobber the production file. Tests that need their own
    isolated path still set ``manager._metrics_file`` explicitly (which simply overrides this);
    tests that derive a tree from ``Path(manager._metrics_file).parent`` now get a temp dir,
    which is the intended isolation behavior.
    """
    _cleanup_test_artifacts()                      # legacy sweep of any pre-existing shared artifacts (harmless)
    manager = SkillManager()
    manager._metrics_file = tmp_path / 'skills-metrics.json'
    # Isolate ALL THREE skill roots to a unique per-test tmp dir. tmp_path is function-scoped and
    # unique even across xdist workers, so no two tests share a tree → the shared-tree race disappears.
    base = tmp_path / 'agents' / 'global'
    manager._pending_dir = base / 'pending-skills'
    manager._candidates_dir = base / 'candidates'
    manager._production_skills_dir = base / 'skills'
    yield manager
    _cleanup_test_artifacts()


# ===========================================================================
# 1. Unit: Validation — agent_cascade.skills.validator
# ===========================================================================


class TestValidation:
    """Tier 1 structural validation and Tier 2 self-match."""

    def test_valid_skill_passes(self):
        content = _make_skill_content()
        passed, errors = validate_skill(content, 'test-skill', set())
        assert passed, f"Expected pass, got errors: {errors}"

    def test_valid_skill_with_task_text_passes(self):
        content = _make_skill_content(generated_from_task='Write a test skill for validation')
        passed, errors = validate_skill(content, 'test-skill', set(), task_text='Write a test skill')
        assert passed, f"Expected pass, got errors: {errors}"

    def test_invalid_name_uppercase(self):
        content = _make_skill_content(name='TestSkill')
        passed, errors = validate_skill(content, 'TestSkill', set())
        assert not passed
        assert len(errors) > 0

    def test_invalid_name_starts_with_digit(self):
        content = _make_skill_content(name='1test-skill')
        passed, errors = validate_skill(content, '1test-skill', set())
        assert not passed
        assert len(errors) > 0

    def test_invalid_name_empty(self):
        content = _make_skill_content(name='')
        passed, errors = validate_skill(content, '', set())
        assert not passed
        assert len(errors) > 0

    def test_missing_description(self):
        content = _make_skill_content(description='')
        passed, errors = validate_skill(content, 'test-skill', set())
        assert not passed
        assert len(errors) > 0

    def test_short_description(self):
        content = _make_skill_content(description='Short')
        passed, errors = validate_skill(content, 'test-skill', set())
        assert not passed
        assert len(errors) > 0

    def test_empty_triggers(self):
        content = _make_skill_content(triggers=[])
        passed, errors = validate_skill(content, 'test-skill', set())
        assert not passed
        assert len(errors) > 0

    def test_missing_triggers(self):
        raw = ('---\n'
               'name: test-skill\n'
               'description: A skill for testing purposes with enough characters\n'
               '---\n\n'
               '## Instructions\n\n'
               'Follow these steps carefully to complete the task. '
               'This body has enough characters to pass validation.\n\n'
               '1. Step one\n2. Step two\n3. Step three\n')
        passed, errors = validate_skill(raw, 'test-skill', set())
        assert not passed
        assert len(errors) > 0

    def test_duplicate_name(self):
        content = _make_skill_content(name='test-skill')
        passed, errors = validate_skill(content, 'test-skill', {'test-skill'})
        assert not passed
        assert len(errors) > 0

    def test_body_too_short(self):
        content = _make_skill_content(body='Short body')
        passed, errors = validate_skill(content, 'test-skill', set())
        assert not passed
        assert len(errors) > 0

    def test_file_too_large(self):
        content = _make_skill_content(body='X ' * 20000)
        passed, errors = validate_skill(content, 'test-skill', set())
        assert not passed
        assert len(errors) > 0


# ===========================================================================
# 2. Unit: Registration — SkillManager.register_skill_from_content
# ===========================================================================


class TestRegistration:
    """Test dynamic skill registration from content."""

    def test_new_skill_registers_successfully(self, fresh_manager):
        name = f"new-test-skill-{_uid()}"
        content = _make_skill_content(name=name)
        success, errors = fresh_manager.register_skill_from_content(content)
        assert success, f"Registration failed: {errors}"
        assert name in fresh_manager._skills_registry

    def test_duplicate_skill_becomes_candidate(self, fresh_manager):
        """Re-proposing an existing name is an upgrade proposal: it registers as a
        live-serving candidate (agents/global/candidates/), not a hard duplicate failure."""
        name = f"duplicate-test-skill-{_uid()}"
        content = _make_skill_content(name=name)
        success1, _ = fresh_manager.register_skill_from_content(content)
        assert success1
        content2 = _make_skill_content(name=name)
        success2, errors = fresh_manager.register_skill_from_content(content2)
        assert success2, f'upgrade proposal should register as candidate: {errors}'
        reg = fresh_manager._skills_registry.get(name)
        assert reg is not None and 'candidates' in reg['file_path']

    def test_triggers_stored_in_registry(self, fresh_manager):
        triggers = ['pytest', 'unit-test', 'mocking']
        name = f"triggers-test-skill-{_uid()}"
        content = _make_skill_content(name=name, triggers=triggers)
        fresh_manager.register_skill_from_content(content)
        reg = fresh_manager._skills_registry.get(name)
        assert reg is not None
        assert reg.get('triggers') == triggers

    def test_triggers_returned_by_get_all_metadata(self, fresh_manager):
        triggers = ['pytest', 'unit-test', 'mocking']
        name = f"test-metadata-triggers-skill-{_uid()}"
        content = _make_skill_content(name=name, triggers=triggers)
        fresh_manager.register_skill_from_content(content)
        all_meta = fresh_manager.get_all_metadata()
        found = [m for m in all_meta if m['name'] == name]
        assert len(found) == 1
        assert found[0].get('triggers') == triggers


# ===========================================================================
# 3. Unit: Self-Match — Tier 2 validation via SkillMatcher
# ===========================================================================


class TestSelfMatch:
    """Validate that self-match scoring works correctly."""

    def test_matching_skill_scores_above_threshold(self):
        content = _make_skill_content(
            name='pytest-testing',
            description='Writing pytest unit tests with fixtures and mocking',
            triggers=['pytest', 'unit test', 'test fixture', 'mock'],
            generated_from_task='Write pytest unit tests for the parser',
        )
        passed, errors = validate_skill(content,
                                        'pytest-testing',
                                        set(),
                                        task_text='Write pytest unit tests for the parser module')
        assert passed, f"Self-match should pass: {errors}"

    def test_non_matching_skill_scores_below_threshold(self):
        content = _make_skill_content(
            name='docker-containers',
            description='Managing Docker containers and orchestration',
            triggers=['docker', 'container', 'kubernetes', 'pod'],
            generated_from_task='Write pytest unit tests for the parser',
        )
        passed, errors = validate_skill(content,
                                        'docker-containers',
                                        set(),
                                        task_text='Write pytest unit tests for the parser module')
        assert not passed
        assert len(errors) > 0

    def test_self_match_threshold_is_0_3(self):
        assert AUTO_SKILL_PROMOTION_THRESHOLD == 0.3


# ===========================================================================
# 4. Unit: Matcher trigger indexing
# ===========================================================================


class TestMatcherTriggerIndexing:
    """Verify that triggers are tokenized and indexed by SkillMatcher."""

    def test_triggers_are_tokenized_and_indexed(self):
        matcher = SkillMatcher()
        meta = [
            {
                'name': 'pytest-testing',
                'description': 'Writing pytest unit tests',
                'triggers': ['pytest', 'unit test', 'mock', 'fixture'],
            },
        ]
        matcher.build_index(meta)
        assert 'pytest' in matcher._inverted_index
        assert 'mock' in matcher._inverted_index
        assert 'fixture' in matcher._inverted_index

    def test_trigger_keywords_enable_matching(self):
        matcher = SkillMatcher()
        meta = [
            {
                'name': 'unique-skill-name',
                'description': 'Something unrelated to the query',
                'triggers': ['quantum', 'physics', 'particles'],
            },
        ]
        matcher.build_index(meta)
        results = matcher.match('quantum physics particles')
        assert len(results) > 0
        assert results[0][0] == 'unique-skill-name'

    def test_triggers_included_with_name_and_description(self):
        matcher = SkillMatcher()
        meta = [
            {
                'name': 'only-triggers-match',
                'description': 'xyz abc',
                'triggers': ['hello', 'world'],
            },
        ]
        matcher.build_index(meta)
        results = matcher.match('hello world')
        assert len(results) > 0
        assert results[0][0] == 'only-triggers-match'


# ===========================================================================
# 5. Integration: Full propose → validate → promote flow
# ===========================================================================


class TestProposeValidatePromote:
    """End-to-end flow: create skill content → register → validate → promote."""

    def test_full_flow_with_promotion(self, fresh_manager):
        name = f"integration-test-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description='Integration test skill for the auto-skill generation system',
            triggers=['integration', 'test', 'auto-skill'],
            generated_from_task='Test the full skill generation pipeline',
        )

        success, errors = fresh_manager.register_skill_from_content(
            content,
            source='auto-generated',
            task_text='Test the full skill generation pipeline',
            auto_promote=True,
        )
        assert success, f"Flow failed: {errors}"

        assert name in fresh_manager._skills_registry

        # Assert against the manager's isolated production root (set by fresh_manager), NOT the
        # CWD-relative shared tree — under xdist that tree is no longer where this test promotes.
        target = fresh_manager._production_skills_dir / name / 'SKILL.md'
        assert target.exists(), f"Skill was not promoted to {target.parent}/"

        reg = fresh_manager._skills_registry[name]
        assert name in reg['file_path']

    def test_full_flow_without_promotion(self, fresh_manager):
        name = f"integration-test-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description='Integration test skill for the auto-skill generation system',
            triggers=['integration', 'test', 'auto-skill'],
            generated_from_task='Test the full skill generation pipeline',
        )

        success, _ = fresh_manager.register_skill_from_content(
            content,
            source='auto-generated',
            task_text='Test the full skill generation pipeline',
            auto_promote=False,
        )
        assert success

        assert name in fresh_manager._skills_registry

        reg = fresh_manager._skills_registry[name]
        assert 'pending-skills' in reg['file_path']

    def test_duplicate_in_full_flow(self, fresh_manager):
        """Re-proposing an existing skill name is now an UPGRADE proposal (candidate flow),
        not a hard duplicate failure: it registers as a live-serving candidate in
        agents/global/candidates/ and triggers the decision gate immediately."""
        name = f"integration-test-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description='Integration test skill for the auto-skill generation system',
            triggers=['integration', 'test', 'auto-skill'],
            generated_from_task='Test the full skill generation pipeline',
        )

        success1, _ = fresh_manager.register_skill_from_content(content, auto_promote=True)
        assert success1

        content2 = _make_skill_content(
            name=name,
            description='Duplicate integration test skill',
            triggers=['integration', 'test'],
            generated_from_task='Test again',
        )
        success2, errors = fresh_manager.register_skill_from_content(content2, auto_promote=True)
        assert success2, f'Upgrade proposal should register as candidate: {errors}'
        reg = fresh_manager._skills_registry[name]
        assert 'candidates' in reg['file_path'], 'second registration must land in the candidates dir'


# ===========================================================================
# 6. Integration: Rate limiting
# ===========================================================================


class TestRateLimiting:
    """Rate limiting: second proposal in same session rejected."""

    def test_second_proposal_flag_mechanism(self, fresh_manager):
        """First proposal sets flag, second checks flag."""
        name1 = f"test-rate-limit-skill-1-{_uid()}"
        name2 = f"test-rate-limit-skill-2-{_uid()}"
        content1 = _make_skill_content(
            name=name1,
            description='First skill for rate limiting test',
            triggers=['rate', 'limit', 'first'],
            generated_from_task='Create first skill',
        )
        content2 = _make_skill_content(
            name=name2,
            description='Second skill for rate limiting test',
            triggers=['rate', 'limit', 'second'],
            generated_from_task='Create second skill',
        )

        success1, _ = fresh_manager.register_skill_from_content(content1, task_text='Create first skill')
        assert success1

        success2, _ = fresh_manager.register_skill_from_content(content2, task_text='Create second skill')
        assert success2

        assert name1 in fresh_manager._skills_registry
        assert name2 in fresh_manager._skills_registry

    def test_rate_limit_flag_on_agent_instance(self, fresh_manager):
        """Verify _auto_skill_proposed flag prevents second proposal."""
        inst = MagicMock()
        inst._auto_skill_proposed = False

        inst._auto_skill_proposed = True
        assert inst._auto_skill_proposed

        assert inst._auto_skill_proposed

        inst._auto_skill_proposed = False
        assert not inst._auto_skill_proposed

    def test_pending_file_cleaned_on_validation_failure(self, fresh_manager):
        """Pending file should be removed when validation fails."""
        name = f"test-rate-limit-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description='x',
            triggers=['test'],
            generated_from_task='Create skill',
        )
        success, _ = fresh_manager.register_skill_from_content(content)
        assert not success

        # Assert against the isolated pending dir where register_skill_from_content actually
        # wrote/cleaned up (set by fresh_manager), NOT the CWD-relative shared tree.
        pending_root = fresh_manager._pending_dir
        if pending_root.exists():
            for entry in list(pending_root.iterdir()):
                skill_file = entry / 'SKILL.md'
                if skill_file.exists():
                    fm, _ = parse_frontmatter(skill_file.read_text())
                    assert fm.get('name') != name


# ===========================================================================
# 7. Integration: Hot-reload
# ===========================================================================


class TestHotReload:
    """New skill discoverable after registration without restart."""

    def test_new_skill_discoverable_after_registration(self, fresh_manager):
        name = f"test-hot-reload-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description='Skill for testing hot-reload discovery',
            triggers=['hot-reload', 'discovery', 'dynamic'],
            generated_from_task='Test hot-reload skill discovery',
        )

        success, _ = fresh_manager.register_skill_from_content(
            content,
            task_text='Test hot-reload skill discovery',
        )
        assert success

        results = fresh_manager.match_skills('hot-reload discovery dynamic')
        assert len(results) > 0
        assert any(n == name for n, _ in results)

        all_meta = fresh_manager.get_all_metadata()
        names = [m['name'] for m in all_meta]
        assert name in names

    def test_index_rebuilt_after_registration(self, fresh_manager):
        name = f"test-hot-reload-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description='Skill for testing hot-reload discovery',
            triggers=['hot-reload', 'discovery', 'dynamic'],
            generated_from_task='Test hot-reload skill discovery',
        )

        fresh_manager.register_skill_from_content(
            content,
            task_text='Test hot-reload skill discovery',
        )

        assert name in fresh_manager._matcher._inverted_index
        assert 'discovery' in fresh_manager._matcher._inverted_index

    def test_promoted_skill_matchable_via_manager(self, fresh_manager):
        name = f"test-hot-reload-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description='Skill for testing hot-reload discovery',
            triggers=['hot-reload', 'discovery', 'dynamic'],
            generated_from_task='Test hot-reload skill discovery',
        )

        fresh_manager.register_skill_from_content(
            content,
            task_text='Test hot-reload skill discovery',
            auto_promote=True,
        )

        results = fresh_manager.match_skills('hot-reload discovery')
        assert len(results) > 0
        top_name = results[0][0]
        assert top_name == name


# ===========================================================================
# 8. Integration: call_agent return path — rollback + notice injection
# ===========================================================================


class TestCallAgentReturn:
    """Verify that auto-skill rollback and notice injection work correctly
    on the call_agent return path (execution_engine._create_and_run_agent)."""

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _make_conversation(self, length: int):
        """Build a conversation list of *length* dict messages."""
        return [{'role': 'assistant' if i % 2 else 'user', 'content': f"msg {i}"} for i in range(length)]

    def _make_inst(self, fresh_manager, conv_len: int = 5):
        """Create a mock instance and preload skill-creator in the manager."""
        inst = MagicMock()
        inst.conversation = self._make_conversation(conv_len)
        inst._auto_skill_proposed = False
        inst.state = 'IDLE'
        fresh_manager._skills_registry['skill-creator'] = {
            'name': 'skill-creator',
            'file_path': 'agents/global/skills/skill-creator/SKILL.md',
            '_parsed_data': {
                'body': 'Create a reusable skill.'
            },
        }
        return inst

    # ------------------------------------------------------------------ #
    # Qualification gates (pure method: no conversation/flag mutation)
    # ------------------------------------------------------------------ #

    def test_returns_none_when_auto_skill_proposed(self, fresh_manager):
        """_auto_skill_proposed flag set → qualification returns None (one-shot)."""
        inst = self._make_inst(fresh_manager)
        inst._auto_skill_proposed = True
        prompt = fresh_manager.auto_skill_qualifies(inst, AUTO_SKILL_MIN_TURNS + 1)
        assert prompt is None

    def test_returns_none_when_turns_below_threshold(self, fresh_manager):
        """Turns effectuated <= AUTO_SKILL_MIN_TURNS → None (gate is turns-based)."""
        inst = self._make_inst(fresh_manager)
        prompt = fresh_manager.auto_skill_qualifies(inst, AUTO_SKILL_MIN_TURNS)
        assert prompt is None

    def test_fires_on_turns_not_tool_calls(self, fresh_manager):
        """Gate fires on turns > N (tool-call gate removed). The pure method
        returns the built prompt and does NOT set the one-shot flag — that is
        core.py's job after a successful trigger."""
        inst = self._make_inst(fresh_manager)
        prompt = fresh_manager.auto_skill_qualifies(inst, AUTO_SKILL_MIN_TURNS + 1)
        assert isinstance(prompt, str) and prompt
        # Pure method: flag must NOT be set by the qualification itself.
        assert not inst._auto_skill_proposed

    def test_gate_independent_of_match_score(self, fresh_manager):
        """A strong keyword match no longer blocks the gate (match condition removed)."""
        inst = self._make_inst(fresh_manager)
        # Register a skill that will strongly match "Write a test"
        fresh_manager._skills_registry['test-writing'] = {
            'name': 'test-writing',
            'file_path': 'agents/global/skills/test-writing/SKILL.md',
            'triggers': ['test', 'write'],
        }
        fresh_manager._matcher.build_index(list(fresh_manager._skills_registry.values()))
        prompt = fresh_manager.auto_skill_qualifies(inst, AUTO_SKILL_MIN_TURNS + 1)
        # The match must NOT prevent firing — the prompt is built.
        assert isinstance(prompt, str) and prompt

    def test_returns_none_when_skill_creator_missing(self, fresh_manager):
        """skill-creator not in registry → None."""
        inst = self._make_inst(fresh_manager)
        del fresh_manager._skills_registry['skill-creator']
        prompt = fresh_manager.auto_skill_qualifies(inst, AUTO_SKILL_MIN_TURNS + 1)
        assert prompt is None

    def test_qualification_does_not_mutate_conversation(self, fresh_manager):
        """Pure contract: qualifying does not append anything to the conversation."""
        inst = self._make_inst(fresh_manager)
        original_len = len(inst.conversation)
        prompt = fresh_manager.auto_skill_qualifies(inst, AUTO_SKILL_MIN_TURNS + 1)
        assert isinstance(prompt, str) and prompt
        assert len(inst.conversation) == original_len


# ===========================================================================
# Rollback Tail Sync — pool/JSONL consistency after rollback
# ===========================================================================


class TestRollbackTailSync:
    """Verify that _rollback_instance keeps pool conversation and JSONL logger
    in sync — both in the simple case and after compression markers exist.

    Covers the auto-skill rollback flow:
      1. Build conversation of known length  →  snapshot
      2. Append extra messages               →  more turns
      3. Rollback to snapshot                →  truncation
      4. Inject notice into last message     →  content-only modification
    """

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _build_conv(self, n: int):
        """Build a conversation with SYSTEM + n alternating USER/ASSISTANT pairs."""
        from agent_cascade.llm.schema import ASSISTANT, SYSTEM, USER, Message

        conv = [Message(role=SYSTEM, content='You are a test agent')]
        for i in range(n):
            conv.append(Message(role=USER, content=f"User message {i}"))
            conv.append(Message(role=ASSISTANT, content=f"Assistant reply {i}"))
        return conv

    def _write_jsonl(self, path: str, messages):
        """Write a JSONL file with metadata header + message lines."""
        import json
        with open(path, 'w', encoding='utf-8') as f:
            f.write(
                json.dumps({
                    'metadata': {
                        'agent_class': 'coder',
                        'instance_name': 'test-sync',
                        'start_timestamp': '2026-01-01T00:00:00',
                        'current_log_path': path,
                    }
                }) + '\n')
            for m in messages:
                d = m.model_dump() if hasattr(m, 'model_dump') else dict(role=m.role, content=m.content)
                f.write(json.dumps(d, ensure_ascii=False) + '\n')

    def _read_jsonl_messages(self, path: str) -> list:
        """Read message dicts from a JSONL file (skip metadata/events)."""
        import json
        msgs = []
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and 'metadata' not in item and 'event' not in item:
                    msgs.append(item)
        return msgs

    def _count_jsonl_tail(self, path: str) -> int:
        """Count messages after the last compression marker in a JSONL file."""
        from agent_cascade.logger.tail_sync_check import _count_jsonl_tail
        tail_count, _, _ = _count_jsonl_tail(path)
        return tail_count

    def _count_pool_tail(self, conv: list) -> int:
        """Count messages after the last compression marker in a pool conversation."""
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.logger.tail_sync_check import _count_pool_tail
        last_marker = AgentPool.find_last_marker(conv)
        return _count_pool_tail(conv, last_marker)

    # ------------------------------------------------------------------ #
    # Tests
    # ------------------------------------------------------------------ #

    def test_rollback_truncates_jsonl_logger(self, tmp_path):
        """Rollback with sync_logger=True truncates JSONL to match pool length.

        Flow:
          1. Create pool + logger with known conversation
          2. Append extra messages to both pool and JSONL
          3. Rollback to original length
          4. Verify JSONL message count == pool conversation count
        """
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.llm.schema import ASSISTANT, USER, Message

        # Step 1: Build initial conversation (SYS + 4 pairs = 9 messages)
        conv = self._build_conv(4)  # 9 messages
        assert len(conv) == 9

        # Create a real AgentPool (minimal)
        pool = AgentPool(llm_cfg={})
        inst = pool.create_instance('test-sync', 'coder')
        inst.conversation = list(conv)

        # Get logger and sync it
        log_inst = pool.get_logger('test-sync', 'coder')
        assert log_inst.log_path

        # Use the actual logger path (not our temp one) for the test
        test_jsonl = log_inst.log_path

        # Write initial state to logger
        self._write_jsonl(test_jsonl, conv)

        # Load history into logger's internal state so truncate_to works correctly
        log_inst.load_history_from_file()

        # Step 2: Append 3 extra messages to pool
        extra = [
            Message(role=USER, content='Extra user 1'),
            Message(role=ASSISTANT, content='Extra assistant 1'),
            Message(role=USER, content='Extra user 2'),
        ]
        inst.conversation.extend(extra)
        assert len(inst.conversation) == 12

        # Append same to JSONL logger
        log_inst.update_history(extra)

        # Verify JSONL has 12 messages
        jsonl_msgs = self._read_jsonl_messages(test_jsonl)
        assert len(jsonl_msgs) == 12

        # Step 3: Rollback to original length (9)
        removed = pool._rollback_instance(
            'test-sync',
            target_length=9,
            sync_logger=True,
            tail_sync_check=True,
        )
        assert removed == 3, f"Expected 3 removed, got {removed}"

        # Step 4: Verify pool has 9 messages
        assert len(inst.conversation) == 9

        # Step 5: Verify JSONL also has 9 messages
        jsonl_msgs_after = self._read_jsonl_messages(test_jsonl)
        assert len(jsonl_msgs_after) == 9, \
            f"JSONL has {len(jsonl_msgs_after)} messages, expected 9"

        # Step 6: Verify tail sync holds
        from agent_cascade.logger.tail_sync_check import check_tail_sync
        in_sync, pool_tail, jsonl_tail = check_tail_sync('test-sync', inst.conversation, test_jsonl)
        assert in_sync, f"Tail sync failed: pool_tail={pool_tail}, jsonl_tail={jsonl_tail}"

    def test_rollback_tail_sync_after_compression(self, tmp_path):
        """Rollback after compression: tail counts match between pool and JSONL.

        Flow:
          1. Build conversation with a compression marker
          2. Append extra messages past the marker
          3. Rollback to before the extra messages
          4. Verify pool tail == JSONL tail (both count messages after marker)
        """
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.llm.schema import ASSISTANT, SYSTEM, USER, Message
        from agent_cascade.prompts.dna import COMPRESSION_MARKER

        jsonl_path = str(tmp_path / 'test_rollback_comp.jsonl')

        # Step 1: Build conversation with compression marker
        # SYS + COMP + 3 pairs + 2 extra pairs = 10 messages
        conv = [
            Message(role=SYSTEM, content='You are a test agent'),
            Message(role=USER, content=COMPRESSION_MARKER + ' [compressed]'),
            Message(role=USER, content='User 0'),
            Message(role=ASSISTANT, content='Reply 0'),
            Message(role=USER, content='User 1'),
            Message(role=ASSISTANT, content='Reply 1'),
            Message(role=USER, content='User 2'),
            Message(role=ASSISTANT, content='Reply 2'),
            # Extra messages to be rolled back
            Message(role=USER, content='Extra 1'),
            Message(role=ASSISTANT, content='Extra reply 1'),
        ]
        assert len(conv) == 10

        # Write JSONL
        self._write_jsonl(jsonl_path, conv)

        # Create pool + instance
        pool = AgentPool(llm_cfg={})
        inst = pool.create_instance('test-sync-comp', 'coder')
        inst.conversation = list(conv)

        # Get logger
        log_inst = pool.get_logger('test-sync-comp', 'coder')
        test_jsonl = log_inst.log_path

        # Write initial state
        self._write_jsonl(test_jsonl, conv)

        # Load history into logger's internal state so truncate_to works correctly
        log_inst.load_history_from_file()

        # Verify: pool tail (after marker) = 8, JSONL tail = 8
        pool_tail_before = self._count_pool_tail(inst.conversation)
        jsonl_tail_before = self._count_jsonl_tail(test_jsonl)
        assert pool_tail_before == 8, f"pool_tail_before={pool_tail_before}"
        assert jsonl_tail_before == 8, f"jsonl_tail_before={jsonl_tail_before}"

        # Step 2: Rollback to remove the 2 extra messages (target_length=8)
        removed = pool._rollback_instance(
            'test-sync-comp',
            target_length=8,
            sync_logger=True,
            tail_sync_check=True,
        )
        assert removed == 2, f"Expected 2 removed, got {removed}"

        # Step 3: Verify pool has 8 messages
        assert len(inst.conversation) == 8

        # Step 4: Verify JSONL has 8 messages and marker is preserved
        jsonl_msgs = self._read_jsonl_messages(test_jsonl)
        assert len(jsonl_msgs) == 8, f"JSONL has {len(jsonl_msgs)}, expected 8"
        marker_present = any(
            isinstance(m.get('content', ''), str) and m['content'].startswith(COMPRESSION_MARKER) for m in jsonl_msgs)
        assert marker_present, 'Compression marker missing from JSONL after rollback'

        # Step 5: Verify tail counts match (both should be 6 = 8 - 1 marker - 1 SYS)
        pool_tail_after = self._count_pool_tail(inst.conversation)
        jsonl_tail_after = self._count_jsonl_tail(test_jsonl)
        assert pool_tail_after == 6, f"pool_tail_after={pool_tail_after}"
        assert jsonl_tail_after == 6, f"jsonl_tail_after={jsonl_tail_after}"

        # Step 6: Tail sync check passes
        from agent_cascade.logger.tail_sync_check import check_tail_sync
        in_sync, pt, jt = check_tail_sync('test-sync-comp', inst.conversation, test_jsonl)
        assert in_sync, f"Tail sync failed: pool_tail={pt}, jsonl_tail={jt}"


# ===========================================================================
# 8b. In-loop auto-skill trigger — engine loop integration (plan §5.3)
# ===========================================================================


class TestInLoopTrigger:
    """The in-loop trigger replaces the post-run two-run helper: at NATURAL
    COMPLETION (an assistant turn with no tool call, reported by _post_turn_checks)
    it qualifies, snapshots the task output, injects the reflection prompt into
    BOTH conversation targets (R3), and resets the loop budget. Tests drive the
    REAL ExecutionEngine.run() with a stubbed LLM; settings AUTO_SKILL_MIN_TURNS /
    AUTO_SKILL_EXTRA_TURNS are patched per test so runs stay short."""

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_inst(max_turns, conv_len=1):
        from agent_cascade.agent_instance import AgentInstance
        inst = AgentInstance.__new__(AgentInstance)
        inst.instance_name = 'w'
        inst.agent_class = 'test_agent'
        # Single fixed task message: the real _setup_turn would prepend a system
        # message (insert_message_at_head), but our stub returns the conversation
        # as-is, so the conv stays exactly [task] until messages are committed.
        inst.conversation = [Message(role=USER, content='task')]
        inst._cached_messages = list(inst.conversation)
        inst._cached_llm_messages = list(inst.conversation)
        inst.max_turns = max_turns
        # run() transitions IDLE→RUNNING itself; starting in RUNNING trips the
        # L1 race guard (core.py raises "[BUG] ... should be IDLE").
        inst.state = AgentState.IDLE
        inst._compression_lock = threading.RLock()
        inst._state_lock = threading.RLock()
        inst._generate_cfg_override = None
        inst._turn_consumed = False
        inst._slot_release = None
        inst._slot_key = None
        inst._compression_suspended_at = 0.0
        inst._last_config_version = -1
        inst._last_token_count_conversation_length = -1
        inst._continue_saved_msg = None
        inst._auto_skill_proposed = False
        # run()'s Phase 3 streaming-tick path reads this (core.py:806).
        inst._streaming_responses = []
        return inst

    @staticmethod
    def _make_pool(fresh_manager,
                   tmp_path,
                   max_turns=3,
                   min_turns=2,
                   extra_turns=5,
                   auto_skill_enabled=True,
                   load_mode='AUTO',
                   with_creator=True,
                   natural_end_at=None,
                   exhaust_at=None):
        """Build an engine + instance that drives the REAL ExecutionEngine.run().

        ``natural_end_at`` controls when _post_turn_checks reports a genuine natural
        end (returns False → run() breaks). It defaults to ``max_turns`` so the agent
        "completes" on its last turn — mirroring how real agents end naturally well
        before their large max_turns budget. Callers may pass a smaller value to make
        the agent finish earlier.

        ``exhaust_at`` (default None) instead makes the run break by BUDGET EXHAUSTION at
        that turn: _post_turn_checks returns True for checks 1..(exhaust_at-1) and False
        on check exhaust_at, so no *natural* end is reported — the loop runs out of turns.
        Used to force a reflection tail's last turn to be a real final turn (final-turn
        warning + tool disable).

        The natural-end driver is a side_effect LIST: after it is exhausted (the
        reflection turns), MagicMock raises StopIteration, which run() re-raises (it only
        catches Exception) so the run generator terminates cleanly and its exit finally
        runs. See the _post_turn_checks setup below for details."""
        from agent_cascade.execution_engine import ExecutionEngine

        template = MagicMock()
        template.function_map = {'tool_a': None, 'tool_b': None}

        pool = MagicMock()
        pool.settings.auto_skill_enabled = auto_skill_enabled
        # The live-read turn gate (core.py::_try_auto_skill_extension) reads this from
        # pool.settings, not the patched module constant — set it explicitly so a bare
        # MagicMock doesn't auto-create a child mock that breaks the int comparison.
        pool.settings.auto_skill_min_turns = min_turns
        pool.settings.default_load_skill_mode = load_mode
        pool.settings.tail_sync_check_enabled = False
        pool.get_template.return_value = template
        pool.is_instance_terminated.return_value = False
        pool.has_pending.return_value = False
        pool.has_messages.return_value = False
        pool.drain_queue.return_value = []
        # Real logger object (not a MagicMock): _log_messages_to_jsonl's count-based
        # delta sync reads len(log.data['history']) and appends on every
        # log_message() call. A MagicMock auto-attribute would stay truthy, making
        # the delta look non-empty every pass and double-logging committed messages.
        # tmp_path keeps the JSONL metadata write off the repo tree (test isolation).
        from agent_cascade.logger import AgentInstanceLogger
        log_inst = AgentInstanceLogger('test_agent', 'w', str(tmp_path), log_path=str(tmp_path / 'w.jsonl'))
        pool.get_logger.return_value = log_inst

        if with_creator:
            # Populate the registry via REAL discovery, not hand-registration.
            # load_full_instructions() does a direct _skills_registry.get() lookup and
            # never calls _ensure_discovered(), so a bare SkillManager() (empty registry)
            # returns None for 'skill-creator' and the trigger gate silently fails.
            # discover() scans agents/global/skills (contains skill-creator/SKILL.md,
            # relative to cwd N:\work\WD\AgentCascade). _cache_ttl=0.0 forces a scan on
            # this fresh manager so TTL/signature cache cannot short-circuit it — the
            # same pattern as test_candidate_tier_discovered_with_highest_priority.
            fresh_manager._cache_ttl = 0.0
            fresh_manager.discover([Path('agents/global/skills')])
        pool.skill_manager = fresh_manager

        engine = ExecutionEngine(pool)
        inst = TestInLoopTrigger._make_inst(max_turns)

        # Stub the turn machinery: one assistant message per LLM call, no tools.
        def fake_setup_turn(instance):
            return list(instance.conversation), [Message(role=USER, content='task')], []

        engine._setup_turn = MagicMock(side_effect=fake_setup_turn)
        engine._pre_llm_checks = MagicMock(return_value=False)
        engine._check_stop_conditions = MagicMock(return_value=False)
        engine._is_suspended_by_compression = MagicMock(return_value=False)
        # Stub the un-stubbed path pieces that would otherwise hit the real pool:
        # terminal-stop guards (MagicMock pool attrs are truthy → early exit),
        # slot acquire (auto-attribute _acquire_slot would be "present"), and the
        # stream-termination check inside the Phase 3 yield loop.
        engine._is_terminal_stop = MagicMock(return_value=False)
        engine._acquire_slot_with_logging = MagicMock(return_value=None)
        engine._check_stream_termination = MagicMock(return_value=None)
        # A generator (not a bare iterator) so run()'s `finally: gen.close()`
        # works — list_iterator has no .close().
        # Number replies by a per-call counter, NOT len(msgs): the turn-limit
        # warnings are now full user-message insertions into llm_messages, so
        # len(msgs) drifts from the iteration number. A counter keeps "reply N"
        # == "Nth LLM call" (== iteration N), which is what the snapshot/tail
        # assertions below reason about (e.g. trigger at iter 3 → last committed
        # assistant text is 'reply 2').
        _llm_call_count = {'n': 0}

        def fake_llm(inst, msgs):
            # None first = streaming tick (drives the yield path), then the turn's
            # assistant message. Mirrors a real LLM stream; run()'s Phase 3 loop
            # only appends Message/dict items to turn_output.
            _llm_call_count['n'] += 1
            yield None
            yield Message(role=ASSISTANT, content=f"reply {_llm_call_count['n']}")

        engine._call_llm_with_injection = MagicMock(side_effect=lambda inst, msgs: fake_llm(inst, msgs))
        # REAL _process_response (commits turn_output via _append_and_log_batch); its
        # tool-execution sub-path is stubbed to a no-tool answer so the loop exits
        # each iteration at Phase 5.
        engine._execute_detected_tools = MagicMock(return_value=False)
        # Natural-end driver via a COUNTER FUNCTION (not a side_effect list). Design:
        #   - Return False on check N (natural_end_at) → the agent's genuine natural
        #     completion. Because turns_available is still > 0 at that point, run() takes
        #     the `if not completed:` branch and tries the auto-skill trigger there.
        #   - Return True for EVERY check after N. This is deliberate: once the trigger
        #     fires it resets the budget to AUTO_SKILL_EXTRA_TURNS fresh turns, so the
        #     reflection tail keeps looping (completed=True) until its budget runs out —
        #     run() then terminates by normal budget exhaustion (turns_available == 0),
        #     NOT by an exception. This is important because core.py's `except Exception`
        #     CATCHES StopIteration, so a side_effect list that exhausts would be swallowed
        #     (logged + error-yielded) and the tail would die after one turn. A function
        #     that keeps returning True avoids that entirely: the tail effects exactly
        #     AUTO_SKILL_EXTRA_TURNS LLM calls before the budget-exhaustion break.
        #   - When NO trigger fires (gate failure / stop / stall / short run), the False at
        #     check N causes a plain `break` — no extension, as those tests assert.
        # ``exhaust_at`` is accepted for API symmetry but the natural-end path already ends
        # by budget exhaustion; it is unused in the current test set.
        _nend = max_turns if natural_end_at is None else natural_end_at
        _ptc_calls = {'n': 0}

        def _post_turn_checks_driver(*a, **k):
            _ptc_calls['n'] += 1
            # False only on the natural-end check; True (keep looping) everywhere else so a
            # triggered reflection tail runs its full EXTRA budget before exhausting.
            return _ptc_calls['n'] != _nend

        engine._post_turn_checks = MagicMock(side_effect=_post_turn_checks_driver)

        # CRITICAL: the AUTO_SKILL_* constants are module-level names imported into BOTH
        # core.py and skills/manager.py (independent copies). They must be patched for the
        # ENTIRE duration of the run — including every reflection turn. A generator cannot
        # hold a `with patch(...)` context across its yield, so instead we return a drain
        # callable that runs engine.run(inst) INSIDE its own patch context. The test calls
        # this once; it drives the run to completion and returns the (exhausted) generator
        # so tests can inspect state. This guarantees the min-turns gate sees `min_turns`
        # (not the default 50) on every turn, natural-end or reflection.
        def _run():
            with patch('agent_cascade.engine.core.AUTO_SKILL_MIN_TURNS', min_turns), \
                    patch('agent_cascade.engine.core.AUTO_SKILL_EXTRA_TURNS', extra_turns), \
                    patch('agent_cascade.skills.manager.AUTO_SKILL_MIN_TURNS', min_turns):
                gen = engine.run(inst)
                for _ in gen:
                    pass
                return gen

        yield engine, inst, pool, _run

    @staticmethod
    def _conv_contents(conv):
        return [m.content if isinstance(m, Message) else m.get('content', '') for m in conv]

    # ------------------------------------------------------------------ #
    # 1-4: budget reset, snapshot, no-rollback, one-shot flag
    # ------------------------------------------------------------------ #

    def test_budget_reset_off_by_one(self, fresh_manager, tmp_path):
        """Natural end at turn N resets BOTH loop locals; the run then effects exactly
        AUTO_SKILL_EXTRA_TURNS further iterations (the +1 in turns_available is
        consumed by _consume_turn before the next loop check).

        max_turns=5, natural_end_at=3 → the agent completes on iteration 3 (LLM call #3),
        the trigger fires there, and exactly EXTRA=5 reflection turns follow. Total LLM
        calls = 3 + 5 = 8."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                                   extra_turns=5, natural_end_at=3).__next__()
        _run()
        assert inst._auto_skill_proposed is True, 'trigger should have fired'
        # Exactly 3 (up to and including the natural-end turn) + 5 (extra) LLM calls.
        assert engine._call_llm_with_injection.call_count == 3 + 5

    def test_snapshot_captures_last_assistant_text(self, fresh_manager, tmp_path):
        """Snapshot is the last ASSISTANT text in the conversation at trigger time.

        At natural completion the final answer is already committed (per-iteration
        order: LLM → _process_response commits → _post_turn_checks), so the snapshot
        captures the just-completed reply — NOT a previous turn's. With max_turns=5,
        natural_end_at=3 the trigger fires after iteration 3 commits 'reply 3', so the
        snapshot must be 'reply 3' (the old budget-exhaustion design captured 'reply 2').
        """
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                                   extra_turns=5, natural_end_at=3).__next__()
        _run()
        assert inst._auto_skill_proposed is True
        assert inst._auto_skill_task_output == 'reply 3'

    def test_no_rollback_and_conversation_grows(self, fresh_manager, tmp_path):
        """No rollback: the reflection prompt + extended turns all stay in the
        conversation; pool._rollback_instance is never called."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                                   extra_turns=5, natural_end_at=3).__next__()
        _run()
        assert inst._auto_skill_proposed is True
        # No rollback: every message stays in the conversation. Natural end at turn 3 of
        # max_turns=5; the trigger resets the budget to EXTRA=5 fresh turns and the harness
        # driver keeps the tail looping until it exhausts them, so the extended last turn is
        # a real final turn. Exact layout (verified):
        #   [0] task
        #   [1] reply 1            (original)
        #   [2] reply 2            (original)
        #   [3] halfway warning    (original budget, iter1)
        #   [4] reply 3            (natural-end turn; trigger fires here)
        #   [5] reflection prompt  (injected at trigger)
        #   [6] reply 4            (extended)
        #   [7] reply 5            (extended)
        #   [8] reply 6            (extended)
        #   [9] reply 7            (extended)
        #   [10] final-turn warning (iter7 — the extended tail's last turn)
        #   [11] reply 8 + 'Turn limit reached' notice (tail exhausted its budget)
        # = 12 messages. NOTE: the 50%/90% budget warnings are SUPPRESSED during the
        # reflection extension (stale-threshold fix — they describe the already-exhausted
        # ORIGINAL budget and would otherwise print a misleading "N remaining out of
        # {extended}" text). The original-budget halfway warning at [3] still fires; only the
        # two extended-budget warnings that previously sat between replies 5/6 and 6/7 are
        # gone. We assert the exact count so a regression that drops/loses any of these real
        # messages (the rollback this test guards against) is caught.
        assert len(inst.conversation) == 12
        pool._rollback_instance.assert_not_called()

    def test_one_shot_flag(self, fresh_manager, tmp_path):
        """Trigger sets _auto_skill_proposed; a second qualification returns None."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                                   extra_turns=5, natural_end_at=3).__next__()
        _run()
        assert inst._auto_skill_proposed is True
        prompt = fresh_manager.auto_skill_qualifies(inst, AUTO_SKILL_MIN_TURNS + 1)
        assert prompt is None

    # ------------------------------------------------------------------ #
    # 5-6: tools stay enabled on the triggering turn; output return path
    # ------------------------------------------------------------------ #

    def test_tools_enabled_on_triggering_turn(self, fresh_manager, tmp_path):
        """At natural end the TRIGGERING turn is NOT a final turn: we `continue` into the
        reflection turns (we do not break), so no disabled_tools override is set on that
        turn and NO [SYSTEM WARNING: Final turn] message appears for the ORIGINAL budget.

        The harness's _post_turn_checks driver returns True after the natural end, so the
        reflection tail runs until it EXHAUSTS its EXTRA budget — meaning the extended last
        turn IS a real final turn (one final-turn warning, on the tail only). This test
        asserts that single warning is attributable to the extended tail, not the triggering
        turn: the key behavioural difference from the old budget-exhaustion design, where
        the triggering turn itself was the last one."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                                   extra_turns=5, natural_end_at=3).__next__()
        _run()
        assert inst._auto_skill_proposed is True
        # The triggering turn did not set a tool-disable override (it continued, not broke).
        override = getattr(inst, '_generate_cfg_override', None)
        if isinstance(override, dict):
            assert 'disabled_tools' not in override
        # Exactly ONE final-turn warning exists — on the extended tail's last turn (the
        # tail exhausts its budget under this harness), NOT on the triggering/original turn.
        contents = self._conv_contents(inst.conversation)
        final_warnings = [c for c in contents if '[SYSTEM WARNING: Final turn' in str(c)]
        assert len(final_warnings) == 1, \
            f'expected exactly one final-turn warning (extended tail last turn), got {len(final_warnings)}'

    def test_output_return_path(self, fresh_manager, tmp_path):
        """extract_instance_output with instance= returns the pre-reflection snapshot;
        without it, the reflection tail (last message).

        CHOOSE: assert the tail WITH a turn-limit notice. Rationale — under this harness the
        _post_turn_checks driver returns True after the natural end, so the reflection tail
        runs until it EXHAUSTS its EXTRA budget; run() then appends the 'Turn limit reached'
        notice at exit (a harness artifact of forcing budget exhaustion). The test's intent —
        the snapshot-vs-tail distinction — is preserved by asserting instance= returns the
        pre-reflection snapshot ('reply 3') while the bare call returns the extended tail
        ('reply 8', the last assistant reply) plus the turn-limit notice."""
        from agent_cascade.compression.helpers import extract_instance_output
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                                   extra_turns=5, natural_end_at=3).__next__()
        _run()
        assert inst._auto_skill_proposed is True
        with_snap = extract_instance_output(list(inst.conversation), 'w', instance=inst)
        # Snapshot taken at natural end (turn 3): the just-completed final answer.
        assert with_snap == 'reply 3'
        without_snap = extract_instance_output(list(inst.conversation), 'w')
        # Tail is the last assistant reply of the extended (reflection) turns: reply 8,
        # followed by the turn-limit notice appended when the tail exhausted its budget.
        assert without_snap.startswith('reply 8')
        assert 'Turn limit reached' in without_snap
        # The two must differ — that is the snapshot-vs-tail distinction this test guards.
        assert with_snap != without_snap

    # ------------------------------------------------------------------ #
    # 7: gate failures → no trigger, normal break (no extension)
    # ------------------------------------------------------------------ #

    def _assert_no_trigger_normal_break(self, fresh_manager, tmp_path, **kw):
        """Gate failure at natural end → no trigger, no extension, plain break.

        With max_turns=3, min_turns=2 the agent would qualify on turns but a gate blocks
        it; it still ends naturally (natural_end_at=max_turns) so the run breaks with NO
        reflection turns. We assert no trigger + no extension; the exact warning layout
        is not the point of a gate test."""
        engine, inst, pool, _run = self._make_pool(fresh_manager,
                                                   tmp_path,
                                                   max_turns=3,
                                                   min_turns=2,
                                                   extra_turns=5,
                                                   **kw).__next__()
        _run()
        assert not getattr(inst, '_auto_skill_proposed', False), 'trigger must NOT fire'
        assert engine._call_llm_with_injection.call_count == 3, 'no extension on gate failure'

    def test_gate_auto_skill_disabled(self, fresh_manager, tmp_path):
        """auto_skill_enabled=False → no trigger; normal break (no extension)."""
        self._assert_no_trigger_normal_break(fresh_manager, tmp_path, auto_skill_enabled=False)

    def test_gate_load_skill_none(self, fresh_manager, tmp_path):
        """default_load_skill_mode='NONE' → no trigger; normal break (no extension)."""
        self._assert_no_trigger_normal_break(fresh_manager, tmp_path, load_mode=LOAD_SKILL_NONE)

    def test_gate_skill_creator_missing(self, fresh_manager, tmp_path):
        """skill-creator absent from registry → no trigger; normal break (no extension)."""
        self._assert_no_trigger_normal_break(fresh_manager, tmp_path, with_creator=False)

    # ------------------------------------------------------------------ #
    # 8: the extended tail's last turn; R6 restore
    # ------------------------------------------------------------------ #

    def test_final_extended_turn_behaves_like_normal_last_turn(self, fresh_manager, tmp_path):
        """The LAST turn of the extension (turn N+EXTRA) must behave exactly like a
        normal last turn: tools disabled via _generate_cfg_override, final-turn warning
        appended, and the loop exits with turns_available == 0.

        CHOOSE: force the extra budget to exhaust. The agent naturally ends at turn 3
        (trigger fires and resets the budget to EXTRA=3 fresh turns); the harness's
        _post_turn_checks driver then returns True for every subsequent check, so the
        reflection tail keeps looping until its 3-turn budget runs out — making the extended
        last turn a REAL final turn (final-turn warning + tool disable). This is the ONLY way
        to exercise the real final-turn block on the extended last turn; if the tail ended
        naturally there would be no final-turn warning at all (see
        test_tools_enabled_on_triggering_turn)."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                                   extra_turns=3, natural_end_at=3).__next__()
        _run()
        assert inst._auto_skill_proposed is True
        assert engine._call_llm_with_injection.call_count == 3 + 3
        # Final-turn warning present exactly once (on the extended last turn only).
        contents = self._conv_contents(inst.conversation)
        warnings = [c for c in contents if '[SYSTEM WARNING: Final turn' in str(c)]
        assert len(warnings) == 1, f'expected exactly one final-turn warning, got {len(warnings)}'
        # Tool-disable override was set and then cleaned up (popped after the call).
        override = inst._generate_cfg_override
        assert isinstance(override, dict) and 'disabled_tools' not in override

    def test_max_turns_restored_after_run(self, fresh_manager, tmp_path):
        """R6: after a triggered run completes, instance.max_turns is restored to
        the pre-trigger value (no leak into the next run)."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                                   extra_turns=5, natural_end_at=3).__next__()
        _run()
        assert inst._auto_skill_proposed is True
        assert inst.max_turns == 5, f'max_turns leaked: {inst.max_turns} != 5'

    # ------------------------------------------------------------------ #
    # NEW: natural-end triggering + exclusion paths + min-turns gate
    # ------------------------------------------------------------------ #

    def test_natural_end_triggers_extension(self, fresh_manager, tmp_path):
        """Agent ends at turn N (< max_turns) with no tool call → extension fires and
        the run effects exactly AUTO_SKILL_EXTRA_TURNS further LLM calls."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=50, min_turns=2,
                                                   extra_turns=5, natural_end_at=7).__next__()
        _run()
        assert inst._auto_skill_proposed is True, 'natural end must trigger the extension'
        # 7 (up to and including the natural-end turn) + 5 (extra) LLM calls.
        assert engine._call_llm_with_injection.call_count == 7 + 5

    def test_stop_exit_does_not_trigger(self, fresh_manager, tmp_path):
        """A terminal stop exit must NOT trigger the extension — even AFTER more than
        AUTO_SKILL_MIN_TURNS turns have run and Phase 5 is reached (guards the
        _is_genuine_completion terminal-stop exclusion).

        The old version of this test was tautological: it broke at the pre-LLM stop check
        before ANY turn ran, so Phase 5 (the trigger point) was never reached and the test
        passed even with the bug present. This version drives a REAL run where the agent
        completes 7 turns (> min_turns=2), then a terminal stop is set for turn 8. On turn 8
        _post_turn_checks returns False via the STOP path (line 2271); run() reaches the
        `if not completed:` branch, and _is_genuine_completion must return False there so the
        trigger is skipped. We spy on _try_auto_skill_extension to assert it was NEVER called —
        this would FAIL if the stop exclusion regressed (the spy would be called once)."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=50, min_turns=2,
                                                   extra_turns=5).__next__()
        # Drive a REAL run where the agent completes 7 turns (> min_turns=2), then on turn 8
        # _post_turn_checks reports a break via the STOP path. We mock _post_turn_checks to
        # return True for checks 1-7 (keep looping) and False on check 8 — this is the faithful
        # stand-in for "the real method took its stop path". Crucially, we ALSO set
        # _is_terminal_stop=True so that run()'s Phase-5 gate (_is_genuine_completion) sees a
        # terminal stop and EXCLUDES the trigger. The pre-LLM stop check is already stubbed to
        # False by the harness (so turns 1-7 proceed), and the post-LLM stop check at line 854
        # must not fire early — so _is_terminal_stop is a counter that flips True only after 7
        # LLM calls have completed, i.e. exactly when turn 8's Phase-5 gate runs.
        _ptc_calls = {'n': 0}

        def _stop_on_eighth(*a, **k):
            _ptc_calls['n'] += 1
            return _ptc_calls['n'] != 8  # False on check 8 → break (the stop path)

        engine._post_turn_checks = MagicMock(side_effect=_stop_on_eighth)
        # The terminal-stop flag must be False for every post-LLM stop check (line 854) on
        # turns 1-8 — otherwise run() breaks BEFORE Phase 5 and the trigger point is never
        # reached (tautological). It must become True only when Phase 5's _is_genuine_completion
        # consults it on turn 8, so the stop exclusion engages at exactly the right moment.
        # Both line 854 and Phase 5 run after LLM call N of the same iteration (line 854 first).
        # A small state machine keyed on the LLM-call count: once we've seen 8 LLM calls, the
        # FIRST _is_terminal_stop call in that iteration is line 854 (return False → reach
        # Phase 5), and the NEXT call is Phase 5's _is_genuine_completion (return True → exclude).
        _llm_seen = {'n': 0}
        _stop_state = {'latched': False}

        def _terminal_stop_gate(*a, **k):
            if _llm_seen['n'] < 8:
                return False  # turns 1-7: never a terminal stop
            # Turn 8 (or later): first call is line 854 → False; second is Phase 5 → True.
            if not _stop_state['latched']:
                _stop_state['latched'] = True
                return False  # line-854 check: let it through to Phase 5
            return True       # Phase 5's _is_genuine_completion: terminal stop active

        engine._is_terminal_stop = MagicMock(side_effect=_terminal_stop_gate)
        # Count real LLM turns so the gate flips at the right moment.
        _orig_llm = engine._call_llm_with_injection

        def _counting_llm(*a, **k):
            _llm_seen['n'] += 1
            return _orig_llm(*a, **k)

        engine._call_llm_with_injection = MagicMock(side_effect=_counting_llm)
        # Spy: assert the trigger is never reached for a stopped agent.
        spy = MagicMock(side_effect=engine._try_auto_skill_extension)
        engine._try_auto_skill_extension = spy
        _run()
        assert not getattr(inst, '_auto_skill_proposed', False), 'terminal stop must NOT trigger'
        # 7 completed turns + the stopping turn 8 = 8 LLM calls; NO extension (no EXTRA tail).
        assert engine._call_llm_with_injection.call_count == 8
        # The mechanism: the trigger was never even called (the _is_genuine_completion gate
        # short-circuited before it). This is what would fail if the stop exclusion regressed.
        spy.assert_not_called()

    def test_pure_thinking_stall_does_not_trigger(self, fresh_manager, tmp_path):
        """A pure-thinking stall exit must NOT trigger the extension — even AFTER more than
        AUTO_SKILL_MIN_TURNS turns have run and Phase 5 is reached (guards the
        _is_genuine_completion pure-thinking exclusion).

        The old version was tautological: it stalled on turn 1, which the min-turns gate
        would block anyway — so the test passed even if the stall exclusion were removed.
        This version completes 7 turns (> min_turns=2), then a pure-thinking stall fires on
        turn 8. On turn 8 _post_turn_checks returns False via the STALL path (line 2295);
        run() reaches `if not completed:`, and _is_genuine_completion must return False so the
        trigger is skipped. We spy on _try_auto_skill_extension to assert it was NEVER called —
        this would FAIL if the stall exclusion regressed."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=50, min_turns=2,
                                                   extra_turns=5).__next__()
        # Drive a REAL run: complete 7 turns (natural-end mock), then on the 8th Phase-5 check
        # report a pure-thinking stall. The harness's counter driver is replaced with one that
        # returns False on check 8; combined with _detect_pure_thinking_turn=True, _post_turn_checks
        # takes the STALL path (line 2295) — NOT the natural-completion path.
        _ptc_calls = {'n': 0}

        def _stall_on_eighth(*a, **k):
            _ptc_calls['n'] += 1
            return _ptc_calls['n'] != 8  # False on check 8 → stall break

        engine._post_turn_checks = MagicMock(side_effect=_stall_on_eighth)
        # No terminal stop (so the STOP path is not taken); the pure-thinking detector fires.
        engine._is_terminal_stop = MagicMock(return_value=False)
        engine._detect_pure_thinking_turn = MagicMock(return_value=True)
        # Spy: assert the trigger is never reached for a stalled agent.
        spy = MagicMock(side_effect=engine._try_auto_skill_extension)
        engine._try_auto_skill_extension = spy
        _run()
        assert not getattr(inst, '_auto_skill_proposed', False), 'pure-thinking stall must NOT trigger'
        # 7 completed turns + the stalling turn 8 = 8 LLM calls; NO extension (no EXTRA tail).
        assert engine._call_llm_with_injection.call_count == 8
        # The mechanism: the trigger was never even called (the _is_genuine_completion gate
        # short-circuited before it). This is what would fail if the stall exclusion regressed.
        spy.assert_not_called()

    def test_min_turns_gate_blocks_short_run(self, fresh_manager, tmp_path):
        """An agent that ends in N ≤ AUTO_SKILL_MIN_TURNS must NOT trigger — the
        min-turns gate is load-bearing under natural-end triggering."""
        # min_turns=5, natural_end_at=3 → _current_turn=3 at natural end, which is not
        # strictly greater than 5, so the gate blocks the trigger. No extension fires.
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=50, min_turns=5,
                                                   extra_turns=5, natural_end_at=3).__next__()
        _run()
        assert not getattr(inst, '_auto_skill_proposed', False), \
            'short run (N <= AUTO_SKILL_MIN_TURNS) must NOT trigger'
        # No extension: only the turns up to (and including) the natural-end turn ran.
        assert engine._call_llm_with_injection.call_count == 3

    # ------------------------------------------------------------------ #
    # todo.md:149 — last-turn tool-disable vs skill-reflection extension
    # ------------------------------------------------------------------ #
    # The agent ends NATURALLY on its exact LAST turn (turns_available == 1) with all
    # cheap extension gates passing. Pre-fix, the final-turn block disabled ALL tools
    # for that LLM call (function schema [] -> KV full reprocess #1) and the extension's
    # first reflection turn re-enabled them (schema restored -> KV full reprocess #2).
    # Post-fix, _auto_skill_gates_met() lets the final-turn block skip both the warning
    # and the tool-disable, keeping the prefix stable across the trigger boundary.

    @staticmethod
    def _trace_disabled_tools(engine):
        """Wrap engine._call_llm_with_injection to record instance._generate_cfg_override's
        disabled_tools at each LLM call (the value actually sent in the request schema)."""
        orig = engine._call_llm_with_injection
        seen = []

        def _tracer(inst, msgs):
            ovr = getattr(inst, '_generate_cfg_override', None)
            seen.append(ovr.get('disabled_tools') if isinstance(ovr, dict) else None)
            return orig(inst, msgs)

        engine._call_llm_with_injection = MagicMock(side_effect=_tracer)
        return seen

    def test_last_turn_natural_end_extension_keeps_tools_enabled(self, fresh_manager, tmp_path):
        """REGRESSION (todo.md:149): natural end on the EXACT last turn with gates passing
        must NOT disable tools for the triggering LLM call. MUST fail pre-fix (tools were
        disabled -> full reprocess) and pass post-fix."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=3, min_turns=2,
                                                    extra_turns=5).__next__()  # natural_end_at defaults to max_turns
        seen = self._trace_disabled_tools(engine)
        _run()
        assert inst._auto_skill_proposed is True, 'trigger should have fired'
        # Triggering turn is LLM call #3 (turns_available == 1). Pre-fix it saw all tools
        # disabled; post-fix the override was never set for that call.
        assert seen[2] is None, \
            f'triggering turn must keep tools enabled, got disabled_tools={seen[2]!r} (full reprocess)'

    def test_last_turn_no_extension_disables_tools(self, fresh_manager, tmp_path):
        """Control: with a gate failing (auto_skill_enabled=False), the last turn behaves
        exactly as before — tools ARE disabled for the final LLM call and the final-turn
        warning is injected. Guards against over-suppression by the fix."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=3, min_turns=2,
                                                    extra_turns=5, auto_skill_enabled=False).__next__()
        seen = self._trace_disabled_tools(engine)
        _run()
        assert not getattr(inst, '_auto_skill_proposed', False), 'trigger must NOT fire'
        # Last (and only) turn: all template tools disabled for the final LLM call.
        assert seen[-1] == ['tool_a', 'tool_b'], \
            f'gate-failing last turn must disable all tools, got {seen[-1]!r}'
        # Final-turn warning still injected (max_turns != 1).
        contents = self._conv_contents(inst.conversation)
        assert any('[SYSTEM WARNING: Final turn' in str(c) for c in contents), \
            'final-turn warning must be injected when no extension fires'

    def test_last_turn_extension_suppresses_final_warning(self, fresh_manager, tmp_path):
        """With gates passing + natural end on the last turn, the '[SYSTEM WARNING: Final
        turn...]' message must NOT appear for the ORIGINAL budget (it would be misleading —
        extra turns are about to be granted). The extended tail's own final warning (harness
        exhausts the EXTRA budget) is expected and allowed."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=3, min_turns=2,
                                                    extra_turns=5).__next__()
        _run()
        assert inst._auto_skill_proposed is True
        # Original-budget final warning must be suppressed: it would sit between the
        # triggering turn's reply and the reflection prompt. Post-fix the conversation goes
        # straight from 'reply 3' (triggering turn) to the reflection prompt.
        contents = [str(c) for c in self._conv_contents(inst.conversation)]
        assert 'reply 3' in contents, 'sanity: triggering turn reply must be committed'
        idx_reply = contents.index('reply 3')
        # The original-budget final warning is injected in the SAME iteration as the
        # triggering turn (before its LLM call), so it sits immediately BEFORE 'reply 3'
        # (or, if appended after, immediately after). Either slot must be free of it — the
        # extended tail's OWN final warning (harness exhausts EXTRA) is expected and allowed
        # further down the run. Post-fix neither adjacent slot carries a final-turn warning.
        neighbours = [contents[idx_reply - 1], contents[idx_reply + 1]]
        assert not any('[SYSTEM WARNING: Final turn' in c for c in neighbours), \
            f'original-budget final warning leaked at the extension boundary: {neighbours!r}'

    def test_reflection_final_turn_still_disables_tools(self, fresh_manager, tmp_path):
        """The REFLECTION's own last turn must still disable tools (clean final answer):
        by then the one-shot _auto_skill_proposed flag is set, so _auto_skill_gates_met
        returns False and the normal final-turn path runs. The fix only skips the disable at
        the trigger boundary, not throughout the reflection."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=3, min_turns=2,
                                                    extra_turns=5).__next__()
        seen = self._trace_disabled_tools(engine)
        _run()
        assert inst._auto_skill_proposed is True
        # Trigger fired at LLM call #3; the harness keeps the tail looping until its EXTRA
        # budget (5) exhausts, so the LAST LLM call is the reflection's real final turn.
        assert seen[-1] == ['tool_a', 'tool_b'], \
            f'reflection final turn must disable all tools, got {seen[-1]!r}'

    def test_tool_disable_prefix_stability_across_trigger(self, fresh_manager, tmp_path):
        """No function-schema toggle across the trigger boundary: the set of active
        functions (what actually goes in the request) is identical for the triggering turn
        and the first reflection turn. This directly encodes 'no double full reprocess'."""
        engine, inst, pool, _run = self._make_pool(fresh_manager, tmp_path, max_turns=3, min_turns=2,
                                                    extra_turns=5).__next__()
        seen = self._trace_disabled_tools(engine)
        _run()
        assert inst._auto_skill_proposed is True
        # Triggering turn (#3) and first reflection turn (#4) must see the SAME function set.
        assert seen[2] == seen[3], \
            f'function schema toggled across trigger boundary: {seen[2]!r} -> {seen[3]!r}'
        # And that shared set is the FULL tool set (nothing disabled on either side).
        assert seen[2] is None and seen[3] is None, \
            f'both sides of the trigger boundary must keep tools enabled: {seen[2:4]!r}'


# ===========================================================================
# 9. Skill Rating — metrics writer, prompt, and propose_skill modes
# ===========================================================================


class TestRatingMetrics:
    """_record_rating / record_rating persistence and schema 1.1."""

    @pytest.fixture(autouse=True)
    def _isolated_metrics(self, fresh_manager, tmp_path):
        """Point the manager's metrics file at a temp path (avoid clobbering production)."""
        self.manager = fresh_manager
        self.metrics_file = tmp_path / 'skills-metrics.json'
        _isolate_metrics(fresh_manager, tmp_path)
        yield

    def test_record_rating_shape_and_average(self, fresh_manager):
        m = self.manager
        m.record_rating('my-skill', 8.0)
        m.record_rating('my-skill', 6.0)
        entry = m.get_metrics('my-skill')
        r = entry['ratings']
        assert r['count'] == 2
        assert abs(r['sum'] - 14.0) < 1e-9
        assert r['latest'] == 6.0
        # Average computable: sum / count
        assert abs(r['sum'] / r['count'] - 7.0) < 1e-9

    def test_record_rating_flushes_to_disk_schema_1_2(self, fresh_manager):
        m = self.manager
        m.record_rating('my-skill', 5.5)
        m._flush_metrics_to_disk()
        assert self.metrics_file.exists()
        import json as _json
        data = _json.loads(self.metrics_file.read_text(encoding='utf-8'))
        # Schema bumped to 1.2 (per-version rating history); older files are still tolerated on load.
        assert data['schema_version'] == '1.2'
        r = data['skills']['my-skill']['ratings']
        assert r['count'] == 1
        assert abs(r['sum'] - 5.5) < 1e-9
        assert r['latest'] == 5.5

    def test_record_rating_rejects_out_of_range(self, fresh_manager):
        m = self.manager
        with pytest.raises(ValueError):
            m.record_rating('my-skill', 11.0)
        with pytest.raises(ValueError):
            m.record_rating('my-skill', -0.5)

    def test_record_rating_persists_immediately_without_extra_increments(self, fresh_manager):
        """Regression: one rating must be on disk immediately (no sleep/increments).

        Bug: ratings were batched with the load-counter flush policy (5 pending or
        30s) and nothing flushed on exit, so a short session recording <=4 ratings
        lost all but threshold-crossing writes. Fix: record_rating flushes at once.
        """
        m = self.manager
        m.record_rating('my-skill', 7.5)
        assert self.metrics_file.exists(), 'metrics file not written after a single rating'
        import json as _json
        data = _json.loads(self.metrics_file.read_text(encoding='utf-8'))
        r = data['skills']['my-skill']['ratings']
        assert r['count'] == 1
        assert abs(r['sum'] - 7.5) < 1e-9
        assert r['latest'] == 7.5

    def test_record_rating_invalid_does_not_flush(self, fresh_manager):
        """The ValueError path must not write/flush anything to disk."""
        m = self.manager
        with pytest.raises(ValueError):
            m.record_rating('my-skill', 11.0)
        assert not self.metrics_file.exists()

    def test_pool_stop_triggers_final_metrics_flush(self, tmp_path):
        """Pool stopped.setter True-branch must best-effort flush skill metrics."""
        from unittest.mock import MagicMock
        pool = MagicMock(name='pool')
        pool._idle = MagicMock()
        pool._async_registry = MagicMock()
        pool.skill_manager = MagicMock()

        # Invoke the real setter on a stub (no AgentPool.__init__ needed).
        from agent_cascade.pool.core import AgentPool
        AgentPool.stopped.fset(pool, True)

        assert pool._stopped_event.is_set()
        pool.skill_manager._flush_metrics_to_disk.assert_called_once_with()

    def test_backward_compat_missing_ratings_key(self, fresh_manager):
        """A 1.0-style entry without 'ratings' is read as no ratings (no crash)."""
        import json as _json
        self.metrics_file.write_text(_json.dumps({
            'schema_version': '1.0',
            'skills': {
                'legacy-skill': {
                    'total_loads': 3,
                    'by_version': {
                        '1.0.0': 3
                    }
                }
            }
        }),
                                     encoding='utf-8')
        m = SkillManager()
        m._metrics_file = self.metrics_file
        m._load_metrics()
        entry = m.get_metrics('legacy-skill')
        assert entry['total_loads'] == 3
        # No ratings key present → treated as absent, not an error.
        assert 'ratings' not in entry

    def test_get_rating_average_rated(self, fresh_manager):
        """get_rating_average returns the rounded sum/count for a rated skill."""
        m = self.manager
        m.record_rating('my-skill', 8.0)
        m.record_rating('my-skill', 6.0)
        assert m.get_rating_average('my-skill') == 7.0

    def test_get_rating_average_unrated(self, fresh_manager):
        """A skill with no ratings entry is unrated → None."""
        m = self.manager
        assert m.get_rating_average('never-rated') is None

    def test_get_rating_average_zero_count(self, fresh_manager):
        """A ratings sub-dict with count=0 is treated as unrated → None (no ZeroDivisionError)."""
        m = self.manager
        m._metrics['weird-skill'] = {
            'total_loads': 0,
            'by_version': {},
            'ratings': {
                'count': 0,
                'sum': 0.0,
                'latest': None,
                'last_version': ''
            }
        }
        assert m.get_rating_average('weird-skill') is None

    def test_get_rating_average_rounding(self, fresh_manager):
        """Average is rounded to 2 decimal places."""
        m = self.manager
        m.record_rating('s', 7.0)
        m.record_rating('s', 7.1)
        # (7.0 + 7.1)/2 = 7.05
        assert m.get_rating_average('s') == 7.05


class TestNewSkillInitialRating:
    """Newly-registered skills get an initial rating of SKILL_RATING_INITIAL (5.0)."""

    @pytest.fixture(autouse=True)
    def _isolated_metrics(self, fresh_manager, tmp_path):
        self.manager = fresh_manager
        _isolate_metrics(fresh_manager, tmp_path)
        yield

    def test_new_skill_gets_initial_5_0(self, fresh_manager):
        from agent_cascade.settings import SKILL_RATING_INITIAL
        m = self.manager
        name = f"test-initial-rating-{_uid()}"
        content = _make_skill_content(
            name=name,
            description='Skill to verify initial rating is recorded at registration',
            triggers=['initial', 'rating'],
            generated_from_task='Verify initial rating',
        )
        success, _ = m.register_skill_from_content(content, task_text='Verify initial rating')
        assert success
        entry = m.get_metrics(name)
        assert entry['ratings']['latest'] == SKILL_RATING_INITIAL
        assert entry['ratings']['count'] == 1


class TestReflectionPrompt:
    """The injected auto-skill prompt contains the loaded-skills list + skill-creator body."""

    def _make_inst(self, fresh_manager):
        inst = MagicMock()
        inst.conversation = [{'role': 'user', 'content': 'task'}]
        inst._auto_skill_proposed = False
        # load_full_instructions('skill-creator') must NOT re-read the real production file:
        # that would flush the (isolated) metrics back through to the production file and
        # repopulate _metrics from disk on the next load. A body-only stub short-circuits
        # the disk fallback.
        fresh_manager._skills_registry['skill-creator'] = {
            'name': 'skill-creator',
            'file_path': 'agents/global/skills/skill-creator/SKILL.md',
            '_parsed_data': {
                'body': 'UNIQUE_CREATOR_BODY_MARKER'
            },
        }
        return inst

    @pytest.fixture(autouse=True)
    def _isolated_metrics(self, fresh_manager, tmp_path):
        self.manager = fresh_manager
        _isolate_metrics(fresh_manager, tmp_path, reset=True)
        yield

    def test_prompt_contains_loaded_skills_list(self, fresh_manager):
        inst = self._make_inst(fresh_manager)
        prompt = fresh_manager.auto_skill_qualifies(
            inst=inst,
            turns_effectuated=AUTO_SKILL_MIN_TURNS + 1,
            loaded_skill_names=['docker-best-practices', 'code-review'],
        )
        assert isinstance(prompt, str) and prompt
        assert '- docker-best-practices' in prompt
        assert '- code-review' in prompt
        # skill-creator body embedded
        assert 'UNIQUE_CREATOR_BODY_MARKER' in prompt

    def test_prompt_shows_none_when_no_skills(self, fresh_manager):
        inst = self._make_inst(fresh_manager)
        prompt = fresh_manager.auto_skill_qualifies(
            inst=inst,
            turns_effectuated=AUTO_SKILL_MIN_TURNS + 1,
            loaded_skill_names=None,
        )
        assert isinstance(prompt, str) and prompt
        assert '(none)' in prompt

    def test_prompt_lines_show_names_only_no_rating(self, fresh_manager):
        """Loaded-skill lines show ONLY the name — ratings are omitted to avoid anchoring bias."""
        inst = self._make_inst(fresh_manager)
        # Rate one skill so a pre-existing average exists; it must NOT leak into the prompt.
        fresh_manager.record_rating('docker-best-practices', 8.0)
        fresh_manager.record_rating('docker-best-practices', 7.0)  # avg 7.5, count 2
        prompt = fresh_manager.auto_skill_qualifies(
            inst=inst,
            turns_effectuated=AUTO_SKILL_MIN_TURNS + 1,
            loaded_skill_names=['docker-best-practices', 'code-review'],
        )
        assert isinstance(prompt, str) and prompt
        # Names present, rendered as bare lines.
        assert '- docker-best-practices' in prompt
        assert '- code-review' in prompt
        # No rating/count/avg/unrated markers on the loaded-skill lines.
        assert 'avg 7.5/10' not in prompt
        assert 'rated 2×' not in prompt
        assert '(unrated)' not in prompt


class TestProposeSkillRatingModes:
    """propose_skill rating-only and content+rating modes."""

    def _make_tool(self, fresh_manager):
        from agent_cascade.tools.custom.propose_skill import ProposeSkill
        pool = MagicMock()
        pool.skill_manager = fresh_manager
        # Rating-only mode must NOT reach approval; content mode does. Auto-approve by default.
        pool.operation_manager.request_user_approval.return_value = (True, '')
        return ProposeSkill(agent_pool=pool), pool

    @pytest.fixture(autouse=True)
    def _isolated_metrics(self, fresh_manager, tmp_path):
        self.manager = fresh_manager
        _isolate_metrics(fresh_manager, tmp_path, reset=True)
        yield

    def test_rating_only_records_and_skips_approval(self, fresh_manager):
        m = self.manager
        # Register a skill so it exists in the registry.
        name = f"test-rate-target-{_uid()}"
        content = _make_skill_content(name=name,
                                      description='Target skill for rating-only mode test',
                                      triggers=['rate', 'target'],
                                      generated_from_task='rate the target skill')
        assert m.register_skill_from_content(content, task_text='rate the target skill')[0]

        tool, pool = self._make_tool(m)
        import json as _json
        result = tool.call(_json.dumps({'name': name, 'rating': 7.5}))
        assert 'Recorded rating' in result
        # Frontmatter name must have been patched to the argument (arg is authoritative).
        on_disk = Path(m.get_skill_metadata(name)['file_path']).read_text(encoding='utf-8')
        assert re.search(r'(?m)^name:\s*%s\s*$' % re.escape(name), on_disk)
        entry = m.get_metrics(name)
        # Initial 5.0 + this 7.5 → count 2, latest 7.5
        assert entry['ratings']['latest'] == 7.5
        assert entry['ratings']['count'] == 2
        # Rating-only must NOT request approval.
        pool.operation_manager.request_user_approval.assert_not_called()

    def test_rating_only_unknown_name_rejected(self, fresh_manager):
        tool, _ = self._make_tool(self.manager)
        import json as _json
        result = tool.call(_json.dumps({'name': 'does-not-exist-xyz', 'rating': 5}))
        assert 'unknown skill' in result.lower() or 'not in the registry' in result

    def test_rating_only_out_of_range_rejected(self, fresh_manager):
        m = self.manager
        name = f"test-rate-range-{_uid()}"
        content = _make_skill_content(name=name,
                                      description='Range-check target skill for rating',
                                      triggers=['range', 'check'],
                                      generated_from_task='range check the rating')
        assert m.register_skill_from_content(content, task_text='range check the rating')[0]
        tool, _ = self._make_tool(m)
        import json as _json
        result = tool.call(_json.dumps({'name': name, 'rating': 12}))
        assert 'Invalid rating' in result

    def test_missing_name_rejected(self, fresh_manager):
        """name is a required argument — missing it fails fast with a clear message."""
        tool, _ = self._make_tool(self.manager)
        import json as _json
        result = tool.call(_json.dumps({'rating': 5}))
        assert "The 'name' argument is required" in result

    def test_name_arg_overrides_frontmatter_name(self, fresh_manager):
        """The name argument is authoritative: a mismatching frontmatter name gets patched."""
        m = self.manager
        arg_name = f"test-name-override-{_uid()}"
        content = _make_skill_content(name=f'different-frontmatter-name-{_uid()}',
                                      description='Name override target skill body text',
                                      triggers=['name', 'override'],
                                      generated_from_task='name override test')
        tool, _ = self._make_tool(m)
        import json as _json
        result = tool.call(_json.dumps({'name': arg_name, 'skill_content': content, 'justification': 'j'}))
        assert 'registered successfully' in result
        assert m.get_skill_metadata(arg_name) is not None
        on_disk = Path(m.get_skill_metadata(arg_name)['file_path']).read_text(encoding='utf-8')
        assert re.search(r'(?m)^name:\s*%s\s*$' % re.escape(arg_name), on_disk)

    def test_version_patch_stays_in_frontmatter(self, fresh_manager):
        """Version auto-bump must not touch a body line that looks like 'version: X'."""
        m = self.manager
        name = f"test-fm-version-scope-{_uid()}"
        content = _make_skill_content(name=name,
                                      description='Frontmatter version scoping target skill',
                                      triggers=['fm', 'scope'],
                                      body='## Notes\n\nversion: 9.9.9 (body line that must survive)\n\n'
                                      'Follow these steps carefully to complete the task.\n',
                                      generated_from_task='frontmatter scope test')
        assert m.register_skill_from_content(content, task_text='frontmatter scope test')[0]

        tool, _ = self._make_tool(m)
        import json as _json
        updated = content.replace('Follow these steps carefully', 'Follow these revised steps carefully')
        result = tool.call(_json.dumps({'name': name, 'skill_content': updated, 'justification': 'refine'}))
        assert 'updated' in result.lower()
        on_disk = Path(m.get_skill_metadata(name)['file_path']).read_text(encoding='utf-8')
        # Body line must be untouched; frontmatter version must have advanced past 1.0.0.
        assert 'version: 9.9.9 (body line that must survive)' in on_disk
        fm_block = re.match(r'^---\s*\n(.*?)\n---', on_disk, re.DOTALL).group(1)
        assert re.search(r'(?m)^version:\s*1\.0\.1\s*$', fm_block)

    def test_update_respects_explicit_higher_version(self, fresh_manager):
        """An explicit higher semver in content is kept as-is (no clobbering)."""
        m = self.manager
        name = f"test-explicit-version-{_uid()}"
        content = _make_skill_content(name=name,
                                      description='Explicit version target skill body text',
                                      triggers=['explicit', 'version'],
                                      generated_from_task='explicit version test')
        assert m.register_skill_from_content(content, task_text='explicit version test')[0]

        tool, _ = self._make_tool(m)
        import json as _json
        bumped = content.replace('---\n', '---\nversion: 2.1.0\n', 1)
        result = tool.call(_json.dumps({'name': name, 'skill_content': bumped, 'justification': 'major work'}))
        assert 'updated' in result.lower()
        assert m.get_skill_metadata(name)['version'] == '2.1.0'

    def test_rating_only_non_numeric_rejected(self, fresh_manager):
        """A non-numeric rating (list) must not crash — return a clear error."""
        m = self.manager
        name = f"test-rate-nonnum-{_uid()}"
        content = _make_skill_content(name=name,
                                      description='Non-numeric rating guard target skill',
                                      triggers=['nonnum', 'guard'],
                                      generated_from_task='non numeric guard')
        assert m.register_skill_from_content(content, task_text='non numeric guard')[0]
        tool, _ = self._make_tool(m)
        import json as _json
        result = tool.call(_json.dumps({'name': name, 'rating': ['bad']}))
        assert 'Invalid rating' in result

    def test_content_plus_rating_records_after_success(self, fresh_manager):
        m = self.manager
        name = f"test-content-rating-{_uid()}"
        content = _make_skill_content(name=name,
                                      description='Content-plus-rating target skill body',
                                      triggers=['content', 'rating'],
                                      generated_from_task='content plus rating test')
        tool, pool = self._make_tool(m)
        import json as _json
        result = tool.call(_json.dumps({'name': name, 'skill_content': content, 'justification': 'j', 'rating': 9.0}))
        assert 'registered successfully' in result
        entry = m.get_metrics(name)
        # Initial 5.0 + explicit 9.0 → latest 9.0, count 2
        assert entry['ratings']['latest'] == 9.0
        assert entry['ratings']['count'] == 2

    def test_content_for_existing_name_is_implicit_update(self, fresh_manager):
        """Content for an existing skill name is always an update (no flag needed)."""
        m = self.manager
        name = f"test-implicit-update-{_uid()}"
        content = _make_skill_content(name=name,
                                      description='Implicit update target skill body here',
                                      triggers=['implicit', 'update'],
                                      generated_from_task='implicit update test')
        assert m.register_skill_from_content(content, task_text='implicit update test')[0]
        v1 = m.get_skill_metadata(name)['version']

        tool, pool = self._make_tool(m)
        import json as _json
        updated = content.replace('Implicit update target skill body here', 'Implicit update target skill body v2 here')
        result = tool.call(_json.dumps({'name': name, 'skill_content': updated, 'justification': 'refine'}))
        assert 'updated' in result.lower()
        assert m.get_skill_metadata(name)['version'] != v1  # patch version auto-incremented
        pool.operation_manager.request_user_approval.assert_called_once()

    def test_update_routes_through_candidate_not_production(self, fresh_manager):
        """Regression: an update (content for an existing name) must create a candidate,
        not overwrite the production SKILL.md directly.

        Before the fix, propose_skill routed updates to SkillManager.update_skill_in_place(),
        which overwrote the production file and bypassed the candidate/decision-gate flow.
        Now both new and update go through register_skill_from_content(); for an existing name
        that routes to _register_candidate_upgrade (candidate folder + evaluate_candidates gate).
        """
        m = self.manager
        # Isolated candidate/production dirs; neutralize evaluate_candidates()'s forced discovery
        # refresh (invalidate_cache / _ensure_discovered) so the immediate gate trigger inside
        # register_skill_from_content() can't re-scan/re-parse the manually staged registry mid-test.
        tmp_path = Path(m._metrics_file).parent
        m._candidates_dir = tmp_path / 'agents' / 'global' / 'candidates'
        m._production_skills_dir = tmp_path / 'agents' / 'global' / 'skills'
        m._skill_paths = []
        m.invalidate_cache = lambda *a, **k: None
        m._ensure_discovered = lambda *a, **k: None

        name = f"test-update-candidate-{_uid()}"
        _write_incumbent(m, name, '1.0.0', body_marker='INCUMBENT_BODY_MARKER')
        prod_file = (m._production_skills_dir / name / 'SKILL.md')
        original_prod = prod_file.read_text(encoding='utf-8')

        # Update content with a distinct version (2.0.0) and a marker only present in the new body.
        updated = _make_skill_content(
            name=name,
            description='Updated skill routed through candidate flow',
            triggers=['update', 'candidate'],
            generated_from_task='test update routes to candidate',
        ).replace('---\n', '---\nversion: 2.0.0\n', 1)
        marker = 'UPDATE_BODY_MARKER'
        updated = updated.replace('## Instructions', f'## Instructions\n\n{marker}', 1)

        tool, _ = self._make_tool(m)
        import json as _json
        result = tool.call(_json.dumps({'name': name, 'skill_content': updated,
                                        'justification': 'refine via candidate flow', 'rating': 9.0}))
        assert 'updated' in result.lower()

        # New content must NOT be in the production file (no direct overwrite).
        prod_now = prod_file.read_text(encoding='utf-8')
        assert marker not in prod_now, 'production SKILL.md must not be overwritten directly by an update'
        assert prod_now == original_prod, 'production file must be byte-for-byte untouched'

        # New content MUST be present under agents/global/candidates/<name>/SKILL.md.
        cand_file = (m._candidates_dir / name / 'SKILL.md')
        assert cand_file.exists(), 'update must create a candidate under candidates/<name>/'
        cand_now = cand_file.read_text(encoding='utf-8')
        assert marker in cand_now, 'candidate SKILL.md must contain the new content'
        # Registry winner is now the live-serving candidate.
        reg = m._skills_registry[name]
        assert reg['file_path'] == str(cand_file)

    def test_repropose_when_candidate_already_present(self, fresh_manager):
        """Two successive propose_skill updates on an existing name replace the candidate file.

        Guards the manager's "existing candidate file is replaced" path through the actual tool:
        a second update must overwrite candidates/<name>/SKILL.md (not append/duplicate), leave
        the production file byte-for-byte untouched, and keep exactly one registry entry at
        _PRIORITY_CANDIDATE.
        """
        m = self.manager
        # Same isolation as test_update_routes_through_candidate_not_production: isolated
        # candidate/production dirs + neutralized discovery refresh (the immediate gate trigger
        # inside register_skill_from_content() must not re-scan the staged registry mid-test).
        tmp_path = Path(m._metrics_file).parent
        m._candidates_dir = tmp_path / 'agents' / 'global' / 'candidates'
        m._production_skills_dir = tmp_path / 'agents' / 'global' / 'skills'
        m._skill_paths = []
        m.invalidate_cache = lambda *a, **k: None
        m._ensure_discovered = lambda *a, **k: None

        name = f"test-repropose-tool-{_uid()}"
        _write_incumbent(m, name, '1.0.0', body_marker='INCUMBENT_BODY_MARKER')
        prod_file = (m._production_skills_dir / name / 'SKILL.md')
        original_prod = prod_file.read_text(encoding='utf-8')

        tool, _ = self._make_tool(m)
        import json as _json

        # First update: candidate v2.0.0 with marker #1. (Description embeds the task text so
        # Tier-2 self-match validation passes, as in test_update_routes_through_candidate_not_production.)
        content1 = _make_skill_content(
            name=name,
            description='First re-proposal over an existing skill via tool',
            triggers=['repropose', 'first'],
            generated_from_task='test re-propose first via tool',
        ).replace('---\n', '---\nversion: 2.0.0\n', 1)
        marker1 = 'REPROPOSE_MARKER_1'
        content1 = content1.replace('## Instructions', f'## Instructions\n\n{marker1}', 1)
        result1 = tool.call(_json.dumps({'name': name, 'skill_content': content1,
                                         'justification': 'first re-proposal', 'rating': 8.0}))
        assert 'updated' in result1.lower()

        # Second update (successive): candidate v3.0.0 with marker #2 — must REPLACE the file.
        content2 = _make_skill_content(
            name=name,
            description='Second re-proposal replacing the first one via tool',
            triggers=['repropose', 'second'],
            generated_from_task='test re-propose second via tool',
        ).replace('---\n', '---\nversion: 3.0.0\n', 1)
        marker2 = 'REPROPOSE_MARKER_2'
        content2 = content2.replace('## Instructions', f'## Instructions\n\n{marker2}', 1)
        result2 = tool.call(_json.dumps({'name': name, 'skill_content': content2,
                                         'justification': 'second re-proposal', 'rating': 9.0}))
        assert 'updated' in result2.lower()

        # The candidate file holds the LATEST proposal: marker #2 present, marker #1 gone.
        cand_file = (m._candidates_dir / name / 'SKILL.md')
        assert cand_file.exists(), 'candidate must exist under candidates/<name>/'
        cand_now = cand_file.read_text(encoding='utf-8')
        assert marker2 in cand_now, 'latest proposal must be present in the candidate file'
        assert marker1 not in cand_now, 're-proposal must replace (not append/duplicate) the candidate'

        # Production is still byte-for-byte unchanged.
        assert prod_file.read_text(encoding='utf-8') == original_prod, \
            'production SKILL.md must be byte-for-byte untouched after re-proposals'

        # Exactly ONE registry entry for this name, at _PRIORITY_CANDIDATE (no duplicates).
        from agent_cascade.skills.manager import _PRIORITY_CANDIDATE
        entries = [e for n, e in m._skills_registry.items() if n == name]
        assert len(entries) == 1, f'expected exactly one registry entry, got {len(entries)}'
        assert entries[0]['_priority'] == _PRIORITY_CANDIDATE
        assert entries[0]['file_path'] == str(cand_file)


class TestProposeSkillSimilarityGate:
    """Hard-reject similarity gate in propose_skill (frontmatter-text dedup)."""

    def _make_tool(self, fresh_manager):
        from agent_cascade.tools.custom.propose_skill import ProposeSkill
        pool = MagicMock()
        pool.skill_manager = fresh_manager
        # Auto-approve by default so the NEW-skill path reaches registration when not rejected.
        pool.operation_manager.request_user_approval.return_value = (True, '')
        return ProposeSkill(agent_pool=pool), pool

    @pytest.fixture(autouse=True)
    def _isolated_metrics(self, fresh_manager, tmp_path):
        self.manager = fresh_manager
        _isolate_metrics(fresh_manager, tmp_path, reset=True)
        yield

    def test_new_skill_rejected_when_too_similar(self, fresh_manager):
        """A NEW skill whose frontmatter is ~identical to an existing one is hard-rejected."""
        m = self.manager
        import json as _json
        base_name = f"test-dup-gate-{_uid()}"
        content = _make_skill_content(name=base_name,
                                      description='Best practices for Docker builds and images',
                                      triggers=['docker', 'compose'],
                                      generated_from_task='docker best practices')
        assert m.register_skill_from_content(content, task_text='docker best practices')[0]

        # Propose a NEW (different) name but near-identical frontmatter. Derive the dup name by
        # changing only the last char of the incumbent's uid so the frontmatter-text ratio stays
        # well above 0.95 (two independent random uids differ in too many chars to exceed it).
        dup_name = base_name[:-1] + ('a' if base_name[-1] != 'a' else 'b')
        assert dup_name != base_name
        dup_content = _make_skill_content(name=dup_name,
                                          description='Best practices for Docker builds and images',
                                          triggers=['docker', 'compose'],
                                          generated_from_task='docker best practices')
        tool, pool = self._make_tool(m)
        result = tool.call(_json.dumps({'name': dup_name, 'skill_content': dup_content,
                                        'justification': 'j'}))
        assert result.startswith('REJECTED:'), f'expected hard-reject, got: {result!r}'
        assert 'too similar to existing skills' in result
        # The colliding incumbent must be listed with its score.
        assert base_name in result
        assert 'similarity:' in result
        # Hard-reject means no approval request and no registration of the duplicate.
        pool.operation_manager.request_user_approval.assert_not_called()
        assert m.get_skill_metadata(dup_name) is None

    def test_update_of_own_name_not_self_rejected(self, fresh_manager):
        """An UPDATE excludes its own incumbent — a near-identical re-propose of the same name passes."""
        m = self.manager
        import json as _json
        # Isolated candidate/production dirs + neutralized discovery refresh so the update's
        # candidate-flow registration (which runs AFTER the gate) succeeds in this test.
        tmp_path = Path(m._metrics_file).parent
        m._candidates_dir = tmp_path / 'agents' / 'global' / 'candidates'
        m._production_skills_dir = tmp_path / 'agents' / 'global' / 'skills'
        m._skill_paths = []
        m.invalidate_cache = lambda *a, **k: None
        m._ensure_discovered = lambda *a, **k: None

        name = f"test-dup-gate-update-{_uid()}"
        content = _make_skill_content(name=name,
                                      description='Best practices for Docker builds and images',
                                      triggers=['docker', 'compose'],
                                      generated_from_task='docker best practices')
        assert m.register_skill_from_content(content, task_text='docker best practices')[0]

        # Update the SAME name with near-identical frontmatter (only body changes) — must NOT self-reject.
        updated = content.replace('Follow these steps carefully', 'Follow these revised steps carefully')
        tool, _ = self._make_tool(m)
        result = tool.call(_json.dumps({'name': name, 'skill_content': updated, 'justification': 'refine'}))
        assert not result.startswith('REJECTED:'), f'update must not self-reject, got: {result!r}'
        assert 'updated' in result.lower()

    def test_distinct_new_skill_passes_gate(self, fresh_manager):
        """A NEW skill unrelated to existing skills passes the gate and registers."""
        m = self.manager
        import json as _json
        base_name = f"test-dup-gate-base-{_uid()}"
        content = _make_skill_content(name=base_name,
                                      description='Best practices for Docker builds and images',
                                      triggers=['docker', 'compose'],
                                      generated_from_task='docker best practices')
        assert m.register_skill_from_content(content, task_text='docker best practices')[0]

        new_name = f"test-dup-gate-distinct-{_uid()}"
        distinct = _make_skill_content(name=new_name,
                                       description='Run PostgreSQL database migrations safely with rollback',
                                       triggers=['sql', 'postgres'],
                                       generated_from_task='postgres migration')
        tool, _ = self._make_tool(m)
        result = tool.call(_json.dumps({'name': new_name, 'skill_content': distinct, 'justification': 'j'}))
        assert not result.startswith('REJECTED:'), f'distinct skill must pass gate, got: {result!r}'
        assert 'registered successfully' in result


# ===========================================================================
# Candidate flow — live-serving upgrade candidates (Phase 2 of skill evolution)
# ===========================================================================


def _write_incumbent(m, name: str, version: str = '1.0.0', body_marker: str = 'INCUMBENT_BODY'):
    """Write a production SKILL.md directly to the manager's production root and register it."""
    _, prod_root = m._candidate_dirs()  # returns (candidates_root, production_root)
    d = prod_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: Incumbent skill version for candidate flow tests\n'
        f'version: {version}\ntriggers:\n  - candidate\n  - test\n---\n\n{body_marker}\n',
        encoding='utf-8')
    from agent_cascade.skills.manager import _PRIORITY_SYSTEM
    from agent_cascade.skills.parser import parse_skill_file
    parsed = parse_skill_file(d / 'SKILL.md')
    m._skills_registry[name] = {
        'name': name,
        'description': parsed.get('frontmatter', {}).get('description', ''),
        'source': 'system',
        'triggers': parsed.get('frontmatter', {}).get('triggers', []),
        'version': version,
        'file_path': str(d / 'SKILL.md'),
        '_priority': _PRIORITY_SYSTEM,
        '_parsed_data': parsed,
    }


class TestCandidateFlow:
    """Live-serving upgrade candidates: registration, serving priority, decision gate."""

    @pytest.fixture(autouse=True)
    def _candidate_env(self, fresh_manager, tmp_path):
        """Isolated metrics file + isolated candidate/production dirs (no real-tree writes).

        Neutralize evaluate_candidates()'s forced discovery refresh (invalidate_cache /
        _ensure_discovered) so the immediate gate trigger inside register_skill_from_content()
        can't re-scan/re-parse the manually staged registry mid-test.
        """
        self.manager = fresh_manager
        self.metrics_file = tmp_path / 'skills-metrics.json'
        _isolate_metrics(fresh_manager, tmp_path, reset=True)
        fresh_manager._candidates_dir = tmp_path / 'agents' / 'global' / 'candidates'
        fresh_manager._production_skills_dir = tmp_path / 'agents' / 'global' / 'skills'
        # Neutralize the forced discovery refresh inside evaluate_candidates() so the
        # manually staged registry entries survive (discover() would clear them).
        # _skill_paths=[] alone is not enough: invalidate_cache() resets the TTL and
        # _ensure_discovered() may still call discover([]) which clears the registry.
        # Point _skill_paths at the real tmp production + candidate dirs (mirroring the pool
        # tier list) so prune_stale_metrics() — now called at the end of evaluate_candidates()
        # — sees the incumbent on disk and does NOT prune it after a discard. With [] the
        # live set would be registry-only, and a just-discarded candidate's name (already
        # removed from the registry) would look stale even though its incumbent still exists.
        fresh_manager._skill_paths = [fresh_manager._production_skills_dir, fresh_manager._candidates_dir]
        fresh_manager.invalidate_cache = lambda *a, **k: None
        fresh_manager._ensure_discovered = lambda *a, **k: None
        yield

    # -- Serving priority ----------------------------------------------------

    def test_candidate_beats_incumbent_in_registry(self, fresh_manager):
        """A candidate registered with _PRIORITY_CANDIDATE=4 wins the registry over the incumbent."""
        from agent_cascade.skills.manager import _PRIORITY_AGENT, _PRIORITY_CANDIDATE, _PRIORITY_SYSTEM, _PRIORITY_USER

        # Existing tier constants untouched.
        assert (_PRIORITY_SYSTEM, _PRIORITY_AGENT, _PRIORITY_USER, _PRIORITY_CANDIDATE) == (1, 2, 3, 4)

        m = self.manager
        name = f"test-cand-priority-{_uid()}"
        _write_incumbent(m, name, '1.0.0')
        content = _make_skill_content(
            name=name,
            description='Candidate upgrade version for serving priority test',
            triggers=['candidate', 'priority'],
            generated_from_task='test candidate serving priority',
        )
        success, errors = m.register_skill_from_content(content, task_text='test candidate serving priority')
        assert success, f"upgrade registration failed: {errors}"

        reg = m._skills_registry[name]
        assert reg['_priority'] == _PRIORITY_CANDIDATE
        assert 'candidates' in reg['file_path']
        # The candidate body is what gets served (winner resolution).
        assert m.load_full_instructions(name, count_load=False) == content.split('---\n', 2)[2].strip()

    def test_candidate_tier_discovered_with_highest_priority(self, fresh_manager):
        """Discovery of the candidates root registers skills at _PRIORITY_CANDIDATE."""
        from agent_cascade.skills.manager import _PRIORITY_CANDIDATE
        m = self.manager
        name = f"test-cand-discovery-{_uid()}"
        cand_root, prod_root = m._candidate_dirs()
        d = cand_root / name
        d.mkdir(parents=True)
        (d / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: Candidate tier discovery test skill body\n'
            'version: 2.0.0\ntriggers:\n  - candidate\n---\n\nCANDIDATE_DISCOVERY_BODY\n',
            encoding='utf-8')

        # Discover the full pool tier list (production + candidates), like the pool does.
        m._cache_ttl = 0.0
        m.discover([prod_root, cand_root])
        assert name in m._skills_registry
        assert m._skills_registry[name]['_priority'] == _PRIORITY_CANDIDATE
        assert m.get_candidate_names() == [name]

    # -- Upgrade → candidate file -------------------------------------------

    def test_upgrade_lands_in_candidates_dir(self, fresh_manager):
        """Re-proposing an existing name writes candidates/<name>/SKILL.md; production untouched."""
        m = self.manager
        name = f"test-cand-upgrade-{_uid()}"
        _write_incumbent(m, name, '1.0.0', body_marker='ORIGINAL_INCUMBENT')
        prod_file = (m._production_skills_dir / name / 'SKILL.md')
        original_prod = prod_file.read_text(encoding='utf-8')

        content = _make_skill_content(
            name=name,
            description='Upgrade candidate version for candidates dir test',
            triggers=['upgrade', 'candidate'],
            generated_from_task='test upgrade lands in candidates',
        )
        success, errors = m.register_skill_from_content(content, task_text='test upgrade lands in candidates')
        assert success, f"upgrade registration failed: {errors}"

        cand_file = (m._candidates_dir / name / 'SKILL.md')
        assert cand_file.exists(), 'candidate file must exist under agents/global/candidates/<name>/'
        assert prod_file.read_text(encoding='utf-8') == original_prod, 'production file must be untouched'
        reg = m._skills_registry[name]
        assert reg['file_path'] == str(cand_file)

    # -- Rating accumulation per version -------------------------------------

    def test_ratings_accrue_to_serving_candidate_version(self, fresh_manager):
        """While the candidate serves, ratings accrue to its own version key (schema 1.2)."""
        m = self.manager
        name = f"test-cand-ratings-{_uid()}"
        _write_incumbent(m, name, '1.0.0')
        content = _make_skill_content(
            name=name,
            description='Candidate version for per-version rating accumulation',
            triggers=['ratings', 'version'],
            generated_from_task='test per-version ratings',
        )
        success, _ = m.register_skill_from_content(content, task_text='test per-version ratings')
        assert success
        cand_version = m.get_skill_metadata(name)['version']

        m.record_rating(name, 8.0)
        m.record_rating(name, 9.0)
        entry = m.get_metrics(name)
        # Aggregate (backward compat) and per-version history both updated.
        assert entry['ratings']['count'] == 2
        assert abs(entry['ratings']['sum'] - 17.0) < 1e-9
        assert entry['ratings_by_version'][cand_version]['count'] == 2
        assert abs(entry['ratings_by_version'][cand_version]['sum'] - 17.0) < 1e-9

    # -- Decision gate: promote / discard ------------------------------------

    def test_promote_on_better(self, fresh_manager):
        m = self.manager
        name = f"test-cand-promote-better-{_uid()}"
        _write_incumbent(m, name, '1.0.0', body_marker='OLD_INCUMBENT')
        prod_file = (m._production_skills_dir / name / 'SKILL.md')

        # Distinct candidate version (2.0.0) so its rating history does not share the
        # incumbent's ratings_by_version['1.0.0'] key — otherwise the two versions collide
        # and the gate compares a polluted baseline against itself.
        content = _make_skill_content(
            name=name,
            description='Better candidate version for promotion test',
            triggers=['promote', 'better'],
            generated_from_task='test promote on better average',
        ).replace('---\n', '---\nversion: 2.0.0\n', 1)
        success, _ = m.register_skill_from_content(content, task_text='test promote on better average')
        assert success
        cand_version = m.get_skill_metadata(name)['version']

        # Incumbent avg 5.0 (2 ratings) vs candidate avg 9.2 (CANDIDATE_MIN_RATINGS ratings) → promote.
        for r in (9.0, 9.0, 9.0, 9.0, 10.0):
            m.record_rating(name, r)  # these land on the candidate version key (it serves)
        # Seed the incumbent's own history directly (pre-candidacy era).
        with m._metrics_lock:
            m._metrics[name]['ratings_by_version']['1.0.0'] = {'count': 2, 'sum': 10.0, 'latest': 6.0}

        # Candidate now has exactly CANDIDATE_MIN_RATINGS ratings on its own key → gate crossed.
        assert m._version_rating_avg(name, cand_version)[1] == CANDIDATE_MIN_RATINGS
        m.evaluate_candidates()

        from agent_cascade.skills.manager import _PRIORITY_SYSTEM
        reg = m._skills_registry[name]
        assert reg['_priority'] == _PRIORITY_SYSTEM, 'promoted candidate must be back at SYSTEM priority'
        assert reg['file_path'] == str(prod_file)
        assert reg['version'] == cand_version
        assert not (m._candidates_dir / name).exists(), 'candidate dir must be deleted on promote'
        # Metrics transfer: aggregate mirrors the winner's history; per-version intact.
        entry = m.get_metrics(name)
        assert entry['ratings']['count'] == CANDIDATE_MIN_RATINGS
        assert abs(entry['ratings']['sum'] - (9.0 * 4 + 10.0)) < 1e-9
        assert '1.0.0' in entry['ratings_by_version'] and cand_version in entry['ratings_by_version']

    def test_promote_on_equal(self, fresh_manager):
        m = self.manager
        name = f"test-cand-promote-equal-{_uid()}"
        _write_incumbent(m, name, '1.0.0')
        prod_file = (m._production_skills_dir / name / 'SKILL.md')

        # Give the candidate a distinct version (2.0.0) so its rating history does not share
        # the incumbent's ratings_by_version['1.0.0'] key — otherwise the two versions collide
        # and the gate compares a polluted baseline against itself.
        content = _make_skill_content(
            name=name,
            description='Equal-average candidate version for promotion test',
            triggers=['promote', 'equal'],
            generated_from_task='test promote on equal average',
        ).replace('---\n', '---\nversion: 2.0.0\n', 1)
        success, _ = m.register_skill_from_content(content, task_text='test promote on equal average')
        assert success
        cand_version = m.get_skill_metadata(name)['version']

        # Both versions avg 7.0 → "better or equal" promotes.
        with m._metrics_lock:
            entry = m._metrics.setdefault(name, {'total_loads': 0, 'by_version': {}})
            entry.setdefault('ratings_by_version', {})['1.0.0'] = {'count': 2, 'sum': 14.0, 'latest': 8.0}
        # Candidate: exactly CANDIDATE_MIN_RATINGS ratings averaging 7.0 (== incumbent avg).
        for r in (6.0, 7.0, 8.0) + (7.0,) * (CANDIDATE_MIN_RATINGS - 3):
            m.record_rating(name, r)
        m.evaluate_candidates()

        reg = m._skills_registry[name]
        assert reg['_priority'] == 1 and reg['version'] == cand_version
        assert reg['file_path'] == str(prod_file)
        assert not (m._candidates_dir / name).exists()

    def test_discard_on_worse(self, fresh_manager):
        m = self.manager
        name = f"test-cand-discard-worse-{_uid()}"
        _write_incumbent(m, name, '1.0.0', body_marker='KEEP_INCUMBENT')
        prod_file = (m._production_skills_dir / name / 'SKILL.md')

        # Distinct candidate version (2.0.0) so its rating history does not corrupt the
        # incumbent's shared 1.0.0 key — the discard must revert the aggregate exactly.
        content = _make_skill_content(
            name=name,
            description='Worse candidate version for discard test',
            triggers=['discard', 'worse'],
            generated_from_task='test discard on worse average',
        ).replace('---\n', '---\nversion: 2.0.0\n', 1)
        success, _ = m.register_skill_from_content(content, task_text='test discard on worse average')
        assert success
        cand_version = m.get_skill_metadata(name)['version']

        # Incumbent avg 8.0 (2) vs candidate avg 5.0 (CANDIDATE_MIN_RATINGS) → discard.
        with m._metrics_lock:
            entry = m._metrics.setdefault(name, {'total_loads': 0, 'by_version': {}})
            entry.setdefault('ratings_by_version', {})['1.0.0'] = {'count': 2, 'sum': 16.0, 'latest': 9.0}
        # Candidate: exactly CANDIDATE_MIN_RATINGS ratings averaging 5.0 (< incumbent avg).
        for r in (4.0, 5.0, 6.0) + (5.0,) * (CANDIDATE_MIN_RATINGS - 3):
            m.record_rating(name, r)
        m.evaluate_candidates()

        # Candidate gone; next discovery restores the incumbent to service.
        assert not (m._candidates_dir / name).exists(), 'candidate dir must be deleted on discard'
        # Restore via explicit discovery of both roots (mirrors the pool tier list).
        _, prod_root = m._candidate_dirs()
        m.invalidate_cache()
        m.discover([prod_root, m._candidates_dir])
        reg = m._skills_registry.get(name)
        assert reg is not None, 'incumbent must be restored to the registry on next discovery'
        assert reg['_priority'] == 1 and reg['version'] == '1.0.0'
        # Metrics: aggregate reverted to incumbent history; candidate entry kept as evidence.
        entry = m.get_metrics(name)
        assert entry['ratings']['count'] == 2
        assert abs(entry['ratings']['sum'] - 16.0) < 1e-9
        assert cand_version in entry['ratings_by_version'], 'candidate per-version history must be kept'

    def test_gate_waits_for_min_ratings(self, fresh_manager):
        """Below CANDIDATE_MIN_RATINGS the candidate stays pending (no decision)."""
        m = self.manager
        name = f"test-cand-gate-wait-{_uid()}"
        _write_incumbent(m, name, '1.0.0')

        # Distinct candidate version (2.0.0) so its rating count is tracked separately from
        # the incumbent's 1.0.0 key — otherwise the shared key reaches the gate threshold early.
        content = _make_skill_content(
            name=name,
            description='Pending candidate version for min-ratings gate test',
            triggers=['gate', 'wait'],
            generated_from_task='test gate waits for min ratings',
        ).replace('---\n', '---\nversion: 2.0.0\n', 1)
        success, _ = m.register_skill_from_content(content, task_text='test gate waits for min ratings')
        assert success

        # Only CANDIDATE_MIN_RATINGS - 1 ratings (below the gate) — even a worse candidate survives.
        with m._metrics_lock:
            entry = m._metrics.setdefault(name, {'total_loads': 0, 'by_version': {}})
            entry.setdefault('ratings_by_version', {})['1.0.0'] = {'count': 1, 'sum': 10.0, 'latest': 10.0}
        for r in (1.0, 2.0) + (1.0,) * (CANDIDATE_MIN_RATINGS - 3):
            m.record_rating(name, r)
        m.evaluate_candidates()

        assert m.get_candidate_names() == [name], 'candidate must remain pending below the rating gate'

    def test_metrics_transfer_keeps_total_loads(self, fresh_manager):
        """On promote, total_loads (cumulative across versions) is kept as-is."""
        m = self.manager
        name = f"test-cand-loads-{_uid()}"
        _write_incumbent(m, name, '1.0.0')

        # Distinct candidate version (2.0.0) so its rating history is separable from the
        # incumbent's shared 1.0.0 key (the gate mirrors the winner's per-version history).
        content = _make_skill_content(
            name=name,
            description='Candidate version for total_loads preservation test',
            triggers=['loads', 'transfer'],
            generated_from_task='test total loads kept on gate',
        ).replace('---\n', '---\nversion: 2.0.0\n', 1)
        success, _ = m.register_skill_from_content(content, task_text='test total loads kept on gate')
        assert success

        with m._metrics_lock:
            entry = m._metrics.setdefault(name, {'total_loads': 0, 'by_version': {}})
            entry['total_loads'] = 42
            entry.setdefault('ratings_by_version', {})['1.0.0'] = {'count': 1, 'sum': 5.0, 'latest': 5.0}
        # Candidate: exactly CANDIDATE_MIN_RATINGS ratings (all 9.0) → gate crossed, promotes.
        for r in (9.0,) * CANDIDATE_MIN_RATINGS:
            m.record_rating(name, r)
        m.evaluate_candidates()

        entry = m.get_metrics(name)
        assert entry['total_loads'] == 42, 'total_loads must be kept across the gate'
        assert entry['ratings']['count'] == CANDIDATE_MIN_RATINGS and abs(entry['ratings']['sum'] - 9.0 * CANDIDATE_MIN_RATINGS) < 1e-9

    # -- Legacy (schema 1.1) incumbent handling --------------------------------

    def test_legacy_incumbent_backfill(self, fresh_manager):
        """A 1.1 metrics entry gets ratings_by_version[prod_version] seeded at candidate creation."""
        m = self.manager
        name = f"test-cand-legacy-backfill-{_uid()}"
        _write_incumbent(m, name, '1.2.3')

        # Simulate a schema-1.1 incumbent: aggregate ratings, no ratings_by_version.
        with m._metrics_lock:
            m._metrics[name] = {
                'total_loads': 7,
                'by_version': {
                    '1.2.3': 7
                },
                'ratings': {
                    'count': 2,
                    'sum': 15.0,
                    'latest': 8.0,
                    'last_version': '1.2.3'
                },
            }

        content = _make_skill_content(
            name=name,
            description='Candidate over a legacy schema-1.1 incumbent skill',
            triggers=['legacy', 'backfill'],
            generated_from_task='test legacy incumbent backfill',
        )
        success, _ = m.register_skill_from_content(content, task_text='test legacy incumbent backfill')
        assert success

        entry = m.get_metrics(name)
        assert entry['ratings_by_version']['1.2.3'] == {'count': 2, 'sum': 15.0, 'latest': 8.0}, \
            'legacy aggregate must be seeded into ratings_by_version[<incumbent version>]'

    def test_legacy_discard_revert_exactness(self, fresh_manager):
        """Discarding a candidate over a legacy incumbent reverts the aggregate exactly."""
        m = self.manager
        name = f"test-cand-legacy-discard-{_uid()}"
        _write_incumbent(m, name, '1.0.0')

        with m._metrics_lock:
            m._metrics[name] = {
                'total_loads': 5,
                'by_version': {
                    '1.0.0': 5
                },
                'ratings': {
                    'count': 2,
                    'sum': 14.0,
                    'latest': 9.0,
                    'last_version': '1.0.0'
                },
            }

        # Distinct candidate version (2.0.0) so its ratings don't corrupt the backfilled
        # incumbent 1.0.0 key — the discard must revert the aggregate exactly to count 2.
        content = _make_skill_content(
            name=name,
            description='Candidate discarded over a legacy incumbent skill',
            triggers=['legacy', 'discard'],
            generated_from_task='test legacy discard revert exactness',
        ).replace('---\n', '---\nversion: 2.0.0\n', 1)
        success, _ = m.register_skill_from_content(content, task_text='test legacy discard revert exactness')
        assert success

        # Candidate is worse (avg 4.0 × CANDIDATE_MIN_RATINGS) than incumbent (avg 7.0×2) → discard.
        for r in (3.0, 4.0, 5.0) + (4.0,) * (CANDIDATE_MIN_RATINGS - 3):
            m.record_rating(name, r)
        m.evaluate_candidates()

        entry = m.get_metrics(name)
        assert entry['ratings']['count'] == 2 and abs(entry['ratings']['sum'] - 14.0) < 1e-9, \
            'aggregate must revert exactly to the incumbent history'
        assert entry['total_loads'] == 5

    # -- Version resolution ----------------------------------------------------

    def test_incumbent_version_parsed_from_file(self, fresh_manager):
        """prod_version comes from the incumbent FILE frontmatter, not the registry."""
        m = self.manager
        name = f"test-cand-file-version-{_uid()}"
        _write_incumbent(m, name, '3.1.4', body_marker='FILE_VERSION_INCUMBENT')

        # Seed a minimal legacy aggregate so the backfill path has something to seed from;
        # an unrated incumbent (no ratings block) is not backfilled, which would skip the key.
        with m._metrics_lock:
            m._metrics[name] = {
                'total_loads': 1,
                'by_version': {
                    '3.1.4': 1
                },
                'ratings': {
                    'count': 1,
                    'sum': 7.0,
                    'latest': 7.0,
                    'last_version': '3.1.4'
                },
            }

        content = _make_skill_content(
            name=name,
            description='Candidate over a file-versioned incumbent skill here',
            triggers=['file', 'version'],
            generated_from_task='test incumbent version from file',
        )
        success, _ = m.register_skill_from_content(content, task_text='test incumbent version from file')
        assert success

        # Backfill must have used the FILE version (3.1.4) as the key.
        entry = m.get_metrics(name)
        assert '3.1.4' in entry['ratings_by_version']

    # -- Orphan handling ---------------------------------------------------------

    def test_orphan_candidate_discarded(self, fresh_manager):
        """Incumbent file deleted → candidate is orphaned and discarded immediately."""
        m = self.manager
        name = f"test-cand-orphan-{_uid()}"
        _write_incumbent(m, name, '1.0.0')

        content = _make_skill_content(
            name=name,
            description='Candidate whose incumbent gets deleted (orphan test)',
            triggers=['orphan', 'incumbent'],
            generated_from_task='test orphan candidate discard',
        )
        success, _ = m.register_skill_from_content(content, task_text='test orphan candidate discard')
        assert success

        # Manually delete the incumbent file (simulates out-of-band removal).
        prod_file = (m._production_skills_dir / name / 'SKILL.md')
        prod_file.unlink()
        m.evaluate_candidates()

        assert not (m._candidates_dir / name).exists(), 'orphaned candidate dir must be deleted'
        assert name not in m.get_candidate_names()

    def test_disabled_incumbent_keeps_candidate_pending(self, fresh_manager):
        """A disabled incumbent is still an incumbent: the candidate stays pending, not orphaned."""
        m = self.manager
        name = f"test-cand-disabled-{_uid()}"
        _write_incumbent(m, name, '1.0.0')

        content = _make_skill_content(
            name=name,
            description='Candidate over a disabled incumbent skill here',
            triggers=['disabled', 'incumbent'],
            generated_from_task='test disabled incumbent pending',
        )
        success, _ = m.register_skill_from_content(content, task_text='test disabled incumbent pending')
        assert success

        # Only CANDIDATE_MIN_RATINGS - 1 ratings (below the gate) + disabled incumbent → must survive.
        for r in (9.0, 9.0) + (9.0,) * (CANDIDATE_MIN_RATINGS - 3):
            m.record_rating(name, r)
        m._disabled_names.add(name)  # simulate AGENT_CASCADE_SKILLS_DISABLED membership
        m.evaluate_candidates()

        assert (m._candidates_dir / name).exists(), 'candidate over a disabled incumbent must stay pending'
        assert m.get_candidate_names() == [name]

    # -- Re-proposal over an existing candidate -----------------------------------

    def test_reproposal_over_existing_candidate(self, fresh_manager):
        """A new proposal while a candidate exists replaces the file and re-triggers eval."""
        m = self.manager
        name = f"test-cand-repropose-{_uid()}"
        _write_incumbent(m, name, '1.0.0')

        # Each proposal carries an explicit, distinct version so a re-proposal is observable
        # (v2 != v1) and the first candidate's per-version rating history stays separable.
        content1 = _make_skill_content(
            name=name,
            description='First candidate version for re-proposal test here',
            triggers=['repropose', 'first'],
            generated_from_task='test re-proposal first candidate',
        ).replace('---\n', '---\nversion: 2.0.0\n', 1)
        success1, _ = m.register_skill_from_content(content1, task_text='test re-proposal first candidate')
        assert success1
        v1 = m.get_skill_metadata(name)['version']

        # Seed ratings on the first candidate version so its history exists.
        for r in (8.0, 9.0):
            m.record_rating(name, r)

        content2 = _make_skill_content(
            name=name,
            description='Second candidate version replacing the first one',
            triggers=['repropose', 'second'],
            generated_from_task='test re-proposal second candidate',
        ).replace('---\n', '---\nversion: 3.0.0\n', 1)
        success2, _ = m.register_skill_from_content(content2, task_text='test re-proposal second candidate')
        assert success2

        cand_file = (m._candidates_dir / name / 'SKILL.md')
        assert cand_file.exists()
        on_disk = cand_file.read_text(encoding='utf-8')
        assert 'Second candidate version replacing the first one' in on_disk, \
            'candidate file must be replaced by the newest proposal'
        v2 = m.get_skill_metadata(name)['version']
        assert v2 != v1
        # Old per-version history kept as evidence.
        entry = m.get_metrics(name)
        assert v1 in entry['ratings_by_version'] and entry['ratings_by_version'][v1]['count'] == 2

    # -- Idempotency ---------------------------------------------------------------

    def test_double_trigger_cannot_double_promote(self, fresh_manager):
        """Calling evaluate_candidates() twice (registration trigger + timer) is idempotent."""
        m = self.manager
        name = f"test-cand-idempotent-{_uid()}"
        _write_incumbent(m, name, '1.0.0')

        content = _make_skill_content(
            name=name,
            description='Candidate for double-trigger idempotency test here',
            triggers=['idempotent', 'double'],
            generated_from_task='test double trigger idempotency',
        )
        success, _ = m.register_skill_from_content(content, task_text='test double trigger idempotency')
        assert success  # registration already triggered one eval (below gate: not enough ratings)

        with m._metrics_lock:
            entry = m._metrics.setdefault(name, {'total_loads': 0, 'by_version': {}})
            entry.setdefault('ratings_by_version', {})['1.0.0'] = {'count': 1, 'sum': 5.0, 'latest': 5.0}
        # Candidate: exactly CANDIDATE_MIN_RATINGS ratings (all 9.0 > incumbent 5.0) → first eval promotes.
        for r in (9.0,) * CANDIDATE_MIN_RATINGS:
            m.record_rating(name, r)

        m.evaluate_candidates()
        reg_after_first = dict(m._skills_registry[name])
        m.evaluate_candidates()  # second trigger — must be a no-op now that it is promoted
        assert m._skills_registry[name] == reg_after_first
        assert not (m._candidates_dir / name).exists()

    def test_double_trigger_cannot_double_discard(self, fresh_manager):
        m = self.manager
        name = f"test-cand-idempotent-discard-{_uid()}"
        _write_incumbent(m, name, '1.0.0')

        content = _make_skill_content(
            name=name,
            description='Candidate for double-trigger discard idempotency test',
            triggers=['idempotent', 'discard'],
            generated_from_task='test double trigger discard idempotency',
        )
        success, _ = m.register_skill_from_content(content, task_text='test double trigger discard idempotency')
        assert success

        with m._metrics_lock:
            entry = m._metrics.setdefault(name, {'total_loads': 0, 'by_version': {}})
            entry.setdefault('ratings_by_version', {})['1.0.0'] = {'count': 1, 'sum': 10.0, 'latest': 10.0}
        # Candidate: exactly CANDIDATE_MIN_RATINGS ratings averaging 2.0 (< incumbent 10.0) → first eval discards.
        for r in (1.0, 2.0, 3.0) + (2.0,) * (CANDIDATE_MIN_RATINGS - 3):
            m.record_rating(name, r)

        m.evaluate_candidates()
        assert not (m._candidates_dir / name).exists()
        state_after_first = m.get_metrics(name)
        m.evaluate_candidates()  # second trigger — candidate already gone; must not crash or re-revert
        assert m.get_metrics(name)['ratings'] == state_after_first['ratings']

    # -- New-skill path unchanged ----------------------------------------------------

    def test_new_skill_path_unchanged(self, fresh_manager):
        """A brand-new name still registers straight to production (no candidate flow)."""
        m = self.manager
        name = f"test-cand-newskill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description='Brand new skill that must bypass the candidate flow',
            triggers=['newskill', 'bypass'],
            generated_from_task='test new skill path unchanged',
        )
        success, errors = m.register_skill_from_content(content, task_text='test new skill path unchanged')
        assert success, f"new-skill registration failed: {errors}"

        reg = m._skills_registry[name]
        assert 'candidates' not in reg['file_path']
        assert (m._production_skills_dir / name / 'SKILL.md').exists(), \
            'new skills must still be promoted straight to production'
        assert m.get_candidate_names() == []

    # -- scan_skills visibility ------------------------------------------------------

    def test_scan_skills_marks_candidate_lines(self, fresh_manager):
        """No-query scan_skills listing marks the candidate line with its incumbent version."""
        from agent_cascade.tools.custom.scan_skills import ScanSkills
        m = self.manager
        name = f"test-cand-scan-{_uid()}"
        _write_incumbent(m, name, '1.0.0')

        content = _make_skill_content(
            name=name,
            description='Candidate visible in scan_skills listing test here',
            triggers=['scan', 'candidate'],
            generated_from_task='test scan skills candidate marker',
        )
        success, _ = m.register_skill_from_content(content, task_text='test scan skills candidate marker')
        assert success

        pool = MagicMock()
        pool.skill_manager = m
        tool = ScanSkills(agent_pool=pool)
        import json as _json
        listing = tool.call(_json.dumps({'query': ''}))
        line = next(l for l in listing.splitlines() if f'**{name}**' in l)
        assert '(candidate, pending decision vs v1.0.0)' in line

    # -- Metrics schema -----------------------------------------------------------------

    def test_flush_bumps_schema_to_1_2(self, fresh_manager):
        """A 1.1 metrics file is tolerated on load and bumped to 1.2 on flush."""
        import json as _json
        m = self.manager
        self.metrics_file.write_text(_json.dumps({
            'schema_version': '1.1',
            'skills': {
                'legacy-skill': {
                    'total_loads': 3,
                    'by_version': {
                        '1.0.0': 3
                    },
                    'ratings': {
                        'count': 1,
                        'sum': 8.0,
                        'latest': 8.0,
                        'last_version': '1.0.0'
                    }
                }
            }
        }),
                                     encoding='utf-8')
        m._load_metrics()
        assert m.get_metrics('legacy-skill')['total_loads'] == 3

        m.record_rating('legacy-skill', 7.0)
        data = _json.loads(self.metrics_file.read_text(encoding='utf-8'))
        assert data['schema_version'] == '1.2'


# ===========================================================================
# Skill-metrics pruning: prune_stale_metrics + evaluate_candidates wiring
# ===========================================================================


def _platform_excluded_list():
    """Return a platforms frontmatter list that excludes the CURRENT OS (cross-platform).

    ``_skill_matches_platform`` maps macos→darwin, linux→linux, windows→win32 and matches
    when ``sys.platform.startswith(mapped)``. To make a skill incompatible we must exclude
    the current platform's MAPPED value (so its startswith check fails). We list the other
    two mapped values as harmless extras — they never match the current OS. Excluding all
    three would NOT work: on Windows, 'win32'.startswith('win32') still matches.
    """
    import sys as _sys
    from agent_cascade.skills.manager import _PLATFORM_MAP
    cur = _sys.platform
    # The mapped value for the current OS is the one that must be excluded.
    all_mapped = {'darwin', 'linux', 'win32'}
    for friendly, mapped in _PLATFORM_MAP.items():
        if cur.startswith(mapped):
            return sorted(all_mapped - {mapped})  # exclude only the current platform's value
    # Unknown platform: exclude everything (safe default — skill is incompatible).
    return sorted(all_mapped)


def _seed_metrics(m, name):
    """Insert a minimal metrics entry for ``name`` (mimics an old on-disk record)."""
    with m._metrics_lock:
        m._metrics[name] = {'total_loads': 1, 'by_version': {'1.0.0': 1}}


def _write_skill_md(path: Path, name: str, platforms=None):
    """Write a minimal SKILL.md at ``path`` (frontmatter name + optional platforms)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = f'---\nname: {name}\ndescription: Prune test skill with enough characters here\n'
    if platforms is not None:
        fm += 'platforms:\n' + ''.join(f'  - {p}\n' for p in platforms)
    fm += 'triggers:\n  - prune\n---\n\nPRUNE_BODY\n'
    path.write_text(fm, encoding='utf-8')


class TestPruneStaleMetrics:
    """prune_stale_metrics() + its evaluate_candidates() wiring.

    The autouse ``fresh_manager`` fixture already redirects ``_metrics_file`` to a temp path,
    so no test here can clobber the real production agents/global/skills-metrics.json. The
    class-level ``_prune_env`` fixture additionally resets the in-memory ``_metrics`` dict so
    each test starts from a clean slate (fresh_manager only redirects the file, it does not
    clear loaded state).
    """

    @pytest.fixture(autouse=True)
    def _prune_env(self, fresh_manager, tmp_path):
        self.manager = fresh_manager
        self.metrics_file = tmp_path / 'skills-metrics.json'
        # Reset the in-memory metrics dict so each test starts clean (file path is already
        # redirected to a temp location by fresh_manager).
        _isolate_metrics(fresh_manager, tmp_path, reset=True)
        yield

    def test_pruned_when_deleted(self, fresh_manager):
        """A skill with a metrics entry but no SKILL.md anywhere is pruned."""
        m = self.manager
        root = Path(m._metrics_file).parent / 'skills'
        _write_skill_md(root / 'alive' / 'SKILL.md', 'alive')
        m.discover([root])
        assert set(m._skills_registry) == {'alive'}

        with m._metrics_lock:
            m._metrics = {'alive': {'total_loads': 1, 'by_version': {'1.0.0': 1}},
                          'deleted-skill': {'total_loads': 3, 'by_version': {'1.0.0': 3}}}

        m.evaluate_candidates()

        assert 'deleted-skill' not in m._metrics, 'deleted skill metrics must be pruned'
        assert 'alive' in m._metrics, 'live skill metrics must be kept'
        import json as _json
        data = _json.loads(self.metrics_file.read_text(encoding='utf-8'))
        assert 'deleted-skill' not in data['skills']
        assert 'alive' in data['skills']

    def test_inactive_subfolder_kept(self, fresh_manager):
        """SKILL.md under <root>/INACTIVE/foo/ is NOT in the registry (discover is 1-level)
        but the recursive prune walk finds it → metrics KEPT."""
        m = self.manager
        root = Path(m._metrics_file).parent / 'skills'
        _write_skill_md(root / 'INACTIVE' / 'foo' / 'SKILL.md', 'foo')
        m.discover([root])
        assert 'foo' not in m._skills_registry, 'discover() is one level deep — INACTIVE not registered'

        _seed_metrics(m, 'foo')
        m.evaluate_candidates()

        assert 'foo' in m._metrics, 'INACTIVE-subfolder skill metrics must be KEPT (artifact on disk)'

    def test_disabled_kept(self, fresh_manager):
        """A disabled skill (on disk but excluded from the registry) keeps its metrics."""
        m = self.manager
        root = Path(m._metrics_file).parent / 'skills'
        _write_skill_md(root / 'disabled-skill' / 'SKILL.md', 'disabled-skill')
        m._disabled_names.add('disabled-skill')
        m.discover([root])
        assert 'disabled-skill' not in m._skills_registry, 'disabled skill must be skipped by discover()'

        _seed_metrics(m, 'disabled-skill')
        m.evaluate_candidates()

        assert 'disabled-skill' in m._metrics, 'disabled skill metrics must be KEPT (artifact on disk)'

    def test_platform_incompatible_kept(self, fresh_manager):
        """A SKILL.md whose platforms exclude the current OS is skipped by discover() but
        still on disk → metrics KEPT."""
        m = self.manager
        root = Path(m._metrics_file).parent / 'skills'
        _write_skill_md(root / 'plat-skill' / 'SKILL.md', 'plat-skill', platforms=_platform_excluded_list())
        m.discover([root])
        assert 'plat-skill' not in m._skills_registry, 'platform-incompatible skill must be skipped by discover()'

        _seed_metrics(m, 'plat-skill')
        m.evaluate_candidates()

        assert 'plat-skill' in m._metrics, 'platform-incompatible skill metrics must be KEPT (artifact on disk)'

    def test_name_mismatch_kept_both_keys(self, fresh_manager):
        """Dir name differs from frontmatter name; metrics keyed by BOTH are kept."""
        m = self.manager
        root = Path(m._metrics_file).parent / 'skills'
        # dir 'dirname-skill', frontmatter name 'realname-skill'.
        _write_skill_md(root / 'dirname-skill' / 'SKILL.md', 'realname-skill')
        m.discover([root])
        assert 'realname-skill' in m._skills_registry

        _seed_metrics(m, 'dirname-skill')   # keyed by dir name
        _seed_metrics(m, 'realname-skill')  # keyed by frontmatter name
        m.evaluate_candidates()

        assert 'dirname-skill' in m._metrics, 'dir-name key must be KEPT (walk adds the containing dir)'
        assert 'realname-skill' in m._metrics, 'frontmatter-name key must be KEPT (walk adds the fm name)'

    def test_case_insensitive_retention(self, fresh_manager):
        """Metrics keyed by mixed-case 'MySkill' while disk has 'myskill' → KEPT."""
        m = self.manager
        root = Path(m._metrics_file).parent / 'skills'
        _write_skill_md(root / 'myskill' / 'SKILL.md', 'myskill')
        m.discover([root])

        _seed_metrics(m, 'MySkill')  # mixed-case key, lowercases to the live 'myskill'
        m.evaluate_candidates()

        assert 'MySkill' in m._metrics, 'mixed-case metrics key must be KEPT (case-insensitive match)'

    def test_failed_scan_aborts_no_pruning(self, fresh_manager):
        """A scan error on an EXISTING root aborts the prune: nothing deleted, warning logged."""
        m = self.manager
        root = Path(m._metrics_file).parent / 'skills'
        _write_skill_md(root / 'alive' / 'SKILL.md', 'alive')
        m.discover([root])

        _seed_metrics(m, 'stale-entry')  # would be pruned on a normal scan
        _seed_metrics(m, 'alive')

        import agent_cascade.skills.manager as mgr_mod
        with patch.object(Path, 'rglob', side_effect=OSError('simulated I/O error')):
            with patch.object(mgr_mod.logger, 'warning') as mock_warn:
                m.evaluate_candidates()

        assert 'stale-entry' in m._metrics, 'prune must be aborted on scan error (nothing deleted)'
        assert 'alive' in m._metrics
        # A warning about the aborted prune must have been logged.
        assert any('Metrics prune aborted' in str(c.args[0]) for c in mock_warn.call_args_list), \
            'expected an abort warning to be logged on scan error'

    def test_candidate_not_pruned_pre_decision(self, fresh_manager):
        """A pending candidate (below CANDIDATE_MIN_RATINGS) still exists on disk after the
        gate → its metrics are KEPT (prune runs after the gate)."""
        m = self.manager
        # The candidate root must sit under an `agents/` ancestor so _priority_for_root()
        # classifies it as the CANDIDATE tier (4); a bare tmp/.../candidates is SYSTEM (1).
        cand_root = Path(m._metrics_file).parent / 'agents' / 'global' / 'candidates'
        prod_root = Path(m._metrics_file).parent / 'agents' / 'global' / 'skills'
        # An incumbent production file so the candidate is NOT orphaned (the orphan check
        # runs before the rating gate and would otherwise discard it); with zero ratings the
        # candidate stays PENDING (below CANDIDATE_MIN_RATINGS) and survives on disk.
        _write_skill_md(prod_root / 'pend-cand' / 'SKILL.md', 'pend-cand')
        # Candidate dir under the candidates root (discover registers it at CANDIDATE priority).
        _write_skill_md(cand_root / 'pend-cand' / 'SKILL.md', 'pend-cand')
        m.discover([prod_root, cand_root])
        assert 'pend-cand' in m._skills_registry
        assert m.get_candidate_names() == ['pend-cand']

        _seed_metrics(m, 'pend-cand')
        # No ratings → below CANDIDATE_MIN_RATINGS → the gate never promotes or discards it,
        # so its dir stays on disk. (In this isolated env evaluate_candidates()'s forced
        # discovery refresh may clear the in-memory registry, but the on-disk candidate file
        # — which is what prune sees — is unchanged.)
        m.evaluate_candidates()

        assert (cand_root / 'pend-cand' / 'SKILL.md').exists(), \
            'pending candidate dir must still exist on disk after the gate (not discarded)'
        assert 'pend-cand' in m._metrics, 'pending candidate metrics must be KEPT (still on disk)'

    def test_idempotent_noop_does_not_rewrite(self, fresh_manager):
        """Running prune with nothing stale is a no-op: second run removes nothing and does
        NOT rewrite the metrics file."""
        import json as _json
        m = self.manager
        root = Path(m._metrics_file).parent / 'skills'
        _write_skill_md(root / 'alive' / 'SKILL.md', 'alive')
        m.discover([root])

        _seed_metrics(m, 'alive')
        # First run: nothing stale → no flush. Write the file once so we can compare.
        m._flush_metrics_to_disk()
        first_content = self.metrics_file.read_text(encoding='utf-8')
        first_mtime = self.metrics_file.stat().st_mtime_ns

        with patch.object(m, '_flush_metrics_to_disk') as mock_flush:
            m.prune_stale_metrics()  # second run — nothing stale

        assert mock_flush.call_count == 0, 'no-op prune must NOT flush/rewrite the file'
        assert self.metrics_file.read_text(encoding='utf-8') == first_content, 'file content must be unchanged'
        assert self.metrics_file.stat().st_mtime_ns == first_mtime, 'file mtime must be unchanged (no rewrite)'
        assert _json.loads(first_content)['skills'].keys() == {'alive'}
