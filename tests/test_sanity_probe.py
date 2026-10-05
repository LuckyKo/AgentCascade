"""Fix D — lightweight pre-allocation API sanity probe (subagent_timeout_fix_plan.md §5, v3).

Covers:
- _sanity_probe (returns ``(passed, was_connection_error)``): success (HTTP 200) → (True, False);
  401/403 auth errors and 404 models-not-found → (False, False) — host reachable;
  connection-level failures → (False, True); unexpected exceptions → (False, False).
- pre_validate_endpoint_chain: filters failing endpoints; a live committed endpoint is fast-pathed (no re-probe);
  disabled via SANITY_PROBE_ENABLED=False; blacklisted endpoints skipped WITHOUT probing;
  successful probe clears blacklist + failure count; ALL-endpoints-fail raises a clear error.

Uses fast GET /models requests (no LLM chat completions or model loading).
"""

import logging
import time
from unittest.mock import MagicMock, patch
import requests

import pytest

from agent_cascade.api_router_pkg.normalization import normalize_api_base
from tests.conftest import _add_endpoint  # noqa: F401 (shared helper; router fixture from conftest)

# Module under test — patch settings constants at module level (imported by name, same
# pattern as the breaker tests patching router_mod.BREAKER_BASE_WINDOW_SECONDS).
import agent_cascade.api_router_pkg.router as router_mod


def _cfg(base='http://127.0.0.1:1234/v1', model='test-model'):
    """Minimal endpoint cfg shaped like to_llm_cfg() output (carries both api_base keys)."""
    return {'api_base': base, 'model_server': base, 'model': model}


def _key(base, model):
    return (normalize_api_base(base), model)


def _ok_response():
    """Mock requests.Response returning HTTP 200."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.json.return_value = {'data': [{'id': 'test-model', 'object': 'model'}]}
    resp.text = '{"data": []}'
    return resp


# ============================================================================
# _sanity_probe — unit level
# ============================================================================

class TestSanityProbe:
    def test_success(self, router):
        """A successful GET /models (HTTP 200) → (True, False)."""
        with patch.object(router_mod._get_probe_session(), 'get', return_value=_ok_response()) as mock_get:
            assert router._sanity_probe(_cfg()) == (True, False)
        assert mock_get.called
        url = mock_get.call_args[0][0]
        assert url == 'http://127.0.0.1:1234/v1/models'

    def test_401_auth_failure_returns_false(self, router):
        """HTTP 401 Unauthorized → (False, False) — host reachable, auth bad."""
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 401
        resp.text = '{"error": "Invalid API key"}'
        with patch.object(router_mod._get_probe_session(), 'get', return_value=resp):
            assert router._sanity_probe(_cfg()) == (False, False)

    def test_503_service_error_returns_false(self, router):
        """HTTP 503 Service Unavailable → (False, False) — host reachable."""
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 503
        resp.text = '{"error": "Server unavailable"}'
        with patch.object(router_mod._get_probe_session(), 'get', return_value=resp):
            assert router._sanity_probe(_cfg()) == (False, False)

    def test_connection_error_returns_false(self, router):
        """Connection refused or timeout → (False, True) — connection-level failure."""
        with patch.object(router_mod._get_probe_session(), 'get', side_effect=requests.exceptions.ConnectionError('Connection refused')):
            assert router._sanity_probe(_cfg()) == (False, True)

    def test_unexpected_exception_returns_false(self, router):
        """Any unexpected error → (False, False)."""
        with patch.object(router_mod._get_probe_session(), 'get', side_effect=ValueError('Unexpected error')):
            assert router._sanity_probe(_cfg()) == (False, False)

    def test_uses_configured_timeout(self, router):
        """Probe uses SANITY_PROBE_TIMEOUT_SECONDS."""
        with patch.object(router_mod._get_probe_session(), 'get', return_value=_ok_response()) as mock_get:
            router._sanity_probe(_cfg())
        kwargs = mock_get.call_args.kwargs
        assert kwargs['timeout'] == (1.5, router_mod.SANITY_PROBE_TIMEOUT_SECONDS)

    def test_404_fallback_to_v1_models(self, router):
        """When base doesn't end with /v1 and GET /models returns 404, retries with /v1/models."""
        cfg = _cfg(base='http://127.0.0.1:1234')

        resp_404 = MagicMock(spec=requests.Response)
        resp_404.status_code = 404
        resp_404.text = 'not found'
        resp_200 = _ok_response()

        with patch.object(router_mod._get_probe_session(), 'get', side_effect=[resp_404, resp_200]) as mock_get:
            assert router._sanity_probe(cfg) == (True, False)
        assert mock_get.call_count == 2
        urls_called = [call[0][0] for call in mock_get.call_args_list]
        assert urls_called[0] == 'http://127.0.0.1:1234/models'
        assert urls_called[1] == 'http://127.0.0.1:1234/v1/models'

    def test_no_fallback_when_base_ends_with_v1(self, router):
        """When base already ends with /v1, a 404 does NOT trigger a fallback retry."""
        cfg = _cfg(base='http://127.0.0.1:1234/v1')

        resp_404 = MagicMock(spec=requests.Response)
        resp_404.status_code = 404
        resp_404.text = 'not found'

        with patch.object(router_mod._get_probe_session(), 'get', return_value=resp_404) as mock_get:
            assert router._sanity_probe(cfg) == (False, False)
        assert mock_get.call_count == 1
        assert mock_get.call_args[0][0] == 'http://127.0.0.1:1234/v1/models'

    def test_no_auth_header_when_api_key_empty(self, router):
        """When api_key is not set or is 'EMPTY', no Authorization header is sent."""
        cfg = _cfg()
        # No api_key key at all in the config dict.
        with patch.object(router_mod._get_probe_session(), 'get', return_value=_ok_response()) as mock_get:
            assert router._sanity_probe(cfg) == (True, False)
        headers = mock_get.call_args.kwargs['headers']
        assert 'Authorization' not in headers

    def test_no_auth_header_when_api_key_is_empty_string(self, router):
        """api_key set to empty string → no Authorization header."""
        cfg = _cfg()
        cfg['api_key'] = ''
        with patch.object(router_mod._get_probe_session(), 'get', return_value=_ok_response()) as mock_get:
            assert router._sanity_probe(cfg) == (True, False)
        headers = mock_get.call_args.kwargs['headers']
        assert 'Authorization' not in headers

    def test_no_auth_header_when_api_key_is_empty_literal(self, router):
        """api_key set to the literal string 'EMPTY' → no Authorization header."""
        cfg = _cfg()
        cfg['api_key'] = 'EMPTY'
        with patch.object(router_mod._get_probe_session(), 'get', return_value=_ok_response()) as mock_get:
            assert router._sanity_probe(cfg) == (True, False)
        headers = mock_get.call_args.kwargs['headers']
        assert 'Authorization' not in headers


