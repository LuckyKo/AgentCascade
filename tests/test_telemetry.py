"""Focused unit tests for ``agent_cascade.telemetry.TelemetryCollector``.

Covers every telemetry entry point (each ``record_*`` method) and every field
surfaced by ``get_session_summary()`` — with particular attention to the
recently-added loop breakdown (``loops_outer`` / ``loops_inner`` / the
``loop_type`` parameter) and session-level auto-continue tracking
(``total_auto_continues``).

Design notes:
- The collector is directly instantiable, so no mocking is required.
- Each test builds its own collector on pytest's ``tmp_path`` fixture so tests
  are isolated and never pollute the real workspace telemetry directory.
- All tests are deterministic: no sleeps, network, or LLM calls.
"""

import pytest

from agent_cascade.telemetry import TelemetryCollector


@pytest.fixture
def collector(tmp_path):
    """A fresh TelemetryCollector writing into an isolated temp dir."""
    return TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')


# ---------------------------------------------------------------------------
# A. Session summary defaults
# ---------------------------------------------------------------------------


class TestSessionSummaryDefaults:

    def test_fresh_collector_all_zero_defaults(self, collector):
        s = collector.get_session_summary()
        assert s['total_turns'] == 0
        assert s['total_llm_calls'] == 0
        assert s['total_tool_calls'] == 0
        assert s['total_loops_detected'] == 0
        assert s['loops_outer'] == 0
        assert s['loops_inner'] == 0
        assert s['total_auto_continues'] == 0
        assert s['total_compressions'] == 0
        assert s['agent_instance_calls'] == 0

    def test_fresh_collector_token_defaults(self, collector):
        s = collector.get_session_summary()
        assert s['total_input_tokens_est'] == 0
        assert s['total_output_tokens_est'] == 0
        assert s['total_tokens'] == 0
        assert s['avg_tps'] == 0
        assert s['avg_llm_latency_ms'] == 0
        assert s['avg_tool_latency_ms'] == 0
        assert s['call_agent_count'] == 0
        assert s['call_agent_latency_ms'] == 0

    def test_fresh_collector_collections_empty(self, collector):
        s = collector.get_session_summary()
        assert s['llm_calls_by_model'] == {}
        assert s['tool_effectiveness'] == {}

    def test_all_expected_keys_present(self, collector):
        """Guard against a summary field being dropped from the returned dict."""
        expected_keys = {
            'session_id',
            'total_turns',
            'total_llm_calls',
            'total_tool_calls',
            'total_input_tokens_est',
            'total_output_tokens_est',
            'total_tokens',
            'avg_tps',
            'avg_llm_latency_ms',
            'avg_tool_latency_ms',
            'call_agent_count',
            'call_agent_latency_ms',
            'total_loops_detected',
            'loops_outer',
            'loops_inner',
            'total_auto_continues',
            'total_retries',
            'total_compressions',
            'write_failures',
            'agent_instance_calls',
            'llm_calls_by_model',
            'tool_effectiveness',
        }
        s = collector.get_session_summary()
        missing = expected_keys - set(s.keys())
        assert not missing, f"Missing summary keys: {missing}"

    def test_new_keys_present(self, collector):
        """The recently-added fields must exist in the dict (not just be 0)."""
        s = collector.get_session_summary()
        for key in ('loops_outer', 'loops_inner', 'total_auto_continues'):
            assert key in s


# ---------------------------------------------------------------------------
# B. Loop detection — loop_type breakdown (the key new feature)
# ---------------------------------------------------------------------------


class TestLoopDetection:

    def test_outer_loop_counts(self, collector):
        for _ in range(3):
            collector.record_loop_detected('inst', 'repeat tool call', loop_type='outer')
        s = collector.get_session_summary()
        assert s['loops_outer'] == 3
        assert s['total_loops_detected'] == 3
        assert s['loops_inner'] == 0

    def test_inner_loop_counts(self, collector):
        for _ in range(2):
            collector.record_loop_detected('inst', 'inner repeat', loop_type='inner')
        s = collector.get_session_summary()
        assert s['loops_inner'] == 2
        assert s['total_loops_detected'] == 2
        assert s['loops_outer'] == 0

    def test_mixed_invariant(self, collector):
        """The combined invariant: loops_outer + loops_inner == total_loops_detected."""
        for _ in range(4):
            collector.record_loop_detected('inst', 'outer reason', loop_type='outer')
        for _ in range(5):
            collector.record_loop_detected('inst', 'inner reason', loop_type='inner')
        s = collector.get_session_summary()
        assert s['loops_outer'] == 4
        assert s['loops_inner'] == 5
        # THE most important assertion — must always hold.
        assert s['loops_outer'] + s['loops_inner'] == s['total_loops_detected']
        assert s['total_loops_detected'] == 9

    def test_default_loop_type_is_outer(self, collector):
        """Omitting loop_type defaults to outer behavior."""
        for _ in range(2):
            collector.record_loop_detected('inst', 'default reason')
        s = collector.get_session_summary()
        assert s['loops_outer'] == 2
        assert s['loops_inner'] == 0
        assert s['total_loops_detected'] == 2

    def test_unknown_loop_type_counts_as_outer(self, collector):
        """Any non-'inner' value is bucketed as outer (matches source else-branch)."""
        collector.record_loop_detected('inst', 'weird', loop_type='bogus')
        s = collector.get_session_summary()
        assert s['loops_outer'] == 1
        assert s['loops_inner'] == 0

    def test_auto_rolled_back_increments_retries(self, collector):
        """A rolled-back loop inside an active turn bumps per-turn retries, which
        roll up into the session total_retries on turn_end."""
        collector.record_turn_start('inst')
        collector.record_loop_detected('inst', 'stuck', auto_rolled_back=True, pop_count=2, loop_type='outer')
        collector.record_turn_end('inst')
        s = collector.get_session_summary()
        assert s['total_retries'] == 1

    def test_no_rollback_does_not_increment_retries(self, collector):
        collector.record_turn_start('inst')
        collector.record_loop_detected('inst', 'stuck', auto_rolled_back=False)
        collector.record_turn_end('inst')
        assert collector.get_session_summary()['total_retries'] == 0


# ---------------------------------------------------------------------------
# C. Auto-continue ("Malformed")
# ---------------------------------------------------------------------------


