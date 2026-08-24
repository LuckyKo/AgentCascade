"""Tests for the SLEEPING cap (Phase 3, Fix A1).

Exercises the real production code path in:
- agent_cascade/engine/core.py (_handle_sleeping_state)

An agent waiting in SLEEPING state for background tools (async agent calls /
async shells) can be held there indefinitely if a tool hangs. Fix A1 caps that
wait at AGENT_SLEEPING_MAX_WAIT_SECONDS. On expiry the agent is forced to
COMPLETING — mirroring the existing COMPLETING-transition pattern exactly
(state lock → TERMINATED check → _transition(COMPLETING) → sleeping_since=None)
— an error message is enqueued, and SleepAction.BREAK_LOOP is returned so run()
exits its loop cleanly.

All tests are self-contained — no LLM or API server required. The pool is a
MagicMock (drain_queue returns [] and has_pending returns True to reach the
"still waiting for background tools" branch) and the instance uses a REAL
threading.Lock so the state-lock path in _handle_sleeping_state runs unmodified.

Run: pytest tests/test_sleeping_timeout.py -v
"""

import threading
from unittest.mock import MagicMock, patch

from agent_cascade.agent_instance import AgentState
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.engine.helpers import SleepAction


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: mock pool + real ExecutionEngine (mirrors test_llm_call_deadline.py)
# ──────────────────────────────────────────────────────────────────────────────

def _make_pool_and_engine():
    """Create a mocked pool and real ExecutionEngine for testing the sleep loop.

    The instance carries a REAL threading.Lock as _state_lock so that the
    ``with instance._state_lock:`` block in _handle_sleeping_state executes the
    actual lock protocol (not a MagicMock), while state/_transition remain mocks
    we can assert on.
    """
    pool = MagicMock()
    instance = MagicMock()

    # Real lock for the state-transition path; everything else stays mocked.
    real_lock = threading.Lock()
    instance._state_lock = real_lock
    instance.state = AgentState.SLEEPING
    instance.instance_name = "test-agent"

    # The waiting branch (cap NOT fired) proceeds to periodic logging, which does
    # float arithmetic on these throttling timestamps — set them to floats so the
    # comparison works instead of raising TypeError against a MagicMock.
    import time as _time
    instance._last_wakeup_log = _time.monotonic() - 100.0
    instance._last_waiting_debug_log = _time.monotonic() - 100.0

    # _is_terminal_stop() reads these — configure so it returns False (no stop).
    pool.stopped = False
    pool.is_instance_terminated.return_value = False
    pool._run_generation = 1

    # Reach the "still waiting for background tools" branch:
    #   drain_queue -> [] (no wakeup messages) AND has_pending -> True.
    pool.drain_queue.return_value = []
    pool.has_pending.return_value = True

    # settings must be a real object with proper attributes, not mocks.
    class Settings:
        sleeping_wakeup_interval = 5.0

    pool.settings = Settings()

    engine = ExecutionEngine(pool)
    # _my_generation is normally captured in run(); this test calls
    # _handle_sleeping_state directly, bypassing run(), so set it here.
    engine._my_generation = 1
    return engine, pool, instance


def _call_sleeping(engine, instance, cap_seconds, sleeping_duration):
    """Drive _handle_sleeping_state with a patched AGENT_SLEEPING_MAX_WAIT_SECONDS.

    ``sleeping_duration`` is the value of (time.monotonic() - instance.sleeping_since)
    at call time — i.e. how long the agent has been sleeping. We set sleeping_since
    relative to the real clock so no fake-clock plumbing is needed. Pass a negative
    sleeping_duration for the disabled case (sleeping_since irrelevant there).
    """
    import time
    if sleeping_duration < 0:
        instance.sleeping_since = None
    else:
        instance.sleeping_since = time.monotonic() - sleeping_duration

    with patch(
        "agent_cascade.engine.core.AGENT_SLEEPING_MAX_WAIT_SECONDS", cap_seconds
    ):
        return engine._handle_sleeping_state(instance, [], [], [])


# ──────────────────────────────────────────────────────────────────────────────
# 1. Cap fires after the max wait → force COMPLETING + error message + BREAK_LOOP
# ──────────────────────────────────────────────────────────────────────────────

