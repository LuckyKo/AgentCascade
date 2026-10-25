"""Tests for `agent_cascade.log` third-party log routing.

BUG_0147 — records from loggers that are NOT part of the AC tree (`asyncio`, `httpx`, …) used to
fall through to `logging.lastResort` (a WARNING-level `_StderrHandler` bound to `sys.stderr`),
which `_CapturingStream` then re-logged as a nameless WARNING from `log.py`. ERROR was
mislabelled and INFO/DEBUG were dropped entirely. `_AppLogBridgeHandler` re-dispatches the
record object into the app logger instead.

BUG_0146 — `asyncio` on Windows 3.12 reports `ConnectionResetError: [WinError 10054]` from
`_ProactorBasePipeTransport._call_connection_lost` whenever a peer resets a socket (browser tab
reload dropping its WebSocket + keep-alive connections). That is benign teardown noise;
`_ProactorTeardownFilter` drops the traceback and leaves one DEBUG line.

BUG_0148/0149 — uvicorn installs its own `StreamHandler`s, so its bridge removes them first and attaches
exactly one handler at the `uvicorn` root; `httpx`/`httpcore` are bridged too but stay at WARNING (only
failures) unless `AGENT_CASCADE_DEBUG_HTTP=1` asks for the per-request trace, which is ~10 lines each.

No event loop and no sockets: the asyncio record shapes are built by hand exactly as CPython
3.12 emits them (`events.Handle._run` message + `base_events.default_exception_handler`
`exc_info`).
"""

import io
import logging

import pytest

import agent_cascade.log as log_module
from agent_cascade.log import (_AppLogBridgeHandler, _ProactorTeardownFilter, _install_asyncio_log_bridge,
                               _install_http_client_log_bridges, install_uvicorn_log_bridge)

# The exact message `events.Handle._run` puts in context['message'] (plus the 'handle:' line
# that BaseEventLoop.default_exception_handler appends).
_PROACTOR_MSG = ('Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)\n'
                 'handle: <Handle _ProactorBasePipeTransport._call_connection_lost(None)>')


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def app_log(monkeypatch):
    """Isolated stand-in for the live app logger (`agent_cascade_logger[.<instance>]`)."""
    lg = logging.getLogger('ac_test_app_logger_for_bugs_0146_0147')
    recorder = _Recorder()
    lg.handlers = [recorder]
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.disabled = False
    monkeypatch.setattr(log_module, 'logger', lg)
    yield lg, recorder
    lg.handlers = []


def _record(exc, message, level=logging.ERROR, name='asyncio'):
    return logging.LogRecord(name,
                             level,
                             'base_events.py',
                             1899,
                             message, None,
                             (type(exc), exc, exc.__traceback__) if exc is not None else None,
                             'default_exception_handler')


class _bridge_installed:
    """Install the asyncio bridge for one test, then restore the global logging state."""

    def __enter__(self):
        self._logger = logging.getLogger('asyncio')
        self._handlers = list(self._logger.handlers)
        self._level = self._logger.level
        self._propagate = self._logger.propagate
        self._flag = log_module._asyncio_bridge_installed
        self._logger.handlers = []
        self._logger.propagate = True
        self._logger.setLevel(logging.DEBUG)
        log_module._asyncio_bridge_installed = False
        _install_asyncio_log_bridge()
        return self._logger

    def __exit__(self, *exc_info):
        self._logger.handlers = self._handlers
        self._logger.setLevel(self._level)
        self._logger.propagate = self._propagate
        log_module._asyncio_bridge_installed = self._flag
        return False


# ── BUG_0147: records keep their level, source and traceback ─────────────────


def test_bridge_preserves_level_name_and_source(app_log):
    _, recorder = app_log
    _AppLogBridgeHandler().emit(_record(ValueError('boom'), 'Unhandled exception in event loop'))

    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.levelno == logging.ERROR
    assert record.levelname == 'ERROR'
    assert record.name == 'asyncio'
    # The emitting library, not log.py / _CapturingStream.
    assert (record.filename, record.lineno) == ('base_events.py', 1899)


@pytest.mark.parametrize('level', [logging.INFO, logging.DEBUG])
def test_bridge_keeps_the_low_levels_that_used_to_vanish(app_log, level):
    """lastResort is WARNING-only, so INFO/DEBUG from third parties were silently dropped."""
    _, recorder = app_log
    _AppLogBridgeHandler().emit(_record(None, 'httpx request line', level=level, name='httpx'))

    assert [r.levelno for r in recorder.records] == [level]


