"""Regression tests for BUG_0142 — WS idle-status heartbeat.

The frontend's ``state.generating`` is a one-way latch: it is set true by every
``stream_update`` and only lowered by a full ``state``/``done`` frame (or ``error``).
When the closing ``done`` frame never reaches the client the UI stayed on
"Generating..." until a page refresh, blocking new input.

The server-side fix has three parts, each covered here:

1. ``build_instance_status_from_pool`` — cheap per-instance liveness snapshot
   (``active`` / ``agent_state`` / ``is_halted``) with no message history.
2. ``should_push_idle_status`` — pure quiet-gate decision for the heartbeat.
3. ``_idle_status_loop`` — actually pushes a ``status`` frame over the WebSocket
   when the connection has been quiet (integration test with shortened timings).
"""

import pytest
from fastapi.testclient import TestClient

from agent_cascade.agent_instance import AgentState
from agent_cascade.api_integration_pkg.state_builder import build_instance_status_from_pool


# ── Stubs ─────────────────────────────────────────────────────────────────────

class _Inst:
    """Minimal AgentInstance stand-in (state + optional state lock)."""

    def __init__(self, state, lock=None):
        self.state = state
        if lock is not None:
            self._state_lock = lock


class _Lock:
    """Context-manager lock stub that records acquisitions."""

    def __init__(self):
        self.entries = 0

    def __enter__(self):
        self.entries += 1
        return self

    def __exit__(self, *exc):
        return False


class _StatusPool:
    """Pool stub for build_instance_status_from_pool only."""

    def __init__(self, instances=None, halted=()):
        self.instances = dict(instances or {})
        self._halted = set(halted)

    def is_instance_halted(self, name):
        return name in self._halted


class _AppPool:
    """Pool stub good enough for create_app() + the minimal-state fallback path."""

    def __init__(self):
        self.instances = {}
        self.templates = {}
        self.stopped = False
        self.settings = None
        self.telemetry = None
        self.operation_manager = None

    def get_instance(self, name):
        # Always None on purpose: build_state() then takes its minimal-state fallback,
        # so the test needs no real instance/conversation. `instances` is still populated
        # for the status snapshot (which reads pool.instances directly).
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


@pytest.fixture
def fast_heartbeat(monkeypatch):
    """Shorten the heartbeat timings so the integration test does not wait 20s."""
    import agent_cascade.api_server as api_server
    monkeypatch.setattr(api_server, 'WS_IDLE_STATUS_TICK_SECS', 0.05)
    monkeypatch.setattr(api_server, 'WS_IDLE_STATUS_QUIET_SECS', 0.0)
    return api_server


# ── 1. build_instance_status_from_pool ───────────────────────────────────────

class TestInstanceStatusSnapshot:
    def test_maps_active_agent_state_and_halt(self):
        lock = _Lock()
        pool = _StatusPool(
            {
                'root': _Inst(AgentState.RUNNING, lock=lock),
                'worker': _Inst(AgentState.IDLE),
                'sleeper': _Inst(AgentState.SLEEPING),
            },
            halted={'worker'},
        )

        status = build_instance_status_from_pool(pool)

        assert status['root'] == {'active': True, 'agent_state': 'RUNNING', 'is_halted': False}
        assert status['worker'] == {'active': False, 'agent_state': 'IDLE', 'is_halted': True}
        assert status['sleeper'] == {'active': False, 'agent_state': 'SLEEPING', 'is_halted': False}
        # The state snapshot must be taken under the instance's state lock.
        assert lock.entries == 1

    def test_unreadable_pool_is_none_valid_empty_pool_is_empty_dict(self):
        # A pool whose instances can't be read at all is UNRELIABLE → None, so the caller
        # omits the instances key and the client refuses to heal on incomplete evidence.
        assert build_instance_status_from_pool(None) is None
        assert build_instance_status_from_pool(object()) is None
        # A valid but empty pool genuinely has nothing active → reliable empty {}.
        assert build_instance_status_from_pool(_StatusPool()) == {}

    def test_partial_snapshot_is_unreliable_returns_none(self):
        class _Broken:
            @property
            def state(self):
                raise RuntimeError('state read failed')

        pool = _StatusPool({'broken': _Broken(), 'root': _Inst(AgentState.RUNNING)})

        # A dropped instance must NOT masquerade as a complete (idle) snapshot: the
        # client would otherwise fake-idle a live run. The whole frame is unreliable.
        assert build_instance_status_from_pool(pool) is None


# ── 2. should_push_idle_status ───────────────────────────────────────────────

