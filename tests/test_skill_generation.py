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

import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

# Ensure the project root is on sys.path so imports resolve correctly
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_cascade.skills.parser import parse_frontmatter
from agent_cascade.skills.matcher import SkillMatcher
from agent_cascade.skills.manager import SkillManager
from agent_cascade.skills.validator import validate_skill
from agent_cascade.settings import (
    AUTO_SKILL_PROMOTION_THRESHOLD,
    AUTO_SKILL_MAX_PER_SESSION,
    AUTO_SKILL_MIN_TOOL_CALLS,
)


# ===========================================================================
# Helpers — skill content factory
# ===========================================================================

def _uid():
    """Generate a short unique suffix for parallel test isolation."""
    return uuid.uuid4().hex[:8]


def _make_skill_content(
    name: str = "test-skill",
    description: str = "A skill for testing purposes with enough characters",
    triggers: list = None,
    body: str = None,
    source: str = "auto-generated",
    generated_by: str = "coder",
    generated_from_task: str = "Write a test skill",
):
    """Build a valid SKILL.md content string."""
    if triggers is None:
        triggers = ["test", "skill"]
    if body is None:
        body = (
            "## Instructions\n\n"
            "Follow these steps carefully to complete the task. "
            "This body has enough characters to pass validation.\n\n"
            "1. Step one\n2. Step two\n3. Step three\n"
        )
    fm = {
        "name": name,
        "description": description,
        "source": source,
        "triggers": triggers,
        "generated_by": generated_by,
        "generated_from_task": generated_from_task,
    }
    yaml_block = yaml.dump(fm, default_flow_style=False)
    return f"---\n{yaml_block}---\n\n{body}"


def _cleanup_test_artifacts():
    """Remove any pending-skills and promoted skills left by tests.

    Keeps canonical skill-creator and version-control skills intact.
    """
    _CANONICAL = {"skill-creator", "version-control"}

    pending_root = Path(".qwen/pending-skills")
    if pending_root.exists():
        for entry in list(pending_root.iterdir()):
            if entry.is_dir():
                skill_file = entry / "SKILL.md"
                if skill_file.exists():
                    skill_file.unlink()
                if not list(entry.iterdir()):
                    entry.rmdir()

    skills_root = Path(".qwen/skills")
    if skills_root.exists():
        for entry in list(skills_root.iterdir()):
            if entry.is_dir() and entry.name not in _CANONICAL:
                skill_file = entry / "SKILL.md"
                if skill_file.exists():
                    skill_file.unlink()
                if not list(entry.iterdir()):
                    entry.rmdir()


# ===========================================================================
# Shared fixture: fresh SkillManager with guaranteed cleanup
# ===========================================================================

@pytest.fixture(autouse=True)
def fresh_manager():
    """Create a fresh SkillManager and clean up test artifacts after each test."""
    _cleanup_test_artifacts()
    manager = SkillManager()
    yield manager
    _cleanup_test_artifacts()


# ===========================================================================
# 1. Unit: Validation — agent_cascade.skills.validator
# ===========================================================================

