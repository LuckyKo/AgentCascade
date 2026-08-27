"""Cursor rotation and fallback chain tests for APIRouter.

Tests cover:
- Full fallback chain (own endpoints → last-successful → default) with simulated failures
- Instance cursor persistence across retries/failures
- Cursor reset after successful endpoint usage
- No caller inheritance: unconfigured agents fall to their own/default pool
- Cooldown filtering during chain construction

No LLM or network connections required. Uses mocks to simulate failures.
"""

import copy
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.api_router import APIRouter, APIEndpoint
from agent_cascade.retry_policy import RetryPolicy
from agent_cascade.api_router_pkg.normalization import normalize_api_base


# ============================================================================
# Fixtures and helpers
# ============================================================================

# Fast retry policy for tests — minimal backoff so tests run quickly.
FAST_RETRY_POLICY = RetryPolicy(
    retry_max_attempts=3,
    base_delay=0.01,      # 10ms instead of 1s
    max_delay=0.05,       # 50ms cap
    jitter_factor=0.0,    # No jitter for deterministic timing
    endpoint_max_retries=1,
)


@pytest.fixture(autouse=True)
def _disable_sanity_probe():
    """Disable the lazy per-endpoint sanity probe for every test in this module.

    These tests use unreachable fake endpoints (http://a-api, http://default-api, ...)
    and exercise call_with_fallback's cursor/fallback logic — not endpoint validation.
    The lazy probe (inside call_with_fallback's endpoint loop) would issue a REAL HTTP
    GET /models to each endpoint just before trying it and SKIP endpoints that fail
    probing, pruning the fake chain. Disabling SANITY_PROBE_ENABLED keeps the chains
    intact. Probe coverage lives in tests/test_sanity_probe.py and
    tests/test_probe_trigger.py."""
    import agent_cascade.api_router_pkg.router as router_mod
    orig = router_mod.SANITY_PROBE_ENABLED
    router_mod.SANITY_PROBE_ENABLED = False
    try:
        yield
    finally:
        router_mod.SANITY_PROBE_ENABLED = orig


@pytest.fixture
def router(tmp_path_factory):
    """Create an isolated APIRouter instance with its own config dir."""
    test_config_dir = str(tmp_path_factory.mktemp("api_router_test"))
    
    with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_config_dir}):
        r = APIRouter(default_llm_cfg={
            'api_base': 'http://default-api',
            'model': 'default-model',
            'max_tokens': 2048,
        }, policy=FAST_RETRY_POLICY)
        # Initialize _pool to None so call_with_fallback termination checks work
        # without requiring the full AgentPool wiring. Production code guards all
        # _pool accesses with "if self._pool and ..." checks.
        r._pool = None
        yield r


def _add_endpoint(router, name, api_base, model='test-model', enabled=True,
                  concurrency_limit=-1, max_retries=3, rate_limit_rpm=0):
    """Helper to add an endpoint to the router."""
    ep = APIEndpoint(
        id=f"ep_{name}",
        name=name,
        api_base=api_base,
        model=model,
        enabled=enabled,
        concurrency_limit=concurrency_limit,
        max_retries=max_retries,
        rate_limit_rpm=rate_limit_rpm,
    )
    router.add_endpoint(ep)


def _set_agent_priorities(router, agent_type, endpoint_ids):
    """Helper to set agent-specific endpoint priorities."""
    router.set_agent_priorities(agent_type, endpoint_ids)


# ============================================================================
# Full 4-tier fallback chain tests
# ============================================================================

