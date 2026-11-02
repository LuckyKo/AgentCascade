"""A fresh WebSocket connect must perform ZERO settings persists (Batch B, Phase 3).

The backend is authoritative: a connecting client only READS the settings snapshot it is
delivered on connect (``pool_settings`` + ``settings_revision``); it never writes. This test
spies on every settings persist path — ``pool._save_pool_settings``,
``api_router.update_default_llm_cfg`` (the LLM bucket), and ``save_session_name`` — and
asserts a fresh connect changes none of them.

Regression guard against the historical ``update_config`` / ``set_session_name`` connect-time
full-push, which is removed in Phase 2/3 (the UI no longer pushes settings on connect).
"""
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.absolute()
import sys
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.agent_instance import PoolSettings


class _RecordingRouter:
    def __init__(self):
        self.default_llm_cfg = {'model': 'm'}
        self.llm_updates = 0

    def update_default_llm_cfg(self, cfg):
        self.llm_updates += 1
        self.default_llm_cfg.update(cfg)


class _NoWritePool:
    """Pool stand-in that records every settings persist (all counters must stay at 0)."""

    def __init__(self):
        self.instances = {}
        self.templates = {}
        self.stopped = False
        self.settings = PoolSettings()
        self.telemetry = None
        self.operation_manager = None
        self.llm_cfg = {}
        self.api_router = _RecordingRouter()
        self._ui_disabled_tools = {}
        self._ui_disabled_tools_lock = threading.Lock()
        self.auto_security = False
        self.save_calls = 0

    def get_instance(self, name):
        return None

    def has_messages(self, name):
        return False

    def get_queue_messages(self, name):
        return []

    def has_pending(self, name):
        return False

    def is_paused(self):
        return False

    def is_instance_halted(self, name):
        return False

    def set_auto_security(self, enabled):
        self.auto_security = bool(enabled)

    def _save_pool_settings(self):
        self.save_calls += 1


def _counters(pool, session_saves):
    return (pool.save_calls, pool.api_router.llm_updates, len(session_saves))


def test_fresh_ws_connect_performs_zero_settings_persists(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_cascade.api_server import create_app
    import agent_cascade.session_state_store as ss

    # Isolate the session-state file and count its saves.
    session_saves = []
    monkeypatch.setattr(ss, '_session_state_path', lambda: tmp_path / 'session_state.json')
    real_save = ss.save_session_name

    def _counting_save(name):
        session_saves.append(name)
        return real_save(name)

    monkeypatch.setattr(ss, 'save_session_name', _counting_save)

    pool = _NoWritePool()
    app = create_app(agents=[], agent_pool=pool, config={'session_name': 'NoWriteSession'})
    client = TestClient(app)

    # Trigger the app lifespan (startup) so any startup-time persistence happens BEFORE the
    # baseline; the connect itself is what must be write-free.
    client.get('/api/settings')
    baseline = _counters(pool, session_saves)

    with client.websocket_connect('/ws/chat') as ws:
        initial = ws.receive_json()
        # The connect delivers a full settings snapshot so the UI can re-hydrate.
        assert 'pool_settings' in initial or 'settings_revision' in initial

    assert _counters(pool, session_saves) == baseline, (
        f"connect persisted settings: baseline={baseline}, after={_counters(pool, session_saves)}")


def test_fresh_ws_connect_delivers_settings_snapshot(tmp_path, monkeypatch):
    """The reconnect path must carry a fresh full snapshot (pool_settings + revision)."""
    from fastapi.testclient import TestClient
    from agent_cascade.api_server import create_app
    import agent_cascade.session_state_store as ss

    monkeypatch.setattr(ss, '_session_state_path', lambda: tmp_path / 'session_state.json')

    pool = _NoWritePool()
    app = create_app(agents=[], agent_pool=pool, config={'session_name': 'SnapSession'})
    client = TestClient(app)
    client.get('/api/settings')

    with client.websocket_connect('/ws/chat') as ws:
        initial = ws.receive_json()

    # The state frame carries the pool_settings snapshot and the current settings revision,
    # so a reconnecting client can replace pendingSettings and re-hydrate.
    assert 'pool_settings' in initial
    assert isinstance(initial.get('settings_revision'), int)
