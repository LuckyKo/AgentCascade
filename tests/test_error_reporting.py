"""Tests for agent_cascade.error_reporting (plan api_failure_feedback_plan.md §4).

Covers:
  1. format_endpoint_error — unit cases + edge cases (None, >160 chars, deep __cause__, non-Exception)
  2. classify_endpoint_failure — category mapping table incl. edge cases
  3. TracebackDedup — window semantics, key independence, thread-safety smoke, counter-based pruning
  4. Router Layer-1 — compact WARNING line + deduped DEBUG traceback + .endpoint_failures on terminal error
  5. llm_call terminal path — multi-line [SYSTEM ERROR] from .endpoint_failures (with/without the attr)

These tests are self-contained: openai/httpx exception types are built by duck-typed fakes
(class-name + attributes), so no live SDK/network is required and the leaf module's import
audit is exercised implicitly.
"""

import threading

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Fakes — duck-typed stand-ins for openai/httpx exception types (never imported here)
# ──────────────────────────────────────────────────────────────────────────────


class _FakeRequest:

    def __init__(self, url):
        self.url = url


class _FakeResponse:
    """Mimics the httpx.Response surface openai/httpx status errors expose."""

    def __init__(self, status_code, text, url):
        self.status_code = status_code
        self.text = text
        self.headers = {}
        self.request = _FakeRequest(url)


class APIStatusError(Exception):
    """Duck-typed openai.APIStatusError (name + .response/.body attrs)."""

    def __init__(self, message, response=None, body=None):
        super().__init__(message)
        self.message = message
        self.response = response
        self.body = body


