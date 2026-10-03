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

import time
from unittest.mock import patch

import pytest

from agent_cascade.api_router import APIEndpoint
from agent_cascade.api_router_pkg.normalization import normalize_api_base
from agent_cascade.slot_queue import SlotHolder, release_slot_permit
from agent_cascade.llm.base import ModelServiceError
from agent_cascade.settings import ENDPOINT_BLACKLIST_SECONDS  # noqa: F401  (re-exported for tests)
from agent_cascade.settings import ENDPOINT_COOLDOWN_SECONDS
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
    return ModelServiceError(code='400', message='deterministic failure')


def _ep_dict(name, api_base, model):
    """Build an APIEndpoint dict for from_dict() payloads (stable id = ep_<name>)."""
    return APIEndpoint(
        id=f"ep_{name}",
        name=name,
        api_base=api_base,
        model=model,
        enabled=True,
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
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', max_retries=0)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', max_retries=0)
        router.set_agent_priorities('coder', ['ep_a', 'ep_b'])

        chain = router.get_endpoint_chain('coder')
        # Tier-1 endpoints first, in the configured priority order.
        assert [c['api_base'] for c in chain[:2]] == ['http://a-api', 'http://b-api']
        # Default (Tier-4) is last-resort only.
        assert chain[-1]['api_base'] == 'http://default-api'

        called = []

        def call_fn(cfg, *a, **k):
            called.append(cfg['api_base'])
            return 'ok'

        result = router.call_with_fallback('coder', call_fn)
        assert result == 'ok'
        # Only the primary was tried — no fallback, no Tier-4.
        assert called == ['http://a-api']

    def test_ep1_fails_ep2_used_and_cooldown_recorded(self, router):
        """ep1 fails (500) → ep2 is used; ep1 gets a cooldown entry."""
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', max_retries=0)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', max_retries=0)
        router.set_agent_priorities('coder', ['ep_a', 'ep_b'])

        called = []

        def call_fn(cfg, *a, **k):
            base = cfg['api_base']
            called.append(base)
            if base == 'http://a-api':
                raise ModelServiceError(code='500', message='server error')  # transient → cooldown
            return 'ok-from-b'

        result = router.call_with_fallback('coder', call_fn)
        assert result == 'ok-from-b'
        # ep1 tried first, then fell back to ep2.
        assert called == ['http://a-api', 'http://b-api']

        # ep1 got a cooldown entry keyed per-(normalized base, model).
        key = (normalize_api_base('http://a-api'), 'model-a')
        with router._lock:
            assert key in router._endpoint_failure_times, \
                f"ep1 should be in cooldown after failure, keys={list(router._endpoint_failure_times)}"

    def test_full_exhaustion_tier4_tried_last(self, router):
        """All assigned endpoints fail → the Tier-4 global default is tried last."""
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', max_retries=0)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', max_retries=0)
        router.set_agent_priorities('coder', ['ep_a', 'ep_b'])

        called = []

        def call_fn(cfg, *a, **k):
            base = cfg['api_base']
            called.append(base)
            if base == 'http://default-api':
                return 'ok-from-default'
            raise ModelServiceError(code='500', message='server error')

        result = router.call_with_fallback('coder', call_fn)
        assert result == 'ok-from-default'
        # ep1 → ep2 → Tier-4 default (last).
        assert called == ['http://a-api', 'http://b-api', 'http://default-api']


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
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', max_retries=0)
        router.set_agent_priorities('coder', ['ep_a'])

        key = (normalize_api_base('http://a-api'), 'model-a')

        # Three consecutive deterministic failures → blacklist (mirrors the real call path).
        for _ in range(3):
            with router._lock:
                count = router._endpoint_deterministic_failures.get(key, 0) + 1
                router._endpoint_deterministic_failures[key] = count
                if count >= 3:
                    router._endpoint_blacklist[key] = time.time() + ENDPOINT_BLACKLIST_SECONDS

        # A is blacklisted → filtered out of the chain (only Tier-4 default remains).
        chain_before = router.get_endpoint_chain('coder')
        assert 'http://a-api' not in [c['api_base'] for c in chain_before]
        assert any(c['api_base'] == 'http://default-api' for c in chain_before)

        # User "fixes" the endpoint via a UI config change (from_dict).
        router.from_dict({
            'endpoints': [_ep_dict('a', 'http://a-api', 'model-a')],
            'agent_priorities': {
                'coder': ['ep_a']
            },
        })

        # Blacklist + failure counters are now empty.
        with router._lock:
            assert router._endpoint_blacklist == {}, \
                f"blacklist should be cleared after from_dict, got {router._endpoint_blacklist}"
            assert router._endpoint_failure_times == {}, \
                f"failure counters should be cleared after from_dict, got {router._endpoint_failure_times}"

        # A is eligible again → back in the chain.
        chain_after = router.get_endpoint_chain('coder')
        assert 'http://a-api' in [c['api_base'] for c in chain_after]

        # And a real call to A now succeeds (it is no longer skipped).
        result = router.call_with_fallback('coder', lambda cfg, *a, **k: 'ok-after-fix')
        assert result == 'ok-after-fix'

    def test_from_dict_clears_stale_failure_counters(self, router):
        """from_dict() clears _endpoint_failure_times even when no blacklist is set."""
        _add_endpoint(router, 'a', 'http://a-api', model='model-a')
        router.set_agent_priorities('coder', ['ep_a'])

        # Simulate a single transient failure (cooldown recorded, below blacklist threshold).
        key = (normalize_api_base('http://a-api'), 'model-a')
        with router._lock:
            router._endpoint_failure_times[key] = time.time()  # within cooldown → A skipped

        chain_before = router.get_endpoint_chain('coder')
        assert 'http://a-api' not in [c['api_base'] for c in chain_before]

        router.from_dict({
            'endpoints': [_ep_dict('a', 'http://a-api', 'model-a')],
            'agent_priorities': {
                'coder': ['ep_a']
            },
        })

        with router._lock:
            assert router._endpoint_failure_times == {}
        chain_after = router.get_endpoint_chain('coder')
        assert 'http://a-api' in [c['api_base'] for c in chain_after]

    def test_from_dict_clears_last_successful_endpoint(self, router):
        """from_dict() clears _last_successful_endpoint_cfg (Tier-3 fallback source).

        A stale last-successful cfg would otherwise be offered as Tier-3 against a server
        that no longer serves that model after the user edits an endpoint.
        """
        _add_endpoint(router, 'a', 'http://a-api', model='model-a')
        router.set_agent_priorities('coder', ['ep_a'])

        # Simulate a prior success on endpoint A (this is what call_with_fallback records).
        with router._lock:
            router._last_successful_endpoint_cfg = {
                'api_base': 'http://a-api',
                'model': 'model-a',
            }
        assert router._last_successful_endpoint_cfg is not None

        # User changes the config (e.g. renames the model) via from_dict.
        router.from_dict({
            'endpoints': [_ep_dict('a', 'http://a-api', 'model-NEW')],
            'agent_priorities': {
                'coder': ['ep_a']
            },
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
        with caplog.at_level(logging.INFO, logger='agent_cascade.api_router_pkg.router'):
            result = router.call_with_fallback('security', lambda cfg, *a, **k: 'ok-default')

        assert result == 'ok-default'
        matches = [
            r for r in caplog.records if r.levelno == logging.INFO and 'no effective endpoints' in r.getMessage()
        ]
        assert len(matches) == 1, \
            f"expected exactly one Tier-4 INFO log per call attempt, got {len(matches)}: {[r.getMessage() for r in matches]}"
        msg = matches[0].getMessage()
        # The default identity is surfaced so the "wtf model is this" confusion is gone.
        assert 'default-model' in msg
        assert 'http://default-api' in msg

    def test_multi_endpoint_chain_does_not_log_tier4(self, router, caplog):
        """Tier-4 as last-resort inside a multi-endpoint chain is normal → NOT logged."""
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', max_retries=0)
        router.set_agent_priorities('coder', ['ep_a'])

        import logging
        with caplog.at_level(logging.INFO, logger='agent_cascade.api_router_pkg.router'):
            result = router.call_with_fallback('coder', lambda cfg, *a, **k: 'ok')

        assert result == 'ok'
        matches = [
            r for r in caplog.records if r.levelno == logging.INFO and 'no effective endpoints' in r.getMessage()
        ]
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

        _add_endpoint(router, 'a', 'http://a-api', model='model-a', max_retries=0)
        router.set_agent_priorities('coder', ['ep_a'])

        # Sanity: before probing, the chain has 2 entries (Tier-1 + Tier-4), so the early
        # len(chain)==1 check does NOT fire.
        assert len(router.get_endpoint_chain('coder')) == 2

        # Enable the lazy probe for this test only. The probe gate applies to EVERY endpoint
        # in the chain — including the Tier-4 default — so we must FAIL the fake Tier-1
        # endpoint's probe but PASS the real global default's, or the default gets pruned too
        # and the call exhausts instead of falling through.
        orig_probe = router_mod.SANITY_PROBE_ENABLED
        router_mod.SANITY_PROBE_ENABLED = True

        def _fake_probe(cfg):
            base = cfg.get('api_base') or cfg.get('model_server', '')
            # HTTP-level failure for the fake Tier-1 (host reachable), success for the default.
            return (base != 'http://a-api', False)

        try:
            with caplog.at_level(logging.INFO, logger='agent_cascade.api_router_pkg.router'):
                with patch.object(router, '_sanity_probe', side_effect=_fake_probe):
                    result = router.call_with_fallback('coder', lambda cfg, *a, **k: 'ok-default')
        finally:
            router_mod.SANITY_PROBE_ENABLED = orig_probe

        # The Tier-1 endpoint was pruned by the failed probe; the call fell through to Tier-4.
        assert result == 'ok-default'
        matches = [
            r for r in caplog.records if r.levelno == logging.INFO and 'no effective endpoints' in r.getMessage()
        ]
        # Exactly ONE log (case 2), not double-logged with the early check.
        assert len(matches) == 1, \
            f"expected exactly one Tier-4 INFO log after probe failure, got {len(matches)}: {[r.getMessage() for r in matches]}"
        msg = matches[0].getMessage()
        # It must be the "all filtered/exhausted" variant (case 2), naming the default.
        assert '(all filtered/exhausted)' in msg
        assert 'default-model' in msg and 'http://default-api' in msg


# ============================================================================
# WinError 10055 probe cascade fix — per-host dedup within a chain pass
#
# When multiple endpoints share the same physical server and the first probe hits a
# connection-level failure (WinError 10055 / WSAENOBUFS, refused, timeout), remaining
# same-base endpoints must SKIP their probes (no HTTP) — probing them would also fail and
# further exhaust socket buffers. Different-base endpoints are still probed normally.
# ============================================================================


class TestProbeDedupPerHost:

    def test_connection_error_skips_same_base_probes(self, router):
        """First endpoint's probe fails with a CONNECTION error → same-base endpoints skip
        their probes (no _sanity_probe call), different-base endpoints are still probed.
        The chain falls through to the working different-base endpoint instead of raising
        'All API endpoints exhausted'."""
        import agent_cascade.api_router_pkg.router as router_mod

        # 3 endpoints on the same base + 1 on a different (working) base.
        _add_endpoint(router, 'a1', 'http://same-host:1234/v1', model='model-a1', max_retries=0)
        _add_endpoint(router, 'a2', 'http://same-host:1234/v1', model='model-a2', max_retries=0)
        _add_endpoint(router, 'a3', 'http://same-host:1234/v1', model='model-a3', max_retries=0)
        _add_endpoint(router, 'b', 'http://other-host:9999/v1', model='model-b', max_retries=0)
        router.set_agent_priorities('coder', ['ep_a1', 'ep_a2', 'ep_a3', 'ep_b'])

        orig_probe = router_mod.SANITY_PROBE_ENABLED
        router_mod.SANITY_PROBE_ENABLED = True

        probed_bases = []

        def _fake_probe(cfg):
            base = cfg.get('api_base') or cfg.get('model_server', '')
            probed_bases.append(base)
            if 'same-host' in base:
                # Connection-level failure (e.g. WinError 10055) — host unreachable NOW.
                return (False, True)
            return (True, False)

        try:
            with patch.object(router, '_sanity_probe', side_effect=_fake_probe):
                result = router.call_with_fallback('coder', lambda cfg, *a, **k: f"ok-{cfg.get('model')}")
        finally:
            router_mod.SANITY_PROBE_ENABLED = orig_probe

        # The working different-base endpoint was reached.
        assert result == 'ok-model-b'
        # Only the FIRST same-base endpoint and the different-base endpoint were probed —
        # the remaining 2 same-base endpoints skipped their probes (per-host dedup).
        assert 'http://same-host:1234/v1' in probed_bases
        assert 'http://other-host:9999/v1' in probed_bases
        assert probed_bases.count('http://same-host:1234/v1') == 1, \
            f"expected exactly ONE probe of the same base (dedup), got {probed_bases}"

    def test_http_error_does_not_skip_same_base_probes(self, router):
        """An HTTP-level failure (host reachable, endpoint bad) does NOT dedup — remaining
        same-base endpoints are still probed normally."""
        import agent_cascade.api_router_pkg.router as router_mod

        _add_endpoint(router, 'a1', 'http://same-host:1234/v1', model='model-a1', max_retries=0)
        _add_endpoint(router, 'a2', 'http://same-host:1234/v1', model='model-a2', max_retries=0)
        router.set_agent_priorities('coder', ['ep_a1', 'ep_a2'])

        orig_probe = router_mod.SANITY_PROBE_ENABLED
        router_mod.SANITY_PROBE_ENABLED = True

        probed_bases = []

        def _fake_probe(cfg):
            base = cfg.get('api_base') or cfg.get('model_server', '')
            probed_bases.append(base)
            # HTTP-level failure (401/403/404/5xx) — host IS reachable.
            return (False, False)

        try:
            with patch.object(router, '_sanity_probe', side_effect=_fake_probe):
                with pytest.raises(Exception, match='exhausted'):
                    router.call_with_fallback('coder', lambda cfg, *a, **k: 'ok')
        finally:
            router_mod.SANITY_PROBE_ENABLED = orig_probe

        # Both same-base endpoints were probed (no dedup for HTTP-level failures).
        # Chain = [a1, a2, Tier-4 default] → 3 probes total, both same-base ones fired.
        assert probed_bases.count('http://same-host:1234/v1') == 2, \
            f"expected BOTH same-base endpoints to be probed (no dedup for HTTP errors), got {probed_bases}"


# ============================================================================
# Fix 3 — set_agent_priorities warns (not INFO) when ALL IDs are invalid
# ============================================================================


class TestPriorityDropWarning:

    def test_all_invalid_ids_logs_warning(self, router, caplog):
        """set_agent_priorities with all-invalid IDs → WARNING log + priorities removed."""
        _add_endpoint(router, 'a', 'http://a-api', model='model-a')
        # Seed existing priorities so the "remove" branch (not the no-op debug branch) fires.
        router.set_agent_priorities('coder', ['ep_a'])

        import logging
        with caplog.at_level(logging.WARNING, logger='agent_cascade.api_router_pkg.router'):
            router.set_agent_priorities('coder', ['nope1', 'nope2'])

        # Priorities were removed (all IDs invalid).
        assert router.get_agent_priorities('coder') == []

        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and 'ALL endpoint IDs invalid' in r.getMessage()
        ]
        assert len(warnings) == 1, \
            f"expected exactly one WARNING for all-invalid IDs, got {len(warnings)}: {[r.getMessage() for r in warnings]}"
        msg = warnings[0].getMessage()
        assert "'coder'" in msg
        # The invalid IDs are surfaced explicitly.
        assert 'nope1' in msg and 'nope2' in msg


# ============================================================================
# L147 — Last-active-endpoint fallback (Tier 1.5)
#
# When an agent has NO endpoints assigned, the chain should prefer the GLOBAL
# endpoint most recently used successfully by any agent (_last_active_endpoint)
# over jumping straight to the Tier-4 global default. No instance_name is
# required to pick it up. If that last-active endpoint is gone (removed/renamed
# by a UI reload), it degrades gracefully to Tier-4.
# ============================================================================


class TestLastActiveEndpointFallback:

    def test_unassigned_agent_with_last_active_endpoint_uses_it_first(self, router):
        """Unassigned agent + global last-active endpoint → chain = [last-active cfg, Tier-4].

        The last-active endpoint is NOT assigned to any agent (so Tier-1 is empty), but some
        agent just succeeded on it. get_endpoint_chain must prepend that endpoint's cfg ahead
        of the Tier-4 global default — instead of returning Tier-4 only. No instance_name is
        required to pick up the global marker (a child spawned via call_agent inherits it).
        """
        # Endpoint exists and is enabled, but is NOT assigned to 'security' (unassigned agent).
        _add_endpoint(router, 'c', 'http://c-api', model='model-c')

        # Simulate a prior success on this endpoint by ANY agent.
        # Key format mirrors call_with_fallback: (normalize_api_base(base), model).
        with router._lock:
            router._last_active_endpoint = (
                normalize_api_base('http://c-api'),
                'model-c',
            )

        # Works whether or not an instance_name is passed.
        chain = router.get_endpoint_chain('security', instance_name='worker1')
        assert [c['api_base'] for c in chain] == ['http://c-api', 'http://default-api']
        assert chain[0]['model'] == 'model-c'
        assert chain[-1]['api_base'] == 'http://default-api'

    def test_last_active_key_stale_after_from_dict_degrades_to_tier4(self, router):
        """Last-active key stale (endpoint renamed by a UI reload) → chain = [Tier-4 only].

        _last_active_endpoint SURVIVES from_dict (it is an in-memory live-connection marker,
        like _instance_committed_endpoint), so a stale key can outlive the config change. The
        last-active tier must NOT offer an endpoint that no longer exists under its old
        identity — it degrades gracefully to Tier-4.
        """
        # Endpoint 'c' originally served model-c; some agent just succeeded on it.
        _add_endpoint(router, 'c', 'http://c-api', model='model-c')
        with router._lock:
            router._last_active_endpoint = (
                normalize_api_base('http://c-api'),
                'model-c',
            )

        # Sanity: before the reload, the last-active tier resolves to the endpoint.
        chain_before = router.get_endpoint_chain('security', instance_name='worker1')
        assert [c['api_base'] for c in chain_before] == ['http://c-api', 'http://default-api']

        # User renames the model via a UI config change (from_dict). The last-active key now
        # points at a (base, model) that no enabled endpoint matches. from_dict does NOT clear
        # _last_active_endpoint, so the stale marker is still present but must not be offered.
        router.from_dict({
            'endpoints': [_ep_dict('c', 'http://c-api', 'model-NEW')],
            'agent_priorities': {},  # 'security' stays unassigned
        })

        # The stale last-active key must NOT be offered → chain is Tier-4 only.
        chain_after = router.get_endpoint_chain('security', instance_name='worker1')
        assert [c['api_base'] for c in chain_after] == ['http://default-api']

    def test_last_active_endpoint_picked_up_without_instance_name(self, router):
        """instance_name=None → last-active tier STILL fires (no instance name required).

        Unlike the old per-instance committed tier, Tier 1.5 now reads the GLOBAL
        _last_active_endpoint and needs no instance_name to look it up. Callers that don't pass
        an instance name (get_llm_config, compressor lookups) must still pick up the endpoint
        most recently used by anyone.
        """
        _add_endpoint(router, 'c', 'http://c-api', model='model-c')
        # A last-active key is set; we call WITHOUT instance_name.
        with router._lock:
            router._last_active_endpoint = (
                normalize_api_base('http://c-api'),
                'model-c',
            )

        chain = router.get_endpoint_chain('security')  # instance_name defaults to None

        # Last-active endpoint IS injected even without an instance name.
        assert [c['api_base'] for c in chain] == ['http://c-api', 'http://default-api']


# ============================================================================
# L147 — Capacity-aware Tier 1.5 (liveness fix)
#
# When an unassigned agent's last-active endpoint pool is at full concurrency,
# the router must route to a free-capacity endpoint instead of handing out the
# saturated one (which would stall for QUEUE_WAIT_TIMEOUT under conc=0 collapse).
# If NO endpoint has room it keeps last-active anyway (never worse than today).
#
# We populate REAL SlotPools via router.scheduler._get_or_create_pool(...) so the
# true endpoint→pool key derivation is exercised — including the conc=0 collapse
# to '_shared_sequential_slot_'. count_active() reads len(pool._running); we insert
# real SlotHolder objects (keyed by instance_name) to occupy slots.
# ============================================================================


def _occupy_pool(router, api_base, concurrency_limit, n_holders=1):
    """Populate the REAL pool that (api_base, concurrency_limit) maps to with n holders.

    Returns the pool so tests can assert on its key / occupancy directly.
    """
    pool = router.scheduler._get_or_create_pool(api_base, concurrency_limit)
    for i in range(n_holders):
        holder = SlotHolder(
            agent_name='busy-agent',
            instance_name=f'busy-{i}',
            acquisition_id=i + 1,
        )
        pool._running[f'busy-{i}'] = holder
    return pool


class TestCapacityAwareLastActiveFallback:

    def test_t1_last_active_full_routes_to_free_endpoint(self, router):
        """T1: last-active pool FULL, another endpoint free → chain head is the free one.

        Last in chain is still the Tier-4 default (unchanged).
        """
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=1)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base('http://a-api'), 'model-a')

        # Occupy pool 'a' (capacity 1) → saturated. Pool 'b' stays empty.
        _occupy_pool(router, 'http://a-api', 1, n_holders=1)

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        assert bases[0] == 'http://b-api', f"expected free endpoint b at head, got {bases}"
        assert bases[-1] == 'http://default-api'

    def test_t2_all_pools_full_falls_back_to_last_active(self, router):
        """T2: ALL pools full → keep last-active (no-regression guarantee)."""
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=1)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base('http://a-api'), 'model-a')

        # Occupy BOTH pools → no free endpoint exists.
        _occupy_pool(router, 'http://a-api', 1, n_holders=1)
        _occupy_pool(router, 'http://b-api', 1, n_holders=1)

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        assert bases[0] == 'http://a-api', f"expected last-active a kept when all full, got {bases}"
        assert bases[-1] == 'http://default-api'

    def test_t3_last_active_not_full_unchanged(self, router):
        """T3: last-active pool NOT full → chain head is last-active (healthy path)."""
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=1)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base('http://a-api'), 'model-a')

        # Occupy only pool 'b' → last-active 'a' still has room.
        _occupy_pool(router, 'http://b-api', 1, n_holders=1)

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        assert bases[0] == 'http://a-api', f"expected last-active a at head (not full), got {bases}"

    def test_t4_conc0_collapse_routes_away_from_shared_pool(self, router):
        """T4 (the L147 condition): two endpoints, different models, SAME base, both conc=0.

        Both collapse into the single '_shared_sequential_slot_' pool (capacity 1). Occupy it
        via the real factory and assert the unassigned agent is routed AWAY from that shared
        pool. Companion assertion pins the finding: count_active(base, 0) is non-zero for BOTH
        endpoints (they share one pool), so a per-endpoint occupancy check would have failed.
        """
        base = 'http://shared-api'
        _add_endpoint(router, 'a', base, model='model-a', concurrency_limit=0)
        _add_endpoint(router, 'b', base, model='model-b', concurrency_limit=0)
        # A third endpoint on a DIFFERENT base with free capacity — the escape hatch.
        _add_endpoint(router, 'c', 'http://free-api', model='model-c', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base(base), 'model-a')

        # Occupy the shared sequential pool (both a and b land here).
        shared_pool = _occupy_pool(router, base, 0, n_holders=1)
        assert shared_pool.key == '_shared_sequential_slot_'

        # Companion assertion (pins the §2.2 finding): BOTH conc=0 endpoints map to the SAME
        # occupied shared pool — count_active(base, 0) is non-zero for each model's base. This is
        # why a naive per-endpoint occupancy check would have failed to see the saturation: the
        # collapse to '_shared_sequential_slot_' makes one holder saturate every conc=0 endpoint.
        assert router.scheduler.count_active(base, 0) > 0, \
            'count_active(base,0) must be non-zero (shared pool occupied)'

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        # Must route away from the saturated shared base → head is the free endpoint.
        assert bases[0] == 'http://free-api', f"expected escape to free endpoint, got {bases}"
        # The chosen cfg must NOT be either conc=0 model on the shared base.
        assert chain[0]['model'] == 'model-c'

    def test_t5_conc_minus1_last_active_never_saturated(self, router):
        """T5: last-active endpoint conc=-1 (unlimited) → never saturated → chain head = last-active."""
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=-1)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base('http://a-api'), 'model-a')

        # Even if we occupy the other pool, unlimited last-active is never full.
        _occupy_pool(router, 'http://b-api', 1, n_holders=1)

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        assert bases[0] == 'http://a-api', f"expected unlimited last-active kept at head, got {bases}"

    def test_t6_absent_pool_reads_as_free(self, router):
        """T6: last-active on an endpoint whose pool was never created → reads as free → chain head = last-active.

        Guards the scheduler.py `pool else 0` branch (absent pool → count_active returns 0).
        """
        # conc=1 but we NEVER call _get_or_create_pool / acquire, so the pool is absent.
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base('http://a-api'), 'model-a')

        # Sanity: the pool truly is absent → count_active reads 0.
        assert router.scheduler.count_active('http://a-api', 1) == 0

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        assert bases[0] == 'http://a-api', f"expected absent-pool last-active kept at head, got {bases}"

    def test_t7_first_match_wins_for_duplicate_last_active_key(self, router):
        """T7: two enabled endpoints share the SAME (api_base, model) but differ in config.

        The pre-change code did `break` on the FIRST match; the capacity-aware rewrite must
        preserve first-match-wins for _la_ep so the healthy path stays byte-identical. If it
        instead kept scanning and took the LAST match, a duplicate-key endpoint with different
        max_input_tokens would silently change which config is handed out.

        Setup: endpoints 'a' (max_input_tokens=100) and 'b' (max_input_tokens=999) both serve
        model-d @ http://d-api with conc=-1 (never saturated). last-active = that shared key.
        The fixture default general_limit is 0, so no substitution happens and the endpoint's
        TRUE max_input_tokens is what reaches the chain → first-match-wins must yield 100.
        """
        _add_endpoint(router, 'a', 'http://d-api', model='model-d', concurrency_limit=-1)
        _add_endpoint(router, 'b', 'http://d-api', model='model-d', concurrency_limit=-1)
        # Give each a distinct max_input_tokens so we can tell them apart in the chain.
        with router._lock:
            for e in router.endpoints.values():
                if e.name == 'a':
                    e.max_input_tokens = 100
                elif e.name == 'b':
                    e.max_input_tokens = 999

        # last-active points at the shared (base, model) key; neither pool is saturated.
        with router._lock:
            router._last_active_endpoint = (normalize_api_base('http://d-api'), 'model-d')

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        assert bases[0] == 'http://d-api'
        # First-match-wins: the FIRST endpoint ('a', max_input_tokens=100) is chosen, not 'b' (999).
        assert chain[0]['max_input_tokens'] == 100, \
            f"expected first-match-wins (max_input_tokens=100), got {chain[0]['max_input_tokens']}"

    # ------------------------------------------------------------------
    # T8-T11 — Tier 1.5 self-saturation fix (count_active_excluding).
    # A conc=0 agent that already holds the shared pool must NOT count its OWN
    # permit as saturation, or it first-fits away from the endpoint it is about
    # to call. Another agent holding it still counts → liveness fix intact.
    # ------------------------------------------------------------------

    def test_t8_self_holder_not_saturated_keeps_last_active(self, router):
        """T8 (the fix): requesting instance's OWN name is the sole holder of the shared pool.

        Unassigned agent on conc=0 endpoint A; its own instance_name already holds
        '_shared_sequential_slot_'. Self-exclusion → not saturated → chain head == A
        (NOT first-fit to another endpoint). This is repro Scenario A2.
        """
        base = 'http://a-api'
        _add_endpoint(router, 'a', base, model='model-a', concurrency_limit=0)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base(base), 'model-a')

        # The requesting instance's OWN holder is the sole occupant of the shared pool.
        pool = router.scheduler._get_or_create_pool(base, 0)
        assert pool.key == '_shared_sequential_slot_'
        own_name = 'Security_guard'
        pool._running[own_name] = SlotHolder(
            agent_name='security', instance_name=own_name, acquisition_id=1,
        )

        chain = router.get_endpoint_chain('security', instance_name=own_name)
        head = chain[0]
        assert head['api_base'] == base and head['model'] == 'model-a', \
            f"expected self-held conc=0 last-active kept at head, got {head}"

    def test_t9_other_holder_still_saturated_routes_away(self, router):
        """T9 (liveness preserved): a DIFFERENT instance holds the shared pool → still saturated.

        Same setup as T8 but the holder is NOT the requesting instance → first-fit must
        still route to the free endpoint. Pins that self-exclusion does not regress L147.
        """
        base = 'http://a-api'
        _add_endpoint(router, 'a', base, model='model-a', concurrency_limit=0)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base(base), 'model-a')

        pool = router.scheduler._get_or_create_pool(base, 0)
        assert pool.key == '_shared_sequential_slot_'
        # Someone ELSE holds the shared pool — self-exclusion must not hide this.
        pool._running['other-agent'] = SlotHolder(
            agent_name='other', instance_name='other-agent', acquisition_id=1,
        )

        chain = router.get_endpoint_chain('security', instance_name='Security_guard')
        head = chain[0]
        # First-fit must land on the free endpoint B (conc=1, own pool), not just "not A".
        assert head['api_base'] == 'http://b-api' and head['model'] == 'model-b', \
            f"expected first-fit to free endpoint B, got {head}"

    def test_t10_conc_gt_0_self_holder_keeps_last_active(self, router):
        """T10 (conc>0 unaffected — MANDATORY): conc=2 endpoint A, sole holder is the requesting
        instance on its own per-base pool → chain head == A (already worked before; guards
        against an over-broad change to count_active_excluding).
        """
        base = 'http://a-api'
        _add_endpoint(router, 'a', base, model='model-a', concurrency_limit=2)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base(base), 'model-a')

        pool = router.scheduler._get_or_create_pool(base, 2)
        assert pool.key == normalize_api_base(base)  # per-base pool, not the shared one
        own_name = 'Security_guard'
        pool._running[own_name] = SlotHolder(
            agent_name='security', instance_name=own_name, acquisition_id=1,
        )

        chain = router.get_endpoint_chain('security', instance_name=own_name)
        head = chain[0]
        assert head['api_base'] == base and head['model'] == 'model-a', \
            f"expected conc>0 self-held last-active kept at head, got {head}"

    def test_t11_conc0_empty_shared_pool_keeps_last_active(self, router):
        """T11 (conc=0 healthy path — MANDATORY): unassigned agent, conc=0 last-active A, shared
        pool EMPTY, instance_name provided → chain head == A (last-active kept). Mirrors T3 but
        with concurrency_limit=0; guards that self-exclusion does not alter the non-saturated path.
        """
        base = 'http://a-api'
        _add_endpoint(router, 'a', base, model='model-a', concurrency_limit=0)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base(base), 'model-a')

        # Create the shared pool but leave it EMPTY.
        pool = router.scheduler._get_or_create_pool(base, 0)
        assert pool.key == '_shared_sequential_slot_'
        assert len(pool._running) == 0

        chain = router.get_endpoint_chain('security', instance_name='Security_guard')
        head = chain[0]
        assert head['api_base'] == base and head['model'] == 'model-a', \
            f"expected empty-pool conc=0 last-active kept at head, got {head}"

    def test_t12_selfsat_scenario_c_slot_and_chain_agree(self, router):
        """Discriminator (Scenario-C invariant): after acquiring a conc=0 slot on an unassigned
        agent, the instance-aware resolvers must agree on api_base:
            get_effective_slot_info(...)['api_base'] == get_endpoint_chain(...)[0]['api_base'].
        Do NOT compare against get_llm_config (no instance_name → still sees self-saturation).
        """
        base = 'http://a-api'
        _add_endpoint(router, 'a', base, model='model-a', concurrency_limit=0)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)
        with router._lock:
            router._last_active_endpoint = (normalize_api_base(base), 'model-a')

        # Simulate the slot acquire that production performs before the first LLM turn.
        pool = router.scheduler._get_or_create_pool(base, 0)
        assert pool.key == '_shared_sequential_slot_'
        own_name = 'Security_guard'
        pool._running[own_name] = SlotHolder(
            agent_name='security', instance_name=own_name, acquisition_id=1,
        )

        slot_info = router.get_effective_slot_info('security', instance_name=own_name)
        chain_head = router.get_endpoint_chain('security', instance_name=own_name)[0]
        assert slot_info['api_base'] == chain_head['api_base'], \
            f"slot api_base {slot_info['api_base']} != chain head api_base {chain_head['api_base']}"


