"""E2E regression tests for the full API endpoint fallback chain (router level).

These pin the behaviors fixed in the live-config / fallback investigation
(reports/api-endpoint-live-config-investigation.md):

  Fix 1 — from_dict() clears endpoint health state (blacklist + failure counters)
          so a "fixed" endpoint is eligible again immediately.
  Fix 2 — Tier-4 (global default) usage is logged explicitly when an agent has NO
          effective endpoints (fires once per call attempt).
  Fix 3 — set_agent_priorities() with ALL-invalid IDs logs a WARNING (not INFO).

Plus the baseline chain behaviors they depend on:
  - Happy path: assigned endpoints are tried in priority order, Tier-4 only as last resort.
  - Fallback on failure: ep1 fails → ep2 is used, ep1 gets a cooldown.
  - Full exhaustion → Tier-4 global default is tried last.

Only the actual HTTP call (call_fn) is mocked — router internals are exercised for real.
The lazy sanity probe is disabled so fake endpoints are not pruned by a real GET /models.
"""

import os
import time
from unittest.mock import patch

import pytest

from agent_cascade.api_router import APIRouter, APIEndpoint
from agent_cascade.api_router_pkg.normalization import normalize_api_base
from agent_cascade.llm.base import ModelServiceError
from agent_cascade.settings import (
    ENDPOINT_COOLDOWN_SECONDS,
    ENDPOINT_BLACKLIST_SECONDS,
)

# Reuse the shared router fixture + helpers from conftest (isolated config dir, FAST policy).
from tests.conftest import _add_endpoint  # noqa: E402


@pytest.fixture(autouse=True)
def _disable_sanity_probe():
    """Disable the lazy per-endpoint sanity probe so fake endpoints aren't pruned.

    These tests exercise call_with_fallback's fallback/blacklist/cooldown logic with
    unreachable fake endpoints — not endpoint validation. The lazy probe would issue a
    REAL HTTP GET /models and skip endpoints that fail probing, pruning the chain.
    """
    import agent_cascade.api_router_pkg.router as router_mod
    orig = router_mod.SANITY_PROBE_ENABLED
    router_mod.SANITY_PROBE_ENABLED = False
    try:
        yield
    finally:
        router_mod.SANITY_PROBE_ENABLED = orig


def _det_error():
    """A deterministic client error (HTTP 400) → counted toward the blacklist threshold."""
    return ModelServiceError(code='400', message="deterministic failure")


def _ep_dict(name, api_base, model):
    """Build an APIEndpoint dict for from_dict() payloads (stable id = ep_<name>)."""
    return APIEndpoint(
        id=f"ep_{name}", name=name, api_base=api_base, model=model, enabled=True,
    ).to_dict()


# ============================================================================
# Baseline: happy path + failure fallback + full exhaustion → Tier-4
# ============================================================================

