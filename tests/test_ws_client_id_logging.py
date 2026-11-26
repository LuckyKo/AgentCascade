"""Tests for WebSocket client-identity logging.

Covers the logging-only change that attributes high-impact WS commands to the
originating client: ``WsMessageHandler`` stores a short ``client_id`` and
``dispatch`` logs high-level lifecycle/control commands with it.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_cascade import log as _log_mod
from agent_cascade.ws_handlers import WsMessageHandler


def _make_handler(client_id=None):
    """Build a minimal WsMessageHandler.

    ``client_id`` is only passed when provided, so the default-'' (backward
    compat) path is exercised too.
    """
    kwargs = dict(
        session={'generating': False, 'generation_id': 0, 'stop_requested': False},
        agent_pool=None,
        agents=[],
        send_queue=asyncio.Queue(),
        broadcast_fn=AsyncMock(),
        build_state_fn=lambda: {},
        start_gen_fn=lambda: None,
        session_lock=threading.Lock(),
        app=None,
    )
    if client_id is not None:
        kwargs['client_id'] = client_id
    return WsMessageHandler(**kwargs)


_HIGH_LEVEL_LOG_FMT = '[WS] high-level command %r from client %s'


def _high_level_calls(logger):
    """Return logger.info calls that carry the high-level-command format line."""
    return [c for c in logger.info.call_args_list if c.args and c.args[0] == _HIGH_LEVEL_LOG_FMT]


class TestClientIdStorage:
    def test_client_id_stored(self):
        assert _make_handler(client_id='abc123').client_id == 'abc123'

    def test_client_id_defaults_empty(self):
        # Existing test constructions (e.g. test_telemetry, test_stack_sessions_backend)
        # omit client_id — it must default to '' so they keep working.
        assert _make_handler().client_id == ''


class TestHighLevelCommandLogging:
    def test_restart_is_high_level(self):
        assert 'restart_server' in WsMessageHandler._HIGH_LEVEL_WS_COMMANDS

    def test_dispatch_high_level_command_logs_client_id(self, monkeypatch):
        """dispatch('restart_server') must route to the restart handler AND log with client id."""
        mock_logger = MagicMock()
        monkeypatch.setattr(_log_mod, 'logger', mock_logger)

        handler = _make_handler(client_id='abc123')
        restart = AsyncMock()
        monkeypatch.setattr(handler, 'handle_restart_server', restart)

        asyncio.run(handler.dispatch({'type': 'restart_server'}))

        # Routing preserved: the restart handler was invoked.
        restart.assert_called_once()
        # Attribution present: the high-level log carried the client id.
        calls = _high_level_calls(mock_logger)
        assert calls, 'expected a high-level command log line'
        assert calls[0].args[2] == 'abc123'

    def test_low_level_command_not_logged(self, monkeypatch):
        """The log must stay targeted — a non-high-level command routes without the extra line."""
        mock_logger = MagicMock()
        monkeypatch.setattr(_log_mod, 'logger', mock_logger)

        handler = _make_handler(client_id='abc123')
        select = AsyncMock()
        monkeypatch.setattr(handler, 'handle_select_agent', select)

        asyncio.run(handler.dispatch({'type': 'select_agent'}))

        select.assert_called_once()
        assert not _high_level_calls(mock_logger)
