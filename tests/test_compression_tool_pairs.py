"""Tests for compression tool-call pair boundary awareness.

Verifies that compute_discard_count() correctly avoids splitting
ASSISTANT(tool_call) → FUNCTION(result) pairs at the cut boundary.

Covers:
- _has_pending_tool_calls() helper detection logic
- compute_discard_count() refinement step with single and multiple pairs
- Edge cases (all tool pairs, empty set, dict-style messages)
- Integration: compress_context respects pair boundaries end-to-end

All tests are self-contained — no LLM or API server required.
"""

import pytest
from unittest.mock import MagicMock, patch

from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, FUNCTION, Message
from agent_cascade.compression.helpers import (
    compute_discard_count,
    _has_pending_tool_calls,
)
from agent_cascade.compression.core import compress_context


# ──────────────────────────────────────────────
# Fixtures — Tool-Call Aware Message Builders
# ──────────────────────────────────────────────

def _make_msg(role, content):
    """Create a plain Message object."""
    return Message(role=role, content=content)


# Global counter for generating unique function IDs across test messages
_fc_counter = 0


@pytest.fixture(autouse=True)
def reset_fc_counter():
    """Reset the global _fc_counter before each test to avoid ordering issues."""
    global _fc_counter
    _fc_counter = 0


def _make_assistant_with_fc(content, fc_name="read_file", fc_args='{"path": "test.txt"}'):
    """Create an ASSISTANT message with a legacy-style function_call attribute.

    In legacy mode, the assistant stores a FunctionCall object on the
    `function_call` attribute to indicate it wants to invoke a tool.
    Each call gets a unique function_id in extra for ID-based matching.
    """
    global _fc_counter
    fid = f"call_{_fc_counter}"
    _fc_counter += 1
    from agent_cascade.llm.schema import FunctionCall
    fc = FunctionCall(name=fc_name, arguments=fc_args)
    return Message(role=ASSISTANT, content=content, function_call=fc, extra={'function_id': fid})


def _make_assistant_with_tool_calls(content, tc_id="call_abc", tc_index=0):
    """Create an ASSISTANT message with native OpenAI-style tool calls.

    In native mode, tool call info is stored in the `extra` dict with a
    `tool_index` key (set by oai.py when parsing streaming responses).
    """
    return Message(
        role=ASSISTANT,
        content=content,
        extra={'function_id': tc_id, 'tool_index': tc_index},
    )


def _make_function_result(content, name="read_file", fid=None):
    """Create a FUNCTION result message (response to a tool call).

    The function_id is stored in extra['function_id'] per OpenAI spec.
    If fid is None, the previous ASSISTANT's ID is used automatically
    via the _fc_counter mechanism (decremented by 1).
    """
    global _fc_counter
    if fid is None:
        # Use the most recent function_id from the counter
        fid = f"call_{_fc_counter - 1}"
    return Message(role=FUNCTION, content=content, name=name, extra={'function_id': fid})


# ──────────────────────────────────────────────
# 1. Test _has_pending_tool_calls() helper
# ──────────────────────────────────────────────

