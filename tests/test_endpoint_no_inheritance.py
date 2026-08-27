"""Tests for the single-FIFO endpoint-resolution design (no caller inheritance).

Background — slot-inheritance self-deadlock fix:
    The old Tier-2 "caller inheritance" let an agent with no own endpoints
    (Security, Compressor) fall back to the *caller's* endpoint chain. Because
    those agents shared the caller's sequential slot key (``_shared_sequential_slot_``,
    capacity 1), the yield-then-reacquire pattern deadlocked: the child released the
    one permit and then waited in FIFO for a fresh grant of that same permit, starving
    every other agent.

    The fix removed caller inheritance entirely. Every agent now resolves through its
    OWN chain — Tier 1 (own endpoints) → Tier 3 (last-successful) → Tier 4 (global
    default) — and meters against a slot key derived from its own resolved endpoint.
    Security/Compressor therefore acquire a distinct slot from their caller and can
    never self-deadlock on the caller's sequential permit.

These tests guard that design decision:
  * ``get_endpoint_chain`` / ``call_with_fallback`` no longer accept ``caller_agent_type``.
  * An unconfigured agent does NOT pick up another agent's endpoints — it falls to
    last-successful (if eligible) or the global default.
  * The chain ordering is unchanged for configured agents (own → last-successful → default).

No LLM or network connections required.
"""

import inspect
import os
from unittest.mock import patch

import pytest

from agent_cascade.api_router import APIRouter, APIEndpoint


# ============================================================================
# Fixtures and helpers
# ============================================================================


@pytest.fixture(autouse=True)
def _disable_sanity_probe():
    """Disable the lazy per-endpoint sanity probe for every test in this module.

    These tests use unreachable fake endpoints (http://default-api, http://caller-api, ...)
    and exercise call_with_fallback's endpoint-resolution logic — not endpoint validation.
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
        })
        # _pool=None keeps call_with_fallback termination checks from needing the
        # full AgentPool wiring; production guards all _pool accesses.
        r._pool = None
        yield r


def _add_endpoint(router, name, api_base, model='test-model', enabled=True,
                  concurrency_limit=-1, max_retries=3):
    """Helper to add an endpoint to the router."""
    ep = APIEndpoint(
        id=f"ep_{name}",
        name=name,
        api_base=api_base,
        model=model,
        enabled=enabled,
        concurrency_limit=concurrency_limit,
        max_retries=max_retries,
        rate_limit_rpm=0,
    )
    router.add_endpoint(ep)


# ============================================================================
# Signature guard: caller inheritance parameter is gone
# ============================================================================


class TestNoCallerInheritanceSignature:
    """If someone re-introduces caller inheritance by threading a caller type
    through the chain builder or call path, these signature checks fail immediately."""

    def test_get_endpoint_chain_has_no_caller_agent_type(self, router):
        sig = inspect.signature(router.get_endpoint_chain)
        assert 'caller_agent_type' not in sig.parameters, \
            "get_endpoint_chain must not accept caller_agent_type (inheritance removed)"

    def test_call_with_fallback_has_no_caller_agent_type(self, router):
        sig = inspect.signature(router.call_with_fallback)
        assert 'caller_agent_type' not in sig.parameters, \
            "call_with_fallback must not accept caller_agent_type (inheritance removed)"

    def test_resolve_own_endpoints_has_no_caller_param(self, router):
        """The renamed resolver takes only agent_type — no caller context."""
        sig = inspect.signature(router._resolve_own_endpoints)
        params = set(sig.parameters) - {'self'}
        assert params == {'agent_type'}, \
            f"_resolve_own_endpoints should take only agent_type, got {params}"


# ============================================================================
# No inheritance: unconfigured agents fall to their own/default pool
# ============================================================================


class TestNoCallerInheritanceBehavior:
    """An agent with no own endpoints must NOT pick up another agent's endpoints."""

    def test_unconfigured_agent_does_not_inherit_caller_endpoints(self, router):
        """Security/Compressor (no own priorities) fall to default, not the caller's endpoint.

        Regression: this is exactly the path that caused the self-deadlock — inheriting
        the caller's sequential endpoint meant metering against the caller's slot key.
        """
        _add_endpoint(router, "caller_ep", "http://caller-api")
        router.set_agent_priorities("orchestrator", ["ep_caller_ep"])

        # 'security' has no configured priorities — must NOT inherit caller's endpoint
        chain = router.get_endpoint_chain("security")

        api_bases = [cfg.get('api_base') for cfg in chain]
        assert 'http://caller-api' not in api_bases, \
            f"Caller's endpoint should NOT be inherited: {api_bases}"
        # Falls through to the global default (Tier 4)
        assert 'http://default-api' in api_bases

    def test_unconfigured_agent_no_last_success_falls_to_default_only(self, router):
        """With no own endpoints AND no last-successful eligibility, chain is just default."""
        _add_endpoint(router, "other_ep", "http://other-api")
        router.set_agent_priorities("coder", ["ep_other_ep"])

        # 'security' never had priorities configured → not eligible for Tier 3
        chain = router.get_endpoint_chain("security")

        assert [cfg.get('api_base') for cfg in chain] == ['http://default-api']

    def test_configured_agent_uses_own_endpoints_not_caller(self, router):
        """A configured agent resolves through its OWN endpoints regardless of any caller."""
        _add_endpoint(router, "coder_ep", "http://coder-api")
        _add_endpoint(router, "caller_ep", "http://caller-api")
        router.set_agent_priorities("coder", ["ep_coder_ep"])
        router.set_agent_priorities("orchestrator", ["ep_caller_ep"])

        chain = router.get_endpoint_chain("coder")
        api_bases = [cfg.get('api_base') for cfg in chain]

        assert 'http://coder-api' in api_bases, "Own endpoint must be present"
        assert 'http://caller-api' not in api_bases, "Caller's endpoint must NOT leak in"


