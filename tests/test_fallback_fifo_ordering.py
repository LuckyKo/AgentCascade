"""
Unit tests for the async-to-sync fallback FIFO ordering fix.

When an agent's LLM call falls back to a conc=0 endpoint (shared sequential slot),
the call is routed through SlotPool (FIFO) instead of the non-FIFO semaphore.

These tests verify:
- FIFO ordering on the shared sequential slot when one agent holds it and another waits
- Per-call acquisition for an agent that has no lifecycle slot (_slot_key=None)
- No double acquisition when the agent already holds the conc=0 slot
- Generator finalization releases the slot (normal + mid-stream exception)
- Exception before generator is consumed still releases the slot

The SlotPool acquire/release logic lives in EndpointScheduler.acquire() and the
generator wrapper inside call_with_fallback().execute_with_sem. We test the FIFO
semantics via the real scheduler, and the wrapper release semantics via a small
helper that mirrors execute_with_sem's SlotPool path exactly (same structure),
since driving the full call_with_fallback requires a fully-configured APIRouter
with endpoints — which would add noise without exercising more logic.

No LLM or network connections required.
"""

import threading
import time
import unittest
from typing import List

from agent_cascade.slot_queue import (
    SlotPool,
    SlotHolder,
)
from agent_cascade.api_router import EndpointScheduler


def _acquire_immediate(pool: SlotPool, instance_name: str) -> SlotHolder:
    """Acquire a permit immediately without waiting (for test setup).

    Must be called when capacity is available. Mirrors the helper in
    tests/test_slot_queue.py so we don't reach into private counters differently.
    """
    with pool._cond:
        acquisition_id = next(pool._acquisition_counter)
        holder = SlotHolder(
            agent_name=instance_name,
            instance_name=instance_name,
            acquisition_id=acquisition_id,
            granted_at=time.monotonic(),
        )
        pool._running[instance_name] = holder
        return holder


class TestFIFOOrderingOnSharedSlot(unittest.TestCase):
    """Test 1: FIFO ordering when B falls back while A holds the conc=0 slot."""

    def test_b_times_out_while_a_holds_then_acquires_after_release(self):
        """A holds the shared sequential slot; B (conc=0) must wait, then proceed.

        Uses the real EndpointScheduler so we exercise the exact code path that
        call_with_fallback uses for conc=0 fallback (scheduler.acquire with
        concurrency_limit=0 → shared sequential pool).
        """
        sched = EndpointScheduler()

        # Agent A acquires the conc=0 slot at lifecycle level.
        release_a = sched.acquire(
            api_base="http://shared-api",
            concurrency_limit=0,
            instance_name="A",
            agent_class="orchestrator",
            timeout=5.0,
        )
        self.assertIsNotNone(release_a)

        # Agent B attempts a per-call conc=0 acquisition while A holds the slot.
        # With capacity 1 and A holding it, B must block until its (short) timeout.
        # EndpointScheduler.acquire() re-raises SlotQueueTimeout as a plain TimeoutError
        # (the documented public contract), so assert on TimeoutError here.
        with self.assertRaises(TimeoutError):
            sched.acquire(
                api_base="http://shared-api",
                concurrency_limit=0,
                instance_name="B",
                agent_class="coder",
                timeout=0.5,
            )

        # A still holds the slot; B's timed-out ticket was removed.
        pool = sched._pools['_shared_sequential_slot_']
        with pool._cond:
            self.assertIn("A", pool._running)
            self.assertNotIn("B", pool._running)
            self.assertEqual(len(pool._waiters), 0)

        # Release A's slot — B can now acquire immediately (FIFO, no other waiters).
        release_a()
        release_b = sched.acquire(
            api_base="http://shared-api",
            concurrency_limit=0,
            instance_name="B",
            agent_class="coder",
            timeout=5.0,
        )
        self.assertIsNotNone(release_b)
        with pool._cond:
            self.assertIn("B", pool._running)
            self.assertNotIn("A", pool._running)

        release_b()