class HTTPStatusError(Exception):
    """Duck-typed httpx.HTTPStatusError (name + .response attr)."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.message = message
        self.response = response


class ConnectError(Exception):
    """Duck-typed httpx.ConnectError / ConnectionError (name only)."""
    pass


class ReadTimeout(Exception):
    """Duck-typed httpx.ReadTimeout / TimeoutException (name only)."""
    pass


def _status_error(code, body_msg, url='http://127.0.0.1:1234/v1', cls=APIStatusError):
    """Build a status-carrying error with a response carrying the given URL + body text."""
    resp = _FakeResponse(status_code=code, text=body_msg, url=url)
    if cls is APIStatusError:
        return cls('status error', response=resp, body={'error': {'message': body_msg}})
    return cls('status error', response=resp)


def _mse(code=None, exception=None, message=None):
    """Build a ModelServiceError (the canonical wrapper oai.py raises)."""
    from agent_cascade.llm.base import ModelServiceError
    if exception is not None:
        return ModelServiceError(exception=exception, code=code)
    return ModelServiceError(code=code, message=message)


# ──────────────────────────────────────────────────────────────────────────────
# 1. format_endpoint_error
# ──────────────────────────────────────────────────────────────────────────────


class TestFormatEndpointError:

    def test_http_502_with_body(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        e = _mse(code='502', exception=_status_error(502, 'llama-server unreachable'))
        out = fmt(e)
        assert 'HTTP 502' in out
        assert '127.0.0.1:1234/v1' in out
        assert 'llama-server unreachable' in out
        assert '\n' not in out  # single line

    def test_http_503_model_load(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        e = _mse(code='503', exception=_status_error(503, "Failed to load model 'Agents-A1-8b'"))
        out = fmt(e)
        assert 'HTTP 503' in out
        assert 'Failed to load model' in out

    def test_connect_error_wrapping_winerror(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        winerr = OSError(10055, 'socket buffer full')
        ce = ConnectError('connect failed')
        ce.__cause__ = winerr
        e = _mse(exception=ce)
        out = fmt(e)
        # Root cause is the inner OSError — its errno text must surface.
        assert '10055' in out or 'socket buffer full' in out

    def test_read_timeout(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        out = fmt(ReadTimeout('timed out after 30s'))
        assert 'timeout' in out.lower()

    def test_api_status_error_with_body_dict(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt

        # Direct APIStatusError (not wrapped in ModelServiceError).
        e = _status_error(429, 'rate limited', url='http://x:1/v1')
        out = fmt(e)
        assert 'HTTP 429' in out
        assert 'rate limited' in out

    def test_bare_runtime_error_fallback(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        out = fmt(RuntimeError('something went wrong'))
        assert 'RuntimeError' in out
        assert 'something went wrong' in out
        assert '\n' not in out

    def test_none_input_safe_fallback(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        out = fmt(None)
        assert isinstance(out, str) and len(out) > 0
        # Must not raise.

    def test_long_message_truncated(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        long_msg = 'x' * 400
        out = fmt(RuntimeError(long_msg))
        assert '\n' not in out
        assert len(out) <= 220  # well under the ~200 budget + type prefix

    def test_deep_cause_chain_root_found(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        root = ValueError('the actual root reason')
        mid = RuntimeError('middle layer')
        outer = ConnectionError('outer wrapper')
        mid.__cause__ = root
        outer.__cause__ = mid
        out = fmt(outer)
        assert 'the actual root reason' in out

    def test_non_exception_object_safe_fallback(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        out = fmt('just a plain string')
        assert isinstance(out, str) and len(out) > 0
        out2 = fmt(12345)
        assert isinstance(out2, str)

    def test_single_line_and_bounded(self):
        from agent_cascade.error_reporting import format_endpoint_error as fmt
        cases = [
            _mse(code='502', exception=_status_error(502, 'boom' * 100)),
            _mse(exception=ConnectError('connect')),
            RuntimeError('y' * 300),
        ]
        for e in cases:
            out = fmt(e)
            assert '\n' not in out
            assert len(out) <= 260


# ──────────────────────────────────────────────────────────────────────────────
# 2. classify_endpoint_failure
# ──────────────────────────────────────────────────────────────────────────────


class TestClassifyEndpointFailure:

    def test_mapping_table(self):
        from agent_cascade.error_reporting import classify_endpoint_failure as cls
        assert cls(_mse(code='429', exception=_status_error(429, 'rate limited'))) == 'rate limited (HTTP 429)'
        assert cls(_mse(code='401', exception=_status_error(401, 'unauthorized'))) == 'authentication failure'
        assert cls(_mse(code='503', exception=_status_error(503, "Failed to load model 'm'"))) == 'model load failure'
        assert cls(_mse(code='502', exception=_status_error(502,
                                                            'llama-server unreachable'))) == 'server error (HTTP 502)'
        assert cls(_mse(exception=ConnectError('connect'))) == 'connection refused/unreachable'
        assert cls(ReadTimeout('timed out')) == 'network timeout'

    def test_text_based_classification(self):
        from agent_cascade.error_reporting import classify_endpoint_failure as cls
        assert cls(RuntimeError('connection refused by host')) == 'connection refused/unreachable'
        assert cls(RuntimeError('read timed out after 30s')) == 'network timeout'
        assert cls(RuntimeError('429 Too Many Requests')) == 'rate limited (HTTP 429)'

    def test_none_input(self):
        from agent_cascade.error_reporting import classify_endpoint_failure as cls
        assert cls(None) == 'unknown error'

    def test_unknown_fallback(self):
        from agent_cascade.error_reporting import classify_endpoint_failure as cls
        assert cls(RuntimeError('totally opaque failure')) == 'unknown error'


# ──────────────────────────────────────────────────────────────────────────────
# 3. TracebackDedup
# ──────────────────────────────────────────────────────────────────────────────


class TestTracebackDedup:

    def test_window_semantics(self):
        from agent_cascade.error_reporting import TracebackDedup
        d = TracebackDedup(window_seconds=60.0)
        k = d.get_tb_key(RuntimeError('boom'))
        assert d.should_log_full_tb(k, now=1000.0) is True  # first time
        assert d.should_log_full_tb(k, now=1010.0) is False  # within window
        assert d.should_log_full_tb(k, now=1100.0) is True  # window expired

    def test_different_keys_independent(self):
        from agent_cascade.error_reporting import TracebackDedup
        d = TracebackDedup(window_seconds=60.0)
        k1 = d.get_tb_key(RuntimeError('a'))
        k2 = d.get_tb_key(RuntimeError('b'))
        assert k1 != k2
        assert d.should_log_full_tb(k1, now=1000.0) is True
        assert d.should_log_full_tb(k2, now=1000.0) is True  # independent — not suppressed

    def test_key_differs_by_root_cause(self):
        from agent_cascade.error_reporting import TracebackDedup
        d = TracebackDedup()
        # Same endpoint/model context but different root message → different key.
        k1 = d.get_tb_key(RuntimeError('root A'))
        k2 = d.get_tb_key(RuntimeError('root B'))
        assert k1 != k2

    def test_thread_safety_smoke(self):
        from agent_cascade.error_reporting import TracebackDedup
        d = TracebackDedup(window_seconds=60.0)
        k = d.get_tb_key(RuntimeError('shared'))
        results = []
        lock = threading.Lock()

        def worker():
            first = d.should_log_full_tb(k, now=1000.0)
            with lock:
                results.append(first)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Exactly ONE thread should observe the "first" True within the same window.
        assert sum(results) == 1, f"expected exactly 1 True, got {sum(results)}"

    def test_counter_based_pruning(self):
        from agent_cascade.error_reporting import TracebackDedup
        d = TracebackDedup(window_seconds=60.0, prune_after_seconds=3600.0, prune_every_n_calls=3)
        # Seed a stale entry (last seen 10s ago; now will be 5000 → age 4990 > 3600).
        d._last_seen['stale_key'] = 10.0
        # Drive 3 calls to trigger the periodic sweep (prune_every_n_calls=3).
        for _ in range(3):
            d.should_log_full_tb('fresh_key', now=5000.0)
        assert 'stale_key' not in d._last_seen, 'stale entry should have been pruned'
        assert 'fresh_key' in d._last_seen, 'recent entry must be retained'
        assert d._size() == 1

    def test_pruning_not_every_call(self):
        """The sweep is counter-gated: with prune_every_n_calls=100, a single call does NOT prune."""
        from agent_cascade.error_reporting import TracebackDedup
        d = TracebackDedup(window_seconds=60.0, prune_after_seconds=3600.0, prune_every_n_calls=100)
        d._last_seen['stale_key'] = 10.0
        # One call — counter hits 1 (not a multiple of 100) → no sweep → stale entry remains.
        d.should_log_full_tb('fresh_key', now=5000.0)
        assert 'stale_key' in d._last_seen

    def test_omitted_now_uses_wall_clock(self, monkeypatch):
        """REGRESSION (review finding #1): production call sites omit ``now``. The method must
        resolve wall-clock time itself — NOT pin to t=0 — or dedup becomes permanent and pruning
        never fires.

        Note: the window is SLIDING — every call (logged or suppressed) updates _last_seen[key],
        so each subsequent call must land >window after the PREVIOUS call to re-log. We advance
        the mocked clock by 70s (> 60s window) before each call and assert it re-logs. With the old
        now=0.0 default every omitted-now call would pin to t=0 → first True, then False forever."""
        import agent_cascade.error_reporting as er
        from agent_cascade.error_reporting import TracebackDedup

        d = TracebackDedup(window_seconds=60.0)
        k = d.get_tb_key(RuntimeError('boom'))

        # First omitted-now call: wall clock at 1000s → logs (first time).
        monkeypatch.setattr(er.time, 'time', lambda: 1000.0)
        assert d.should_log_full_tb(k) is True

        # Second omitted-now call 70s later (> window since last update at 1000) → re-logs.
        monkeypatch.setattr(er.time, 'time', lambda: 1070.0)
        assert d.should_log_full_tb(k) is True

        # Third omitted-now call another 70s later (> window since last update at 1070) → re-logs.
        # With the old now=0.0 default this would be False (permanent dedup pinned at t=0).
        monkeypatch.setattr(er.time, 'time', lambda: 1140.0)
        assert d.should_log_full_tb(k) is True

        # Sanity: a call within the window of the last update IS suppressed (window still works).
        monkeypatch.setattr(er.time, 'time', lambda: 1150.0)
        assert d.should_log_full_tb(k) is False

    def test_omitted_now_pruning_uses_wall_clock(self, monkeypatch):
        """Companion to the above: with omitted now, counter-based pruning must fire on real time.
        A stale entry (last seen long ago in wall-clock terms) is pruned once a sweep runs."""
        import agent_cascade.error_reporting as er
        from agent_cascade.error_reporting import TracebackDedup

        d = TracebackDedup(window_seconds=60.0, prune_after_seconds=3600.0, prune_every_n_calls=1)
        # Seed a stale entry at wall-clock 1000s; current wall clock is 5000s → age 4000 > 3600.
        d._last_seen['stale_key'] = 1000.0
        monkeypatch.setattr(er.time, 'time', lambda: 5000.0)
        # One omitted-now call triggers the sweep (prune_every_n_calls=1).
        d.should_log_full_tb('fresh_key')
        assert 'stale_key' not in d._last_seen, 'stale entry should be pruned using wall-clock time'
        assert 'fresh_key' in d._last_seen


# ──────────────────────────────────────────────────────────────────────────────
# 4. Router Layer-1 — compact WARNING + deduped DEBUG + .endpoint_failures
# ──────────────────────────────────────────────────────────────────────────────


def _make_router_with_failing_endpoint(fail_exc_factory, max_retries=0):
    """Build an APIRouter with one endpoint whose call always raises ``fail_exc_factory()``.

    Sanity probe is disabled so the chain reaches the failing endpoint directly.
    """
    from agent_cascade.api_router import APIEndpoint, APIRouter

    router = APIRouter(default_llm_cfg={
        'model': 'default-model',
        'api_base': 'http://localhost:1111/v1',
        'model_type': 'qwenvl_oai',
    })
    ep = APIEndpoint(name='test-ep', api_base='http://localhost:9998/v1', model='mock-model', max_retries=max_retries)
    router.add_endpoint(ep)
    router.set_agent_priorities('coder', [ep.id])

    def do_call(llm_cfg):
        raise fail_exc_factory()

    return router, do_call


class TestRouterLayer1:

    @pytest.fixture(autouse=True)
    def _disable_probe(self, monkeypatch):
        import agent_cascade.api_router_pkg.router as router_mod
        monkeypatch.setattr(router_mod, 'SANITY_PROBE_ENABLED', False)

    def test_terminal_error_has_endpoint_failures_attr(self):
        """The terminal RuntimeError must carry .endpoint_failures (plan §3.4 / review fix #3)."""
        import agent_cascade.api_router_pkg.router as router_mod
        from agent_cascade.llm.base import ModelServiceError

        # Reset the shared dedup so the DEBUG-traceback assertion is deterministic.
        router_mod.TB_DEDUP = type(router_mod.TB_DEDUP)()

        def fail():
            return ModelServiceError(exception=ConnectError('connection refused'))

        router, do_call = _make_router_with_failing_endpoint(fail, max_retries=0)
        with pytest.raises(RuntimeError, match='All API endpoints exhausted') as exc_info:
            router.call_with_fallback('coder', do_call, agent_instance_name='test-inst')

        exc = exc_info.value
        # The compact per-endpoint list is attached as structured data.
        assert hasattr(exc, 'endpoint_failures'), 'terminal RuntimeError must carry .endpoint_failures'
        assert isinstance(exc.endpoint_failures, list) and len(exc.endpoint_failures) >= 1
        # First line of the message is preserved exactly (existing tests rely on it).
        assert str(exc).split('\n', 1)[0] == "All API endpoints exhausted for agent type 'coder'."

    def test_warning_is_compact_single_line(self, caplog):
        """Layer-1 WARNING is a single compact line (no embedded traceback) and DEBUG TB is deduped."""
        import logging

        import agent_cascade.api_router_pkg.router as router_mod
        from agent_cascade.llm.base import ModelServiceError

        # Fresh dedup so the "exactly one DEBUG" assertion is deterministic.
        router_mod.TB_DEDUP = type(router_mod.TB_DEDUP)()

        def fail():
            return ModelServiceError(exception=ConnectError('connection refused'))

        router, do_call = _make_router_with_failing_endpoint(fail, max_retries=2)  # 3 attempts
        with caplog.at_level(logging.DEBUG, logger='agent_cascade.api_router_pkg.router'):
            with pytest.raises(RuntimeError, match='All API endpoints exhausted'):
                router.call_with_fallback('coder', do_call, agent_instance_name='test-inst')

        warning_lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and '[APIRouter]' in r.getMessage() and 'attempt' in r.getMessage()
        ]
        assert warning_lines, 'expected per-attempt WARNING lines'
        # Every per-attempt WARNING is a single compact line — no traceback text embedded.
        for wl in warning_lines:
            assert 'Traceback' not in wl, f"WARNING must be compact one-liner, got: {wl!r}"
            assert '\n' not in wl

        # Full traceback at DEBUG appears EXACTLY ONCE across the 3 identical failures (deduped).
        debug_tb_lines = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and 'Traceback:' in r.getMessage()
        ]
        assert len(debug_tb_lines) == 1, (f"expected exactly 1 deduped DEBUG traceback, got {len(debug_tb_lines)}: "
                                          f"{[line[:80] for line in debug_tb_lines]}")