class TestFourTierFallbackChain:
    """Test the complete 4-tier fallback chain behavior."""

    def test_chain_includes_all_tiers(self, router):
        """Chain includes agent-specific (T1), last-successful (T3), and default (T4)."""
        _add_endpoint(router, "agent_ep", "http://agent-api")
        _set_agent_priorities(router, "coder", ["ep_agent_ep"])

        # Set last successful endpoint (Tier 3)
        with router._lock:
            router._last_successful_endpoint_cfg = {
                'api_base': 'http://last-successful',
                'model': 'success-model',
            }
            router._agent_types_with_priorities.add('coder')

        chain = router.get_endpoint_chain("coder")

        # Should have: agent_ep (T1), last-successful (T3), default (T4)
        assert len(chain) >= 2, f"Expected at least 2 endpoints, got {len(chain)}"

        # Default should always be last
        assert chain[-1]['api_base'] == 'http://default-api'

    def test_tier1_agent_specific_first(self, router):
        """Agent-specific endpoints (Tier 1) are tried first."""
        _add_endpoint(router, "coder_ep", "http://coder-api")
        _set_agent_priorities(router, "coder", ["ep_coder_ep"])

        chain = router.get_endpoint_chain("coder")

        assert chain[0]['api_base'] == 'http://coder-api', \
            f"Tier 1 endpoint should be first, got {chain[0].get('api_base')}"

    def test_no_caller_inheritance(self, router):
        """An unconfigured agent does NOT inherit the caller's endpoints — it falls to default.

        Regression: Tier-2 caller inheritance was removed so every agent meters against its own
        resolved endpoint pool (single FIFO slot system). An agent with no own priorities must not
        pick up a caller's endpoint; it resolves through its own chain (Tier 3/Tier 4) only.
        """
        _add_endpoint(router, "caller_ep", "http://caller-api")
        _set_agent_priorities(router, "orchestrator", ["ep_caller_ep"])

        # 'generalist' has no configured priorities — must NOT inherit caller's endpoint
        chain = router.get_endpoint_chain("generalist")

        api_bases = [cfg.get('api_base') for cfg in chain]
        assert 'http://caller-api' not in api_bases, \
            f"Caller's endpoint should NOT be inherited: {api_bases}"
        # Falls through to the global default
        assert 'http://default-api' in api_bases

    def test_tier3_last_successful_endpoint(self, router):
        """Last successful endpoint (Tier 3) is used when agent-specific endpoints are exhausted."""
        _add_endpoint(router, "coder_ep", "http://coder-api")
        _add_endpoint(router, "recovery_ep", "http://recovery-api")  # The recovery endpoint must exist
        _set_agent_priorities(router, "Coder", ["ep_coder_ep"])
        
        # Simulate: agent_ep fails, last-successful was the recovery endpoint
        with router._lock:
            router._last_successful_endpoint_cfg = {
                'api_base': 'http://recovery-api',
                'model': 'recovery-model',
            }
        
        # Disable the agent-specific endpoint so Tier 3 kicks in
        with router._lock:
            router.endpoints['ep_coder_ep'].enabled = False
        
        chain = router.get_endpoint_chain("Coder")
        
        api_bases = [cfg.get('api_base') for cfg in chain]
        assert 'http://recovery-api' in api_bases, \
            f"Tier 3 fallback missing: {api_bases}"

    def test_tier4_default_always_last(self, router):
        """Default endpoint is always the last in the chain."""
        _add_endpoint(router, "ep1", "http://ep1")
        _add_endpoint(router, "ep2", "http://ep2")
        _set_agent_priorities(router, "coder", ["ep_ep1", "ep_ep2"])
        
        chain = router.get_endpoint_chain("coder")
        
        assert chain[-1]['api_base'] == 'http://default-api', \
            f"Default should be last, got {chain[-1].get('api_base')}"

    def test_no_configured_endpoints_falls_back_to_default(self, router):
        """Agent with no configured endpoints gets only the default."""
        chain = router.get_endpoint_chain("unknown_agent")
        
        assert len(chain) == 1
        assert chain[0]['api_base'] == 'http://default-api'


# ============================================================================
# Simulated failures at each tier level
# ============================================================================

class TestSimulatedFailuresPerTier:
    """Test fallback behavior when specific tiers fail."""

    def test_tier1_failure_falls_to_next(self, router):
        """When Tier 1 endpoint fails, call_with_fallback tries next in chain."""
        _add_endpoint(router, "failing_ep", "http://failing-api", max_retries=0)
        _set_agent_priorities(router, "coder", ["ep_failing_ep"])
        
        call_count = [0]
        
        def mock_call(llm_cfg, *args, **kwargs):
            call_count[0] += 1
            api_base = llm_cfg.get('api_base')
            if api_base == 'http://failing-api':
                raise ConnectionError("Tier 1 failed")
            return "success from fallback"
        
        result = router.call_with_fallback("coder", mock_call)
        
        assert result == "success from fallback"
        assert call_count[0] >= 2, f"Should have tried at least 2 endpoints, got {call_count[0]}"

    def test_all_tiers_fail_raises_runtime_error(self, router):
        """When all endpoints fail, RuntimeError is raised with all errors."""
        _add_endpoint(router, "bad_ep", "http://bad-api", max_retries=0)
        _set_agent_priorities(router, "coder", ["ep_bad_ep"])
        
        # Override default to also fail
        router.default_llm_cfg['api_base'] = 'http://also-bad'
        
        def always_fail(llm_cfg, *args, **kwargs):
            raise ConnectionError(f"Failed at {llm_cfg.get('api_base')}")
        
        with pytest.raises(RuntimeError) as exc_info:
            router.call_with_fallback("coder", always_fail)
        
        assert "All API endpoints exhausted" in str(exc_info.value)


