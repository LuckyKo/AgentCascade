"""Dead-man's-switch tests for the slot queue (plan todo163, §5).

Covers the sliding activity window + hard cap that replace the old fixed
wall-clock deadline, plus the additive ``progress_ts`` context key, the
``SlotQueueTimeout.reason`` payload, the ``_queue_limits`` live-read, the
state-builder emission, and the config-handler range validation.

All timing tests use SMALL windows (a couple of seconds) so the suite stays
fast — the plan's 300s/1800s figures are the production values, not the test
values. The acquire tick is 1.0s, so a window of N seconds fires in ~N to
~N+2 seconds.

Concurrency discipline (mirrors the slot-acquire-blocking skill): the main
thread NEVER blocks on a full pool. The holder is granted on the main thread
(fast path, capacity available), and the WAITER is driven on a background
thread whose result is observed via a box. Every wait is bounded by a deadline.
"""

import threading
import time
import unittest
from typing import Any, Dict

from agent_cascade.slot_queue import (
    SlotPool,
    SlotQueueTimeout,
    _holder_activity_context,
)


# ──────────────────────────────────────────────────────────────────────────────
# Test doubles
# ──────────────────────────────────────────────────────────────────────────────


class _FakeInst:
    """Minimal stand-in for an AgentInstance: only the two activity stamps that
    the dead-man's switch reads (plus ``state`` for the reason string)."""

    def __init__(self, last_activity: float, llm_activity: float = 0.0):
        self.last_activity = last_activity
        self._last_llm_activity = llm_activity
        self._llm_call_active = False

    def advance(self, ts: float) -> None:
        self.last_activity = ts


def _grant_holder(pool: SlotPool, name: str = 'H'):
    """Grant the single permit on the main thread (fast path)."""
    return pool.acquire(instance_name=name, agent_class='t')


def _start_waiter(pool: SlotPool, name: str, timeout: float, hard_cap: float,
                  resolver, box: Dict[str, Any]) -> threading.Thread:
    """Start ONE blocking acquire on a background thread; record outcome in box.

    Does NOT join — the caller decides when to join. For the healthy-holder tests the
    waiter cannot finish until the holder releases, so joining first would deadlock the
    test and (worse) prevent the activity stepper from ever running during the wait.
    """

    def _w():
        try:
            rel = pool.acquire(instance_name=name, agent_class='t',
                                timeout=timeout, hard_cap=hard_cap,
                                instance_resolver=resolver)
            box['rel'] = rel
        except Exception as e:  # noqa: BLE001 — record, don't mask
            box['exc'] = e

    t = threading.Thread(target=_w, daemon=True)
    t.start()
    return t


def _join(t: threading.Thread, timeout: float = 15.0) -> None:
    t.join(timeout=timeout)



# ──────────────────────────────────────────────────────────────────────────────
# D1 — Healthy holder resets → waiter succeeds past the window
# ──────────────────────────────────────────────────────────────────────────────


