"""Cost Slice 1 — per-endpoint pricing → session cost (see-spend).

Pins the additive, default-off cost feature end to end:
  * ``cost.tokens_to_cost`` arithmetic (cache-aware), including absent-key
    defaults and ``None`` when unpriced (the no-misleading-$0 contract),
  * ``get_session_summary`` key-presence gating — ``session_cost`` present iff
    a priced call was measured, absent otherwise (default-off byte-identical),
  * ``APIEndpoint`` cost-field round-trip (G6 — a wrong field name would fail
    silently through the hasattr/from_dict gates).

Mirrors the fixture + call style of tests/test_telemetry_cache_waste.py.
"""

import os
from unittest.mock import patch

import pytest

from agent_cascade.api_router import APIRouter
from agent_cascade.api_router_pkg.endpoints import APIEndpoint
from agent_cascade.cost import is_priced, tokens_to_cost
from agent_cascade.telemetry import TelemetryCollector


@pytest.fixture
def collector(tmp_path):
    """A fresh TelemetryCollector writing into an isolated temp dir."""
    return TelemetryCollector(log_dir=str(tmp_path), instance_id='cost_slice1_test')


def _measured_call(collector, inst, prompt, cached, completion, pricing=None, model='m'):
    """Drive one server-reported (cache-classified) LLM call so cost is measurable.

    Only calls the server reported usage for are cost-measured (telemetry gates
    cost on ``cache_classified``), so we must supply usage details here.
    """
    collector.record_llm_call_start(inst, model=model, pricing=pricing)
    collector.record_token_usage(inst, prompt_tokens=prompt, completion_tokens=completion,
                                  details={'cached_tokens': cached})
    collector.record_llm_call_end(inst)


class TestTokensToCostPure:
    """cost.py is the single source of truth — verify the math in isolation."""

    def test_basic_input_output(self):
        c = tokens_to_cost(
            {'prompt_tokens': 1000, 'completion_tokens': 100, 'cached_tokens': 0},
            {'input_per_m': 10.0, 'output_per_m': 30.0},
        )
        assert c == pytest.approx((1000 * 10.0 + 100 * 30.0) / 1e6)

    def test_cache_split(self):
        # 600 non-cached at 10, 400 cached at 1, no output.
        c = tokens_to_cost(
            {'prompt_tokens': 1000, 'completion_tokens': 0, 'cached_tokens': 400},
            {'input_per_m': 10.0, 'cache_read_per_m': 1.0},
        )
        assert c == pytest.approx((600 * 10.0 + 400 * 1.0) / 1e6)

    def test_cache_write_term(self):
        c = tokens_to_cost(
            {'prompt_tokens': 100, 'completion_tokens': 0, 'cached_tokens': 0,
             'cache_write_tokens': 500},
            {'input_per_m': 0.0, 'cache_write_per_m': 20.0},
        )
        assert c == pytest.approx(500 * 20.0 / 1e6)

    def test_absent_keys_default_zero(self):
        # Only input priced; missing output/cache keys contribute nothing.
        c = tokens_to_cost(
            {'prompt_tokens': 1000, 'completion_tokens': 999},  # output priced 0
            {'input_per_m': 1.0},
        )
        assert c == pytest.approx(1000 * 1.0 / 1e6)

    def test_cached_cannot_exceed_prompt(self):
        # Defensive: a bogus cached > prompt must not make non-cached negative.
        c = tokens_to_cost(
            {'prompt_tokens': 100, 'completion_tokens': 0, 'cached_tokens': 999},
            {'input_per_m': 10.0, 'cache_read_per_m': 1.0},
        )
        assert c == pytest.approx(100 * 1.0 / 1e6)

    def test_returns_none_when_pricing_none(self):
        assert tokens_to_cost({'prompt_tokens': 100}, None) is None

    def test_returns_none_when_pricing_empty(self):
        assert tokens_to_cost({'prompt_tokens': 100}, {}) is None

    def test_returns_none_when_all_rates_zero(self):
        assert tokens_to_cost(
            {'prompt_tokens': 100, 'completion_tokens': 10},
            {'input_per_m': 0.0, 'output_per_m': 0.0,
             'cache_read_per_m': 0.0, 'cache_write_per_m': 0.0},
        ) is None


class TestIsPriced:
    def test_none_and_empty(self):
        assert is_priced(None) is False
        assert is_priced({}) is False

    def test_all_zero_is_unpriced(self):
        assert is_priced({'input_per_m': 0.0, 'output_per_m': 0.0}) is False

    def test_any_nonzero_is_priced(self):
        assert is_priced({'cache_write_per_m': 0.01}) is True


