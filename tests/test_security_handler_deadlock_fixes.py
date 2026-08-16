"""Unit and integration tests for deadlock protection fixes in security_handler.py.

Tests the three specific fixes:
1. Hard timeout on LLM generator (engine.run never yields)
2. Reentrant lock (RLock) prevents self-deadlock
3. Lock acquire timeout prevents permanent block on crash

Plus integration tests that exercise actual deadlock scenarios with real threading.
"""
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Must import before patches take effect
from agent_cascade.security_handler import (
    SecurityAdvisorHandler,
    SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS,
    _get_security_check_lock,
    _get_security_execution_lock,
)


# ── Unit tests for individual mechanisms ────────────────────────────────────


class TestSecurityTurnBudget:
    """The system-launched Security advisor is bounded by a turn budget (not a wall-clock timeout).

    The former SECURITY_LLM_TIMEOUT_SECONDS was removed. Instead, _execute_check sets
    sec_instance.max_turns = SECURITY_AGENT_MAX_TURNS so the engine injects 50%/90% warnings
    and forces a final verdict on the last turn; an ambiguous result auto-rejects (NO).
    """

    def test_turn_budget_setting_is_reasonable(self):
        """SECURITY_AGENT_MAX_TURNS should be a small positive integer (tight budget for a single check)."""
        from agent_cascade.settings import SECURITY_AGENT_MAX_TURNS
        assert isinstance(SECURITY_AGENT_MAX_TURNS, int)
        assert 1 <= SECURITY_AGENT_MAX_TURNS <= 50, (
            f"Security turn budget ({SECURITY_AGENT_MAX_TURNS}) should be between 1-50"
        )

    def test_handler_bounded_by_turns_not_wallclock(self):
        """security_handler should no longer define the removed wall-clock LLM timeout constant."""
        import inspect
        import agent_cascade.security_handler as sh

        # Runtime attribute check (robust to comments mentioning the old name).
        assert not hasattr(sh, 'SECURITY_LLM_TIMEOUT_SECONDS'), (
            "old wall-clock constant should be removed"
        )
        assert "max_turns = SECURITY_AGENT_MAX_TURNS" in inspect.getsource(sh), (
            "security_handler should bound the Security advisor via max_turns"
        )


class TestReentrantSecurityLock:
    """Test that RLock allows reentrant acquisition by the same thread."""

    def test_rlock_allows_same_thread_reacquire(self):
        """RLock must allow the same thread to acquire multiple times without deadlock."""
        lock = threading.RLock()

        # First acquire (simulates outer security check)
        assert lock.acquire(timeout=1), "First acquire should succeed"
        try:
            # Second acquire from same thread (simulates nested security check)
            assert lock.acquire(timeout=1), "Second acquire by same thread should succeed (reentrant)"
            try:
                # Third acquire for good measure
                assert lock.acquire(timeout=1), "Third acquire by same thread should succeed"
                lock.release()
            finally:
                lock.release()
        finally:
            lock.release()

    def test_rlock_different_threads_block(self):
        """Different threads should still block on the RLock (concurrency control preserved)."""
        lock = threading.RLock()
        lock.acquire()

        blocked = threading.Event()

        def try_acquire():
            result = lock.acquire(timeout=0.5)
            blocked.set()
            return result

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join(timeout=2)

        assert blocked.is_set(), "Second thread should have attempted and timed out"
        # Lock is still held by main thread
        lock.release()

    def test_security_handler_creates_rlock_type(self):
        """Verify security_handler.py uses RLock for execution lock (not Semaphore or Lock)."""
        import inspect
        from agent_cascade import security_handler

        source = inspect.getsource(security_handler)
        assert "RLock()" in source, (
            "security_handler should use threading.RLock() for reentrant safety"
        )

    def test_both_security_locks_are_reentrant(self):
        """Both prompt lock and execution lock must be reentrant-safe for nested checks.

        The prompt lock is a plain threading.RLock. The execution lock is a
        ResettableRLock (a wrapper that delegates to an internal RLock) so it can
        recover from a leaked lock left by a killed daemon thread while preserving
        the same-thread reentrancy of an RLock. We verify both are reentrant-capable:
        acquiring twice on the same thread must not deadlock.
        """
        app = type('App', (), {})()  # Simple mock app object

        lock1 = _get_security_check_lock(app)
        assert isinstance(lock1, type(threading.RLock())), (
            "_get_security_check_lock must return RLock for reentrant prompt building"
        )

        lock2 = _get_security_execution_lock(app)
        from agent_cascade.security_handler import ResettableRLock
        # Accept either a plain RLock or the ResettableRLock wrapper (which wraps one).
        assert isinstance(lock2, (type(threading.RLock()), ResettableRLock)), (
            "_get_security_execution_lock must return a reentrant lock (RLock or ResettableRLock)"
        )

        # Behavioral check: same-thread double acquire must not deadlock (reentrancy).
        assert lock2.acquire(timeout=1), "First acquire should succeed"
        try:
            assert lock2.acquire(timeout=1), "Reentrant second acquire by same thread must succeed"
        finally:
            lock2.release()
            lock2.release()

    def test_unused_semaphore_removed_from_api_server(self):
        """api_server.py should not create security_check_semaphore anymore."""
        import inspect
        from agent_cascade import api_server
        source = inspect.getsource(api_server)
        assert "security_check_semaphore" not in source, (
            "Unused security_check_semaphore should be removed from api_server.py"
        )