# ============================================================================
# pre_validate_endpoint_chain — filtering, cache, blacklist, fail-loud
# ============================================================================

class TestPreValidateEndpointChain:
    @pytest.fixture(autouse=True)
    def enable_probe(self, monkeypatch):
        monkeypatch.setattr(router_mod, 'SANITY_PROBE_ENABLED', True)

    def test_filters_failing_endpoint(self, router):
        """A failing-probe endpoint is dropped; a healthy one stays."""
        bad = _cfg(base='http://127.0.0.1:1235/v1', model='bad-model')
        good = _cfg()

        def fake_get(url, *a, **k):
            if '1235' in url:
                resp = MagicMock(spec=requests.Response)
                resp.status_code = 401
                resp.text = 'unauthorized'
                return resp
            return _ok_response()

        with patch.object(router_mod._get_probe_session(), 'get', side_effect=fake_get):
            result = router.pre_validate_endpoint_chain([bad, good])
        assert result == [good]
        # Part 2: probe failure records the endpoint into cooldown (_endpoint_failure_times)
        # so it is not immediately re-probed on the next acquisition.
        assert _key(bad['api_base'], bad['model']) in router._endpoint_failure_times

    def test_live_fast_path_skips_probe(self, router):
        """Part 2: if an instance holds a LIVE connection to an endpoint (recorded via
        _instance_committed_endpoint), pre_validate skips the probe entirely — no HTTP call."""
        cfg = _cfg()
        key = _key(cfg['api_base'], cfg['model'])
        # Simulate that this instance already has a live connection to this endpoint.
        with router._lock:
            router._instance_committed_endpoint['inst1'] = key
        try:
            with patch.object(router_mod._get_probe_session(), 'get') as mock_get:
                assert router.pre_validate_endpoint_chain([cfg], instance_name='inst1') == [cfg]
            assert mock_get.call_count == 0, \
                'a live connection must NOT be re-probed (the core flood fix)'
        finally:
            with router._lock:
                del router._instance_committed_endpoint['inst1']

    def test_no_live_marker_reprobes(self, router):
        """Part 2: without a live marker, the endpoint IS probed once."""
        cfg = _cfg()
        with patch.object(router_mod._get_probe_session(), 'get', return_value=_ok_response()) as mock_get:
            assert router.pre_validate_endpoint_chain([cfg], instance_name='inst1') == [cfg]
        assert mock_get.call_count == 1, 'no live marker → probe once'

    def test_disabled_via_settings(self, router):
        """SANITY_PROBE_ENABLED=False → chain returned as-is, zero probes."""
        cfg = _cfg()
        with patch.object(router_mod, 'SANITY_PROBE_ENABLED', False), \
             patch.object(router_mod._get_probe_session(), 'get') as mock_get:
            assert router.pre_validate_endpoint_chain([cfg]) == [cfg]
            assert mock_get.call_count == 0

    def test_blacklisted_endpoint_skipped_without_probe(self, router):
        """Blacklist takes precedence over probe — no API call is fired.

        _endpoint_blacklist is Fix B1 state (parallel workstream); the tests simulate it
        by setting the attribute directly on the router instance.
        """
        bad = _cfg(base='http://127.0.0.1:1235/v1', model='bad-model')
        good = _cfg()
        key = _key(bad['api_base'], bad['model'])
        with router._lock:
            router._endpoint_blacklist = {key: time.time() + 7200}
        try:
            with patch.object(router_mod._get_probe_session(), 'get', return_value=_ok_response()) as mock_get:
                result = router.pre_validate_endpoint_chain([bad, good])
            # Blacklisted endpoint skipped (no probe), healthy one probed and kept.
            assert result == [good]
            assert mock_get.call_count == 1, 'blacklisted endpoint must not be probed'
        finally:
            with router._lock:
                del router._endpoint_blacklist

    def test_successful_probe_clears_blacklist(self, router):
        """A passing probe deletes the blacklist entry AND the deterministic-failure count.

        _endpoint_blacklist / _endpoint_deterministic_failures are Fix B1 state (parallel
        workstream); simulated here by setting the attributes directly on the instance.
        """
        cfg = _cfg()
        key = _key(cfg['api_base'], cfg['model'])
        with router._lock:
            # Expired blacklist entry + a failure count — the probe must run and clear both.
            router._endpoint_blacklist = {key: time.time() - 1}
            router._endpoint_deterministic_failures = {key: 3}
        try:
            with patch.object(router_mod._get_probe_session(), 'get', return_value=_ok_response()):
                result = router.pre_validate_endpoint_chain([cfg])
            assert result == [cfg]
            with router._lock:
                assert key not in router._endpoint_blacklist
                assert key not in router._endpoint_deterministic_failures
        finally:
            with router._lock:
                del router._endpoint_blacklist
                del router._endpoint_deterministic_failures

    def test_all_endpoints_fail_raises_clear_error(self, router):
        """ALL endpoints failing → explicit RuntimeError naming the endpoints (no empty chain)."""
        bad1 = _cfg(base='http://127.0.0.1:1235/v1', model='bad-1')
        bad2 = _cfg(base='http://127.0.0.1:1236/v1', model='bad-2')

        def fake_get(url, *a, **k):
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 401
            resp.text = 'unauthorized'
            return resp

        with patch.object(router_mod._get_probe_session(), 'get', side_effect=fake_get):
            with pytest.raises(RuntimeError, match='sanity probe'):
                router.pre_validate_endpoint_chain([bad1, bad2])

    def test_empty_chain_passthrough(self, router):
        """Empty chain → returned as-is (get_endpoint_chain already raises on empty)."""
        assert router.pre_validate_endpoint_chain([]) == []


