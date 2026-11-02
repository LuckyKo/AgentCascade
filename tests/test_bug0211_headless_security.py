#!/usr/bin/env python3
"""BUG_0211: Auto-Ask Security must start without an open browser tab.

The Web UI's ``renderApprovals()`` loop used to be the only sender of the
``ask_security`` WebSocket message. Headless / Telegram-only runs have no tab, so
no security check ever started and every tool approval waited for the timeout.

Fix: the server's approval poll now drives the same ``SecurityAdvisorHandler`` via
``auto_start_pending_security_checks``. These tests cover that driver: enabled gate,
once-per-request-id, one-at-a-time, active-check suppression, and per-check failure
isolation. A source guard locks in the api_server wiring.

Run: python -m pytest tests/test_bug0211_headless_security.py -v -p no:xdist -o addopts=""
"""

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade import security_handler
from agent_cascade.runtime_state import state


class _FakeAdvisor:
    """Records run_check payloads instead of spawning a real advisor thread."""

    instances = []
    fail = set()

    def __init__(self, agent_pool, session, app_state, send_queue, broadcast_fn):
        self.agent_pool = agent_pool
        self.session = session
        self.app_state = app_state
        self.send_queue = send_queue
        self.broadcast_fn = broadcast_fn
        self.calls = []
        type(self).instances.append(self)

    async def run_check(self, data):
        self.calls.append(data)
        if data.get('request_id') in type(self).fail:
            raise RuntimeError('advisor boom')


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    """Fresh fake advisor + no leaked active checks / auto-security value."""
    saved_auto = state.auto_security
    with state._active_checks_lock:
        saved_checks = set(state._active_checks)
        state._active_checks.clear()
    _FakeAdvisor.instances = []
    _FakeAdvisor.fail = set()
    monkeypatch.setattr(security_handler, 'SecurityAdvisorHandler', _FakeAdvisor)
    try:
        yield
    finally:
        state.set_auto_security(saved_auto)
        with state._active_checks_lock:
            state._active_checks.clear()
            state._active_checks.update(saved_checks)


def _trigger(pending, attempted, session=None):
    return asyncio.run(security_handler.auto_start_pending_security_checks(
        agent_pool=object(),
        session=session or {'session_name': 'Maine'},
        app_state=object(),
        send_queue=object(),
        pending=pending,
        attempted=attempted,
    ))


def _approval(rid):
    return {'request_id': rid, 'agent_name': 'coder', 'tool_name': 'shell_cmd'}


def test_starts_check_when_auto_security_enabled():
    """Red/green discriminator: an approval with no browser sends must be checked."""
    state.set_auto_security(True)
    attempted = set()
    started = _trigger([_approval('op_a')], attempted)
    assert started == 1
    assert attempted == {'op_a'}
    assert len(_FakeAdvisor.instances) == 1
    assert _FakeAdvisor.instances[0].calls == [
        {'request_id': 'op_a', 'auto_apply': True}
    ]


def test_auto_security_off_never_starts_check():
    """With Auto-Ask off, approvals wait for the user/Telegram — no advisor run."""
    state.set_auto_security(False)
    attempted = set()
    assert _trigger([_approval('op_a')], attempted) == 0
    assert attempted == set()
    assert _FakeAdvisor.instances == []


def test_each_request_id_is_attempted_once():
    """A slow check must not be re-launched on every 0.3s poll."""
    state.set_auto_security(True)
    attempted = set()
    pending = [_approval('op_a'), _approval('op_b')]
    assert _trigger(pending, attempted) == 1
    assert _trigger(pending, attempted) == 1
    assert _trigger(pending, attempted) == 0
    assert attempted == {'op_a', 'op_b'}
    started_ids = [c['request_id'] for inst in _FakeAdvisor.instances for c in inst.calls]
    assert started_ids == ['op_a', 'op_b']


def test_active_check_suppresses_new_start():
    """Mirror the UI's one-at-a-time rule: a check in flight blocks a second worker."""
    state.set_auto_security(True)
    with state._active_checks_lock:
        state._active_checks.add('op_inflight')
    attempted = set()
    assert _trigger([_approval('op_a')], attempted) == 0
    assert attempted == set(), 'a suppressed start must not burn the one-shot attempt'


def test_one_failing_check_does_not_poison_the_next():
    """A handler exception is logged and the id is not retried forever; others proceed."""
    state.set_auto_security(True)
    _FakeAdvisor.fail = {'op_bad'}
    attempted = set()
    assert _trigger([_approval('op_bad')], attempted) == 0
    assert attempted == {'op_bad'}
    assert _trigger([_approval('op_good')], attempted) == 1
    assert attempted == {'op_bad', 'op_good'}


def test_approval_loop_wires_the_headless_trigger():
    """Wiring guard: the server's approval poll must actually call the new driver."""
    from agent_cascade import api_server

    src = Path(api_server.__file__).read_text(encoding='utf-8')
    assert 'async def _approval_loop' in src
    loop_body = src.split('async def _approval_loop', 1)[1]
    assert 'auto_start_pending_security_checks' in loop_body, \
        'the approval loop must drive headless Auto-Ask security'
    assert 'auto_checked' in loop_body, \
        'the approval loop must keep the one-shot attempt set across polls'