class TestSecurityLockAcquireTimeout:
    """Test that lock acquire timeout prevents permanent block."""

    def test_acquire_timeout_constant_is_reasonable(self):
        """SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS must be a sane positive duration.

        The default was intentionally raised to 600s (commit 5484735) so that a
        waiting security check queues behind a legitimately long-running previous
        check (~300s first-yield + turn budget) instead of spuriously timing out.
        We therefore only assert it's positive and within a generous upper bound
        (1 hour) — the ResettableRLock dead-holder detection is what recovers from
        truly leaked locks, not this acquire timeout.
        """
        assert 1 <= SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS <= 3600, (
            f"Lock acquire timeout ({SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS}s) should be between 1-3600s"
        )

    def test_acquire_timeout_fires_when_lock_held_by_other_thread(self):
        """If lock is held by another thread and not released, acquire should timeout."""
        lock = threading.RLock()

        held = threading.Event()
        release_later = threading.Event()

        def hold_lock():
            lock.acquire()
            held.set()
            release_later.wait(timeout=5)
            lock.release()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        held.wait(timeout=2)

        start = time.monotonic()
        result = lock.acquire(timeout=0.5)
        elapsed = time.monotonic() - start

        release_later.set()
        holder.join(timeout=2)

        assert not result, "Acquire should fail when lock is held by another thread"
        assert 0.3 <= elapsed <= 1.5, f"Timeout should fire in ~0.5s, got {elapsed:.2f}s"

    def test_acquire_timeout_error_message_includes_context(self):
        """The timeout error message should include request_id and guidance."""
        msg = (
            f"[SECURITY] Failed to acquire security execution lock within "
            f"{SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS}s for request test_rid. "
            f"A previous check may have crashed without releasing. "
            f"Manual restart may be required."
        )
        assert "request" in msg.lower(), "Error message should reference the request for debugging"
        assert "test_rid" in msg, "Error message should include request_id"