def test_bridge_respects_the_app_logger_level(app_log):
    """A DEBUG record must not leak into an INFO-level app log."""
    lg, recorder = app_log
    lg.setLevel(logging.WARNING)
    _AppLogBridgeHandler().emit(_record(None, 'noisy detail', level=logging.DEBUG))

    assert recorder.records == []


def test_bridge_preserves_the_traceback(app_log):
    _, recorder = app_log
    try:
        raise RuntimeError('inner failure')
    except RuntimeError as exc:
        _AppLogBridgeHandler().emit(_record(exc, 'Unhandled exception in event loop'))

    formatted = logging.Formatter('%(levelname)s - %(message)s').format(recorder.records[0])
    assert 'RuntimeError: inner failure' in formatted
    assert 'Traceback' in formatted


def test_app_logger_still_propagates_to_root():
    """caplog and any root-level handler must keep seeing AC's own records."""
    assert log_module.logger.propagate is True


# ── BUG_0146: the benign proactor teardown is downgraded ─────────────────────


def test_proactor_teardown_is_dropped_and_leaves_one_debug_line(app_log):
    _, recorder = app_log
    exc = ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host')

    kept = _ProactorTeardownFilter().filter(_record(exc, _PROACTOR_MSG))

    assert kept is False
    assert len(recorder.records) == 1
    debug_line = recorder.records[0]
    assert debug_line.levelno == logging.DEBUG
    assert 'ConnectionResetError' in debug_line.getMessage()
    assert '_ProactorBasePipeTransport._call_connection_lost' in debug_line.getMessage()


@pytest.mark.parametrize('exc_type', [ConnectionResetError, ConnectionAbortedError, BrokenPipeError])
def test_all_peer_gone_exceptions_are_treated_as_benign(app_log, exc_type):
    _, recorder = app_log
    assert _ProactorTeardownFilter().filter(_record(exc_type(10054, 'gone'), _PROACTOR_MSG)) is False
    assert [r.levelno for r in recorder.records] == [logging.DEBUG]


def test_same_exception_in_a_different_callback_is_kept(app_log):
    """Only the teardown callback is benign — a reset inside another callback is real signal."""
    _, recorder = app_log
    exc = ConnectionResetError(10054, 'reset')
    message = 'Exception in callback <Task step>'

    assert _ProactorTeardownFilter().filter(_record(exc, message)) is True
    assert recorder.records == []


def test_proactor_non_connection_exception_is_kept(app_log):
    """A genuine bug in _call_connection_lost must never be silenced."""
    _, recorder = app_log
    assert _ProactorTeardownFilter().filter(_record(ValueError('bug in my code'), _PROACTOR_MSG)) is True
    assert recorder.records == []


def test_record_without_exc_info_is_kept(app_log):
    _, recorder = app_log
    assert _ProactorTeardownFilter().filter(_record(None, _PROACTOR_MSG)) is True
    assert recorder.records == []


# ── Install / wiring ────────────────────────────────────────────────────────


def test_install_is_idempotent_and_disables_asyncio_propagation(app_log):
    with _bridge_installed() as asyncio_logger:
        _install_asyncio_log_bridge()
        _install_asyncio_log_bridge()

        bridges = [h for h in asyncio_logger.handlers if isinstance(h, _AppLogBridgeHandler)]
        assert len(bridges) == 1
        assert asyncio_logger.propagate is False
        assert any(isinstance(f, _ProactorTeardownFilter) for f in bridges[0].filters)


def test_installed_bridge_delivers_an_asyncio_error_at_error_level(app_log):
    """End-to-end through the real wiring: asyncio -> bridge -> app logger."""
    _, recorder = app_log
    with _bridge_installed() as asyncio_logger:
        try:
            raise RuntimeError('task blew up')
        except RuntimeError as exc:
            asyncio_logger.error('Unhandled exception in event loop', exc_info=exc)

    errors = [r for r in recorder.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].name == 'asyncio'
    assert 'RuntimeError: task blew up' in logging.Formatter('%(message)s').format(errors[0])