class TestSleepingCapFires:
    """When sleeping_duration >= cap, the agent is forced to COMPLETING."""

    def test_cap_fires_transitions_to_completing(self):
        engine, pool, instance = _make_pool_and_engine()

        action, yield_value = _call_sleeping(engine, instance, cap_seconds=3600, sleeping_duration=3601)

        assert action == SleepAction.BREAK_LOOP
        assert yield_value is None
        instance._transition.assert_called_once_with(AgentState.COMPLETING)

    def test_cap_fires_clears_sleeping_since(self):
        engine, pool, instance = _make_pool_and_engine()

        _call_sleeping(engine, instance, cap_seconds=3600, sleeping_duration=4000)

        assert instance.sleeping_since is None

    def test_cap_fires_enqueues_error_message(self):
        engine, pool, instance = _make_pool_and_engine()

        _call_sleeping(engine, instance, cap_seconds=3600, sleeping_duration=3601)

        pool.enqueue_message.assert_called_once()
        args = pool.enqueue_message.call_args[0]
        assert args[0] == "test-agent"
        msg = args[1]
        assert isinstance(msg, str)
        assert "[SYSTEM]" in msg
        assert "SLEEPING timeout" in msg
        # The message explains that pending background tools didn't complete.
        assert "background tools" in msg

    def test_cap_fires_at_exact_boundary(self):
        """sleeping_duration >= cap uses >=, so the boundary value fires."""
        engine, pool, instance = _make_pool_and_engine()

        action, _ = _call_sleeping(engine, instance, cap_seconds=10, sleeping_duration=10)

        assert action == SleepAction.BREAK_LOOP
        instance._transition.assert_called_once_with(AgentState.COMPLETING)


# ──────────────────────────────────────────────────────────────────────────────
# 2. Cap does NOT fire within the limit → keep waiting (CONTINUE_LOOP)
# ──────────────────────────────────────────────────────────────────────────────

class TestSleepingCapNotFired:
    """A sleeping agent still within the cap keeps waiting for background tools."""

    def test_no_fire_within_limit(self):
        engine, pool, instance = _make_pool_and_engine()

        # Sleeping 1800s against a 3600s cap — comfortably within the limit.
        action, yield_value = _call_sleeping(engine, instance, cap_seconds=3600, sleeping_duration=1800)

        assert action == SleepAction.CONTINUE_LOOP
        # Waiting state is signalled by yielding an empty list (no turn consumed).
        assert yield_value == []
        # No forced transition and no timeout message.
        instance._transition.assert_not_called()
        pool.enqueue_message.assert_not_called()
        # sleeping_since must be preserved so the wait keeps accumulating.
        assert instance.sleeping_since is not None


# ──────────────────────────────────────────────────────────────────────────────
# 3. Cap disabled (0) → legacy unbounded-wait behavior
# ──────────────────────────────────────────────────────────────────────────────

class TestSleepingCapDisabled:
    """Setting AGENT_SLEEPING_MAX_WAIT_SECONDS to 0 disables the cap entirely."""

    def test_zero_disables_cap(self):
        engine, pool, instance = _make_pool_and_engine()

        # Cap disabled (0): even a very long sleep must not force completion.
        action, yield_value = _call_sleeping(engine, instance, cap_seconds=0, sleeping_duration=-1)

        assert action == SleepAction.CONTINUE_LOOP
        assert yield_value == []
        instance._transition.assert_not_called()
        pool.enqueue_message.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# 4. TERMINATED short-circuit under the lock (mirrors COMPLETING pattern)
# ──────────────────────────────────────────────────────────────────────────────

class TestSleepingCapTerminatedShortCircuit:
    """If the instance is already TERMINATED, break out without transitioning."""

    def test_terminated_breaks_without_transition(self):
        engine, pool, instance = _make_pool_and_engine()
        instance.state = AgentState.TERMINATED

        action, yield_value = _call_sleeping(engine, instance, cap_seconds=3600, sleeping_duration=4000)

        assert action == SleepAction.BREAK_LOOP
        assert yield_value is None
        # The COMPLETING transition must NOT be attempted on a terminated agent.
        instance._transition.assert_not_called()