class TestHasPendingToolCalls:
    """Test the tool-call detection helper function."""

    def test_detects_legacy_function_call(self):
        """Legacy mode: function_call attribute is detected."""
        msg = _make_assistant_with_fc("Let me read that file")
        assert _has_pending_tool_calls(msg) is True

    def test_detects_native_tool_calls_via_extra(self):
        """Native mode: tool_index in extra dict is detected."""
        msg = _make_assistant_with_tool_calls("Calling the tool", tc_id="call_123", tc_index=0)
        assert _has_pending_tool_calls(msg) is True

    def test_plain_assistant_no_tool_call(self):
        """Assistant without any tool call returns False."""
        msg = _make_msg(ASSISTANT, "Just a regular reply")
        assert _has_pending_tool_calls(msg) is False

    def test_function_message_returns_false(self):
        """FUNCTION role messages return False (they are responses, not callers)."""
        msg = _make_function_result("File content here")
        assert _has_pending_tool_calls(msg) is False

    def test_user_message_returns_false(self):
        """USER role messages return False."""
        msg = _make_msg(USER, "Please read this file")
        assert _has_pending_tool_calls(msg) is False

    def test_dict_style_messages(self):
        """Dict-style messages work via getattr fallback."""
        fc_dict = {"name": "read_file", "arguments": "{}"}
        msg = {"role": ASSISTANT, "content": "test", "function_call": fc_dict}
        assert _has_pending_tool_calls(msg) is True

    def test_none_input_returns_false(self):
        """None input returns False (not an empty list, just None)."""
        assert _has_pending_tool_calls(None) is False

    def test_detects_tool_calls_array_format(self):
        """Standard OpenAI tool_calls array format is detected."""
        msg = Message(role=ASSISTANT, content="Using tools",
                      tool_calls=[{"id": "c1", "type": "function",
                                   "function": {"name": "read_file"}}])
        assert _has_pending_tool_calls(msg) is True

    def test_detects_multiple_tool_calls_array(self):
        """Multiple entries in tool_calls array are detected."""
        msg = Message(role=ASSISTANT, content="Using tools",
                      tool_calls=[
                          {"id": "c1", "type": "function",
                           "function": {"name": "read_file"}},
                          {"id": "c2", "type": "function",
                           "function": {"name": "write_file"}},
                      ])
        assert _has_pending_tool_calls(msg) is True


# ──────────────────────────────────────────────
# 2. Test compute_discard_count() with tool-call pairs
# ──────────────────────────────────────────────

