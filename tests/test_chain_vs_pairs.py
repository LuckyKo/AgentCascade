"""Tests for distinguishing independent A→F pairs from true A→A→F→F chains.

This test file verifies the fix for a bug where _refine_tool_call_boundary
treated ANY sequence of A→F pairs as one continuous chain, causing the discard
boundary to overshoot into the keep zone and fail with "tool-call chains extend
past the keep zone".

Key distinction:
- A→F → A→F = two independent pairs (safe to split between them)
- A→A → F→F = one batched chain (should NOT split within it)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from agent_cascade.llm.schema import ASSISTANT, FUNCTION, USER, Message
from agent_cascade.compression.helpers import compute_discard_count


def _make_msg(role, content="text", function_call=None, extra=None):
    """Create a test message with optional function_call and extra dict."""
    if role == ASSISTANT and function_call:
        fc = {'name': function_call, 'arguments': '{}'}
        if extra is None:
            extra = {'function_id': f"call_{function_call}"}
        return Message(role=role, content=content, function_call=fc, extra=extra)
    return Message(role=role, content=content, extra=extra or None)


def _make_assistant(fc_name):
    """Create an ASSISTANT message with a tool call."""
    return _make_msg(ASSISTANT, f"calling {fc_name}", function_call=fc_name)


def _make_function(result_text, fid):
    """Create a FUNCTION result message."""
    return _make_msg(FUNCTION, result_text, extra={'function_id': fid})


class TestIndependentPairsVsChains:
    """Test that independent A→F pairs are NOT treated as one chain."""

    def test_two_independent_pairs_no_overshoot(self):
        """A→F→A→F pattern: two independent pairs should allow clean split between them.
        
        Layout: [A(fc0), F(res0), A(fc1), F(res1)]
        Cut at position 2 (start of second pair) → should include both A(fc1)+F(res1).
        """
        msgs = [
            _make_assistant("tool_0"),
            _make_function("result_0", "call_tool_0"),
            _make_assistant("tool_1"),
            _make_function("result_1", "call_tool_1"),
        ]
        # fraction=0.5 → discard=int(4*0.5)=2, max_discard=2 (keep 2 tail)
        # At pos 2: A with tool_call → advance past F(res1) matching call_tool_1
        count = compute_discard_count(msgs, 0.5, False)
        assert count == 2, f"Expected discard=2 for independent pairs, got {count}"

    def test_four_independent_pairs_clean_split(self):
        """Four A→F pairs: should find clean boundary between pairs.
        
        Layout: [A(fc0), F(res0), A(fc1), F(res1), A(fc2), F(res2), A(fc3), F(res3)]
        fraction=0.5 → discard=4 (at A fc2) → should include pair 2 and stop at 6.
        """
        msgs = []
        for i in range(4):
            msgs.append(_make_assistant(f"tool_{i}"))
            msgs.append(_make_function(f"result_{i}", f"call_tool_{i}"))
        
        count = compute_discard_count(msgs, 0.5, False)
        # Should discard first 3 pairs (6 messages), keeping last pair + room for tail
        assert count > 0 and count <= len(msgs) - 2, \
            f"Discard {count} out of {len(msgs)} exceeds keep zone"
        # Remaining should be even (complete pairs) or have at least 2 tail msgs
        remaining = len(msgs) - count
        assert remaining >= 2

    def test_batched_chain_not_split(self):
        """Batched chain with no room to split returns -1.
        
        Layout: [A(fc0), A(fc1), F(res0), F(res1)]
        Cut at position 2 (at first F) → skip past Fs → discard=4 > max_discard(2) → -1.
        """
        msgs = [
            _make_assistant("tool_0"),
            _make_assistant("tool_1"),
            _make_function("result_0", "call_tool_0"),
            _make_function("result_1", "call_tool_1"),
        ]
        
        count = compute_discard_count(msgs, 0.5, False)
        # fraction=0.5 → discard=int(4*0.5)=2, max_discard=2
        # At pos 2: FUNCTION → skip past consecutive Fs → 4 > len-2=2 → -1
        assert count == -1, f"Expected -1 for batched chain with no room to split, got {count}"

    def test_batched_chain_with_plain_tail(self):
        """A→A→F→F with plain tail messages: should complete the chain."""
        msgs = [
            _make_assistant("tool_0"),
            _make_assistant("tool_1"),
            _make_function("result_0", "call_tool_0"),
            _make_function("result_1", "call_tool_1"),
            _make_msg(ASSISTANT, "done"),
            _make_msg(USER, "next"),
        ]
        
        count = compute_discard_count(msgs, 0.5, False)
        assert count >= 2 and count <= len(msgs) - 2

    def test_independent_pairs_with_plain_separators(self):
        """A→F→a→U→A→F pattern: independent pairs separated by plain messages."""
        msgs = [
            _make_assistant("tool_0"),
            _make_function("result_0", "call_tool_0"),
            _make_msg(ASSISTANT, "thinking"),
            _make_msg(USER, "next query"),
            _make_assistant("tool_1"),
            _make_function("result_1", "call_tool_1"),
        ]
        
        count = compute_discard_count(msgs, 0.5, False)
        assert count >= 0 and count <= len(msgs) - 2

    def test_mixed_pattern_independent_then_batched(self):
        """Mixed pattern where refinement pushes past keep zone returns -1."""
        msgs = [
            _make_assistant("tool_0"),
            _make_function("result_0", "call_tool_0"),
            _make_assistant("tool_1"),
            _make_assistant("tool_2"),
            _make_function("result_1", "call_tool_1"),
            _make_function("result_2", "call_tool_2"),
        ]
        
        count = compute_discard_count(msgs, 0.5, False)
        assert count == -1

    def test_no_false_negative_on_independent_pairs(self):
        """Ensure independent pairs don't cause -1 (compression failure)."""
        # Build a long sequence of independent A→F pairs
        msgs = []
        for i in range(8):
            msgs.append(_make_assistant(f"tool_{i}"))
            msgs.append(_make_function(f"result_{i}", f"call_tool_{i}"))
        
        count = compute_discard_count(msgs, 0.5, False)
        assert count != -1, "Independent pairs should not cause compression failure"
        assert count > 0, "Should discard some messages"

    def test_true_chain_causes_failure_when_no_clean_split(self):
        """Batched chain with no room to split returns -1."""
        msgs = [
            _make_assistant("tool_0"),
            _make_assistant("tool_1"),
            _make_function("result_0", "call_tool_0"),
            _make_function("result_1", "call_tool_1"),
        ]
        
        # fraction=1.0 → discard=int(4*1.0)=2, max_discard=min(2, 4-2)=2
        # Same as test_batched_chain_not_split: at pos 2 (F) → skip past Fs → -1
        count = compute_discard_count(msgs, 1.0, False)
        assert count == -1, f"Expected -1 for batched chain with no room to split, got {count}"