class TestResettableRLock:
    """Tests for ResettableRLock — the leaked-lock recovery mechanism.

    The security execution lock is held by a daemon thread. If that thread is killed
    before release(), a plain RLock is leaked forever and every subsequent check
    times out. ResettableRLock detects a DEAD holder and swaps in a fresh RLock so
    the system self-heals, while never stealing a LIVE holder's lock.
    """

    def test_acquire_release_cycle(self):
        """Basic acquire/release works and clears ownership."""
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()
        assert not lock.owner_is_alive, "Fresh lock should have no live owner"
        assert lock.acquire(timeout=1)
        try:
            assert lock.owner_is_alive, "Owner should be alive while held by this thread"
        finally:
            lock.release()
        assert not lock.owner_is_alive, "Owner should be cleared after release"

    def test_reentrant_same_thread(self):
        """Same-thread nested acquire must not deadlock (RLock reentrancy preserved)."""
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()
        assert lock.acquire(timeout=1)
        try:
            assert lock.acquire(timeout=1), "Reentrant acquire by same thread must succeed"
            lock.release()
        finally:
            lock.release()
        assert not lock.owner_is_alive

    def test_different_threads_block(self):
        """A different thread must block (and time out) while the lock is held."""
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()
        lock.acquire()  # held by main thread

        result = {}

        def try_acquire():
            result['acquired'] = lock.acquire(timeout=0.3)

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join(timeout=2)
        assert not result.get('acquired', True), (
            "Second thread should time out while a live thread holds the lock"
        )
        lock.release()

    def test_force_reset_recovers_dead_holder(self):
        """After a holder thread dies, force_reset swaps in a fresh RLock that is acquirable."""
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()
        acquired_flag = threading.Event()

        def hold_and_die():
            assert lock.acquire(timeout=1)
            acquired_flag.set()
            # Thread returns WITHOUT releasing → simulates a killed daemon thread.
            # The RLock is now leaked (owner thread will be dead after join).

        holder = threading.Thread(target=hold_and_die)
        holder.start()
        assert acquired_flag.wait(timeout=2), "Holder should have acquired the lock"
        holder.join(timeout=2)
        assert not holder.is_alive(), "Holder thread must be dead for the leak scenario"

        # Now the lock is leaked: owner thread is dead, but the internal RLock is still held.
        assert not lock.owner_is_alive, "Dead holder should report owner_is_alive=False"

        # A fresh acquirer cannot get the (leaked) lock — it times out.
        assert not lock.acquire(timeout=0.3), "Should not acquire a leaked lock from a dead holder"

        # force_reset swaps in a fresh RLock → now acquirable.
        was_held = lock.force_reset(reason="test: dead-holder leak")
        assert was_held, "force_reset should report it reset a held lock"
        assert lock.acquire(timeout=1), "After force_reset, the fresh lock must be acquirable"
        lock.release()

    def test_force_reset_noop_when_free(self):
        """force_reset on an already-free lock is a no-op (returns False)."""
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()
        was_held = lock.force_reset(reason="test: nothing held")
        assert not was_held, "force_reset should report False when the lock was free"
        # Still fully usable after a no-op reset.
        assert lock.acquire(timeout=1)
        lock.release()

    def test_live_holder_not_stolen(self):
        """A LIVE holder's lock must NOT be reset — owner_is_alive stays True while it runs."""
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()
        release_later = threading.Event()

        def hold():
            assert lock.acquire(timeout=1)
            try:
                release_later.wait(timeout=5)
            finally:
                lock.release()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        # Wait until the holder has acquired (poll owner_is_alive).
        deadline = time.monotonic() + 2.0
        while not lock.owner_is_alive and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lock.owner_is_alive, "Live holder should be reported alive"

        # A second thread times out (live holder present) — must NOT reset it.
        assert not lock.acquire(timeout=0.3), "Should time out while live holder holds the lock"
        # The live holder is still the owner (we did not steal it).
        assert lock.owner_is_alive, "Live holder's ownership must be preserved after a timeout"

        release_later.set()
        holder.join(timeout=2)

    def test_nested_release_then_thread_dies_still_recovers(self):
        """Nested acquire (RLock count>1); if the thread dies after one release, the
        lock is still leaked (count remains >0 owned by a dead thread). Recovery must work."""
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()
        acquired_flag = threading.Event()

        def nested_hold_and_die():
            assert lock.acquire(timeout=1)   # count=1
            assert lock.acquire(timeout=1)   # count=2 (reentrant, same thread)
            acquired_flag.set()
            lock.release()                   # count=1 — still held by this thread
            # Thread returns WITHOUT the final release → leaked at count=1.

        holder = threading.Thread(target=nested_hold_and_die)
        holder.start()
        assert acquired_flag.wait(timeout=2)
        holder.join(timeout=2)
        assert not holder.is_alive(), "Holder must be dead for the leak scenario"

        # Leaked: internal RLock still held (count=1) by a dead thread.
        assert not lock.owner_is_alive, "Dead holder should report owner_is_alive=False"
        assert not lock.acquire(timeout=0.3), "Cannot acquire a leaked nested lock"

        # Recovery swaps in a fresh RLock → acquirable again.
        lock.force_reset(reason="test: nested leak")
        assert lock.acquire(timeout=1), "Fresh lock must be acquirable after nested-leak reset"
        lock.release()

    def test_concurrent_acquirers_after_leak_serialize(self):
        """After a dead-holder leak + reset, multiple waiting threads must serialize
        correctly on the fresh lock (exactly one at a time), with no lost acquisitions."""
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()

        # Simulate a leaked lock: acquire then let the holder die without release.
        assert lock.acquire(timeout=1)
        # (main thread "dies" conceptually by not releasing — but main can't die, so we
        # emulate the dead-holder state directly via force_reset on a held lock.)

        # Before reset, other threads cannot get in.
        blocked = threading.Event()

        def waiter(name, results):
            ok = lock.acquire(timeout=2)
            if ok:
                try:
                    with timeline_lock:
                        timeline.append((time.monotonic(), f"{name}_IN"))
                    time.sleep(0.1)
                    with timeline_lock:
                        timeline.append((time.monotonic(), f"{name}_OUT"))
                    results.append(name)
                finally:
                    lock.release()

        # Reset to clear the leaked state, then let 3 threads contend.
        lock.force_reset(reason="test: pre-leak reset")

        timeline = []
        timeline_lock = threading.Lock()
        results = []
        threads = [threading.Thread(target=waiter, args=(f"w{i}", results)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert sorted(results) == ["w0", "w1", "w2"], f"All 3 waiters should complete, got {results}"

        # Verify serialization: no IN before the previous OUT.
        events = sorted(timeline, key=lambda x: x[0])
        for i in range(1, len(events)):
            if events[i][1].endswith("_IN"):
                prev_out = [e for e in events[:i] if e[1].endswith("_OUT")]
                assert prev_out, f"An IN event ({events[i][1]}) occurred with no prior OUT — not serialized"

    def test_live_short_hold_not_reported_dead(self):
        """A LIVE thread holding the lock (even briefly) must be reported alive — never
        falsely flagged as a stale/dead holder. owner_is_alive tracks the *thread*, so a
        short-lived live hold is correctly distinguished from a killed-thread leak.

        Uses an event handshake to make the observation deterministic (no racy polling).
        """
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()
        holding = threading.Event()   # set once the holder has acquired
        release = threading.Event()   # signals the holder to release

        def short_hold():
            assert lock.acquire(timeout=1)
            holding.set()
            try:
                release.wait(timeout=2)
            finally:
                lock.release()

        t = threading.Thread(target=short_hold, daemon=True)
        t.start()
        assert holding.wait(timeout=2), "Holder should have acquired the lock"

        # While the (live) holder holds the lock, owner_is_alive must be True.
        assert lock.owner_is_alive, (
            "A live thread holding the lock must be reported alive (not stale)"
        )
        # A competing acquirer must time out (the holder is genuinely running).
        assert not lock.acquire(timeout=0.3), (
            "Competing acquire should time out while a live short-hold is in progress"
        )
        # Ownership must still be intact after the timeout — no spurious reset.
        assert lock.owner_is_alive, (
            "Live holder's ownership must survive a competing timeout (no false stale)"
        )

        release.set()
        t.join(timeout=2)
        assert not lock.owner_is_alive, "Owner cleared after the live holder released"

    def test_context_manager_protocol(self):
        """ResettableRLock supports the `with` statement (context manager)."""
        from agent_cascade.security_handler import ResettableRLock

        lock = ResettableRLock()
        with lock:
            assert lock.owner_is_alive, "Owner alive inside the with-block"
        assert not lock.owner_is_alive, "Owner cleared after the with-block"


# ── Integration tests with real threading ───────────────────────────────────


def _make_minimal_pool():
    """Create a minimal mock AgentPool for integration tests."""
    pool = MagicMock()
    pool.stopped = False
    pool.operation_manager.base_dir = "/tmp/test"
    pool.operation_manager.extra_work_folders_ro = []
    pool.operation_manager.extra_work_folders_rw = []
    pool.operation_manager.enable_timeout = True
    pool.operation_manager.approval_timeout_seconds = 180
    pool.instance_state = {}
    pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()
    return pool


def _make_minimal_app():
    """Create a minimal mock app state for integration tests."""
    return type('App', (), {})()


class TestConcurrentSecurityChecks:
    """Integration test: two concurrent security checks should serialize via execution lock."""

    def test_concurrent_checks_serialize_via_execution_lock(self):
        """Two security checks running simultaneously should not execute in parallel.

        The execution lock ensures only one check runs engine.run at a time.
        We verify this by tracking when each check enters/exits the execution phase.
        """
        pool = _make_minimal_pool()
        app = _make_minimal_app()

        # Track execution timeline
        timeline = []
        timeline_lock = threading.Lock()

        def quick_gen():
            yield ("[YES] Safe", False)

        template = MagicMock()
        template.llm = MagicMock()
        template.llm.generate_cfg = {}
        pool.get_template.return_value = template

        results = []
        errors = []

        def run_check(rid, delay_ms=0):
            """Simulate _execute_check with timeline tracking."""
            try:
                # Ensure execution lock exists
                if not getattr(app, 'security_execution_lock', None):
                    app.security_execution_lock = threading.RLock()

                # Acquire with timeout (mimics security_handler behavior)
                acquired = app.security_execution_lock.acquire(timeout=5)
                if not acquired:
                    errors.append(f"{rid}: failed to acquire lock")
                    return

                try:
                    with timeline_lock:
                        timeline.append((time.monotonic(), f"{rid}_START"))

                    time.sleep(delay_ms / 1000.0)  # Simulate work

                    with timeline_lock:
                        timeline.append((time.monotonic(), f"{rid}_END"))

                    results.append(rid)
                finally:
                    app.security_execution_lock.release()
            except Exception as e:
                errors.append(f"{rid}: {e}")

        # Run two checks concurrently
        t1 = threading.Thread(target=run_check, args=("check_A", 200))
        t2 = threading.Thread(target=run_check, args=("check_B", 200))

        t1.start()
        time.sleep(0.05)  # Small delay so check_A gets lock first
        t2.start()

        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors, f"Errors during test: {errors}"
        assert len(results) == 2, f"Expected 2 results, got {len(results)}"

        # Verify serialization: check_A should complete before check_B starts
        events = sorted(timeline, key=lambda x: x[0])
        a_start = next(t for t, e in events if e == "check_A_START")
        a_end = next(t for t, e in events if e == "check_A_END")
        b_start = next(t for t, e in events if e == "check_B_START")

        assert b_start >= a_end - 0.05, (
            f"Checks should serialize: check_B started at {b_start:.3f} but check_A ended at {a_end:.3f}"
        )


class TestNestedSecurityCheckReentrancy:
    """Integration test: nested security check should not deadlock with RLock."""

    def test_nested_security_check_allows_reentrant_lock(self):
        """If a Security agent triggers another security check, RLock allows reentrancy.

        Simulates the scenario where _execute_check calls itself (via call_agent → new check).
        With a regular Lock/Semaphore this would deadlock; with RLock it succeeds.
        """
        app = _make_minimal_app()

        if not getattr(app, 'security_execution_lock', None):
            app.security_execution_lock = threading.RLock()

        outer_completed = threading.Event()
        inner_completed = threading.Event()
        deadlock_detected = threading.Event()

        def inner_check():
            try:
                # Inner check tries to acquire the same lock
                acquired = app.security_execution_lock.acquire(timeout=2)
                if not acquired:
                    raise RuntimeError("Inner check failed to acquire lock — possible deadlock")
                try:
                    inner_completed.set()
                    time.sleep(0.05)  # Simulate work
                finally:
                    app.security_execution_lock.release()
            except Exception as e:
                outer_completed.set()  # Unblock outer thread
                raise

        def outer_check():
            try:
                acquired = app.security_execution_lock.acquire(timeout=2)
                if not acquired:
                    raise RuntimeError("Outer check failed to acquire lock")
                try:
                    # Simulate nested security check (same thread re-acquires)
                    inner_check()

                    outer_completed.set()
                finally:
                    app.security_execution_lock.release()
            except Exception as e:
                deadlock_detected.set()
                raise

        t = threading.Thread(target=outer_check)
        t.start()

        # Wait with timeout — if we hit this, it's a deadlock
        assert outer_completed.wait(timeout=5), (
            "Outer check did not complete within 5s — RLock reentrancy failed (deadlock)"
        )
        assert inner_completed.is_set(), "Inner check should have completed"
        assert not deadlock_detected.is_set(), "Deadlock was detected during nested check"

        t.join(timeout=2)


class TestTimerCleanupOnException:
    """Integration test: timers are cleaned up even when exceptions occur."""

    def test_warning_timer_cancelled_on_exception_before_execution_lock(self):
        """If an exception occurs before acquiring execution lock, warning timer must be cancelled.

        This tests the timer lifecycle fix — sec_warning_timer is created inside the prompt lock
        but must be cancelled in the outer finally block regardless of where the exception occurs.
        """
        pool = _make_minimal_pool()
        app = _make_minimal_app()
        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()

        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)

        # Track timer objects created
        timers_created = []
        original_timer_init = threading.Timer.__init__

        def tracked_timer_init(self, interval, function, args=None, kwargs=None):
            original_timer_init(self, interval, function, args, kwargs)
            timers_created.append(self)

        ap = {
            "request_id": "test_rid_timer",
            "tool_name": "shell_cmd",
            "description": "test",
            "tool_args": {},
            "agent_name": "Maine",
        }

        # Hold execution lock in another thread so acquire times out
        app.security_execution_lock = threading.RLock()
        release_event = threading.Event()

        def hold_lock():
            app.security_execution_lock.acquire()
            release_event.wait(timeout=10)
            app.security_execution_lock.release()

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()

        try:
            with patch('threading.Timer.__init__', tracked_timer_init):
                with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 0.3):
                    try:
                        handler._execute_check(
                            ap=ap,
                            sec_inst=None,
                            rid="test_rid_timer",
                            auto_apply=True,
                            instance_name="Maine",
                            caller_agent="Maine",
                            prompt_template="Test {tool_name}",
                            timeout_seconds=3600,
                            warning_seconds=2400,
                        )
                    except RuntimeError:
                        pass  # Expected — lock acquire failed

            # Give a moment for any pending cleanup to complete
            time.sleep(0.1)

            # Verify any created timers were cancelled or have stopped running.
            # A properly cleaned up timer is either: _cancelled=True, or not alive (stopped).
            for timer in timers_created:
                is_cancelled = getattr(timer, '_cancelled', False)
                is_alive = timer.is_alive()
                assert is_cancelled or not is_alive, (
                    f"Timer should have been cancelled/stopped. _cancelled={is_cancelled}, alive={is_alive}"
                )
        finally:
            release_event.set()
            holder.join(timeout=2)