# ──────────────────────────────────────────────────────────────────────────────
# 5. llm_call terminal path — multi-line [SYSTEM ERROR] from .endpoint_failures
# ──────────────────────────────────────────────────────────────────────────────


def _build_engine_with_failing_llm(exc, caplog=None):
    """Build an ExecutionEngine whose LLM call raises ``exc`` on every attempt.

    The terminal [SYSTEM ERROR] branch (llm_call.py:1156) fires only when ``retry_count >
    _max_attempts`` at the top of the retry loop; a plain raise from the stub only reaches
    ``== _max_attempts`` and falls through to "Empty LLM response". In production the branch is
    reached because ``_abort_stream`` pre-increments retry_count mid-stream. The test bodies
    therefore use a two-part design: (a) drive this real engine to confirm it surfaces a
    [SYSTEM ERROR] on exhaustion, and (b) assert the exact terminal message produced by
    ``build_terminal_message(exc, cap)`` — the function that branch calls. Everything downstream
    (retry loop, error classification, backoff) is real; only the innermost LLM call and the
    pre-LLM checks are stubbed so the test stays fast and focused.
    """
    from types import SimpleNamespace

    from agent_cascade.agent_instance import AgentInstance
    from agent_cascade.engine.core import ExecutionEngine

    engine = ExecutionEngine.__new__(ExecutionEngine)  # skip __init__ (heavy handlers)

    pool = SimpleNamespace()
    pool.settings = SimpleNamespace(retry_max_attempts=2, retry_base_delay=0.1, retry_max_delay=0.5)
    pool.api_router = None
    pool.telemetry = None
    engine.pool = pool
    engine.compression_handler = SimpleNamespace()

    instance = AgentInstance(
        instance_name='test-inst',
        agent_class='coder',
        conversation=[],
        created_at=0.0,
        last_activity=0.0,
        latest_marker_index=0,
    )
    template = SimpleNamespace(llm=None, name='coder')
    active_functions = []

    # Stub the pre-LLM checks (stop/halt/compression/loop) to "continue".
    engine._pre_llm_checks = lambda *a, **k: False
    # Stub telemetry so _record_telemetry_event is a no-op.
    engine._telemetry = lambda: None

    def _failing_execute(*args, **kwargs):
        raise exc

    engine._execute_llm_call = _failing_execute
    return engine, instance, template, active_functions