class TestAutoContinue:

    def test_counts_accumulate(self, collector):
        for _ in range(4):
            collector.record_auto_continue('inst', 'malformed output')
        assert collector.get_session_summary()['total_auto_continues'] == 4

    def test_does_not_affect_loops_or_turns(self, collector):
        collector.record_auto_continue('inst', 'malformed')
        s = collector.get_session_summary()
        assert s['total_loops_detected'] == 0
        assert s['loops_outer'] == 0
        assert s['loops_inner'] == 0
        assert s['total_turns'] == 0

    def test_event_written(self, collector):
        collector.record_auto_continue('inst', 'malformed output')
        events = collector.get_recent_events(count=10)
        auto_events = [e for e in events if e.get('type') == 'auto_continue']
        assert len(auto_events) == 1
        assert auto_events[0]['instance'] == 'inst'
        assert auto_events[0]['reason'] == 'malformed output'


# ---------------------------------------------------------------------------
# D. Compression
# ---------------------------------------------------------------------------


class TestCompression:

    def test_increments_total_compressions(self, collector):
        for _ in range(3):
            collector.record_compression('inst', 0.5)
        assert collector.get_session_summary()['total_compressions'] == 3

    def test_tokens_saved_in_event(self, collector):
        collector.record_compression('inst', 0.4, tokens_before=1000, tokens_after=600)
        events = [e for e in collector.get_recent_events(count=10) if e['type'] == 'compression']
        assert len(events) == 1
        assert events[0]['tokens_saved'] == 400
        assert events[0]['fraction'] == 0.4

    def test_compression_tracked_per_turn(self, collector):
        """A compression during an active turn is reflected in the config stats."""
        fp = 'fp_compress'
        collector.record_turn_start('inst', config_fingerprint=fp)
        collector.record_compression('inst', 0.5, tokens_before=100, tokens_after=50)
        collector.record_turn_end('inst')
        cfgs = {c['config_fingerprint']: c for c in collector.get_config_comparison()}
        assert fp in cfgs


# ---------------------------------------------------------------------------
# E. Turn lifecycle
# ---------------------------------------------------------------------------


class TestTurnLifecycle:

    def test_start_then_end_increments_total_turns(self, collector):
        collector.record_turn_start('inst')
        collector.record_turn_end('inst')
        assert collector.get_session_summary()['total_turns'] == 1

    def test_multiple_turns_accumulate(self, collector):
        for _ in range(3):
            collector.record_turn_start('inst')
            collector.record_turn_end('inst')
        assert collector.get_session_summary()['total_turns'] == 3

    def test_end_without_start_is_safe_noop(self, collector):
        # Must not raise and must not increment.
        collector.record_turn_end('never_started')
        assert collector.get_session_summary()['total_turns'] == 0

    def test_turn_end_with_loop_and_auto_continue_during_turn(self, collector):
        """Regression guard: a turn that recorded a loop and an auto-continue
        must end cleanly (no KeyError from the removed per-turn auto_continues)."""
        collector.record_turn_start('inst')
        collector.record_loop_detected('inst', 'stuck', loop_type='inner')
        collector.record_auto_continue('inst', 'malformed')
        collector.record_turn_end('inst')  # must not raise
        s = collector.get_session_summary()
        assert s['total_turns'] == 1
        assert s['loops_inner'] == 1
        assert s['total_auto_continues'] == 1


# ---------------------------------------------------------------------------
# F. LLM call lifecycle
# ---------------------------------------------------------------------------


class TestLLMCallLifecycle:

    def test_full_lifecycle(self, collector):
        collector.record_llm_call_start('inst', input_tokens_est=100, model='qwen3-4b')
        collector.record_llm_first_token('inst')
        collector.record_llm_call_end('inst', output_tokens_est=50)
        s = collector.get_session_summary()
        assert s['total_llm_calls'] == 1
        assert s['llm_calls_by_model']['qwen3-4b'] == 1
        assert s['total_input_tokens_est'] == 100
        assert s['total_output_tokens_est'] == 50
        assert s['total_tokens'] == 150

    def test_end_without_start_is_safe_noop(self, collector):
        # Must not raise and must not count a call.
        collector.record_llm_call_end('never_started')
        assert collector.get_session_summary()['total_llm_calls'] == 0

    def test_token_usage_overrides_input_estimate(self, collector):
        """record_token_usage updates the active call's input estimate with ground truth."""
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_token_usage('inst', prompt_tokens=42, completion_tokens=7)
        collector.record_llm_call_end('inst')
        s = collector.get_session_summary()
        assert s['total_input_tokens_est'] == 42
        # Ground-truth completion tokens take priority over the char-count fallback.
        assert s['total_output_tokens_est'] == 7

    def test_token_usage_without_active_call_is_noop(self, collector):
        # Must not raise.
        collector.record_token_usage('never_started', prompt_tokens=5, completion_tokens=5)