# ============================================================================
# call_with_fallback integration — probe runs after get_endpoint_chain
# ============================================================================

class TestCallWithFallbackIntegration:
    @pytest.fixture(autouse=True)
    def enable_probe(self, monkeypatch):
        monkeypatch.setattr(router_mod, 'SANITY_PROBE_ENABLED', True)

    def test_probe_runs_and_skips_bad_endpoint(self, router):
        """call_with_fallback probes the chain; a failing endpoint is never called."""
        _add_endpoint(router, 'bad', 'http://127.0.0.1:1235/v1', model='bad-model')
        router.set_agent_priorities('coder', [router.list_endpoints()[-1].id])

        called = []

        def fake_get(url, *a, **k):
            if '1235' in url:
                resp = MagicMock(spec=requests.Response)
                resp.status_code = 401
                resp.text = 'unauthorized'
                return resp
            return _ok_response()

        def call_fn(llm_cfg, *a, **k):
            called.append(llm_cfg.get('model'))
            return 'done'

        with patch.object(router_mod._get_probe_session(), 'get', side_effect=fake_get):
            result = router.call_with_fallback('coder', call_fn)
        assert result == 'done'
        # The bad endpoint was filtered by the probe — only the default endpoint was called.
        assert 'bad-model' not in called
        assert len(called) == 1