class TestFallbackChainBasics:
    def test_happy_path_priority_order_no_tier4(self, router):
        """Agent with 2 assigned endpoints → chain is [ep1, ep2] in priority order.

        Tier-4 (the default) is appended as last-resort but must NOT be tried when the
        primary succeeds.
        """
        _add_endpoint(router, "a", "http://a-api", model="model-a", max_retries=0)
        _add_endpoint(router, "b", "http://b-api", model="model-b", max_retries=0)
        router.set_agent_priorities("coder", ["ep_a", "ep_b"])

        chain = router.get_endpoint_chain("coder")
        # Tier-1 endpoints first, in the configured priority order.
        assert [c['api_base'] for c in chain[:2]] == ["http://a-api", "http://b-api"]
        # Default (Tier-4) is last-resort only.
        assert chain[-1]['api_base'] == 'http://default-api'

        called = []

        def call_fn(cfg, *a, **k):
            called.append(cfg['api_base'])
            return "ok"

        result = router.call_with_fallback("coder", call_fn)
        assert result == "ok"
        # Only the primary was tried — no fallback, no Tier-4.
        assert called == ["http://a-api"]

    def test_ep1_fails_ep2_used_and_cooldown_recorded(self, router):
        """ep1 fails (500) → ep2 is used; ep1 gets a cooldown entry."""
        _add_endpoint(router, "a", "http://a-api", model="model-a", max_retries=0)
        _add_endpoint(router, "b", "http://b-api", model="model-b", max_retries=0)
        router.set_agent_priorities("coder", ["ep_a", "ep_b"])

        called = []

        def call_fn(cfg, *a, **k):
            base = cfg['api_base']
            called.append(base)
            if base == 'http://a-api':
                raise ModelServiceError(code='500', message="server error")  # transient → cooldown
            return "ok-from-b"

        result = router.call_with_fallback("coder", call_fn)
        assert result == "ok-from-b"
        # ep1 tried first, then fell back to ep2.
        assert called == ["http://a-api", "http://b-api"]

        # ep1 got a cooldown entry keyed per-(normalized base, model).
        key = (normalize_api_base('http://a-api'), 'model-a')
        with router._lock:
            assert key in router._endpoint_failure_times, \
                f"ep1 should be in cooldown after failure, keys={list(router._endpoint_failure_times)}"

    def test_full_exhaustion_tier4_tried_last(self, router):
        """All assigned endpoints fail → the Tier-4 global default is tried last."""
        _add_endpoint(router, "a", "http://a-api", model="model-a", max_retries=0)
        _add_endpoint(router, "b", "http://b-api", model="model-b", max_retries=0)
        router.set_agent_priorities("coder", ["ep_a", "ep_b"])

        called = []

        def call_fn(cfg, *a, **k):
            base = cfg['api_base']
            called.append(base)
            if base == 'http://default-api':
                return "ok-from-default"
            raise ModelServiceError(code='500', message="server error")

        result = router.call_with_fallback("coder", call_fn)
        assert result == "ok-from-default"
        # ep1 → ep2 → Tier-4 default (last).
        assert called == ["http://a-api", "http://b-api", "http://default-api"]


# ============================================================================
# Fix 1 — from_dict clears health state (blacklist + failure counters)
# ============================================================================