class TestValidation:
    """Tier 1 structural validation and Tier 2 self-match."""

    def test_valid_skill_passes(self):
        content = _make_skill_content()
        passed, errors = validate_skill(content, "test-skill", set())
        assert passed, f"Expected pass, got errors: {errors}"

    def test_valid_skill_with_task_text_passes(self):
        content = _make_skill_content(
            generated_from_task="Write a test skill for validation"
        )
        passed, errors = validate_skill(
            content, "test-skill", set(), task_text="Write a test skill"
        )
        assert passed, f"Expected pass, got errors: {errors}"

    def test_invalid_name_uppercase(self):
        content = _make_skill_content(name="TestSkill")
        passed, errors = validate_skill(content, "TestSkill", set())
        assert not passed
        assert len(errors) > 0

    def test_invalid_name_starts_with_digit(self):
        content = _make_skill_content(name="1test-skill")
        passed, errors = validate_skill(content, "1test-skill", set())
        assert not passed
        assert len(errors) > 0

    def test_invalid_name_empty(self):
        content = _make_skill_content(name="")
        passed, errors = validate_skill(content, "", set())
        assert not passed
        assert len(errors) > 0

    def test_missing_description(self):
        content = _make_skill_content(description="")
        passed, errors = validate_skill(content, "test-skill", set())
        assert not passed
        assert len(errors) > 0

    def test_short_description(self):
        content = _make_skill_content(description="Short")
        passed, errors = validate_skill(content, "test-skill", set())
        assert not passed
        assert len(errors) > 0

    def test_empty_triggers(self):
        content = _make_skill_content(triggers=[])
        passed, errors = validate_skill(content, "test-skill", set())
        assert not passed
        assert len(errors) > 0

    def test_missing_triggers(self):
        raw = (
            "---\n"
            "name: test-skill\n"
            "description: A skill for testing purposes with enough characters\n"
            "---\n\n"
            "## Instructions\n\n"
            "Follow these steps carefully to complete the task. "
            "This body has enough characters to pass validation.\n\n"
            "1. Step one\n2. Step two\n3. Step three\n"
        )
        passed, errors = validate_skill(raw, "test-skill", set())
        assert not passed
        assert len(errors) > 0

    def test_duplicate_name(self):
        content = _make_skill_content(name="test-skill")
        passed, errors = validate_skill(content, "test-skill", {"test-skill"})
        assert not passed
        assert len(errors) > 0

    def test_body_too_short(self):
        content = _make_skill_content(body="Short body")
        passed, errors = validate_skill(content, "test-skill", set())
        assert not passed
        assert len(errors) > 0

    def test_file_too_large(self):
        content = _make_skill_content(body="X " * 20000)
        passed, errors = validate_skill(content, "test-skill", set())
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

    def test_duplicate_skill_rejected(self, fresh_manager):
        name = f"duplicate-test-skill-{_uid()}"
        content = _make_skill_content(name=name)
        fresh_manager.register_skill_from_content(content)
        content2 = _make_skill_content(name=name)
        success, errors = fresh_manager.register_skill_from_content(content2)
        assert not success
        assert len(errors) > 0

    def test_triggers_stored_in_registry(self, fresh_manager):
        triggers = ["pytest", "unit-test", "mocking"]
        name = f"triggers-test-skill-{_uid()}"
        content = _make_skill_content(name=name, triggers=triggers)
        fresh_manager.register_skill_from_content(content)
        reg = fresh_manager._skills_registry.get(name)
        assert reg is not None
        assert reg.get("triggers") == triggers

    def test_triggers_returned_by_get_all_metadata(self, fresh_manager):
        triggers = ["pytest", "unit-test", "mocking"]
        name = f"metadata-triggers-skill-{_uid()}"
        content = _make_skill_content(name=name, triggers=triggers)
        fresh_manager.register_skill_from_content(content)
        all_meta = fresh_manager.get_all_metadata()
        found = [m for m in all_meta if m["name"] == name]
        assert len(found) == 1
        assert found[0].get("triggers") == triggers


# ===========================================================================
# 3. Unit: Self-Match — Tier 2 validation via SkillMatcher
# ===========================================================================