class TestIdleStatusGate:
    def _fn(self):
        from agent_cascade.api_server import should_push_idle_status
        return should_push_idle_status

    def test_no_clients_never_pushes(self):
        assert self._fn()(now=1000.0, last_broadcast=0.0, connection_count=0) is False
        assert self._fn()(now=1000.0, last_broadcast=999.0, connection_count=0) is False

    def test_first_frame_after_connect_is_pushed(self):
        """Nothing broadcast yet (0.0) — a freshly connected client gets a status frame."""
        assert self._fn()(now=1000.0, last_broadcast=0.0, connection_count=1) is True

    def test_quiet_gate_blocks_recent_broadcasts(self):
        from agent_cascade import api_server
        quiet = api_server.WS_IDLE_STATUS_QUIET_SECS
        fn = self._fn()
        assert fn(now=1000.0, last_broadcast=1000.0, connection_count=1) is False
        assert fn(now=1000.0, last_broadcast=1000.0 - quiet / 2, connection_count=1) is False
        assert fn(now=1000.0, last_broadcast=1000.0 - quiet, connection_count=1) is True
        assert fn(now=1000.0, last_broadcast=1000.0 - quiet - 1, connection_count=1) is True


# ── 3. End-to-end: the status frame reaches a client ─────────────────────────

class TestHeartbeatOverWebSocket:
    def test_status_frame_pushed_when_quiet(self, fast_heartbeat):
        """A connected client receives {'type': 'status'} after the initial state frame."""
        app = fast_heartbeat.create_app(
            agents=[], agent_pool=_AppPool(), config={'session_name': 'HeartbeatTest'})

        with TestClient(app) as client:
            with client.websocket_connect('/ws/chat') as ws:
                first = ws.receive_json()
                assert first['type'] == 'state', f'expected initial state frame, got {first.get('type')}'

                frame = ws.receive_json()
                assert frame['type'] == 'status', f'expected status heartbeat, got {frame.get('type')}'
                assert frame['generating'] is False
                assert frame['instances'] == {}

    def test_status_frame_carries_instance_flags(self, fast_heartbeat):
        """The heartbeat carries per-instance liveness so the client can repair agent flags."""
        pool = _AppPool()
        pool.instances = {'root': _Inst(AgentState.RUNNING)}
        app = fast_heartbeat.create_app(
            agents=[], agent_pool=pool, config={'session_name': 'HeartbeatTest2'})

        with TestClient(app) as client:
            with client.websocket_connect('/ws/chat') as ws:
                assert ws.receive_json()['type'] == 'state'
                frame = ws.receive_json()
                assert frame['type'] == 'status'
                # build_state's minimal fallback needs get_instance() -> None, so the
                # instance never becomes the root state object; the snapshot still
                # reports the pool's live instance.
                assert frame['instances'] == {
                    'root': {'active': True, 'agent_state': 'RUNNING', 'is_halted': False},
                }

    def test_status_frame_does_not_self_spam(self, monkeypatch):
        """The heartbeat's own frame must reset the quiet gate (it is delivered via
        broadcast()), so a stuck UI gets exactly ONE status frame — not one every tick.

        Guards against a regression that sends status frames around broadcast() (e.g.
        a direct websocket.send_text), which would leave every other test green but
        spam the client every WS_IDLE_STATUS_TICK_SECS."""
        import time
        import agent_cascade.api_server as api_server
        import agent_cascade.api_integration_pkg.state_builder as sb

        monkeypatch.setattr(api_server, 'WS_IDLE_STATUS_TICK_SECS', 0.05)
        monkeypatch.setattr(api_server, 'WS_IDLE_STATUS_QUIET_SECS', 0.5)

        builds = {'n': 0}
        real = sb.build_instance_status_from_pool

        def counting(pool):
            builds['n'] += 1
            return real(pool)

        # Patch BEFORE create_app so the hoisted import binds the counting wrapper.
        monkeypatch.setattr(sb, 'build_instance_status_from_pool', counting)

        app = api_server.create_app(
            agents=[], agent_pool=_AppPool(), config={'session_name': 'SpamTest'})

        with TestClient(app) as client:
            with client.websocket_connect('/ws/chat') as ws:
                assert ws.receive_json()['type'] == 'state'
                first = ws.receive_json()
                assert first['type'] == 'status'
                assert builds['n'] >= 1

                # The first status frame's broadcast() stamps _last_broadcast_mono, so the
                # gate cannot fire again for another QUIET seconds. Let a sub-QUIET window
                # elapse: a heartbeat that bypassed broadcast() would build a 2nd frame here.
                time.sleep(0.25)
                assert builds['n'] == 1, (
                    f'heartbeat self-spammed: built {builds['n']} status frames within '
                    f'the quiet window (expected exactly 1)')
