"""Regression tests for D-5: single-flight acquire gate in SlotPool.

One instance can have at most ONE in-flight blocking acquire per pool.
A concurrent duplicate is suppressed (WARNING logged, no 2nd ticket) and
waits for the primary to finish before re-checking capacity.
"""
import logging
import threading
import time

import pytest

from agent_cascade.slot_queue import SlotPool, SlotQueueTimeout


def _app_logger_names():
    from agent_cascade.instance_id import get_instance_id
    _id = get_instance_id() or ''
    names = ['agent_cascade_logger', 'agent_cascade']
    if _id:
        names.append(f'agent_cascade_logger.{_id}')
    return names


def test_second_acquire_same_instance_does_not_enqueue(caplog):
    """Concurrent duplicate acquire by same instance must NOT enqueue a 2nd ticket."""
    for name in _app_logger_names():
        caplog.set_level(logging.WARNING, logger=name)

    pool = SlotPool(key='sf1', capacity=1)
    cb_a = pool.acquire(instance_name='A', agent_class='test')

    results = [None, None]
    errors = [None, None]

    def acquire_b(idx):
        try:
            results[idx] = pool.acquire(instance_name='B', agent_class='test', timeout=10.0)
        except Exception as e:
            errors[idx] = e

    t1 = threading.Thread(target=acquire_b, args=(0,), daemon=True)
    t2 = threading.Thread(target=acquire_b, args=(1,), daemon=True)
    t1.start()
    time.sleep(0.3)  # let t1 enqueue
    t2.start()       # t2 is the duplicate

    time.sleep(1.0)  # both should be waiting now

    # Only ONE ticket for B in the waiters
    b_tickets = [t for t in pool._waiters.values() if t.instance_name == 'B']
    assert len(b_tickets) == 1, f"Expected 1 ticket for B, got {len(b_tickets)}"
    assert 'B' in pool._inflight

    # DUPLICATE-ACQUIRE warning was emitted
    msgs = [r.getMessage() for r in caplog.records]
    assert any('DUPLICATE-ACQUIRE suppressed' in m for m in msgs), \
        f"No DUPLICATE-ACQUIRE warning found in: {msgs[:5]}"

    # Release A: the primary (t1, head of queue) gets granted first.
    cb_a()
    t1.join(timeout=5.0)
    assert errors[0] is None, f"Primary error: {errors[0]}"
    assert results[0] is not None

    # The duplicate (t2) wakes up, finds pool busy (t1 holds it), falls through
    # to normal enqueue and waits. Release t1's permit so t2 can complete.
    results[0]()
    t2.join(timeout=5.0)
    assert errors[1] is None, f"Duplicate error: {errors[1]}"
    assert results[1] is not None

    # Clean up
    results[1]()
    assert len(pool._running) == 0
    assert pool._orphan_overwrites == 0


def test_duplicate_waiter_times_out_without_enqueueing():
    """A duplicate with a short timeout raises SlotQueueTimeout without enqueuing."""
    pool = SlotPool(key='sf2', capacity=1)
    cb_a = pool.acquire(instance_name='A', agent_class='test')

    result = [None]
    error = [None]

    def acquire_b():
        try:
            result[0] = pool.acquire(instance_name='B', agent_class='test', timeout=1.5)
        except SlotQueueTimeout as e:
            error[0] = e

    t1 = threading.Thread(target=lambda: pool.acquire(instance_name='B', agent_class='test', timeout=10.0), daemon=True)
    t2 = threading.Thread(target=acquire_b, daemon=True)
    t1.start()
    time.sleep(0.3)
    t2.start()

    t2.join(timeout=5.0)
    assert not t2.is_alive(), 'Duplicate waiter did not time out'
    assert isinstance(error[0], SlotQueueTimeout)

    # Still only 1 ticket for B (the primary's)
    b_tickets = [t for t in pool._waiters.values() if t.instance_name == 'B']
    assert len(b_tickets) <= 1

    cb_a()
    t1.join(timeout=5.0)