# ============================================================================
# Tier 1.5 "last-released ENDPOINT" preference (capability matching)
#
# The racy global _last_active_endpoint marker can be overwritten by another agent
# between a caller's release and the spawned Security check's resolution, sending the
# guard to an endpoint NOT capable of what the caller was doing. Fix: record the
# just-released holder's committed ENDPOINT in release_slot_permit (single funnel) and
# let Tier 1.5 lead with it for unassigned agents. We populate REAL SlotPools via
# router.scheduler._get_or_create_pool(...) + insert real SlotHolder objects — we do NOT
# patch count_active (that would skip the endpoint→pool mapping, the part most likely wrong).
# ============================================================================


class _ReleaseHolder:
    """Minimal release_slot_permit holder wired to a real pool's api_router.

    Mirrors AgentInstance's surface for the release path: a state lock, a live
    _slot_release callback (the scheduler's release cb), and a _pool_ref whose
    .api_router is the router under test. The committed map is populated on that
    router directly (same write shape as call_with_fallback success at router.py:2149).
    """

    def __init__(self, router, pool):
        import threading
        self._state_lock = threading.Lock()
        self._slot_release = None  # set by caller after scheduler.acquire()
        self._slot_key = pool.key if pool is not None else None
        self._pool_ref = _PoolRef(router)