def _drive_engine(engine, instance, template, active_functions):
    """Drive the real retry loop to exhaustion and return the yielded messages.

    Confirms the engine surfaces a [SYSTEM ERROR] message when every LLM attempt fails (the
    integration half of the terminal-path tests). The exact terminal message content is asserted
    separately against build_terminal_message — see the test bodies.
    """
    messages = list(engine._execute_llm_call_with_retry(instance, [], template, active_functions))
    return messages


class TestLlmCallTerminalPath:

    @pytest.fixture(autouse=True)
    def _disable_probe(self, monkeypatch):
        import agent_cascade.api_router_pkg.router as router_mod
        monkeypatch.setattr(router_mod, 'SANITY_PROBE_ENABLED', False)

    def test_terminal_message_with_endpoint_failures(self, caplog):
        """Terminal message (with .endpoint_failures): multi-line per-endpoint lines + action hint.

        Two-part: (a) the engine surfaces a [SYSTEM ERROR] on exhaustion; (b) build_terminal_message
        — the exact function the terminal branch calls — produces the readable multi-line content.
        """
        import logging

        from agent_cascade.error_reporting import TB_DEDUP, build_terminal_message

        # Fresh dedup so log assertions are deterministic.
        TB_DEDUP.__init__()

        exc = RuntimeError(
            "All API endpoints exhausted for agent type 'coder'.\n"
            "Endpoint 'a' @ http://127.0.0.1:1/v1 attempt 1/2: HTTP 502 from http://127.0.0.1:1/v1: llama-server unreachable\n"
            "Endpoint 'b' @ http://127.0.0.1:2/v1 attempt 1/2: connection error to http://127.0.0.1:2/v1 (WinError 10055)"
        )
        exc.endpoint_failures = [
            "Endpoint 'a' @ http://127.0.0.1:1/v1 attempt 1/2: HTTP 502 from http://127.0.0.1:1/v1: llama-server unreachable",
            "Endpoint 'b' @ http://127.0.0.1:2/v1 attempt 1/2: connection error to http://127.0.0.1:2/v1 (WinError 10055)",
        ]

        # (a) Engine integration: exhaustion surfaces a [SYSTEM ERROR] message.
        engine, instance, template, active_functions = _build_engine_with_failing_llm(exc)
        with caplog.at_level(logging.DEBUG):
            messages = _drive_engine(engine, instance, template, active_functions)
        error_msgs = [m for m in messages if getattr(m, 'content', '') and '[SYSTEM ERROR:' in str(m.content)]
        assert error_msgs, f"no [SYSTEM ERROR] message yielded: {[getattr(m,'content',None) for m in messages]}"

        # (b) Terminal message unit: build_terminal_message produces the readable multi-line content.
        content = build_terminal_message(exc, 2)
        assert 'LLM unavailable after' in content
        assert 'Endpoints tried:' in content
        assert 'llama-server unreachable' in content
        assert 'WinError 10055' in content or 'connection error' in content
        # Dominant category is connection → the reachable hint.
        assert 'Check that the LLM server is running and reachable.' in content

        # The terminal ERROR log line (if any) must NOT contain a raw traceback dump.
        err_logs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        for el in err_logs:
            assert 'Traceback' not in el, f"terminal ERROR log must be compact, got: {el!r}"

    def test_terminal_message_without_endpoint_failures(self):
        """Fallback path: no .endpoint_failures attr → build_terminal_message still yields readable text."""
        from agent_cascade.error_reporting import build_terminal_message

        exc = RuntimeError("All API endpoints exhausted for agent type 'coder'.")
        # Deliberately no .endpoint_failures attribute.
        assert not hasattr(exc, 'endpoint_failures')

        # (a) Engine integration: exhaustion surfaces a [SYSTEM ERROR] message.
        engine, instance, template, active_functions = _build_engine_with_failing_llm(exc)
        messages = _drive_engine(engine, instance, template, active_functions)
        error_msgs = [m for m in messages if getattr(m, 'content', '') and '[SYSTEM ERROR:' in str(m.content)]
        assert error_msgs, 'no [SYSTEM ERROR] message yielded on fallback path'

        # (b) Terminal message unit: falls back to the first line of str(e).
        content = build_terminal_message(exc, 2)
        assert 'LLM unavailable after' in content
        assert 'All API endpoints exhausted for agent type' in content


