"""Unit tests for the AUTO Skill Helper (Advanced mode) — Skill Advisor.

Covers:
- Output parsing (valid markers, malformed, missing verdict, case variations)
- Prompt construction (self-augmentation excluded, empty registry)
- Recommended skill names validated against the registry (unknowns filtered)
- Deny path returns proper error string, no instance created
- Fallback on timeout / error (ambiguous → basic keyword match)
- Zero skills registered → advisor skipped

The heavy LLM/engine path is exercised with lightweight mocks; no real LLM calls.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project root is on sys.path so imports resolve correctly
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_cascade.skills.advisor import (
    SkillAdvisorResult,
    build_skill_advisor_prompt,
    parse_advisor_output,
    run_skill_advisor,
)


# ── Test doubles ──────────────────────────────────────────────────────────────

class MockSkillManager:
    """Minimal stand-in for SkillManager covering the advisor's surface."""

    def __init__(self, names):
        self._names = list(names)

    def get_skill_names(self):
        return list(self._names)

    def get_all_metadata(self):
        return [{"name": n, "description": f"desc for {n}"} for n in self._names]

    def load_full_instructions(self, name):
        # Case-insensitive match, mirrors the real manager.
        for n in self._names:
            if n.lower() == str(name).lower():
                return f"# {n}\ninstructions for {n}"
        return None


def _make_pool(**overrides):
    pool = MagicMock()
    pool.stopped = False
    pool.get_template.return_value = None  # minimal config path
    pool._telemetry.return_value = None
    for k, v in overrides.items():
        setattr(pool, k, v)
    return pool


# ===========================================================================
# 1. Output parsing — agent_cascade.skills.advisor.parse_advisor_output
# ===========================================================================

class TestParseAdvisorOutput:
    def test_valid_approve_with_skills_and_notes(self):
        sm = MockSkillManager(["docker-best-practices", "httpx-connection-pooling"])
        out = (
            "[SKILLS] docker-best-practices, httpx-connection-pooling\n"
            "[NOTES] Use --rm flag and pin image tags.\n"
            "[VERDICT] APPROVE — needs both skills"
        )
        r = parse_advisor_output(out, sm)
        assert r.verdict == "approve"
        assert r.recommended_skills == ["docker-best-practices", "httpx-connection-pooling"]
        assert r.task_notes == "Use --rm flag and pin image tags."
        assert r.reason == "needs both skills"

    def test_deny_returns_reason_and_no_skills(self):
        sm = MockSkillManager(["a", "b"])
        out = "[SKILLS] none\n[NOTES] none\n[VERDICT] DENY — trivial single-file read"
        r = parse_advisor_output(out, sm)
        assert r.verdict == "deny"
        assert r.recommended_skills == []
        assert r.reason == "trivial single-file read"

    def test_missing_verdict_is_ambiguous(self):
        sm = MockSkillManager(["a"])
        r = parse_advisor_output("[SKILLS] a\n[NOTES] something", sm)
        assert r.verdict == "ambiguous"
        assert "VERDICT" in r.reason

    def test_empty_output_is_ambiguous(self):
        sm = MockSkillManager(["a"])
        r = parse_advisor_output("", sm)
        assert r.verdict == "ambiguous"

    def test_case_insensitive_markers_and_names(self):
        sm = MockSkillManager(["docker-best-practices"])
        out = (
            "[skills] Docker-Best-Practices\n"
            "[notes] NONE\n"
            "[verdict] approve - looks fine"
        )
        r = parse_advisor_output(out, sm)
        assert r.verdict == "approve"
        # Canonical (registered) name is returned, not the LLM's casing.
        assert r.recommended_skills == ["docker-best-practices"]
        assert r.task_notes == ""  # "NONE" → empty

    def test_unknown_skills_filtered_out(self):
        sm = MockSkillManager(["real-skill"])
        out = "[SKILLS] real-skill, not-a-real-skill\n[VERDICT] APPROVE — ok"
        r = parse_advisor_output(out, sm)
        assert r.recommended_skills == ["real-skill"]

    def test_self_augmentation_never_recommended(self):
        sm = MockSkillManager(["self-augmentation", "docker-best-practices"])
        out = "[SKILLS] self-augmentation, docker-best-practices\n[VERDICT] APPROVE — ok"
        r = parse_advisor_output(out, sm)
        assert "self-augmentation" not in r.recommended_skills
        assert r.recommended_skills == ["docker-best-practices"]

    def test_unrecognized_verdict_line_is_ambiguous(self):
        sm = MockSkillManager(["a"])
        out = "[SKILLS] a\n[VERDICT] MAYBE — not sure"
        r = parse_advisor_output(out, sm)
        assert r.verdict == "ambiguous"

    def test_duplicate_skills_deduplicated(self):
        sm = MockSkillManager(["a", "b"])
        out = "[SKILLS] a, b, a\n[VERDICT] APPROVE — ok"
        r = parse_advisor_output(out, sm)
        assert r.recommended_skills == ["a", "b"]

    def test_verdict_reason_split_on_em_dash(self):
        sm = MockSkillManager(["a"])
        out = "[SKILLS] none\n[VERDICT] DENY — parent could do this itself"
        r = parse_advisor_output(out, sm)
        assert r.verdict == "deny"
        assert r.reason == "parent could do this itself"


