"""Unit tests for reasoning-only soft "continue" (pure-resend) retry.

Covers the approved design in ``plans/reasoning_only_continue_retry_plan.md``:
for a reasoning-only end turn we do up to N pure-resend soft continues before
falling back to the existing full-retry behavior, all bounded by the shared
``MAX_AUTO_CONTINUE_ATTEMPTS`` cap.

Key invariants under test (from the review notes):
- N3: ``_reasoning_only_soft_attempts`` never resets mid-episode → once it hits N the
  soft path is permanently closed for that episode.
- N1: after a full-retry rollback pops nudges, only ``_reasoning_only_pending_nudges``
  is zeroed; subsequent full retries pop exactly ``len(turn_output)`` (no over-pop).
- N2: the ``pop_count += _reasoning_only_pending_nudges`` adjustment is guarded to
  reasoning-only, so broken-json / empty-output / truncation are unaffected.

Run: pytest tests/test_reasoning_only_continue_retry.py -v
"""

import time
from typing import List, Optional

import pytest

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.engine.helpers import _is_incomplete_state
from agent_cascade.llm.schema import ASSISTANT, USER, Message
from agent_cascade.settings import (
    MAX_AUTO_CONTINUE_ATTEMPTS,
    REASONING_ONLY_CONTINUE_ATTEMPTS,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_instance() -> AgentInstance:
    """Construct a minimal real AgentInstance for engine-method tests."""
    now = time.monotonic()
    return AgentInstance(
        instance_name="TestAgent",
        agent_class="coder",
        conversation=[],
        created_at=now,
        last_activity=now,
        latest_marker_index=-1,
    )


def _reasoning_only_msg() -> Message:
    """A reasoning-only assistant message (has reasoning, no content, no tool call)."""
    return Message(role=ASSISTANT, content="", reasoning_content="let me think about this carefully")


def _empty_output_msg() -> Message:
    """An empty-output assistant message (nothing at all)."""
    return Message(role=ASSISTANT, content="")


def _broken_json_msg() -> dict:
    """A broken-json assistant message (tool call with mismatched braces).

    NOTE: built as a plain dict, not a ``Message``. ``_is_incomplete_state`` reads the
    tool-call arguments via ``func_call.get('arguments')`` only when ``func_call`` is a
    *dict* (helpers.py:338); a pydantic ``FunctionCall`` object falls through to ``''`` and
    would never be flagged. This pre-existing quirk is out of scope — we exercise the
    detection with the shape it actually handles.
    """
    return {
        "role": ASSISTANT,
        "content": "",
        "function_call": {"name": "some_tool", "arguments": '{"a": 1'},
    }


class _FakeLogger:
    def __init__(self):
        self.log_path = None

    def log_message(self, msg):
        pass


class _FakePool:
    """Minimal fake AgentPool exposing only what the engine methods under test touch."""

    def __init__(self, *, auto_continue=True, nudge_enabled=False):
        self.auto_continue = auto_continue
        self.nudge_enabled = nudge_enabled
        # tail_sync_check_enabled=False keeps _inject_soft_continue_nudge off the
        # real logger / tail-sync path (no shell windows, no filesystem).
        self.settings = type("Settings", (), {
            "auto_continue": auto_continue,
            "SOFT_CONTINUE_NUDGE_ENABLED": nudge_enabled,
            "tail_sync_check_enabled": False,
        })()
        self.stopped = False
        self._run_generation = 0
        self.telemetry = None
        self._rollback_calls: List[int] = []

    # ── pool API used by the engine methods under test ────────────────────
    def _rollback_instance(self, inst_name, pop_count):
        self._rollback_calls.append(pop_count)

    def _mark_activity(self, inst_name):
        pass

    def is_instance_terminated(self, inst_name):
        return False

    def get_logger(self, inst_name, agent_class):
        return _FakeLogger()


class _Engine:
    """Bind the real engine methods under test to a fake pool.

    Uses ``__new__`` (no full ExecutionEngine construction) and binds only the
    collaborators the code paths touch, so tests stay deterministic and isolated.

    ``nudge_enabled`` must be passed explicitly for nudge-ON tests: the production code reads
    ``SOFT_CONTINUE_NUDGE_ENABLED`` as a module-level constant in ``core.py`` (evaluated at import
    time), so the test must monkeypatch that symbol — see the TestNudgeOnPath fixture.
    """

    def __init__(self, pool: _FakePool):
        self.pool = pool
        self._my_generation = 0
        # Bind the real implementations (not stubs) so we test production logic.
        self._check_and_handle_truncation = ExecutionEngine._check_and_handle_truncation.__get__(self, type(self))
        self._is_terminal_stop = ExecutionEngine._is_terminal_stop.__get__(self, type(self))
        self._telemetry = lambda: None  # telemetry is optional (None → skipped) in the engine path
        self._inject_soft_continue_nudge = ExecutionEngine._inject_soft_continue_nudge.__get__(self, type(self))
        self._sync_conversation_log = ExecutionEngine._sync_conversation_log.__get__(self, type(self))
        self._reasoning_only_continue_text = ExecutionEngine._reasoning_only_continue_text
        self._make_user_message = ExecutionEngine._make_user_message  # staticmethod: returns Message(role=USER, content=text)
        self._rebuild_working_set = _Rebuilder()
        self._append_and_log = _Appender()


class _Rebuilder:
    """Stand-in for ``_rebuild_working_set``: clears the working sets in place.

    Mirrors the production contract that after a rollback the messages/llm_messages
    lists are rebuilt (cleared here) and any injected nudges are gone from them.
    """

    def __call__(self, messages, llm_messages, inst_name):
        messages.clear()
        llm_messages.clear()


class _Appender:
    """Stand-in for ``_append_and_log`` (lock_held=True path).

    Appends to ``instance.conversation`` so the nudge-ON cleanup assertions can
    verify nudges are removed from conversation after a rollback.
    """

    def __call__(self, instance, msg, *, lock_held=False):
        instance.conversation.append(msg)


def _run(engine: _Engine, instance: AgentInstance, turn_output: List[Message],
         is_truncated: bool = False) -> bool:
    messages: List[Message] = []
    llm_messages: List[Message] = []
    response: List[Message] = list(turn_output)  # _process_response does response.extend(turn_output)
    return engine._check_and_handle_truncation(
        is_truncated, turn_output, instance, instance.instance_name,
        messages, llm_messages, response,
    )


# ── Unit regression: _is_incomplete_state ───────────────────────────────────

class TestIsIncompleteStateRegression:
    """The detection helper must keep classifying the right shapes."""

    def test_reasoning_only(self):
        assert _is_incomplete_state([_reasoning_only_msg()]) == "reasoning-only"

    def test_empty_output(self):
        assert _is_incomplete_state([_empty_output_msg()]) == "empty-output"

    def test_broken_json(self):
        # Dict-shaped message (the shape _is_incomplete_state actually inspects — see helper note).
        assert _is_incomplete_state([_broken_json_msg()]) == "broken-json"

    def test_broken_json_message_object_not_flagged(self):
        """Regression guard for a pre-existing quirk: a pydantic FunctionCall object's arguments
        are read as '' by helpers.py:338, so it is NOT flagged broken-json. We must not change this
        behavior (out of scope); the test documents it so a future fix is deliberate."""
        from agent_cascade.llm.schema import FunctionCall
        msg = Message(role=ASSISTANT, content="", function_call=FunctionCall(name="t", arguments='{"a": 1'))
        assert _is_incomplete_state([msg]) is None

    def test_complete_content_is_none(self):
        msg = Message(role=ASSISTANT, content="here is the answer")
        assert _is_incomplete_state([msg]) is None


# ── Default pure-resend path (nudge OFF) ────────────────────────────────────

class TestPureResendDefault:
    """With SOFT_CONTINUE_NUDGE_ENABLED=False, soft continues append nothing and roll back nothing."""

    def _engine(self):
        return _Engine(_FakePool(nudge_enabled=False))

    def test_first_reasoning_only_is_pure_resend(self):
        engine = self._engine()
        instance = _make_instance()
        result = _run(engine, instance, [_reasoning_only_msg()])

        assert result is True  # re-call the LLM
        assert engine.pool._rollback_calls == []  # NO rollback
        assert instance.conversation == []  # NO message appended to conversation
        assert instance._reasoning_only_soft_attempts == 1
        assert instance._reasoning_only_pending_nudges == 0
        assert instance._auto_continue_triggered is True

    def test_second_reasoning_only_is_pure_resend(self):
        engine = self._engine()
        instance = _make_instance()
        _run(engine, instance, [_reasoning_only_msg()])
        result = _run(engine, instance, [_reasoning_only_msg()])

        assert result is True
        assert engine.pool._rollback_calls == []  # still no rollback on the 2nd soft continue
        assert instance.conversation == []  # still nothing appended
        assert instance._reasoning_only_soft_attempts == 2
        assert instance._reasoning_only_pending_nudges == 0

    def test_soft_to_full_transition_pops_only_turn_output(self):
        """Turn #3 (N=2) → full retry pops exactly len(turn_output); soft counter stays at N."""
        engine = self._engine()
        instance = _make_instance()
        _run(engine, instance, [_reasoning_only_msg()])  # soft 1
        _run(engine, instance, [_reasoning_only_msg()])  # soft 2
        result = _run(engine, instance, [_reasoning_only_msg()])  # full retry

        assert result is True
        assert engine.pool._rollback_calls == [1]  # pop_count == len(turn_output) ONLY
        assert instance._reasoning_only_soft_attempts == 2  # N3: stays at N
        assert instance._reasoning_only_pending_nudges == 0


# ── N3 — soft path stays closed after full retry ────────────────────────────

class TestSoftPathStaysClosedAfterFullRetry:
    def _engine(self):
        return _Engine(_FakePool(nudge_enabled=False))

    def test_full_retries_keep_firing_after_soft_budget_exhausted(self):
        engine = self._engine()
        instance = _make_instance()
        # One episode: N=2 soft continues, then full retries until the cap is hit.
        results = [
            _run(engine, instance, [_reasoning_only_msg()])
            for _ in range(MAX_AUTO_CONTINUE_ATTEMPTS)
        ]

        # The cap uses `>= MAX_AUTO_CONTINUE_ATTEMPTS`, so it fires on attempt #5:
        # attempts 1-2 soft (True), attempts 3-4 full retries (True), attempt 5 hits cap → False.
        assert results == [True, True, True, True, False]
        # Soft path was used exactly N times; the remaining were full retries before the cap.
        n_full = MAX_AUTO_CONTINUE_ATTEMPTS - REASONING_ONLY_CONTINUE_ATTEMPTS - 1
        assert len(engine.pool._rollback_calls) == n_full
        # Every full retry popped exactly len(turn_output) (N1: no over-pop, pending_nudges=0).
        assert engine.pool._rollback_calls == [1] * n_full
        # After the cap-hit reset, all counters are zero.
        assert instance._auto_continue_count == 0
        assert instance._reasoning_only_soft_attempts == 0
        assert instance._reasoning_only_pending_nudges == 0

    def test_total_attempts_never_exceeds_cap(self):
        """A single episode may not exceed the cap: attempts 1..(cap-1) True, attempt #cap → False.

        The cap-hit reset zeroes ALL counters, so a *subsequent* reasoning-only turn starts a fresh
        episode and continues again — that is correct (bounded per-episode), not an over-run.
        """
        engine = self._engine()
        instance = _make_instance()
        results = [
            _run(engine, instance, [_reasoning_only_msg()])
            for _ in range(MAX_AUTO_CONTINUE_ATTEMPTS)
        ]
        # Exactly (cap - 1) continue attempts succeeded; the cap-th attempt gave up.
        assert results.count(True) == MAX_AUTO_CONTINUE_ATTEMPTS - 1
        assert results[-1] is False
        # N soft continues + (cap-1-N) full retries = cap-1 total attempts before give-up.
        assert len(engine.pool._rollback_calls) == MAX_AUTO_CONTINUE_ATTEMPTS - REASONING_ONLY_CONTINUE_ATTEMPTS - 1
        # The next reasoning-only turn opens a fresh episode (counters were reset by the cap-hit).
        assert _run(engine, instance, [_reasoning_only_msg()]) is True


# ── N1 — no over-pop across consecutive full retries ────────────────────────

class TestNoOverPop:
    def test_consecutive_full_retries_pop_exactly_turn_output(self):
        engine = _Engine(_FakePool(nudge_enabled=False))
        instance = _make_instance()
        # Exhaust the soft budget first.
        for _ in range(REASONING_ONLY_CONTINUE_ATTEMPTS):
            _run(engine, instance, [_reasoning_only_msg()])
        # Two consecutive full retries: each must pop exactly len(turn_output)=1.
        _run(engine, instance, [_reasoning_only_msg()])
        _run(engine, instance, [_reasoning_only_msg()])
        assert engine.pool._rollback_calls == [1, 1]


# ── Normal completion resets counters ───────────────────────────────────────

class TestNormalCompletionResets:
    def test_clean_turn_resets_both_counters(self):
        engine = _Engine(_FakePool(nudge_enabled=False))
        instance = _make_instance()
        # Seed the counters as if a soft continue had just happened.
        instance._reasoning_only_soft_attempts = 2
        instance._reasoning_only_pending_nudges = 0
        instance._auto_continue_count = 1

        clean_msg = Message(role=ASSISTANT, content="done")
        result = _run(engine, instance, [clean_msg])

        assert result is False  # no continue injected for a complete turn
        assert engine.pool._rollback_calls == []
        assert instance._auto_continue_count == 0
        assert instance._reasoning_only_soft_attempts == 0
        assert instance._reasoning_only_pending_nudges == 0


# ── auto_continue=False regression guard ────────────────────────────────────

class TestAutoContinueDisabled:
    def test_reasoning_only_left_untouched(self):
        engine = _Engine(_FakePool(auto_continue=False))
        instance = _make_instance()
        result = _run(engine, instance, [_reasoning_only_msg()])

        assert result is False  # left completely untouched
        assert engine.pool._rollback_calls == []
        assert instance.conversation == []
        assert instance._auto_continue_count == 0
        assert instance._reasoning_only_soft_attempts == 0
        assert instance._reasoning_only_pending_nudges == 0


# ── Other malformed cases unchanged (N2) ────────────────────────────────────

class TestOtherMalformedCasesUnchanged:
    def test_empty_output_is_immediate_full_retry(self):
        engine = _Engine(_FakePool(nudge_enabled=False))
        instance = _make_instance()
        result = _run(engine, instance, [_empty_output_msg()])

        assert result is True
        assert engine.pool._rollback_calls == [1]  # immediate full retry, no soft continue
        assert instance._reasoning_only_soft_attempts == 0  # reasoning-only counters untouched (N2)
        assert instance._reasoning_only_pending_nudges == 0

    def test_broken_json_is_immediate_full_retry(self):
        engine = _Engine(_FakePool(nudge_enabled=False))
        instance = _make_instance()
        result = _run(engine, instance, [_broken_json_msg()])

        assert result is True
        assert engine.pool._rollback_calls == [1]
        assert instance._reasoning_only_soft_attempts == 0
        assert instance._reasoning_only_pending_nudges == 0

    def test_truncation_is_immediate_full_retry(self):
        engine = _Engine(_FakePool(nudge_enabled=False))
        instance = _make_instance()
        # A complete message that is also flagged truncated → truncation path.
        result = _run(engine, instance, [Message(role=ASSISTANT, content="partial")], is_truncated=True)

        assert result is True
        assert engine.pool._rollback_calls == [1]
        assert instance._reasoning_only_soft_attempts == 0
        assert instance._reasoning_only_pending_nudges == 0


# ── Nudge-ON path (deferred feature behind the flag) ────────────────────────

class TestNudgeOnPath:
    """With SOFT_CONTINUE_NUDGE_ENABLED=True, soft continues append an escalating USER nudge.

    The production code reads ``SOFT_CONTINUE_NUDGE_ENABLED`` as a module-level constant in
    ``core.py`` (evaluated at import time), so we monkeypatch that symbol for these tests.
    """

    @pytest.fixture(autouse=True)
    def _enable_nudge(self, monkeypatch):
        import agent_cascade.engine.core as core_mod
        # Enable the nudge flag AND raise N to 3 so the soft→full transition is reachable under
        # cap=5 (with the default N=2 the cap fires on attempt 3 before any full retry occurs).
        monkeypatch.setattr(core_mod, "SOFT_CONTINUE_NUDGE_ENABLED", True)
        monkeypatch.setattr(core_mod, "REASONING_ONLY_CONTINUE_ATTEMPTS", 3)
        yield

    def _engine(self):
        return _Engine(_FakePool(nudge_enabled=True))

    def test_first_nudge_appended_and_counters_advance(self):
        engine = self._engine()
        instance = _make_instance()
        result = _run(engine, instance, [_reasoning_only_msg()])

        assert result is True
        assert engine.pool._rollback_calls == []  # no rollback on a soft continue
        assert instance._reasoning_only_soft_attempts == 1
        assert instance._reasoning_only_pending_nudges == 1
        # A USER nudge was appended to conversation (via _append_and_log).
        assert len(instance.conversation) == 1
        assert instance.conversation[0].role == USER

    def test_second_nudge_escalates(self):
        engine = self._engine()
        instance = _make_instance()
        _run(engine, instance, [_reasoning_only_msg()])  # attempt 1
        result = _run(engine, instance, [_reasoning_only_msg()])  # attempt 2

        assert result is True
        assert instance._reasoning_only_soft_attempts == 2
        assert instance._reasoning_only_pending_nudges == 2
        assert len(instance.conversation) == 2
        # The two nudges must differ (escalating text).
        assert instance.conversation[0].content != instance.conversation[1].content

    def test_first_full_retry_pops_turn_output_plus_all_nudges(self):
        """F1/N1: the FIRST full retry pops turn_output + all N nudges, then zeros pending_nudges only.

        With N=3 (monkeypatched above): attempts 1-3 are soft continues (3 nudges in context);
        attempt 4 is the first full retry → pops len(turn_output) + 3 = 4 messages, and the
        pending-nudge counter is reset to 0 so later retries can't over-pop.
        """
        engine = self._engine()
        instance = _make_instance()
        for _ in range(3):
            _run(engine, instance, [_reasoning_only_msg()])  # 3 soft continues → 3 nudges
        result = _run(engine, instance, [_reasoning_only_msg()])  # first full retry

        assert result is True
        # pop_count == len(turn_output) + pending_nudges = 1 + 3 = 4 (turn_output + all N nudges)
        assert engine.pool._rollback_calls == [4]
        # The nudge counter is zeroed so subsequent retries don't re-pop them (N1).
        assert instance._reasoning_only_pending_nudges == 0
        # Soft budget stays closed at N — this full retry did NOT reopen the soft path (N3).
        assert instance._reasoning_only_soft_attempts == 3

    def test_no_over_pop_after_nudge_cleanup(self):
        """N1 with nudge ON: once the full retry pops all N nudges, pending_nudges is 0 → no over-pop.

        Distinct from the first-full-retry test above (which checks the pop *amount*): this one proves
        the cleanup actually prevents a double-count. With N=3 under cap=5 the episode runs 3 soft +
        exactly ONE full retry before the cap fires on attempt #5, so that single rollback must pop
        len(turn_output)+nudges = 4 — and afterwards pending_nudges is 0, so no further nudge can ever
        be re-popped (the over-pop guard). The two-consecutive-full-retry variant lives in TestNoOverPop.
        """
        engine = self._engine()
        instance = _make_instance()
        results = [
            _run(engine, instance, [_reasoning_only_msg()])
            for _ in range(MAX_AUTO_CONTINUE_ATTEMPTS)
        ]
        # 3 soft (True), the single full retry (True, pops 1+3=4), attempt 5 hits cap → False.
        assert results == [True, True, True, True, False]
        # The one full retry popped turn_output + all N nudges — and nothing more (no over-pop).
        assert engine.pool._rollback_calls == [4]
        # The nudge counter is zeroed after the rollback, so a later retry could never re-pop them.
        assert instance._reasoning_only_pending_nudges == 0

    def test_default_n_cap_hit_leaves_pending_nudge(self):
        """Documented edge: with default N=2 < cap, the cap fires on attempt #5 (a full retry), so a
        full retry DID occur and its rollback popped the reasoning message + both nudges. Counters
        are zeroed by the cap-hit reset. This matches the plan's documented behavior — do not 'fix'
        it into a behavior change."""
        import agent_cascade.engine.core as core_mod
        # Restore default N (the autouse fixture set it to 3 above).
        core_mod.REASONING_ONLY_CONTINUE_ATTEMPTS = REASONING_ONLY_CONTINUE_ATTEMPTS
        engine = self._engine()
        instance = _make_instance()
        results = [
            _run(engine, instance, [_reasoning_only_msg()])
            for _ in range(MAX_AUTO_CONTINUE_ATTEMPTS)
        ]

        # Attempts 1-2 soft (nudges appended), attempts 3-4 full retries, attempt 5 hits cap → False.
        assert results == [True, True, True, True, False]
        # Full retry #1 pops len(turn_output)+pending_nudges = 1+2 = 3, then zeros pending_nudges (N1).
        # Full retry #2 therefore pops exactly len(turn_output) = 1 — no over-pop. This is N1 with nudge ON.
        assert engine.pool._rollback_calls == [3, 1]
        # Cap-hit reset zeroes the counters.
        assert instance._auto_continue_count == 0
        assert instance._reasoning_only_soft_attempts == 0
        assert instance._reasoning_only_pending_nudges == 0


# ── Deterministic nudge text (F4) ───────────────────────────────────────────

class TestReasoningOnlyContinueText:
    def test_attempt_one_text(self):
        text = ExecutionEngine._reasoning_only_continue_text(1)
        assert "continue" in text.lower()
        assert "STOP thinking" not in text

    def test_attempt_two_escalates(self):
        text = ExecutionEngine._reasoning_only_continue_text(2)
        assert "STOP thinking" in text

    def test_deterministic(self):
        assert ExecutionEngine._reasoning_only_continue_text(1) == ExecutionEngine._reasoning_only_continue_text(1)
        assert ExecutionEngine._reasoning_only_continue_text(3) == ExecutionEngine._reasoning_only_continue_text(2)