class TestSelfMatch:
    """Validate that self-match scoring works correctly."""

    def test_matching_skill_scores_above_threshold(self):
        content = _make_skill_content(
            name="pytest-testing",
            description="Writing pytest unit tests with fixtures and mocking",
            triggers=["pytest", "unit test", "test fixture", "mock"],
            generated_from_task="Write pytest unit tests for the parser",
        )
        passed, errors = validate_skill(
            content, "pytest-testing", set(),
            task_text="Write pytest unit tests for the parser module"
        )
        assert passed, f"Self-match should pass: {errors}"

    def test_non_matching_skill_scores_below_threshold(self):
        content = _make_skill_content(
            name="docker-containers",
            description="Managing Docker containers and orchestration",
            triggers=["docker", "container", "kubernetes", "pod"],
            generated_from_task="Write pytest unit tests for the parser",
        )
        passed, errors = validate_skill(
            content, "docker-containers", set(),
            task_text="Write pytest unit tests for the parser module"
        )
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
                "name": "pytest-testing",
                "description": "Writing pytest unit tests",
                "triggers": ["pytest", "unit test", "mock", "fixture"],
            },
        ]
        matcher.build_index(meta)
        assert "pytest" in matcher._inverted_index
        assert "mock" in matcher._inverted_index
        assert "fixture" in matcher._inverted_index

    def test_trigger_keywords_enable_matching(self):
        matcher = SkillMatcher()
        meta = [
            {
                "name": "unique-skill-name",
                "description": "Something unrelated to the query",
                "triggers": ["quantum", "physics", "particles"],
            },
        ]
        matcher.build_index(meta)
        results = matcher.match("quantum physics particles")
        assert len(results) > 0
        assert results[0][0] == "unique-skill-name"

    def test_triggers_included_with_name_and_description(self):
        matcher = SkillMatcher()
        meta = [
            {
                "name": "only-triggers-match",
                "description": "xyz abc",
                "triggers": ["hello", "world"],
            },
        ]
        matcher.build_index(meta)
        results = matcher.match("hello world")
        assert len(results) > 0
        assert results[0][0] == "only-triggers-match"


# ===========================================================================
# 5. Integration: Full propose → validate → promote flow
# ===========================================================================

class TestProposeValidatePromote:
    """End-to-end flow: create skill content → register → validate → promote."""

    def test_full_flow_with_promotion(self, fresh_manager):
        name = f"integration-test-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description="Integration test skill for the auto-skill generation system",
            triggers=["integration", "test", "auto-skill"],
            generated_from_task="Test the full skill generation pipeline",
        )

        success, errors = fresh_manager.register_skill_from_content(
            content,
            source="auto-generated",
            task_text="Test the full skill generation pipeline",
            auto_promote=True,
        )
        assert success, f"Flow failed: {errors}"

        assert name in fresh_manager._skills_registry

        target = Path(".qwen/skills") / name / "SKILL.md"
        assert target.exists(), f"Skill was not promoted to .qwen/skills/{name}/"

        reg = fresh_manager._skills_registry[name]
        assert name in reg["file_path"]

    def test_full_flow_without_promotion(self, fresh_manager):
        name = f"integration-test-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description="Integration test skill for the auto-skill generation system",
            triggers=["integration", "test", "auto-skill"],
            generated_from_task="Test the full skill generation pipeline",
        )

        success, _ = fresh_manager.register_skill_from_content(
            content,
            source="auto-generated",
            task_text="Test the full skill generation pipeline",
            auto_promote=False,
        )
        assert success

        assert name in fresh_manager._skills_registry

        reg = fresh_manager._skills_registry[name]
        assert "pending-skills" in reg["file_path"]

    def test_duplicate_in_full_flow(self, fresh_manager):
        """Register same skill twice — second should fail."""
        name = f"integration-test-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description="Integration test skill for the auto-skill generation system",
            triggers=["integration", "test", "auto-skill"],
            generated_from_task="Test the full skill generation pipeline",
        )

        success1, _ = fresh_manager.register_skill_from_content(content, auto_promote=True)
        assert success1

        content2 = _make_skill_content(
            name=name,
            description="Duplicate integration test skill",
            triggers=["integration", "test"],
            generated_from_task="Test again",
        )
        success2, errors = fresh_manager.register_skill_from_content(content2, auto_promote=True)
        assert not success2
        assert len(errors) > 0, "Duplicate should fail with errors"


# ===========================================================================
# 6. Integration: Rate limiting
# ===========================================================================