# ============================================================================
# Instance cursor persistence and rotation
# ============================================================================

class TestInstanceCursorPersistence:
    """Test per-instance cursor tracking across retries."""

    def test_cursor_advanced_on_inner_loop(self, router):
        """advance_instance_endpoint increments cursor for the instance."""
        pos = router.advance_instance_endpoint("worker1")
        assert pos == 1
        
        pos = router.advance_instance_endpoint("worker1")
        assert pos == 2

    def test_cursor_is_per_instance(self, router):
        """Each instance has its own independent cursor."""
        router.advance_instance_endpoint("worker1")
        router.advance_instance_endpoint("worker1")
        
        # worker1 at position 2
        # worker2 should be at 0 (default)
        with router._lock:
            assert router._instance_endpoint_position.get("worker1", 0) == 2
            assert router._instance_endpoint_position.get("worker2", 0) == 0

    def test_cursor_rotates_chain_on_retry(self, router):
        """Chain is rotated based on instance cursor position."""
        _add_endpoint(router, "ep_a", "http://a-api")
        _add_endpoint(router, "ep_b", "http://b-api")
        _add_endpoint(router, "ep_c", "http://c-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a", "ep_ep_b", "ep_ep_c"])
        
        # First call — cursor at 0, chain starts with ep_a
        chain1 = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain1[0]['api_base'] == 'http://a-api'
        
        # Advance cursor (simulating inner-loop detection)
        router.advance_instance_endpoint("worker1")
        
        # Next call — chain should start with ep_b
        chain2 = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain2[0]['api_base'] == 'http://b-api', \
            f"Expected chain to rotate to ep_b, got {chain2[0].get('api_base')}"

    def test_cursor_wraps_around(self, router):
        """Cursor wraps around when advanced past the number of endpoints."""
        _add_endpoint(router, "ep_a", "http://a-api")
        _add_endpoint(router, "ep_b", "http://b-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a", "ep_ep_b"])
        
        # Advance past the number of endpoints
        for _ in range(5):
            router.advance_instance_endpoint("worker1")
        
        # Cursor should wrap (5 % 2 = 1)
        chain = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain[0]['api_base'] == 'http://b-api', \
            f"Expected wrapped cursor to start at ep_b, got {chain[0].get('api_base')}"

    def test_cursor_not_advanced_for_other_instances(self, router):
        """Advancing one instance's cursor doesn't affect others."""
        _add_endpoint(router, "ep_a", "http://a-api")
        _add_endpoint(router, "ep_b", "http://b-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a", "ep_ep_b"])
        
        router.advance_instance_endpoint("worker1")
        
        # worker2 should still get unrotated chain
        chain = router.get_endpoint_chain("coder", instance_name="worker2")
        assert chain[0]['api_base'] == 'http://a-api'


# ============================================================================
# Cursor reset after success
# ============================================================================

class TestCursorResetAfterSuccess:
    """Test cursor is reset when agent completes successfully."""

    def test_reset_clears_cursor(self, router):
        """reset_instance_endpoint clears the cursor for an instance."""
        router.advance_instance_endpoint("worker1")
        router.advance_instance_endpoint("worker1")
        
        assert router._instance_endpoint_position.get("worker1", 0) == 2
        
        router.reset_instance_endpoint("worker1")
        
        assert "worker1" not in router._instance_endpoint_position

    def test_reset_restores_original_chain_order(self, router):
        """After reset, chain returns to original order."""
        _add_endpoint(router, "ep_a", "http://a-api")
        _add_endpoint(router, "ep_b", "http://b-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a", "ep_ep_b"])
        
        # Advance cursor
        router.advance_instance_endpoint("worker1")
        
        # Reset (simulating successful completion)
        router.reset_instance_endpoint("worker1")
        
        chain = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain[0]['api_base'] == 'http://a-api', \
            "Chain should be back to original order after reset"

    def test_reset_nonexistent_cursor_is_noop(self, router):
        """Resetting a cursor that doesn't exist is safe."""
        # Should not raise
        router.reset_instance_endpoint("nonexistent_worker")


# ============================================================================
# Cursor reset on endpoint config change (from_dict) — REGRESSION (compression loop bug)
# ============================================================================