# ──────────────────────────────────────────────────────────────────────────────
# 6. _make_retrying_message — error param (plan §3.5)
# ──────────────────────────────────────────────────────────────────────────────


class TestMakeRetryingMessage:

    def test_error_none_preserves_exact_old_text(self):
        from agent_cascade.engine.core import ExecutionEngine
        engine = ExecutionEngine.__new__(ExecutionEngine)
        m = engine._make_retrying_message(None, 1, 3, 2.5)  # error defaults to None
        assert m.content == '[RETRYING] Connection lost, retrying (1/3) in 2.5s...'

    def test_error_provided_uses_classified_reason(self):
        from agent_cascade.engine.core import ExecutionEngine
        engine = ExecutionEngine.__new__(ExecutionEngine)
        exc = _mse(code='502', exception=_status_error(502, 'llama-server unreachable'))
        m = engine._make_retrying_message(None, 1, 3, 2.5, error=exc)
        assert m.content.startswith('[RETRYING]')
        assert 'server error (HTTP 502)' in m.content
        assert 'retrying (1/3) in 2.5s...' in m.content


# ──────────────────────────────────────────────────────────────────────────────
# 7. Non-API Python module crashes — compact line + deduped DEBUG traceback
#    (plans/api_failure_feedback_GAP_REPORT.md R1-R6)
# ──────────────────────────────────────────────────────────────────────────────


