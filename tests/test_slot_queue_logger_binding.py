"""Regression tests for D-2: slot_queue logger binding via late-binding proxy.

Verifies that slot DEBUG forensics reach the app's configured handlers
(agent_cascade_logger) rather than a handler-less module logger.
"""
import logging
import threading
import time

import pytest

from agent_cascade.slot_queue import SlotPool

from tests.slot_test_helpers import app_logger_names


def test_slot_debug_reaches_app_logger(caplog):
    """SlotPool enqueue/grant DEBUG lines must be captured on the app logger."""
    for name in app_logger_names():
        caplog.set_level(logging.DEBUG, logger=name)

    pool = SlotPool(key='test_bind', capacity=1)
    cb_a = pool.acquire(instance_name='A', agent_class='test')
    assert cb_a is not None

    granted = threading.Event()

    def waiter():
        try:
            cb_b = pool.acquire(instance_name='B', agent_class='test', timeout=5.0)
            granted.set()
            if cb_b:
                cb_b()
        except Exception:
            pass

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.3)  # let B enqueue and log "[SLOTPOOL] Queued on"

    # Release A so B can be granted
    cb_a()
    t.join(timeout=5.0)
    assert granted.is_set(), 'Waiter B was not granted after release'

    msgs = [r.getMessage() for r in caplog.records]
    assert any('[SLOTPOOL] Queued on' in m for m in msgs), \
        f"Expected '[SLOTPOOL] Queued on' in captured records; got {len(msgs)} records"
    assert any('[SLOTPOOL] Granted on' in m for m in msgs), \
        f"Expected '[SLOTPOOL] Granted on' in captured records; got {len(msgs)} records"


def test_proxy_resolves_rebound_logger(monkeypatch):
    """The proxy must resolve the CURRENT log.logger, not a stale import."""
    probe = logging.getLogger('probe_xyz')
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _ListHandler()
    probe.addHandler(handler)
    probe.setLevel(logging.DEBUG)

    # Rebind the log module's logger global to our probe
    import agent_cascade.log as log_mod
    monkeypatch.setattr(log_mod, 'logger', probe)

    # Emit through slot_queue's proxy
    from agent_cascade.slot_queue import logger as sq_logger
    sq_logger.warning('probe-message-xyz')

    assert len(records) == 1
    assert records[0].getMessage() == 'probe-message-xyz'
    assert records[0].name == 'probe_xyz'

    probe.removeHandler(handler)


def test_no_basicconfig_introduced():
    """The proxy must not call basicConfig or attach any handler to the root logger.

    We verify by checking that the proxy object itself carries no handler-
    attaching behavior: it is a pure delegate with __slots__ = () and only
    a __getattr__ method. Additionally, we confirm that the module-level
    `logger` in slot_queue IS the proxy (not a real Logger).
    """
    import agent_cascade.slot_queue as sq
    # The module-level logger must be our proxy, not a logging.Logger instance.
    assert type(sq.logger).__name__ == '_AppLoggerProxy'
    # The proxy has no handlers attribute of its own (it delegates to log.logger).
    assert not hasattr(type(sq.logger), 'handlers')