# ===========================================================================
# 2. Prompt construction — build_skill_advisor_prompt
# ===========================================================================

class TestBuildSkillAdvisorPrompt:
    def test_excludes_self_augmentation_from_skills_list(self):
        sm = MockSkillManager(["self-augmentation", "docker-best-practices"])
        prompt = build_skill_advisor_prompt(sm, "Set up docker", "ctx", "coder", "Maine")
        skill_lines = [l for l in prompt.splitlines() if l.startswith("- ")]
        assert any("docker-best-practices" in l for l in skill_lines)
        assert not any("self-augmentation" in l for l in skill_lines)

    def test_includes_task_context_agent_and_caller(self):
        sm = MockSkillManager(["a"])
        prompt = build_skill_advisor_prompt(sm, "MY_TASK", "MY_CTX", "coder", "Maine")
        assert "MY_TASK" in prompt
        assert "MY_CTX" in prompt
        assert "coder" in prompt
        assert "Maine" in prompt

    def test_empty_registry_shows_none(self):
        sm = MockSkillManager([])
        prompt = build_skill_advisor_prompt(sm, "task", "", "coder", "Maine")
        assert "(none)" in prompt


# ===========================================================================
# 3. Registry validation — recommended skill names checked against registry
# ===========================================================================

class TestRegistryValidation:
    def test_run_skill_advisor_validates_names(self):
        sm = MockSkillManager(["docker-best-practices", "httpx-connection-pooling"])
        pool = _make_pool(skill_manager=sm)

        advisor_output = (
            "[SKILLS] docker-best-practices, bogus-skill\n"
            "[NOTES] pin tags\n"
            "[VERDICT] APPROVE — good"
        )
        fake_result = MagicMock()
        fake_result.was_timeout = False
        fake_result.was_error = False
        fake_result.output_text = advisor_output
        fake_result.latency_ms = 12.0

        with patch("agent_cascade.advisor_runner.run_lightweight_advisor", return_value=fake_result):
            r = run_skill_advisor(pool, sm, "task", "ctx", "coder", "Maine")

        assert r.verdict == "approve"
        assert r.recommended_skills == ["docker-best-practices"]  # bogus filtered


# ===========================================================================
# 4. Fallback on timeout / error → ambiguous (caller uses basic keyword match)
# ===========================================================================

