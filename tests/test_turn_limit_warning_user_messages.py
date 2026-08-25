"""Regression tests: 50%/90% turn-limit warnings are SEPARATE USER-role messages.

Background: the main loop in ``ExecutionEngine.run()`` emits three turn-limit
warnings — "Halfway" (50%), "Turn limit approaching" (90%) and "Final turn".
Historically the first two were stitched INLINE into the last message's content
via ``_append_system_notification()``, while only the final-turn warning was a
proper, distinct USER-role message. This change makes the 50%/90% warnings use
the SAME pattern as the final-turn warning:

    warn_user = self._make_user_message(warn_msg)
    self._append_and_log(instance, warn_user)   # -> instance.conversation + JSONL log
    llm_messages.append(warn_user)              # -> what actually goes to the LLM

These tests drive the REAL ``ExecutionEngine.run()`` generator with a scripted
fake LLM (same harness family as test_auto_continue_turn_budget.py) and assert:

1. The warning is present in the LLM messages (``llm.calls``) at the right turn.
2. The warning message has role == USER.
3. It appears as a SEPARATE message (a distinct Message whose OWN content holds
   the warning text — not stitched into a previous message's content).
4. No duplication: across the whole run each warning text appears exactly once
   in ``instance.conversation``.

Driving multiple turns: a clean assistant response with no tool call makes the
engine treat the agent as "complete" and break after 1 LLM call. To keep the loop
running for max_turns iterations, the fake LLM emits an assistant message carrying
a tool call. Because ``get_template()`` returns None in the fake pool, the real
tool-execution path takes its self-contained "auto-deny" branch (appends a
FUNCTION result, sets used_any_tool=True) — so every iteration is exactly one
real turn and no actual tool runs.

Threshold math (from core.py):
    turns_50pct = max(3, int(max_turns * 0.5))
    turns_90pct = max(2, int(max_turns * 0.1))

Run: pytest tests/test_turn_limit_warning_user_messages.py -v
"""

import time
from typing import List

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import ASSISTANT, USER, Message


# ── Harness (mirrors test_auto_continue_turn_budget.py) ──────────────────────

def _make_instance(max_turns: int) -> AgentInstance:
    """Minimal real AgentInstance with a pre-seeded conversation (so _setup_turn works)."""
    now = time.monotonic()
    inst = AgentInstance(
        instance_name="WarnAgent",
        agent_class="coder",
        conversation=[Message(role=USER, content="do the task")],
        created_at=now,
        last_activity=now,
        latest_marker_index=-1,
    )
    inst.max_turns = max_turns
    return inst


def _tool_call_msg(i: int) -> Message:
    """Assistant message with a tool call (unique name per turn).

    Each turn uses a distinct tool name so the conversation never contains two
    identical assistant calls (avoids tripping loop detection / confusing the
    "distinct message" assertions). The fake template's function_map includes every
    such name, so the real ``_execute_detected_tools`` dispatches through our stub
    dispatcher and appends a FUNCTION result — used_any_tool=True keeps the loop
    alive for one more turn. This is what lets us drive max_turns iterations of the
    real run().
    """
    return Message(role=ASSISTANT, content="", function_call={"name": f"noop_{i}", "arguments": "{}"})


class _FakeLogger:
    def __init__(self):
        self.log_path = None
        self.data = {"history": []}

    def log_message(self, msg):
        self.data["history"].append(msg)


class _FakeCompressionHandler:
    """No-op stand-in for CompressionHandler (rollback/compress commands never fire)."""

    def handle_rollback_command(self, instance, messages, llm_messages, response=None):
        return False

    def handle_compress_command(self, instance, messages, llm_messages, response=None):
        return False

    def _assemble_tool_result(self, *args, **kwargs):
        """Return the tool result unchanged (no spillover/truncation in tests)."""
        # args: (instance, tool_result, ...) — tool_result is the 2nd positional arg.
        return args[1] if len(args) > 1 else kwargs.get("tool_result", "")

    def _legacy_drain_tool_result(self, instance, tool_result):
        return tool_result


class _FakeToolDispatcher:
    """Stub dispatcher: returns a trivial result so no real tool ever executes."""

    def execute_tool(self, instance, tool_name, tool_args, llm_messages, function_id=None):
        return f"[stub] {tool_name} executed"


