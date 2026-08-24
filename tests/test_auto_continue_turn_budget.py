"""Regression tests: auto-continue must consume a real turn (no counter reset).

Background: when the LLM output is truncated or incomplete, the engine's
``_check_and_handle_truncation()`` rolls back and re-calls the LLM ("auto-continue").
Historically the main loop in ``ExecutionEngine.run()`` FULLY RESET ``turns_available``
to ``max_turns`` on every auto-continue (gated by ``instance._auto_continue_triggered``),
so an agent could make up to ~1,250 effective LLM calls per task instead of its
configured ``max_turns``.

The reset was HARD-REMOVED: the decrement at Phase 2 (``turns_available -= 1``) runs
BEFORE the LLM call on every iteration, so each auto-continue now consumes exactly one
real turn and ``max_turns`` is a true hard budget. These tests drive the REAL
``ExecutionEngine.run()`` generator with a scripted fake LLM to prove:

1. An agent whose output is ALWAYS truncated stops after EXACTLY ``max_turns`` total
   LLM calls (auto-continues included) — not an unbounded/extended number.
2. The final-turn warning is injected before the last call (budget accounting intact).
3. A clean completion still ends the run normally (no off-by-one from the removal).

Run: pytest tests/test_auto_continue_turn_budget.py -v
"""

import time
from typing import List

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import ASSISTANT, USER, Message


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_instance(max_turns: int) -> AgentInstance:
    """Minimal real AgentInstance with a pre-seeded conversation (so _setup_turn works)."""
    now = time.monotonic()
    inst = AgentInstance(
        instance_name="BudgetAgent",
        agent_class="coder",
        conversation=[Message(role=USER, content="do the task")],
        created_at=now,
        last_activity=now,
        latest_marker_index=-1,
    )
    inst.max_turns = max_turns
    return inst


def _truncated_msg() -> Message:
    """Assistant message flagged truncated (finish_reason == 'length')."""
    return Message(role=ASSISTANT, content="partial answer ...", extra={"finish_reason": "length"})


def _clean_msg() -> Message:
    """A complete assistant message (no truncation, real content)."""
    return Message(role=ASSISTANT, content="here is the final answer")


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


class _FakePool:
    """Minimal fake AgentPool exposing only what ExecutionEngine.run() touches."""

    def __init__(self):
        self.auto_continue = True
        self.settings = type("Settings", (), {
            "auto_continue": True,
            "tail_sync_check_enabled": False,          # keep tail-sync off the filesystem path
            "compression_force_threshold": 96.0,      # force-compress threshold (token %)
            "compression_warning_threshold": 90.0,    # warn threshold (token %)
            "compression_context_reserve_tokens": 2048,
            "auto_rollback_on_loop": True,            # loop-detection inline rollback toggle
            "max_auto_rollbacks": 5,                  # max loop-recovery retries
        })()
        self.stopped = False
        self._run_generation = 0
        self._config_version = 0
        self._compression_halted = set()
        self._halted_instances = set()
        self.telemetry = None
        self.api_router = None
        self.instances: dict = {}                     # name → AgentInstance (for _rollback_instance)

    def get_template(self, agent_class):
        return None  # no system-message injection, no final-turn tool disabling

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

    def slice_history_for_llm(self, conv):
        return list(conv)

    def _rollback_instance(
        self, instance_name: str, *, pop_count: int = 0, target_length: int = -1,
        preserve_system_user: bool = True, refine_function_boundary: bool = True,
        sync_logger: bool = True, tail_sync_check: bool = True, reason=None,
    ) -> int:
        """Minimal rollback: pop N messages from the instance conversation.

        Mirrors the real pool helper's core behavior (del conv[new_len:]) without
        logger sync or boundary refinement — sufficient for truncation auto-continue.
        """
        inst = self.instances.get(instance_name)
        if not inst or pop_count <= 0:
            return 0
        with inst._compression_lock:
            conv = inst.conversation
            current_len = len(conv)
            new_len = max(0, current_len - pop_count)
            del conv[new_len:]
            # Clear working-set caches so _setup_turn rebuilds from the trimmed conversation
            inst._cached_messages.clear()
            inst._cached_llm_messages.clear()
        return current_len - len(conv)


