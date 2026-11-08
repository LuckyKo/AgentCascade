"""Cache-waste diagnosis telemetry (borrow-audit #7).

Covers the per-instance prev-snapshot miss detector added to TelemetryCollector:
  * the missed-token formula  ``missed = min(prev_prompt, cur_prompt) - cached``
  * the STICKY reported_cache guard that keeps a never-reports-cache provider
    (e.g. a backend that omits cached_tokens) from being miscounted as waste,
  * the noise-floor threshold,
  * the three root-cause tags (model_change / idle_ttl / other),
  * the compression reset (post-compression smaller prompt must NOT count),
  * session + per-config waste totals,
  * the optional $-cost gated on MODEL_PRICING.

Mirrors the fixture + call style of tests/test_telemetry.py.
"""

import pytest

import agent_cascade.telemetry as telemetry_module
from agent_cascade.telemetry import CACHE_IDLE_TTL_MS, TelemetryCollector


@pytest.fixture
def collector(tmp_path):
    """A fresh TelemetryCollector writing into an isolated temp dir."""
    return TelemetryCollector(log_dir=str(tmp_path), instance_id='cache_waste_test')


def _classify(collector, inst, prompt, cached, model='m'):
    """Drive one classified LLM call (start → usage → end) on ``collector``."""
    collector.record_llm_call_start(inst, model=model)
    collector.record_token_usage(inst, prompt_tokens=prompt, completion_tokens=1,
                                  details={'cached_tokens': cached})
    collector.record_llm_call_end(inst)


def _wasted_events(collector):
    return [e for e in collector.events
            if e.get('type') == 'llm_call' and 'waste_root_cause' in e]


