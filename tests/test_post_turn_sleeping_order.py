"""Tests for the _post_turn_checks check ordering (Fix A: reorder).

Regression guard for the idle-wakeup bug: in `_post_turn_checks` the pending-async-tool
→ SLEEPING transition must run BEFORE the pure-thinking stall detector. Otherwise a parent
that dispatched an async child and then emitted a reasoning-only post-turn would break to
IDLE (via the pure-thinking path) before it could sleep, and the child's later result would
be lost by the run() finally block (clear_pending + drain_queue).

Target order under test:
    1. unexecuted tool call in output        → return True   (continue)
    2. pending async tool                    → SLEEPING      → return True (MOVED UP — was #3)
    3. pure thinking turn (stalled, no work) → return False  (was #2)
    4. drain post-generation messages        → return True   (unchanged)

The tests exercise the REAL `_post_turn_checks` / `_transition_to_sleeping_if_pending` /
`_detect_pure_thinking_turn` code paths and only mock the pool + a couple of guard methods,
so they fail if the ordering regresses. Deterministic — no sleeps.
"""

import time

from agent_cascade.llm.schema import Message, ASSISTANT, USER, FUNCTION
from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.execution_engine import ExecutionEngine


def _make_pool(has_pending):
    """A MagicMock pool whose async-registry reports the given pending state.

    Everything else on the pool is a MagicMock so incidental attribute access (e.g.
    `pool.settings`, `pool.telemetry`) never raises during engine construction.
    """
    from unittest.mock import MagicMock
    pool = MagicMock()
    pool.has_pending.return_value = has_pending
    # _is_terminal_stop() reads these; configure them so it returns False (no terminal stop)
    # and we reach the post-turn checks under test.
    pool.stopped = False
    pool._run_generation = 1
    pool.is_instance_terminated.return_value = False
    # No queued/drainable messages: a bare MagicMock makes has_messages() truthy, which would
    # short-circuit _drain_post_generation_messages to True and mask the pure-thinking break.
    pool.has_messages.return_value = False
    pool.drain_queue.return_value = []
    return pool


def _make_engine(has_pending):
    """Real ExecutionEngine backed by a mock pool (mirrors test_embedded_tool_call_detection)."""
    engine = ExecutionEngine(_make_pool(has_pending))
    # _my_generation is normally captured in run(); we call _post_turn_checks directly, so set
    # it here to match pool._run_generation and avoid an AttributeError in _is_terminal_stop().
    engine._my_generation = 1
    return engine


def _make_instance(name="parent"):
    """A RUNNING AgentInstance — the state a post-turn check runs in."""
    inst = AgentInstance(
        instance_name=name,
        agent_class="Orchestrator",
        conversation=[],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=0,
    )
    inst.state = AgentState.RUNNING
    return inst


def _reasoning_only_response():
    """A response whose only assistant output is reasoning/thinking — no real text.

    This is exactly the shape `_detect_pure_thinking_turn` classifies as a stalled turn
    (has_thinking True, has_real_content False), so it would trigger the pure-thinking break
    if that check ran before the pending-tool check.
    """
    return [
        Message(role=USER, content="Dispatch the research child and wait for it."),
        Message(
            role=ASSISTANT,
            content="",  # no real text — reasoning only
            reasoning_content="Let me think about what to do next…",
        ),
    ]


def _tool_call_response():
    """A response with an unexecuted (standard) tool call on the last assistant message."""
    return [
        Message(role=USER, content="Read the file."),
        Message(
            role=ASSISTANT,
            content="",
            function_call={"name": "read_file", "arguments": '{"path": "x"}'},
        ),
    ]


class TestPostTurnSleepingOrder:
    """Fix A invariant: pending async work wins over the pure-thinking stall break."""

    def test_pending_tool_with_reasoning_only_turn_sleeps_not_breaks(self):
        """Pending tool + reasoning-only turn → SLEEPING (returns True), NOT a break.

        This is THE regression guard for the reorder: with has_pending True the pending→SLEEPING
        check must fire and return True before `_detect_pure_thinking_turn` can break the loop.
        """
        engine = _make_engine(has_pending=True)
        inst = _make_instance()

        result = engine._post_turn_checks(inst, [], [], _reasoning_only_response())

        # The pending check won: continue (True), not a stall-break (False).
        assert result is True, (
            "Pending async tool must take priority over the pure-thinking break — "
            "_post_turn_checks returned False (broke to IDLE) instead of continuing to SLEEPING"
        )
        # The pending check actually consulted the pool and transitioned the instance.
        engine.pool.has_pending.assert_called_once_with("parent")
        assert inst.state == AgentState.SLEEPING, (
            f"instance should be SLEEPING after a pending-tool post-turn, got {inst.state}"
        )

    def test_reasoning_only_turn_no_pending_still_breaks(self):
        """Reasoning-only turn + NO pending tool → still breaks (returns False).

        Preserves the stall-detection behavior: a genuinely stuck agent with no outstanding
        work must still be interrupted by the pure-thinking path.
        """
        engine = _make_engine(has_pending=False)
        inst = _make_instance()

        result = engine._post_turn_checks(inst, [], [], _reasoning_only_response())

        assert result is False, (
            "A reasoning-only turn with no pending async work must still break out of the loop"
        )
        # No sleep transition was taken.
        assert inst.state == AgentState.RUNNING, (
            f"instance should stay RUNNING (no pending work), got {inst.state}"
        )

    def test_tool_call_present_continues_regardless(self):
        """Unexecuted tool call → continues via check #1 (regression guard for reordering).

        Confirms the reorder did not disturb the first check: a real tool call in the output
        returns True before either the pending or pure-thinking checks are consulted.
        """
        engine = _make_engine(has_pending=False)  # pending is irrelevant here
        inst = _make_instance()

        result = engine._post_turn_checks(inst, [], [], _tool_call_response())

        assert result is True, "An unexecuted tool call must continue the loop (check #1)"
        # Check #1 short-circuits: the pending check should never have been consulted.
        engine.pool.has_pending.assert_not_called()
        assert inst.state == AgentState.RUNNING