class TestCursorResetOnConfigChange:
    """Regression: a stale positional cursor must never survive an endpoint config change.

    The per-instance cursor is a POSITIONAL index into the tier chain, which is rebuilt from live
    settings on every get_endpoint_chain() call. If the user reorders/replaces endpoints via the UI
    (from_dict), the old positional cursor points at the WRONG endpoint. from_dict must clear all
    instance cursors so no stale rotation survives a config change.
    """

    def _ep(self, name, api_base, model="test-model"):
        return APIEndpoint(
            id=f"ep_{name}",
            name=name,
            api_base=api_base,
            model=model,
            enabled=True,
        )

    def test_from_dict_clears_all_instance_cursors(self, router):
        """Advancing a cursor then calling from_dict clears it (no stale positional rotation)."""
        _add_endpoint(router, "ep_a", "http://a-api")
        _add_endpoint(router, "ep_b", "http://b-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a", "ep_ep_b"])

        # Advance the cursor so a positional rotation would normally apply.
        router.advance_instance_endpoint("worker1")
        assert router._instance_endpoint_position.get("worker1", 0) == 1

        # Config change via the UI entry point (from_dict).
        router.from_dict({
            "endpoints": [self._ep("ep_a", "http://a-api").to_dict(),
                          self._ep("ep_b", "http://b-api").to_dict()],
            "agent_priorities": {"coder": ["ep_ep_a", "ep_ep_b"]},
        })

        # All cursors must be cleared.
        assert "worker1" not in router._instance_endpoint_position, \
            f"Cursor should be cleared after from_dict, got {router._instance_endpoint_position}"

    def test_from_dict_order_change_no_stale_rotation(self, router):
        """After a config change that reorders endpoints, the next chain uses fresh (unrotated) order.

        Before the fix: cursor=1 survived from_dict and rotated the NEW chain to start at ep_b.
        After the fix: cursor is cleared, so the chain starts at the new first endpoint (ep_c).
        """
        _add_endpoint(router, "ep_a", "http://a-api")
        _add_endpoint(router, "ep_b", "http://b-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a", "ep_ep_b"])

        # Advance cursor (position 1 → would rotate to ep_b under the old order).
        router.advance_instance_endpoint("worker1")

        # Reorder endpoints via from_dict: new priority order is [c, a, b].
        router.from_dict({
            "endpoints": [self._ep("ep_a", "http://a-api").to_dict(),
                          self._ep("ep_b", "http://b-api").to_dict(),
                          self._ep("ep_c", "http://c-api").to_dict()],
            "agent_priorities": {"coder": ["ep_ep_c", "ep_ep_a", "ep_ep_b"]},
        })

        chain = router.get_endpoint_chain("coder", instance_name="worker1")
        # Cursor cleared → unrotated order → first endpoint is the new Tier-1 head (ep_c).
        assert chain[0]["api_base"] == "http://c-api", \
            f"Expected fresh order (ep_c) after config change, got {chain[0].get('api_base')}"

    def test_from_dict_resets_concurrent_live_cursor_of_other_instance(self, router):
        """from_dict clears ALL instance cursors — a concurrent live cursor for a DIFFERENT
        instance does not survive the config change (the "clear all" side effect).

        Regression (MINOR #5): the clear-all behaviour was previously untested. A single endpoint
        edit via from_dict must invalidate every running agent's positional cursor, not just the one
        that triggered the edit.
        """
        _add_endpoint(router, "ep_a", "http://a-api")
        _add_endpoint(router, "ep_b", "http://b-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a", "ep_ep_b"])

        # Two concurrent live cursors for DIFFERENT instances.
        router.advance_instance_endpoint("worker1")
        router.advance_instance_endpoint("worker2")
        assert router._instance_endpoint_position.get("worker1", 0) == 1
        assert router._instance_endpoint_position.get("worker2", 0) == 1

        # Unrelated config change (same endpoints, same priorities) via the UI entry point.
        router.from_dict({
            "endpoints": [self._ep("ep_a", "http://a-api").to_dict(),
                          self._ep("ep_b", "http://b-api").to_dict()],
            "agent_priorities": {"coder": ["ep_ep_a", "ep_ep_b"]},
        })

        # The concurrent live cursor for the OTHER instance must also be reset.
        assert router._instance_endpoint_position == {}, (
            f"from_dict must clear ALL cursors; got {router._instance_endpoint_position}"
        )

    def test_from_dict_without_prior_cursor_is_noop(self, router):
        """from_dict with no prior cursors leaves the cursor store empty AND re-applies the config.

        Rewritten (NIT #6c) to assert a meaningful invariant: even though there is nothing to clear,
        from_dict must still populate endpoints/priorities and leave the cursor store empty — i.e. it
        is a safe no-op on cursors without corrupting the freshly-loaded state.
        """
        router.from_dict({
            "endpoints": [self._ep("ep_a", "http://a-api").to_dict()],
            "agent_priorities": {"coder": ["ep_ep_a"]},
        })
        # No cursors were present, so the store stays empty.
        assert router._instance_endpoint_position == {}
        # The config was still applied (not a silent no-op on state).
        assert "ep_ep_a" in router.endpoints
        chain = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain[0]["api_base"] == "http://a-api"


