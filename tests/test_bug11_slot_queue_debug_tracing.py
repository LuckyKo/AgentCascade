"""
Unit tests for BUG-11 fix — DEBUG tracing of the slot-queue lifecycle.

Spec: reports/fix_plans/BUG-11_slot_queue_debug_tracing.md

Covers (all via caplog on the agent_cascade.slot_queue logger):
- Enqueue log: once per enqueue, carries agent, ticket, position, waiters, holders.
- Grant (queued): ticket + waited fields; Grant (fast path): "(fast-path)", no ticket.
- Release: held=<duration> ≥ actual sleep delta.
- Cancel: pre-raise in acquire, cancel() by ticket and by agent, terminate_for_agent.
- No-spam guard: nothing logs per poll tick inside the 1s wait loop.
- Regression: _log_acquire_timeout WARNING still fires alongside new coverage.
"""

import threading
import time

import pytest

from agent_cascade.slot_queue import (
    SlotPool,
    SlotCancelled,
    QUEUE_WAIT_TIMEOUT,
)


@pytest.fixture()
def slot_log(caplog):
    """Capture DEBUG+ records from the slot_queue module logger."""
    import logging
    caplog.set_level(logging.DEBUG, logger="agent_cascade.slot_queue")
    return caplog


def records(log, needle):
    return [r for r in log.records if needle in r.getMessage()]


def acquire_immediate(pool: SlotPool, name: str):
    """Acquire with no contention expected; returns the release callback."""
    cb = pool.acquire(instance_name=name, agent_class="test")
    # Fast-path grants still return a working release callback.
    return cb


