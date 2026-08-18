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
    return TelemetryCollector(log_dir=str(tmp_path), instance_id="unit_test")


# ---------------------------------------------------------------------------
# A. Session summary defaults
# ---------------------------------------------------------------------------

class TestSessionSummaryDefaults:
    def test_fresh_collector_all_zero_defaults(self, collector):
        s = collector.get_session_summary()
        assert s["total_turns"] == 0
        assert s["total_llm_calls"] == 0
        assert s["total_tool_calls"] == 0
        assert s["total_loops_detected"] == 0
        assert s["loops_outer"] == 0
        assert s["loops_inner"] == 0
        assert s["total_auto_continues"] == 0
        assert s["total_compressions"] == 0
        assert s["agent_instance_calls"] == 0

    def test_fresh_collector_token_defaults(self, collector):
        s = collector.get_session_summary()
        assert s["total_input_tokens_est"] == 0
        assert s["total_output_tokens_est"] == 0
        assert s["total_tokens"] == 0
        assert s["avg_tps"] == 0
        assert s["avg_llm_latency_ms"] == 0
        assert s["avg_tool_latency_ms"] == 0
        assert s["call_agent_count"] == 0
        assert s["call_agent_latency_ms"] == 0

    def test_fresh_collector_collections_empty(self, collector):
        s = collector.get_session_summary()
        assert s["llm_calls_by_model"] == {}
        assert s["tool_effectiveness"] == {}

    def test_all_expected_keys_present(self, collector):
        """Guard against a summary field being dropped from the returned dict."""
        expected_keys = {
            "session_id", "total_turns", "total_llm_calls", "total_tool_calls",
            "total_input_tokens_est", "total_output_tokens_est", "total_tokens",
            "avg_tps", "avg_llm_latency_ms", "avg_tool_latency_ms",
            "call_agent_count", "call_agent_latency_ms", "total_loops_detected",
            "loops_outer", "loops_inner", "total_auto_continues", "total_retries",
            "total_compressions", "write_failures", "agent_instance_calls",
            "llm_calls_by_model", "tool_effectiveness",
        }
        s = collector.get_session_summary()
        missing = expected_keys - set(s.keys())
        assert not missing, f"Missing summary keys: {missing}"

    def test_new_keys_present(self, collector):
        """The recently-added fields must exist in the dict (not just be 0)."""
        s = collector.get_session_summary()
        for key in ("loops_outer", "loops_inner", "total_auto_continues"):
            assert key in s


# ---------------------------------------------------------------------------
# B. Loop detection — loop_type breakdown (the key new feature)
# ---------------------------------------------------------------------------

class TestLoopDetection:
    def test_outer_loop_counts(self, collector):
        for _ in range(3):
            collector.record_loop_detected("inst", "repeat tool call", loop_type="outer")
        s = collector.get_session_summary()
        assert s["loops_outer"] == 3
        assert s["total_loops_detected"] == 3
        assert s["loops_inner"] == 0

    def test_inner_loop_counts(self, collector):
        for _ in range(2):
            collector.record_loop_detected("inst", "inner repeat", loop_type="inner")
        s = collector.get_session_summary()
        assert s["loops_inner"] == 2
        assert s["total_loops_detected"] == 2
        assert s["loops_outer"] == 0

    def test_mixed_invariant(self, collector):
        """The combined invariant: loops_outer + loops_inner == total_loops_detected."""
        for _ in range(4):
            collector.record_loop_detected("inst", "outer reason", loop_type="outer")
        for _ in range(5):
            collector.record_loop_detected("inst", "inner reason", loop_type="inner")
        s = collector.get_session_summary()
        assert s["loops_outer"] == 4
        assert s["loops_inner"] == 5
        # THE most important assertion — must always hold.
        assert s["loops_outer"] + s["loops_inner"] == s["total_loops_detected"]
        assert s["total_loops_detected"] == 9

    def test_default_loop_type_is_outer(self, collector):
        """Omitting loop_type defaults to outer behavior."""
        for _ in range(2):
            collector.record_loop_detected("inst", "default reason")
        s = collector.get_session_summary()
        assert s["loops_outer"] == 2
        assert s["loops_inner"] == 0
        assert s["total_loops_detected"] == 2

    def test_unknown_loop_type_counts_as_outer(self, collector):
        """Any non-'inner' value is bucketed as outer (matches source else-branch)."""
        collector.record_loop_detected("inst", "weird", loop_type="bogus")
        s = collector.get_session_summary()
        assert s["loops_outer"] == 1
        assert s["loops_inner"] == 0

    def test_auto_rolled_back_increments_retries(self, collector):
        """A rolled-back loop inside an active turn bumps per-turn retries, which
        roll up into the session total_retries on turn_end."""
        collector.record_turn_start("inst")
        collector.record_loop_detected(
            "inst", "stuck", auto_rolled_back=True, pop_count=2, loop_type="outer"
        )
        collector.record_turn_end("inst")
        s = collector.get_session_summary()
        assert s["total_retries"] == 1

    def test_no_rollback_does_not_increment_retries(self, collector):
        collector.record_turn_start("inst")
        collector.record_loop_detected("inst", "stuck", auto_rolled_back=False)
        collector.record_turn_end("inst")
        assert collector.get_session_summary()["total_retries"] == 0


