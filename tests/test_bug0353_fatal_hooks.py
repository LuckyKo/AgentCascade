"""BUG_0353 — fatal-exception hardening: unhandled exceptions become diagnosable.

Light smoke test for the two observability hooks added to ``start_api_server.py``:

  (a) the ``sys.excepthook`` wrapper (``_install_fatal_excepthook``) records an
      unhandled main-thread exception to the AC logger at CRITICAL with a
      ``[FATAL] unhandled exception`` marker, THEN delegates to the original hook;
  (b) the asyncio exception handler (``_ac_asyncio_exception_handler``, armed by
      ``_ac_serve_with_handler``) records an unhandled *task* exception (raised in a
      task and never awaited — the silent-death signature) at CRITICAL with a
      ``[FATAL] unhandled asyncio exception`` marker.

Both hooks resolve the LIVE ``agent_cascade.log.logger`` at call time, so the test
monkeypatches that module global to a temp capturing logger — no ``init_logging()``,
no real handlers, no production log files touched.
"""
import asyncio
import gc
import logging
import sys

import pytest

import agent_cascade.log as ac_log
import start_api_server as sas


class _CaptureHandler(logging.Handler):
    """A logging handler that just appends every record to ``records``."""

    def __init__(self):
        super().__init__(level=logging.CRITICAL)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def capture_logger(monkeypatch):
    """A temp logger standing in for the live AC logger, with a capturing handler.

    The BUG_0353 hooks do ``from agent_cascade.log import logger`` at call time, so
    rebinding that module global routes their CRITICAL records here.
    """
    tmp = logging.getLogger('bug0353_test_logger')
    tmp.setLevel(logging.CRITICAL)
    tmp.propagate = False
    handler = _CaptureHandler()
    tmp.addHandler(handler)
    monkeypatch.setattr(ac_log, 'logger', tmp)
    return handler


def test_excepthook_wrapper_logs_fatal_and_delegates(capture_logger, monkeypatch):
    """(a) An unhandled main-thread exception is logged at CRITICAL with the [FATAL]
    marker, and the ORIGINAL hook is still invoked (delegation preserved)."""
    called = []

    def fake_original(exc_type, exc_value, exc_tb):
        called.append(exc_value)

    # Install the wrapper over a known original hook.
    monkeypatch.setattr(sys, 'excepthook', fake_original)
    monkeypatch.setattr(sas, '_FATAL_EXCEPTHOOK_INSTALLED', False)  # reset idempotency guard
    sas._install_fatal_excepthook()

    # Simulate an unhandled main-thread exception by invoking the installed hook.
    exc_ref = []
    try:
        raise RuntimeError('boom')
    except RuntimeError as e:
        exc_ref.append(e)  # 'as e' is deleted after the block; keep a reference
        exc_type, exc_value, exc_tb = sys.exc_info()
        sys.excepthook(exc_type, exc_value, exc_tb)

    fatal = [r for r in capture_logger.records if '[FATAL] unhandled exception' in r.getMessage()]
    assert fatal, 'expected a [FATAL] unhandled exception CRITICAL record'
    assert fatal[0].levelno == logging.CRITICAL
    assert len(called) == 1 and called[0] is exc_ref[0], 'the original excepthook must still be delegated to'


def test_asyncio_handler_logs_unhandled_task_exception(capture_logger):
    """(b) An unhandled task exception (raised in a task, never awaited) is routed to
    the loop's exception handler and logged at CRITICAL with the [FATAL] marker."""

    async def _boom():
        raise RuntimeError('task boom')

    async def _serve():
        # Mirror _ac_serve_with_handler: arm the handler on the running loop, then
        # spawn a task that raises and is NEVER awaited (the unhandled-task path).
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(sas._ac_asyncio_exception_handler)
        task = loop.create_task(_boom())
        await asyncio.sleep(0)  # let the task run to failure (exception stored)
        del task               # drop the only reference
        gc.collect()           # Task.__del__ -> loop.call_exception_handler(...)
        return None

    asyncio.run(_serve())

    fatal = [r for r in capture_logger.records
             if '[FATAL] unhandled asyncio exception' in r.getMessage()]
    assert fatal, 'expected a [FATAL] unhandled asyncio exception CRITICAL record'
    assert all(r.levelno == logging.CRITICAL for r in fatal)