class TestCacheClassificationFromUsage:
    """Authoritative prompt-cache hit/miss from server-reported ``cached_tokens``.

    The classification lives in the pure static helper ``classify_cache_hit`` and is
    wired into ``record_token_usage`` (the usage-callback path). It fully replaced the
    retired TTFB-based ``Cache-Status`` header heuristic, so a call's cache outcome is
    now measured solely from the usage object.
    """

    # ── Pure helper: boundary + unknown mapping ──────────────────────────────
    def test_hit_when_cached_exceeds_floor(self):
        # floor = max(1, int(0.05 * 1000)) = 50; 80 >= 50 → hit
        assert TelemetryCollector.classify_cache_hit(80, 1000) == 'hit'

    def test_miss_when_cached_below_floor(self):
        # floor = 50; 49 < 50 → miss (trivial overlap is not a hit)
        assert TelemetryCollector.classify_cache_hit(49, 1000) == 'miss'

    def test_hit_at_exact_floor_boundary(self):
        # cached == floor counts as a hit (>= comparison)
        assert TelemetryCollector.classify_cache_hit(50, 1000) == 'hit'

    def test_min_floor_is_one_token(self):
        # Small prompt: int(0.05 * 2) = 0 → floor clamps to 1; cached=1 → hit
        assert TelemetryCollector.classify_cache_hit(1, 2) == 'hit'
        # cached=0 on a real prompt is always a miss
        assert TelemetryCollector.classify_cache_hit(0, 2) == 'miss'

    def test_absent_cached_field_with_usage_is_miss(self):
        # Usage present with a real prompt but no cached_tokens field → treated as 0
        # (nothing served from cache) → miss. Only the absence of a usable prompt count
        # is "unknown".
        assert TelemetryCollector.classify_cache_hit(None, 1000) == 'miss'

    def test_unknown_when_no_prompt_reported(self):
        # No prompt tokens → cannot classify meaningfully
        assert TelemetryCollector.classify_cache_hit(50, 0) is None
        assert TelemetryCollector.classify_cache_hit(50, None) is None

    def test_bad_input_degrades_to_unknown_not_error(self):
        # Must never raise on malformed values.
        assert TelemetryCollector.classify_cache_hit('garbage', 'also-bad') is None
        assert TelemetryCollector.classify_cache_hit(None, 'bad') is None

    # ── Wired through record_token_usage into session counters ────────────────
    def test_usage_hit_increments_hits_counter(self, collector):
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_token_usage(
            'inst',
            prompt_tokens=1000,
            completion_tokens=5,
            details={'cached_tokens': 80},
        )
        collector.record_llm_call_end('inst')
        s = collector.get_session_summary()
        assert s['llm_cache_hits'] == 1
        assert s['llm_cache_misses'] == 0
        assert s['llm_cache_unknown'] == 0

    def test_usage_miss_increments_misses_counter(self, collector):
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_token_usage(
            'inst',
            prompt_tokens=1000,
            completion_tokens=5,
            details={'cached_tokens': 0},
        )
        collector.record_llm_call_end('inst')
        s = collector.get_session_summary()
        assert s['llm_cache_misses'] == 1
        assert s['llm_cache_hits'] == 0

    def test_usage_present_but_no_cached_field_is_miss(self, collector):
        # Usage present (prompt_tokens > 0) but no cached_tokens → treated as 0 → miss.
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_token_usage(
            'inst',
            prompt_tokens=1000,
            completion_tokens=5,
            details=None,
        )
        collector.record_llm_call_end('inst')
        s = collector.get_session_summary()
        assert s['llm_cache_hits'] == 0
        assert s['llm_cache_misses'] == 1
        assert s['llm_cache_unknown'] == 0

    def test_no_usage_at_all_is_not_classified(self, collector):
        # Non-supporting backend: no usage callback fires at all → nothing classified.
        # (record_token_usage is simply never called; the call ends with no cache info.)
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_llm_call_end('inst')
        s = collector.get_session_summary()
        assert s['llm_cache_hits'] == 0
        assert s['llm_cache_misses'] == 0
        assert s['llm_cache_unknown'] == 0

    def test_usage_with_zero_prompt_tokens_is_not_classified(self, collector):
        """Regression: classify_cache_hit returns None when prompt_tokens <= 0.

        A usage object with no usable prompt count must NOT increment any counter —
        in particular it must not fall into a spurious "miss" branch. (This guards the
        exact bug where an unguarded else would inflate llm_cache_misses.)
        """
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        # prompt_tokens=0 → classify returns None → no counter change.
        collector.record_token_usage(
            'inst',
            prompt_tokens=0,
            completion_tokens=5,
            details={'cached_tokens': 80},
        )
        assert 'cache_classified' not in collector._active_llm_calls['inst']
        collector.record_llm_call_end('inst')
        s = collector.get_session_summary()
        assert s['llm_cache_hits'] == 0
        assert s['llm_cache_misses'] == 0
        assert s['llm_cache_unknown'] == 0

    # ── Idempotency: a call is classified at most once ────────────────────────
    def test_repeated_usage_does_not_double_count(self, collector):
        """Once a call is classified from cached_tokens, further usage callbacks are no-ops.

        The ``cache_classified`` guard on the active call guarantees at most one counter
        bump per call even if the usage callback fires more than once (e.g. a backend
        that emits multiple usage chunks). This replaced the old header-vs-usage
        double-counting guard.
        """
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        # First usage classifies a hit and marks the call classified.
        collector.record_token_usage(
            'inst',
            prompt_tokens=1000,
            completion_tokens=5,
            details={'cached_tokens': 80},
        )
        assert collector._active_llm_calls['inst'].get('cache_classified') is True
        # A second usage for the same call must not increment a counter again.
        collector.record_token_usage(
            'inst',
            prompt_tokens=1000,
            completion_tokens=5,
            details={'cached_tokens': 80},
        )
        collector.record_llm_call_end('inst')
        s = collector.get_session_summary()
        assert s['llm_cache_hits'] == 1  # exactly one, not two
        assert s['llm_cache_misses'] == 0

    def test_coverage_ratio_reflects_classified_share(self, collector):
        """llm_cache_classified_ratio = (hits+misses)/total_llm_calls."""
        # 4 calls: 1 hit (usage), 2 misses (usage / no cached field), 1 unmeasured
        # (no usage callback at all — a non-supporting backend).
        collector.record_llm_call_start('inst0', input_tokens_est=10, model='m')
        collector.record_token_usage('inst0', 1000, 5, {'cached_tokens': 80})  # hit
        collector.record_llm_call_end('inst0')

        collector.record_llm_call_start('inst1', input_tokens_est=10, model='m')
        collector.record_token_usage('inst1', 1000, 5, {'cached_tokens': 0})  # miss
        collector.record_llm_call_end('inst1')

        collector.record_llm_call_start('inst2', input_tokens_est=10, model='m')
        collector.record_token_usage('inst2', 1000, 5, None)  # miss (no cached field)
        collector.record_llm_call_end('inst2')

        collector.record_llm_call_start('inst3', input_tokens_est=10, model='m')
        collector.record_llm_call_end('inst3')  # unmeasured (no usage)

        s = collector.get_session_summary()
        assert s['total_llm_calls'] == 4
        assert s['llm_cache_hits'] == 1
        assert s['llm_cache_misses'] == 2
        assert s['llm_cache_classified_ratio'] == round(3 / 4, 3)

    def test_coverage_ratio_none_when_no_llm_calls(self, collector):
        assert collector.get_session_summary()['llm_cache_classified_ratio'] is None


# ---------------------------------------------------------------------------
# G. Tool call lifecycle
# ---------------------------------------------------------------------------