class _PoolRef:
    """Stand-in for the pool object that release_slot_permit reads .api_router from."""

    def __init__(self, router):
        self.api_router = router


def _release_and_record(router, holder_name, api_base, concurrency_limit, model):
    """Acquire a slot on (api_base, conc), commit the endpoint, then release.

    Returns (pool, released_ok). The committed-map entry is written under the lock exactly
    as call_with_fallback's success path does; release_slot_permit then records it into
    router._last_released_endpoint BEFORE popping the committed map.
    """
    pool = router.scheduler._get_or_create_pool(api_base, concurrency_limit)
    rel = router.scheduler.acquire(
        api_base, concurrency_limit, instance_name=holder_name, agent_class='coder')
    with router._lock:
        router._instance_committed_endpoint[holder_name] = (normalize_api_base(api_base), model)
    h = _ReleaseHolder(router, pool)
    if rel is not None:
        h._slot_release = rel
    ok = release_slot_permit(h, holder_name, action='drop-handoff', pool=pool)
    return pool, ok


class TestLastReleasedEndpointPreference:

    def test_prefers_last_released_over_racy_global(self, router):
        """The exact user scenario: caller commits A; another agent overwrites the GLOBAL
        marker to B; caller releases (records A); unassigned Security resolves → chain[0] is A.

        Without the fix, Tier 1.5 would read the racy global (B) and hand out B's model.
        """
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=1)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)

        # Caller commits A (success path).
        pool_a, ok = _release_and_record(router, 'caller1', 'http://a-api', 1, 'model-a')
        assert ok is True
        # Another agent succeeds on B → overwrites the racy GLOBAL marker.
        with router._lock:
            router._last_active_endpoint = (normalize_api_base('http://b-api'), 'model-b')

        # Release recorded A into _last_released_endpoint (BEFORE the committed pop).
        assert router._last_released_endpoint == (normalize_api_base('http://a-api'), 'model-a'), \
            f"release should have recorded A, got {router._last_released_endpoint}"

        chain = router.get_endpoint_chain('security', instance_name='sec1')
        # Chain head must be A's model, NOT the racy global B.
        assert chain[0]['model'] == 'model-a', \
            f"expected last-released A at head, got {[(c['model'], c['api_base']) for c in chain]}"
        assert chain[0]['api_base'] == 'http://a-api'

    def test_conc0_collapse_still_prefers_endpoint(self, router):
        """A and B both conc=0 (shared pool). Same as above; assert chain[0] model == A's model.

        Proves capability survives the conc=0 collapse — endpoint identity (model) is what we
        record, not slot/pool identity (which collapses to _shared_sequential_slot_). This is the
        repro's key finding: chain[0] differs (model-a vs model-b) even under shared-pool collapse.
        """
        base = 'http://shared-api'
        _add_endpoint(router, 'a', base, model='model-a', concurrency_limit=0)
        _add_endpoint(router, 'b', base, model='model-b', concurrency_limit=0)

        # Caller commits A (conc=0 → shared sequential pool).
        pool_a, ok = _release_and_record(router, 'caller1', base, 0, 'model-a')
        assert ok is True
        assert pool_a.key == '_shared_sequential_slot_'
        # Another agent succeeds on B → overwrites the racy GLOBAL marker.
        with router._lock:
            router._last_active_endpoint = (normalize_api_base(base), 'model-b')

        chain = router.get_endpoint_chain('security', instance_name='sec1')
        # Even though both share one pool, the ENDPOINT identity (model-a) must win.
        assert chain[0]['model'] == 'model-a', \
            f"expected A's model to survive conc=0 collapse, got {[(c['model'], c['api_base']) for c in chain]}"

    def test_falls_through_when_last_released_saturated(self, router):
        """A saturated at resolve time → unassigned resolves to a free endpoint via first-fit.

        Never B, never worse than baseline: the existing capacity-aware first-fit routes away
        from the saturated last-released endpoint automatically (no extra code).
        """
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=1)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)

        # Caller commits A and releases (records A).
        pool_a, ok = _release_and_record(router, 'caller1', 'http://a-api', 1, 'model-a')
        assert ok is True
        with router._lock:
            router._last_active_endpoint = (normalize_api_base('http://b-api'), 'model-b')
        assert router._last_released_endpoint == (normalize_api_base('http://a-api'), 'model-a')

        # Saturate A's pool (capacity 1) → last-released endpoint is full.
        _occupy_pool(router, 'http://a-api', 1, n_holders=1)

        chain = router.get_endpoint_chain('security', instance_name='sec1')
        bases = [c['api_base'] for c in chain]
        # Must route to the free endpoint B (first-fit), not the saturated A.
        assert bases[0] == 'http://b-api', \
            f"expected first-fit escape to free B, got {[(c['model'], c['api_base']) for c in chain]}"

    def test_cold_start_uses_global_marker(self, router):
        """_last_released_endpoint is None (no release yet) → behavior identical to the current
        global-marker path. No regression on cold start."""
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=1)
        _add_endpoint(router, 'b', 'http://b-api', model='model-b', concurrency_limit=1)

        # No release has happened → field stays None.
        assert router._last_released_endpoint is None
        with router._lock:
            router._last_active_endpoint = (normalize_api_base('http://a-api'), 'model-a')

        chain = router.get_endpoint_chain('security', instance_name='sec1')
        bases = [c['api_base'] for c in chain]
        # Global-marker path: head is last-active A.
        assert bases[0] == 'http://a-api', \
            f"expected global-marker A at head on cold start, got {[(c['model'], c['api_base']) for c in chain]}"

    def test_release_funnel_records_committed_endpoint_before_pop(self, router):
        """Release-funnel unit test: release_slot_permit records the holder's committed endpoint
        into _last_released_endpoint, and does so BEFORE the committed-map pop.

        The ordering is pinned by asserting BOTH that the field is set AND that the committed map
        was subsequently cleared (popped) — if the read ran after the pop it would record nothing
        and the field would stay None.
        """
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=1)

        pool_a, ok = _release_and_record(router, 'caller1', 'http://a-api', 1, 'model-a')
        assert ok is True
        expected = (normalize_api_base('http://a-api'), 'model-a')
        # The field was recorded from the committed map.
        assert router._last_released_endpoint == expected, \
            f"expected {expected}, got {router._last_released_endpoint}"
        # ...and the committed-map entry was popped (the read happened BEFORE this pop).
        with router._lock:
            assert 'caller1' not in router._instance_committed_endpoint, \
                'committed map should be cleared after release'

    def test_release_no_committed_entry_leaves_field_unchanged(self, router):
        """Edge case: caller had no successful call (committed map empty) → guarded read finds None
        → field stays as-is (previous value or None). No crash.

        The holder acquires a slot but NEVER commits an endpoint (no successful call), so the
        committed map has no entry for it. release_slot_permit's guarded read finds None and must
        leave _last_released_endpoint untouched rather than clobbering it with None.
        """
        _add_endpoint(router, 'a', 'http://a-api', model='model-a', concurrency_limit=1)

        # Seed a previous value so we can prove it is NOT clobbered.
        with router._lock:
            router._last_released_endpoint = (normalize_api_base('http://b-api'), 'model-b')

        pool_a = router.scheduler._get_or_create_pool('http://a-api', 1)
        rel = router.scheduler.acquire(
            'http://a-api', 1, instance_name='caller1', agent_class='coder')
        # NOTE: no _instance_committed_endpoint write — the caller never succeeded.
        h = _ReleaseHolder(router, pool_a)
        if rel is not None:
            h._slot_release = rel
        ok = release_slot_permit(h, 'caller1', action='drop-handoff', pool=pool_a)
        assert ok is True

        # Field unchanged — the empty committed read recorded nothing (did NOT set to None).
        assert router._last_released_endpoint == (normalize_api_base('http://b-api'), 'model-b')

    def test_second_reacquire_updates_last_released_endpoint(self, router):
        """THE regression: a holder that does TWO yield/reacquire cycles must keep updating
        _last_released_endpoint to whatever endpoint it currently holds — not freeze at the
        first release's endpoint.

        Before the fix, the committed marker (popped on the first release) was never repopulated
        by a re-acquire whose LLM call never ran, so the second release recorded nothing and the
        field stayed frozen at A. The acquire-time held-endpoint marker now backs the read.
        """
        base_a = 'http://a-api'
        base_b = 'http://b-api'
        _add_endpoint(router, 'a', base_a, model='model-a', concurrency_limit=0)
        _add_endpoint(router, 'b', base_b, model='model-b', concurrency_limit=0)
        # Give the holder its own Tier-1 chain [A, B] so cursor rotation can move it A → B.
        router.set_agent_priorities('coder', ['ep_a', 'ep_b'])

        key_a = (normalize_api_base(base_a), 'model-a')
        key_b = (normalize_api_base(base_b), 'model-b')

        # ── Cycle 1: acquire on A, note held endpoint, release → records A. ────────────────
        pool = router.scheduler._get_or_create_pool(base_a, 0)
        rel = router.scheduler.acquire(base_a, 0, instance_name='holder', agent_class='coder')
        # Simulate the acquire-time writeback (what Funnel A/B now do via note_held_endpoint).
        router.note_held_endpoint('coder', 'holder')
        with router._lock:
            assert router._instance_held_endpoint.get('holder') == key_a, \
                f"held marker should be A after first acquire, got {router._instance_held_endpoint.get('holder')}"
        h = _ReleaseHolder(router, pool)
        if rel is not None:
            h._slot_release = rel
        ok = release_slot_permit(h, 'holder', action='drop-handoff', pool=pool)
        assert ok is True
        # Release recorded A; BOTH markers were popped (read-before-pop ordering preserved).
        assert router._last_released_endpoint == key_a, \
            f"first release should record A, got {router._last_released_endpoint}"
        with router._lock:
            assert 'holder' not in router._instance_committed_endpoint
            assert 'holder' not in router._instance_held_endpoint

        # ── Cycle 2: cursor rotates to B; re-acquire notes held=B; release → records B. ─────
        # Kick the instance past A so get_endpoint_chain resolves to B (the real yield/reacquire
        # resolution path that note_held_endpoint reads).
        router.advance_instance_endpoint('holder')
        pool_b = router.scheduler._get_or_create_pool(base_b, 0)
        rel2 = router.scheduler.acquire(base_b, 0, instance_name='holder', agent_class='coder')
        router.note_held_endpoint('coder', 'holder')
        with router._lock:
            assert router._instance_held_endpoint.get('holder') == key_b, \
                f"held marker should be B after re-acquire, got {router._instance_held_endpoint.get('holder')}"
        h2 = _ReleaseHolder(router, pool_b)
        if rel2 is not None:
            h2._slot_release = rel2
        ok2 = release_slot_permit(h2, 'holder', action='drop-handoff', pool=pool_b)
        assert ok2 is True
        # THE fix: the second release records B (the endpoint just held), NOT the frozen A.
        assert router._last_released_endpoint == key_b, \
            f"second release should record B (not frozen at A), got {router._last_released_endpoint}"

    def test_release_prefers_committed_over_held(self, router):
        """When BOTH committed and held markers are present for a holder, release uses the
        COMMITTED value (preserves existing behavior). The held marker is only a fallback."""
        base_a = 'http://a-api'
        base_b = 'http://b-api'
        _add_endpoint(router, 'a', base_a, model='model-a', concurrency_limit=0)
        _add_endpoint(router, 'b', base_b, model='model-b', concurrency_limit=0)

        key_a = (normalize_api_base(base_a), 'model-a')
        key_b = (normalize_api_base(base_b), 'model-b')

        pool = router.scheduler._get_or_create_pool(base_a, 0)
        rel = router.scheduler.acquire(base_a, 0, instance_name='holder', agent_class='coder')
        # Seed BOTH markers with DIFFERENT values: committed=A (a real call succeeded), held=B.
        with router._lock:
            router._instance_committed_endpoint['holder'] = key_a
            router._instance_held_endpoint['holder'] = key_b

        h = _ReleaseHolder(router, pool)
        if rel is not None:
            h._slot_release = rel
        ok = release_slot_permit(h, 'holder', action='drop-handoff', pool=pool)
        assert ok is True
        # Committed wins over held.
        assert router._last_released_endpoint == key_a, \
            f"committed should win over held, got {router._last_released_endpoint}"
        with router._lock:
            assert 'holder' not in router._instance_committed_endpoint
            assert 'holder' not in router._instance_held_endpoint

    def test_note_held_endpoint_does_not_touch_committed(self, router):
        """Probe-gate isolation guard: note_held_endpoint writes ONLY _instance_held_endpoint.
        It must never write to _instance_committed_endpoint (the sanity-probe fast-path gate) —
        otherwise an instance that acquires a slot but never completes a call would skip the probe."""
        base_a = 'http://a-api'
        _add_endpoint(router, 'a', base_a, model='model-a', concurrency_limit=0)
        # Assign the endpoint to 'coder' so note_held_endpoint resolves Tier-1 (not Tier-4 default).
        router.set_agent_priorities('coder', ['ep_a'])

        router.note_held_endpoint('coder', 'holder')
        with router._lock:
            # Held marker was written...
            assert router._instance_held_endpoint.get('holder') == (normalize_api_base(base_a), 'model-a'), \
                f"held marker should be set, got {router._instance_held_endpoint.get('holder')}"
            # ...and the committed probe-gate marker must remain absent.
            assert 'holder' not in router._instance_committed_endpoint, \
                'note_held_endpoint must NOT write to the committed probe-gate marker'

        # Also verify it stays absent when a prior committed value exists (no overwrite).
        with router._lock:
            router._instance_committed_endpoint['other'] = ('sentinel', 'sentinel')
        router.note_held_endpoint('coder', 'holder')
        with router._lock:
            assert 'holder' not in router._instance_committed_endpoint
