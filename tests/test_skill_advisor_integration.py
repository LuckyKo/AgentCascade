"""Integration tests for the AUTO Skill Advisor gate inside
``ExecutionEngine._create_and_run_agent()``.

Unlike ``test_skill_advisor.py`` (which unit-tests parsing / prompt building /
the advisor module in isolation), these tests drive the REAL gate block in
``core.py`` to verify the pieces are wired together in the right ORDER:

  1. recall check  →  (if new) run_skill_advisor  →  find_or_create_instance
  2. DENY         →  return error, NO instance allocated
  3. APPROVE      →  advisor's recommended skills injected into sys_msg
  4. ambiguous / exception →  fallback to basic keyword match (resolve_load_skill)
  5. basic mode   →  advisor never invoked
  6. recall path  →  advisor skipped, existing system message preserved

We mock at the LLM boundary (``run_skill_advisor``) and at instance allocation
(``find_or_create_instance``), but we do NOT mock:
  - the gate condition logic (should_run_advisor)
  - the early recall check
  - parse_advisor_output (the advisor module is real; only its LLM call is faked)

No real LLM calls, no network.

Run: python -m pytest tests/test_skill_advisor_integration.py --tb=short -q
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the project root is on sys.path so imports resolve correctly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_cascade.agent_instance import AgentState
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import ASSISTANT, SYSTEM, FUNCTION, Message
from agent_cascade.skills.advisor import SkillAdvisorResult


# Sentinel for _run_gate(load_skill_value=...) meaning "omit the key from args"
# so the gate falls back to pool.settings.default_load_skill_mode.
_OMIT_LOAD_SKILL = object()


# ── Test doubles ──────────────────────────────────────────────────────────────

class FakeSkillManager:
    """Minimal stand-in for SkillManager covering the gate's surface.

    ``resolve_load_skill`` is a MagicMock so tests can assert the basic keyword-match
    fallback path actually ran (vs. advisor-recommended injection).
    """

    def __init__(self, names):
        self._names = list(names)
        # Basic keyword-match fallback — returns a distinctive body so tests can tell
        # the fallback path ran. Wrapped in MagicMock to record/inspect calls.
        self.resolve_load_skill = MagicMock(
            return_value=["# basic-keyword-match-skill\nresolved via basic matching"]
        )

    def get_skill_names(self):
        return list(self._names)

    def get_all_metadata(self):
        return [{"name": n, "description": f"desc for {n}"} for n in self._names]

    def load_full_instructions(self, name):
        for n in self._names:
            if n.lower() == str(name).lower():
                return f"# {n}\ninstructions for {n}"
        return None


class FakeSettings:
    def __init__(self, auto_skill_mode="advanced", default_load_skill_mode="AUTO"):
        self.auto_skill_mode = auto_skill_mode
        self.default_load_skill_mode = default_load_skill_mode


class FakeExecutionState:
    """Stand-in for pool._execution — exposes the state lock + active stack."""

    def __init__(self):
        self._state_lock = threading.RLock()
        self.active_stack = []


class FakePool:
    """Minimal fake AgentPool exposing only what the advisor gate touches.

    ``order`` is a shared list recording the sequence of key events (advisor run,
    allocation) so tests can assert the advisor runs BEFORE instance creation —
    the core invariant of the gate.
    """

    def __init__(self, skill_manager, auto_skill_mode="advanced", default_load_skill_mode="AUTO"):
        self.settings = FakeSettings(auto_skill_mode, default_load_skill_mode)
        self.skill_manager = skill_manager
        self._execution = FakeExecutionState()
        self.instances = {}  # name → instance (for the early recall check)
        self.order = []     # event sequence: "advisor" then "allocate"

    def is_instance_terminated(self, instance_name):
        return False


class FakeInstance:
    """Minimal stand-in for AgentInstance used by the gate + lifecycle mocks."""

    def __init__(self, agent_class="coder", conversation=None):
        self.agent_class = agent_class
        self.conversation = conversation if conversation is not None else []
        self._state_lock = threading.RLock()
        self.state = AgentState.IDLE  # gate compares against AgentState.IDLE/TERMINATED
        self._nest_depth = 0  # used by active_stack tracking after allocation


class FakeLifecycle:
    """Stand-in for AgentLifecycleManager — records calls, returns fakes.

    ``find_or_create_instance`` is the allocation boundary we assert on. It
    registers the returned instance in ``pool.instances`` so that a subsequent
    recall check (or the real lifecycle reuse logic) would see it.
    """

    def __init__(self, pool):
        self.pool = pool
        self.find_or_create_instance = MagicMock(
            side_effect=self._find_or_create
        )
        self.build_system_message = MagicMock(return_value=Message(role=SYSTEM, content="SYS"))
        self.build_task_message = MagicMock(return_value=Message(role="user", content="TASK"))
        self.initialize_conversation = MagicMock(return_value=[Message(role=SYSTEM, content="SYS")])
        self.propagate_settings = MagicMock()

    def _find_or_create(self, agent_class, instance_name, caller, nest_depth, force_fresh, log_file=None):
        self.pool.order.append("allocate")  # record allocation for order assertions
        inst = FakeInstance(agent_class)
        self.pool.instances[instance_name] = inst
        return inst, False, False  # (inst, is_reuse=False, session_was_loaded=False)


def _noop_run_generator(inst):
    """Yield nothing — the real LLM turn loop is irrelevant to the gate under test.

    Signature matches ``ExecutionEngine.run(instance)`` (called as ``self.run(inst)``).
    """
    return iter(())


def _build_engine(pool, lifecycle):
    """Real gate logic; only the LLM boundary + allocation are faked.

    Mirrors the ``ExecutionEngine.__new__`` pattern from
    test_auto_continue_turn_budget.py — we bypass __init__ (which would build
    real handlers against a fake pool) and inject just what _create_and_run_agent touches.

    The gate block runs BEFORE find_or_create_instance() and the system/skill
    injection happens right after; everything downstream of that (the real LLM
    turn loop via self.run) is irrelevant to integration wiring, so we short-circuit it.
    """
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.pool = pool
    engine.lifecycle = lifecycle
    engine.stream_publisher = MagicMock()
    engine._telemetry = lambda: None  # skip telemetry recording
    engine.run = _noop_run_generator  # short-circuit the real LLM loop
    return engine


def _run_gate(advisor_result, auto_skill_mode="advanced", default_load_skill_mode="AUTO",
              skill_names=("docker-best-practices", "httpx-connection-pooling"),
              pre_existing_instance=None, advisor_raises=False, load_skill_value="AUTO"):
    """Drive the real gate block and return (engine, pool, lifecycle, result).

    ``advisor_result`` is returned by the patched run_skill_advisor unless
    ``advisor_raises`` is True (then it raises RuntimeError to exercise the
    exception → ambiguous fallback path).

    ``load_skill_value`` is placed in args["load_skill"]. Pass the sentinel
    ``_OMIT_LOAD_SKILL`` to omit the key entirely, so the gate falls back to
    ``pool.settings.default_load_skill_mode`` (exercises the None branch).
    """
    sm = FakeSkillManager(skill_names)
    pool = FakePool(sm, auto_skill_mode=auto_skill_mode, default_load_skill_mode=default_load_skill_mode)
    if pre_existing_instance is not None:
        pool.instances["worker1"] = pre_existing_instance

    lifecycle = FakeLifecycle(pool)
    engine = _build_engine(pool, lifecycle)

    args = {
        "task": "Set up a Docker deployment",
        "context": "Use the docker skill",
    }
    if load_skill_value is not _OMIT_LOAD_SKILL:
        args["load_skill"] = load_skill_value

    def _fake_advisor(**kwargs):
        pool.order.append("advisor")  # record advisor run for order assertions
        if advisor_raises:
            raise RuntimeError("simulated LLM timeout")
        return advisor_result

    with patch("agent_cascade.skills.advisor.run_skill_advisor", side_effect=_fake_advisor) as mock_advisor:
        result = engine._create_and_run_agent(
            agent_class="coder", instance_name="worker1",
            args=args, caller="maine", nest_depth=0, force_fresh=False,
        )

    return engine, pool, lifecycle, mock_advisor, result


# ===========================================================================
# 1. Advanced mode + AUTO → advisor runs, skills injected
# ===========================================================================

class TestAdvisorApprove:
    def test_advisor_called_and_skills_injected(self):
        advisor_result = SkillAdvisorResult(
            verdict="approve",
            reason="looks like a docker task",
            recommended_skills=["docker-best-practices"],
            task_notes="Use Docker for this",
        )
        _, pool, lifecycle, mock_advisor, result = _run_gate(advisor_result)

        # Advisor was invoked exactly once with the right params.
        assert mock_advisor.call_count == 1
        call = mock_advisor.call_args.kwargs
        assert call["task_text"] == "Set up a Docker deployment"
        assert call["context_text"] == "Use the docker skill"
        assert call["agent_class"] == "coder"
        assert call["caller_name"] == "maine"

        # Instance was allocated AFTER the advisor approved — order is the core
        # invariant: the gate must run BEFORE find_or_create_instance so a DENY
        # never allocates. pool.order records ["advisor", "allocate"].
        lifecycle.find_or_create_instance.assert_called_once()
        assert pool.order == ["advisor", "allocate"]

        # Recommended skill body was injected into the system message (advisor's
        # semantic recommendation, NOT basic keyword match).
        sys_msg = lifecycle.build_system_message.return_value
        assert "# docker-best-practices" in sys_msg.content
        assert "instructions for docker-best-practices" in sys_msg.content
        # Basic fallback must NOT have been used on the approve path.
        assert "# basic-keyword-match-skill" not in sys_msg.content
        pool.skill_manager.resolve_load_skill.assert_not_called()


# ===========================================================================
# 2. Advanced mode + DENY → no instance created, error returned
# ===========================================================================

class TestAdvisorDeny:
    def test_deny_prevents_allocation(self):
        advisor_result = SkillAdvisorResult(
            verdict="deny",
            reason="trivial task",
            recommended_skills=[],
            task_notes="",
        )
        _, pool, lifecycle, mock_advisor, result = _run_gate(advisor_result)

        # Advisor ran and returned deny.
        assert mock_advisor.call_count == 1

        # No instance was allocated — the whole point of gating BEFORE creation.
        lifecycle.find_or_create_instance.assert_not_called()
        # Order proves the gate ran first and short-circuited before allocation.
        assert pool.order == ["advisor"]  # "allocate" never recorded

        # Return shape: (None, [Message]) with denial text from the advisor gate.
        assert isinstance(result, tuple) and len(result) == 2
        inst, msgs = result
        assert inst is None
        assert len(msgs) == 1
        # ASSISTANT role (not FUNCTION) so extract_instance_output surfaces the text
        assert msgs[0].role == ASSISTANT
        assert "[SKILL-ADVISOR DENIED]" in msgs[0].content
        assert "trivial task" in msgs[0].content


# ===========================================================================
# 3. Advanced mode + timeout/error → fallback to basic keyword match
# ===========================================================================

class TestAdvisorFallback:
    def test_exception_falls_back_to_basic_match(self):
        # run_skill_advisor raises → gate catches and treats as ambiguous.
        _, pool, lifecycle, mock_advisor, result = _run_gate(
            advisor_result=None, advisor_raises=True
        )

        # Advisor was attempted (raised), so it WAS called.
        assert mock_advisor.call_count == 1

        # Instance still allocated (fallback proceeds as if basic mode).
        lifecycle.find_or_create_instance.assert_called_once()

        # Fallback path: resolve_load_skill (basic keyword match) was ACTUALLY called,
        # NOT the advisor's recommendations. The injected skill body is the basic one.
        pool.skill_manager.resolve_load_skill.assert_called_once()
        sys_msg = lifecycle.build_system_message.return_value
        assert "# basic-keyword-match-skill" in sys_msg.content

    def test_ambiguous_verdict_falls_back_to_basic_match(self):
        advisor_result = SkillAdvisorResult(
            verdict="ambiguous",
            reason="no [VERDICT] marker found",
            recommended_skills=[],
            task_notes="",
        )
        _, pool, lifecycle, mock_advisor, result = _run_gate(advisor_result)

        assert mock_advisor.call_count == 1
        lifecycle.find_or_create_instance.assert_called_once()
        # Fallback used basic keyword match (resolve_load_skill), not advisor skills.
        pool.skill_manager.resolve_load_skill.assert_called_once()
        sys_msg = lifecycle.build_system_message.return_value
        assert "# basic-keyword-match-skill" in sys_msg.content


# ===========================================================================
# 4. Basic mode → advisor never called
# ===========================================================================

class TestBasicModeSkipsAdvisor:
    def test_basic_mode_never_invokes_advisor(self):
        advisor_result = SkillAdvisorResult(verdict="approve")
        _, pool, lifecycle, mock_advisor, result = _run_gate(
            advisor_result, auto_skill_mode="basic"
        )

        # The gate condition (auto_skill_mode == "advanced") is False → no advisor.
        assert mock_advisor.call_count == 0

        # Still allocates and uses basic keyword matching (resolve_load_skill).
        lifecycle.find_or_create_instance.assert_called_once()
        pool.skill_manager.resolve_load_skill.assert_called_once()
        sys_msg = lifecycle.build_system_message.return_value
        assert "# basic-keyword-match-skill" in sys_msg.content


# ===========================================================================
# 5. Recall path → advisor skipped, existing system message preserved
# ===========================================================================

class TestRecallSkipsAdvisor:
    def test_recall_of_idle_instance_skips_advisor(self):
        # Pre-existing idle instance with a valid SYSTEM conversation[0].
        existing = FakeInstance(
            agent_class="coder",
            conversation=[Message(role=SYSTEM, content="ORIGINAL SYS")],
        )
        advisor_result = SkillAdvisorResult(verdict="approve", recommended_skills=["docker-best-practices"])

        # The early recall check looks up pool.instances BEFORE find_or_create_instance,
        # so the existing instance MUST be registered there for the advisor to be skipped.
        sm = FakeSkillManager(["docker-best-practices"])
        pool = FakePool(sm, auto_skill_mode="advanced")
        pool.instances["worker1"] = existing

        lifecycle = FakeLifecycle(pool)
        # Recall: reuse=True, session_was_loaded=False → _is_recall True (real logic at line 2903).
        # NOTE: side_effect already set in FakeLifecycle; a plain return_value does NOT override it.
        # Reconfigure the mock to return the existing instance as a recall (no side_effect).
        lifecycle.find_or_create_instance = MagicMock(return_value=(existing, True, False))

        engine = _build_engine(pool, lifecycle)
        args = {"task": "do it", "context": "", "load_skill": "AUTO"}

        def _record_advisor(*a, **kw):
            pool.order.append("advisor")
            return SkillAdvisorResult(verdict="approve")

        with patch("agent_cascade.skills.advisor.run_skill_advisor", side_effect=_record_advisor) as mock_advisor:
            result = engine._create_and_run_agent(
                agent_class="coder", instance_name="worker1",
                args=args, caller="maine", nest_depth=0, force_fresh=False,
            )

        # Recall detected BEFORE the advisor → advisor never invoked.
        assert mock_advisor.call_count == 0

        # The recall path still allocates (reuses) but NEVER runs the advisor.
        lifecycle.find_or_create_instance.assert_called_once()
        # No "advisor" event recorded — the early recall check skipped it.
        assert "advisor" not in pool.order

        # The existing system message is preserved verbatim (no rebuild, no skill injection).
        sys_msg = lifecycle.initialize_conversation.call_args.args[1]
        assert sys_msg is existing.conversation[0]
        assert sys_msg.content == "ORIGINAL SYS"


# ===========================================================================
# 6. load_skill value normalization (regression: LLM passes '["AUTO"]' string)
# ===========================================================================

class TestLoadSkillNormalization:
    """The gate must normalize the many shapes an LLM can emit for load_skill
    into a plain "AUTO"/"NONE" before comparing modes. The bug: a JSON-encoded
    list string like '["AUTO"]' failed the old ``== "AUTO"`` comparison, so the
    advisor silently never ran. Each scenario asserts only whether the advisor
    was invoked (the single observable that distinguishes AUTO vs NONE mode)."""

    def _approve(self):
        return SkillAdvisorResult(
            verdict="approve", reason="ok", recommended_skills=[], task_notes=""
        )

    # ── AUTO variants → advisor runs ────────────────────────────────────────

    def test_plain_auto_string_runs_advisor(self):
        _, pool, lifecycle, mock_advisor, _ = _run_gate(
            self._approve(), load_skill_value="AUTO"
        )
        assert mock_advisor.call_count == 1

    def test_json_encoded_auto_list_string_runs_advisor(self):
        # The actual bug: LLM emitted the JSON string '["AUTO"]'.
        _, pool, lifecycle, mock_advisor, _ = _run_gate(
            self._approve(), load_skill_value='["AUTO"]'
        )
        assert mock_advisor.call_count == 1

    def test_python_auto_list_runs_advisor(self):
        _, pool, lifecycle, mock_advisor, _ = _run_gate(
            self._approve(), load_skill_value=["AUTO"]
        )
        assert mock_advisor.call_count == 1

    def test_multi_element_explicit_skills_still_runs_advisor(self):
        # Multi-element list is NOT a mode — normalization skips it (len != 1),
        # final fallback treats non-str as "AUTO" → advisor still runs so it can
        # validate/recommend against the explicit skill names.
        _, pool, lifecycle, mock_advisor, _ = _run_gate(
            self._approve(),
            load_skill_value=["docker-best-practices", "httpx-connection-pooling"],
        )
        assert mock_advisor.call_count == 1

    # ── NONE variants → advisor does NOT run ────────────────────────────────

    def test_plain_none_string_skips_advisor(self):
        _, pool, lifecycle, mock_advisor, _ = _run_gate(
            self._approve(), load_skill_value="NONE"
        )
        assert mock_advisor.call_count == 0

    def test_json_encoded_none_list_string_skips_advisor(self):
        _, pool, lifecycle, mock_advisor, _ = _run_gate(
            self._approve(), load_skill_value='["NONE"]'
        )
        assert mock_advisor.call_count == 0

    def test_python_none_list_skips_advisor(self):
        _, pool, lifecycle, mock_advisor, _ = _run_gate(
            self._approve(), load_skill_value=["NONE"]
        )
        assert mock_advisor.call_count == 0


# ===========================================================================
# 7. load_skill omitted → falls back to default_load_skill_mode
# ===========================================================================

class TestLoadSkillDefaultFallback:
    """When args has no 'load_skill' key, the gate uses
    pool.settings.default_load_skill_mode as the value."""

    def _approve(self):
        return SkillAdvisorResult(
            verdict="approve", reason="ok", recommended_skills=[], task_notes=""
        )

    def test_default_auto_runs_advisor(self):
        _, pool, lifecycle, mock_advisor, _ = _run_gate(
            self._approve(), default_load_skill_mode="AUTO",
            load_skill_value=_OMIT_LOAD_SKILL,
        )
        assert mock_advisor.call_count == 1

    def test_default_none_skips_advisor(self):
        _, pool, lifecycle, mock_advisor, _ = _run_gate(
            self._approve(), default_load_skill_mode="NONE",
            load_skill_value=_OMIT_LOAD_SKILL,
        )
        assert mock_advisor.call_count == 0