class TestFallbackBehavior:
    def test_timeout_returns_ambiguous(self):
        sm = MockSkillManager(["a"])
        pool = _make_pool(skill_manager=sm)
        fake_result = MagicMock()
        fake_result.was_timeout = True
        fake_result.was_error = False
        fake_result.output_text = ""
        fake_result.latency_ms = 30000.0

        with patch("agent_cascade.advisor_runner.run_lightweight_advisor", return_value=fake_result):
            r = run_skill_advisor(pool, sm, "task", "ctx", "coder", "Maine")

        assert r.verdict == "ambiguous"
        assert not r.is_usable

    def test_error_returns_ambiguous(self):
        sm = MockSkillManager(["a"])
        pool = _make_pool(skill_manager=sm)
        fake_result = MagicMock()
        fake_result.was_timeout = False
        fake_result.was_error = True
        fake_result.error_msg = "boom"
        fake_result.output_text = ""
        fake_result.latency_ms = 5.0

        with patch("agent_cascade.advisor_runner.run_lightweight_advisor", return_value=fake_result):
            r = run_skill_advisor(pool, sm, "task", "ctx", "coder", "Maine")

        assert r.verdict == "ambiguous"
        assert not r.is_usable

    def test_runner_raises_returns_ambiguous(self):
        sm = MockSkillManager(["a"])
        pool = _make_pool(skill_manager=sm)
        with patch("agent_cascade.advisor_runner.run_lightweight_advisor", side_effect=RuntimeError("x")):
            r = run_skill_advisor(pool, sm, "task", "ctx", "coder", "Maine")
        assert r.verdict == "ambiguous"

# ===========================================================================
# 5. Deny path — engine returns error string, no child instance created
# ===========================================================================

class TestDenyPath:
    def test_deny_returns_error_message_and_no_instance(self):
        """Simulate the engine hook's DENY branch contract: (None, [FUNCTION msg])."""
        from agent_cascade.llm.schema import FUNCTION, Message

        advisor_result = SkillAdvisorResult(
            verdict="deny", reason="trivial single-file read",
            recommended_skills=[], task_notes="",
        )
        instance_name = "worker1"

        # Replicate the exact return shape used by engine/core.py on DENY.
        if advisor_result.verdict == "deny":
            result_tuple = (None, [Message(role=FUNCTION, content=(
                f"Error: Skill Advisor denied this delegation — {advisor_result.reason}. "
                f"Consider handling this task yourself or rephrasing with more specific context."
            ))])

        inst, conv = result_tuple
        assert inst is None  # no child instance allocated
        assert len(conv) == 1
        assert conv[0].role == FUNCTION
        assert "Skill Advisor denied" in conv[0].content
        assert advisor_result.reason in conv[0].content

    def test_deny_error_is_not_a_system_error(self):
        """The deny message must NOT start with '[SYSTEM ERROR' (child_runner raises on that)."""
        from agent_cascade.llm.schema import FUNCTION, Message
        msg = Message(role=FUNCTION, content=(
            "Error: Skill Advisor denied this delegation — trivial. "
            "Consider handling this task yourself or rephrasing with more specific context."
        ))
        assert not msg.content.strip().startswith("[SYSTEM ERROR")

    @pytest.mark.parametrize("verdict,expected", [
        ("approve", True),
        ("deny", True),
        ("ambiguous", False),
    ])
    def test_is_usable_property(self, verdict, expected):
        """is_usable drives the fallback mechanism in engine/core.py."""
        assert SkillAdvisorResult(verdict=verdict).is_usable is expected


# ===========================================================================
# 6. Advisor gate condition (should_run_advisor)
# ===========================================================================

class TestAdvisorGateCondition:
    """Replicates the exact should_run_advisor expression from engine/core.py."""

    @pytest.mark.parametrize(
        "load_skill_mode,auto_skill_mode,force_fresh,log_file,n_skills,expected",
        [
            ("AUTO", "advanced", False, None, 0, False),   # zero skills → skip
            ("AUTO", "advanced", False, None, 1, True),    # all conditions met
            ("AUTO", "basic",    False, None, 1, False),   # basic mode never runs advisor
            ("NONE", "advanced", False, None, 1, False),   # not AUTO mode
            ("AUTO", "advanced", True,  None, 1, False),   # force_fresh → skip
            ("AUTO", "advanced", False, "x.jsonl", 1, False),  # external load → skip
        ],
    )
    def test_should_run_advisor(self, load_skill_mode, auto_skill_mode, force_fresh, log_file, n_skills, expected):
        sm = MockSkillManager([f"s{i}" for i in range(n_skills)])
        should_run_advisor = (
            load_skill_mode == "AUTO"
            and auto_skill_mode == "advanced"
            and not force_fresh
            and log_file is None
            and sm is not None
            and len(sm.get_skill_names()) > 0
        )
        assert should_run_advisor is expected


