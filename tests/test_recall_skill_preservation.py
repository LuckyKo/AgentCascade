"""Tests for todo.md:115 — skill / system-prompt handling on idle-agent recall.

Verifies the fix in ExecutionEngine._create_and_run_agent:
  * Recall (is_reuse=True) does NOT rebuild or modify the SYSTEM prompt and does
    NOT re-inject skills, even when load_skill="NONE".
  * New instance with load_skill="AUTO" still injects Self-Augmentation + matched
    skills (unchanged behavior).
  * New instance with load_skill="NONE" injects no skills (unchanged behavior).
  * External load (session_was_loaded=True) still injects Self-Augmentation.

All tests are self-contained — no LLM or API server required.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.llm.schema import SYSTEM, USER, Message


# ──────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────

def _make_mock_instance(instance_name="worker1", agent_class="test_agent", conversation=None):
    """Create a minimal AgentInstance mock."""
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._generate_cfg_override = None
    inst.conversation = conversation if conversation is not None else []
    inst._compression_lock = threading.RLock()
    inst._last_token_count_conversation_length = -1
    return inst


def _make_engine(inst, is_reuse=False, session_was_loaded=False, global_mode="AUTO"):
    """Build an ExecutionEngine with lifecycle/stream publisher mocked out.

    global_mode sets pool.settings.default_load_skill_mode — the GLOBAL "Enable
    skills" toggle ('AUTO'=ON, 'NONE'=OFF) that gates Self-Augmentation.

    Returns (engine, mock_pool).
    """
    from agent_cascade.execution_engine import ExecutionEngine

    mock_pool = MagicMock()
    mock_pool.templates = {"test_agent": MagicMock()}
    mock_pool.stopped = False
    mock_pool.is_instance_terminated = MagicMock(return_value=False)
    # GLOBAL "Enable skills" setting (UI toggle → default_load_skill_mode).
    mock_pool.settings.default_load_skill_mode = global_mode
    # No skill manager by default (skills disabled path). Tests that need skills
    # set mock_pool.skill_manager explicitly.

    mock_execution = MagicMock()
    mock_execution.active_stack = []
    mock_execution._state_lock = threading.RLock()
    mock_pool._execution = mock_execution

    engine = ExecutionEngine(mock_pool)

    engine.lifecycle = MagicMock()
    engine.lifecycle.find_or_create_instance = MagicMock(return_value=(inst, is_reuse, session_was_loaded))
    # build_system_message returns a FRESH message so we can detect whether the
    # recall path (which must NOT call it) was taken.
    fresh_sys = Message(role=SYSTEM, content="FRESH SYSTEM MESSAGE")
    engine.lifecycle.build_system_message = MagicMock(return_value=fresh_sys)
    engine.lifecycle.build_task_message = MagicMock(return_value=Message(role=USER, content="task"))
    engine.lifecycle.initialize_conversation = MagicMock(return_value=list(inst.conversation))
    engine.lifecycle.propagate_settings = MagicMock(return_value=None)
    engine.stream_publisher = MagicMock()

    return engine, mock_pool


def _run(engine, load_skill):
    """Drive _create_and_run_agent with a stubbed run() and capture args."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured['called'] = True
        return iter([])

    args = {"task": "do something"}
    if load_skill is not None:
        args["load_skill"] = load_skill

    # Neutralize the auto-skill proposal helper so it doesn't append "[Auto-skill
    # created:]" to the system message on our mock pool — that's unrelated to the
    # behavior under test and would pollute exact-content assertions.
    with patch.object(engine, 'run', side_effect=fake_run), \
            patch("agent_cascade.auto_skill_helpers.run_auto_skill_proposal", return_value=[]):
        engine._create_and_run_agent(
            agent_class="test_agent",
            instance_name="worker1",
            args=args,
            caller="main",
            nest_depth=0,
        )
    return captured


# ──────────────────────────────────────────────
# 1. Recall does NOT change system message / re-inject skills
# ──────────────────────────────────────────────