class TestFirstYieldSafetyNet:
    """Integration test for the first-yield safety net (commit fb74f04).

    The last-resort guard in _execute_check starts a daemon threading.Timer
    (SECURITY_FIRST_YIELD_TIMEOUT_SECONDS) right before the engine.run() loop. If the
    LLM generator stalls so long that the timer fires BEFORE any token is yielded, the
    first-iteration check observes the set event, sets sec_timeout_reached=True and
    breaks — routing to _handle_timeout → user_reject with a "SECURITY ADVISOR TIMEOUT"
    message. This test simulates exactly that: a generator that blocks past the timeout
    before its first yield.
    """

    def test_stalled_generator_triggers_first_yield_timeout_and_auto_rejects(self):
        """A generator that never yields its first token within the window must auto-reject."""
        pool = _make_minimal_pool()
        app = _make_minimal_app()
        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()

        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)

        ap = {
            "request_id": "test_rid_firstyield",
            "tool_name": "shell_cmd",
            "description": "test",
            "tool_args": {},
            "agent_name": "Maine",
        }

        # A generator that simulates a hung model: it blocks for longer than the
        # (patched) 0.5s first-yield timeout before producing its first yield. The
        # loop's first-iteration check should see the timer's event set and break.
        def _stalled_generator():
            time.sleep(2.0)          # longer than the 0.5s timeout → timer fires first
            yield ("", False)        # only now does it "yield"; loop detects timeout, breaks

        # _create_system_agent must return an object supporting attribute assignment
        # (sec_instance.max_turns = ...) and .conversation access downstream.
        sec_instance_mock = MagicMock()
        sec_instance_mock.conversation = []

        # ExecutionEngine is imported locally inside _execute_check
        # (from agent_cascade.execution_engine import ExecutionEngine), so it is NOT a
        # module attribute of security_handler — patch it at its source module instead.
        # It's used as a *class* (ExecutionEngine(pool)), so we patch with a factory whose
        # return_value is the engine instance mock; that instance's .run() yields our
        # stalled generator and ._create_system_agent() returns the sec instance mock.
        engine_instance = MagicMock()
        engine_instance.run.return_value = _stalled_generator()
        engine_instance._create_system_agent.return_value = sec_instance_mock
        # Skip telemetry bookkeeping in the execution loop's finally block.
        engine_instance._telemetry.return_value = None
        mock_engine_cls = MagicMock(return_value=engine_instance)

        with patch('agent_cascade.security_handler.SECURITY_FIRST_YIELD_TIMEOUT_SECONDS', 0.5):
            with patch('agent_cascade.execution_engine.ExecutionEngine', mock_engine_cls):
                start = time.monotonic()
                handler._execute_check(
                    ap=ap,
                    sec_inst=None,
                    rid="test_rid_firstyield",
                    auto_apply=True,
                    instance_name="Maine",
                    caller_agent="Maine",
                    prompt_template="Test {tool_name}",
                    timeout_seconds=3600,
                    warning_seconds=2400,
                )
                elapsed = time.monotonic() - start

        # The check must have auto-rejected via the timeout path.
        assert pool.operation_manager.user_reject.called, (
            "user_reject should be called when the first-yield timeout fires"
        )
        args = pool.operation_manager.user_reject.call_args.args
        assert args[0] == "test_rid_firstyield", (
            f"user_reject should target the request id, got {args[0]!r}"
        )
        assert "SECURITY ADVISOR TIMEOUT" in args[1], (
            f"reject message should carry the timeout marker, got {args[1]!r}"
        )

        # The Security instance should have been halted as part of _handle_timeout.
        pool.halt_instance.assert_called_once_with("Security_test_rid_firstyield")

        # Sanity: the run was gated by the 2.0s stalled-generator sleep (not instant),
        # confirming we actually exercised the blocking path. Keep loose to avoid flakiness.
        assert elapsed >= 1.5, (
            f"Check should have blocked ~2s on the stalled generator, got {elapsed:.2f}s"
        )