class TestToolCallLifecycle:

    def test_successful_tool_call(self, collector):
        collector.record_tool_call_start('a', 'read_file')
        collector.record_tool_call_end('a', 'read_file', success=True)
        s = collector.get_session_summary()
        assert s['total_tool_calls'] == 1
        # tool_effectiveness is keyed by tool name.
        eff = s['tool_effectiveness']['read_file']
        assert eff['total'] == 1
        assert eff['failures'] == 0
        assert eff['success_rate'] == 100.0

    def test_failed_tool_call_reflected_in_success_rate(self, collector):
        # One success + one failure for the same tool -> 50% success rate.
        collector.record_tool_call_start('a', 'write_file')
        collector.record_tool_call_end('a', 'write_file', success=True)
        collector.record_tool_call_start('a', 'write_file')
        collector.record_tool_call_end('a', 'write_file', success=False, error='boom')
        eff = collector.get_session_summary()['tool_effectiveness']['write_file']
        assert eff['total'] == 2
        assert eff['failures'] == 1
        assert eff['success_rate'] < 100.0

    def test_call_agent_routing(self, collector):
        """is_call_agent=True routes latency separately and is counted via call_agent_count."""
        collector.record_tool_call_start('a', 'call_agent')
        collector.record_tool_call_end('a', 'call_agent', success=True, is_call_agent=True)
        s = collector.get_session_summary()
        # Count is driven by the is_call_agent flag (single source of truth),
        # so it always agrees with call_agent_latency_ms.
        assert s['call_agent_count'] == 1
        # Regular (non-agent) tool latency denominator stays clean.
        assert s['total_tool_calls'] == 1

    def test_call_agent_count_ignores_name_when_flag_off(self, collector):
        """Regression: a tool merely NAMED 'call_agent' with the flag off must NOT
        be counted as an agent delegation — count and latency share one source."""
        collector.record_tool_call_start('a', 'call_agent')
        collector.record_tool_call_end('a', 'call_agent', success=True, is_call_agent=False)
        s = collector.get_session_summary()
        assert s['call_agent_count'] == 0
        # It still counts as a regular tool call and its latency goes to the
        # regular pool (not the call_agent pool).
        assert s['total_tool_calls'] == 1
        assert s['call_agent_latency_ms'] == 0

    def test_end_without_start_is_safe_noop(self, collector):
        # Must not raise and must not count a call.
        collector.record_tool_call_end('a', 'ghost_tool')
        assert collector.get_session_summary()['total_tool_calls'] == 0


# ---------------------------------------------------------------------------
# H. Agent instance call
# ---------------------------------------------------------------------------


class TestAgentInstanceCall:

    def test_increments_agent_instance_calls(self, collector):
        collector.record_agent_instance_call('inst', 'coder', 'orchestrator', latency_ms=123.0)
        assert collector.get_session_summary()['agent_instance_calls'] == 1

    def test_multiple_calls_accumulate(self, collector):
        for _ in range(3):
            collector.record_agent_instance_call('inst', 'researcher', 'Maine')
        assert collector.get_session_summary()['agent_instance_calls'] == 3


# ---------------------------------------------------------------------------
# I. Config comparison
# ---------------------------------------------------------------------------


class TestConfigComparison:

    def test_fingerprint_appears_with_turns(self, collector):
        fp = 'fp_abc123'
        collector.record_turn_start('inst', config_fingerprint=fp)
        collector.record_turn_end('inst')
        cfgs = {c['config_fingerprint']: c for c in collector.get_config_comparison()}
        assert fp in cfgs
        assert cfgs[fp]['turns'] >= 1

    def test_empty_when_no_configured_turn(self, collector):
        # A turn with no fingerprint does not create a per-config entry.
        collector.record_turn_start('inst')
        collector.record_turn_end('inst')
        assert collector.get_config_comparison() == []

    def test_row_exposes_total_streaming_time_sec(self, collector):
        """Guard the frontend "Total Time" column: each A/B row must expose
        ``total_streaming_time_sec`` (per-endpoint total streaming time)."""
        fp = 'fp_stream'
        collector.record_turn_start('inst', config_fingerprint=fp)
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        # Record first token so streaming_time_ms is measured (non-zero), then end.
        collector.record_llm_first_token('inst')
        collector.record_llm_call_end('inst', output_tokens_est=5)
        collector.record_turn_end('inst')

        cfgs = {c['config_fingerprint']: c for c in collector.get_config_comparison()}
        assert fp in cfgs
        row = cfgs[fp]
        assert 'total_streaming_time_sec' in row
        assert isinstance(row['total_streaming_time_sec'], (int, float))
        assert row['total_streaming_time_sec'] >= 0


# ---------------------------------------------------------------------------
# I-bis. Config fingerprint is model-only (no prompt print)
# ---------------------------------------------------------------------------


class TestFingerprintModelOnly:

    def test_same_model_different_configs_same_fingerprint(self):
        """Same model but different prompts/params/tools/api_base -> SAME fingerprint."""
        base = TelemetryCollector.fingerprint_config(model='qwen3-4b')
        varied = TelemetryCollector.fingerprint_config(
            model='qwen3-4b',
            generate_cfg={
                'temperature': 0.9,
                'max_tokens': 8192
            },
            system_prompt='You are a totally different agent.',
            tools=['read_file', 'write_file'],
            api_base='http://localhost:1234/v1',
        )
        assert base == varied

    def test_different_models_different_fingerprint(self):
        fp_a = TelemetryCollector.fingerprint_config(model='qwen3-4b')
        fp_b = TelemetryCollector.fingerprint_config(model='llama-3-8b')
        assert fp_a != fp_b

    def test_fingerprint_is_stable_string(self):
        """Fingerprint must stay a stable short string for _config_stats/JSONL compat."""
        fp = TelemetryCollector.fingerprint_config(model='qwen3-4b')
        assert isinstance(fp, str)
        assert len(fp) == 12
        # Deterministic across calls.
        assert fp == TelemetryCollector.fingerprint_config(model='qwen3-4b')

    def test_fingerprint_ignores_system_prompt_arg(self):
        """system_prompt no longer influences the fingerprint (prompt print removed)."""
        with_prompt = TelemetryCollector.fingerprint_config(model='m', system_prompt='You are Security_op_091f048b.')
        without_prompt = TelemetryCollector.fingerprint_config(model='m')
        assert with_prompt == without_prompt


# ---------------------------------------------------------------------------
# I-ter. Agent class usage summary
# ---------------------------------------------------------------------------