class _FakeTemplate:
    """Minimal template exposing a function_map that includes the 'noop' tool.

    Because 'noop' IS in function_map, the real ``_execute_detected_tools`` does NOT
    take the auto-deny branch; it dispatches through our stub dispatcher and appends
    a FUNCTION result — keeping used_any_tool=True so the loop runs one more turn.
    """

    def __init__(self, tool_names):
        self.name = "coder"
        self.agent_type = "coder"
        # Include every per-turn tool name so the real execution path treats each as
        # a valid (non-auto-denied) tool and dispatches through our stub dispatcher.
        self.function_map = {n: (lambda **kw: f"[stub] {n} executed") for n in tool_names}
        self.llm = type("LLM", (), {"generate_cfg": {}})()


class _FakePool:
    """Minimal fake AgentPool exposing only what ExecutionEngine.run() touches."""

    def __init__(self, max_turns: int):
        # Pre-build the template with every per-turn tool name (noop_0..noop_{max-1}).
        self._template = _FakeTemplate([f"noop_{i}" for i in range(max_turns)])
        self.settings = type("Settings", (), {
            "auto_continue": True,
            "tail_sync_check_enabled": False,          # keep tail-sync off the filesystem path
            "compression_force_threshold": 96.0,
            "compression_warning_threshold": 90.0,
            "compression_context_reserve_tokens": 2048,
            "auto_rollback_on_loop": False,         # disable loop-detection rollback (repetitive scripted output would trip it)
            "max_auto_rollbacks": 5,
            "cache_threshold_chars": 100000,        # tool-output cache threshold (large → never spillover)
        })()
        self.stopped = False
        self._run_generation = 0
        self._config_version = 0
        self._compression_halted = set()
        self._halted_instances = set()
        self.telemetry = None
        self.api_router = None
        self.llm_cfg = {}
        self.instances: dict = {}

    def get_template(self, agent_class):
        # A template whose function_map includes every per-turn tool name, so the real
        # tool-execution path dispatches through our stub dispatcher (used_any_tool=True
        # → loop continues one more turn).
        return self._template

    def get_instance(self, inst_name):
        return self.instances.get(inst_name)

    def get_conversation(self, inst_name):
        inst = self.instances.get(inst_name)
        return list(inst.conversation) if inst else []

    def get_logger(self, inst_name, agent_class):
        return _FakeLogger()

    def is_instance_terminated(self, inst_name):
        return False

    def has_pending(self, inst_name):
        return False

    def has_messages(self, inst_name):
        return False

    def drain_queue(self, inst_name):
        return []

    def enqueue_message(self, inst_name, text):
        pass

    def _mark_activity(self, inst_name):
        pass

    def is_paused(self):
        return False  # never paused in the fake — tool auto-deny path proceeds immediately

    def wait_if_paused(self, timeout=1.0):
        return None

    def slice_history_for_llm(self, conv):
        return list(conv)


class _ScriptedLLM:
    """Stands in for ``_call_llm_with_injection``: yields one scripted message per call.

    Records the llm_messages it received on every call (used to assert that a
    warning is present as a distinct USER message at the right turn).
    """

    def __init__(self, script: List[Message]):
        self._script = list(script)
        self.calls: List[List[Message]] = []  # llm_messages snapshot per LLM call

    def __call__(self, instance, llm_messages):
        """Signature matches ``_call_llm_with_injection(instance, llm_messages)``."""
        self.calls.append(list(llm_messages))
        if self._script:
            yield self._script.pop(0)
        else:
            raise AssertionError("LLM called more times than scripted")


def _build_engine(pool: _FakePool, instance: AgentInstance, llm: _ScriptedLLM) -> ExecutionEngine:
    """Real run() loop; only the LLM call is faked."""
    pool.instances[instance.instance_name] = instance
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.pool = pool
    engine._my_generation = 0
    engine.compression_handler = _FakeCompressionHandler()
    engine.tool_dispatcher = _FakeToolDispatcher()
    engine._call_llm_with_injection = llm
    return engine


def _run(max_turns: int):
    """Drive the real run() loop for max_turns turns; returns (llm, instance)."""
    pool = _FakePool(max_turns)
    # One unique tool-call response per turn keeps the loop alive for exactly max_turns calls.
    llm = _ScriptedLLM([_tool_call_msg(i) for i in range(max_turns)])
    instance = _make_instance(max_turns)
    engine = _build_engine(pool, instance, llm)
    list(engine.run(instance))  # exhaust the generator
    return llm, instance


