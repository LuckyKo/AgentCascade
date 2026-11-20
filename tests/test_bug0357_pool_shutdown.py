"""``AgentPool.shutdown()``: deterministic ordered stop+join of every pool background
thread + ZMQ-client teardown, before interpreter/GC teardown.

The cure adds a NEW idempotent ``AgentPool.shutdown()`` that stops+joins the work
threads (idle-checker, candidate-eval, memory-hint, async executor) and then drains
the ZMQ kernel clients + terms the owned ``zmq.Context`` LAST. These tests pin that
ordering and the bounded / null-safe / swallow-raises contract using fakes + mocks
(no real threads, no real zmq context) so they run instantly and side-effect-free.
"""

import threading
from unittest.mock import MagicMock, patch

from agent_cascade.pool.core import AgentPool


class _FakeThread(threading.Thread):
    """A real ``threading.Thread`` (so the ``isinstance`` guards in ``shutdown()``
    pass) that reports ``is_alive() == True`` and records ``join()`` calls, without
    ever being started.

    Pass ``order`` to record each join into a shared list (for the ordering test).
    """

    def __init__(self, *args, order=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.joined = False
        self._order = order

    def is_alive(self):
        return True

    def join(self, timeout=None):
        self.joined = True
        if self._order is not None:
            self._order.append(self.name)


def _make_pool(order=None):
    """Build an ``AgentPool`` WITHOUT running the heavy ``__init__``, then attach the
    exact attributes ``shutdown()`` operates on (all fakes/mocks)."""
    pool = object.__new__(AgentPool)
    pool._idle = MagicMock(name='idle')
    pool._candidate_eval_stop = MagicMock(name='stop_event')
    pool._candidate_eval_thread = _FakeThread(name='skill-candidate-eval', order=order)
    mhm = MagicMock(name='memory_hint_manager')
    mhm._worker = _FakeThread(name='memory-hint-worker', order=order)
    pool.memory_hint_manager = mhm
    registry = MagicMock(name='async_registry')
    registry._executor = MagicMock(name='executor')
    registry._executor._threads = {_FakeThread(name='async_tool', order=order)}
    pool._async_registry = registry
    pool.instances = {'Maine': object()}
    return pool


def test_shutdown_stops_joins_all_threads_and_terms_zmq_last():
    """Every work thread is stop+joined, the ZMQ context is drained+termed LAST, and the
    whole sequence is ordered (work threads FIRST, ZMQ LAST)."""
    order = []
    pool = _make_pool(order)

    # Record the relative order of the non-thread teardown steps via side_effects.
    pool._idle.stop_and_join.side_effect = lambda **k: order.append('idle')
    pool._candidate_eval_stop.set.side_effect = lambda: order.append('cand_signal')
    pool.memory_hint_manager.stop.side_effect = lambda: order.append('hint_stop')
    pool._async_registry.shutdown.side_effect = lambda **k: order.append('async_shutdown')

    with patch('agent_cascade.pool.core.cleanup_kernels_for_session') as cleanup:
        cleanup.side_effect = lambda name: order.append('zmq')
        pool.shutdown()

    # (1) idle-checker: bounded stop+join (the previously-dead stop_and_join)
    pool._idle.stop_and_join.assert_called_once_with(timeout=2.0)
    # (2) candidate-eval: signal then bounded-join
    pool._candidate_eval_stop.set.assert_called_once()
    assert pool._candidate_eval_thread.joined is True
    # (3) memory-hint: sentinel stop then bounded-join
    pool.memory_hint_manager.stop.assert_called_once()
    assert pool.memory_hint_manager._worker.joined is True
    # (4) async executor: quick stop with cancel_futures, then join each worker
    pool._async_registry.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    async_worker = next(iter(pool._async_registry._executor._threads))
    assert async_worker.joined is True
    # (5) ZMQ clients drained + context term'd LAST
    cleanup.assert_called_once_with('Maine')

    # Ordering: idle first, ZMQ last, and the thread joins nest before the ZMQ step.
    assert order[0] == 'idle'
    assert order[-1] == 'zmq'
    assert order.index('skill-candidate-eval') < order.index('memory-hint-worker') \
        < order.index('async_tool') < order.index('zmq')


def test_shutdown_is_idempotent():
    """A second ``shutdown()`` call is a no-op (no repeated stops/joins/cleanups)."""
    pool = _make_pool()
    with patch('agent_cascade.pool.core.cleanup_kernels_for_session') as cleanup:
        pool.shutdown()
        idle_calls = pool._idle.stop_and_join.call_count
        cand_signal_calls = pool._candidate_eval_stop.set.call_count
        cleanup_calls = cleanup.call_count
        pool.shutdown()  # second call — must be a no-op
    assert pool._idle.stop_and_join.call_count == idle_calls
    assert pool._candidate_eval_stop.set.call_count == cand_signal_calls
    assert cleanup.call_count == cleanup_calls
    assert pool._shutdown_done is True


def test_shutdown_swallows_raising_stop_and_continues():
    """A stop/join that raises is swallowed (never breaks teardown); the remaining
    steps still run, and the ZMQ drain still happens LAST."""
    pool = _make_pool()
    pool._idle.stop_and_join.side_effect = RuntimeError('idle boom')
    pool.memory_hint_manager.stop.side_effect = RuntimeError('hint boom')
    pool._async_registry.shutdown.side_effect = RuntimeError('async boom')

    with patch('agent_cascade.pool.core.cleanup_kernels_for_session') as cleanup:
        pool.shutdown()  # must NOT raise

    # Even with the idle/hint/async stops raising, the candidate-eval join ran and the
    # ZMQ drain still fired (ordering preserved).
    assert pool._candidate_eval_thread.joined is True
    cleanup.assert_called_once_with('Maine')
    assert pool._shutdown_done is True