class TestAgentClassSummary:

    def test_empty_when_no_agent_class_turns(self, collector):
        # A turn without agent_class does not create an entry.
        collector.record_turn_start('inst')
        collector.record_turn_end('inst')
        assert collector.get_agent_class_summary() == []

    def test_accuracy_time_tokens_for_scripted_sequence(self, collector):
        """Two turns for one class: 3 tool calls (2 success, 1 failure) -> ~66.7% accuracy."""
        # Turn 1: two successful tool calls.
        collector.record_turn_start('inst', agent_class='coder')
        collector.record_tool_call_start('inst', 'read_file')
        collector.record_tool_call_end('inst', 'read_file', success=True)
        collector.record_tool_call_start('inst', 'write_file')
        collector.record_tool_call_end('inst', 'write_file', success=True)
        collector.record_llm_call_start('inst', input_tokens_est=100, model='qwen3-4b')
        collector.record_llm_call_end('inst', output_tokens_est=50)
        collector.record_turn_end('inst')

        # Turn 2: one failing tool call.
        collector.record_turn_start('inst', agent_class='coder')
        collector.record_tool_call_start('inst', 'write_file')
        collector.record_tool_call_end('inst', 'write_file', success=False, error='boom')
        collector.record_llm_call_start('inst', input_tokens_est=200, model='qwen3-4b')
        collector.record_llm_call_end('inst', output_tokens_est=80)
        collector.record_turn_end('inst')

        rows = {r['agent_class']: r for r in collector.get_agent_class_summary()}
        assert 'coder' in rows
        row = rows['coder']
        assert row['turns'] == 2
        # tokens_generated = completion tokens only = 50 + 80 = 130
        # (input/prompt tokens 100+200 are NOT generated by the model)
        assert row['tokens_generated'] == 130
        # accuracy = (3 - 1) / 3 * 100 = 66.7
        assert row['tool_usage_accuracy'] == 66.7
        # total_time_sec is a non-negative float (turns actually took some wall time).
        assert isinstance(row['total_time_sec'], float)
        assert row['total_time_sec'] >= 0

    def test_no_tool_calls_gives_null_accuracy(self, collector):
        collector.record_turn_start('inst', agent_class='researcher')
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_llm_call_end('inst', output_tokens_est=5)
        collector.record_turn_end('inst')

        rows = {r['agent_class']: r for r in collector.get_agent_class_summary()}
        assert rows['researcher']['tool_usage_accuracy'] is None
        # tokens_generated = completion tokens only (output=5), not input+output (10+5)
        assert rows['researcher']['tokens_generated'] == 5

    def test_multiple_classes_are_separate_rows(self, collector):
        """One model can serve multiple classes -> separate accumulator rows."""
        for cls in ('coder', 'reviewer'):
            collector.record_turn_start('inst', agent_class=cls)
            collector.record_llm_call_start('inst', input_tokens_est=10, model='same-model')
            collector.record_llm_call_end('inst', output_tokens_est=5)
            collector.record_turn_end('inst')

        rows = {r['agent_class']: r for r in collector.get_agent_class_summary()}
        assert set(rows) == {'coder', 'reviewer'}
        assert rows['coder']['turns'] == 1
        assert rows['reviewer']['turns'] == 1

    def test_all_tool_calls_fail_gives_zero_accuracy(self, collector):
        """When every tool call fails, accuracy is exactly 0 (not None)."""
        collector.record_turn_start('inst', agent_class='coder')
        collector.record_tool_call_start('inst', 'read_file')
        collector.record_tool_call_end('inst', 'read_file', success=False, error='e1')
        collector.record_tool_call_start('inst', 'write_file')
        collector.record_tool_call_end('inst', 'write_file', success=False, error='e2')
        collector.record_turn_end('inst')

        rows = {r['agent_class']: r for r in collector.get_agent_class_summary()}
        assert rows['coder']['tool_usage_accuracy'] == 0.0

    def test_summary_rows_sorted_by_agent_class(self, collector):
        """get_agent_class_summary returns rows sorted by agent_class name."""
        for cls in ('reviewer', 'coder', 'orchestrator'):
            collector.record_turn_start('inst', agent_class=cls)
            collector.record_llm_call_start('inst', input_tokens_est=1, model='m')
            collector.record_llm_call_end('inst', output_tokens_est=1)
            collector.record_turn_end('inst')

        names = [r['agent_class'] for r in collector.get_agent_class_summary()]
        assert names == sorted(names) == ['coder', 'orchestrator', 'reviewer']

    def test_llm_calls_attributed_to_agent_class(self, collector):
        """Each LLM call inside a turn is attributed to that instance's agent class."""
        collector.record_turn_start('inst', agent_class='coder')
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_llm_call_end('inst', output_tokens_est=5)

        rows = {r['agent_class']: r for r in collector.get_agent_class_summary()}
        assert rows['coder']['llm_calls'] == 1

        # A second call within the same (still-active) turn -> 2.
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_llm_call_end('inst', output_tokens_est=5)
        rows = {r['agent_class']: r for r in collector.get_agent_class_summary()}
        assert rows['coder']['llm_calls'] == 2

    def test_no_active_turn_not_attributed(self, collector):
        """An LLM call with NO active turn is not attributed anywhere (no crash, no row)."""
        collector.record_llm_call_start('ghost', input_tokens_est=10, model='m')
        collector.record_llm_call_end('ghost', output_tokens_est=5)
        assert collector.get_agent_class_summary() == []

    def test_empty_agent_class_not_attributed(self, collector):
        """An LLM call inside a turn with an empty agent_class is not attributed."""
        collector.record_turn_start('inst')  # agent_class defaults to ""
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_llm_call_end('inst', output_tokens_est=5)
        collector.record_turn_end('inst')
        assert collector.get_agent_class_summary() == []

    def test_sub_agent_calls_isolated_per_class(self, collector):
        """Two instances with different agent classes each count only their own calls."""
        # "coder" makes 2 calls; "researcher" makes 1 call.
        for _ in range(2):
            collector.record_turn_start('worker_a', agent_class='coder')
            collector.record_llm_call_start('worker_a', input_tokens_est=10, model='m')
            collector.record_llm_call_end('worker_a', output_tokens_est=5)
            collector.record_turn_end('worker_a')

        collector.record_turn_start('worker_b', agent_class='researcher')
        collector.record_llm_call_start('worker_b', input_tokens_est=10, model='m')
        collector.record_llm_call_end('worker_b', output_tokens_est=5)
        collector.record_turn_end('worker_b')

        rows = {r['agent_class']: r for r in collector.get_agent_class_summary()}
        assert rows['coder']['llm_calls'] == 2
        assert rows['researcher']['llm_calls'] == 1

    def test_llm_calls_key_present_with_default_zero(self, collector):
        """A class that had turns but zero LLM calls still reports llm_calls == 0."""
        collector.record_turn_start('inst', agent_class='coder')
        # No LLM call — only a tool call.
        collector.record_tool_call_start('inst', 'read_file')
        collector.record_tool_call_end('inst', 'read_file', success=True)
        collector.record_turn_end('inst')

        rows = {r['agent_class']: r for r in collector.get_agent_class_summary()}
        assert 'coder' in rows
        assert rows['coder']['llm_calls'] == 0