class TestHealthyHolderResets(unittest.TestCase):
    def test_waiter_succeeds_past_window(self):
        """D1 (THE incident regression test): a healthy holder that keeps advancing
        activity must NOT trip the window, so the waiter is granted long after the
        window would have fired under the old fixed-deadline code."""
        pool = SlotPool(key='d1', capacity=1)
        holder = _grant_holder(pool)
        inst = _FakeInst(time.monotonic())
        resolver = lambda n: inst

        box: Dict[str, Any] = {}
        # Window is 3s; we keep the holder alive for ~5s (> window) then release.
        wt = _start_waiter(pool, 'W', timeout=3.0, hard_cap=60.0, resolver=resolver, box=box)

        stop = threading.Event()

        def _stepper():
            while not stop.is_set():
                inst.advance(time.monotonic())
                time.sleep(0.3)

        st = threading.Thread(target=_stepper, daemon=True)
        st.start()
        try:
            # Outlive the 3s window several times while the holder is alive.
            time.sleep(5.0)
            self.assertNotIn('exc', box, f"healthy holder killed the waiter: {box.get('exc')!r}")
            # Now release the holder — the waiter must be granted.
            holder()
            deadline = time.monotonic() + 5.0
            while 'rel' not in box and 'exc' not in box and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            stop.set()
            st.join(timeout=5)
            _join(wt)

        self.assertIn('rel', box, f"waiter was never granted: {box!r}")
        box['rel']()

    def test_stuck_holder_fails_at_window_with_reason(self):
        """D2: a holder whose stamps never advance fails the waiter at ~window,
        with a reason that names the holder and the pool key."""
        pool = SlotPool(key='d2', capacity=1)
        holder = _grant_holder(pool, 'HOLDME')
        # Stuck: last_activity frozen in the past.
        inst = _FakeInst(time.monotonic() - 100.0)
        resolver = lambda n: inst

        box: Dict[str, Any] = {}
        t0 = time.monotonic()
        _join(_start_waiter(pool, 'W', timeout=2.0, hard_cap=60.0, resolver=resolver, box=box))
        elapsed = time.monotonic() - t0

        self.assertIn('exc', box, f"stuck holder should have failed the waiter: {box!r}")
        exc = box['exc']
        self.assertIsInstance(exc, SlotQueueTimeout)
        # Failed in the window band, not the hard cap.
        self.assertGreaterEqual(elapsed, 2.0)
        self.assertLess(elapsed, 8.0, f"failed too late ({elapsed:.1f}s) — cap path?")
        # Real reason: holder name + pool key present.
        msg = str(exc)
        self.assertIn('HOLDME', msg, f"reason missing holder name: {msg}")
        self.assertIn('d2', msg, f"reason missing pool key: {msg}")
        self.assertIn('no progress', msg, f"reason not the fail-fast path: {msg}")
        # Ticket removed from the queue.
        self.assertEqual(len(pool._waiters), 0, 'ticket not removed after timeout')
        # And the permit is untouched — the holder still owns it.
        self.assertIn('HOLDME', pool._running)
        holder()


# ──────────────────────────────────────────────────────────────────────────────
# D3 — Hard cap & ordering (parametrized)
# ──────────────────────────────────────────────────────────────────────────────


class TestHardCapAndOrdering(unittest.TestCase):
    def test_window_fires_when_detection_broken(self):
        """D3(a) CORRECTED: when the resolver returns None, progress_ts stays 0.0, so
        the window can NEVER reset — the WAITER fails at the WINDOW, not the cap.

        (The plan's original D3(a) expected the cap to fire here; that is not how the
        sliding window works. A broken detector means no resets, so the window is the
        first deadline to elapse. The cap is the safety net for the OPPOSITE case — a
        holder that keeps resetting the window (D3(b)). Flagged to the planner.)"""
        pool = SlotPool(key='d3a', capacity=1)
        holder = _grant_holder(pool, 'H')
        box: Dict[str, Any] = {}
        t0 = time.monotonic()
        # window=2, hard_cap=4: no activity → the window (2s) fires, not the cap.
        _join(_start_waiter(pool, 'W', timeout=2.0, hard_cap=4.0, resolver=lambda n: None, box=box))
        elapsed = time.monotonic() - t0
        self.assertIn('exc', box, f"expected timeout: {box!r}")
        self.assertIsInstance(box['exc'], SlotQueueTimeout)
        self.assertGreaterEqual(elapsed, 2.0)
        self.assertLess(elapsed, 8.0, f"failed at the cap, not the window: {elapsed:.1f}s")
        self.assertIn('no progress', str(box['exc']), f"not the window path: {str(box['exc'])}")
        holder()

    def test_cap_bounds_pathologically_active_holder(self):
        """D3(b): a holder that keeps advancing activity must STILL be bounded by the
        cap — the window keeps resetting, but the cap never slides. This also proves
        the load-bearing ORDERING: the cap (①) is checked before the reset (②), so an
        always-active holder cannot extend the wait past the cap."""
        pool = SlotPool(key='d3b', capacity=1)
        holder = _grant_holder(pool, 'H')
        inst = _FakeInst(time.monotonic())
        resolver = lambda n: inst
        box: Dict[str, Any] = {}
        t0 = time.monotonic()
        wt = _start_waiter(pool, 'W', timeout=2.0, hard_cap=4.0, resolver=resolver, box=box)
        stop = threading.Event()

        def _stepper():
            while not stop.is_set():
                inst.advance(time.monotonic())
                time.sleep(0.2)

        st = threading.Thread(target=_stepper, daemon=True)
        st.start()
        try:
            _join(wt)
        finally:
            stop.set()
            st.join(timeout=5)
        elapsed = time.monotonic() - t0
        self.assertIn('exc', box, f"expected timeout: {box!r}")
        self.assertIsInstance(box['exc'], SlotQueueTimeout)
        self.assertGreaterEqual(elapsed, 4.0, f"cap did not bound an active holder: {elapsed:.1f}s")
        self.assertLess(elapsed, 9.0)
        self.assertIn('hard cap', str(box['exc']), f"not the cap path: {str(box['exc'])}")
        holder()


    def test_window_honoured_not_cap(self):
        """D3(c): a stuck holder with a generous cap fails at the WINDOW, not the cap."""
        pool = SlotPool(key='d3c', capacity=1)
        holder = _grant_holder(pool, 'H')
        inst = _FakeInst(time.monotonic() - 100.0)
        resolver = lambda n: inst
        box: Dict[str, Any] = {}
        t0 = time.monotonic()
        _join(_start_waiter(pool, 'W', timeout=2.0, hard_cap=60.0, resolver=resolver, box=box))
        elapsed = time.monotonic() - t0
        self.assertIn('exc', box, f"expected timeout: {box!r}")
        self.assertGreaterEqual(elapsed, 2.0)
        self.assertLess(elapsed, 8.0, f"failed at the cap, not the window: {elapsed:.1f}s")
        self.assertIn('no progress', str(box['exc']), f"not the window path: {str(box['exc'])}")
        holder()