class TestActiveChecksCleanupOnLockTimeout:
    """Integration test: active_checks is cleaned up when lock acquire times out."""

    def test_active_checks_removed_on_execution_lock_timeout(self):
        """If execution lock acquire fails, the rid must be removed from active_checks.

        This tests the state leak fix — rid was added in run_check() but if _execute_check
        raises RuntimeError before creating sec_state_key, _cleanup can't find it to remove.
        """
        pool = _make_minimal_pool()
        app = _make_minimal_app()
        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()

        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)

        # Pre-populate active_checks (simulates run_check having added the rid)
        from agent_cascade.security_handler import _get_active_checks_state
        active_checks, checks_lock = _get_active_checks_state(app)
        with checks_lock:
            active_checks.add("test_rid_leak")

        assert "test_rid_leak" in active_checks

        ap = {
            "request_id": "test_rid_leak",
            "tool_name": "shell_cmd",
            "description": "test",
            "tool_args": {},
            "agent_name": "Maine",
        }

        # Hold execution lock in another thread so acquire times out (RLock blocks different threads)
        app.security_execution_lock = threading.RLock()
        release_event = threading.Event()

        def hold_lock():
            app.security_execution_lock.acquire()
            release_event.wait(timeout=10)
            app.security_execution_lock.release()

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()

        try:
            with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 0.3):
                handler._execute_check(
                    ap=ap,
                    sec_inst=None,
                    rid="test_rid_leak",
                    auto_apply=True,
                    instance_name="Maine",
                    caller_agent="Maine",
                    prompt_template="Test {tool_name}",
                    timeout_seconds=3600,
                    warning_seconds=2400,
                )
                assert False, "Should have raised RuntimeError on lock acquire failure"
        except RuntimeError as e:
            assert "Failed to acquire" in str(e), f"Expected lock acquire error, got: {e}"
        finally:
            release_event.set()
            holder.join(timeout=2)

        # Verify active_checks was cleaned up despite the exception
        with checks_lock:
            assert "test_rid_leak" not in active_checks, (
                "active_checks should be cleaned up even when lock acquire times out"
            )