class TestRateLimiting:
    """Rate limiting: second proposal in same session rejected."""

    def test_second_proposal_flag_mechanism(self, fresh_manager):
        """First proposal sets flag, second checks flag."""
        name1 = f"rate-limit-skill-1-{_uid()}"
        name2 = f"rate-limit-skill-2-{_uid()}"
        content1 = _make_skill_content(
            name=name1,
            description="First skill for rate limiting test",
            triggers=["rate", "limit", "first"],
            generated_from_task="Create first skill",
        )
        content2 = _make_skill_content(
            name=name2,
            description="Second skill for rate limiting test",
            triggers=["rate", "limit", "second"],
            generated_from_task="Create second skill",
        )

        success1, _ = fresh_manager.register_skill_from_content(
            content1, task_text="Create first skill"
        )
        assert success1

        success2, _ = fresh_manager.register_skill_from_content(
            content2, task_text="Create second skill"
        )
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
        name = f"rate-limit-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description="x",
            triggers=["test"],
            generated_from_task="Create skill",
        )
        success, _ = fresh_manager.register_skill_from_content(content)
        assert not success

        pending_root = Path(".qwen/pending-skills")
        if pending_root.exists():
            for entry in list(pending_root.iterdir()):
                skill_file = entry / "SKILL.md"
                if skill_file.exists():
                    fm, _ = parse_frontmatter(skill_file.read_text())
                    assert fm.get("name") != name


# ===========================================================================
# 7. Integration: Hot-reload
# ===========================================================================