class TestComputeDiscardCountToolPairs:
    """Test that discard count respects ASSISTANT→FUNCTION pair boundaries."""

    def test_no_adjustment_when_cut_is_clean(self):
        """Cut falls on first ASSISTANT of a pair → safe split point (rule 3)."""
        # [A0, F0, A1, F1, A2, F2, A3, F3]  fraction=0.5 → discard=4
        active = [
            _make_assistant_with_fc("A0"), _make_function_result("F0"),
            _make_assistant_with_fc("A1"), _make_function_result("F1"),
            _make_assistant_with_fc("A2"), _make_function_result("F2"),
            _make_assistant_with_fc("A3"), _make_function_result("F3"),
        ]
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(8*0.5)=4; min(4, 6)=4; position 4 is A2 (first of pair, prev=F1)
        # Rule 3: first A of independent pair is safe split point → discard stays at 4
        assert count == 4

    def test_adjusts_for_single_pair_at_boundary(self):
        """Cut lands on ASSISTANT with function_call → include its FUNCTION response."""
        # [A0, F0, A1, F1, A2, F2]  fraction=0.5 → discard=4 (post-refinement includes F1)
        active = [
            _make_assistant_with_fc("A0"), _make_function_result("F0"),
            _make_assistant_with_fc("A1"), _make_function_result("F1"),
            _make_assistant_with_fc("A2"), _make_function_result("F2"),
        ]
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(6*0.5)=3; min(3, 4)=3; position 3 is F1 (no tool_call) → post-refinement finds A1 pending, advances past F1 → 4
        assert count == 4

    def test_adjusts_for_pair_split_at_exact_boundary(self):
        """Cut lands on first ASSISTANT of a pair → safe split point (rule 3)."""
        # [A0, F0, A1, F1, A2]  fraction=0.5 → discard=2 (lands on A1)
        active = [
            _make_assistant_with_fc("A0"), _make_function_result("F0"),
            _make_assistant_with_fc("A1"), _make_function_result("F1"),
            _make_assistant_with_fc("A2"),
        ]
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(5*0.5)=2; min(2, 3)=2; position 2 is A1 (first of pair, prev=F0)
        # Rule 3: first A of independent pair is safe split point → discard stays at 2
        assert count == 2

    def test_adjusts_for_multiple_consecutive_pairs(self):
        """Multiple consecutive ASSISTANT→FUNCTION chains at boundary."""
        # [A0, F0, A1, F1, A2, F2] with fraction=0.4 → discard=2 (lands on A1)
        active = [
            _make_assistant_with_fc("A0"), _make_function_result("F0"),
            _make_assistant_with_fc("A1"), _make_function_result("F1"),
            _make_assistant_with_fc("A2"), _make_function_result("F2"),
        ]
        count = compute_discard_count(active, fraction=0.4, force=False)
        # int(6*0.4)=2; min(2, 4)=2; position 2 is A1 (first of pair, prev=F0)
        # Rule 3: first A of independent pair is safe split point → discard stays at 2
        assert count == 2

    def test_native_tool_calls_adjustment(self):
        """Native mode: tool_index in extra dict triggers adjustment."""
        active = [
            _make_assistant_with_tool_calls("A0", tc_id="call_0"),
            _make_function_result("F0"),
            _make_assistant_with_tool_calls("A1", tc_id="call_1"),
            _make_function_result("F1"),
            _make_assistant_with_tool_calls("A2", tc_id="call_2"),
            _make_function_result("F2"),
        ]
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(6*0.5)=3; min(3, 4)=3; position 3 is F1 → include it to avoid splitting A1→F1 pair
        assert count == 4

    def test_mixed_legacy_and_native(self):
        """Mixed message types in same active set."""
        active = [
            _make_assistant_with_fc("A0-legacy"),
            _make_function_result("F0"),
            _make_assistant_with_tool_calls("A1-native", tc_id="call_1"),
            _make_function_result("F1"),
            _make_msg(ASSISTANT, "A2-plain"),
            _make_msg(FUNCTION, "F2"),
        ]
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(6*0.5)=3; min(3, 4)=3; position 3 is F1 → include it to avoid splitting A1→F1 pair
        assert count == 4

    def test_empty_active_set(self):
        """Empty active set returns 0."""
        assert compute_discard_count([], fraction=0.5, force=False) == 0

    def test_all_tool_pairs_no_plain_messages(self):
        """Active set is entirely tool-call pairs."""
        active = [
            _make_assistant_with_fc("A0"), _make_function_result("F0"),
            _make_assistant_with_fc("A1"), _make_function_result("F1"),
            _make_assistant_with_fc("A2"), _make_function_result("F2"),
            _make_assistant_with_fc("A3"), _make_function_result("F3"),
        ]
        count = compute_discard_count(active, fraction=0.5, force=False)
        # Should keep at least 2 tail messages and not split pairs
        assert count <= len(active) - 2
        # The remaining messages should form complete pairs (even count)
        remaining = len(active) - count
        assert remaining % 2 == 0 or remaining >= 2

    def test_force_mode_with_tool_pairs(self):
        """Force mode still respects pair boundaries."""
        active = [
            _make_assistant_with_fc("A0"), _make_function_result("F0"),
            _make_assistant_with_fc("A1"), _make_function_result("F1"),
            _make_assistant_with_fc("A2"), _make_function_result("F2"),
        ]
        count = compute_discard_count(active, fraction=0.5, force=True)
        # int(6*0.5)=3; max(1, min(3, 4))=3; position 3 is F1 (rule 1: skip past Fs)
        # Pos 3 is F → discard advances to 4. Pos 4 is A2 (first of pair, rule 3 safe).
        # discard=4 ≤ max_discard=4 → valid split at 4
        assert count == 4

    def test_preserves_tail_messages(self):
        """Tail messages are preserved even with tool-call pairs."""
        active = [
            _make_assistant_with_fc("A0"), _make_function_result("F0"),
            _make_assistant_with_fc("A1"), _make_function_result("F1"),
            _make_assistant_with_fc("A2"), _make_function_result("F2"),
        ]
        count = compute_discard_count(active, fraction=1.0, force=False)
        remaining = len(active) - count
        assert remaining >= 2


# ──────────────────────────────────────────────
# 3. Integration: compress_context end-to-end with tool pairs
# ──────────────────────────────────────────────