# ---------------------------------------------------------------------------
# C. Auto-continue ("Malformed")
# ---------------------------------------------------------------------------

class TestAutoContinue:
    def test_counts_accumulate(self, collector):
        for _ in range(4):
            collector.record_auto_continue("inst", "malformed output")
        assert collector.get_session_summary()["total_auto_continues"] == 4

    def test_does_not_affect_loops_or_turns(self, collector):
        collector.record_auto_continue("inst", "malformed")
        s = collector.get_session_summary()
        assert s["total_loops_detected"] == 0
        assert s["loops_outer"] == 0
        assert s["loops_inner"] == 0
        assert s["total_turns"] == 0

    def test_event_written(self, collector):
        collector.record_auto_continue("inst", "malformed output")
        events = collector.get_recent_events(count=10)
        auto_events = [e for e in events if e.get("type") == "auto_continue"]
        assert len(auto_events) == 1
        assert auto_events[0]["instance"] == "inst"
        assert auto_events[0]["reason"] == "malformed output"


# ---------------------------------------------------------------------------
# D. Compression
# ---------------------------------------------------------------------------

class TestCompression:
    def test_increments_total_compressions(self, collector):
        for _ in range(3):
            collector.record_compression("inst", 0.5)
        assert collector.get_session_summary()["total_compressions"] == 3

    def test_tokens_saved_in_event(self, collector):
        collector.record_compression("inst", 0.4, tokens_before=1000, tokens_after=600)
        events = [e for e in collector.get_recent_events(count=10) if e["type"] == "compression"]
        assert len(events) == 1
        assert events[0]["tokens_saved"] == 400
        assert events[0]["fraction"] == 0.4

    def test_compression_tracked_per_turn(self, collector):
        """A compression during an active turn is reflected in the config stats."""
        fp = "fp_compress"
        collector.record_turn_start("inst", config_fingerprint=fp)
        collector.record_compression("inst", 0.5, tokens_before=100, tokens_after=50)
        collector.record_turn_end("inst")
        cfgs = {c["config_fingerprint"]: c for c in collector.get_config_comparison()}
        assert fp in cfgs


# ---------------------------------------------------------------------------
# E. Turn lifecycle
# ---------------------------------------------------------------------------

class TestTurnLifecycle:
    def test_start_then_end_increments_total_turns(self, collector):
        collector.record_turn_start("inst")
        collector.record_turn_end("inst")
        assert collector.get_session_summary()["total_turns"] == 1

    def test_multiple_turns_accumulate(self, collector):
        for _ in range(3):
            collector.record_turn_start("inst")
            collector.record_turn_end("inst")
        assert collector.get_session_summary()["total_turns"] == 3

    def test_end_without_start_is_safe_noop(self, collector):
        # Must not raise and must not increment.
        collector.record_turn_end("never_started")
        assert collector.get_session_summary()["total_turns"] == 0

    def test_turn_end_with_loop_and_auto_continue_during_turn(self, collector):
        """Regression guard: a turn that recorded a loop and an auto-continue
        must end cleanly (no KeyError from the removed per-turn auto_continues)."""
        collector.record_turn_start("inst")
        collector.record_loop_detected("inst", "stuck", loop_type="inner")
        collector.record_auto_continue("inst", "malformed")
        collector.record_turn_end("inst")  # must not raise
        s = collector.get_session_summary()
        assert s["total_turns"] == 1
        assert s["loops_inner"] == 1
        assert s["total_auto_continues"] == 1


# ---------------------------------------------------------------------------
# F. LLM call lifecycle
# ---------------------------------------------------------------------------

class TestLLMCallLifecycle:
    def test_full_lifecycle(self, collector):
        collector.record_llm_call_start("inst", input_tokens_est=100, model="qwen3-4b")
        collector.record_llm_first_token("inst")
        collector.record_llm_call_end("inst", output_tokens_est=50)
        s = collector.get_session_summary()
        assert s["total_llm_calls"] == 1
        assert s["llm_calls_by_model"]["qwen3-4b"] == 1
        assert s["total_input_tokens_est"] == 100
        assert s["total_output_tokens_est"] == 50
        assert s["total_tokens"] == 150

    def test_end_without_start_is_safe_noop(self, collector):
        # Must not raise and must not count a call.
        collector.record_llm_call_end("never_started")
        assert collector.get_session_summary()["total_llm_calls"] == 0

    def test_token_usage_overrides_input_estimate(self, collector):
        """record_token_usage updates the active call's input estimate with ground truth."""
        collector.record_llm_call_start("inst", input_tokens_est=10, model="m")
        collector.record_token_usage("inst", prompt_tokens=42, completion_tokens=7)
        collector.record_llm_call_end("inst")
        s = collector.get_session_summary()
        assert s["total_input_tokens_est"] == 42
        # Ground-truth completion tokens take priority over the char-count fallback.
        assert s["total_output_tokens_est"] == 7

    def test_token_usage_without_active_call_is_noop(self, collector):
        # Must not raise.
        collector.record_token_usage("never_started", prompt_tokens=5, completion_tokens=5)