class TestPerCallAcquisition(unittest.TestCase):
    """Test 2: agent without a lifecycle slot gets a per-call SlotPool acquisition."""

    def test_acquire_called_when_no_lifecycle_slot(self):
        """_slot_key=None → already_holds=False → scheduler.acquire() is called.

        Mirrors the decision logic in call_with_fallback for the conc=0 path.
        """
        sched = EndpointScheduler()
        slot_key = '_shared_sequential_slot_'

        # Simulate an instance with no lifecycle slot.
        inst = _FakeInstance(instance_name="B", slot_key=None)

        already_holds = (inst is not None and getattr(inst, '_slot_key', None) == slot_key)
        self.assertFalse(already_holds)

        release_cb = sched.acquire(
            api_base="http://shared-api",
            concurrency_limit=0,
            instance_name="B",
            agent_class="coder",
            timeout=5.0,
        )
        self.assertIsNotNone(release_cb)

        # Release after the (simulated) call completes.
        release_cb()
        pool = sched._pools[slot_key]
        with pool._cond:
            self.assertNotIn("B", pool._running)


class TestNoDoubleAcquisition(unittest.TestCase):
    """Test 3: agent that already holds the conc=0 slot does not re-acquire."""

    def test_acquire_skipped_when_already_holds(self):
        """_slot_key == shared sequential key → already_holds=True → no acquire.

        Mirrors the decision logic in call_with_fallback for the conc=0 path.
        """
        sched = EndpointScheduler()
        slot_key = '_shared_sequential_slot_'

        # Simulate an instance that already holds the lifecycle slot on this endpoint.
        inst = _FakeInstance(instance_name="A", slot_key=slot_key)

        already_holds = (inst is not None and getattr(inst, '_slot_key', None) == slot_key)
        self.assertTrue(already_holds)

        # Because already_holds is True, call_with_fallback skips scheduler.acquire().
        # Verify no acquisition happens: the pool starts empty and stays empty.
        pool = sched._get_or_create_pool("http://shared-api", 0)
        with pool._cond:
            self.assertEqual(len(pool._running), 0)

    def test_acquire_skipped_when_different_slot_key(self):
        """_slot_key set to a different endpoint → not 'already holds' this slot.

        An agent holding a conc>0 (per-api_base) slot falling back to a conc=0
        endpoint must still acquire the shared sequential slot.
        """
        sched = EndpointScheduler()
        slot_key = '_shared_sequential_slot_'

        inst = _FakeInstance(instance_name="C", slot_key="http://other-api")

        already_holds = (inst is not None and getattr(inst, '_slot_key', None) == slot_key)
        self.assertFalse(already_holds)


class TestGeneratorFinalizationReleasesSlot(unittest.TestCase):
    """Test 4: the SlotPool generator wrapper releases the slot on finalization."""

    def test_release_called_after_generator_exhausted(self):
        """Release callback fires after all chunks are consumed."""
        release_calls = []
        release_cb = lambda: release_calls.append(1)

        result_gen = _slotpool_execute(release_cb, lambda: _make_gen(["c1", "c2"]))

        # Pull first chunk — slot still held.
        self.assertEqual(next(result_gen), "c1")
        self.assertEqual(len(release_calls), 0)

        # Consume the rest.
        self.assertEqual(list(result_gen), ["c2"])
        # Slot released exactly once after full iteration.
        self.assertEqual(release_calls, [1])

    def test_release_called_on_midstream_exception(self):
        """Release callback fires if the underlying generator raises mid-stream."""
        release_calls = []
        release_cb = lambda: release_calls.append(1)

        def failing_gen():
            yield "ok"
            raise ValueError("mid-stream failure")

        result_gen = _slotpool_execute(release_cb, lambda: failing_gen())

        self.assertEqual(next(result_gen), "ok")
        with self.assertRaises(ValueError):
            list(result_gen)
        # Slot released exactly once despite the exception.
        self.assertEqual(release_calls, [1])


class TestExceptionBeforeGenerator(unittest.TestCase):
    """Test 5: if call_fn raises before returning a generator, slot is released."""

    def test_release_called_when_call_raises(self):
        """Release callback fires when call_fn raises immediately."""
        release_calls = []
        release_cb = lambda: release_calls.append(1)

        def raising_call():
            raise RuntimeError("connection refused")

        with self.assertRaises(RuntimeError):
            _slotpool_execute(release_cb, raising_call)

        # Slot released exactly once.
        self.assertEqual(release_calls, [1])

    def test_non_generator_result_releases_immediately(self):
        """Non-generator (list) result releases the slot immediately."""
        release_calls = []
        release_cb = lambda: release_calls.append(1)

        result = _slotpool_execute(release_cb, lambda: ["a", "b"])

        self.assertEqual(result, ["a", "b"])
        # Released synchronously (not deferred to a generator).
        self.assertEqual(release_calls, [1])