class TestSessionCostGating:
    """session_cost present iff a priced call was measured; absent otherwise."""

    def test_present_when_priced(self, collector):
        pricing = {'input_per_m': 10.0, 'output_per_m': 30.0}
        _measured_call(collector, 'inst', prompt=1000, cached=0,
                       completion=100, pricing=pricing)
        s = collector.get_session_summary()
        assert 'session_cost' in s
        assert s['session_cost'] == round((1000 * 10.0 + 100 * 30.0) / 1e6, 6)

    def test_accumulates_across_calls(self, collector):
        pricing = {'input_per_m': 10.0, 'output_per_m': 30.0}
        _measured_call(collector, 'inst', prompt=1000, cached=0, completion=100, pricing=pricing)
        _measured_call(collector, 'inst', prompt=1000, cached=0, completion=100, pricing=pricing)
        s = collector.get_session_summary()
        assert s['session_cost'] == round(2 * (1000 * 10.0 + 100 * 30.0) / 1e6, 6)

    def test_absent_when_unpriced(self, collector):
        # Same measured call but no pricing → feature off, key absent (G4/default-off).
        _measured_call(collector, 'inst', prompt=1000, cached=0, completion=100, pricing=None)
        s = collector.get_session_summary()
        assert 'session_cost' not in s

    def test_absent_when_pricing_all_zero(self, collector):
        _measured_call(collector, 'inst', prompt=1000, cached=0, completion=100,
                       pricing={'input_per_m': 0.0, 'output_per_m': 0.0})
        s = collector.get_session_summary()
        assert 'session_cost' not in s

    def test_unpriced_session_has_only_new_zero_field(self, collector):
        # Default-off: an unpriced session emits no cost key at all, so the
        # summary is unchanged from a fresh collector (no misleading $0).
        fresh = collector.get_session_summary()
        assert 'session_cost' not in fresh
        _measured_call(collector, 'inst', prompt=1000, cached=500, completion=100)
        after = collector.get_session_summary()
        # Everything except the counters that legitimately changed: still no cost key.
        assert 'session_cost' not in after


class TestEndpointCostRoundTrip:
    """G6: cost fields must round-trip to_dict/from_dict and default to 0.0."""

    COST_KEYS = ('input_per_m', 'output_per_m', 'cache_read_per_m', 'cache_write_per_m')

    def test_defaults_zero(self):
        ep = APIEndpoint()
        for k in self.COST_KEYS:
            assert getattr(ep, k) == 0.0

    def test_round_trip_preserves_values(self):
        ep = APIEndpoint(input_per_m=3.0, output_per_m=15.0,
                         cache_read_per_m=0.30, cache_write_per_m=3.75)
        d = ep.to_dict()
        for k in self.COST_KEYS:
            assert d[k] == getattr(ep, k)
        back = APIEndpoint.from_dict(d)
        for k in self.COST_KEYS:
            assert getattr(back, k) == getattr(ep, k)

    def test_legacy_config_without_cost_keys(self):
        # An existing persisted endpoint (no cost keys) decodes to 0.0 defaults.
        legacy = {'id': 'abc', 'name': 'old', 'api_base': 'http://x/v1', 'model': 'm'}
        ep = APIEndpoint.from_dict(legacy)
        for k in self.COST_KEYS:
            assert getattr(ep, k) == 0.0

    def test_unknown_key_is_dropped_not_stored(self):
        # from_dict filters to known fields (hasattr gate); a wrong cost field
        # name must not be silently accepted as if it were the real one.
        ep = APIEndpoint.from_dict({'input_per_m': 1.0, 'input_price_per_m': 99.0})
        assert ep.input_per_m == 1.0
        assert not hasattr(ep, 'input_price_per_m')


# ── Router-level resolution (get_endpoint_pricing — highest-risk new code) ─────────
# Must build a FRESH APIRouter inside a patched config dir so its persistence never
# touches the live gitignored config/api_endpoints.json (mirrors test_api_router_persistence).


@pytest.fixture
def router(tmp_path):
    """Fresh APIRouter whose persistence is confined to a per-test temp dir."""
    with patch.dict(os.environ, {'AGENT_CASCADE_TEST_CONFIG_DIR': str(tmp_path / 'config')}):
        r = APIRouter(default_llm_cfg={
            'api_base': 'http://default-api', 'model': 'default-model', 'max_tokens': 2048,
        })
        r._pool = None
        yield r


def _add(router, **overrides):
    ep = APIEndpoint(**overrides)
    router.endpoints[ep.id] = ep
    return ep


class TestRouterEndpointPricingResolution:
    """get_endpoint_pricing resolves from the CHAIN-HEAD endpoint, by (api_base, model)."""

    def test_resolves_pricing_for_assigned_priced_endpoint(self, router):
        ep = _add(router, name='priced', api_base='http://price', model='mm',
                  input_per_m=3.0, output_per_m=15.0, cache_read_per_m=0.30,
                  cache_write_per_m=0.0)
        router.agent_priorities = {'Coder': [ep.id]}

        got = router.get_endpoint_pricing('Coder')
        assert got == {'input_per_m': 3.0, 'output_per_m': 15.0,
                       'cache_read_per_m': 0.30, 'cache_write_per_m': 0.0}
        assert is_priced(got)

    def test_returns_none_for_unpriced_chain_head(self, router):
        ep = _add(router, name='free', api_base='http://free', model='mm')  # all rates 0.0
        router.agent_priorities = {'Coder': [ep.id]}

        assert router.get_endpoint_pricing('Coder') is None

    def test_resolution_follows_chain_head_not_any_priced_endpoint(self, router):
        # Proves lookup is driven by the chain head, NOT by scanning endpoints for the
        # first priced one: with a priced endpoint present in the pool, an agent whose
        # chain head is the UNPRICED endpoint must still resolve to None.
        priced = _add(router, name='p', api_base='http://p', model='pmodel', input_per_m=5.0)
        unpriced = _add(router, name='u', api_base='http://u', model='umodel')  # all 0.0
        # dict order puts 'priced' first — a scan-for-priced implementation would return it.
        router.agent_priorities = {'Coder': [unpriced.id]}
        assert router.get_endpoint_pricing('Coder') is None

        # Reassigning the chain head to the priced endpoint flips it to its pricing.
        router.agent_priorities = {'Coder': [priced.id]}
        assert router.get_endpoint_pricing('Coder')['input_per_m'] == 5.0