def _fresh_dedup():
    """Reset the shared TB_DEDUP singleton so "exactly one DEBUG" assertions are deterministic."""
    from agent_cascade.error_reporting import TB_DEDUP
    TB_DEDUP.__init__()


class TestFormatCrash:
    """format_crash — generic (non-endpoint) root-cause line, stdlib-only leaf constraint."""

    def test_simple_exception_type_and_message(self):
        from agent_cascade.error_reporting import format_crash
        assert format_crash(ValueError('boom')) == 'ValueError: boom'

    def test_empty_message_returns_type_only(self):
        from agent_cascade.error_reporting import format_crash
        assert format_crash(KeyError()) == 'KeyError'

    def test_none_input_safe_fallback(self):
        from agent_cascade.error_reporting import format_crash
        assert format_crash(None) == 'no error information'

    def test_non_exception_described_safely(self):
        from agent_cascade.error_reporting import format_crash
        out = format_crash('not an exception')
        assert 'str: not an exception' in out

    def test_root_cause_walks_chain_to_innermost(self):
        """A chained (raise ... from) crash summarizes the INNERMOST root cause, not the wrapper."""
        from agent_cascade.error_reporting import format_crash
        try:
            try:
                raise ZeroDivisionError('division by zero')
            except ZeroDivisionError as inner:
                raise RuntimeError('outer wrapper') from inner
        except RuntimeError as e:
            # Root cause is the ZeroDivisionError, not the outer RuntimeError.
            assert format_crash(e) == 'ZeroDivisionError: division by zero'

    def test_message_truncated_to_max(self):
        from agent_cascade.error_reporting import MAX_MSG_CHARS, format_crash
        out = format_crash(ValueError('x' * 500))
        # Bounded to MAX_MSG_CHARS + the ellipsis marker.
        assert len(out) <= len('ValueError: ') + MAX_MSG_CHARS + 1
        assert out.endswith('…')

    def test_never_raises_on_hostile_str(self):
        """A type whose __str__ raises must not break reporting (defensive fallback)."""
        from agent_cascade.error_reporting import format_crash

        class Hostile(Exception):

            def __str__(self):
                raise RuntimeError('no str')

        out = format_crash(Hostile())
        assert isinstance(out, str) and len(out) > 0


