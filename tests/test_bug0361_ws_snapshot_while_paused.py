"""Regression tests for BUG_0361 — WS "Save Now" (stacks_snapshot) blocked on paused session.

The ``_is_generating()`` guard in ``handle_stacks_snapshot`` must be
paused-aware: a paused pool sits at a clean turn boundary, so the snapshot
is safe.  The guard should only reject when generation is actively
in progress AND the pool is NOT paused.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, patch

import pytest

from agent_cascade.ws_handlers import WsMessageHandler


def _make_handler(generating: bool, paused: bool):
    """Build a minimal WsMessageHandler with controlled generating/paused state."""
    pool = type('Pool', (), {'is_paused': lambda self: paused})()
    handler = WsMessageHandler(
        session={'generating': generating, 'generation_id': 0, 'stop_requested': False},
        agent_pool=pool,
        agents=[],
        send_queue=asyncio.Queue(),
        broadcast_fn=AsyncMock(),
        build_state_fn=lambda: {},
        start_gen_fn=lambda: None,
        session_lock=threading.Lock(),
        app=None,
    )
    return handler


@pytest.fixture(autouse=True)
def _enable_stack_swap(monkeypatch):
    import agent_cascade.settings as s
    monkeypatch.setattr(s, 'ENABLE_STACK_SWAP', True)


class TestSnapshotWhilePaused:
    """BUG_0360: snapshot must succeed when pool is paused (even with generating=True)."""

    def test_snapshot_allowed_when_paused(self):
        handler = _make_handler(generating=True, paused=True)
        with patch('agent_cascade.snapshot.snapshot_stack', return_value={'stack_id': 'test'}) as mock_snap, \
             patch.object(handler, '_broadcast_stacks', new_callable=AsyncMock):
            asyncio.run(handler.handle_stacks_snapshot({}))
            mock_snap.assert_called_once()
        # broadcast should carry STACKS_RESULT, not the "Cannot take snapshot" warning
        broadcasted = handler.broadcast_fn.call_args[0][0]
        assert 'Cannot take snapshot' not in broadcasted.get('message', '')

    def test_snapshot_blocked_when_active(self):
        """Companion: guard still rejects when generating=True AND pool is NOT paused."""
        handler = _make_handler(generating=True, paused=False)
        with patch('agent_cascade.snapshot.snapshot_stack') as mock_snap, \
             patch.object(handler, '_broadcast_stacks', new_callable=AsyncMock):
            asyncio.run(handler.handle_stacks_snapshot({}))
            mock_snap.assert_not_called()
        broadcasted = handler.broadcast_fn.call_args[0][0]
        assert 'Cannot take snapshot' in broadcasted.get('message', '')