def test_inflight_cleared_on_primary_exception():
    """After the primary times out, _inflight is empty so a later acquire works."""
    pool = SlotPool(key='sf3', capacity=1)
    cb_a = pool.acquire(instance_name='A', agent_class='test')

    error = [None]

    def acquire_b():
        try:
            pool.acquire(instance_name='B', agent_class='test', timeout=1.0)
        except SlotQueueTimeout as e:
            error[0] = e

    t = threading.Thread(target=acquire_b, daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert isinstance(error[0], SlotQueueTimeout)

    # _inflight must be empty after the primary exits
    assert pool._inflight == {}, f"_inflight not cleared: {pool._inflight}"

    # A fresh acquire by B should work now (after releasing A)
    cb_a()
    cb_b = pool.acquire(instance_name='B', agent_class='test', timeout=5.0)
    assert cb_b is not None
    cb_b()


def test_distinct_instances_unaffected():
    """Two different instances both enqueue independently (gate is per-instance)."""
    pool = SlotPool(key='sf4', capacity=1)
    cb_a = pool.acquire(instance_name='A', agent_class='test')

    results = [None, None]

    def acquire(name):
        results[0 if name == 'B' else 1] = pool.acquire(instance_name=name, agent_class='test', timeout=10.0)

    t1 = threading.Thread(target=acquire, args=('B',), daemon=True)
    t2 = threading.Thread(target=acquire, args=('C',), daemon=True)
    t1.start()
    time.sleep(0.2)
    t2.start()
    time.sleep(0.5)

    # Both B and C should have tickets (distinct instances, no suppression)
    assert len(pool._waiters) == 2, f"Expected 2 waiters, got {len(pool._waiters)}"

    cb_a()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    for r in results:
        if r:
            r()


def test_inflight_never_wedges():
    """Hammer the gate with 8 concurrent threads; _inflight must be empty after."""
    pool = SlotPool(key='sf5', capacity=1)
    cb_holder = pool.acquire(instance_name='HOLDER', agent_class='test')

    errors = []

    def hammer(i):
        try:
            pool.acquire(instance_name='X', agent_class='test', timeout=2.0)
        except (SlotQueueTimeout, Exception):
            pass  # expected under contention

    threads = [threading.Thread(target=hammer, args=(i,), daemon=True) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    cb_holder()
    # Give a moment for any stragglers
    time.sleep(0.5)
    assert pool._inflight == {}, f"_inflight wedged: {pool._inflight}"


def test_cancel_all_during_duplicate_wait():
    """Cancel the primary's ticket while duplicate waits: both terminate, no leak.

    The duplicate does NOT consult termination directly (documented limitation).
    It exits because the primary's SlotCancelled raise triggers `finally` which
    sets the Event, waking the duplicate. The duplicate then re-checks capacity;
    if still busy it enqueues normally and we cancel that ticket too.
    """
    from agent_cascade.slot_queue import SlotCancelled

    pool = SlotPool(key='sf6', capacity=1)
    cb_a = pool.acquire(instance_name='A', agent_class='test')

    errors = [None, None]

    def acquire_b(idx):
        try:
            pool.acquire(instance_name='B', agent_class='test', timeout=30.0)
        except (SlotCancelled, SlotQueueTimeout, Exception) as e:
            errors[idx] = e

    t1 = threading.Thread(target=acquire_b, args=(0,), daemon=True)
    t2 = threading.Thread(target=acquire_b, args=(1,), daemon=True)
    t1.start()
    time.sleep(0.3)  # let t1 enqueue as primary
    t2.start()       # t2 is the duplicate

    time.sleep(1.0)  # both should be waiting now

    # Cancel the primary's ticket — triggers SlotCancelled in its wait loop,
    # which sets the Event in finally, waking the duplicate.
    with pool._cond:
        for ticket in list(pool._waiters.values()):
            if ticket.instance_name == 'B':
                ticket.cancelled.set()
                ticket.cancelled_by = 'test'

    t1.join(timeout=5.0)
    assert not t1.is_alive(), 'Primary thread did not terminate after cancel'
    assert isinstance(errors[0], SlotCancelled)

    # The duplicate is now awake (event set). It re-checks: pool still busy (A holds).
    # It enqueues a new ticket. Cancel that too so it exits.
    time.sleep(0.5)  # give the duplicate time to re-enqueue
    with pool._cond:
        for ticket in list(pool._waiters.values()):
            if ticket.instance_name == 'B':
                ticket.cancelled.set()
                ticket.cancelled_by = 'test'

    t2.join(timeout=5.0)
    assert not t2.is_alive(), 'Duplicate thread did not terminate'

    # No leaked ticket
    assert len(pool._waiters) == 0, f"Leaked tickets: {len(pool._waiters)}"

    # _inflight must be empty (finally cleared on both exit paths)
    assert pool._inflight == {}, f"_inflight not cleared: {pool._inflight}"

    cb_a()