class TestCacheWasteDetection:

    # 1. no prev → no waste.
    def test_first_call_on_instance_is_never_waste(self, collector):
        _classify(collector, 'inst', prompt=5000, cached=0)
        s = collector.get_session_summary()
        assert s['llm_wasted_tokens'] == 0
        assert s['llm_waste_count'] == 0
        assert _wasted_events(collector) == []

    # 2. hit (high cached) → low missed (below noise floor, not counted).
    def test_high_cache_hit_is_below_noise_floor(self, collector):
        _classify(collector, 'inst', prompt=5000, cached=4000)  # baseline, reported_cache True
        _classify(collector, 'inst', prompt=5000, cached=4500)  # missed = 500 < 1024 → not counted
        s = collector.get_session_summary()
        assert s['llm_waste_count'] == 0
        assert s['llm_wasted_tokens'] == 0

    # 3. total miss (prev reported cache, cached=0) → counted.
    def test_total_miss_after_reporting_baseline_is_counted(self, collector):
        _classify(collector, 'inst', prompt=5000, cached=4000)  # reported_cache becomes True
        _classify(collector, 'inst', prompt=5000, cached=0)     # missed = 5000 > 1024 → counted
        s = collector.get_session_summary()
        assert s['llm_waste_count'] == 1
        assert s['llm_wasted_tokens'] == 5000
        evs = _wasted_events(collector)
        assert len(evs) == 1
        assert evs[0]['wasted_tokens'] == 5000

    # 4. provider never reports (prev cached=0, cur cached=0) → NOT counted.
    #    Sticky reported_cache rule: a backend that omits cached_tokens entirely
    #    must never be blamed for "wasting" its (never-claimed) cache.
    def test_never_reporting_provider_is_not_counted(self, collector):
        _classify(collector, 'inst', prompt=5000, cached=0)  # prev reported_cache stays False
        _classify(collector, 'inst', prompt=5000, cached=0)  # sticky guard excludes → no waste
        s = collector.get_session_summary()
        assert s['llm_waste_count'] == 0
        assert s['llm_wasted_tokens'] == 0

    # 5. missed <= noise floor → not counted (boundary is strictly >).
    def test_miss_at_noise_floor_is_not_counted(self, collector):
        _classify(collector, 'inst', prompt=5000, cached=4000)
        # missed = 5000 - 3976 = 1024, exactly the floor → not > floor → not counted.
        _classify(collector, 'inst', prompt=5000, cached=3976)
        s = collector.get_session_summary()
        assert s['llm_waste_count'] == 0

    # 6. model change → root_cause='model_change'.
    def test_model_change_tags_root_cause(self, collector):
        _classify(collector, 'inst', prompt=5000, cached=4000, model='A')
        _classify(collector, 'inst', prompt=5000, cached=0, model='B')
        evs = _wasted_events(collector)
        assert len(evs) == 1
        assert evs[0]['waste_root_cause'] == 'model_change'

    # 7. same model, idle > TTL → root_cause='idle_ttl'.
    def test_idle_beyond_ttl_tags_root_cause(self, collector):
        _classify(collector, 'inst', prompt=5000, cached=4000, model='A')
        # Push the previous call's timestamp far enough back that the next call's
        # idle gap exceeds CACHE_IDLE_TTL_MS (simulates an evicted cold cache).
        collector._cache_scan_state['inst']['prev_ts_ms'] -= (CACHE_IDLE_TTL_MS + 1000)
        _classify(collector, 'inst', prompt=5000, cached=0, model='A')
        evs = _wasted_events(collector)
        assert len(evs) == 1
        assert evs[0]['waste_root_cause'] == 'idle_ttl'
        assert evs[0]['idle_ms'] > CACHE_IDLE_TTL_MS

    # 8. same model, idle<=TTL, cached=0, prev reported → root_cause='other'.
    def test_same_model_active_cache_miss_tags_other(self, collector):
        _classify(collector, 'inst', prompt=5000, cached=4000, model='A')
        _classify(collector, 'inst', prompt=5000, cached=0, model='A')
        evs = _wasted_events(collector)
        assert len(evs) == 1
        assert evs[0]['waste_root_cause'] == 'other'

    # 9. compression resets prev → post-compression smaller prompt NOT counted
    #    (the qwen3.8-flash Compressor 0%-hit scenario).
    def test_compression_resets_baseline(self, collector):
        _classify(collector, 'inst', prompt=5000, cached=4000)
        # Without the reset, this smaller cached=0 prompt would count as
        # min(5000,3000)-0 = 3000 tokens of waste.
        collector.record_compression('inst', fraction=0.5, tokens_before=5000, tokens_after=3000)
        _classify(collector, 'inst', prompt=3000, cached=0)
        s = collector.get_session_summary()
        assert s['llm_waste_count'] == 0
        assert s['llm_wasted_tokens'] == 0

    # 10. session + per-config counters accumulate.
    def test_session_and_per_config_counters_accumulate(self, collector):
        fp = TelemetryCollector.fingerprint_config(model='m')
        # Open a turn (creates the config row and lets record_llm_call_start
        # snapshot the fingerprint) but do NOT end it before the calls, so each
        # call attributes to the fp.
        collector.record_turn_start('inst', config_fingerprint=fp,
                                    config_description={'model': 'm'})
        _classify(collector, 'inst', prompt=5000, cached=4000)  # baseline, no waste
        _classify(collector, 'inst', prompt=5000, cached=0)     # +5000
        _classify(collector, 'inst', prompt=5000, cached=0)     # +5000
        s = collector.get_session_summary()
        assert s['llm_waste_count'] == 2
        assert s['llm_wasted_tokens'] == 10000

        cfgs = {c['config_fingerprint']: c for c in collector.get_config_comparison()}
        assert cfgs[fp]['waste_count'] == 2
        assert cfgs[fp]['wasted_tokens'] == 10000
        # Existing hit/miss fields must still reconcile (no regression).
        assert cfgs[fp]['cache_hits'] == 1
        assert cfgs[fp]['cache_misses'] == 2

    # 11. optional cost: with pricing dict → computed; without → absent.
    def test_optional_cost_gated_on_pricing(self, collector, monkeypatch):
        monkeypatch.setattr(
            telemetry_module, 'MODEL_PRICING',
            {'m': {'input_per_m': 3.0, 'cache_read_per_m': 0.3}},
        )
        _classify(collector, 'inst', prompt=5000, cached=4000)
        _classify(collector, 'inst', prompt=5000, cached=0)  # missed=5000 → cost=5000*2.7/1e6
        s = collector.get_session_summary()
        assert 'llm_wasted_cost' in s
        assert s['llm_wasted_cost'] == round(5000 * (3.0 - 0.3) / 1e6, 6)

    # 11b. without pricing the cost key is absent (token waste is the default).
    def test_cost_absent_without_pricing(self, collector, monkeypatch):
        monkeypatch.setattr(telemetry_module, 'MODEL_PRICING', {})
        _classify(collector, 'inst', prompt=5000, cached=4000)
        _classify(collector, 'inst', prompt=5000, cached=0)
        s = collector.get_session_summary()
        assert 'llm_wasted_cost' not in s
        # Token waste is still measured provider-agnostically.
        assert s['llm_wasted_tokens'] == 5000
        assert s['llm_waste_count'] == 1


class TestDetectCacheWastePure:
    """The detector is pure and total — verify it in isolation."""

    def test_none_when_no_prev(self):
        assert TelemetryCollector.detect_cache_waste(
            None, 5000, 0, 'm', 1000.0) is None

    def test_none_when_no_prompt(self):
        prev = {'prev_prompt_tokens': 5000, 'prev_ts_ms': 0, 'prev_model': 'm',
                'reported_cache': True}
        assert TelemetryCollector.detect_cache_waste(prev, 0, 0, 'm', 100.0) is None

    def test_bad_input_degrades_to_none(self):
        # Must never raise on malformed values (mirrors classify_cache_hit).
        assert TelemetryCollector.detect_cache_waste('junk', 'junk', 'junk', None, 'x') is None