class TestRecallPreservesSystemPrompt:
    def test_recall_does_not_rebuild_system_message(self):
        """On recall, build_system_message must NOT be called (no fresh rebuild)."""
        existing_sys = Message(role=SYSTEM, content="ORIGINAL SYSTEM PROMPT")
        inst = _make_mock_instance(conversation=[existing_sys])
        engine, _ = _make_engine(inst, is_reuse=True)

        _run(engine, load_skill="NONE")

        # The recall path must not rebuild the system message at all.
        engine.lifecycle.build_system_message.assert_not_called()
        # initialize_conversation must receive the EXISTING system message object.
        # Signature: (inst, sys_msg, task_msg, is_reuse, ...) -> sys_msg is arg index 1.
        init_positional = engine.lifecycle.initialize_conversation.call_args[0]
        assert init_positional[1] is existing_sys, "recall must pass existing conversation[0] as sys_msg"

    def test_recall_ignores_load_skill_none(self):
        """Recall with load_skill='NONE' must not drop Self-Augmentation / rebuild."""
        # System prompt that already contains the Active Skills block (Self-Aug).
        existing_sys = Message(role=SYSTEM, content="You are worker1.\n## Active Skills\n- self-augmentation")
        inst = _make_mock_instance(conversation=[existing_sys])
        engine, mock_pool = _make_engine(inst, is_reuse=True)

        # Even with a skill manager present, recall must not touch it.
        mock_pool.skill_manager = MagicMock()
        mock_pool.skill_manager.resolve_load_skill = MagicMock(return_value=[])
        mock_pool.skill_manager.load_full_instructions = MagicMock(return_value="SELF-AUG")

        _run(engine, load_skill="NONE")

        # No skill resolution / injection happened on recall.
        mock_pool.skill_manager.resolve_load_skill.assert_not_called()
        mock_pool.skill_manager.load_full_instructions.assert_not_called()
        engine.lifecycle.build_system_message.assert_not_called()
        # System content unchanged byte-for-byte.
        assert inst.conversation[0].content == "You are worker1.\n## Active Skills\n- self-augmentation"

    def test_recall_ignores_load_skill_auto(self):
        """Recall with load_skill='AUTO' is also ignored (no re-injection)."""
        existing_sys = Message(role=SYSTEM, content="ORIGINAL SYSTEM PROMPT")
        inst = _make_mock_instance(conversation=[existing_sys])
        engine, mock_pool = _make_engine(inst, is_reuse=True)

        mock_pool.skill_manager = MagicMock()
        mock_pool.skill_manager.resolve_load_skill = MagicMock(return_value=["some-skill"])
        mock_pool.skill_manager.load_full_instructions = MagicMock(return_value="SELF-AUG")

        _run(engine, load_skill="AUTO")

        mock_pool.skill_manager.resolve_load_skill.assert_not_called()
        engine.lifecycle.build_system_message.assert_not_called()
        assert inst.conversation[0].content == "ORIGINAL SYSTEM PROMPT"

    def test_recall_empty_conversation_falls_back_to_build(self):
        """Defensive: recall where conversation is EMPTY must fall back to the build
        path (build_system_message called) rather than crash."""
        inst = _make_mock_instance(conversation=[])
        engine, mock_pool = _make_engine(inst, is_reuse=True, session_was_loaded=False)

        mock_pool.skill_manager = MagicMock()
        mock_pool.skill_manager.resolve_load_skill = MagicMock(return_value=[])
        mock_pool.skill_manager.load_full_instructions = MagicMock(return_value="SELF-AUG")

        _run(engine, load_skill="NONE")

        # Empty conversation → not a recall → build path taken.
        engine.lifecycle.build_system_message.assert_called_once()


# ──────────────────────────────────────────────
# 1b. Recall defensive fallbacks (non-system first message) + external-load edge
# ──────────────────────────────────────────────

class TestRecallFallbacks:
    def test_recall_non_system_first_message_falls_back_to_build(self):
        """Defensive: recall where conversation[0] is NOT a SYSTEM message must fall
        back to the build path rather than crash."""
        first_msg = Message(role=USER, content="some user message")
        inst = _make_mock_instance(conversation=[first_msg])
        engine, mock_pool = _make_engine(inst, is_reuse=True, session_was_loaded=False)

        mock_pool.skill_manager = MagicMock()
        mock_pool.skill_manager.resolve_load_skill = MagicMock(return_value=[])
        mock_pool.skill_manager.load_full_instructions = MagicMock(return_value="SELF-AUG")

        _run(engine, load_skill="NONE")

        # conversation[0] is not SYSTEM → not a recall → build path taken.
        engine.lifecycle.build_system_message.assert_called_once()

    def test_external_load_with_existing_idle_instance_not_treated_as_recall(self):
        """FIX 1: when an idle instance exists AND a log_file restore happened,
        find_or_create_instance returns is_reuse=True AND session_was_loaded=True.
        That is a RESTORED session, NOT a recall — it must build a fresh system
        message and inject Self-Augmentation (global ON)."""
        # Restored session: existing instance with a populated conversation.
        existing_sys = Message(role=SYSTEM, content="RESTORED SYSTEM PROMPT")
        inst = _make_mock_instance(conversation=[existing_sys])
        engine, mock_pool = _make_engine(inst, is_reuse=True, session_was_loaded=True, global_mode="AUTO")

        mock_pool.skill_manager = MagicMock()
        mock_pool.skill_manager.resolve_load_skill = MagicMock(return_value=[])
        mock_pool.skill_manager.load_full_instructions = MagicMock(return_value="SELF-AUGMENTATION-TEXT")

        _run(engine, load_skill="NONE")

        # NOT a recall → fresh system message built...
        engine.lifecycle.build_system_message.assert_called_once()
        # ...and Self-Augmentation injected (global ON), despite per-call NONE.
        assert "self-augmentation" in [c.args[0] for c in mock_pool.skill_manager.load_full_instructions.call_args_list]