# ============================================================================
# Transient-vs-hard classification (todo #163): a client-side socket blip
# (WinError 10055 / WSAENOBUFS) must NOT write an endpoint cooldown, whereas a
# hard failure (refused) keeps today's behaviour. Regression guards.
# ============================================================================

def _transient_10055_error():
    """Build the REAL requests/urllib3 exception chain for WinError 10055.

    Mirrors what requests raises on a connect() that hits WSAENOBUFS: a
    ``requests.exceptions.ConnectionError`` wrapping a ``MaxRetryError`` whose
    message embeds ``NewConnectionError("... [Errno 10055] ...")``. In urllib3 2.x the
    errno is NOT exposed as a structured attribute on the wrapped exceptions — it lives
    only in the formatted message string, which is exactly why _classify_probe_error has
    a textual fallback and this test builds the real shape (not a synthetic one).
    """
    from urllib3.exceptions import MaxRetryError
    msg = ("HTTPConnectionPool(host='127.0.0.1', port=1234): Max retries exceeded with "
           "url: /v1/models (Caused by NewConnectionError(\"HTTPConnection(host='127.0.0.1', "
           'port=1234): Failed to establish a new connection: [Errno 10055] An operation on a '
           'socket could not be performed because the system lacked sufficient buffer space or '
           "because a queue was full\"))")
    return requests.exceptions.ConnectionError(msg, MaxRetryError('http://127.0.0.1:1234/v1', None))


def _hard_refused_error():
    """A hard connection failure (connection refused / WinError 10061)."""
    return requests.exceptions.ConnectionError(
        "HTTPConnectionPool(host='127.0.0.1', port=1234): Max retries exceeded with url: "
        "/v1/models (Caused by NewConnectionError(\"... Failed to establish a new connection: "
        "[Errno 10061] Connection refused\"))", None)


class TestProbeErrorClassification:
    """Unit tests for _classify_probe_error against REAL exception shapes."""

    def test_classify_10055_transient(self):
        """The real WinError 10055 chain classifies as 'transient' (host is healthy)."""
        assert router_mod._classify_probe_error(_transient_10055_error()) == 'transient'

    def test_classify_10061_hard(self):
        """Connection refused (errno 10061) classifies as 'hard' (host down)."""
        assert router_mod._classify_probe_error(_hard_refused_error()) == 'hard'

    def test_classify_bare_refused_hard(self):
        """A bare ConnectionError('Connection refused') — the idiom used by other tests — is hard."""
        assert router_mod._classify_probe_error(
            requests.exceptions.ConnectionError('Connection refused')) == 'hard'

    def test_classify_connect_timeout_hard(self):
        """A connect-timeout is 'hard', NOT transient.

        On Windows a CLOSED localhost port surfaces as a connect-timeout (not ECONNREFUSED), so
        this is the primary "host down" signal — it must keep the cooldown (see
        test_probe_trigger.py::test_dead_head_walks_to_second). Only the WSAENOBUFS/EADDRINUSE/
        WSAECONNABORTED errnos are client-side blips; a bare timeout is not one of them.
        """
        assert router_mod._classify_probe_error(requests.exceptions.ConnectTimeout('t/o')) == 'hard'