def test_installed_bridge_silences_the_real_proactor_scenario(app_log):
    """The todo.md:114 traceback: nothing at INFO+, one DEBUG line under AGENT_CASCADE_DEBUG."""
    lg, recorder = app_log

    with _bridge_installed() as asyncio_logger:
        asyncio_logger.error(_PROACTOR_MSG,
                             exc_info=(ConnectionResetError, ConnectionResetError(
                                 10054, 'An existing connection was forcibly closed by the remote host'), None))
        assert [r for r in recorder.records if r.levelno >= logging.INFO] == []
        assert [r.levelno for r in recorder.records] == [logging.DEBUG]

        # ...and with the app logger at INFO (the production default) not even that remains.
        recorder.records.clear()
        lg.setLevel(logging.INFO)
        asyncio_logger.error(_PROACTOR_MSG,
                             exc_info=(ConnectionResetError, ConnectionResetError(10054, 'reset'), None))
        assert recorder.records == []


# ── BUG_0148: uvicorn / httpx loggers — bridge once, never twice ─────────────


class _uvicorn_configured:
    """Reproduce uvicorn 0.34's LOGGING_CONFIG graph: own StreamHandlers, propagate=False.

    The ``StreamHandler``s stand in for the stderr/stdout handlers uvicorn installs; whatever they
    receive is what AC's ``_CapturingStream`` would re-log as a nameless WARNING, and what the
    bridge must therefore prevent from firing at all.
    """

    def __enter__(self):
        self._saved = {}
        for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
            lg = logging.getLogger(name)
            self._saved[name] = (list(lg.handlers), lg.level, lg.propagate)
            lg.handlers = []
            lg.setLevel(logging.INFO)

        # uvicorn / uvicorn.access carry handlers and do not propagate.
        self.stderr_stream = io.StringIO()
        self.stdout_stream = io.StringIO()
        logging.getLogger('uvicorn').addHandler(logging.StreamHandler(self.stderr_stream))
        logging.getLogger('uvicorn').propagate = False
        logging.getLogger('uvicorn.access').addHandler(logging.StreamHandler(self.stdout_stream))
        logging.getLogger('uvicorn.access').propagate = False
        logging.getLogger('uvicorn.error').propagate = True
        return self

    def __exit__(self, *exc_info):
        for name, (handlers, level, propagate) in self._saved.items():
            lg = logging.getLogger(name)
            lg.handlers = handlers
            lg.setLevel(level)
            lg.propagate = propagate
        return False


def _bridges(logger_name):
    return [h for h in logging.getLogger(logger_name).handlers if isinstance(h, _AppLogBridgeHandler)]


def test_uvicorn_bridge_removes_upstream_handlers_and_bridges_only_the_root(app_log):
    with _uvicorn_configured():
        # Same as uvicorn 0.34: uvicorn + uvicorn.access carry the StreamHandlers.
        for name in ('uvicorn', 'uvicorn.access'):
            assert any(isinstance(h, logging.StreamHandler) for h in logging.getLogger(name).handlers), name

        install_uvicorn_log_bridge()

        for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
            assert not any(isinstance(h, logging.StreamHandler) for h in logging.getLogger(name).handlers), name
        # One bridge on the tree root only: the children reach it through the handler walk.
        assert len(_bridges('uvicorn')) == 1
        assert _bridges('uvicorn.error') == []
        assert _bridges('uvicorn.access') == []


def test_uvicorn_error_is_logged_once_with_its_real_name(app_log):
    _, recorder = app_log
    with _uvicorn_configured() as state:
        install_uvicorn_log_bridge()

        logging.getLogger('uvicorn.error').warning('Invalid HTTP request received.')

        assert [r.getMessage() for r in recorder.records] == ['Invalid HTTP request received.']
        assert recorder.records[0].levelno == logging.WARNING
        assert recorder.records[0].name == 'uvicorn.error'
        # The upstream stderr handler is detached, so nothing is re-captured as a second line.
        assert state.stderr_stream.getvalue() == ''


def test_uvicorn_access_record_is_logged_once_with_args_expanded(app_log):
    _, recorder = app_log
    with _uvicorn_configured() as state:
        install_uvicorn_log_bridge()

        logging.getLogger('uvicorn.access').info('%s - "%s %s HTTP/%s" %d',
                                                 '127.0.0.1:41000', 'GET', '/api/state', '1.1', 200)

        assert len(recorder.records) == 1
        assert recorder.records[0].getMessage() == '127.0.0.1:41000 - "GET /api/state HTTP/1.1" 200'
        assert recorder.records[0].levelno == logging.INFO
        assert state.stdout_stream.getvalue() == ''