def _make_agent_with_failing_tool(exc):
    """Build a minimal concrete Agent whose single tool always raises ``exc``.

    Uses BasicAgent (a concrete subclass of the abstract Agent) so instantiation succeeds, and
    passes llm=None so __init__ stays cheap. The tool subclasses BaseTool (required by _init_tool);
    only function_map matters to _call_tool, so the test is focused on the crash-handler logic.
    """
    from agent_cascade.agent import BasicAgent
    from agent_cascade.tools.base import BaseTool

    class _CrashTool(BaseTool):
        name = 'crashy'

        def call(self, params='{}', **kwargs):
            raise exc

    return BasicAgent(llm=None, function_list=[_CrashTool()])


class TestToolCrashFeedback:
    """R1/R2/R5 — Agent._call_tool crash handler (agent.py)."""

    def test_returns_compact_result_no_full_stack(self, caplog):
        """The LLM-facing tool result is a compact one-liner — NEVER the multi-frame stack."""
        import logging

        _fresh_dedup()
        agent = _make_agent_with_failing_tool(ValueError('boom'))
        with caplog.at_level(logging.DEBUG, logger='agent_cascade_logger'):
            result = agent._call_tool('crashy', '{}')

        # R2: compact — no traceback frames, no 'Traceback:' marker, single line.
        assert isinstance(result, str)
        assert 'ValueError: boom' in result
        assert 'Traceback' not in result
        assert 'File "' not in result
        assert '\n' not in result

    def test_warning_compact_and_debug_has_full_tb_once(self, caplog):
        """WARNING is a compact line; the full traceback appears at DEBUG exactly once (deduped)."""
        import logging

        _fresh_dedup()
        agent = _make_agent_with_failing_tool(ValueError('boom'))
        with caplog.at_level(logging.DEBUG, logger='agent_cascade_logger'):
            # Two identical crashes → the full traceback must be logged only once (dedup window).
            agent._call_tool('crashy', '{}')
            agent._call_tool('crashy', '{}')

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any('ValueError: boom' in w for w in warnings)
        # No WARNING line may embed a full stack.
        for w in warnings:
            assert 'Traceback' not in w

        debug_tbs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and 'full traceback' in r.getMessage()
        ]
        assert len(debug_tbs) == 1, f"expected exactly 1 deduped DEBUG traceback, got {len(debug_tbs)}"
        # The DEBUG block carries a real stack (multi-frame).
        assert 'Traceback' in debug_tbs[0] and '\n' in debug_tbs[0]


class TestDispatcherCrashFeedback:
    """R3 — tool_execution.py dispatcher crash handler uses the same shape as agent.py."""

    def test_dispatcher_crash_compact_and_deduped(self, caplog):
        import logging
        from types import SimpleNamespace

        import agent_cascade.engine.tool_execution as te
        from agent_cascade.agent_instance import AgentInstance
        from agent_cascade.llm.schema import Message

        _fresh_dedup()

        engine = object.__new__(te.ToolExecMixin)  # skip __init__ (heavy handlers)
        instance = AgentInstance(
            instance_name='test-inst',
            agent_class='coder',
            conversation=[],
            created_at=0.0,
            last_activity=0.0,
            latest_marker_index=0,
        )

        pool = SimpleNamespace()
        pool.get_template = lambda name: None  # no template → skip disabled-tool auto-deny path
        pool.get_instance = lambda name: instance  # is_terminated=False → no AgentTerminatedError
        pool.is_paused = lambda: False
        pool.settings = SimpleNamespace(cache_threshold_chars=10**9)  # _cache_tool_output threshold
        engine.pool = pool
        engine._telemetry = lambda: None
        engine.compression_handler = None  # skip _assemble_tool_result
        engine._is_terminal_stop = lambda name: False
        engine._is_suspended_by_compression = lambda name: False
        engine._is_stopped = lambda name: False
        engine._proactive_compression_check = lambda *a, **k: None
        engine._cache_tool_output = lambda *a, **k: None  # skip rolling-cache (defined on ExecutionEngine)

        # A tool call message the mixin's _detect_tool recognizes (reads msg.function_call).
        from agent_cascade.llm.schema import FunctionCall
        out = Message(role='assistant',
                      content='',
                      extra={'function_id': '1'},
                      function_call=FunctionCall(name='crashy', arguments='{}'))

        def failing_execute(*args, **kwargs):
            raise ValueError('dispatcher boom')

        engine.tool_dispatcher = SimpleNamespace(execute_tool=failing_execute)
        # _detect_tool is defined on the Agent base class; bind it onto the mixin instance.
        from agent_cascade.agent import Agent
        engine._detect_tool = Agent._detect_tool.__get__(engine, type(engine))
        # Stub _append_and_log (defined on ExecutionEngine) to just append so fn_msg is captured.
        appended = []
        engine._append_and_log = lambda inst, msg, **k: appended.append(msg)

        with caplog.at_level(logging.DEBUG, logger='agent_cascade_logger'):
            engine._execute_detected_tools(instance, 'test-inst', [out], [], [], [])

        # R3: the FUNCTION result is a compact one-liner — same shape as agent.py (no full stack).
        assert appended, 'expected a FUNCTION message to be appended'
        fn_content = appended[0].content
        assert isinstance(fn_content, str)
        assert 'ValueError: dispatcher boom' in fn_content
        assert 'Traceback' not in fn_content
        assert 'File "' not in fn_content

        # ERROR line is compact; full traceback at DEBUG exactly once (deduped).
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any('ValueError: dispatcher boom' in e for e in errors)
        debug_tbs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and 'full traceback' in r.getMessage()
        ]
        assert len(debug_tbs) == 1, f"expected exactly 1 deduped DEBUG traceback, got {len(debug_tbs)}"