# ============================================================================
# Chain ordering preserved (own → last-successful → default)
# ============================================================================


class TestChainOrdering:
    """The single-FIFO chain still orders own endpoints first, then last-successful,
    then the global default — for agents that are eligible for Tier 3."""

    def test_chain_includes_own_and_last_successful(self, router):
        _add_endpoint(router, "agent_ep", "http://agent-api")
        _add_endpoint(router, "recovery_ep", "http://recovery-api")
        router.set_agent_priorities("coder", ["ep_agent_ep"])

        # Agent had priorities; its own endpoint is disabled so Tier 3 kicks in
        with router._lock:
            router.endpoints['ep_agent_ep'].enabled = False
            router._last_successful_endpoint_cfg = {
                'api_base': 'http://recovery-api',
                'model': 'recovery-model',
            }

        chain = router.get_endpoint_chain("coder")
        api_bases = [cfg.get('api_base') for cfg in chain]

        assert 'http://recovery-api' in api_bases, f"Tier 3 (last-successful) missing: {api_bases}"
        assert chain[-1]['api_base'] == 'http://default-api', "Default must be last"

    def test_default_always_last(self, router):
        _add_endpoint(router, "ep1", "http://ep1")
        router.set_agent_priorities("coder", ["ep_ep1"])

        chain = router.get_endpoint_chain("coder")
        assert chain[-1]['api_base'] == 'http://default-api'


# ============================================================================
# call_with_fallback: no caller leakage through the live call path
# ============================================================================


class TestCallWithFallbackNoInheritance:
    """The live fallback path must only try the agent's own/default endpoints."""

    def test_unconfigured_agent_only_tries_default(self, router):
        _add_endpoint(router, "caller_ep", "http://caller-api")
        router.set_agent_priorities("orchestrator", ["ep_caller_ep"])

        call_bases = []

        def track_calls(llm_cfg, *args, **kwargs):
            call_bases.append(llm_cfg.get('api_base'))
            return "ok"

        # 'security' has no own endpoints → only the default should be tried
        result = router.call_with_fallback("security", track_calls)

        assert result == "ok"
        assert 'http://caller-api' not in call_bases, \
            f"Caller's endpoint must not be tried: {call_bases}"
        assert 'http://default-api' in call_bases


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
