"""Fix D — pre-allocation API sanity probe (subagent_timeout_fix_plan.md §5, v3).

Covers:
- _sanity_probe: success path; ModelServiceError 4xx → False; non-4xx service error → False;
  unexpected exception → False; uses max_tokens=1 + request_timeout.
- pre_validate_endpoint_chain: filters failing endpoints; respects cache TTL (no re-probe);
  disabled via SANITY_PROBE_ENABLED=False; blacklisted endpoints skipped WITHOUT probing;
  successful probe clears blacklist + failure count; ALL-endpoints-fail raises a clear error.

No real LLM/network required: get_chat_model is mocked per test.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.api_router_pkg.normalization import normalize_api_base
from agent_cascade.llm.base import ModelServiceError
from tests.conftest import _add_endpoint  # noqa: F401 (shared helper; router fixture from conftest)

# Module under test — patch settings constants at module level (imported by name, same
# pattern as the breaker tests patching router_mod.BREAKER_BASE_WINDOW_SECONDS).
import agent_cascade.api_router_pkg.router as router_mod


def _cfg(base='http://127.0.0.1:1234/v1', model='test-model'):
    """Minimal endpoint cfg shaped like to_llm_cfg() output (carries both api_base keys)."""
    return {'api_base': base, 'model_server': base, 'model': model}


def _key(base, model):
    return (normalize_api_base(base), model)


def _ok_chat():
    """MagicMock llm whose chat() returns a single assistant message (non-streaming)."""
    llm = MagicMock()
    llm.chat.return_value = [MagicMock(role='assistant', content='ok')]
    return llm


# ============================================================================
# _sanity_probe — unit level
# ============================================================================

class TestSanityProbe:
    def test_success(self, router):
        """A successful minimal chat completion → True."""
        with patch('agent_cascade.llm.get_chat_model', return_value=_ok_chat()) as mock_gcm:
            assert router._sanity_probe(_cfg()) is True
        cfg_arg = mock_gcm.call_args[0][0]
        assert cfg_arg['model'] == 'test-model'

    def test_400_failure_returns_false(self, router):
        """ModelServiceError with a 4xx code (the incident's failure mode) → False."""
        llm = MagicMock()
        llm.chat.side_effect = ModelServiceError(
            code='400', message="Function tools with reasoning_effort are not supported")
        with patch('agent_cascade.llm.get_chat_model', return_value=llm):
            assert router._sanity_probe(_cfg()) is False

    def test_non_4xx_service_error_returns_false(self, router):
        """Non-4xx ModelServiceError (e.g. 503) → conservatively False."""
        llm = MagicMock()
        llm.chat.side_effect = ModelServiceError(code='503', message='server busy')
        with patch('agent_cascade.llm.get_chat_model', return_value=llm):
            assert router._sanity_probe(_cfg()) is False

    def test_unexpected_exception_returns_false(self, router):
        """Any unexpected error (bad cfg → ValueError, SDK crash, etc.) → False."""
        with patch('agent_cascade.llm.get_chat_model', side_effect=ValueError('Invalid model cfg')):
            assert router._sanity_probe(_cfg()) is False

    def test_uses_minimal_call_shape(self, router):
        """Probe must be non-streaming with max_tokens=1 and the configured timeout."""
        llm = _ok_chat()
        with patch('agent_cascade.llm.get_chat_model', return_value=llm):
            router._sanity_probe(_cfg())
        kwargs = llm.chat.call_args.kwargs
        assert kwargs['stream'] is False
        assert len(kwargs['functions']) == 1
        extra = kwargs['extra_generate_cfg']
        assert extra['max_tokens'] == 1
        assert extra['request_timeout'] == router_mod.SANITY_PROBE_TIMEOUT_SECONDS


# ============================================================================
# pre_validate_endpoint_chain — filtering, cache, blacklist, fail-loud
# ============================================================================

class TestPreValidateEndpointChain:
    def test_filters_failing_endpoint(self, router):
        """A 400-probe endpoint is dropped; a healthy one stays."""
        bad = _cfg(base='http://127.0.0.1:1235/v1', model='bad-model')
        good = _cfg()

        def fake_gcm(cfg):
            if cfg['api_base'] == 'http://127.0.0.1:1235/v1':
                m = MagicMock()
                m.chat.side_effect = ModelServiceError(code='400', message='not supported')
                return m
            return _ok_chat()

        with patch('agent_cascade.llm.get_chat_model', side_effect=fake_gcm):
            result = router.pre_validate_endpoint_chain([bad, good])
        assert result == [good]
        # Failure is cached so subsequent calls within TTL don't re-probe.
        assert _key(bad['api_base'], bad['model']) in router._probe_cache

    def test_cache_ttl_respected(self, router):
        """A successful probe is cached; a second call within TTL makes no new probe."""
        cfg = _cfg()
        with patch('agent_cascade.llm.get_chat_model', return_value=_ok_chat()) as mock_gcm:
            assert router.pre_validate_endpoint_chain([cfg]) == [cfg]
            assert router.pre_validate_endpoint_chain([cfg]) == [cfg]
        assert mock_gcm.call_count == 1, "second call within TTL must be served from cache"

    def test_cache_ttl_expiry_reprobes(self, router):
        """After the TTL elapses the endpoint is probed again."""
        cfg = _cfg()
        with patch('agent_cascade.llm.get_chat_model', return_value=_ok_chat()) as mock_gcm:
            assert router.pre_validate_endpoint_chain([cfg]) == [cfg]
            # Age the cache entry past the TTL.
            key = _key(cfg['api_base'], cfg['model'])
            with router._lock:
                success, ts = router._probe_cache[key]
                router._probe_cache[key] = (success, ts - router_mod.SANITY_PROBE_TTL_SECONDS - 1)
            assert router.pre_validate_endpoint_chain([cfg]) == [cfg]
        assert mock_gcm.call_count == 2

    def test_disabled_via_settings(self, router):
        """SANITY_PROBE_ENABLED=False → chain returned as-is, zero probes."""
        cfg = _cfg()
        with patch.object(router_mod, 'SANITY_PROBE_ENABLED', False), \
             patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            assert router.pre_validate_endpoint_chain([cfg]) == [cfg]
            assert mock_gcm.call_count == 0

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
            with patch('agent_cascade.llm.get_chat_model', return_value=_ok_chat()) as mock_gcm:
                result = router.pre_validate_endpoint_chain([bad, good])
            # Blacklisted endpoint skipped (no probe), healthy one probed and kept.
            assert result == [good]
            assert mock_gcm.call_count == 1, "blacklisted endpoint must not be probed"
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
            with patch('agent_cascade.llm.get_chat_model', return_value=_ok_chat()):
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

        def fake_gcm(cfg):
            m = MagicMock()
            m.chat.side_effect = ModelServiceError(code='400', message='not supported')
            return m

        with patch('agent_cascade.llm.get_chat_model', side_effect=fake_gcm):
            with pytest.raises(RuntimeError, match='sanity probe'):
                router.pre_validate_endpoint_chain([bad1, bad2])

    def test_empty_chain_passthrough(self, router):
        """Empty chain → returned as-is (get_endpoint_chain already raises on empty)."""
        assert router.pre_validate_endpoint_chain([]) == []


# ============================================================================
# call_with_fallback integration — probe runs after get_endpoint_chain
# ============================================================================

class TestCallWithFallbackIntegration:
    def test_probe_runs_and_skips_bad_endpoint(self, router):
        """call_with_fallback probes the chain; a 400-probe endpoint is never called."""
        _add_endpoint(router, 'bad', 'http://127.0.0.1:1235/v1', model='bad-model')
        router.set_agent_priorities('coder', [router.list_endpoints()[-1].id])

        called = []

        def fake_gcm(cfg):
            if cfg.get('model') == 'bad-model':
                m = MagicMock()
                m.chat.side_effect = ModelServiceError(code='400', message='not supported')
                return m
            return _ok_chat()

        def call_fn(llm_cfg, *a, **k):
            called.append(llm_cfg.get('model'))
            return 'done'

        with patch('agent_cascade.llm.get_chat_model', side_effect=fake_gcm):
            result = router.call_with_fallback('coder', call_fn)
        assert result == 'done'
        # The bad endpoint was filtered by the probe — only the default endpoint was called.
        assert 'bad-model' not in called
        assert len(called) == 1