class _ScriptedLLM:
    """Stands in for ``_call_llm_with_injection``: yields one scripted message per call.

    The real method returns a generator; the run() loop consumes it with
    ``for msg in gen:`` and closes it in ``finally`` — so a plain generator works.
    Each call records the llm_messages it received (used to assert final-turn notice).
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
    """Real run() + real truncation handling; only the LLM call is faked."""
    pool.instances[instance.instance_name] = instance  # for _rollback_instance
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.pool = pool
    engine._my_generation = 0
    engine.compression_handler = _FakeCompressionHandler()
    engine._call_llm_with_injection = llm
    return engine


def _llm_call_count(llm: _ScriptedLLM) -> int:
    """Number of LLM calls actually made (== number of turn-loop iterations)."""
    return len(llm.calls)


def _last_turn_had_final_notice(llm: _ScriptedLLM) -> bool:
    """The final-turn warning is appended to llm_messages before the last LLM call."""
    if not llm.calls:
        return False
    return any(
        isinstance(m, Message) and "Final turn" in (m.content or "")
        for m in llm.calls[-1]
    )


# ── Tests ───────────────────────────────────────────────────────────────────

class TestAutoContinueConsumesTurns:
    """Every auto-continue costs exactly one real turn — max_turns is a hard budget."""

    def test_always_truncated_stops_after_exactly_max_turns(self):
        """max_turns=3, every response truncated → run ends after EXACTLY 3 LLM calls.

        Under the OLD reset behavior the loop would keep auto-continuing far beyond
        max_turns (up to ~cap*max_turns effective calls). The hard removal makes the
        budget exact: call #1 truncates (continue), call #2 truncates (continue),
        call #3 is the final turn (notice injected) and the loop exits.
        """
        max_turns = 3
        pool = _FakePool()
        llm = _ScriptedLLM([_truncated_msg()] * max_turns)
        instance = _make_instance(max_turns)
        engine = _build_engine(pool, instance, llm)

        list(engine.run(instance))  # exhaust the generator

        assert _llm_call_count(llm) == max_turns, (
            f"Expected exactly {max_turns} LLM calls (auto-continues consume turns), "
            f"got {_llm_call_count(llm)} — turn counter may still be reset on auto-continue"
        )
        # The final-turn warning must have been injected before the last call,
        # proving the loop reached turns_available == 1 (not reset back to max).
        assert _last_turn_had_final_notice(llm), (
            "Final-turn notice missing from the last LLM call — turn budget accounting is off"
        )

    def test_truncated_twice_then_clean_stops_at_max_turns(self):
        """max_turns=2, script [truncated, truncated, clean] → EXACTLY 2 LLM calls.

        This shape distinguishes OLD (reset) vs NEW (no reset) behavior:

        OLD behavior (turn counter reset on auto-continue):
          Call #1: truncated → auto-continue → turns_available RESET to 2
          Call #2: truncated → auto-continue → turns_available RESET to 2
          Call #3: clean → completes
          Total: 3 LLM calls

        NEW behavior (no reset — each auto-continue consumes a real turn):
          Call #1: truncated → auto-continue → turns_available decremented to 1
          Call #2: truncated → auto-continue → turns_available decremented to 0
          Loop exits (turns_available <= 0) → turn-limit notice injected
          Total: EXACTLY 2 LLM calls (the 3rd scripted message is never consumed)

        The assertion on exact call count is the regression guard: if someone re-introduces
        the reset, this test will see 3 calls instead of 2 and fail.
        """
        max_turns = 2
        pool = _FakePool()
        llm = _ScriptedLLM([_truncated_msg(), _truncated_msg(), _clean_msg()])
        instance = _make_instance(max_turns)
        engine = _build_engine(pool, instance, llm)

        list(engine.run(instance))

        # Exactly 2 calls — the 3rd scripted message (clean) is never reached.
        assert _llm_call_count(llm) == max_turns, (
            f"Expected exactly {max_turns} LLM calls under hard budget, "
            f"got {_llm_call_count(llm)} — turn counter may still be reset on auto-continue"
        )
        # Turn-limit path: the last assistant message in conversation gets the notice appended.
        conv = instance.conversation
        last_assistant_content = None
        for msg in reversed(conv):
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == ASSISTANT:
                content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
                last_assistant_content = content
                break
        assert last_assistant_content is not None and "Turn limit reached" in last_assistant_content, (
            "Turn-limit notice not found on last assistant message — budget exhaustion path not exercised"
        )

    def test_clean_only_completes_in_one_call(self):
        """Sanity: a single clean response ends the run after exactly 1 LLM call."""
        pool = _FakePool()
        llm = _ScriptedLLM([_clean_msg()])
        instance = _make_instance(5)
        engine = _build_engine(pool, instance, llm)

        list(engine.run(instance))

        assert _llm_call_count(llm) == 1
