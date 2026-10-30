"""BUG_0178: a refused state load/save must log WHY, not just the HTTP status.

The incident this guards (2026-10-29 09:38): the engine refused a 2.38 GiB restore with
``400 slot_restore_failed: session snapshot does not fit the free KV capacity; evict other
sessions first``. AC logged only ``State restore returned status 400`` (DEBUG) and ``State
restore failed … cleared label`` (WARNING), so the sentence that says what to do existed
only in the autoloader's console log. Two requests later the engine latched on
``checkpoint recovery owner is stale`` and every request 500ed until the model was reloaded.

What these tests pin:
  - a refused restore logs status + the engine's own message, at WARNING (the caller's
    "cleared label" line cannot carry the reason: ``restore_state`` returns bool);
  - the body is collapsed to one line and bounded, whatever the server sends;
  - a transport failure names the exception instead of vanishing at DEBUG;
  - a successful restore logs no warning (the diagnostic must not become noise);
  - a refused save keeps its body at DEBUG (saves are best-effort: a non-capable endpoint
    answers 501 on every attempt, so WARNING would be periodic noise);
  - behaviour is unchanged: the bool return and the label-clearing still happen.

Capturing style mirrors ``tests/test_bug0103_state_ops_media_gen_logging.py``: state_ops
logs through the late-bound ``_AppLoggerProxy``, so the assertions attach a handler to the
live sink object (``agent_cascade.log.logger``) instead of relying on propagation.
"""

import logging
import threading
from types import SimpleNamespace

import pytest

import agent_cascade.log as log_mod
import agent_cascade.state_ops as state_ops

ENGINE_REFUSAL = (
    'slot_restore_failed: session snapshot does not fit the free KV capacity; '
    'evict other sessions first'
)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _FakeResponse:
    """Only what state_ops reads: status_code and text."""

    def __init__(self, status_code, text='', text_error=None):
        self.status_code = status_code
        self._text = text
        self._text_error = text_error

    @property
    def text(self):
        if self._text_error is not None:
            raise self._text_error
        return self._text


@pytest.fixture
def sink():
    """Records routed through the app log sink (the object the proxy delegates to)."""
    target = log_mod.logger
    handler = _ListHandler()
    prev_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        target.setLevel(prev_level)
        target.removeHandler(handler)


def _messages(records):
    return [r.getMessage() for r in records]


def _warnings(records):
    return [r.getMessage() for r in records if r.levelno >= logging.WARNING]