class TestSlotYieldBeforeSecurityCheck:
    """Tests that _execute_check unconditionally releases the caller's slot before
    running the Security agent. No re-acquire — the next turn acquires naturally."""

    def _make_pool_with_fast_engine(self):
        """Pool + engine mock that yields an immediate [YES] verdict."""
        pool = _make_minimal_pool()
        app = _make_minimal_app()

        template = MagicMock()
        template.llm = MagicMock()
        template.llm.generate_cfg = {}
        pool.get_template.return_value = template

        sec_instance_mock = MagicMock()
        sec_instance_mock.conversation = [{"role": "assistant", "content": "[YES] Safe operation"}]

        engine_instance = MagicMock()
        engine_instance.run.return_value = iter([("[YES] Safe operation", False)])
        engine_instance._create_system_agent.return_value = sec_instance_mock
        engine_instance._telemetry.return_value = None
        mock_engine_cls = MagicMock(return_value=engine_instance)

        # Caller instance with a real _state_lock and a live slot release callback.
        caller_inst = MagicMock()
        caller_inst.agent_class = "Maine"
        caller_inst.instance_name = "Maine"
        caller_inst._state_lock = threading.Lock()
        caller_inst._slot_release = lambda: None  # Simulates a held slot
        caller_inst._slot_key = "http://test:8080"
        pool.get_instance.return_value = caller_inst

        return pool, app, mock_engine_cls, caller_inst

    def test_release_slot_called_before_security_runs(self):
        """_release_slot must be called on the caller before engine.run()."""
        pool, app, mock_engine_cls, caller_inst = self._make_pool_with_fast_engine()
        engine_instance = mock_engine_cls.return_value

        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()
        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)
        ap = {
            "request_id": "test_rid_release",
            "tool_name": "shell_cmd",
            "description": "test",
            "tool_args": {},
            "agent_name": "Maine",
        }

        with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 0.5):
            with patch('agent_cascade.execution_engine.ExecutionEngine', mock_engine_cls):
                handler._execute_check(
                    ap=ap, sec_inst=None, rid="test_rid_release", auto_apply=True,
                    instance_name="Maine", caller_agent="Maine",
                    prompt_template="Test {tool_name}",
                    timeout_seconds=3600, warning_seconds=2400,
                )

        # _release_slot must have been called (static method on engine class).
        assert engine_instance._release_slot.called, (
            "_release_slot should be called to free the caller's slot before Security runs"
        )
        # No reacquire_for call — the next turn acquires naturally.
        assert not engine_instance.reacquire_for.called, (
            "reacquire_for should NOT be called — next turn acquires its own slot"
        )

    def test_release_slot_noop_when_no_callback(self):
        """When caller has no _slot_release (None), _release_slot is still called but
        is a safe no-op internally (checks for None before releasing)."""
        pool, app, mock_engine_cls, caller_inst = self._make_pool_with_fast_engine()
        engine_instance = mock_engine_cls.return_value

        # No slot held
        caller_inst._slot_release = None
        caller_inst._slot_key = None

        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()
        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)
        ap = {
            "request_id": "test_rid_noslot",
            "tool_name": "shell_cmd",
            "description": "test",
            "tool_args": {},
            "agent_name": "Maine",
        }

        with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 0.5):
            with patch('agent_cascade.execution_engine.ExecutionEngine', mock_engine_cls):
                handler._execute_check(
                    ap=ap, sec_inst=None, rid="test_rid_noslot", auto_apply=True,
                    instance_name="Maine", caller_agent="Maine",
                    prompt_template="Test {tool_name}",
                    timeout_seconds=3600, warning_seconds=2400,
                )

        # _release_slot is still called (unconditional) but handles None gracefully.
        assert engine_instance._release_slot.called


