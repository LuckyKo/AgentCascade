#!/usr/bin/env python3
"""Test script to verify call_agent exemption logic in telemetry."""

import sys
sys.path.insert(0, 'N:\\work\\WD\\AgentCascade_unified')

from agent_cascade.telemetry import TelemetryCollector

def test_edge_cases():
    """Test various edge cases for call_agent exemption."""
    
    print("=" * 60)
    print("Testing call_agent telemetry exemption logic")
    print("=" * 60)
    
    # Test 1: Only call_agent calls
    print("\n[TEST 1] ONLY CALL_AGENT CALLS")
    tc1 = TelemetryCollector()
    tc1.record_tool_call_start("inst1", "call_agent")
    tc1.record_tool_call_end("inst1", "call_agent", is_call_agent=True)
    
    summary1 = tc1.get_session_summary()
    print(f"  total_tool_calls: {summary1['total_tool_calls']}")
    print(f"  call_agent_count: {summary1['call_agent_count']}")
    print(f"  avg_tool_latency_ms: {summary1['avg_tool_latency_ms']}")
    print(f"  call_agent_latency_ms: {summary1['call_agent_latency_ms']}")
    
    assert summary1['total_tool_calls'] == 1, "Should have 1 total tool call"
    assert summary1['call_agent_count'] == 1, "Should have 1 call_agent count"
    assert summary1['avg_tool_latency_ms'] == 0, "Avg should be 0 when no non-call_agent tools"
    assert summary1['call_agent_latency_ms'] > 0, "Call agent latency should be recorded"
    print("  ✅ PASS")
    
    # Test 2: Only regular tool calls (no call_agent)
    print("\n[TEST 2] ONLY REGULAR TOOL CALLS")
    tc2 = TelemetryCollector()
    tc2.record_tool_call_start("inst1", "search_web")
    tc2.record_tool_call_end("inst1", "search_web", is_call_agent=False)
    
    summary2 = tc2.get_session_summary()
    print(f"  total_tool_calls: {summary2['total_tool_calls']}")
    print(f"  call_agent_count: {summary2['call_agent_count']}")
    print(f"  avg_tool_latency_ms: {summary2['avg_tool_latency_ms']}")
    print(f"  call_agent_latency_ms: {summary2['call_agent_latency_ms']}")
    
    assert summary2['total_tool_calls'] == 1, "Should have 1 total tool call"
    assert summary2['call_agent_count'] == 0, "Should have 0 call_agent count"
    assert summary2['avg_tool_latency_ms'] > 0, "Avg should be non-zero for regular tools"
    assert summary2['call_agent_latency_ms'] == 0, "Call agent latency should be 0"
    print("  ✅ PASS")
    
    # Test 3: Mixed calls (both call_agent and regular)
    print("\n[TEST 3] MIXED CALLS")
    tc3 = TelemetryCollector()
    tc3.record_tool_call_start("inst1", "search_web")
    tc3.record_tool_call_end("inst1", "search_web", is_call_agent=False)
    tc3.record_tool_call_start("inst1", "call_agent")
    tc3.record_tool_call_end("inst1", "call_agent", is_call_agent=True)
    tc3.record_tool_call_start("inst1", "analyze_data")
    tc3.record_tool_call_end("inst1", "analyze_data", is_call_agent=False)
    
    summary3 = tc3.get_session_summary()
    print(f"  total_tool_calls: {summary3['total_tool_calls']}")
    print(f"  call_agent_count: {summary3['call_agent_count']}")
    print(f"  avg_tool_latency_ms: {summary3['avg_tool_latency_ms']}")
    print(f"  call_agent_latency_ms: {summary3['call_agent_latency_ms']}")
    
    assert summary3['total_tool_calls'] == 3, "Should have 3 total tool calls"
    assert summary3['call_agent_count'] == 1, "Should have 1 call_agent count"
    # avg should be based on (search_web + analyze_data) / 2, excluding call_agent
    # Let's verify the calculation is correct by checking it's reasonable
    print(f"  Non-call_agent calls: {summary3['total_tool_calls'] - summary3['call_agent_count']}")
    assert summary3['avg_tool_latency_ms'] > 0, "Avg should be non-zero for mixed case"
    print("  ✅ PASS")
    
    # Test 4: Per-tool latency accumulation (including call_agent)
    print("\n[TEST 4] PER-TOOL LATENCY ACCUMULATION")
    tc4 = TelemetryCollector()
    tc4.record_tool_call_start("inst1", "call_agent")
    tc4.record_tool_call_end("inst1", "call_agent", is_call_agent=True)
    tc4.record_tool_call_start("inst1", "search_web")
    tc4.record_tool_call_end("inst1", "search_web", is_call_agent=False)
    
    summary4 = tc4.get_session_summary()
    print(f"  tool_effectiveness: {summary4['tool_effectiveness']}")
    
    assert 'call_agent' in summary4['tool_effectiveness'], "call_agent should be in tool effectiveness"
    assert 'search_web' in summary4['tool_effectiveness'], "search_web should be in tool effectiveness"
    print("  ✅ PASS")
    
    # Test 5: Backward compatibility - calling without is_call_agent parameter
    print("\n[TEST 5] BACKWARD COMPATIBILITY (no is_call_agent param)")
    tc5 = TelemetryCollector()
    # This simulates old code that doesn't pass is_call_agent
    tc5.record_tool_call_start("inst1", "search_web")
    # Call without is_call_agent - should default to False
    import inspect
    sig = inspect.signature(tc5.record_tool_call_end)
    print(f"  record_tool_call_end signature: {sig}")
    assert 'is_call_agent' in sig.parameters, "Parameter should exist"
    assert sig.parameters['is_call_agent'].default is False, "Default should be False"
    
    # Actually call it without the parameter to verify default works
    try:
        tc5.record_tool_call_end("inst1", "search_web")
        summary5 = tc5.get_session_summary()
        assert summary5['call_agent_count'] == 0, "Should not count as call_agent"
        assert summary5['total_tool_calls'] == 1, "Should count as regular tool call"
        print("  ✅ PASS - default parameter works correctly")
    except Exception as e:
        print(f"  ❌ FAIL - {e}")
        raise
    
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED! ✅")
    print("=" * 60)

if __name__ == "__main__":
    test_edge_cases()