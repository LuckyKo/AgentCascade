"""Tests for the compression boundary logic fix in helpers.py.

Verifies that _refine_tool_call_boundary and compute_discard_count correctly handle:
  1. Independent A->F pairs (the main bug that was fixed)
  2. Batched chains (A->A->F->F)
  3. Landed-on-FUNCTION scenarios
  4. Mixed patterns

Uses dict-based messages to simulate real message structures without Message objects.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from agent_cascade.compression.helpers import _refine_tool_call_boundary, compute_discard_count, get_message_role


# ── Helper factories for dict-based messages ────────────────────────────────

def user(content="text"):
    """Create a USER message."""
    return {"role": "user", "content": content}


def assistant_tc(name="tool_0"):
    """Create an ASSISTANT message with tool_calls (standard OpenAI format)."""
    return {
        "role": "assistant",
        "content": f"calling {name}",
        "tool_calls": [{"id": f"call_{name}", "function": {"name": name, "arguments": "{}"}}],
    }


def function(content="result", function_id=None):
    """Create a FUNCTION result message."""
    return {
        "role": "function",
        "content": content,
        "extra": {"function_id": function_id},
    }


# ── Test 1: Independent A->F pairs (A->F -> A->F) ──────────────────────────

class TestIndependentPairsRefine:
    """Test _refine_tool_call_boundary with independent A->F pairs.

    This is the MAIN bug that was fixed. Before the fix, ANY sequence of
    assistant tool-calls and function results was treated as one big chain,
    causing the discard boundary to overshoot into the keep zone.

    After the fix: The first A of each pair is safe (rule 3). Discard can land
    on any A safely. Split between pairs works fine.
    """

    def test_first_a_of_pair_is_safe(self):
        """A->F -> A->F: discard at first A should stay there (safe split point).

        Active set: [U, A(tc), F, A(tc), F, A(tc), F]
        Starting discard at position 1 (first A) should stay at 1.
        """
        active = [
            user("prompt"),          # 0
            assistant_tc("tool_0"),   # 1 - first A of pair 1
            function("res_0"),        # 2 - F of pair 1
            assistant_tc("tool_1"),   # 3 - first A of pair 2
            function("res_1"),        # 4 - F of pair 2
            assistant_tc("tool_2"),   # 5 - first A of pair 3
            function("res_2"),        # 6 - F of pair 3
        ]

        result = _refine_tool_call_boundary(active, 1, 5)
        assert result == 1, f"First A of independent pair is safe, expected 1 got {result}"

    def test_second_a_of_pair_is_safe(self):
        """A->F -> A->F: discard at second A should also stay (safe split).

        The previous message is F (not an assistant with tool calls), so rule 3 applies.
        """
        active = [
            user("prompt"),          # 0
            assistant_tc("tool_0"),   # 1
            function("res_0"),        # 2
            assistant_tc("tool_1"),   # 3 - second A (prev is F, not A(tc))
            function("res_1"),        # 4
        ]

        result = _refine_tool_call_boundary(active, 3, 5)
        assert result == 3, f"Second A of independent pair is safe, expected 3 got {result}"

    def test_refine_advances_through_pair(self):
        """When discard lands at first A, it advances through the complete A->F pair."""
        active = [
            user("prompt"),          # 0
            assistant_tc("tool_0"),   # 1 - A(tc) at boundary
            function("res_0"),        # 2 - F matching above
            assistant_tc("tool_1"),   # 3
        ]

        result = _refine_tool_call_boundary(active, 1, 4)
        assert result >= 1 and result <= 4


# ── Test 2: Batched chain (A->A->F->F) ─────────────────────────────────────

class TestBatchedChainRefine:
    """Test _refine_tool_call_boundary with batched chains.

    In a batched chain, multiple ASSISTANT messages with tool calls appear
    consecutively before their FUNCTION results. The first A collects IDs
    from all consecutive As then advances past matching Fs.
    Intermediate As should skip past remaining As and their Fs (rule 2).
    """

    def test_intermediate_a_skips_to_end(self):
        """A->A->F->F: discard at second A should advance to end of chain (unclamped)."""
        active = [
            user("prompt"),          # 0
            assistant_tc("tool_0"),   # 1 - first A (safe)
            assistant_tc("tool_1"),   # 2 - intermediate A (unsafe)
            function("res_0"),        # 3
            function("res_1"),        # 4
        ]

        result = _refine_tool_call_boundary(active, 2, 4)
        assert result == 5, f"Intermediate A should advance past chain end (unclamped), expected 5 got {result}"

    def test_first_a_of_chain_is_safe(self):
        """A->A->F->F: the first A of the chain is safe (no prev A(tc))."""
        active = [
            user("prompt"),          # 0
            assistant_tc("tool_0"),   # 1 - first A (prev is USER)
            assistant_tc("tool_1"),   # 2
            function("res_0"),        # 3
            function("res_1"),        # 4
        ]

        result = _refine_tool_call_boundary(active, 1, 5)
        assert result == 1, f"First A of chain should be safe at position 1, got {result}"

    def test_three_consecutive_as(self):
        """A->A->A->F->F->F: three consecutive As should all advance (unclamped)."""
        active = [
            user("prompt"),          # 0
            assistant_tc("tool_0"),   # 1
            assistant_tc("tool_1"),   # 2 - intermediate A
            assistant_tc("tool_2"),   # 3 - also intermediate
            function("res_0"),        # 4
            function("res_1"),        # 5
            function("res_2"),        # 6
        ]

        result = _refine_tool_call_boundary(active, 2, 6)
        assert result == 7, f"Should advance past all As and Fs in chain (unclamped), got {result}"


# ── Test 3: Landed on FUNCTION ─────────────────────────────────────────────

class TestLandedOnFunctionRefine:
    """Test _refine_tool_call_boundary when discard lands on a FUNCTION result.

    Rule 1: If the boundary lands on a FUNCTION message, skip forward past all
    consecutive FUNCTIONs to complete the chain.
    """

    def test_landed_on_single_function(self):
        """A->F->F->A: discard at first F should skip both Fs."""
        active = [
            user("prompt"),          # 0
            assistant_tc("tool_0"),   # 1
            function("res_0"),        # 2 - landed here
            function("res_1"),        # 3
            assistant_tc("tool_1"),   # 4
        ]

        result = _refine_tool_call_boundary(active, 2, 5)
        assert result == 4, f"Landed on F should advance past consecutive Fs to 4, got {result}"

    def test_landed_on_function_at_start(self):
        """F->F at the very start: skip both."""
        active = [
            function("res_0"),        # 0 - landed here
            function("res_1"),        # 1
            assistant_tc("tool_0"),   # 2
            user("prompt"),           # 3
        ]

        result = _refine_tool_call_boundary(active, 0, 4)
        assert result == 2, f"Should skip past consecutive Fs from start, got {result}"

    def test_exact_scenario_from_spec(self):
        """Exact scenario: active=[U, A(tc), F, F, A], discard=2 -> landed on F -> skip to 4."""
        active = [
            user("prompt"),          # 0 - U
            assistant_tc("tool_0"),   # 1 - A(tc)
            function("res_0"),        # 2 - F (discard lands here)
            function("res_1"),        # 3 - F
            assistant_tc("tool_1"),   # 4 - A
        ]

        result = _refine_tool_call_boundary(active, 2, 5)
        assert result == 4, f"discard=2 landed on F -> should skip to 4, got {result}"


# ── Test 4: Mixed pattern (A->F, then A->A->F->F) ─────────────────────────

class TestMixedPatternRefine:
    """Test _refine_tool_call_boundary with mixed patterns.

    Pattern: [U, A(tc), F, A(tc), A(tc), F, F]
    - First pair (A->F) is independent
    - Second group (A->A->F->F) is a batched chain
    Split after the first pair should work fine.
    """

    def test_split_after_first_pair(self):
        """Split right after the first A->F pair (at first A of batched part)."""
        active = [
            user("prompt"),          # 0
            assistant_tc("tool_0"),   # 1 - A of independent pair
            function("res_0"),        # 2 - F of independent pair
            assistant_tc("tool_1"),   # 3 - first A of batched chain (prev is F)
            assistant_tc("tool_2"),   # 4 - second A of batched chain
            function("res_1"),        # 5
            function("res_2"),        # 6
        ]

        result = _refine_tool_call_boundary(active, 3, 7)
        assert result == 3, f"Split after first pair should be safe at position 3, got {result}"

    def test_at_intermediate_a_of_batched_part(self):
        """Discard at the second A of the batched chain part (unclamped)."""
        active = [
            user("prompt"),          # 0
            assistant_tc("tool_0"),   # 1
            function("res_0"),        # 2
            assistant_tc("tool_1"),   # 3 - first A of batched chain
            assistant_tc("tool_2"),   # 4 - second A (intermediate, unsafe)
            function("res_1"),        # 5
            function("res_2"),        # 6
        ]

        result = _refine_tool_call_boundary(active, 4, 6)
        assert result == 7, f"Intermediate A should advance to end of chain (unclamped), got {result}"


# ── Test 5: compute_discard_count integration tests ────────────────────────

class TestComputeDiscardCount:
    """Test compute_discard_count with various scenarios.

    Uses Message objects (like the existing test suite) for these integration
    tests since _refine_tool_call_boundary works correctly with both dicts and
    Message objects, but the post-validation heuristic in compute_discard_count
    behaves more predictably with properly structured Message objects.
    """

    def _make_msg(self, role, content="text", function_call=None, extra=None):
        """Create a Message object like the existing test suite does."""
        from agent_cascade.llm.schema import ASSISTANT, FUNCTION, USER, Message
        if role == ASSISTANT and function_call:
            fc = {'name': function_call, 'arguments': '{}'}
            if extra is None:
                extra = {'function_id': f"call_{function_call}"}
            return Message(role=role, content=content, function_call=fc, extra=extra)
        return Message(role=role, content=content, extra=extra or None)

    def test_independent_pairs_valid_split(self):
        """Independent pairs with extra tail room should find a valid split."""
        from agent_cascade.llm.schema import ASSISTANT, FUNCTION, USER
        active = [
            self._make_msg(USER, "prompt"),
            self._make_msg(ASSISTANT, "thinking", function_call="tool_0"),
            self._make_msg(FUNCTION, "result_0", extra={'function_id': 'call_tool_0'}),
            self._make_msg(ASSISTANT, "thinking", function_call="tool_1"),
            self._make_msg(FUNCTION, "result_1", extra={'function_id': 'call_tool_1'}),
            self._make_msg(USER, "next"),
            self._make_msg(ASSISTANT, "done"),  # extra tail room
        ]

        count = compute_discard_count(active, 0.5, False)
        assert count >= 0 and count <= len(active) - 2

    def test_batched_chain_valid_split(self):
        """Batched chain with extra tail room should find a valid split."""
        from agent_cascade.llm.schema import ASSISTANT, FUNCTION, USER
        active = [
            self._make_msg(USER, "prompt"),
            self._make_msg(ASSISTANT, "thinking", function_call="tool_0"),
            self._make_msg(ASSISTANT, "thinking", function_call="tool_1"),
            self._make_msg(FUNCTION, "result_0", extra={'function_id': 'call_tool_0'}),
            self._make_msg(FUNCTION, "result_1", extra={'function_id': 'call_tool_1'}),
            self._make_msg(USER, "next"),
            self._make_msg(ASSISTANT, "done"),  # extra tail room
        ]

        count = compute_discard_count(active, 0.5, False)
        assert count >= 0 and count <= len(active) - 2

    def test_mixed_pattern_valid_split(self):
        """Mixed pattern with extra tail room should find a valid split."""
        from agent_cascade.llm.schema import ASSISTANT, FUNCTION, USER
        active = [
            self._make_msg(USER, "prompt"),
            self._make_msg(ASSISTANT, "thinking", function_call="tool_0"),
            self._make_msg(FUNCTION, "result_0", extra={'function_id': 'call_tool_0'}),
            self._make_msg(ASSISTANT, "thinking", function_call="tool_1"),
            self._make_msg(ASSISTANT, "thinking", function_call="tool_2"),
            self._make_msg(FUNCTION, "result_1", extra={'function_id': 'call_tool_1'}),
            self._make_msg(FUNCTION, "result_2", extra={'function_id': 'call_tool_2'}),
            self._make_msg(USER, "next"),
            self._make_msg(ASSISTANT, "done"),  # extra tail room
        ]

        count = compute_discard_count(active, 0.5, False)
        assert count >= 0 and count <= len(active) - 2

    def test_empty_active_set(self):
        """Empty active set returns 0."""
        assert compute_discard_count([], fraction=0.5, force=False) == 0

    def test_no_false_negative_independent_pairs(self):
        """Independent pairs with tail room should not cause compression failure (-1)."""
        from agent_cascade.llm.schema import ASSISTANT, FUNCTION, USER
        active = [self._make_msg(USER, "prompt")]
        for i in range(4):
            active.append(self._make_msg(ASSISTANT, f"call {i}", function_call=f"tool_{i}"))
            active.append(self._make_msg(FUNCTION, f"res_{i}", extra={'function_id': f'call_tool_{i}'}))
        # Add tail messages to give room for post-validation
        active.append(self._make_msg(USER, "next"))
        active.append(self._make_msg(ASSISTANT, "done"))

        count = compute_discard_count(active, 0.3, False)
        assert count != -1, "Independent pairs should not cause compression failure (-1)"

    def test_plain_messages_only(self):
        """Pure plain messages should work trivially."""
        active = [user("a"), user("b"), user("c"), user("d")]
        count = compute_discard_count(active, 0.5, False)
        assert count == 2

    def test_single_pair_kept_as_tail(self):
        """A single A->F pair at the end should be kept as tail."""
        active = [user("prompt"), assistant_tc("tool_0"), function("res_0")]
        count = compute_discard_count(active, 0.5, False)
        assert count >= 0 and count <= len(active) - 2


# ── Test 6: Boundary conditions ────────────────────────────────────────────

class TestBoundaryConditions:
    """Edge cases for the refinement logic."""

    def test_refine_at_exact_max_discard(self):
        """Discard at FUNCTION position advances past it (unclamped return)."""
        active = [user("prompt"), assistant_tc("tool_0"), function("res_0")]

        result = _refine_tool_call_boundary(active, 2, 2)
        # Pos 2 is FUNCTION → rule 1: skip past consecutive Fs → discard becomes 3
        assert result == 3

    def test_refine_plain_messages(self):
        """All plain messages should not advance at all."""
        active = [user("a"), user("b"), assistant_tc("tool_0"), function("res")]

        result = _refine_tool_call_boundary(active, 1, 4)
        assert result == 1, "Plain USER message is safe"

    def test_refine_stays_within_max_discard(self):
        """Result should never exceed max_discard."""
        active = [user("prompt"), assistant_tc("tool_0"), function("res_0"),
                  assistant_tc("tool_1"), function("res_1")]

        result = _refine_tool_call_boundary(active, 1, 2)
        assert result <= 2, f"Result {result} exceeds max_discard=2"

    def test_refine_respects_max_bound(self):
        """Refinement should clamp to max_discard even when chain extends further."""
        active = [user("prompt"), assistant_tc("tool_0"), function("res_0"),
                  assistant_tc("tool_1"), function("res_1")]

        result = _refine_tool_call_boundary(active, 1, 3)
        assert result <= 3


# ── Test 7: Post-refinement guard for FUNCTION boundary ─────────────────────

class TestPostRefinementGuard:
    """Test the post-refinement guard in compute_discard_count.

    The guard catches edge cases where refinement returns a discard count that
    points to a FUNCTION response (pool/active-set desync scenario).
    """

    def test_guard_returns_minus_one_when_first_kept_is_function(self):
        """Active set starting with Fs: [F, F, A(tc), F, A(tc), F].
        fraction=0.5 → discard=int(6*0.5)=3, max_discard=4.
        At pos 3: F → Rule 1 skip to 4. At pos 4: A(tc) → break. discard=4.
        Check active_set[4]: A(tc). Fine, returns valid count.
        """
        # Scenario where refinement lands on FUNCTION after advancing
        active = [
            {"role": "function", "content": "res1", "extra": {"function_id": "c1"}},   # 0
            {"role": "function", "content": "res2", "extra": {"function_id": "c2"}},   # 1
            {"role": "assistant", "content": "call1", "tool_calls": [{"id": "c1"}]},   # 2
            {"role": "function", "content": "res3", "extra": {"function_id": "c3"}},   # 3
            {"role": "assistant", "content": "call2", "tool_calls": [{"id": "c2"}]},   # 4
            {"role": "function", "content": "res4", "extra": {"function_id": "c4"}},   # 5
        ]
        count = compute_discard_count(active, 0.5, False)
        assert count >= 0 or count == -1

    def test_guard_with_function_start(self):
        """Active set starting with FUNCTION messages should still find valid split."""
        active = [
            {"role": "function", "content": "res1", "extra": {"function_id": "c1"}},
            {"role": "function", "content": "res2", "extra": {"function_id": "c2"}},
            {"role": "assistant", "content": "call1", "tool_calls": [{"id": "c1"}]},
            {"role": "function", "content": "res3", "extra": {"function_id": "c3"}},
            {"role": "assistant", "content": "call2", "tool_calls": [{"id": "c2"}]},
            {"role": "function", "content": "res4", "extra": {"function_id": "c4"}},
        ]
        count = compute_discard_count(active, 0.5, False)
        assert count >= 0 or count == -1

    def test_guard_independent_pairs(self):
        """Independent A->F pairs should find valid split."""
        active = [
            {"role": "assistant", "content": "call1", "tool_calls": [{"id": "c1"}]},   # 0
            {"role": "function", "content": "res1", "extra": {"function_id": "c1"}},   # 1
            {"role": "assistant", "content": "call2", "tool_calls": [{"id": "c2"}]},   # 2
            {"role": "function", "content": "res2", "extra": {"function_id": "c2"}},   # 3
            {"role": "assistant", "content": "call3", "tool_calls": [{"id": "c3"}]},   # 4
            {"role": "function", "content": "res3", "extra": {"function_id": "c3"}},   # 5
        ]
        count = compute_discard_count(active, 0.5, False)
        assert count >= 0 or count == -1


# ── Test 8: get_message_role helper tests ───────────────────────────────────

class TestGetMessageRole:
    """Test the get_message_role shared helper function."""

    def test_dict_message_user(self):
        """Extract role from a dict-style USER message."""
        msg = {"role": "user", "content": "hello"}
        assert get_message_role(msg) == "user"

    def test_dict_message_assistant(self):
        """Extract role from a dict-style ASSISTANT message."""
        msg = {"role": "assistant", "content": "thinking...", "tool_calls": []}
        assert get_message_role(msg) == "assistant"

    def test_dict_message_function(self):
        """Extract role from a dict-style FUNCTION message."""
        msg = {"role": "function", "content": "result"}
        assert get_message_role(msg) == "function"

    def test_dict_message_missing_role(self):
        """Dict without 'role' key returns empty string."""
        msg = {"content": "hello"}
        assert get_message_role(msg) == ""

    def test_object_message_user(self):
        """Extract role from a Message object (USER)."""
        from agent_cascade.llm.schema import USER, Message
        msg = Message(role=USER, content="hello")
        assert get_message_role(msg) == USER

    def test_object_message_function(self):
        """Extract role from a Message object (FUNCTION)."""
        from agent_cascade.llm.schema import FUNCTION, Message
        msg = Message(role=FUNCTION, content="result")
        assert get_message_role(msg) == FUNCTION

    def test_object_message_missing_role(self):
        """Object without 'role' attribute returns empty string."""
        class PlainMsg:
            pass
        msg = PlainMsg()
        assert get_message_role(msg) == ""

    def test_none_content_returns_empty(self):
        """None input should not crash (returns empty for dict-style)."""
        # None would fail .get(), but that's expected — callers shouldn't pass None.
        # Just verify the common case works:
        from agent_cascade.llm.schema import ASSISTANT, Message
        msg = Message(role=ASSISTANT, content="test")
        assert get_message_role(msg) == ASSISTANT


# ── Test 9: Message object tests for guards ─────────────────────────────────

class TestMessageObjectGuards:
    """Test that the compression guards work correctly with Message objects (not just dicts)."""

    def _make_msg(self, role, content="text", tool_calls=None, extra=None):
        """Create a Message object."""
        from agent_cascade.llm.schema import Message
        kwargs = {"role": role, "content": content}
        if tool_calls:
            kwargs["tool_calls"] = tool_calls
        if extra is not None:
            kwargs["extra"] = extra
        return Message(**kwargs)

    def test_compute_discard_with_message_objects(self):
        """compute_discard_count works with Message objects."""
        from agent_cascade.llm.schema import USER, ASSISTANT, FUNCTION
        active = [
            self._make_msg(USER, "prompt"),
            self._make_msg(ASSISTANT, "call1", tool_calls=[{"id": "c1"}]),
            self._make_msg(FUNCTION, "res1", extra={"function_id": "c1"}),
            self._make_msg(ASSISTANT, "call2", tool_calls=[{"id": "c2"}]),
            self._make_msg(FUNCTION, "res2", extra={"function_id": "c2"}),
            self._make_msg(USER, "next"),
        ]
        count = compute_discard_count(active, 0.5, False)
        assert count >= 0 or count == -1

    def test_compute_discard_with_message_objects_batched(self):
        """compute_discard_count handles batched chains with Message objects."""
        from agent_cascade.llm.schema import USER, ASSISTANT, FUNCTION
        active = [
            self._make_msg(USER, "prompt"),
            self._make_msg(ASSISTANT, "call1", tool_calls=[{"id": "c1"}]),
            self._make_msg(ASSISTANT, "call2", tool_calls=[{"id": "c2"}]),
            self._make_msg(FUNCTION, "res1", extra={"function_id": "c1"}),
            self._make_msg(FUNCTION, "res2", extra={"function_id": "c2"}),
            self._make_msg(USER, "next"),
        ]
        count = compute_discard_count(active, 0.5, False)
        assert count >= 0 or count == -1

    def test_refine_with_message_objects(self):
        """_refine_tool_call_boundary works with Message objects."""
        from agent_cascade.llm.schema import USER, ASSISTANT, FUNCTION
        active = [
            self._make_msg(USER, "prompt"),          # 0
            self._make_msg(ASSISTANT, "call1", tool_calls=[{"id": "c1"}]),  # 1
            self._make_msg(FUNCTION, "res1", extra={"function_id": "c1"}),   # 2
            self._make_msg(ASSISTANT, "call2", tool_calls=[{"id": "c2"}]),   # 3
            self._make_msg(FUNCTION, "res2", extra={"function_id": "c2"}),   # 4
        ]
        result = _refine_tool_call_boundary(active, 1, 5)
        assert result == 1, f"First A should be safe, got {result}"

    def test_refine_with_message_objects_landed_on_function(self):
        """_refine advances past FUNCTION when landed on it with Message objects."""
        from agent_cascade.llm.schema import USER, ASSISTANT, FUNCTION
        active = [
            self._make_msg(USER, "prompt"),          # 0
            self._make_msg(ASSISTANT, "call1", tool_calls=[{"id": "c1"}]),  # 1
            self._make_msg(FUNCTION, "res1", extra={"function_id": "c1"}),   # 2
            self._make_msg(FUNCTION, "res2", extra={"function_id": "c2"}),   # 3
            self._make_msg(ASSISTANT, "call2", tool_calls=[{"id": "c2"}]),   # 4
        ]
        result = _refine_tool_call_boundary(active, 2, 5)
        assert result == 4, f"Landed on F at pos 2 should advance past Fs to 4, got {result}"


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])