# ---------------------------------------------------------------------------
# I-quad. Live per-config / per-class aggregation (Fix B)
# ---------------------------------------------------------------------------
# Per-config and per-class counters are now accumulated LIVE inside the recorders
# (turn_start / llm_call_end / tool_call_end / loop_detected / compression) instead of
# being flushed once in record_turn_end. These tests prove the values are visible BEFORE
# turn_end AND that turn_end does NOT re-add them (a MOVE, not a copy — no double count).


class TestLivePerConfigClassAggregation:
    FP = 'fp_live'

    def _cfg(self, collector):
        return {c['config_fingerprint']: c for c in collector.get_config_comparison()}[self.FP]

    def _cls(self, collector):
        return {r['agent_class']: r for r in collector.get_agent_class_summary()}['coder']

    def test_turns_live_before_turn_end(self, collector):
        """turns is incremented at turn_start — visible before any turn_end."""
        collector.record_turn_start('inst', config_fingerprint=self.FP, agent_class='coder')
        # BEFORE turn_end:
        assert self._cfg(collector)['turns'] == 1
        assert self._cls(collector)['turns'] == 1

    def test_llm_calls_and_tokens_live_before_turn_end(self, collector):
        """llm_calls + input/output tokens (config) and tokens_generated (class) update live."""
        collector.record_turn_start('inst', config_fingerprint=self.FP, agent_class='coder')
        collector.record_llm_call_start('inst', input_tokens_est=120, model='m')
        collector.record_llm_first_token('inst')
        collector.record_llm_call_end('inst', output_tokens_est=45)

        # BEFORE turn_end:
        cfg = self._cfg(collector)
        assert cfg['llm_calls'] == 1
        assert cfg['input_tokens_est'] == 120
        assert cfg['output_tokens_est'] == 45
        assert self._cls(collector)['tokens_generated'] == 45

    def test_tool_calls_and_failures_live_before_turn_end(self, collector):
        """A failing tool updates per-config per-name counts and class accuracy live."""
        collector.record_turn_start('inst', config_fingerprint=self.FP, agent_class='coder')
        # one success + one failure for "write_file"
        collector.record_tool_call_start('inst', 'write_file')
        collector.record_tool_call_end('inst', 'write_file', success=True)
        collector.record_tool_call_start('inst', 'write_file')
        collector.record_tool_call_end('inst', 'write_file', success=False, error='boom')

        # BEFORE turn_end:
        cfg = self._cfg(collector)
        assert cfg['tool_calls'] == 2
        eff = cfg['tool_effectiveness']['write_file']
        assert eff['total'] == 2
        assert eff['success_rate'] == 50.0
        # class accuracy reflects the failure live: (2 - 1)/2 = 50.0
        assert self._cls(collector)['tool_usage_accuracy'] == 50.0

    def test_loops_and_retries_live_before_turn_end(self, collector):
        """record_loop_detected(auto_rolled_back=True) updates config loops + retries live."""
        collector.record_turn_start('inst', config_fingerprint=self.FP, agent_class='coder')
        collector.record_loop_detected('inst', 'stuck', auto_rolled_back=True)

        # BEFORE turn_end:
        cfg = self._cfg(collector)
        assert cfg['loops_detected'] == 1
        assert cfg['retries'] == 1

    def test_no_double_count_full_turn(self, collector):
        """CRITICAL regression: a full turn then turn_end must NOT re-add live counters.

        Captures the per-config/per-class values BEFORE turn_end, ends the turn, and asserts
        the SAME values after — proving record_turn_end is a no-op for the moved counters.
        """
        N_LLM = 3
        M_TOOLS = 2  # one success + one failure

        collector.record_turn_start('inst', config_fingerprint=self.FP, agent_class='coder')
        for _ in range(N_LLM):
            collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
            collector.record_llm_first_token('inst')
            collector.record_llm_call_end('inst', output_tokens_est=20)
        collector.record_tool_call_start('inst', 'read_file')
        collector.record_tool_call_end('inst', 'read_file', success=True)
        collector.record_tool_call_start('inst', 'write_file')
        collector.record_tool_call_end('inst', 'write_file', success=False, error='e')
        collector.record_loop_detected('inst', 'stuck', auto_rolled_back=True)
        collector.record_compression('inst', 0.5, tokens_before=100, tokens_after=50)

        # ---- capture live values BEFORE turn_end ----
        cfg_before = self._cfg(collector)
        cls_before = self._cls(collector)
        before = {
            'turns': cfg_before['turns'],
            'llm_calls': cfg_before['llm_calls'],
            'tool_calls': cfg_before['tool_calls'],
            'input_tokens_est': cfg_before['input_tokens_est'],
            'output_tokens_est': cfg_before['output_tokens_est'],
            'loops_detected': cfg_before['loops_detected'],
            'retries': cfg_before['retries'],
            'total_compressions': collector._config_stats[self.FP]['total_compressions'],
            'cls_turns': cls_before['turns'],
            'cls_tokens_generated': cls_before['tokens_generated'],
            # get_agent_class_summary() exposes tool_usage_accuracy (derived), not the raw
            # tool_calls count, so read it straight from the internal accumulator.
            'cls_tool_calls': collector._agent_class_stats['coder']['tool_calls'],
            # Per-name tool breakdowns + class failures (explicit guards against
            # double-counting that a total alone wouldn't catch if offsets cancelled).
            'cfg_tool_by_name': dict(collector._config_stats[self.FP]['tool_calls_by_name']),
            'cfg_tool_fail_by_name': dict(collector._config_stats[self.FP]['tool_failures_by_name']),
            'cls_tool_failures': collector._agent_class_stats['coder']['tool_failures'],
        }

        # sanity: live values are already correct mid-turn
        assert before['turns'] == 1
        assert before['llm_calls'] == N_LLM
        assert before['tool_calls'] == M_TOOLS
        assert before['input_tokens_est'] == N_LLM * 10
        assert before['output_tokens_est'] == N_LLM * 20
        assert before['loops_detected'] == 1
        assert before['retries'] == 1
        assert before['total_compressions'] == 1
        assert before['cls_turns'] == 1
        assert before['cls_tokens_generated'] == N_LLM * 20
        assert before['cls_tool_calls'] == M_TOOLS

        # ---- end the turn ----
        collector.record_turn_end('inst')

        # ---- after turn_end, moved counters must be UNCHANGED (move, not copy) ----
        cfg_after = self._cfg(collector)
        cls_after = self._cls(collector)
        assert cfg_after['turns'] == before['turns']
        assert cfg_after['llm_calls'] == before['llm_calls']
        assert cfg_after['tool_calls'] == before['tool_calls']
        assert cfg_after['input_tokens_est'] == before['input_tokens_est']
        assert cfg_after['output_tokens_est'] == before['output_tokens_est']
        assert cfg_after['loops_detected'] == before['loops_detected']
        assert cfg_after['retries'] == before['retries']
        assert collector._config_stats[self.FP]['total_compressions'] == before['total_compressions']
        assert cls_after['turns'] == before['cls_turns']
        assert cls_after['tokens_generated'] == before['cls_tokens_generated']
        assert collector._agent_class_stats['coder']['tool_calls'] == before['cls_tool_calls']
        # Per-name breakdowns + class failures must also be unchanged (no re-flush).
        assert dict(collector._config_stats[self.FP]['tool_calls_by_name']) == before['cfg_tool_by_name']
        assert dict(collector._config_stats[self.FP]['tool_failures_by_name']) == before['cfg_tool_fail_by_name']
        assert collector._agent_class_stats['coder']['tool_failures'] == before['cls_tool_failures']

    def test_duration_zero_before_positive_after_turn_end(self, collector):
        """total_duration_sec is 0 while the turn is in progress and > 0 after it ends."""
        collector.record_turn_start('inst', config_fingerprint=self.FP)
        collector.record_llm_call_start('inst', input_tokens_est=10, model='m')
        collector.record_llm_first_token('inst')
        collector.record_llm_call_end('inst', output_tokens_est=5)

        # BEFORE turn_end: no completed time yet.
        assert self._cfg(collector)['total_duration_sec'] == 0

        collector.record_turn_end('inst')
        # AFTER turn_end: duration reflects the (tiny but real) elapsed wall time.
        assert self._cfg(collector)['total_duration_sec'] >= 0
        # A completed turn must register *some* duration > 0 in practice; guard only that
        # it's a non-negative number and the field is populated (perf_counter guarantees >0).
        assert collector._config_stats[self.FP]['total_duration_ms'] > 0

    def test_compression_not_double_counted(self, collector):
        """record_compression bumps per-config total_compressions live; turn_end must NOT add again.

        Regression for the latent double-count (record_turn_end used to add turn["compressions"]).
        """
        collector.record_turn_start('inst', config_fingerprint=self.FP)
        collector.record_compression('inst', 0.5, tokens_before=100, tokens_after=50)

        # BEFORE turn_end: exactly 1.
        assert collector._config_stats[self.FP]['total_compressions'] == 1

        collector.record_turn_end('inst')
        # AFTER turn_end: still exactly 1 (NOT 2).
        assert collector._config_stats[self.FP]['total_compressions'] == 1