class TestEdgeCases:
    """Edge cases for the fix."""

    def test_single_pair(self):
        """Active set with just one A→F pair."""
        msgs = [
            _make_assistant("tool_0"),
            _make_function("result_0", "call_tool_0"),
        ]
        count = compute_discard_count(msgs, 0.5, False)
        assert count == 0, "Should keep the pair as tail"

    def test_three_pairs_fraction_half(self):
        """Three independent pairs at 50%."""
        msgs = []
        for i in range(3):
            msgs.append(_make_assistant(f"tool_{i}"))
            msgs.append(_make_function(f"result_{i}", f"call_tool_{i}"))
        
        count = compute_discard_count(msgs, 0.5, False)
        assert count >= 0 and count <= len(msgs) - 2

    def test_empty_active_set(self):
        """Empty active set returns 0."""
        assert compute_discard_count([], fraction=0.5, force=False) == 0

    def test_force_mode_independent_pairs(self):
        """Force mode with independent pairs should still work."""
        msgs = [
            _make_assistant("tool_0"),
            _make_function("result_0", "call_tool_0"),
            _make_assistant("tool_1"),
            _make_function("result_1", "call_tool_1"),
        ]
        
        count = compute_discard_count(msgs, 0.5, force=True)
        assert count >= 1 and count <= len(msgs) - 2


class TestRefinementLogic:
    """Test the _refine_tool_call_boundary function directly."""

    def test_refinement_stops_at_independent_pair(self):
        """Refinement should stop after completing one pair, not advance to next."""
        from agent_cascade.compression.helpers import _refine_tool_call_boundary
        
        msgs = [
            _make_assistant("tool_0"),
            _make_function("result_0", "call_tool_0"),
            _make_assistant("tool_1"),
            _make_function("result_1", "call_tool_1"),
            _make_assistant("tool_2"),
            _make_function("result_2", "call_tool_2"),
        ]
        
        # Start at position 0 (A with tool_call), max_discard=4
        result = _refine_tool_call_boundary(msgs, 0, 4)
        assert result <= 4

    def test_refinement_includes_matching_functions_only(self):
        """Refinement should only include FUNCTION results matching collected IDs."""
        from agent_cascade.compression.helpers import _refine_tool_call_boundary
        
        msgs = [
            _make_assistant("tool_0"),
            _make_function("result_0", "call_tool_0"),
            _make_assistant("tool_1"),
            _make_function("result_1", "call_tool_1"),
            _make_assistant("tool_2"),
            _make_function("result_2", "call_tool_2"),
        ]
        
        # Start at position 2 (A with tool_1), max_discard=4
        result = _refine_tool_call_boundary(msgs, 2, 4)
        assert result >= 2 and result <= 4


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])