# ──────────────────────────────────────────────────────────────────────────────
# D6 — Reset precedes fail-fast
# ──────────────────────────────────────────────────────────────────────────────


class TestResetPrecedesFailFast(unittest.TestCase):
    def test_no_timeout_on_first_progress_tick(self):
        """D6: a holder that becomes active shortly before the window deadline must NOT
        be killed on the tick its progress first becomes visible — the reset runs before
        the fail-fast check."""
        pool = SlotPool(key='d6', capacity=1)
        holder = _grant_holder(pool, 'H')
        inst = _FakeInst(time.monotonic())  # fresh from the start
        resolver = lambda n: inst
        box: Dict[str, Any] = {}
        wt = _start_waiter(pool, 'W', timeout=3.0, hard_cap=60.0, resolver=resolver, box=box)
        stop = threading.Event()

        def _stepper():
            while not stop.is_set():
                inst.advance(time.monotonic())
                time.sleep(0.4)

        st = threading.Thread(target=_stepper, daemon=True)
        st.start()
        try:
            # Outlive the 3s window; a healthy holder must survive.
            time.sleep(4.5)
            self.assertNotIn('exc', box, f"reset did not precede fail-fast: {box.get('exc')!r}")
            holder()
            deadline = time.monotonic() + 5.0
            while 'rel' not in box and 'exc' not in box and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            stop.set()
            st.join(timeout=5)
            _join(wt)
        self.assertIn('rel', box, f"waiter never granted: {box!r}")
        box['rel']()


# ──────────────────────────────────────────────────────────────────────────────
# D7 — progress_ts is the fresher of the two (additive guarantee)
# ──────────────────────────────────────────────────────────────────────────────


class TestProgressTs(unittest.TestCase):
    def _ctx(self, la: float, llm: float, now: float) -> Dict[str, Any]:
        pool = SlotPool(key='d7', capacity=1)
        # A holder must exist for _holder_activity_context to resolve.
        pool.acquire(instance_name='H', agent_class='t')
        inst = _FakeInst(la, llm)
        return _holder_activity_context(pool, now, resolver=lambda n: inst)

    def test_progress_ts_is_fresher_of_two(self):
        """D7: progress_ts = max(last_activity, _last_llm_activity) in BOTH directions."""
        now = time.monotonic()
        # Fresh last_activity, stale llm.
        ctx = self._ctx(la=now, llm=now - 50.0, now=now)
        self.assertAlmostEqual(ctx['progress_ts'], now, delta=0.5)
        # Fresh llm, stale last_activity — inverse.
        ctx = self._ctx(la=now - 50.0, llm=now, now=now)
        self.assertAlmostEqual(ctx['progress_ts'], now, delta=0.5)

    def test_additive_keys_untouched(self):
        """D7 (additive-form guarantee): last_activity_age_s and streaming are computed
        from _last_llm_activity ONLY — the new progress_ts key must not perturb them.
        A stale last_activity must not widen streaming / shrink the LLM age."""
        now = time.monotonic()
        # llm activity fresh → streaming True (llm active + within ACTIVE_S window).
        pool = SlotPool(key='d7b', capacity=1)
        pool.acquire(instance_name='H', agent_class='t')
        inst = _FakeInst(now - 500.0, now)
        inst._llm_call_active = True
        ctx = _holder_activity_context(pool, now, resolver=lambda n: inst)
        self.assertTrue(ctx['streaming'], 'streaming should be True for a fresh LLM stamp')
        self.assertGreaterEqual(ctx['last_activity_age_s'], 0.0)
        self.assertLessEqual(ctx['last_activity_age_s'], 1.0,
                             'last_activity_age_s must be derived from the LLM stamp only')
        # progress_ts is the fresher (last_activity is stale, llm fresh).
        self.assertAlmostEqual(ctx['progress_ts'], now, delta=0.5)