# ---------------------------------------------------------------------------
# I2. Skill usage summary (per-skill accumulator)
# ---------------------------------------------------------------------------


class TestSkillUsageSummary:

    def test_empty_when_nothing_recorded(self, collector):
        assert collector.get_skill_usage_summary() == []

    def test_single_record(self, collector):
        collector.record_skills_loaded('coder', ['docker-best-practices'], 'explicit')
        rows = collector.get_skill_usage_summary()
        assert len(rows) == 1
        r = rows[0]
        assert r['skill'] == 'docker-best-practices'
        assert r['loads'] == 1
        assert r['agent_classes'] == ['coder']
        assert r['top_mode'] == 'explicit'

    def test_same_skill_two_agent_classes(self, collector):
        """Same skill loaded by two classes -> loads=2, both classes listed."""
        collector.record_skills_loaded('coder', ['httpx-connection-pooling'], 'auto')
        collector.record_skills_loaded('researcher', ['httpx-connection-pooling'], 'auto')
        rows = {r['skill']: r for r in collector.get_skill_usage_summary()}
        r = rows['httpx-connection-pooling']
        assert r['loads'] == 2
        assert r['agent_classes'] == ['coder', 'researcher']

    def test_multiple_modes_top_mode_majority(self, collector):
        """top_mode picks the majority; ties broken alphabetically."""
        collector.record_skills_loaded('coder', ['skill-x'], 'auto')
        collector.record_skills_loaded('coder', ['skill-x'], 'auto')
        collector.record_skills_loaded('coder', ['skill-x'], 'explicit')
        rows = {r['skill']: r for r in collector.get_skill_usage_summary()}
        assert rows['skill-x']['top_mode'] == 'auto'

    def test_top_mode_tie_broken_alphabetically(self, collector):
        collector.record_skills_loaded('coder', ['skill-y'], 'runtime')
        collector.record_skills_loaded('coder', ['skill-y'], 'advisor')
        rows = {r['skill']: r for r in collector.get_skill_usage_summary()}
        # advisor (1) vs runtime (1) -> alphabetical winner is "advisor".
        assert rows['skill-y']['top_mode'] == 'advisor'

    def test_dedupe_within_single_call(self, collector):
        """Duplicate names within one call count once."""
        collector.record_skills_loaded('coder', ['dup', 'dup'], 'explicit')
        rows = {r['skill']: r for r in collector.get_skill_usage_summary()}
        assert rows['dup']['loads'] == 1

    def test_sorted_by_loads_desc_then_name(self, collector):
        collector.record_skills_loaded('a', ['bbb'], 'auto')
        collector.record_skills_loaded('a', ['aaa'], 'auto')
        collector.record_skills_loaded('a', ['aaa'], 'auto')
        rows = collector.get_skill_usage_summary()
        assert [r['skill'] for r in rows] == ['aaa', 'bbb']

    def test_noop_on_empty_and_none(self, collector):
        collector.record_skills_loaded('coder', [], 'explicit')
        collector.record_skills_loaded('coder', None, 'explicit')
        assert collector.get_skill_usage_summary() == []