# ---------------------------------------------------------------------------
# G. Tool call lifecycle
# ---------------------------------------------------------------------------

class TestToolCallLifecycle:
    def test_successful_tool_call(self, collector):
        collector.record_tool_call_start("a", "read_file")
        collector.record_tool_call_end("a", "read_file", success=True)
        s = collector.get_session_summary()
        assert s["total_tool_calls"] == 1
        # tool_effectiveness is keyed by tool name.
        eff = s["tool_effectiveness"]["read_file"]
        assert eff["total"] == 1
        assert eff["failures"] == 0
        assert eff["success_rate"] == 100.0

    def test_failed_tool_call_reflected_in_success_rate(self, collector):
        # One success + one failure for the same tool -> 50% success rate.
        collector.record_tool_call_start("a", "write_file")
        collector.record_tool_call_end("a", "write_file", success=True)
        collector.record_tool_call_start("a", "write_file")
        collector.record_tool_call_end("a", "write_file", success=False, error="boom")
        eff = collector.get_session_summary()["tool_effectiveness"]["write_file"]
        assert eff["total"] == 2
        assert eff["failures"] == 1
        assert eff["success_rate"] < 100.0

    def test_call_agent_routing(self, collector):
        """is_call_agent=True routes latency separately and is counted via call_agent_count."""
        collector.record_tool_call_start("a", "call_agent")
        collector.record_tool_call_end(
            "a", "call_agent", success=True, is_call_agent=True
        )
        s = collector.get_session_summary()
        # Count is driven by the is_call_agent flag (single source of truth),
        # so it always agrees with call_agent_latency_ms.
        assert s["call_agent_count"] == 1
        # Regular (non-agent) tool latency denominator stays clean.
        assert s["total_tool_calls"] == 1

    def test_call_agent_count_ignores_name_when_flag_off(self, collector):
        """Regression: a tool merely NAMED 'call_agent' with the flag off must NOT
        be counted as an agent delegation — count and latency share one source."""
        collector.record_tool_call_start("a", "call_agent")
        collector.record_tool_call_end(
            "a", "call_agent", success=True, is_call_agent=False
        )
        s = collector.get_session_summary()
        assert s["call_agent_count"] == 0
        # It still counts as a regular tool call and its latency goes to the
        # regular pool (not the call_agent pool).
        assert s["total_tool_calls"] == 1
        assert s["call_agent_latency_ms"] == 0

    def test_end_without_start_is_safe_noop(self, collector):
        # Must not raise and must not count a call.
        collector.record_tool_call_end("a", "ghost_tool")
        assert collector.get_session_summary()["total_tool_calls"] == 0


# ---------------------------------------------------------------------------
# H. Agent instance call
# ---------------------------------------------------------------------------

class TestAgentInstanceCall:
    def test_increments_agent_instance_calls(self, collector):
        collector.record_agent_instance_call("inst", "coder", "orchestrator", latency_ms=123.0)
        assert collector.get_session_summary()["agent_instance_calls"] == 1

    def test_multiple_calls_accumulate(self, collector):
        for _ in range(3):
            collector.record_agent_instance_call("inst", "researcher", "Maine")
        assert collector.get_session_summary()["agent_instance_calls"] == 3


# ---------------------------------------------------------------------------
# I. Config comparison
# ---------------------------------------------------------------------------

class TestConfigComparison:
    def test_fingerprint_appears_with_turns(self, collector):
        fp = "fp_abc123"
        collector.record_turn_start("inst", config_fingerprint=fp)
        collector.record_turn_end("inst")
        cfgs = {c["config_fingerprint"]: c for c in collector.get_config_comparison()}
        assert fp in cfgs
        assert cfgs[fp]["turns"] >= 1

    def test_empty_when_no_configured_turn(self, collector):
        # A turn with no fingerprint does not create a per-config entry.
        collector.record_turn_start("inst")
        collector.record_turn_end("inst")
        assert collector.get_config_comparison() == []


# ---------------------------------------------------------------------------
# J. Event log
# ---------------------------------------------------------------------------

class TestEventLog:
    def test_recent_events_have_type_and_timestamp(self, collector):
        collector.record_turn_start("inst")
        collector.record_loop_detected("inst", "stuck", loop_type="inner")
        collector.record_auto_continue("inst", "malformed")
        collector.record_compression("inst", 0.5)
        events = collector.get_recent_events(count=20)
        assert len(events) > 0
        for e in events:
            assert isinstance(e, dict)
            assert "type" in e
            assert "timestamp" in e

    def test_get_recent_events_respects_count(self, collector):
        for _ in range(5):
            collector.record_auto_continue("inst", "malformed")
        # Plus the initial session_start event written at construction.
        events = collector.get_recent_events(count=3)
        assert len(events) == 3

    def test_session_start_event_written_on_init(self, collector):
        events = collector.get_recent_events(count=50)
        types = [e["type"] for e in events]
        assert "session_start" in types