# ──────────────────────────────────────────────────────────────────────────────
# D4 — Window respects the setting, read live
# ──────────────────────────────────────────────────────────────────────────────


class TestQueueLimitsLiveRead(unittest.TestCase):
    def test_queue_limits_reads_setting_live(self):
        """D4: _queue_limits reads pool.settings at CALL time and derives the cap as
        max(6×window, 60). Changing the setting between calls changes the result —
        the guard against the 'read at import time' class (gotcha #10)."""
        from agent_cascade.pool.slots import _queue_limits
        from agent_cascade.agent_instance import PoolSettings

        class _P:
            def __init__(self):
                self.settings = PoolSettings()

        p = _P()
        p.settings.slot_queue_timeout_seconds = 30
        window, cap = _queue_limits(p)
        self.assertEqual(window, 30.0)
        self.assertEqual(cap, 180.0)  # 6 × 30

        # Change the setting → the NEXT call must see it (no import-time caching).
        p.settings.slot_queue_timeout_seconds = 60
        window, cap = _queue_limits(p)
        self.assertEqual(window, 60.0)
        self.assertEqual(cap, 360.0)

    def test_queue_limits_floor(self):
        """Cap floor: a tiny window still yields a >= 60s cap."""
        from agent_cascade.pool.slots import _queue_limits
        from agent_cascade.agent_instance import PoolSettings

        class _P:
            def __init__(self):
                self.settings = PoolSettings()

        p = _P()
        p.settings.slot_queue_timeout_seconds = 30
        window, cap = _queue_limits(p)
        self.assertGreaterEqual(cap, 60.0)

    def test_queue_limits_falls_back_to_constant(self):
        """When no settings object is reachable, fall back to the module constants —
        this is what the monkeypatching stress/e2e harnesses depend on."""
        from agent_cascade.pool.slots import _queue_limits
        import agent_cascade.slot_queue as _sq

        class _NoSettings:
            pass

        w, cap = _queue_limits(_NoSettings())
        self.assertEqual(w, float(_sq.QUEUE_WAIT_TIMEOUT))
        self.assertEqual(cap, _sq.SLOT_QUEUE_HARD_CAP_DEFAULT)


# ──────────────────────────────────────────────────────────────────────────────
# D8 — Both state-builder blocks emit the key
# ──────────────────────────────────────────────────────────────────────────────


class TestStateBuilderEmitsKey(unittest.TestCase):
    def _pool(self, tmp_dir: str):
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.agent_instance import AgentInstance

        DUMMY = {'model': 'mock', 'api_base': 'http://127.0.0.1:1/v1',
                 'model_server': 'http://127.0.0.1:1/v1', 'api_key': 'EMPTY'}
        pool = AgentPool(DUMMY)
        name = f"D8_{uuid.uuid4().hex[:8]}"
        inst = AgentInstance(instance_name=name, agent_class='coder',
                             conversation=[], created_at=time.monotonic(),
                             last_activity=time.monotonic(), latest_marker_index=0)
        inst.compression_summary = ''
        pool.instances[name] = inst
        return pool, name

    def test_both_blocks_emit_key(self):
        """D8: 'slot_queue_timeout_seconds' appears in the pool_settings of BOTH
        build_state_from_pool and build_stream_update_from_pool. A test on only the
        first would pass while the UI reverts the value on every stream tick."""
        import tempfile
        from agent_cascade.api_integration_pkg.state_builder import (
            build_state_from_pool,
            build_stream_update_from_pool,
        )

        with tempfile.TemporaryDirectory() as tmp:
            pool, name = self._pool(tmp)
            # Set a non-default value so the assertion is meaningful.
            pool.settings.slot_queue_timeout_seconds = 420

            state = build_state_from_pool(pool, name)
            self.assertIsNotNone(state, 'build_state_from_pool returned None')
            self.assertIn('slot_queue_timeout_seconds', state['pool_settings'],
                          f"state block missing key: {state['pool_settings'].keys()}")
            self.assertEqual(state['pool_settings']['slot_queue_timeout_seconds'], 420)

            stream = build_stream_update_from_pool(pool, name)
            self.assertIsNotNone(stream, 'build_stream_update_from_pool returned None')
            self.assertIn('slot_queue_timeout_seconds', stream['pool_settings'],
                          f"stream block missing key: {stream['pool_settings'].keys()}")
            self.assertEqual(stream['pool_settings']['slot_queue_timeout_seconds'], 420)