class TestTopLevelCrashFeedback:
    """R4 — run_agent_unified.py top-level handler logs a compact line + deduped DEBUG traceback."""

    def test_top_level_handler_logs_traceback(self, caplog):
        import logging
        import threading
        from types import SimpleNamespace

        import agent_cascade.run_agent_unified as rau

        _fresh_dedup()

        # run_agent_thread_unified does a LOCAL `from .api_integration import ...`, so patching the
        # rau module attributes is shadowed. Patch the real api_integration module attrs instead.
        from agent_cascade import api_integration as ai
        _orig = (ai.run_agent_in_pool_with_recovery, ai.build_stream_update_from_pool)

        def boom(*args, **kwargs):
            raise RuntimeError('engine crash')

        ai.run_agent_in_pool_with_recovery = boom  # raises inside the try → outer except handler
        ai.build_stream_update_from_pool = lambda *a, **k: None  # broadcast path is a no-op here
        pool = SimpleNamespace(
            stopped=False,
            _run_generation=0,
            _instance_threads_lock=threading.Lock(),
            _instance_threads={},
            _halted_instances=set(),
            is_instance_terminated=lambda name: False,
            get_instance=lambda name: None,
        )

        try:
            with caplog.at_level(logging.DEBUG, logger='agent_cascade_logger'):
                # pool._execution is absent → hasattr guard skips it; then boom() raises inside the try.
                rau.run_agent_thread_unified(pool, 'test-inst', None, {}, None, None)
        finally:
            ai.run_agent_in_pool_with_recovery, ai.build_stream_update_from_pool = _orig

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any('engine crash' in e for e in errors), f"expected a compact ERROR line, got: {errors}"
        # R4: the full traceback is now present at DEBUG (was previously absent entirely).
        debug_tbs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and 'full traceback' in r.getMessage()
        ]
        assert len(debug_tbs) == 1, f"expected exactly 1 deduped DEBUG traceback, got {len(debug_tbs)}"
        assert 'Traceback' in debug_tbs[0]

    def test_top_level_error_message_is_compact(self):
        """The user-facing [SYSTEM ERROR] message carries the compact root cause, not a stack."""
        import threading
        from types import SimpleNamespace

        import agent_cascade.run_agent_unified as rau

        _fresh_dedup()

        captured = {}

        from agent_cascade import api_integration as ai
        _orig = (ai.run_agent_in_pool_with_recovery, ai.build_stream_update_from_pool)

        def boom(*args, **kwargs):
            raise RuntimeError('engine crash')

        # Capture the error_msg built in the handler via the broadcast path:
        # build_stream_update_from_pool receives [error_msg].
        def capture(pool, instance_name, responses, **k):
            captured['responses'] = responses
            return None  # returning None → no stream_update sent (send_queue is None anyway)

        ai.run_agent_in_pool_with_recovery = boom  # raises inside the try → outer except handler
        ai.build_stream_update_from_pool = capture
        pool = SimpleNamespace(
            stopped=False,
            _run_generation=0,
            _instance_threads_lock=threading.Lock(),
            _instance_threads={},
            _halted_instances=set(),
            is_instance_terminated=lambda name: False,
            get_instance=lambda name: None,
        )

        try:
            with caplog_dummy():  # noqa - silence logs; we only inspect captured content
                rau.run_agent_thread_unified(pool, 'test-inst', None, {}, None, None)
        finally:
            ai.run_agent_in_pool_with_recovery, ai.build_stream_update_from_pool = _orig

        responses = captured.get('responses') or []
        assert responses, 'expected the handler to build an error message'
        content = str(responses[0].content)
        assert '[SYSTEM ERROR:' in content
        assert 'engine crash' in content
        # Compact: no full stack embedded in the user-facing message.
        assert 'Traceback' not in content and 'File "' not in content


def caplog_dummy():
    """Minimal no-op context manager to keep logs quiet where only captured data is asserted."""

    class _Ctx:

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _Ctx()