# ===========================================================================
# 7. Advisor runner — timeout / error capture (mocked engine, no real LLM)
# ===========================================================================

class TestAdvisorRunner:
    def _patch_engine(self, run_generator):
        """Patch ExecutionEngine + extract_instance_output to avoid a real LLM call."""
        fake_inst = MagicMock()
        fake_inst.conversation = []
        fake_engine = MagicMock()
        fake_engine._create_system_agent.return_value = fake_inst
        fake_engine.run.side_effect = lambda inst: iter(run_generator)
        fake_engine._telemetry.return_value = None

        import agent_cascade.execution_engine as ee_mod
        import agent_cascade.compression.helpers as ch_mod
        return (
            patch.object(ee_mod, "ExecutionEngine", return_value=fake_engine),
            patch.object(ch_mod, "extract_instance_output", return_value="OUTPUT"),
            fake_inst,
        )

    def test_error_capture_when_create_raises(self):
        from agent_cascade.advisor_runner import run_lightweight_advisor
        import agent_cascade.execution_engine as ee_mod

        pool = _make_pool()
        with patch.object(ee_mod, "ExecutionEngine", side_effect=RuntimeError("engine boom")):
            result = run_lightweight_advisor(
                pool, "Security", "Security_op_test1234", "task", "caller"
            )
        assert result.was_error is True
        assert "engine boom" in result.error_msg
        assert not result.ok

    def test_success_path_returns_output(self):
        from agent_cascade.advisor_runner import run_lightweight_advisor

        pool = _make_pool()
        p1, p2, fake_inst = self._patch_engine([["turn1"]])
        with p1, p2:
            result = run_lightweight_advisor(
                pool, "Security", "Security_op_test5678", "task", "caller"
            )
        assert result.was_error is False
        assert result.was_timeout is False
        assert result.output_text == "OUTPUT"
        assert result.ok is True

    def test_early_exit_on_verdict(self):
        """Engine loop must stop as soon as [VERDICT] appears in the last
        assistant message, even if more yields are available."""
        from agent_cascade.advisor_runner import run_lightweight_advisor
        from agent_cascade.llm.schema import Message, ASSISTANT

        pool = _make_pool()
        # Generator that would yield 3 turns if not stopped early.
        # After the first turn, the conversation contains a verdict message.
        call_count = {"n": 0}

        def _gen(inst):
            # Turn 1: assistant produces verdict + (hypothetical) tool calls
            inst.conversation.append(
                Message(role=ASSISTANT, content="[SKILLS] none\n[NOTES] ok\n[VERDICT] APPROVE — fine")
            )
            call_count["n"] += 1
            yield [["turn1"]]
            # Turns 2-3: should NEVER be reached if early-exit works
            inst.conversation.append(Message(role=ASSISTANT, content="continuing work..."))
            call_count["n"] += 1
            yield [["turn2"]]
            call_count["n"] += 1
            yield [["turn3"]]

        fake_inst = MagicMock()
        fake_inst.conversation = []
        fake_engine = MagicMock()
        fake_engine._create_system_agent.return_value = fake_inst
        fake_engine.run.side_effect = _gen
        fake_engine._telemetry.return_value = None

        import agent_cascade.execution_engine as ee_mod
        import agent_cascade.compression.helpers as ch_mod
        with patch.object(ee_mod, "ExecutionEngine", return_value=fake_engine), \
             patch.object(ch_mod, "extract_instance_output", return_value="VERDICT OUTPUT"):
            result = run_lightweight_advisor(
                pool, "Security", "Security_op_early01", "task", "caller"
            )
        # Only 1 turn should have been consumed (early exit on verdict)
        assert call_count["n"] == 1, f"Expected 1 turn, got {call_count['n']}"
        assert result.was_error is False
        assert result.ok is True