# ──────────────────────────────────────────────────────────────────────────────
# D9 — Handler range validation
# ──────────────────────────────────────────────────────────────────────────────


class TestHandlerRangeValidation(unittest.TestCase):
    def _handler(self):
        from agent_cascade import config_handlers as ch
        return ch._handle_slot_queue_timeout_seconds

    def _pool(self):
        from agent_cascade.agent_instance import PoolSettings

        class _P:
            def __init__(self):
                self.settings = PoolSettings()
        return _P()

    def test_out_of_range_rejected_and_state_unchanged(self):
        """D9: values below 30 or above 7200 are rejected and pool.settings is
        UNCHANGED (the handler's raise is swallowed upstream, so the test must
        verify state, not just that an exception was raised)."""
        h = self._handler()
        pool = self._pool()
        pool.settings.slot_queue_timeout_seconds = 300
        for bad in (1, 29, 7201, 99999):
            with self.assertRaises(ValueError):
                h({'slot_queue_timeout_seconds': bad}, pool, [])
            self.assertEqual(pool.settings.slot_queue_timeout_seconds, 300,
                             f"state mutated after rejecting {bad}")

    def test_in_range_accepted(self):
        """D9: 30, 300, 7200 are accepted and written to pool.settings."""
        h = self._handler()
        pool = self._pool()
        for good in (30, 300, 7200):
            h({'slot_queue_timeout_seconds': good}, pool, [])
            self.assertEqual(pool.settings.slot_queue_timeout_seconds, good)


# ──────────────────────────────────────────────────────────────────────────────
# D11 — Lock-ordering regression
# ──────────────────────────────────────────────────────────────────────────────


class TestLockOrderingRegression(unittest.TestCase):
    def test_no_deadlock_while_holder_mutates(self):
        """D11: a waiter blocked in acquire while a holder thread repeatedly acquires a
        separate _state_lock-guarded region must complete without a lock-order
        inversion (no 'cannot release un-acquired lock', no deadlock)."""
        pool = SlotPool(key='d11', capacity=1)
        holder = _grant_holder(pool, 'H')
        inst = _FakeInst(time.monotonic() - 100.0)  # stuck → waiter times out cleanly

        state_lock = threading.Lock()
        stop = threading.Event()

        def _holder_mutate():
            # Contend on a lock that the (real) instance would guard. The resolver
            # acquires it while pool._cond is released — this is the exact ordering
            # the release()/acquire() dance is designed to keep safe.
            while not stop.is_set():
                with state_lock:
                    time.sleep(0.02)

        mut = threading.Thread(target=_holder_mutate, daemon=True)
        mut.start()
        box: Dict[str, Any] = {}
        try:
            # Resolver takes the contended lock — exercises the released-section path.
            def _resolver(n):
                with state_lock:
                    return inst
            _join(_start_waiter(pool, 'W', timeout=2.0, hard_cap=60.0,
                        resolver=_resolver, box=box))
        finally:
            stop.set()
            mut.join(timeout=5)
        self.assertIn('exc', box, f"expected a clean timeout: {box!r}")
        self.assertIsInstance(box['exc'], SlotQueueTimeout)
        self.assertNotIn('cannot release', str(box['exc']).lower())
        holder()


if __name__ == '__main__':
    unittest.main(verbosity=2)