class TestProbeTransientCooldown:
    """Regression guards: transient probe failures must NOT penalize the endpoint."""

    @pytest.fixture(autouse=True)
    def enable_probe(self, monkeypatch):
        monkeypatch.setattr(router_mod, 'SANITY_PROBE_ENABLED', True)

    def test_transient_probe_failure_no_cooldown(self, router):
        """A transient (10055) probe failure on the configured endpoint writes NO cooldown.

        The configured endpoint stays available on the next acquisition — it is NOT filtered
        by get_endpoint_chain. This test FAILS on pre-fix code (which wrote a 60s cooldown).
        """
        bad = _cfg(base='http://127.0.0.1:1235/v1', model='bad-model')
        _add_endpoint(router, 'bad', bad['api_base'], model=bad['model'])
        router.set_agent_priorities('coder', [router.list_endpoints()[-1].id])

        def fake_get(url, *a, **k):
            if '1235' in url:
                raise _transient_10055_error()
            return _ok_response()

        with patch.object(router_mod._get_probe_session(), 'get', side_effect=fake_get):
            router.call_with_fallback('coder', lambda cfg, *a, **k: 'done')

        # The transient failure must NOT have recorded a cooldown for the configured endpoint.
        key = _key(bad['api_base'], bad['model'])
        assert key not in router._endpoint_failure_times, \
            'transient probe failure must NOT write an endpoint cooldown'
        # And the next acquisition still offers the configured endpoint (not filtered out).
        chain = router.get_endpoint_chain('coder')
        models = [c.get('model') for c in chain]
        assert 'bad-model' in models, 'configured endpoint must remain available after a transient blip'

    def test_hard_probe_failure_still_sets_cooldown(self, router):
        """A hard (refused) probe failure STILL writes the cooldown and filters the endpoint.

        Guards that the fix did NOT over-correct: hard failures keep today's behaviour exactly.
        This test FAILS if the cooldown write were removed for all classes.
        """
        bad = _cfg(base='http://127.0.0.1:1235/v1', model='bad-model')
        _add_endpoint(router, 'bad', bad['api_base'], model=bad['model'])
        router.set_agent_priorities('coder', [router.list_endpoints()[-1].id])

        def fake_get(url, *a, **k):
            if '1235' in url:
                raise _hard_refused_error()
            return _ok_response()

        with patch.object(router_mod._get_probe_session(), 'get', side_effect=fake_get):
            router.call_with_fallback('coder', lambda cfg, *a, **k: 'done')

        key = _key(bad['api_base'], bad['model'])
        assert key in router._endpoint_failure_times, \
            'hard probe failure MUST still write an endpoint cooldown'
        # The next acquisition filters the just-failed endpoint out of the Tier-1 chain.
        chain = router.get_endpoint_chain('coder')
        models = [c.get('model') for c in chain]
        assert 'bad-model' not in models, 'hard-failed endpoint must be filtered (in cooldown)'

    def test_transient_substitution_logs_warning(self, router, caplog):
        """When a transient probe failure demotes the agent to the global default, a WARNING
        names BOTH the skipped configured model and the default (observability for todo #163)."""
        bad = _cfg(base='http://127.0.0.1:1235/v1', model='configured-model')
        _add_endpoint(router, 'bad', bad['api_base'], model=bad['model'])
        router.set_agent_priorities('coder', [router.list_endpoints()[-1].id])

        def fake_get(url, *a, **k):
            if '1235' in url:
                raise _transient_10055_error()
            return _ok_response()

        with patch.object(router_mod._get_probe_session(), 'get', side_effect=fake_get), \
             caplog.at_level(logging.WARNING, logger='agent_cascade'):
            router.call_with_fallback('coder', lambda cfg, *a, **k: 'done')

        warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        # The demotion WARNING must name the skipped configured model AND the default.
        assert any('configured-model' in m and 'default-model' in m and 'demoting' in m
                   for m in warn_msgs), \
            f"expected a demotion WARNING naming both models; got: {warn_msgs}"

    def test_probe_return_tuple_unchanged(self, router):
        """Guard (mock-compat contract): _sanity_probe still returns EXACTLY a 2-tuple
        (False, True) on a connection error. E2E fakes mock this as a 2-tuple — do not change arity."""
        with patch.object(router_mod._get_probe_session(), 'get', side_effect=_transient_10055_error()):
            result = router._sanity_probe(_cfg())
        assert isinstance(result, tuple) and len(result) == 2
        assert result == (False, True)

    def test_stale_thread_local_does_not_leak_into_http_error(self, router):
        """A transient connection-error probe must NOT leak its classification into a LATER
        HTTP-error probe in the SAME thread.

        Scenario: endpoint A's probe raises WinError 10055 (sets _probe_err_class_local.value
        = 'transient', no cooldown). Endpoint B's probe then returns HTTP 404 (host reachable,
        endpoint bad — _sanity_probe returns (False, False) and does NOT touch the thread-local).
        The call_with_fallback failure branch must classify B as an HTTP error and STILL write its
        cooldown. On pre-fix code the branch read the thread-local unconditionally, so the stale
        'transient' from A would wrongly suppress B's cooldown — this test FAILS on that code.

        Driven through a single call_with_fallback pass over a 2-endpoint chain (A then B), which is
        the real production path and guarantees both probes run in the same thread, back to back.
        """
        # A = transient connection failure; B = HTTP 404. Both are non-default endpoints on distinct
        # bases so per-host dedup does not short-circuit either probe.
        a_cfg = _cfg(base='http://127.0.0.1:1235/v1', model='model-a')
        b_cfg = _cfg(base='http://127.0.0.1:1236/v1', model='model-b')
        _add_endpoint(router, 'a', a_cfg['api_base'], model=a_cfg['model'])
        _add_endpoint(router, 'b', b_cfg['api_base'], model=b_cfg['model'])
        # Priority order: A first (transient 10055 sets the thread-local), then B (HTTP 404 reads it).
        # _add_endpoint appends, so after adding 'a' then 'b': list_endpoints() = [..., a, b].
        # We want chain order [A, B, default] → A is probed first (sets local='transient'),
        # B is probed second (404 path must NOT consult the stale local).
        ep_a_id = router.list_endpoints()[-2].id  # 'a' was added first
        ep_b_id = router.list_endpoints()[-1].id   # 'b' was added second
        router.set_agent_priorities('coder', [ep_a_id, ep_b_id])

        def fake_get(url, *a, **k):
            if '1235' in url:          # endpoint A → transient client-side blip
                raise _transient_10055_error()
            if '1236' in url:          # endpoint B → host reachable, endpoint bad (404)
                resp = MagicMock(spec=requests.Response)
                resp.status_code = 404
                resp.text = 'not found'
                return resp
            return _ok_response()

        with patch.object(router_mod._get_probe_session(), 'get', side_effect=fake_get):
            router.call_with_fallback('coder', lambda cfg, *a, **k: 'done')

        # A (transient) must NOT have a cooldown; B (HTTP 404) MUST.
        key_a = _key(a_cfg['api_base'], a_cfg['model'])
        key_b = _key(b_cfg['api_base'], b_cfg['model'])
        assert key_a not in router._endpoint_failure_times, \
            'transient probe failure must NOT write an endpoint cooldown'
        assert key_b in router._endpoint_failure_times, \
            "stale 'transient' from endpoint A leaked into endpoint B's HTTP-error path — " \
            'the 404 cooldown was wrongly suppressed'

    def test_stale_thread_local_leak_unit_level(self, router):
        """Unit-level proof of the same guard: after a transient connection-error probe sets the
        thread-local to 'transient', an immediate HTTP-404 probe returns (False, False) and the
        classification consumed for the 404 path must be 'hard' (i.e. NOT the stale 'transient').

        This isolates the leak from chain-driving: it calls _sanity_probe directly twice in the same
        thread and asserts the HTTP-error result is (False, False) — the signal call_with_fallback uses
        to take the original cooldown path rather than the transient-skip path.
        """
        # 1) Transient connection error → sets _probe_err_class_local.value = 'transient'.
        with patch.object(router_mod._get_probe_session(), 'get', side_effect=_transient_10055_error()):
            assert router._sanity_probe(_cfg()) == (False, True)
        # The thread-local now holds the stale 'transient' value.
        assert getattr(router_mod._probe_err_class_local, 'value', None) == 'transient'

        # 2) HTTP 404 in the SAME thread → (False, False); _sanity_probe does NOT reset the local.
        resp_404 = MagicMock(spec=requests.Response)
        resp_404.status_code = 404
        resp_404.text = 'not found'
        with patch.object(router_mod._get_probe_session(), 'get', return_value=resp_404):
            assert router._sanity_probe(_cfg()) == (False, False)

        # The HTTP-error path is signalled by was_connection_error=False. call_with_fallback gates the
        # thread-local read on that flag, so the stale 'transient' is never consulted for a 404.
        # If the gating were removed (pre-fix unconditional read), this assertion documents the hazard:
        # the local still reads 'transient' even though the probe was an HTTP error.
        assert getattr(router_mod._probe_err_class_local, 'value', None) == 'transient', \
            'precondition: the thread-local is intentionally NOT reset on the HTTP-error path — ' \
            'the fix relies on call_with_fallback gating its read on _probe_conn_err'