def test_uvicorn_bridge_preserves_levels_and_is_idempotent(app_log):
    with _uvicorn_configured():
        uvicorn_logger = install_uvicorn_log_bridge()
        install_uvicorn_log_bridge()

        assert len(_bridges('uvicorn')) == 1
        assert uvicorn_logger.propagate is False
        # uvicorn's quietness policy (log_level='warning') is untouched.
        assert uvicorn_logger.level == logging.INFO
        assert logging.getLogger('uvicorn.access').level == logging.INFO
        assert logging.getLogger('uvicorn.error').propagate is True


class _http_client_loggers_reset:
    """Clear the httpx/httpcore handler state for one test, restore it afterwards."""

    def __enter__(self):
        self._saved = {}
        for name in ('httpx', 'httpcore'):
            lg = logging.getLogger(name)
            self._saved[name] = (list(lg.handlers), lg.level, lg.propagate)
            lg.handlers = []
            lg.setLevel(logging.NOTSET)
        return self

    def __exit__(self, *exc_info):
        for name, (handlers, level, propagate) in self._saved.items():
            lg = logging.getLogger(name)
            lg.handlers = handlers
            lg.setLevel(level)
            lg.propagate = propagate
        return False


def test_http_client_loggers_are_quiet_by_default(app_log):
    """WARNING only: the app level must not raise them (BUG_0149 regression)."""
    with _http_client_loggers_reset():
        applied = _install_http_client_log_bridges()

        assert applied == {'httpx': logging.WARNING, 'httpcore': logging.WARNING}
        for name in ('httpx', 'httpcore'):
            assert logging.getLogger(name).propagate is False
            assert len(_bridges(name)) == 1
        # Idempotent: a second install must not stack a second bridge.
        _install_http_client_log_bridges()
        assert len(_bridges('httpx')) == 1


def test_agent_cascade_debug_alone_does_not_unmute_http_tracing(app_log, monkeypatch):
    """The separate switch exists because AGENT_CASCADE_DEBUG=1 is the normal dev setup."""
    monkeypatch.setenv('AGENT_CASCADE_DEBUG', '1')
    monkeypatch.delenv('AGENT_CASCADE_DEBUG_HTTP', raising=False)

    with _http_client_loggers_reset():
        assert _install_http_client_log_bridges()['httpcore'] == logging.WARNING


@pytest.mark.parametrize('http_debug', [False, True], ids=['default', 'opt-in'])
def test_http_client_trace_levels_are_opt_in_only(app_log, http_debug):
    expected = ({'httpx': logging.INFO, 'httpcore': logging.DEBUG} if http_debug else
                {'httpx': logging.WARNING, 'httpcore': logging.WARNING})

    with _http_client_loggers_reset():
        assert _install_http_client_log_bridges(http_debug) == expected
        for name, level in expected.items():
            assert logging.getLogger(name).level == level


def test_http_request_lines_need_the_explicit_opt_in(app_log, monkeypatch):
    _, recorder = app_log
    httpx_logger = logging.getLogger('httpx')
    httpcore_logger = logging.getLogger('httpcore')

    with _http_client_loggers_reset():
        _install_http_client_log_bridges()
        # These two shapes were ~70% of a dev console.log before BUG_0149.
        httpx_logger.info('HTTP Request: POST https://api.telegram.org/bot/getUpdates "HTTP/1.1 200 OK"')
        httpcore_logger.debug('receive_response_body.started')
        assert recorder.records == []

        # Real problems still surface at the quiet default.
        httpx_logger.warning('connection pool exhausted')
        assert [r.levelno for r in recorder.records] == [logging.WARNING]

        recorder.records.clear()
        monkeypatch.setenv('AGENT_CASCADE_DEBUG_HTTP', '1')
        _install_http_client_log_bridges()
        httpx_logger.info('HTTP Request: POST https://api.telegram.org/bot/getUpdates "HTTP/1.1 200 OK"')
        httpcore_logger.debug('receive_response_body.started')
        assert [r.levelno for r in recorder.records] == [logging.INFO, logging.DEBUG]