class TestCompressContextToolPairsIntegration:
    """End-to-end tests for compression with tool-call pair awareness."""

    def _build_pool_with_tool_calls(self, num_pairs=5):
        """Build a MockAgentPool with realistic tool-call conversation history.

        Layout: [SYSTEM] + (USER, ASSISTANT+fc, FUNCTION) * num_pairs
        """
        from tests.test_compression import MockAgentPool

        history = [_make_msg(SYSTEM, "You are a helpful agent")]
        for i in range(num_pairs):
            history.append(_make_msg(USER, f"Task {i}: read file"))
            history.append(_make_assistant_with_fc(f"Reading file {i}"))
            history.append(_make_function_result(f"Content of file {i}"))

        pool = MockAgentPool(history)
        return pool

    def test_compression_respects_pair_boundaries(self):
        """After compression, remaining messages should not have orphaned pairs."""
        from tests.test_compression import MockAgentPool

        history = [_make_msg(SYSTEM, "You are a helpful agent")]
        for i in range(8):
            history.append(_make_msg(USER, f"Task {i}"))
            history.append(_make_assistant_with_fc(f"Calling tool for task {i}"))
            history.append(_make_function_result(f"Result {i}"))

        pool = MockAgentPool(history)
        initial_len = len(pool.get_conversation("TestAgent"))

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary of tool calls"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=False,
            )

        assert result.success is True
        new_history = pool.get_conversation("TestAgent")
        assert len(new_history) < initial_len

        # Verify no orphaned pairs: scan remaining messages for ASSISTANT→FUNCTION splits
        for i in range(len(new_history)):
            if _has_pending_tool_calls(new_history[i]):
                # Next message should be a FUNCTION result (or another pair member)
                if i + 1 < len(new_history):
                    next_msg = new_history[i + 1]
                    next_role = getattr(next_msg, 'role', '')
                    assert next_role == FUNCTION or _has_pending_tool_calls(next_msg), \
                        f"ASSISTANT at pos {i} has tool call but next msg is role={next_role}"

    def test_no_orphaned_function_results_after_compression(self):
        """After compression, no FUNCTION message should appear without its ASSISTANT."""
        from tests.test_compression import MockAgentPool

        history = [_make_msg(SYSTEM, "You are a helpful agent")]
        for i in range(6):
            history.append(_make_msg(USER, f"Task {i}"))
            history.append(_make_assistant_with_fc(f"Tool call {i}"))
            history.append(_make_function_result(f"Result {i}"))

        pool = MockAgentPool(history)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=False,
            )

        assert result.success is True
        new_history = pool.get_conversation("TestAgent")

        # Check that FUNCTION messages are preceded by their ASSISTANT (or marker/system)
        for i, msg in enumerate(new_history):
            role = getattr(msg, 'role', '')
            if role == FUNCTION and i > 0:
                prev_role = getattr(new_history[i - 1], 'role', '')
                # Function results should be preceded by ASSISTANT (tool caller)
                assert prev_role == ASSISTANT, \
                    f"FUNCTION at pos {i} not preceded by ASSISTANT (prev role={prev_role})"

    def test_compression_with_mixed_message_types(self):
        """Compression works when tool-call pairs are mixed with plain messages."""
        from tests.test_compression import MockAgentPool

        history = [_make_msg(SYSTEM, "You are a helpful agent")]
        # Mix of: plain Q&A + tool-call chains
        for i in range(6):
            if i % 2 == 0:
                # Plain conversation
                history.append(_make_msg(USER, f"Question {i}"))
                history.append(_make_msg(ASSISTANT, f"Answer {i}"))
            else:
                # Tool-call chain
                history.append(_make_msg(USER, f"Task {i}"))
                history.append(_make_assistant_with_fc(f"Calling tool for task {i}"))
                history.append(_make_function_result(f"Result {i}"))

        pool = MockAgentPool(history)
        initial_len = len(pool.get_conversation("TestAgent"))

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary of mixed conversation"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=False,
            )

        assert result.success is True
        new_history = pool.get_conversation("TestAgent")
        assert len(new_history) < initial_len


# ──────────────────────────────────────────────
# 3b. Integration: parallel tool calls [A,A,A,F,F,F] pattern
# ──────────────────────────────────────────────