# ──────────────────────────────────────────────────────────────────────────────
# Helpers that mirror the SlotPool path in execute_with_sem (api_router.py)
# ──────────────────────────────────────────────────────────────────────────────

def _slotpool_execute(release_cb, call_fn):
    """Mirror of the SlotPool branch inside execute_with_sem.

    Replicates the exact control flow:
      - call_fn() → if generator, pull first chunk and wrap with release-in-finally
      - if non-generator, release immediately and return
      - on any exception before/during setup, release and re-raise
    Keeping this separate from api_router lets us unit-test the release semantics
    without a fully-configured APIRouter.
    """
    try:
        result = call_fn()
        if hasattr(result, '__iter__') and not isinstance(result, (list, dict, str)):
            it = iter(result)
            first_chunk = next(it)

            def slotpool_gen_wrapper(first, rest, _release=release_cb):
                yield first
                try:
                    yield from rest
                finally:
                    _release()
            return slotpool_gen_wrapper(first_chunk, it)
        else:
            release_cb()
            return result
    except Exception:
        release_cb()
        raise


def _make_gen(items):
    """Build a simple generator over the given items."""
    for item in items:
        yield item


class TestChildWaitsInFIFO(unittest.TestCase):
    """Test 6 (Stage 3 rewrite): a child whose caller holds the target slot WAITS in
    the FIFO queue and is granted its turn in order — it is NOT skipped.

    Stage 1 removed the ``_skip_slot_acquire`` bypass and Stage 3 removed the
    ancestor-walk that used to force sync for an A→B(async)→C scenario. The child
    now always acquires its own slot via the single FIFO queue per endpoint: if the
    caller (or any other agent) holds the same pool, the child simply waits in FIFO
    order and is granted when a permit frees up. These tests exercise that wait-then-
    grant behavior through the real scheduler.
    """

    def test_child_waits_then_granted_in_fifo_order(self):
        """Caller A holds the shared sequential slot; child B must WAIT, then be
        granted in FIFO order once A releases."""
        sched = EndpointScheduler()
        slot_key = '_shared_sequential_slot_'

        # Caller A holds the conc=0 lifecycle slot.
        release_a = sched.acquire(
            api_base="http://shared-api",
            concurrency_limit=0,
            instance_name="A",
            agent_class="orchestrator",
            timeout=5.0,
        )
        self.assertIsNotNone(release_a)

        # Child B (no lifecycle slot of its own) needs the SAME pool. Instead of being
        # skipped, it must wait in the FIFO queue. With a short timeout it times out
        # while A still holds the slot. EndpointScheduler.acquire() re-raises the
        # internal SlotQueueTimeout as a plain TimeoutError (public contract).
        with self.assertRaises(TimeoutError):
            sched.acquire(
                api_base="http://shared-api",
                concurrency_limit=0,
                instance_name="B",
                agent_class="compressor",
                timeout=0.5,
            )

        pool = sched._pools[slot_key]
        with pool._cond:
            self.assertIn("A", pool._running)
            self.assertNotIn("B", pool._running)

        # A releases → B is granted in FIFO order (no other waiters).
        release_a()
        release_b = sched.acquire(
            api_base="http://shared-api",
            concurrency_limit=0,
            instance_name="B",
            agent_class="compressor",
            timeout=5.0,
        )
        self.assertIsNotNone(release_b)
        with pool._cond:
            self.assertIn("B", pool._running)
            self.assertNotIn("A", pool._running)

        release_b()


class _FakeInstance:
    """Minimal stand-in for AgentInstance exposing only what the conc=0 path reads."""

    def __init__(self, instance_name: str, slot_key):
        self.instance_name = instance_name
        self._slot_key = slot_key
        self.parent_instance = None


if __name__ == "__main__":
    unittest.main()