class TestSkillAdvisorCountersInSummary:
    """The advisor session counters must be surfaced by get_session_summary()."""

    def test_advisor_counters_present_and_zero_by_default(self, collector):
        s = collector.get_session_summary()
        assert s['skill_advisor_calls'] == 0
        assert s['skill_advisor_denials'] == 0
        assert s['skill_advisor_fallbacks'] == 0

    def test_advisor_counters_increment_and_surface(self, collector):
        collector.record_skill_advisor_decision('inst', 'approve')
        collector.record_skill_advisor_decision('inst', 'deny')
        collector.record_skill_advisor_decision('inst', 'ambiguous', was_fallback=True)
        s = collector.get_session_summary()
        assert s['skill_advisor_calls'] == 3
        assert s['skill_advisor_denials'] == 1
        assert s['skill_advisor_fallbacks'] == 1


# ---------------------------------------------------------------------------
# J. Event log
# ---------------------------------------------------------------------------


class TestEventLog:

    def test_recent_events_have_type_and_timestamp(self, collector):
        collector.record_turn_start('inst')
        collector.record_loop_detected('inst', 'stuck', loop_type='inner')
        collector.record_auto_continue('inst', 'malformed')
        collector.record_compression('inst', 0.5)
        events = collector.get_recent_events(count=20)
        assert len(events) > 0
        for e in events:
            assert isinstance(e, dict)
            assert 'type' in e
            assert 'timestamp' in e

    def test_get_recent_events_respects_count(self, collector):
        for _ in range(5):
            collector.record_auto_continue('inst', 'malformed')
        # Plus the initial session_start event written at construction.
        events = collector.get_recent_events(count=3)
        assert len(events) == 3

    def test_session_start_event_written_on_init(self, collector):
        events = collector.get_recent_events(count=50)
        types = [e['type'] for e in events]
        assert 'session_start' in types


# ---------------------------------------------------------------------------
# K. User-turn counting (total_user_turns)
# ---------------------------------------------------------------------------


class TestUserTurnCounting:
    """``total_user_turns`` counts fresh agent runs/budgets, independently of the
    run-cycle counter ``total_turns``."""

    def test_fresh_collector_defaults_to_zero_and_key_present(self, collector):
        s = collector.get_session_summary()
        assert 'total_user_turns' in s
        assert s['total_user_turns'] == 0

    def test_record_user_turn_accumulates(self, collector):
        for _ in range(4):
            collector.record_user_turn('inst')
        assert collector.get_session_summary()['total_user_turns'] == 4

    def test_independent_of_total_turns(self, collector):
        # Run cycles and user turns are separate counters.
        collector.record_turn_start('inst')
        collector.record_turn_end('inst')
        collector.record_user_turn('inst')
        s = collector.get_session_summary()
        assert s['total_turns'] == 1
        assert s['total_user_turns'] == 1


# ---------------------------------------------------------------------------
# L. Engine-level first-turn semantics (_consume_turn)
# ---------------------------------------------------------------------------


class _FakeInstance:
    """Minimal stand-in exposing only what ``_consume_turn`` touches."""

    def __init__(self, name='inst'):
        self.instance_name = name
        self._turn_consumed = False


def _make_engine_with_telemetry(collector):
    from agent_cascade.execution_engine import ExecutionEngine

    class _Pool:
        telemetry = collector

    return ExecutionEngine(_Pool())


class TestConsumeTurnFirstTurnSemantics:
    """Drive ``ExecutionEngine._consume_turn`` directly to verify the first-consumption
    of a run records exactly one user turn."""

    def test_k_consumptions_record_one_user_turn(self, collector):
        engine = _make_engine_with_telemetry(collector)
        inst = _FakeInstance()
        turns = 10
        for _ in range(5):  # simulate K LLM iterations within one run
            turns = engine._consume_turn(inst, turns)
        assert turns == 5
        assert collector.get_session_summary()['total_user_turns'] == 1

    def test_consecutive_runs_same_instance(self, collector):
        """Two full runs on the same instance (flag reset between) → exactly 2 user turns."""
        engine = _make_engine_with_telemetry(collector)
        inst = _FakeInstance()
        # Run 1: several iterations.
        inst._turn_consumed = False
        for _ in range(4):
            engine._consume_turn(inst, 10)
        # Run 2: fresh budget on the same instance.
        inst._turn_consumed = False
        for _ in range(2):
            engine._consume_turn(inst, 10)
        assert collector.get_session_summary()['total_user_turns'] == 2

    def test_no_consumption_records_nothing(self, collector):
        """A run that exits before consuming any budget records no user turn.

        Models an early-exit path (e.g. terminal stop) where _consume_turn is never
        called: the flag stays False and total_user_turns stays 0.
        """
        _make_engine_with_telemetry(collector)
        inst = _FakeInstance()
        # No budget consumption occurs (early exit before the turn loop).
        assert getattr(inst, '_turn_consumed', False) is False
        assert collector.get_session_summary()['total_user_turns'] == 0

    def test_pre_llm_first_iteration_counts_one(self, collector):
        """A pre-LLM consumption as the very first consumption records exactly one user turn."""
        engine = _make_engine_with_telemetry(collector)
        inst = _FakeInstance()
        turns_wrapper = [10]
        # Simulate _pre_llm_checks consuming on iteration 1 (e.g. async injection pending).
        turns_wrapper[0] = engine._consume_turn(inst, turns_wrapper[0])
        assert collector.get_session_summary()['total_user_turns'] == 1
        # A subsequent normal-iteration consumption in the same run must NOT add another.
        turns_wrapper[0] = engine._consume_turn(inst, turns_wrapper[0])
        assert collector.get_session_summary()['total_user_turns'] == 1

    def test_flag_reset_per_run(self, collector):
        """After a run consumes, resetting _turn_consumed=False (run start) makes the next
        consumption count a fresh user turn — so a recalled/reused instance counts again."""
        engine = _make_engine_with_telemetry(collector)
        inst = _FakeInstance()
        engine._consume_turn(inst, 10)
        assert inst._turn_consumed is True
        # run() start resets the flag:
        inst._turn_consumed = False
        engine._consume_turn(inst, 10)
        assert collector.get_session_summary()['total_user_turns'] == 2

    def test_no_telemetry_is_safe(self):
        """_consume_turn must not raise when no telemetry collector is attached."""
        from agent_cascade.execution_engine import ExecutionEngine

        class _NoTelPool:
            pass  # no `telemetry` attribute → _telemetry() returns None

        engine = ExecutionEngine(_NoTelPool())
        inst = _FakeInstance()
        assert engine._consume_turn(inst, 5) == 4