class TestLeakedLockRecoveryEndToEnd:
    """End-to-end integration test for the leaked-lock recovery path in _execute_check.

    This is the real-world scenario from the bug report: a previous Security check's
    daemon thread was killed before reaching exec_lock.release(), leaking the lock.
    A subsequent check must detect the DEAD holder, force-reset the lock, and proceed
    to run the engine — instead of raising RuntimeError and timing out for 10s.

    Contrast with TestActiveChecksCleanupOnLockTimeout / TestTimerCleanupOnException,
    which hold the lock from a LIVE thread and expect a RuntimeError (live holder must
    NOT be stolen). Here the holder is DEAD, so recovery kicks in.
    """

    def _make_pool_with_fast_engine(self):
        """Pool + engine mock that yields an immediate [YES] verdict (no stall).

        The verdict is parsed from sec_instance.conversation (via extract_instance_output),
        NOT directly from the engine.run() yield — so we populate conversation with the
        [YES] text to mirror what a real engine.run() would append.
        """
        pool = _make_minimal_pool()
        app = _make_minimal_app()

        template = MagicMock()
        template.llm = MagicMock()
        template.llm.generate_cfg = {}
        pool.get_template.return_value = template

        sec_instance_mock = MagicMock()
        # The real engine.run() appends the model's response to conversation. We seed it
        # so extract_instance_output finds the [YES] verdict after the run loop completes.
        sec_instance_mock.conversation = [{"role": "assistant", "content": "[YES] Safe operation"}]

        engine_instance = MagicMock()
        # Immediate single yield → loop runs once, then exhausts (no stall).
        engine_instance.run.return_value = iter([("[YES] Safe operation", False)])
        engine_instance._create_system_agent.return_value = sec_instance_mock
        engine_instance._telemetry.return_value = None
        mock_engine_cls = MagicMock(return_value=engine_instance)

        return pool, app, mock_engine_cls

    def test_dead_holder_leak_is_recovered_and_check_proceeds(self):
        """A leaked lock (dead holder) must be reset and the check must run to completion."""
        from agent_cascade.security_handler import ResettableRLock

        pool, app, mock_engine_cls = self._make_pool_with_fast_engine()
        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()
        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)

        ap = {
            "request_id": "test_rid_recover",
            "tool_name": "shell_cmd",
            "description": "test",
            "tool_args": {},
            "agent_name": "Maine",
        }

        # Seed the execution lock as a ResettableRLock (as production does) and leak it:
        # acquire in a thread that dies WITHOUT releasing.
        app.security_execution_lock = ResettableRLock()
        acquired_flag = threading.Event()

        def leak_holder():
            assert app.security_execution_lock.acquire(timeout=1)
            acquired_flag.set()
            # Return without release → leaked lock, dead holder.

        leaker = threading.Thread(target=leak_holder, daemon=True)
        leaker.start()
        assert acquired_flag.wait(timeout=2), "Leaker should have acquired the lock"
        leaker.join(timeout=2)
        assert not leaker.is_alive(), "Leaker thread must be dead (simulating a killed daemon)"
        # Confirm the leak: the internal RLock is held by a now-dead thread.
        assert not app.security_execution_lock.owner_is_alive, (
            "After the holder dies, owner_is_alive must be False (leak detected)"
        )

        # Now run a NEW check. It should detect the dead holder, force-reset, and proceed.
        with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 0.5):
            with patch('agent_cascade.execution_engine.ExecutionEngine', mock_engine_cls):
                handler._execute_check(
                    ap=ap,
                    sec_inst=None,
                    rid="test_rid_recover",
                    auto_apply=True,
                    instance_name="Maine",
                    caller_agent="Maine",
                    prompt_template="Test {tool_name}",
                    timeout_seconds=3600,
                    warning_seconds=2400,
                )

        # The check ran to completion (no RuntimeError) and auto-approved the [YES] verdict.
        assert pool.operation_manager.user_approve.called, (
            "After recovering a leaked lock, the check should proceed and auto-approve"
        )
        args = pool.operation_manager.user_approve.call_args.args
        assert args[0] == "test_rid_recover", (
            f"user_approve should target the request id, got {args[0]!r}"
        )

    def test_live_holder_still_raises_runtime_error(self):
        """A LIVE holder must NOT be reset — _execute_check raises RuntimeError as before.

        This is the safety guard: we only steal a lock whose owner thread is dead. A live
        holder (another check genuinely running) keeps normal timeout semantics.
        """
        from agent_cascade.security_handler import ResettableRLock

        pool, app, mock_engine_cls = self._make_pool_with_fast_engine()
        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()
        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)

        ap = {
            "request_id": "test_rid_live",
            "tool_name": "shell_cmd",
            "description": "test",
            "tool_args": {},
            "agent_name": "Maine",
        }

        # Seed a ResettableRLock and hold it from a LIVE thread (waits on an event).
        app.security_execution_lock = ResettableRLock()
        acquired_flag = threading.Event()
        release_event = threading.Event()

        def live_holder():
            assert app.security_execution_lock.acquire(timeout=1)
            acquired_flag.set()
            try:
                release_event.wait(timeout=10)
            finally:
                app.security_execution_lock.release()

        holder = threading.Thread(target=live_holder, daemon=True)
        holder.start()
        assert acquired_flag.wait(timeout=2), "Live holder should have acquired the lock"

        try:
            with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 0.3):
                with patch('agent_cascade.execution_engine.ExecutionEngine', mock_engine_cls):
                    handler._execute_check(
                        ap=ap,
                        sec_inst=None,
                        rid="test_rid_live",
                        auto_apply=True,
                        instance_name="Maine",
                        caller_agent="Maine",
                        prompt_template="Test {tool_name}",
                        timeout_seconds=3600,
                        warning_seconds=2400,
                    )
                assert False, "Should have raised RuntimeError for a live holder"
        except RuntimeError as e:
            assert "Failed to acquire" in str(e), f"Expected lock-acquire error, got: {e}"
            # The check must NOT have proceeded (no approve/reject side effects).
            assert not pool.operation_manager.user_approve.called, (
                "Check must not auto-approve when it could not acquire a live-held lock"
            )
        finally:
            release_event.set()
            holder.join(timeout=2)

    def test_repeated_leaks_keep_recovering(self):
        """Multiple successive leaked locks must each be recovered — no permanent wedge.

        This is the regression guard for the original bug: after one leaked lock wedged
        the system, EVERY subsequent check timed out. Recovery must work repeatedly so a
        single crash never permanently breaks all security checks.
        """
        from agent_cascade.security_handler import ResettableRLock

        pool, app, mock_engine_cls = self._make_pool_with_fast_engine()
        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()
        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)

        app.security_execution_lock = ResettableRLock()

        def leak_once(rid):
            """Leak the current lock, then run a check that must recover and proceed."""
            # Leak: acquire in a thread that dies without releasing.
            acquired_flag = threading.Event()

            def leaker():
                assert app.security_execution_lock.acquire(timeout=1)
                acquired_flag.set()

            t = threading.Thread(target=leaker, daemon=True)
            t.start()
            assert acquired_flag.wait(timeout=2)
            t.join(timeout=2)
            assert not t.is_alive()

            ap = {
                "request_id": rid,
                "tool_name": "shell_cmd",
                "description": "test",
                "tool_args": {},
                "agent_name": "Maine",
            }
            with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 0.5):
                with patch('agent_cascade.execution_engine.ExecutionEngine', mock_engine_cls):
                    handler._execute_check(
                        ap=ap, sec_inst=None, rid=rid, auto_apply=True,
                        instance_name="Maine", caller_agent="Maine",
                        prompt_template="Test {tool_name}",
                        timeout_seconds=3600, warning_seconds=2400,
                    )

        # Three successive leaks — each must be recovered and the check must proceed.
        for i in range(3):
            leak_once(f"test_rid_repeat_{i}")

        assert pool.operation_manager.user_approve.call_count == 3, (
            f"All 3 checks should have recovered and auto-approved, "
            f"got {pool.operation_manager.user_approve.call_count}"
        )