def wait_queued(pool: SlotPool, name: str, timeout: float = 5.0) -> bool:
    """Block until `name` has a live ticket in the pool's FIFO queue.

    Fixes the setup race where the waiter thread signals 'starting' before
    pool.acquire() actually enqueues its ticket.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = pool.get_status()
        if any(w["instance_name"] == name for w in status["waiters"]):
            return True
        time.sleep(0.02)
    return False


class TestBug11EnqueueLog:

    def test_enqueue_logs_once_with_position_and_holders(self, slot_log):
        pool = SlotPool(key="p1", capacity=1)
        holder_cb = acquire_immediate(pool, "A")

        acquired = threading.Event()
        holder_box = {}

        def waiter():
            holder_box["cb"] = pool.acquire(instance_name="B", agent_class="coder")
            acquired.set()

        t = threading.Thread(target=waiter)
        t.start()
        try:
            assert wait_queued(pool, "B"), "B never made it into the queue"
            time.sleep(0.2)  # give a grant (if buggy) a moment to surface
            assert not acquired.is_set(), "B must NOT acquire while A holds"
        finally:
            pass

        queued = records(slot_log, "[SLOTPOOL] Queued")
        assert len(queued) == 1, f"expected exactly one Queued record, got {len(queued)}"
        msg = queued[0].getMessage()
        assert "agent=B (coder)" in msg
        assert "ticket=" in msg
        assert "position=1" in msg
        assert "holders=['A']" in msg
        assert f"timeout={QUEUE_WAIT_TIMEOUT}" in msg

        # Cleanup: release so the waiter finishes, then join.
        holder_cb()
        assert acquired.wait(timeout=5)
        holder_box["cb"]()
        t.join(timeout=5)


class TestBug11GrantLog:

    def test_queued_grant_logs_ticket_and_waited(self, slot_log):
        pool = SlotPool(key="p2", capacity=1)
        holder_cb = acquire_immediate(pool, "A")

        acquired = threading.Event()
        box = {}

        def waiter():
            box["cb"] = pool.acquire(instance_name="B", agent_class="test")
            acquired.set()

        t = threading.Thread(target=waiter)
        t.start()
        assert wait_queued(pool, "B"), "B never made it into the queue"
        time.sleep(0.3)          # accumulate a measurable wait while queued
        holder_cb()              # release → B granted from queue

        assert acquired.wait(timeout=5)
        granted = [r for r in records(slot_log, "[SLOTPOOL] Granted")
                   if "ticket=" in r.getMessage()]
        assert len(granted) == 1
        msg = granted[0].getMessage()
        assert "agent=B" in msg
        assert "waited=" in msg
        waited = float(msg.split("waited=")[1].split("s")[0])
        assert waited >= 0.2, f"waited={waited}s should reflect real queue time"

        box["cb"]()
        t.join(timeout=5)

    def test_fast_path_grant_logs_fastpath_without_ticket(self, slot_log):
        pool = SlotPool(key="p3", capacity=1)
        cb = pool.acquire(instance_name="A", agent_class="test")

        granted = records(slot_log, "[SLOTPOOL] Granted")
        assert len(granted) == 1
        msg = granted[0].getMessage()
        assert "(fast-path)" in msg
        assert "ticket=" not in msg

        cb()


class TestBug11ReleaseLog:

    def test_release_logs_held_duration(self, slot_log):
        pool = SlotPool(key="p4", capacity=1)
        cb = pool.acquire(instance_name="A", agent_class="test")
        slot_log.clear()  # drop enqueue/grant noise from setup
        hold_for = 0.25
        time.sleep(hold_for)
        cb()

        released = records(slot_log, "[SLOTPOOL] Released")
        assert len(released) == 1
        msg = released[0].getMessage()
        held = float(msg.split("held=")[1].split("s")[0])
        assert held >= hold_for - 0.05, f"held={held}s should be ≥ {hold_for - 0.05}s"
        assert "running=0/1" in msg

    def test_stale_release_stays_silent(self, slot_log):
        pool = SlotPool(key="p5", capacity=1)
        cb = pool.acquire(instance_name="A", agent_class="test")
        slot_log.clear()
        cb()   # real release → logs
        cb()   # stale/idempotent release → silent

        assert len(records(slot_log, "[SLOTPOOL] Released")) == 1


class TestBug11CancelLogs:

    def test_cancelled_while_waiting_logs_before_raise(self, slot_log):
        pool = SlotPool(key="p6", capacity=1)
        cb = acquire_immediate(pool, "A")

        caught = threading.Event()

        def waiter():
            try:
                pool.acquire(instance_name="B", agent_class="test", timeout=10)
            except SlotCancelled:
                caught.set()

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.2)  # let B enqueue
        pool.terminate_for_agent("B")

        assert caught.wait(timeout=5)
        cancelled = records(slot_log, "Cancelled while waiting")
        assert len(cancelled) == 1
        assert "agent=B" in cancelled[0].getMessage()
        cb()
        t.join(timeout=5)

    def test_cancel_by_ticket_id_logs(self, slot_log):
        pool = SlotPool(key="p7", capacity=1)
        cb = acquire_immediate(pool, "A")

        caught = threading.Event()
        box = {}

        def waiter():
            try:
                box["cb"] = pool.acquire(instance_name="B", agent_class="test", timeout=10)
            except SlotCancelled:
                caught.set()

        t = threading.Thread(target=waiter)
        t.start()
        assert wait_queued(pool, "B"), "B never made it into the queue"

        # Find B's live ticket id via status.
        status = pool.get_status()
        assert status["waiting_count"] == 1
        tid = status["waiters"][0]["ticket_id"]
        assert pool.cancel(ticket_id=tid) is True

        assert caught.wait(timeout=5), "B should have raised SlotCancelled"
        cancelled = records(slot_log, "[SLOTPOOL] Cancelled on")
        assert any(f"ticket={tid}" in r.getMessage() for r in cancelled)
        cb()
        t.join(timeout=5)

    def test_terminate_for_agent_logs(self, slot_log):
        pool = SlotPool(key="p8", capacity=1)
        cb = acquire_immediate(pool, "A")

        caught = threading.Event()

        def waiter():
            try:
                pool.acquire(instance_name="B", agent_class="test", timeout=10)
            except SlotCancelled:
                caught.set()

        t = threading.Thread(target=waiter)
        t.start()
        assert wait_queued(pool, "B"), "B never made it into the queue"

        cancelled_count, _ = pool.terminate_for_agent("B")
        assert cancelled_count == 1

        assert caught.wait(timeout=5), "B should have raised SlotCancelled"
        terminated = records(slot_log, "[SLOTPOOL] Terminated on")
        assert len(terminated) == 1
        assert "agent=B" in terminated[0].getMessage()
        assert "tickets=[" in terminated[0].getMessage()
        cb()
        t.join(timeout=5)


class TestBug11NoSpamAndRegression:

    def test_no_per_tick_spam_in_wait_loop(self, slot_log):
        """A queued waiter ticking through many poll iterations must not add
        any Queued records beyond the single enqueue line."""
        pool = SlotPool(key="p9", capacity=1)
        cb = acquire_immediate(pool, "A")

        acquired = threading.Event()
        box = {}

        def waiter():
            box["cb"] = pool.acquire(instance_name="B", agent_class="test")
            acquired.set()

        t = threading.Thread(target=waiter)
        t.start()
        assert wait_queued(pool, "B"), "B never made it into the queue"

        # Hold capacity shut long enough for ≥3 poll ticks (loop waits 1s per tick).
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            assert pool.get_status()["waiting_count"] == 1
            time.sleep(0.1)

        before = len(records(slot_log, "[SLOTPOOL] Queued"))
        assert before == 1, "enqueue must log exactly once"

        cb()  # grant B so its loop exits
        assert acquired.wait(timeout=15), "B should acquire after A releases"
        box["cb"]()
        t.join(timeout=15)

        after = len(records(slot_log, "[SLOTPOOL] Queued"))
        assert after == 1, "no additional Queued records may appear during polling"

    def test_timeout_warning_still_fires(self, slot_log):
        """Regression guard: existing _log_acquire_timeout WARNING unchanged."""
        import logging
        caplog_at_warn = slot_log
        pool = SlotPool(key="p10", capacity=1)
        cb = acquire_immediate(pool, "A")

        timed_out = threading.Event()

        def waiter():
            try:
                pool.acquire(instance_name="B", agent_class="test", timeout=1)
            except TimeoutError:
                timed_out.set()

        caplog_at_warn.set_level(logging.DEBUG, logger="agent_cascade.slot_queue")
        t = threading.Thread(target=waiter)
        t.start()
        t.join(timeout=10)

        assert timed_out.wait(timeout=5)
        warnings = [r for r in caplog_at_warn.records
                    if r.levelno >= logging.WARNING and "[SLOTPOOL] Acquire timeout" in r.getMessage()]
        assert len(warnings) == 1
        cb()