def _restore(monkeypatch, response=None, exc=None):
    def _post(*args, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(state_ops.httpx, 'post', _post)


def _instance(label='Maine'):
    return SimpleNamespace(
        instance_name='Maine',
        _state_lock=threading.Lock(),
        _state_label=label,
        _last_endpoint_config={'api_base': 'http://127.0.0.1:1234/v1',
                               'model': 'qwen38-27b-swift15abl-nvfp4full-dflash2'},
    )


# ────────────────────────────────────────────────────────────────────────────
# restore_state — the HTTP layer
# ────────────────────────────────────────────────────────────────────────────


class TestRestoreRefusalLogging:
    def test_refusal_logs_status_and_engine_message(self, monkeypatch, sink):
        """The incident's exact shape: 400 + the slot_restore_failed sentence."""
        _restore(monkeypatch, _FakeResponse(400, ENGINE_REFUSAL))

        assert state_ops.restore_state('http://127.0.0.1:1234/v1', 'model-x', 'Maine') is False

        msgs = _warnings(sink)
        assert len(msgs) == 1, f'expected exactly one warning, got {msgs!r}'
        msg = msgs[0]
        assert 'HTTP 400' in msg, msg
        assert 'label=Maine' in msg, msg
        assert 'slot_restore_failed' in msg, msg
        assert 'does not fit the free KV capacity' in msg, msg

    def test_refusal_body_is_one_bounded_line(self, monkeypatch, sink):
        """A gateway error page must not paste a wall of text (or newlines) into the log."""
        body = ('gateway\n\n  exploded\t' + 'x' * 5000)
        _restore(monkeypatch, _FakeResponse(502, body))

        state_ops.restore_state('http://127.0.0.1:1234/v1', 'model-x', 'Maine')

        msg = _warnings(sink)[0]
        assert '\n' not in msg and '\t' not in msg, msg
        assert 'gateway exploded' in msg, msg
        # 400-char body cap + the short prefix/suffix around it.
        assert len(msg) < 600, f'unbounded body in log line ({len(msg)} chars)'
        assert 'chars)' in msg, msg  # truncation marker names what was dropped

    def test_unreadable_body_degrades_to_placeholder(self, monkeypatch, sink):
        """Logging must never raise: a body that cannot be decoded still names the refusal."""
        _restore(monkeypatch, _FakeResponse(400, text_error=UnicodeDecodeError('utf-8', b'', 0, 1, 'boom')))

        assert state_ops.restore_state('http://127.0.0.1:1234/v1', 'model-x', 'Maine') is False

        msg = _warnings(sink)[0]
        assert 'HTTP 400' in msg and '<unreadable body>' in msg, msg

    def test_empty_body_degrades_to_placeholder(self, monkeypatch, sink):
        _restore(monkeypatch, _FakeResponse(400, ''))

        state_ops.restore_state('http://127.0.0.1:1234/v1', 'model-x', 'Maine')

        assert '<empty body>' in _warnings(sink)[0]

    def test_transport_failure_names_the_exception(self, monkeypatch, sink):
        """A timeout/connect error used to be a DEBUG line, invisible in console.log."""
        _restore(monkeypatch, exc=TimeoutError('timed out after 180s'))

        assert state_ops.restore_state('http://127.0.0.1:1234/v1', 'model-x', 'Maine') is False

        msgs = _warnings(sink)
        assert len(msgs) == 1, msgs
        assert 'TimeoutError' in msgs[0] and 'timed out after 180s' in msgs[0], msgs[0]
        assert 'label=Maine' in msgs[0], msgs[0]

    def test_success_logs_no_warning(self, monkeypatch, sink):
        """The diagnostic must not fire on the happy path (it runs on every reacquire)."""
        _restore(monkeypatch, _FakeResponse(200, '{"tokens": 79124}'))

        assert state_ops.restore_state('http://127.0.0.1:1234/v1', 'model-x', 'Maine') is True

        assert _warnings(sink) == [], _warnings(sink)


# ────────────────────────────────────────────────────────────────────────────
# save_state — same defect class, deliberately still DEBUG
# ────────────────────────────────────────────────────────────────────────────


class TestSaveRefusalLogging:
    def test_refused_save_keeps_body_at_debug(self, monkeypatch, sink):
        _restore(monkeypatch, _FakeResponse(400, 'label contains a path separator'))

        assert state_ops.save_state('http://127.0.0.1:1234/v1', 'model-x', 'Maine') is None

        # Still DEBUG (best-effort path), but the reason is now there.
        debugs = [r.getMessage() for r in sink
                  if r.levelno == logging.DEBUG and 'State save' in r.getMessage()]
        assert any('HTTP 400' in m and 'label contains a path separator' in m for m in debugs), \
            f'refused save must carry its body: {debugs!r}'
        assert not any('State save' in m for m in _warnings(sink)), \
            'a refused save must not be promoted to WARNING (periodic noise on 501 endpoints)'


# ────────────────────────────────────────────────────────────────────────────
# restore_instance_state — end-to-end: reason AND the label-clearing consequence
# ────────────────────────────────────────────────────────────────────────────


class TestRestoreInstanceStatePairing:
    def test_failed_restore_logs_reason_and_still_clears_label(self, monkeypatch, sink):
        """The two-line pair the operator sees must be correlatable by label."""
        _restore(monkeypatch, _FakeResponse(400, ENGINE_REFUSAL))
        inst = _instance()

        assert state_ops.restore_instance_state(inst) is False

        msgs = _warnings(sink)
        assert len(msgs) == 2, msgs
        reason, consequence = msgs
        assert 'State restore refused for label=Maine' in reason, reason
        assert 'does not fit the free KV capacity' in reason, reason
        assert 'State restore failed for Maine' in consequence, consequence
        assert 'cleared label' in consequence, consequence
        assert inst._state_label is None, 'the label must still be cleared (no retry storm)'

    def test_successful_restore_clears_label_without_warning(self, monkeypatch, sink):
        _restore(monkeypatch, _FakeResponse(200, '{}'))
        inst = _instance()

        assert state_ops.restore_instance_state(inst) is True

        assert _warnings(sink) == [], _warnings(sink)
        assert inst._state_label is None