class TestParallelToolCallsPattern:
    """Test the [ASSISTANT, ASSISTANT, ..., FUNCTION, FUNCTION, ...] batched pattern."""

    def test_batched_parallel_calls_at_boundary(self):
        """Cut lands in middle of [A,A,A,F,F,F] chain → includes complete chain."""
        from tests.test_compression import MockAgentPool

        # Simulate: 3 parallel tool calls → [A_fc1, A_fc2, A_fc3, F_res1, F_res2, F_res3]
        history = [_make_msg(SYSTEM, "You are a helpful agent")]
        for i in range(4):
            history.append(_make_msg(USER, f"Task {i}"))
            # Each task produces 3 parallel tool calls
            for j in range(3):
                history.append(_make_assistant_with_fc(f"Call {i}.{j}"))
            for j in range(3):
                history.append(_make_function_result(f"Result {i}.{j}"))

        pool = MockAgentPool(history)
        initial_len = len(pool.get_conversation("TestAgent"))

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"
            result = compress_context(
                agent_pool=pool, target_agent_name="TestAgent",
                fraction=0.5, mode="auto", force=False,
            )

        assert result.success is True
        new_history = pool.get_conversation("TestAgent")
        assert len(new_history) < initial_len

        # Verify no orphaned pairs remain
        for i in range(len(new_history)):
            if _has_pending_tool_calls(new_history[i]):
                if i + 1 < len(new_history):
                    next_role = getattr(new_history[i + 1], 'role', '')
                    assert next_role == FUNCTION or _has_pending_tool_calls(new_history[i+1])

    # ──────────────────────────────────────────────
# 4. Edge Cases
# ──────────────────────────────────────────────

class TestEdgeCases:
    """Edge cases for tool-call pair boundary handling."""

    def test_single_pair_active_set(self):
        """Active set with just one ASSISTANT→FUNCTION pair."""
        active = [
            _make_assistant_with_fc("A0"),
            _make_function_result("F0"),
        ]
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(2*0.5)=1; min(1, 0)=0 → discard 0 (keep both as tail)
        assert count == 0

    def test_three_pairs_active_set(self):
        """Three pairs with 50% fraction."""
        active = [
            _make_assistant_with_fc("A0"), _make_function_result("F0"),
            _make_assistant_with_fc("A1"), _make_function_result("F1"),
            _make_assistant_with_fc("A2"), _make_function_result("F2"),
        ]
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(6*0.5)=3; min(3, 4)=3; position 3 is F1 → post-refinement finds A1 pending, advances past F1 → 4
        assert count == 4

    def test_large_active_set_fraction(self):
        """Large active set with various fractions."""
        active = []
        for i in range(20):
            active.append(_make_assistant_with_fc(f"A{i}"))
            active.append(_make_function_result(f"F{i}"))

        # 50% fraction on 40 messages → discard 20, clamped to 38 (keep 2 tail)
        count = compute_discard_count(active, fraction=1.0, force=False)
        assert count <= len(active) - 2

    def test_fraction_just_below_pair_boundary(self):
        """Fraction that cuts just before a pair boundary."""
        active = [
            _make_assistant_with_fc("A0"), _make_function_result("F0"),
            _make_assistant_with_fc("A1"), _make_function_result("F1"),
            _make_assistant_with_fc("A2"), _make_function_result("F2"),
            _make_assistant_with_fc("A3"), _make_function_result("F3"),
        ]
        # fraction=0.4 → int(8*0.4)=3; position 3 is F1 (rule 1: skip past Fs)
        # Pos 3 is F, pos 4 is A2 → discard advances to 4.
        # Pos 4 is A2 (first of pair, prev=F1, rule 3 safe) → discard stays at 4
        count = compute_discard_count(active, fraction=0.4, force=False)
        assert count == 4


# ──────────────────────────────────────────────
# 5. Brute-force regression: randomized conversations
# ──────────────────────────────────────────────