# ── Assertion helpers ────────────────────────────────────────────────────────

def _find_warning_in_call(call_msgs: List[Message], text_fragment: str):
    """Return the distinct Message in a single LLM call whose OWN content holds the fragment.

    Returns None if no such standalone message exists (i.e. it was stitched into
    another message's content, or absent). A "standalone" match means a message
    whose content starts with the warning prefix — not merely contains the
    fragment as a substring of some other (larger) message.
    """
    for m in call_msgs:
        if not isinstance(m, Message):
            continue
        content = m.content
        if not isinstance(content, str):
            continue
        if text_fragment in content and content.strip().startswith("[SYSTEM WARNING:"):
            return m
    return None


def _count_in_conversation(instance: AgentInstance, text_fragment: str) -> int:
    """Count how many conversation messages contain the fragment in their own content."""
    count = 0
    for msg in instance.conversation:
        role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
        content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
        if isinstance(content, str) and text_fragment in content:
            count += 1
    return count


def _msg_role(m) -> str:
    return m.get('role') if isinstance(m, dict) else getattr(m, 'role', '')


# ── Tests ────────────────────────────────────────────────────────────────────

class TestTurnLimitWarningUserMessages:
    """50%/90% warnings are distinct USER-role messages, not inline-stitched."""

    def test_50pct_warning_is_separate_user_message(self):
        """max_turns=6 → turns_50pct=max(3,int(3))=3. The "Halfway" warning fires when
        turns_available==3 (4th LLM call) and must be a distinct USER-role message."""
        max_turns = 6
        llm, instance = _run(max_turns)

        # turns_available==3 occurs on the 4th LLM call (index 3).
        halfway_call = llm.calls[3]
        warn = _find_warning_in_call(halfway_call, "Halfway through your turn budget")
        assert warn is not None, (
            "50% 'Halfway' warning not found as a standalone message in the LLM call "
            f"where turns_available=={max_turns - 3 + 1}"
        )
        # It must be a USER-role message.
        assert _msg_role(warn) == USER, f"50% warning role is {_msg_role(warn)!r}, expected USER"

    def test_90pct_warning_is_separate_user_message(self):
        """max_turns=6 → turns_90pct=max(2,int(0.6))=2. The 90% warning fires when
        turns_available==2 (5th LLM call) and must be a distinct USER-role message."""
        max_turns = 6
        llm, instance = _run(max_turns)

        # turns_available==2 occurs on the 5th LLM call (index 4).
        ninety_call = llm.calls[4]
        warn = _find_warning_in_call(ninety_call, "Turn limit approaching")
        assert warn is not None, (
            "90% 'Turn limit approaching' warning not found as a standalone message in the "
            f"LLM call where turns_available=={max_turns - 2 + 1}"
        )
        assert _msg_role(warn) == USER, f"90% warning role is {_msg_role(warn)!r}, expected USER"

    def test_50pct_warning_not_stitched_into_previous_message(self):
        """The 50% warning must be its OWN message, not appended to the prior assistant msg.

        A standalone USER message contains ONLY the warning text (a fresh message),
        proving it was not stitched onto the tail of a previous message's content.
        """
        max_turns = 6
        llm, instance = _run(max_turns)

        halfway_call = llm.calls[3]
        warn = _find_warning_in_call(halfway_call, "Halfway through your turn budget")
        assert warn is not None, "50% warning missing"
        content = warn.content if isinstance(warn, Message) else warn.get('content', '')
        # A standalone USER message contains ONLY the warning (no preceding
        # assistant text glued onto it).
        assert content.strip().startswith("[SYSTEM WARNING: Halfway"), (
            f"50% warning is not a clean standalone message; content={content[:80]!r}"
        )

    def test_no_duplication_in_conversation(self):
        """Across the whole run each warning text appears EXACTLY once in instance.conversation."""
        max_turns = 6
        llm, instance = _run(max_turns)

        halfway_count = _count_in_conversation(instance, "Halfway through your turn budget")
        ninety_count = _count_in_conversation(instance, "Turn limit approaching")
        final_count = _count_in_conversation(instance, "Final turn.")

        assert halfway_count == 1, f"50% warning appears {halfway_count}x in conversation (expected 1)"
        assert ninety_count == 1, f"90% warning appears {ninety_count}x in conversation (expected 1)"
        assert final_count == 1, f"final-turn warning appears {final_count}x in conversation (expected 1)"

    def test_max_turns_4_both_warnings_fire_once(self):
        """max_turns=4 → turns_50pct=max(3,int(2))=3, turns_90pct=max(2,int(0.4))=2.

        Exercises a smaller budget where both thresholds are reachable and distinct
        from the final turn (turns_available==1). Both warnings must fire exactly once.
        """
        max_turns = 4
        llm, instance = _run(max_turns)

        # turns_available==3 → call index 1 ; turns_available==2 → call index 2.
        assert _find_warning_in_call(llm.calls[1], "Halfway through your turn budget") is not None, \
            "50% warning missing on the turns_available==3 call"
        assert _find_warning_in_call(llm.calls[2], "Turn limit approaching") is not None, \
            "90% warning missing on the turns_available==2 call"

        assert _count_in_conversation(instance, "Halfway through your turn budget") == 1
        assert _count_in_conversation(instance, "Turn limit approaching") == 1

    def test_warning_texts_unchanged(self):
        """The exact warning text (incl. [SYSTEM WARNING: ...] prefixes) is preserved."""
        max_turns = 6
        llm, instance = _run(max_turns)

        # Gather all standalone USER messages across the whole conversation.
        user_msgs = []
        for msg in instance.conversation:
            role = _msg_role(msg)
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if role == USER and isinstance(content, str):
                user_msgs.append(content)

        expected_halfway = (
            "[SYSTEM WARNING: Halfway through your turn budget. "
            f"You have 3 turn(s) remaining out of {max_turns} total. "
            "Assess your progress and plan remaining steps.]"
        )
        expected_ninety = (
            "[SYSTEM WARNING: Turn limit approaching. "
            f"You have 2 turn(s) remaining out of {max_turns} total. "
            "Plan your remaining steps carefully.]"
        )
        assert expected_halfway in user_msgs, "exact 50% warning text not preserved"
        assert expected_ninety in user_msgs, "exact 90% warning text not preserved"

    def test_warning_present_in_conversation_and_llm_messages(self):
        """REGRESSION: pin the observable guarantees of the USER-message pattern.

        After a run where the 50% warning fired, assert that the SAME warning message
        object is (a) present EXACTLY once in ``instance.conversation`` and (b) present
        as a distinct USER message in the LLM messages of the call it was injected into
        (``llm.calls[...]``). Object identity between the two proves the LLM actually
        saw the very message that lives in conversation — i.e. "the LLM sees it" is not
        an accidental content match but the same Message instance.

        NOTE on the local ``messages`` working set: it is NOT directly observable from
        this harness (``run()`` keeps it as a local var and it is consumed internally by
        loop detection / compression). We therefore pin only the two guarantees that ARE
        observable — conversation (exactly once) and llm_messages (present at the right
        turn). The known consequence that ``messages`` will NOT include these warnings for
        the rest of the run (because ``_setup_turn()`` runs once per run, not per
        iteration) is documented in core.py above the warning block; it is bounded (<=2
        messages) and does not affect the LLM context.
        """
        max_turns = 6
        llm, instance = _run(max_turns)

        # (a) Present exactly once in conversation.
        conv_warning = None
        for msg in instance.conversation:
            role = _msg_role(msg)
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if role == USER and isinstance(content, str) and "Halfway through your turn budget" in content:
                assert conv_warning is None, "50% warning appears more than once in conversation"
                conv_warning = msg
        assert conv_warning is not None, "50% warning missing from instance.conversation"

        # (b) Present as a distinct USER message in the LLM call where it fired.
        # turns_available==3 → 4th LLM call (index 3).
        halfway_call = llm.calls[3]
        llm_warning = _find_warning_in_call(halfway_call, "Halfway through your turn budget")
        assert llm_warning is not None, "50% warning missing from the LLM messages at its injection turn"
        assert _msg_role(llm_warning) == USER

        # Object identity: the message the LLM saw IS the one stored in conversation.
        assert llm_warning is conv_warning, (
            "the 50% warning message seen by the LLM is not the same object as the one "
            "stored in instance.conversation"
        )