# ============================================================================
# Cooldown filtering in chain construction
# ============================================================================

class TestCooldownFiltering:
    """Test that endpoints in cooldown are skipped during chain construction."""

    def test_cooldowned_endpoint_skipped(self, router):
        """Endpoint in cooldown period is excluded from the chain."""
        _add_endpoint(router, "ep_a", "http://a-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a"])

        # Mark endpoint as failed (within cooldown). Cooldown keys are per-(base, model)
        # since the router-cascade fix — use the same key format get_endpoint_chain reads.
        with router._lock:
            router._endpoint_failure_times[(normalize_api_base('http://a-api'), 'test-model')] = time.time()

        chain = router.get_endpoint_chain("coder")
        
        api_bases = [cfg.get('api_base') for cfg in chain]
        # ep_a should be skipped, only default remains
        assert 'http://a-api' not in api_bases

    def test_cooldown_expires_endpoint_available(self, router):
        """Endpoint becomes available after cooldown expires."""
        _add_endpoint(router, "ep_a", "http://a-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a"])

        # Mark as failed long ago (beyond cooldown) — per-(base, model) key format.
        with router._lock:
            router._endpoint_failure_times[(normalize_api_base('http://a-api'), 'test-model')] = time.time() - 3600

        chain = router.get_endpoint_chain("coder")
        
        api_bases = [cfg.get('api_base') for cfg in chain]
        assert 'http://a-api' in api_bases


# ============================================================================
# call_with_fallback generator handling
# ============================================================================

class TestCallWithFallbackGenerators:
    """Test that call_with_fallback correctly handles generator functions."""

    def test_generator_passed_through(self, router):
        """Generator results are passed through without double-wrapping."""
        _add_endpoint(router, "ep_a", "http://a-api")
        _set_agent_priorities(router, "coder", ["ep_ep_a"])
        
        def gen_call(llm_cfg, *args, **kwargs):
            yield "chunk1"
            yield "chunk2"
        
        result = router.call_with_fallback("coder", gen_call)
        
        # Should be a generator
        assert hasattr(result, '__iter__') and hasattr(result, '__next__')
        
        chunks = list(result)
        assert chunks == ["chunk1", "chunk2"]

    def test_generator_error_triggers_fallback(self, router):
        """Errors on first chunk of generator trigger fallback to next endpoint.

        Note: The retry/fallback logic in call_with_fallback only wraps the call when
        a semaphore exists (concurrency_limit >= 0). With concurrency_limit=-1 (unlimited),
        calls go directly through without retry wrapping. This test uses concurrency_limit=1
        to ensure the fallback mechanism is exercised.
        """
        _add_endpoint(router, "bad_ep", "http://bad-api", max_retries=0, concurrency_limit=1)
        _set_agent_priorities(router, "Coder", ["ep_bad_ep"])
        
        call_bases = []  # Track which endpoints were called and in what order
        
        def gen_call(llm_cfg, *args, **kwargs):
            api_base = llm_cfg.get('api_base')
            call_bases.append(api_base)
            if api_base == 'http://bad-api':
                raise ConnectionError("First chunk failure")
            yield "fallback success"
        
        # call_with_fallback should catch the error and retry with fallback endpoints.
        result = router.call_with_fallback("Coder", gen_call)
        
        try:
            chunks = list(result)
        except Exception as e:
            pytest.fail(f"Fallback should have succeeded but raised: {e}")
        
        # Should have tried the bad endpoint first, then fallback (default)
        assert len(call_bases) >= 2, \
            f"Expected at least 2 call attempts, got {len(call_bases)}: {call_bases}"
        assert call_bases[0] == 'http://bad-api', \
            f"First attempt should be bad_ep, got {call_bases[0]}"
        # Fallback should be the default endpoint
        assert 'http://default-api' in call_bases, \
            f"Should have fallen back to default: {call_bases}"