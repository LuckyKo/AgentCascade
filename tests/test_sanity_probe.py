"""Fix D — lightweight pre-allocation API sanity probe (subagent_timeout_fix_plan.md §5, v3).

Covers:
- _sanity_probe: success path (HTTP 200); 401/403 auth errors → False; 404 models not found → False;
  connection errors / exceptions → False; checks base_url/models.
- pre_validate_endpoint_chain: filters failing endpoints; respects cache TTL (no re-probe);
  disabled via SANITY_PROBE_ENABLED=False; blacklisted endpoints skipped WITHOUT probing;
  successful probe clears blacklist + failure count; ALL-endpoints-fail raises a clear error.

Uses fast GET /models requests (no LLM chat completions or model loading).
"""

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
        """A successful GET /models (HTTP 200) → True."""
        with patch('requests.get', return_value=_ok_response()) as mock_get:
            assert router._sanity_probe(_cfg()) is True
        assert mock_get.called
        url = mock_get.call_args[0][0]
        assert url == 'http://127.0.0.1:1234/v1/models'

    def test_401_auth_failure_returns_false(self, router):
        """HTTP 401 Unauthorized → False."""
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 401
        resp.text = '{"error": "Invalid API key"}'
        with patch('requests.get', return_value=resp):
            assert router._sanity_probe(_cfg()) is False

    def test_503_service_error_returns_false(self, router):
        """HTTP 503 Service Unavailable → False."""
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 503
        resp.text = '{"error": "Server unavailable"}'
        with patch('requests.get', return_value=resp):
            assert router._sanity_probe(_cfg()) is False

    def test_connection_error_returns_false(self, router):
        """Connection refused or timeout → False."""
        with patch('requests.get', side_effect=requests.exceptions.ConnectionError('Connection refused')):
            assert router._sanity_probe(_cfg()) is False

    def test_unexpected_exception_returns_false(self, router):
        """Any unexpected error → False."""
        with patch('requests.get', side_effect=ValueError('Unexpected error')):
            assert router._sanity_probe(_cfg()) is False

    def test_uses_configured_timeout(self, router):
        """Probe uses SANITY_PROBE_TIMEOUT_SECONDS."""
        with patch('requests.get', return_value=_ok_response()) as mock_get:
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

        with patch('requests.get', side_effect=[resp_404, resp_200]) as mock_get:
            assert router._sanity_probe(cfg) is True
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

        with patch('requests.get', return_value=resp_404) as mock_get:
            assert router._sanity_probe(cfg) is False
        assert mock_get.call_count == 1
        assert mock_get.call_args[0][0] == 'http://127.0.0.1:1234/v1/models'

    def test_no_auth_header_when_api_key_empty(self, router):
        """When api_key is not set or is 'EMPTY', no Authorization header is sent."""
        cfg = _cfg()
        # No api_key key at all in the config dict.
        with patch('requests.get', return_value=_ok_response()) as mock_get:
            assert router._sanity_probe(cfg) is True
        headers = mock_get.call_args.kwargs['headers']
        assert 'Authorization' not in headers

    def test_no_auth_header_when_api_key_is_empty_string(self, router):
        """api_key set to empty string → no Authorization header."""
        cfg = _cfg()
        cfg['api_key'] = ''
        with patch('requests.get', return_value=_ok_response()) as mock_get:
            assert router._sanity_probe(cfg) is True
        headers = mock_get.call_args.kwargs['headers']
        assert 'Authorization' not in headers

    def test_no_auth_header_when_api_key_is_empty_literal(self, router):
        """api_key set to the literal string 'EMPTY' → no Authorization header."""
        cfg = _cfg()
        cfg['api_key'] = 'EMPTY'
        with patch('requests.get', return_value=_ok_response()) as mock_get:
            assert router._sanity_probe(cfg) is True
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

        with patch('requests.get', side_effect=fake_get):
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
            with patch('requests.get') as mock_get:
                assert router.pre_validate_endpoint_chain([cfg], instance_name='inst1') == [cfg]
            assert mock_get.call_count == 0, \
                "a live connection must NOT be re-probed (the core flood fix)"
        finally:
            with router._lock:
                del router._instance_committed_endpoint['inst1']

    def test_no_live_marker_reprobes(self, router):
        """Part 2: without a live marker, the endpoint IS probed once."""
        cfg = _cfg()
        with patch('requests.get', return_value=_ok_response()) as mock_get:
            assert router.pre_validate_endpoint_chain([cfg], instance_name='inst1') == [cfg]
        assert mock_get.call_count == 1, "no live marker → probe once"

    def test_disabled_via_settings(self, router):
        """SANITY_PROBE_ENABLED=False → chain returned as-is, zero probes."""
        cfg = _cfg()
        with patch.object(router_mod, 'SANITY_PROBE_ENABLED', False), \
             patch('requests.get') as mock_get:
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
            with patch('requests.get', return_value=_ok_response()) as mock_get:
                result = router.pre_validate_endpoint_chain([bad, good])
            # Blacklisted endpoint skipped (no probe), healthy one probed and kept.
            assert result == [good]
            assert mock_get.call_count == 1, "blacklisted endpoint must not be probed"
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
            with patch('requests.get', return_value=_ok_response()):
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

        with patch('requests.get', side_effect=fake_get):
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

        with patch('requests.get', side_effect=fake_get):
            result = router.call_with_fallback('coder', call_fn)
        assert result == 'done'
        # The bad endpoint was filtered by the probe — only the default endpoint was called.
        assert 'bad-model' not in called
        assert len(called) == 1