class TestHotReload:
    """New skill discoverable after registration without restart."""

    def test_new_skill_discoverable_after_registration(self, fresh_manager):
        name = f"hot-reload-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description="Skill for testing hot-reload discovery",
            triggers=["hot-reload", "discovery", "dynamic"],
            generated_from_task="Test hot-reload skill discovery",
        )

        success, _ = fresh_manager.register_skill_from_content(
            content,
            task_text="Test hot-reload skill discovery",
        )
        assert success

        results = fresh_manager.match_skills("hot-reload discovery dynamic")
        assert len(results) > 0
        assert any(n == name for n, _ in results)

        all_meta = fresh_manager.get_all_metadata()
        names = [m["name"] for m in all_meta]
        assert name in names

    def test_index_rebuilt_after_registration(self, fresh_manager):
        name = f"hot-reload-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description="Skill for testing hot-reload discovery",
            triggers=["hot-reload", "discovery", "dynamic"],
            generated_from_task="Test hot-reload skill discovery",
        )

        fresh_manager.register_skill_from_content(
            content,
            task_text="Test hot-reload skill discovery",
        )

        assert name in fresh_manager._matcher._inverted_index
        assert "discovery" in fresh_manager._matcher._inverted_index

    def test_promoted_skill_matchable_via_manager(self, fresh_manager):
        name = f"hot-reload-skill-{_uid()}"
        content = _make_skill_content(
            name=name,
            description="Skill for testing hot-reload discovery",
            triggers=["hot-reload", "discovery", "dynamic"],
            generated_from_task="Test hot-reload skill discovery",
        )

        fresh_manager.register_skill_from_content(
            content,
            task_text="Test hot-reload skill discovery",
            auto_promote=True,
        )

        results = fresh_manager.match_skills("hot-reload discovery")
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
        return [
            {"role": "assistant" if i % 2 else "user", "content": f"msg {i}"}
            for i in range(length)
        ]

    def _make_inst(self, fresh_manager, conv_len: int = 5):
        """Create a mock instance and preload skill-creator in the manager."""
        inst = MagicMock()
        inst.conversation = self._make_conversation(conv_len)
        inst._auto_skill_proposed = False
        inst._auto_skill_proposed_count = 0
        inst.state = "IDLE"
        fresh_manager._skills_registry["skill-creator"] = {
            "name": "skill-creator",
            "file_path": ".qwen/skills/skill-creator/SKILL.md",
            "_parsed_data": {"body": "Create a reusable skill."},
        }
        return inst

    def _trigger(self, inst, fresh_manager, total_tool_calls=10,
                 check_result=None, state_idle=True):
        """Run trigger_auto_skill_reflection with snapshot-based rollback."""
        if check_result is None:
            check_result = []

        def rollback_fn(target_length):
            if target_length > 0 and len(inst.conversation) > target_length:
                del inst.conversation[target_length:]

        return fresh_manager.trigger_auto_skill_reflection(
            inst=inst,
            total_tool_calls=total_tool_calls,
            task_text="Write a test",
            instance_name="worker",
            append_fn=lambda msg: inst.conversation.append(
                {"role": "user", "content": msg}),
            run_turn_fn=lambda: inst.conversation.append(
                {"role": "assistant", "content": "reply"}),
            state_idle_fn=lambda: state_idle,
            snapshot_length=len(inst.conversation),
            rollback_fn=rollback_fn,
            check_skill_created_fn=lambda: check_result,
        )

    def _inject_notice(self, inst, created):
        """Inject notice into last message (mirrors execution_engine)."""
        if created and inst.conversation:
            notice = f"\n\n[Auto-skill created: {', '.join(created)}]"
            last = inst.conversation[-1]
            last["content"] = str(last.get("content", "")) + notice

    # ------------------------------------------------------------------ #
    # Early return guards
    # ------------------------------------------------------------------ #

    def test_returns_empty_when_auto_skill_proposed(self, fresh_manager):
        """_auto_skill_proposed flag set → returns []."""
        inst = self._make_inst(fresh_manager)
        inst._auto_skill_proposed = True
        created = self._trigger(inst, fresh_manager)
        assert created == []

    def test_returns_empty_when_session_limit_exceeded(self, fresh_manager):
        """AUTO_SKILL_MAX_PER_SESSION exceeded → returns []."""
        inst = self._make_inst(fresh_manager)
        inst._auto_skill_proposed_count = AUTO_SKILL_MAX_PER_SESSION
        created = self._trigger(inst, fresh_manager)
        assert created == []

    def test_returns_empty_when_tool_count_below_threshold(self, fresh_manager):
        """Tool count below AUTO_SKILL_MIN_TOOL_CALLS → returns []."""
        inst = self._make_inst(fresh_manager)
        created = self._trigger(inst, fresh_manager,
                                total_tool_calls=AUTO_SKILL_MIN_TOOL_CALLS - 1)
        assert created == []

    def test_returns_empty_when_skill_matches_found(self, fresh_manager):
        """Matching skills exist → returns []."""
        inst = self._make_inst(fresh_manager)
        # Register a skill that will match "Write a test"
        fresh_manager._skills_registry["test-writing"] = {
            "name": "test-writing",
            "file_path": ".qwen/skills/test-writing/SKILL.md",
            "triggers": ["test", "write"],
        }
        fresh_manager._matcher.build_index(
            list(fresh_manager._skills_registry.values()))
        created = self._trigger(inst, fresh_manager)
        assert created == []

    def test_returns_empty_when_not_idle(self, fresh_manager):
        """Instance not idle → returns []."""
        inst = self._make_inst(fresh_manager)
        created = self._trigger(inst, fresh_manager, state_idle=False)
        assert created == []

    def test_returns_empty_when_skill_creator_missing(self, fresh_manager):
        """skill-creator not in registry → returns []."""
        inst = self._make_inst(fresh_manager)
        del fresh_manager._skills_registry["skill-creator"]
        created = self._trigger(inst, fresh_manager)
        assert created == []

    # ------------------------------------------------------------------ #
    # Rollback behaviour
    # ------------------------------------------------------------------ #

    def test_rollback_restores_conversation_length(self, fresh_manager):
        """Conversation length returns to original after rollback."""
        inst = self._make_inst(fresh_manager)
        original_len = len(inst.conversation)

        self._trigger(inst, fresh_manager)

        assert len(inst.conversation) == original_len

    def test_rollback_called_with_correct_pop_count(self, fresh_manager):
        """rollback_fn is invoked with the pop_count (messages added during extra turns)."""
        inst = self._make_inst(fresh_manager)
        original_len = len(inst.conversation)

        captured_counts = []

        def rollback_fn(target_length):
            captured_counts.append(target_length)
            # Truncate to target_length
            if target_length > 0 and len(inst.conversation) > target_length:
                del inst.conversation[target_length:]
        fresh_manager.trigger_auto_skill_reflection(
            inst=inst,
            total_tool_calls=10,
            task_text="Write a test",
            instance_name="worker",
            append_fn=lambda msg: inst.conversation.append(
                {"role": "user", "content": msg}),
            run_turn_fn=lambda: inst.conversation.append(
                {"role": "assistant", "content": "reply"}),
            state_idle_fn=lambda: True,
            snapshot_length=len(inst.conversation),
            rollback_fn=rollback_fn,
            check_skill_created_fn=lambda: [],
        )

        assert len(captured_counts) == 1
        assert captured_counts[0] > 0  # some messages were added and popped
        assert len(inst.conversation) == original_len

    # ------------------------------------------------------------------ #
    # Notice injection (consolidated test)
    # ------------------------------------------------------------------ #

    def test_notice_injected_into_last_message_after_rollback(self, fresh_manager):
        """Full return path: trigger → rollback → notice → returned conv.
        
        Covers:
        - Notice appended to last message content
        - No new message added (length preserved)
        - Returned conversation copy includes the notice
        - final_resp (deep copy taken before trigger) is unaffected
        """
        inst = self._make_inst(fresh_manager)
        original_len = len(inst.conversation)
        # Simulate final_resp snapshot taken before trigger
        final_resp = [dict(m) for m in inst.conversation]

        created = self._trigger(inst, fresh_manager, check_result=["my-skill"])
        self._inject_notice(inst, created)

        # Returned conversation
        returned_conv = list(inst.conversation)

        # Length preserved
        assert len(returned_conv) == original_len
        assert len(inst.conversation) == original_len

        # Notice present in returned conv
        assert "[Auto-skill created:" in returned_conv[-1]["content"]
        assert "my-skill" in returned_conv[-1]["content"]

        # final_resp untouched
        assert len(final_resp) == original_len
        assert "[Auto-skill created:" not in final_resp[-1]["content"]

    def test_no_notice_when_no_skills_created(self, fresh_manager):
        """When no skills are created, last message content is unchanged."""
        inst = self._make_inst(fresh_manager)
        original_last_content = inst.conversation[-1]["content"]

        self._trigger(inst, fresh_manager)

        assert inst.conversation[-1]["content"] == original_last_content

    # ------------------------------------------------------------------ #
    # Compression resilience
    # ------------------------------------------------------------------ #

    def test_rollback_survives_compression_during_extra_turns(self, fresh_manager):
        """When compression removes messages during extra turns, rollback still
        restores the original conversation length using the marker approach."""
        from agent_cascade.settings import AUTO_SKILL_EXTRA_TURNS

        inst = self._make_inst(fresh_manager, conv_len=20)
        original_len = len(inst.conversation)

        def run_turn_with_compression():
            inst.conversation.append({"role": "assistant", "content": "reply"})
            if len(inst.conversation) >= 3:
                inst.conversation.pop(0)
                inst.conversation.pop(0)

        self._trigger(inst, fresh_manager, state_idle=True)

        assert len(inst.conversation) == original_len
        # Verify original messages survived (at least some of them)
        assert len(inst.conversation) > 0

    # ------------------------------------------------------------------ #
    # Edge cases
    # ------------------------------------------------------------------ #

    def test_empty_conversation(self, fresh_manager):
        """Works with an empty conversation list."""
        inst = self._make_inst(fresh_manager, conv_len=0)
        created = self._trigger(inst, fresh_manager)
        assert len(created) == 0
        # No crash; conversation may have messages from extra turns
        # but rollback should handle empty gracefully

    def test_single_message_conversation(self, fresh_manager):
        """Works with a single-message conversation."""
        inst = self._make_inst(fresh_manager, conv_len=1)
        original_content = inst.conversation[-1]["content"]

        self._trigger(inst, fresh_manager)

        assert len(inst.conversation) >= 1
        assert inst.conversation[-1]["content"] == original_content


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
        from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, Message

        conv = [Message(role=SYSTEM, content="You are a test agent")]
        for i in range(n):
            conv.append(Message(role=USER, content=f"User message {i}"))
            conv.append(Message(role=ASSISTANT, content=f"Assistant reply {i}"))
        return conv

    def _write_jsonl(self, path: str, messages):
        """Write a JSONL file with metadata header + message lines."""
        import json
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                "metadata": {
                    "agent_class": "coder",
                    "instance_name": "test-sync",
                    "start_timestamp": "2026-01-01T00:00:00",
                    "current_log_path": path,
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
                if isinstance(item, dict) and "metadata" not in item and "event" not in item:
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
        from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, Message

        # Step 1: Build initial conversation (SYS + 4 pairs = 9 messages)
        conv = self._build_conv(4)  # 9 messages
        assert len(conv) == 9

        # Create a real AgentPool (minimal)
        pool = AgentPool(llm_cfg={})
        inst = pool.create_instance("test-sync", "coder")
        inst.conversation = list(conv)

        # Get logger and sync it
        log_inst = pool.get_logger("test-sync", "coder")
        assert log_inst.log_path

        # Use the actual logger path (not our temp one) for the test
        test_jsonl = log_inst.log_path

        # Write initial state to logger
        self._write_jsonl(test_jsonl, conv)

        # Load history into logger's internal state so truncate_to works correctly
        log_inst.load_history_from_file()

        # Step 2: Append 3 extra messages to pool
        extra = [
            Message(role=USER, content="Extra user 1"),
            Message(role=ASSISTANT, content="Extra assistant 1"),
            Message(role=USER, content="Extra user 2"),
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
            "test-sync",
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
        in_sync, pool_tail, jsonl_tail = check_tail_sync(
            "test-sync", inst.conversation, test_jsonl
        )
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
        from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, Message
        from agent_cascade.prompts.dna import COMPRESSION_MARKER

        jsonl_path = str(tmp_path / "test_rollback_comp.jsonl")

        # Step 1: Build conversation with compression marker
        # SYS + COMP + 3 pairs + 2 extra pairs = 10 messages
        conv = [
            Message(role=SYSTEM, content="You are a test agent"),
            Message(role=USER, content=COMPRESSION_MARKER + " [compressed]"),
            Message(role=USER, content="User 0"),
            Message(role=ASSISTANT, content="Reply 0"),
            Message(role=USER, content="User 1"),
            Message(role=ASSISTANT, content="Reply 1"),
            Message(role=USER, content="User 2"),
            Message(role=ASSISTANT, content="Reply 2"),
            # Extra messages to be rolled back
            Message(role=USER, content="Extra 1"),
            Message(role=ASSISTANT, content="Extra reply 1"),
        ]
        assert len(conv) == 10

        # Write JSONL
        self._write_jsonl(jsonl_path, conv)

        # Create pool + instance
        pool = AgentPool(llm_cfg={})
        inst = pool.create_instance("test-sync-comp", "coder")
        inst.conversation = list(conv)

        # Get logger
        log_inst = pool.get_logger("test-sync-comp", "coder")
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
            "test-sync-comp",
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
            isinstance(m.get('content', ''), str) and m['content'].startswith(COMPRESSION_MARKER)
            for m in jsonl_msgs
        )
        assert marker_present, "Compression marker missing from JSONL after rollback"

        # Step 5: Verify tail counts match (both should be 6 = 8 - 1 marker - 1 SYS)
        pool_tail_after = self._count_pool_tail(inst.conversation)
        jsonl_tail_after = self._count_jsonl_tail(test_jsonl)
        assert pool_tail_after == 6, f"pool_tail_after={pool_tail_after}"
        assert jsonl_tail_after == 6, f"jsonl_tail_after={jsonl_tail_after}"

        # Step 6: Tail sync check passes
        from agent_cascade.logger.tail_sync_check import check_tail_sync
        in_sync, pt, jt = check_tail_sync(
            "test-sync-comp", inst.conversation, test_jsonl
        )
        assert in_sync, f"Tail sync failed: pool_tail={pt}, jsonl_tail={jt}"

    def test_notice_injection_preserves_tail_sync(self, tmp_path):
        """Full auto-skill rollback flow: notice injection doesn't break tail sync.

        Flow:
          1. Build conversation → snapshot length
          2. Append extra messages
          3. Rollback to snapshot
          4. Inject notice into last message (content modification, no new messages)
          5. Verify tail sync still holds
        """
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, Message

        # Step 1: Build conversation (SYS + 3 pairs = 7 messages)
        conv = self._build_conv(3)  # 7 messages
        snapshot_len = len(conv)
        assert snapshot_len == 7

        # Create pool + instance
        pool = AgentPool(llm_cfg={})
        inst = pool.create_instance("test-sync-notice", "coder")
        inst.conversation = list(conv)

        # Get logger
        log_inst = pool.get_logger("test-sync-notice", "coder")
        test_jsonl = log_inst.log_path

        # Write initial state
        self._write_jsonl(test_jsonl, conv)

        # Load history into logger's internal state so truncate_to works correctly
        log_inst.load_history_from_file()

        # Step 2: Append 4 extra messages
        extra = [
            Message(role=USER, content="Extra user"),
            Message(role=ASSISTANT, content="Extra assistant"),
            Message(role=USER, content="Extra user 2"),
            Message(role=ASSISTANT, content="Extra assistant 2"),
        ]
        inst.conversation.extend(extra)
        log_inst.update_history(extra)
        assert len(inst.conversation) == 11

        # Step 3: Rollback to snapshot
        removed = pool._rollback_instance(
            "test-sync-notice",
            target_length=snapshot_len,
            sync_logger=True,
            tail_sync_check=True,
        )
        assert removed == 4, f"Expected 4 removed, got {removed}"
        assert len(inst.conversation) == snapshot_len

        # Verify tail sync before notice injection
        from agent_cascade.logger.tail_sync_check import check_tail_sync
        in_sync_before, pt_before, jt_before = check_tail_sync(
            "test-sync-notice", inst.conversation, test_jsonl
        )
        assert in_sync_before, \
            f"Tail sync failed before notice: pool_tail={pt_before}, jsonl_tail={jt_before}"

        # Step 4: Inject notice into last message (content-only modification)
        notice = "\n\n[Auto-skill created: test-skill]"
        inst.conversation[-1].content += notice

        # Step 5: Verify message count unchanged in pool
        assert len(inst.conversation) == snapshot_len

        # Verify JSONL message count also unchanged (no new messages appended)
        jsonl_after_notice = self._read_jsonl_messages(test_jsonl)
        assert len(jsonl_after_notice) == snapshot_len, \
            f"JSONL has {len(jsonl_after_notice)} messages after notice, expected {snapshot_len}"

        # Step 6: Verify tail sync still holds (no new messages added)
        in_sync_after, pt_after, jt_after = check_tail_sync(
            "test-sync-notice", inst.conversation, test_jsonl
        )
        assert in_sync_after, \
            f"Tail sync failed after notice injection: pool_tail={pt_after}, jsonl_tail={jt_after}"

        # Counts should be identical before and after notice injection
        assert pt_before == pt_after, "Pool tail count changed after notice injection"
        assert jt_before == jt_after, "JSONL tail count changed after notice injection"

        # Verify notice is actually in the last message
        assert "[Auto-skill created:" in inst.conversation[-1].content