# ──────────────────────────────────────────────
# 2. New instance with AUTO still injects Self-Augmentation + matched skills
# ──────────────────────────────────────────────

class TestNewInstanceAutoInjectsSkills:
    def test_new_instance_auto_injects_self_augmentation(self):
        """New instance (empty conversation) with load_skill='AUTO' injects skills."""
        inst = _make_mock_instance(conversation=[])
        engine, mock_pool = _make_engine(inst, is_reuse=False)

        mock_pool.skill_manager = MagicMock()
        mock_pool.skill_manager.resolve_load_skill = MagicMock(return_value=["matched-skill"])
        # load_full_instructions returns the Self-Augmentation instructions.
        mock_pool.skill_manager.load_full_instructions = MagicMock(return_value="SELF-AUGMENTATION-TEXT")

        _run(engine, load_skill="AUTO")

        # Skills were resolved and self-augmentation looked up.
        mock_pool.skill_manager.resolve_load_skill.assert_called_once()
        assert "self-augmentation" in [c.args[0] for c in mock_pool.skill_manager.load_full_instructions.call_args_list]
        # A fresh system message was built (new-instance path).
        engine.lifecycle.build_system_message.assert_called_once()


# ──────────────────────────────────────────────
# 3. New instance with per-call NONE: matched suppressed, Self-Aug kept (global ON)
# ──────────────────────────────────────────────

class TestNewInstancePerCallNone:
    def test_per_call_none_still_injects_self_augmentation_when_global_on(self):
        """CORRECTED RULE: new instance + per-call load_skill='NONE' with the GLOBAL
        'Enable skills' toggle ON must STILL inject Self-Augmentation; only matched/
        auto skills are suppressed (resolve_load_skill NOT called)."""
        inst = _make_mock_instance(conversation=[])
        engine, mock_pool = _make_engine(inst, is_reuse=False, global_mode="AUTO")

        mock_pool.skill_manager = MagicMock()
        # If resolve were (wrongly) called it would return a matched skill — assert it isn't.
        mock_pool.skill_manager.resolve_load_skill = MagicMock(return_value=["should-not-run"])
        mock_pool.skill_manager.load_full_instructions = MagicMock(return_value="SELF-AUGMENTATION-TEXT")

        _run(engine, load_skill="NONE")

        # Per-call NONE suppresses matched-skill resolution...
        mock_pool.skill_manager.resolve_load_skill.assert_not_called()
        # ...but Self-Augmentation is still looked up (global toggle ON).
        assert "self-augmentation" in [c.args[0] for c in mock_pool.skill_manager.load_full_instructions.call_args_list]
        engine.lifecycle.build_system_message.assert_called_once()

    def test_global_off_injects_nothing_even_if_per_call_auto(self):
        """CORRECTED RULE: when the GLOBAL 'Enable skills' toggle is OFF (mode NONE),
        NOTHING is injected — even if the per-call load_skill arg is AUTO."""
        inst = _make_mock_instance(conversation=[])
        engine, mock_pool = _make_engine(inst, is_reuse=False, global_mode="NONE")

        mock_pool.skill_manager = MagicMock()
        mock_pool.skill_manager.resolve_load_skill = MagicMock(return_value=["should-not-run"])
        mock_pool.skill_manager.load_full_instructions = MagicMock(return_value="SELF-AUGMENTATION-TEXT")

        _run(engine, load_skill="AUTO")

        # Global OFF gates the whole skill block — neither resolve nor self-aug lookup.
        mock_pool.skill_manager.resolve_load_skill.assert_not_called()
        mock_pool.skill_manager.load_full_instructions.assert_not_called()
        engine.lifecycle.build_system_message.assert_called_once()


# ──────────────────────────────────────────────
# 4. External load still injects Self-Augmentation
# ──────────────────────────────────────────────

class TestExternalLoadInjectsSelfAugmentation:
    def test_external_load_injects_self_augmentation(self):
        """session_was_loaded=True (fresh instance restored from log) builds fresh
        system message and injects skills (Self-Augmentation included)."""
        # External load produces a FRESH instance with empty conversation.
        inst = _make_mock_instance(conversation=[])
        engine, mock_pool = _make_engine(inst, is_reuse=False, session_was_loaded=True)

        mock_pool.skill_manager = MagicMock()
        mock_pool.skill_manager.resolve_load_skill = MagicMock(return_value=[])
        mock_pool.skill_manager.load_full_instructions = MagicMock(return_value="SELF-AUGMENTATION-TEXT")

        _run(engine, load_skill="AUTO")

        # Fresh system message built + self-augmentation looked up and injected.
        engine.lifecycle.build_system_message.assert_called_once()
        assert "self-augmentation" in [c.args[0] for c in mock_pool.skill_manager.load_full_instructions.call_args_list]
