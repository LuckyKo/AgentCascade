import asyncio
import logging
import threading
import pytest

from agent_cascade.api_integration_pkg.streaming import _put_stream_update
from agent_cascade.slot_queue import SlotPool
from agent_cascade.pool.message_queue import MessageQueueMixin


class MockAgentPool(MessageQueueMixin):
    def __init__(self):
        self.message_queues = {}
        self._queue_lock = threading.Lock()
        self._message_condition = threading.Condition(self._queue_lock)

    def _mark_activity(self, instance_name: str):
        pass


@pytest.mark.asyncio
async def test_ws_send_queue_high_watermark_warning(caplog):
    queue = asyncio.Queue(maxsize=10)
    # Fill up to 7 items (below 75%)
    for i in range(7):
        queue.put_nowait({'tick': i})

    with caplog.at_level(logging.WARNING):
        # 8th item reaches 8/10 (80% >= 75%)
        await _put_stream_update(queue, {'tick': 8})
        assert any("WS send queue high-watermark reached" in rec.message for rec in caplog.records)


def test_slot_pool_contention_warning(caplog):
    pool = SlotPool("test_provider", capacity=1)
    # First acquire succeeds immediately without warning
    release1 = pool.acquire("Agent1", "Worker")

    with caplog.at_level(logging.WARNING):
        # Second acquire cannot succeed immediately (capacity=1) -> enqueued as waiter
        # Spawn thread for second acquire so it blocks
        release_holder = []
        def acquire_second():
            release_holder.append(pool.acquire("Agent2", "Worker", timeout=2.0))

        t = threading.Thread(target=acquire_second)
        t.start()
        t.join(timeout=0.2)  # Wait for it to enqueue and log

        assert any("Slot contention on 'test_provider'" in rec.message for rec in caplog.records)

        # Release first slot so second can complete cleanly
        release1()
        t.join(timeout=1.0)
        if release_holder:
            release_holder[0]()


def test_message_queue_depth_warning(caplog):
    mock_pool = MockAgentPool()

    with caplog.at_level(logging.WARNING):
        for i in range(4):
            mock_pool.enqueue_message("Maine", f"msg_{i}")
        assert not any("message queue depth high" in rec.message for rec in caplog.records)

        # 5th message triggers warning
        mock_pool.enqueue_message("Maine", "msg_4")
        assert any("Instance 'Maine' message queue depth high: 5 messages pending execution" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_ws_send_queue_full_purge_and_resync(caplog):
    from agent_cascade.api_integration_pkg.streaming import _last_force_full, _last_force_full_lock
    queue = asyncio.Queue(maxsize=5)

    # Set existing force_full timestamp
    with _last_force_full_lock:
        _last_force_full['Maine'] = 999999.0

    # Fill queue with 4 stream_updates and 1 critical done event
    for i in range(4):
        queue.put_nowait({'type': 'stream_update', 'instance': 'Maine', 'tick': i})
    queue.put_nowait({'type': 'done', 'final': True})

    assert queue.full()

    with caplog.at_level(logging.WARNING):
        # Putting 6th event raises QueueFull -> triggers purge of stale stream_updates and timer reset
        await _put_stream_update(queue, {'type': 'stream_update', 'instance': 'Maine', 'tick': 5})

        # Check that log warned about purge and resync
        assert any("purged 4 stale stream_update delta(s)" in rec.message for rec in caplog.records)

        # Done event was preserved
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())

        types = [it.get('type') for it in items]
        assert 'done' in types
        # Current event was enqueued
        assert any(it.get('tick') == 5 for it in items)

        # Force full timer for Maine was cleared
        with _last_force_full_lock:
            assert 'Maine' not in _last_force_full