class TestFromDictClearsHealthState:
    def test_blacklisted_endpoint_eligible_after_from_dict(self, router):
        """A blacklisted endpoint is eligible again immediately after a from_dict() config change.

        Steps:
          1. Endpoint A fails 3× (deterministic 400) → blacklisted for ENDPOINT_BLACKLIST_SECONDS.
          2. get_endpoint_chain skips A (only the Tier-4 default remains).
          3. from_dict() with updated config → blacklist cleared.
          4. A is back in the chain and a call to A succeeds.
        """
        _add_endpoint(router, "a", "http://a-api", model="model-a", max_retries=0)
        router.set_agent_priorities("coder", ["ep_a"])

        key = (normalize_api_base('http://a-api'), 'model-a')

        # Three consecutive deterministic failures → blacklist (mirrors the real call path).
        for _ in range(3):
            with router._lock:
                count = router._endpoint_deterministic_failures.get(key, 0) + 1
                router._endpoint_deterministic_failures[key] = count
                if count >= 3:
                    router._endpoint_blacklist[key] = time.time() + ENDPOINT_BLACKLIST_SECONDS

        # A is blacklisted → filtered out of the chain (only Tier-4 default remains).
        chain_before = router.get_endpoint_chain("coder")
        assert 'http://a-api' not in [c['api_base'] for c in chain_before]
        assert any(c['api_base'] == 'http://default-api' for c in chain_before)

        # User "fixes" the endpoint via a UI config change (from_dict).
        router.from_dict({
            "endpoints": [_ep_dict("a", "http://a-api", "model-a")],
            "agent_priorities": {"coder": ["ep_a"]},
        })

        # Blacklist + failure counters are now empty.
        with router._lock:
            assert router._endpoint_blacklist == {}, \
                f"blacklist should be cleared after from_dict, got {router._endpoint_blacklist}"
            assert router._endpoint_failure_times == {}, \
                f"failure counters should be cleared after from_dict, got {router._endpoint_failure_times}"

        # A is eligible again → back in the chain.
        chain_after = router.get_endpoint_chain("coder")
        assert 'http://a-api' in [c['api_base'] for c in chain_after]

        # And a real call to A now succeeds (it is no longer skipped).
        result = router.call_with_fallback("coder", lambda cfg, *a, **k: "ok-after-fix")
        assert result == "ok-after-fix"

    def test_from_dict_clears_stale_failure_counters(self, router):
        """from_dict() clears _endpoint_failure_times even when no blacklist is set."""
        _add_endpoint(router, "a", "http://a-api", model="model-a")
        router.set_agent_priorities("coder", ["ep_a"])

        # Simulate a single transient failure (cooldown recorded, below blacklist threshold).
        key = (normalize_api_base('http://a-api'), 'model-a')
        with router._lock:
            router._endpoint_failure_times[key] = time.time()  # within cooldown → A skipped

        chain_before = router.get_endpoint_chain("coder")
        assert 'http://a-api' not in [c['api_base'] for c in chain_before]

        router.from_dict({
            "endpoints": [_ep_dict("a", "http://a-api", "model-a")],
            "agent_priorities": {"coder": ["ep_a"]},
        })

        with router._lock:
            assert router._endpoint_failure_times == {}
        chain_after = router.get_endpoint_chain("coder")
        assert 'http://a-api' in [c['api_base'] for c in chain_after]

    def test_from_dict_clears_last_successful_endpoint(self, router):
        """from_dict() clears _last_successful_endpoint_cfg (Tier-3 fallback source).

        A stale last-successful cfg would otherwise be offered as Tier-3 against a server
        that no longer serves that model after the user edits an endpoint.
        """
        _add_endpoint(router, "a", "http://a-api", model="model-a")
        router.set_agent_priorities("coder", ["ep_a"])

        # Simulate a prior success on endpoint A (this is what call_with_fallback records).
        with router._lock:
            router._last_successful_endpoint_cfg = {
                'api_base': 'http://a-api', 'model': 'model-a',
            }
        assert router._last_successful_endpoint_cfg is not None

        # User changes the config (e.g. renames the model) via from_dict.
        router.from_dict({
            "endpoints": [_ep_dict("a", "http://a-api", "model-NEW")],
            "agent_priorities": {"coder": ["ep_a"]},
        })

        # The stale Tier-3 cfg must be gone so it is not offered against the new config.
        assert router._last_successful_endpoint_cfg is None, \
            f"last-successful should be cleared after from_dict, got {router._last_successful_endpoint_cfg}"


# ============================================================================
# Fix 2 — Tier-4 global-default usage is logged explicitly (once per call attempt)
# ============================================================================