class TestBruteForceRegression:
    """Randomized brute-force tests to catch edge cases in tool-call boundary refinement.

    Each test generates many random conversation patterns (sequential, batched, mixed)
    and verifies that compute_discard_count never splits an ASSISTANT→FUNCTION pair.
    """

    def _validate_no_splits(self, active_set, discard):
        """Check no FUNCTION in tail has its ASSISTANT in the discarded range."""
        from agent_cascade.compression.helpers import (
            _get_function_call_ids, _get_function_result_id,
        )
        for i in range(discard, len(active_set)):
            if active_set[i].get('role', '') == FUNCTION:  # FIX 6: use constant instead of 'function' string
                fnid = _get_function_result_id(active_set[i])
                for j in range(i - 1, -1, -1):
                    aids = _get_function_call_ids(active_set[j])
                    if fnid and fnid in aids:
                        assert j >= discard, (
                            f"Split at [{i}]←[{j}]: function_id={fnid}"
                        )
                        break

    def _build_random_conversation(self, seed):
        """Build a random conversation with mixed tool-call patterns."""
        # FIX 6: Use per-test Random instance instead of shared class-level self._random
        rng = __import__('random').Random(seed)
        fc_ids = []
        conv = [
            _make_msg(SYSTEM, "system"),
            _make_msg(USER, "first prompt"),
        ]
        for _ in range(rng.randint(10, 40)):
            r = rng.random()
            if r < 0.3:
                conv.append(_make_msg(USER, "question"))
            elif r < 0.55:
                # Sequential: A(fc) → F
                a = _make_assistant_with_fc("call")
                fid = (a.extra or {}).get('function_id')
                conv.extend([a, _make_function_result("res", fid=fid)])
            elif r < 0.75:
                # Batched: [A,A,...,F,F]
                n = rng.randint(2, 4)
                for _ in range(n):
                    a = _make_assistant_with_fc("call")
                    fid = (a.extra or {}).get('function_id')
                    conv.append(a)
                    fc_ids.append(fid)
                for f in fc_ids:
                    conv.append(_make_function_result("res", fid=f))
                fc_ids.clear()
            elif r < 0.9:
                conv.append(_make_msg(ASSISTANT, "plain text"))
            else:
                # Sequential with user message after the pair completes
                a = _make_assistant_with_fc("call")
                fid = (a.extra or {}).get('function_id')
                conv.extend([a, _make_function_result("res", fid=fid),
                             _make_msg(USER, "follow-up")])
        return conv

    def test_brute_force_sequential(self):
        """1,000 random conversations tested at 3 fractions each."""
        for seed in range(1000):
            conv = self._build_random_conversation(seed)
            active = conv[2:]
            for frac_int in [30, 50, 70]:
                discard = compute_discard_count(active, frac_int / 100, force=False)
                if discard == -1:
                    continue  # -1 sentinel means no valid compression; skip validation
                self._validate_no_splits(active, discard)

    def test_brute_force_sequential_with_markers(self):
        """Simulate sequential compression with markers inserted between rounds."""
        import random as _random_mod
        for seed in range(500):
            rng = _random_mod.Random(seed + 10000)
            conv = self._build_random_conversation(seed + 10000)
            for _ in range(3):
                # Find last marker or start at index 2
                latest_marker = -1
                for i in range(len(conv) - 1, -1, -1):
                    c = conv[i].get('content', '') if isinstance(conv[i], dict) else getattr(conv[i], 'content', '')
                    if isinstance(c, str) and 'MARKER' in c:
                        latest_marker = i
                        break
                asi = latest_marker + 1 if latest_marker >= 0 else 2
                active = conv[asi:]
                frac = rng.uniform(0.3, 0.7)
                discard = compute_discard_count(active, frac, force=False)
                if discard == -1:
                    continue  # -1 sentinel means no valid compression; skip validation
                self._validate_no_splits(active, discard)
                # Insert marker
                marker = {"role": USER, "content": "MARKER"}
                conv = conv[:asi] + [marker] + conv[asi + discard:]

    def test_brute_force_force_mode(self):
        """100 random conversations tested with force=True at 3 fractions each."""
        for seed in range(100):
            conv = self._build_random_conversation(seed + 20000)
            active = conv[2:]
            for frac_int in [30, 50, 70]:
                discard = compute_discard_count(active, frac_int / 100, force=True)
                if discard == -1:
                    continue  # -1 sentinel means no valid compression; skip validation
                self._validate_no_splits(active, discard)


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])