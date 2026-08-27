"""Comprehensive tests for the loop detection system (agent_cascade.loop_detection).

Covers:
- Unit tests for detect_exact_loop() core algorithm (basic detection, false positive guards,
  divergence bug fixes from consolidation, pop count accuracy)
- Recovery handler tests for run_agent_in_pool_with_recovery
- Integration tests through ExecutionEngine flow
- Edge cases (empty lists, all-system messages, None content, long conversations)

All tests are self-contained — no LLM or API server required.

LAYOUT NOTE (two-tier redesign, plan §5.1): this file is the PINNED 67-test suite for
the exact (Tier-1) matcher — the "pinned suite = 67 in test_loop_detection.py" invariant
is preserved on purpose; do NOT relocate these tests to a new file. Class names are
grouped by area:

- Tier-1 detector unit tests (detect_exact_loop algorithm):
  TestExactTierBasicDetection, TestExactTierFalsePositiveGuards,
  TestExactTierDivergenceBugs, TestExactTierPopCountAccuracy,
  TestEdgeCases, TestParametrizedPatternLengths, TestFeatureExtraction
- Tier-1 engine integration (rollback path through ExecutionEngine):
  TestExecutionEngineIntegration, TestMaxAutoRollbacksEnforcement
- Legacy-compat (pre-redesign, kept for behavior pins — NOT Tier-1 detector tests):
  TestRecoveryHandler (run_agent_in_pool_with_recovery + LoopDetectedError)

Tier-2 (fuzzy) wiring lives in tests/test_tool_loop_detect.py.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from agent_cascade.exact_loop_detect import detect_exact_loop
from agent_cascade.loop_detection import LoopDetectedError
from agent_cascade.llm.schema import (
    SYSTEM, USER, ASSISTANT, FUNCTION, Message, FunctionCall,
)


# ──────────────────────────────────────────────
# Helpers — message factory utilities
# ──────────────────────────────────────────────

def _msg(role: str, content: str = "", reasoning_content: str = None, function_call=None):
    """Create a Message object for testing."""
    return Message(
        role=role,
        content=content,
        reasoning_content=reasoning_content or None,
        function_call=function_call,
    )


def _dict_msg(role: str, content: str = "", **kwargs):
    """Create a dict-style message for testing (some callers pass dicts)."""
    return {"role": role, "content": content, **kwargs}


# ══════════════════════════════════════════════
# PART 1 — Unit Tests: detect_exact_loop() Core Algorithm
# ══════════════════════════════════════════════

class TestExactTierBasicDetection:
    """Test the core pattern-matching algorithm with crafted message lists."""

    def test_t1_clear_repeating_pattern_length_2(self):
        # Pattern: [USER "hello", ASSISTANT "world"] × 3 = 6 messages
        msgs = [
            _msg(USER, "hello"),
            _msg(ASSISTANT, "world"),
            _msg(USER, "hello"),
            _msg(ASSISTANT, "world"),
            _msg(USER, "hello"),
            _msg(ASSISTANT, "world"),
        ]
        result = detect_exact_loop(msgs)
        assert result is not None, "Should detect repeating pattern of length 2"
        reason, pop_count = result
        assert "repeat" in reason.lower() or "loop" in reason.lower()
        assert pop_count > 0

    def test_t2_no_loop_on_short_conversation(self):
        msgs = [
            _msg(USER, "a"),
            _msg(ASSISTANT, "b"),
            _msg(USER, "c"),
        ]
        assert detect_exact_loop(msgs) is None

    def test_t3_pattern_length_4_repeating_3_times(self):
        # L=4 < 5 → K=3 required: pattern of length 4 × 3 = 12 messages.
        pat = [
            _msg(USER, "q1"),
            _msg(ASSISTANT, "a1"),
            _msg(FUNCTION, "f1"),
            _msg(USER, "q2"),
        ]
        msgs = pat * 3  # 12 messages
        result = detect_exact_loop(msgs)
        assert result is not None, "Should detect L=4 pattern repeated 3 times"

    def test_t3b_pattern_length_5_repeating_twice(self):
        # L≥5 → K=2 suffices.
        pat = [
            _msg(USER, "u1"),
            _msg(ASSISTANT, "a1"),
            _msg(FUNCTION, "f1"),
            _msg(USER, "u2"),
            _msg(ASSISTANT, "a2"),
        ]
        msgs = pat * 2  # 10 messages
        result = detect_exact_loop(msgs)
        assert result is not None, "Should detect L=5 pattern repeated 2 times"

    def test_t4_non_repeating_conversation(self):
        msgs = [
            _msg(USER, f"question_{i}") for i in range(3)
        ] + [
            _msg(ASSISTANT, f"answer_{i}") for i in range(3)
        ]
        assert detect_exact_loop(msgs) is None

    def test_t5_pattern_repeats_only_once(self):
        # L=2 pattern repeated exactly 2 times = 4 messages (too short anyway)
        # Let's do L=2 pattern × 2 + some extras but not reaching K=3 threshold
        msgs = [
            _msg(USER, "hello"),
            _msg(ASSISTANT, "world"),
            _msg(FUNCTION, "tool_result"),
            _msg(USER, "hello"),
            _msg(ASSISTANT, "world"),
        ]
        # Pattern [USER:hello, ASSISTANT:world] appears twice but K=3 needed for L<5
        result = detect_exact_loop(msgs)
        assert result is None, "Pattern repeated only 2× with L<5 should not trigger"

    def test_t5b_pattern_length_1_repeats_twice(self):
        msgs = [
            _msg(ASSISTANT, "same") for _ in range(2)
        ] + [_msg(USER, f"diff_{i}") for i in range(4)]  # pad to ≥6
        result = detect_exact_loop(msgs)
        assert result is None


class TestExactTierFalsePositiveGuards:
    """Test that common non-loop patterns are not flagged."""

    def test_t6_single_function_pattern(self):
        # Parallel tool calls produce consecutive identical function messages.
        msgs = [
            _msg(FUNCTION, "result") for _ in range(8)
        ]
        result = detect_exact_loop(msgs)
        assert result is None, "Single-function pattern should not trigger"

    def test_t7_consecutive_function_only(self):
        msgs = [
            _msg(FUNCTION, f"tool_result_{i % 3}") for i in range(10)
        ]
        result = detect_exact_loop(msgs)
        assert result is None

    def test_t8_identical_user_messages_detected_as_l2_pattern(self):
        # L==1 USER is guarded (single-element patterns skipped), but 8 identical
        # USER messages also match as an [USER,USER] period (L=2 < 5 → K=3 needed).
        msgs = [
            _msg(USER, "same_input") for _ in range(8)
        ]
        result = detect_exact_loop(msgs)
        assert result is not None, "Should detect repeating USER pattern (L>=2)"
        reason, pop_count = result
        assert pop_count > 0

    def test_t8c_l1_assistant_detected_not_guarded(self):
        # L=1 ASSISTANT is NOT guarded (unlike USER/FUNCTION).
        msgs = [_msg(ASSISTANT, "same") for _ in range(8)]
        result = detect_exact_loop(msgs)
        assert result is not None, "L=1 ASSISTANT should be detected"

    def test_t8b_alternating_user_assistant_not_repeating(self):
        msgs = []
        for i in range(6):
            msgs.append(_msg(USER, f"question_{i}"))
            msgs.append(_msg(ASSISTANT, f"answer_{i}"))
        result = detect_exact_loop(msgs)
        assert result is None


class TestExactTierDivergenceBugs:
    """Test bugs that were fixed during the consolidation."""

    def test_t9_truncation_marker_normalization(self):
        # Before consolidation, varying '[TOOL RESPONSE TRUNCATED: N%]' markers were
        # treated as different features.
        # Pattern repeats but with slightly different truncation percentages
        pat1 = [
            _msg(FUNCTION, "data...\n[TOOL RESPONSE TRUNCATED: 50%]"),
            _msg(ASSISTANT, "Processing..."),
        ]
        pat2 = [
            _msg(FUNCTION, "data...\n[TOOL RESPONSE TRUNCATED: 48%]"),
            _msg(ASSISTANT, "Processing..."),
        ]
        pat3 = [
            _msg(FUNCTION, "data...\n[TOOL RESPONSE TRUNCATED: 52%]"),
            _msg(ASSISTANT, "Processing..."),
        ]
        msgs = pat1 + pat2 + pat3  # L=2 pattern × K=3 with varying truncation markers
        result = detect_exact_loop(msgs)
        assert result is not None, "Should match after normalizing truncation markers"

    def test_t10_multimodal_content(self):
        # Messages with list-style multimodal content
        pat = [
            _dict_msg(USER, [{"type": "text", "text": "hello"}, {"type": "image", "url": "x"}]),
            _dict_msg(ASSISTANT, [{"type": "text", "text": "world"}]),
        ]
        msgs = pat * 3  # L=2 × K=3
        result = detect_exact_loop(msgs)
        assert result is not None, "Should detect loops in multimodal content"

    def test_t10b_multimodal_with_message_objects(self):
        # Multimodal via Message objects with ContentItem lists.
        from agent_cascade.llm.schema import ContentItem
        
        text_hello = [ContentItem(text="hello")]
        text_world = [ContentItem(text="world")]
        
        pat = [
            _msg(USER, content=text_hello),
            _msg(ASSISTANT, content=text_world),
        ]
        msgs = pat * 3
        result = detect_exact_loop(msgs)
        assert result is not None

    def test_t11_reasoning_tags(self):
        # Both reasoning and content feed the feature.
        pat = [
            _msg(USER, "solve this"),
            _msg(ASSISTANT, "The answer is 42", reasoning_content="Let me think..."),
        ]
        msgs = pat * 3  # L=2 × K=3
        result = detect_exact_loop(msgs)
        assert result is not None, "Should detect loops with reasoning content"

    def test_t11b_reasoning_and_content_combination(self):
        # </think> tags embedded in content.
        pat = [
            _msg(USER, "question"),
            _msg(ASSISTANT, "<think>reasoning</think>\nanswer"),
        ]
        msgs = pat * 3
        result = detect_exact_loop(msgs)
        assert result is not None

    def test_t11c_reasoning_only_no_content(self):
        # Different reasoning → different features → no loop.
        msgs = [
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="r1"),
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="r2"),
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="r3"),
        ]
        result = detect_exact_loop(msgs)
        # With different reasoning each time, features differ → no loop
        assert result is None


class TestExactTierPopCountAccuracy:
    """Verify that pop_count correctly identifies messages to remove."""

    def test_t12_pop_count_correct_for_known_pattern(self):
        # [A, B] × 3: second rep starts at index 2 → pop from the end back to just
        # after the first occurrence.
        msgs = [
            _msg(USER, "hello"),      # 0
            _msg(ASSISTANT, "world"), # 1
            _msg(USER, "hello"),      # 2 (start of 2nd rep)
            _msg(ASSISTANT, "world"), # 3
            _msg(USER, "hello"),      # 4 (start of 3rd rep)
            _msg(ASSISTANT, "world"), # 5
        ]
        result = detect_exact_loop(msgs)
        assert result is not None
        reason, pop_count = result
        
        # [USER:hello, ASSISTANT:world] × 3 = 6 messages.
        # Pattern starts at index 0, second rep starts at index 2.
        # pop_count should remove from end back to just after first occurrence → 4 msgs popped.
        assert pop_count == 4, f"Expected pop_count=4 for L=2×K=3 pattern, got {pop_count}"
        
        remaining = msgs[:-pop_count] if pop_count > 0 else msgs
        assert len(remaining) >= 2, "Should keep at least the first pattern occurrence"
        # Exactly 2 messages remain: [USER:hello, ASSISTANT:world]
        assert len(msgs) - pop_count == 2, f"Expected exactly 2 remaining after popping {pop_count}"

    def test_t12b_pop_count_respects_window_boundary(self):
        # Non-repeating prefix before the loop.
        prefix = [
            _msg(USER, "start"),
            _msg(ASSISTANT, "ok starting"),
        ]
        pat = [
            _msg(FUNCTION, "result_1"),
            _msg(ASSISTANT, "processing"),
        ]
        msgs = prefix + pat * 3  # prefix + 6 loop messages
        result = detect_exact_loop(msgs)
        assert result is not None
        reason, pop_count = result
        
        # Pattern [FUNCTION:result_1, ASSISTANT:processing] × 3 = 6 loop msgs.
        # Second rep starts at index len(prefix)+2 within the feature list.
        # pop_count should be 4 (remove from end back to just after first pattern occurrence).
        assert pop_count == 4, f"Expected pop_count=4 for L=2×K=3 with prefix, got {pop_count}"
        
        remaining = msgs[:-pop_count] if pop_count > 0 else msgs
        # Should still have prefix + first occurrence of pattern
        assert len(remaining) >= len(prefix) + 2

    def test_t12c_pop_count_with_system_messages(self):
        # Interleaved SYSTEM messages (filtered from features but present in the list).
        pat = [
            _msg(USER, "q"),
            _msg(ASSISTANT, "a"),
        ]
        # Interleave system messages (they get filtered but still count toward window)
        msgs = []
        for i in range(3):
            msgs.append(_msg(SYSTEM, f"system_{i}"))
            msgs.extend(pat)
        
        result = detect_exact_loop(msgs)
        assert result is not None, "Should detect loop with interleaved SYSTEM messages"
        reason, pop_count = result
        assert pop_count > 0, f"pop_count should be positive for detected loop, got {pop_count}"


# ══════════════════════════════════════════════
# PART 2 — Recovery Handler Tests
# ══════════════════════════════════════════════

class TestRecoveryHandler:
    """Test the recovery wrapper at api_integration.py:346-398."""

    def _make_pool(self, instance_name="test_agent"):
        """Create a mock AgentPool with a testable instance."""
        pool = MagicMock()
        inst = MagicMock()
        inst.instance_name = instance_name
        pool.get_instance.return_value = inst
        return pool, inst

    @patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')
    def test_r1_surgical_rollback_targets_specific_agent(self, mock_run):
        # LoopDetectedError with agent_name set → surgical_rollback targets that agent.
        from agent_cascade.api_integration import run_agent_in_pool_with_recovery
        
        pool, inst = self._make_pool("main_agent")
        
        # First call detects loop on a sub-agent named "worker1"
        def run_gen():
            yield [_msg(ASSISTANT, "thinking")]
            raise LoopDetectedError(
                reason="pattern found",
                agent_name="worker1",
                pop_count=4,
            )
        
        # Second call succeeds
        def run_gen_success():
            yield [_msg(ASSISTANT, "done")]
        
        mock_run.side_effect = [run_gen(), run_gen_success()]
        
        results = list(run_agent_in_pool_with_recovery(pool, "main_agent"))
        
        # Verify surgical_rollback was called on "worker1", not "main_agent"
        pool.surgical_rollback.assert_called_once()
        call_args = pool.surgical_rollback.call_args
        assert call_args[0][0] == "worker1", f"Expected rollback on 'worker1', got '{call_args[0][0]}'"
        assert call_args[0][1] == 4

    @patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')
    def test_r2_fallback_to_instance_name(self, mock_run):
        # No agent_name on the error → fallback to instance_name.
        from agent_cascade.api_integration import run_agent_in_pool_with_recovery
        
        pool, inst = self._make_pool("my_agent")
        
        def run_gen():
            yield [_msg(ASSISTANT, "thinking")]
            raise LoopDetectedError(
                reason="pattern found",
                agent_name=None,  # No specific agent — should fallback
                pop_count=3,
            )
        
        def run_gen_success():
            yield [_msg(ASSISTANT, "done")]
        
        mock_run.side_effect = [run_gen(), run_gen_success()]
        
        list(run_agent_in_pool_with_recovery(pool, "my_agent"))
        
        # Should fallback to instance_name "my_agent"
        pool.surgical_rollback.assert_called_once()
        assert pool.surgical_rollback.call_args[0][0] == "my_agent"

    @patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')
    def test_r3_retry_limit_enforcement(self, mock_run):
        # max_auto_retries=2 → 3 attempts total (retry_count 0, 1, 2), then error yield.
        from agent_cascade.api_integration import run_agent_in_pool_with_recovery
        
        pool, inst = self._make_pool("test_agent")
        
        # Always loop — never succeeds
        def run_gen():
            yield [_msg(ASSISTANT, "thinking")]
            raise LoopDetectedError(
                reason="pattern found",
                agent_name="test_agent",
                pop_count=2,
            )
        
        # max_auto_retries=2 means 3 attempts total (retry_count 0, 1, 2)
        mock_run.side_effect = [run_gen(), run_gen(), run_gen()]
        
        results = list(run_agent_in_pool_with_recovery(pool, "test_agent", max_auto_retries=2))
        
        assert len(results) >= 1
        # Last result should contain error message
        last_msgs = results[-1]
        assert any("Loop detected" in (m.content or "") for m in last_msgs), \
            "Should yield error message after exhausting retries"
        assert mock_run.call_count == 3, "Should have attempted exactly 3 calls"

    @patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')
    def test_r4_hint_injection(self, mock_run):
        # Loop-avoidance hint appended (USER role) to the rolled-back instance.
        from agent_cascade.api_integration import run_agent_in_pool_with_recovery
        
        pool, inst = self._make_pool("test_agent")
        
        def run_gen():
            yield [_msg(ASSISTANT, "thinking")]
            raise LoopDetectedError(
                reason="pattern found",
                agent_name="test_agent",
                pop_count=2,
            )
        
        def run_gen_success():
            yield [_msg(ASSISTANT, "done")]
        
        mock_run.side_effect = [run_gen(), run_gen_success()]
        
        list(run_agent_in_pool_with_recovery(pool, "test_agent"))
        
        # Verify hint was injected via append_message
        assert inst.append_message.called, "Hint should be appended to instance"
        hint_msg = inst.append_message.call_args[0][0]
        assert "[SYSTEM]: You appear to be stuck in a loop" in (hint_msg.content or "")
        assert hint_msg.role == USER

    @patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')
    def test_r5_auto_rollback_disabled(self, mock_run):
        # auto_rollback_enabled=False → error yielded, no rollback / hint.
        from agent_cascade.api_integration import run_agent_in_pool_with_recovery
        
        pool, inst = self._make_pool("test_agent")
        
        def run_gen():
            yield [_msg(ASSISTANT, "thinking")]
            raise LoopDetectedError(reason="loop", agent_name="test_agent", pop_count=2)
        
        mock_run.side_effect = [run_gen()]
        
        results = list(run_agent_in_pool_with_recovery(
            pool, "test_agent", auto_rollback_enabled=False))
        
        assert len(results) >= 1
        # Should NOT have called surgical_rollback or injected hint
        assert not pool.surgical_rollback.called, "Should NOT rollback when disabled"
        assert not inst.append_message.called, "Should NOT inject hint when disabled"

    @patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')
    def test_r6_instance_not_found_after_rollback(self, mock_run):
        # get_instance returns None for the looped agent post-rollback → error yield.
        from agent_cascade.api_integration import run_agent_in_pool_with_recovery
        
        pool, inst = self._make_pool("test_agent")
        
        def run_gen():
            yield [_msg(ASSISTANT, "thinking")]
            raise LoopDetectedError(reason="loop", agent_name="worker1", pop_count=2)
        
        mock_run.side_effect = [run_gen()]
        
        # Make pool.get_instance return None for the looped agent after first call
        def get_instance_side_effect(name):
            if name == "test_agent":
                return inst
            return None  # worker1 not found
        
        pool.get_instance.side_effect = get_instance_side_effect
        
        results = list(run_agent_in_pool_with_recovery(pool, "test_agent"))
        
        assert any(
            "Rollback performed but loop recovery failed" in (m.content or "")
            for m in results[-1]
        ), "Should yield error when instance not found after rollback"

    @patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')
    def test_r7_unlimited_retries(self, mock_run):
        # max_auto_retries=-1 → unlimited mode (converted to 999_999 internally).
        from agent_cascade.api_integration import run_agent_in_pool_with_recovery
        
        pool, inst = self._make_pool("test_agent")
        
        call_count_tracker = [0]
        
        def run_gen():
            call_count_tracker[0] += 1
            yield [_msg(ASSISTANT, "thinking")]
            raise LoopDetectedError(reason="loop", agent_name="test_agent", pop_count=2)
        
        # Provide enough generators for unlimited retries (more than default limit of 5)
        mock_run.side_effect = [run_gen() for _ in range(20)]
        
        results = list(run_agent_in_pool_with_recovery(pool, "test_agent", max_auto_retries=-1))
        
        assert len(results) >= 1
        # Should have consumed all provided generators (unlimited retries mode)
        assert call_count_tracker[0] == 20, f"Expected 20 calls in unlimited mode, got {call_count_tracker[0]}"

    @patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')
    def test_r9_non_loop_exception(self, mock_run):
        # Non-loop exception → "SYSTEM ERROR" yield (not the loop-recovery path).
        from agent_cascade.api_integration import run_agent_in_pool_with_recovery
        
        pool, inst = self._make_pool("test_agent")
        
        def run_gen():
            yield [_msg(ASSISTANT, "thinking")]
            raise RuntimeError("LLM timeout")
        
        mock_run.side_effect = [run_gen()]
        
        results = list(run_agent_in_pool_with_recovery(pool, "test_agent"))
        
        assert len(results) >= 1
        last_msgs = results[-1]
        assert any("SYSTEM ERROR" in (m.content or "") for m in last_msgs), \
            "Should yield error message for non-loop exceptions"


# ══════════════════════════════════════════════
# PART 3 — Integration Tests: ExecutionEngine Flow
# ══════════════════════════════════════════════

class TestExecutionEngineIntegration:
    """Test the full flow through ExecutionEngine."""

    def test_i1_main_agent_loop_detection(self):
        # Uses the canonical detect_exact_loop (same function the engine wires in).
        import_func = detect_exact_loop  # same function referenced by execution engine
        
        # Simulate messages accumulating in execution engine
        msgs = []
        for i in range(3):
            msgs.append(_msg(USER, f"step_{i}"))
            msgs.append(_msg(ASSISTANT, f"result_{i}"))
        
        result = import_func(msgs)
        # 6 messages with alternating unique content — no loop yet
        assert result is None
        
        # Now add repeating pattern
        for _ in range(3):
            msgs.append(_msg(USER, "repeat_q"))
            msgs.append(_msg(ASSISTANT, "repeat_a"))
        
        result = import_func(msgs)
        assert result is not None

    def test_i2_compression_cooldown(self):
        # The engine sets _suppress_loop_detection_next_turn=True after compression; verify
        # the flag is respected by patching detect_exact_loop where the engine imports it.
        from agent_cascade.execution_engine import ExecutionEngine
        
        # Build messages with a loop pattern
        msgs = [
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
        ]
        
        # Verify the loop exists in messages
        assert detect_exact_loop(msgs) is not None
        
        # Mock instance with cooldown flag set (simulates post-compression state)
        class FakeInstance:
            pass
        
        inst = FakeInstance()
        inst._suppress_loop_detection_next_turn = True
        
        # Patch detect_exact_loop at the module level where execution_engine imports it.
        # During cooldown, _pre_llm_checks skips calling _canonical_detect_exact_loop entirely.
        with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=("loop", 2)) as mock_detect:
            # Simulate what _pre_llm_checks does (execution_engine.py:1200)
            if not getattr(inst, '_suppress_loop_detection_next_turn', False):
                mock_detect(msgs)
            
            assert mock_detect.call_count == 0, "detect_exact_loop should NOT be called during cooldown"
        
        # Clear the flag — next turn should run detection
        inst._suppress_loop_detection_next_turn = False
        
        with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=("loop", 2)) as mock_detect:
            if not getattr(inst, '_suppress_loop_detection_next_turn', False):
                mock_detect(msgs)
            
            assert mock_detect.call_count == 1, "detect_exact_loop should be called after cooldown clears"

    def test_i3_sub_agent_loop_via_manager_ops(self):
        # manager_ops imports detect_exact_loop from the canonical module; use the same
        # import path to verify compatibility.
        from agent_cascade.exact_loop_detect import detect_exact_loop as _mgr_detect_exact_loop
        
        msgs = []
        for i in range(3):
            msgs.append(_msg(ASSISTANT, f"thinking_{i}"))
            msgs.append(_msg(FUNCTION, f"tool_result_{i}"))
        
        # No loop with unique content
        assert _mgr_detect_exact_loop(msgs) is None
        
        # Add repeating pattern
        for _ in range(3):
            msgs.append(_msg(ASSISTANT, "same_thought"))
            msgs.append(_msg(FUNCTION, "same_result"))
        
        result = _mgr_detect_exact_loop(msgs)
        assert result is not None


# ══════════════════════════════════════════════
# PART 4 — Edge Cases
# ══════════════════════════════════════════════

class TestEdgeCases:
    """Test boundary conditions and unusual inputs."""

    def test_e1_empty_message_list(self):
        assert detect_exact_loop([]) is None

    def test_e2_all_system_messages(self):
        msgs = [_msg(SYSTEM, f"instruction_{i}") for i in range(10)]
        result = detect_exact_loop(msgs)
        assert result is None

    def test_e3_none_content(self):
        # Empty-string messages are identical → they DO form a loop pattern.
        msgs = [
            _msg(USER, ""),  # Empty string (None becomes "" in Message constructor)
            _msg(ASSISTANT, ""),
            _msg(USER, ""),
            _msg(ASSISTANT, ""),
            _msg(USER, ""),
            _msg(ASSISTANT, ""),
        ]
        result = detect_exact_loop(msgs)
        assert result is not None  # Empty strings are identical → loop detected

    def test_e3b_none_content_no_crash_with_dicts(self):
        # Same as E3 but with dict-style messages.
        msgs = [
            {"role": USER, "content": None},
            {"role": ASSISTANT, "content": None},
            {"role": USER, "content": None},
            {"role": ASSISTANT, "content": None},
            {"role": USER, "content": None},
            {"role": ASSISTANT, "content": None},
        ]
        result = detect_exact_loop(msgs)
        assert result is not None

    def test_e4_very_long_conversation(self):
        # 60 unique messages, then a repeating pattern at the tail (within window).
        msgs = [_msg(USER, f"unique_{i}") for i in range(30)] + \
               [_msg(ASSISTANT, f"answer_{i}") for i in range(30)]
        
        # Add loop at the very end (within window)
        for _ in range(3):
            msgs.append(_msg(USER, "loop_q"))
            msgs.append(_msg(ASSISTANT, "loop_a"))
        
        result = detect_exact_loop(msgs)
        assert result is not None

    def test_e4b_window_limit_respected(self):
        """E4 variant: stale loop behind fresh work does not trigger (window-60 boundary).

        REWRITTEN for the two-tier redesign (plan §5.1 — the one intended
        behavior change among the 67 pinned tests): the exact window grew from
        40 to 60 non-system messages, so the old filler count (50) no longer
        pushes a 6-msg pattern out of view. Now: 6-msg loop at the start + 58
        unique fillers = 64 msgs; last 60 = indices 4-63, which contain only
        unique fillers → no loop detected."""
        # Pattern at start + unique filler after
        msgs = [
            _msg(USER, "start_q"), _msg(ASSISTANT, "start_a")
        ] * 3  # L=2 × K=3 loop at beginning (indices 0-5)

        # Add enough unique messages to push the pattern out of the 60-message window
        for i in range(29):
            msgs.append(_msg(USER, f"filler_{i}"))
            msgs.append(_msg(ASSISTANT, f"response_{i}"))

        result = detect_exact_loop(msgs)
        # The initial loop is pushed out of the 60-message window
        assert result is None

    def test_e5_function_call_messages(self):
        # feature = name + args (not content).
        pat = [
            _msg(ASSISTANT, "", function_call=FunctionCall("write_file", '{"path":"x"}')),
            _msg(FUNCTION, "done"),
        ]
        msgs = pat * 3
        result = detect_exact_loop(msgs)
        assert result is not None

    def test_e6_mixed_message_types(self):
        # Mixed Message objects and dicts in one list.
        msgs = [
            _msg(USER, "hello"),
            {"role": ASSISTANT, "content": "world"},
            _msg(USER, "hello"),
            {"role": ASSISTANT, "content": "world"},
            _msg(USER, "hello"),
            {"role": ASSISTANT, "content": "world"},
        ]
        result = detect_exact_loop(msgs)
        assert result is not None

    def test_e7_mixed_role_pattern(self):
        # USER→ASSISTANT→FUNCTION period.
        pat = [
            _msg(USER, "query"),
            _msg(ASSISTANT, "thinking"),
            _msg(FUNCTION, "result"),
        ]
        msgs = pat * 3  # L=3 < 5 → K=3 needed
        result = detect_exact_loop(msgs)
        assert result is not None

    def test_e8_single_pattern_length_1_assistant(self):
        # L=1 ASSISTANT is not guarded → detected.
        msgs = [_msg(ASSISTANT, "same_response") for _ in range(6)]
        result = detect_exact_loop(msgs)
        assert result is not None

    def test_e9_pattern_at_exact_boundary(self):
        # 30 unique prefix + L=5 × K=2 period at the tail.
        prefix = [_msg(USER, f"p{i}") for i in range(30)] + \
                 [_msg(ASSISTANT, f"a{i}") for i in range(10)]
        pat = [
            _msg(FUNCTION, "r"),
            _msg(ASSISTANT, "s"),
            _msg(USER, "t"),
            _msg(FUNCTION, "u"),
            _msg(ASSISTANT, "v"),
        ]
        msgs = prefix + pat * 2  # 40 + 10 = 50 messages
        result = detect_exact_loop(msgs)
        assert result is not None


# ══════════════════════════════════════════════
# PART 6 — Parametrized Tests for Pattern Lengths
# ══════════════════════════════════════════════

class TestParametrizedPatternLengths:
    """Use pytest.parametrize to test various pattern configurations."""

    @pytest.mark.parametrize("pattern_length, repetitions, should_detect", [
        (2, 3, True),   # L=2 < 5 → K=3 needed ✓
        (2, 2, False),  # L=2 < 5 → only 2 reps ✗
        (4, 3, True),   # L=4 < 5 → K=3 needed ✓
        (5, 2, True),   # L=5 ≥ 5 → K=2 needed ✓
        (10, 2, True),  # L=10 ≥ 5 → K=2 needed ✓
        (5, 1, False),  # L=5 needs K=2, only 1 rep ✗
        (4, 2, False),  # L=4 needs K=3, only 2 reps ✗
    ])
    def test_various_pattern_lengths(self, pattern_length, repetitions, should_detect):
        roles_cycle = [USER, ASSISTANT, FUNCTION]
        
        pat = []
        for i in range(pattern_length):
            role = roles_cycle[i % len(roles_cycle)]
            # Avoid all-FUNCTION patterns which are filtered
            if i == 0:
                role = USER
            pat.append(_msg(role, f"content_{i}"))
        
        msgs = []
        for r in range(repetitions):
            msgs.extend(pat)
        
        result = detect_exact_loop(msgs)
        if should_detect:
            assert result is not None, \
                f"L={pattern_length}, K={repetitions}: expected detection"
        else:
            assert result is None, \
                f"L={pattern_length}, K={repetitions}: expected no detection"

    @pytest.mark.parametrize("role", [USER, ASSISTANT, FUNCTION])
    def test_single_role_patterns(self, role):
        # Identical messages also form L=2 patterns; the L==1 guard only applies when
        # the FIRST match is a single-element period. FUNCTION-only sequences are filtered.
        msgs = [_msg(role, "same") for _ in range(6)]
        
        # FUNCTION-only sequences are filtered (all-function guard), so expect no detection
        if role == FUNCTION:
            assert detect_exact_loop(msgs) is None, f"FUNCTION-only pattern should not trigger"
        else:
            result = detect_exact_loop(msgs)
            assert result is not None, f"Should detect repeating {role} pattern (L>=1)"

    @pytest.mark.parametrize("content_type", [
        lambda i: f"text_{i}",
        lambda i: f"data...\n[TOOL RESPONSE TRUNCATED: {i}%]",
        lambda i: "<think>reasoning</think>\nresult",
    ])
    def test_content_variations(self, content_type):
        pat = [
            _msg(USER, "query"),
            _msg(ASSISTANT, content_type(0)),
        ]
        msgs = pat * 3
        result = detect_exact_loop(msgs)
        assert result is not None


# ══════════════════════════════════════════════
# PART 7 — Feature Extraction Tests
# ══════════════════════════════════════════════

class TestFeatureExtraction:
    """Test the internal feature extraction logic indirectly via detect_exact_loop."""

    def test_function_call_feature(self):
        # Same FC (name+args) with differing prose → still loops (content is ignored).
        msgs = [
            _msg(ASSISTANT, "different_content_1", 
                 function_call=FunctionCall("tool_a", '{"arg":"val"}')),
            _msg(FUNCTION, "result"),
            _msg(ASSISTANT, "different_content_2",
                 function_call=FunctionCall("tool_a", '{"arg":"val"}')),
            _msg(FUNCTION, "result"),
            _msg(ASSISTANT, "different_content_3",
                 function_call=FunctionCall("tool_a", '{"arg":"val"}')),
            _msg(FUNCTION, "result"),
        ]
        result = detect_exact_loop(msgs)
        assert result is not None, "Same function call should match regardless of content"

    def test_reasoning_content_feature(self):
        # Different reasoning → different features → no loop.
        msgs = [
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="reason_1"),
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="reason_2"),
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="reason_3"),
        ]
        result = detect_exact_loop(msgs)
        assert result is None, "Different reasoning → different features"

    def test_thought_attribute_fallback(self):
        # Feature extraction falls back to the legacy 'thought' attribute when
        # reasoning_content is absent (older message formats).
        pat = [
            _dict_msg(USER, "solve this"),
            _dict_msg(ASSISTANT, "The answer is 42", thought="Let me think..."),
        ]
        msgs = pat * 3  # L=2 × K=3
        result = detect_exact_loop(msgs)
        assert result is not None, "Should detect loops using 'thought' attribute fallback"

    def test_long_content_truncation(self):
        # Long prose (truncated to 3000 chars in the feature) still matches when identical.
        long_text = "word " * 500  # ~2500 chars per message
        msgs = []
        for i in range(3):
            msgs.append(_msg(USER, "q"))       # Identical USER messages
            msgs.append(_msg(ASSISTANT, long_text))  # Identical ASSISTANT messages
        
        result = detect_exact_loop(msgs)
        assert result is not None, "Should detect pattern with identical long content"


# ══════════════════════════════════════════════
# PART 8 — Max Auto-Rollbacks Enforcement Tests
# ══════════════════════════════════════════════

class TestMaxAutoRollbacksEnforcement:
    """Test max_auto_rollbacks enforcement and auto_rollback_on_loop toggle in _pre_llm_checks."""

    def _make_fake_instance(self, name: str = "test_agent", rollback_count: int = 0):
        """Create a minimal fake AgentInstance for testing _pre_llm_checks.

        Uses a plain class instead of MagicMock to avoid auto-created attributes
        interfering with getattr(instance, '_suppress_loop_detection_next_turn', False).
        """
        class FakeInstance:
            def __init__(self, instance_name, rollback_count):
                self.instance_name = instance_name
                if rollback_count >= 0:
                    self._loop_rollback_count = rollback_count

        return FakeInstance(name, rollback_count)

    def _make_fake_pool(self, max_auto_rollbacks=3, auto_rollback_on_loop=True):
        """Create a minimal mock AgentPool with settings."""
        pool = MagicMock()
        pool.settings = MagicMock()
        pool.settings.max_auto_rollbacks = max_auto_rollbacks
        pool.settings.auto_rollback_on_loop = auto_rollback_on_loop
        return pool

    def _make_engine(self, pool):
        """Create an ExecutionEngine with a mocked pool and dependencies."""
        from agent_cascade.execution_engine import ExecutionEngine

        engine = MagicMock(spec=ExecutionEngine)
        engine.pool = pool
        engine.compression_handler = MagicMock()
        engine.compression_handler.handle_rollback_command.return_value = False
        engine.compression_handler.handle_compress_command.return_value = False

        # Bind real _pre_llm_checks method to the mock
        engine._pre_llm_checks = ExecutionEngine._pre_llm_checks.__get__(engine, ExecutionEngine)
        engine._check_stop_conditions = MagicMock(return_value=False)
        engine._inject_async_messages = MagicMock(return_value=False)
        engine._check_and_trigger_compression = MagicMock(return_value=False)
        engine._telemetry = MagicMock(return_value=None)
        engine._append_and_log = MagicMock()
        engine._inline_rollback_and_hint = MagicMock()

        return engine

    def test_rollback_limit_enforced(self):
        # max=2: rollbacks 1 and 2 proceed; the 3rd (3 > 2) terminates.
        pool = self._make_fake_pool(max_auto_rollbacks=2, auto_rollback_on_loop=True)
        engine = self._make_engine(pool)

        # Build a repeating pattern that detect_exact_loop will flag
        msgs = [
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
        ]

        inst = self._make_fake_instance("test_agent")

        # First loop detection: rollback_count goes to 1, within limit
        turns = [50]
        with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=("repeat", 2)):
            result = engine._pre_llm_checks(inst, msgs, [], [], turns)

        assert result is True, "Should continue loop after rollback"
        assert inst._loop_rollback_count == 1
        engine._inline_rollback_and_hint.assert_called_once()
        pool.terminate_instance.assert_not_called()

        # Second detection: rollback_count goes to 2, still within limit
        engine.reset_mock()
        turns = [50]
        with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=("repeat", 2)):
            result = engine._pre_llm_checks(inst, msgs, [], [], turns)

        assert result is True
        assert inst._loop_rollback_count == 2
        pool.terminate_instance.assert_not_called()

        # Third detection: rollback_count goes to 3, exceeds limit (3 > 2) → terminate
        engine.reset_mock()
        turns = [50]
        with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=("repeat", 2)):
            result = engine._pre_llm_checks(inst, msgs, [], [], turns)

        assert result is True, "Should return True so caller breaks on stop_conditions"
        assert inst._loop_rollback_count == 3
        pool.terminate_instance.assert_called_once()
        call_kwargs = pool.terminate_instance.call_args
        assert call_kwargs.kwargs.get("set_global_stopped") is False

    def test_rollback_limit_unlimited(self):
        # max=-1 (unlimited): 10 consecutive rollbacks, no termination.
        pool = self._make_fake_pool(max_auto_rollbacks=-1, auto_rollback_on_loop=True)
        engine = self._make_engine(pool)

        msgs = [
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
        ]

        inst = self._make_fake_instance("test_agent")

        # Simulate 10 loop detections — none should terminate
        for i in range(10):
            engine.reset_mock()
            turns = [50]
            with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=("repeat", 2)):
                result = engine._pre_llm_checks(inst, msgs, [], [], turns)

            assert result is True, f"Detection {i+1}: should continue loop"
            pool.terminate_instance.assert_not_called(), \
                f"Detection {i+1}: should not terminate with unlimited rollbacks"

        assert inst._loop_rollback_count == 10

    def test_rollback_limit_zero(self):
        # max=0: the FIRST rollback (1 > 0) already terminates.
        pool = self._make_fake_pool(max_auto_rollbacks=0, auto_rollback_on_loop=True)
        engine = self._make_engine(pool)

        msgs = [
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
        ]

        inst = self._make_fake_instance("test_agent")

        turns = [50]
        with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=("repeat", 2)):
            result = engine._pre_llm_checks(inst, msgs, [], [], turns)

        # Rollback is performed (count becomes 1), then limit check: 1 > 0 → terminate
        assert result is True
        assert inst._loop_rollback_count == 1
        engine._inline_rollback_and_hint.assert_called_once()
        pool.terminate_instance.assert_called_once()

    def test_auto_rollback_disabled_no_rollback(self):
        # Toggle off: detection only — no rollback, returns False (proceed to LLM).
        pool = self._make_fake_pool(max_auto_rollbacks=5, auto_rollback_on_loop=False)
        engine = self._make_engine(pool)

        msgs = [
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
        ]

        inst = self._make_fake_instance("test_agent")

        turns = [50]
        with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=("repeat", 2)):
            result = engine._pre_llm_checks(inst, msgs, [], [], turns)

        # Returns False → caller proceeds to LLM call with current context
        assert result is False, "Should proceed to LLM when auto_rollback_on_loop=False"
        engine._inline_rollback_and_hint.assert_not_called()
        pool.terminate_instance.assert_not_called()
        # No turn consumed in _pre_llm_checks; normal decrement at caller applies
        assert turns[0] == 50

    def test_turn_consumed_on_loop_rollback(self):
        pool = self._make_fake_pool(max_auto_rollbacks=5, auto_rollback_on_loop=True)
        engine = self._make_engine(pool)

        msgs = [
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
        ]

        inst = self._make_fake_instance("test_agent")

        turns = [42]
        with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=("repeat", 2)):
            engine._pre_llm_checks(inst, msgs, [], [], turns)

        assert turns[0] == 41, "Turn should be consumed on loop rollback"

    def test_config_handler_accepts_minus_one(self):
        # Only -1 is special-cased (unlimited).
        from agent_cascade.config_handlers import _handle_max_auto_rollbacks

        pool = self._make_fake_pool(max_auto_rollbacks=3)
        ui_cfg = {"max_auto_rollbacks": -1}

        _handle_max_auto_rollbacks(ui_cfg, pool, [])

        assert pool.settings.max_auto_rollbacks == -1, "-1 should be preserved as unlimited"

    def test_config_handler_clamps_other_negatives_to_zero(self):
        # Other negatives (e.g. -5) clamp to 0.
        from agent_cascade.config_handlers import _handle_max_auto_rollbacks

        pool = self._make_fake_pool(max_auto_rollbacks=3)
        ui_cfg = {"max_auto_rollbacks": -5}

        _handle_max_auto_rollbacks(ui_cfg, pool, [])

        assert pool.settings.max_auto_rollbacks == 0, "-5 should clamp to 0 (only -1 is special)"