class TestTier4Logging:
    def test_tier4_only_logs_info(self, router, caplog):
        """Agent with NO effective endpoints → INFO log fires exactly once per call."""
        # 'security' has no assigned endpoints → chain is only the Tier-4 default.
        import logging
        with caplog.at_level(logging.INFO, logger="agent_cascade.api_router_pkg.router"):
            result = router.call_with_fallback(
                "security", lambda cfg, *a, **k: "ok-default"
            )

        assert result == "ok-default"
        matches = [r for r in caplog.records
                   if r.levelno == logging.INFO and "no effective endpoints" in r.getMessage()]
        assert len(matches) == 1, \
            f"expected exactly one Tier-4 INFO log per call attempt, got {len(matches)}: {[r.getMessage() for r in matches]}"
        msg = matches[0].getMessage()
        # The default identity is surfaced so the "wtf model is this" confusion is gone.
        assert "default-model" in msg
        assert "http://default-api" in msg

    def test_multi_endpoint_chain_does_not_log_tier4(self, router, caplog):
        """Tier-4 as last-resort inside a multi-endpoint chain is normal → NOT logged."""
        _add_endpoint(router, "a", "http://a-api", model="model-a", max_retries=0)
        router.set_agent_priorities("coder", ["ep_a"])

        import logging
        with caplog.at_level(logging.INFO, logger="agent_cascade.api_router_pkg.router"):
            result = router.call_with_fallback("coder", lambda cfg, *a, **k: "ok")

        assert result == "ok"
        matches = [r for r in caplog.records
                   if r.levelno == logging.INFO and "no effective endpoints" in r.getMessage()]
        assert matches == [], \
            f"Tier-4 as last-resort in a multi-endpoint chain must NOT log, got {[r.getMessage() for r in matches]}"

    def test_tier4_only_after_probe_failure_logs_info(self, router, caplog):
        """Tier-1 endpoint(s) fail their lazy sanity probe → only Tier-4 remains → INFO log.

        This is the case the early len(chain)==1 check CANNOT see: the chain starts with a
        real Tier-1 endpoint (so len(chain)>1 at construction), but that endpoint's probe
        fails inside the loop, leaving only the global default to be tried. The log must
        fire exactly once and name the default.
        """
        import logging
        import agent_cascade.api_router_pkg.router as router_mod

        _add_endpoint(router, "a", "http://a-api", model="model-a", max_retries=0)
        router.set_agent_priorities("coder", ["ep_a"])

        # Sanity: before probing, the chain has 2 entries (Tier-1 + Tier-4), so the early
        # len(chain)==1 check does NOT fire.
        assert len(router.get_endpoint_chain("coder")) == 2

        # Enable the lazy probe for this test only. The probe gate applies to EVERY endpoint
        # in the chain — including the Tier-4 default — so we must FAIL the fake Tier-1
        # endpoint's probe but PASS the real global default's, or the default gets pruned too
        # and the call exhausts instead of falling through.
        orig_probe = router_mod.SANITY_PROBE_ENABLED
        router_mod.SANITY_PROBE_ENABLED = True

        def _fake_probe(cfg):
            base = cfg.get('api_base') or cfg.get('model_server', '')
            return base != 'http://a-api'  # fail the fake Tier-1, pass the real default

        try:
            with caplog.at_level(logging.INFO, logger="agent_cascade.api_router_pkg.router"):
                with patch.object(router, "_sanity_probe", side_effect=_fake_probe):
                    result = router.call_with_fallback(
                        "coder", lambda cfg, *a, **k: "ok-default"
                    )
        finally:
            router_mod.SANITY_PROBE_ENABLED = orig_probe

        # The Tier-1 endpoint was pruned by the failed probe; the call fell through to Tier-4.
        assert result == "ok-default"
        matches = [r for r in caplog.records
                   if r.levelno == logging.INFO and "no effective endpoints" in r.getMessage()]
        # Exactly ONE log (case 2), not double-logged with the early check.
        assert len(matches) == 1, \
            f"expected exactly one Tier-4 INFO log after probe failure, got {len(matches)}: {[r.getMessage() for r in matches]}"
        msg = matches[0].getMessage()
        # It must be the "all filtered/exhausted" variant (case 2), naming the default.
        assert "(all filtered/exhausted)" in msg
        assert "default-model" in msg and "http://default-api" in msg


# ============================================================================
# Fix 3 — set_agent_priorities warns (not INFO) when ALL IDs are invalid
# ============================================================================

class TestPriorityDropWarning:
    def test_all_invalid_ids_logs_warning(self, router, caplog):
        """set_agent_priorities with all-invalid IDs → WARNING log + priorities removed."""
        _add_endpoint(router, "a", "http://a-api", model="model-a")
        # Seed existing priorities so the "remove" branch (not the no-op debug branch) fires.
        router.set_agent_priorities("coder", ["ep_a"])

        import logging
        with caplog.at_level(logging.WARNING, logger="agent_cascade.api_router_pkg.router"):
            router.set_agent_priorities("coder", ["nope1", "nope2"])

        # Priorities were removed (all IDs invalid).
        assert router.get_agent_priorities("coder") == []

        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING and "ALL endpoint IDs invalid" in r.getMessage()]
        assert len(warnings) == 1, \
            f"expected exactly one WARNING for all-invalid IDs, got {len(warnings)}: {[r.getMessage() for r in warnings]}"
        msg = warnings[0].getMessage()
        assert "'coder'" in msg
        # The invalid IDs are surfaced explicitly.
        assert "nope1" in msg and "nope2" in msg
