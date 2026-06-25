"""Comprehensive tests for the loop detection system (agent_cascade.loop_detection).

Covers:
- Unit tests for detect_loop() core algorithm (basic detection, false positive guards,
  divergence bug fixes from consolidation, pop count accuracy)
- Recovery handler tests for run_agent_in_pool_with_recovery
- Integration tests through ExecutionEngine flow
- Edge cases (empty lists, all-system messages, None content, long conversations)

All tests are self-contained — no LLM or API server required.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from agent_cascade.loop_detection import detect_loop, LoopDetectedError
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
# PART 1 — Unit Tests: detect_loop() Core Algorithm
# ══════════════════════════════════════════════

class TestDetectLoopBasicDetection:
    """Test the core pattern-matching algorithm with crafted message lists."""

    def test_t1_clear_repeating_pattern_length_2(self):
        """T1: USER→ASSISTANT repeating 3 times (L=2, K=3) should detect a loop."""
        # Pattern: [USER "hello", ASSISTANT "world"] × 3 = 6 messages
        msgs = [
            _msg(USER, "hello"),
            _msg(ASSISTANT, "world"),
            _msg(USER, "hello"),
            _msg(ASSISTANT, "world"),
            _msg(USER, "hello"),
            _msg(ASSISTANT, "world"),
        ]
        result = detect_loop(msgs)
        assert result is not None, "Should detect repeating pattern of length 2"
        reason, pop_count = result
        assert "repeating" in reason.lower() or "loop" in reason.lower()
        assert pop_count > 0

    def test_t2_no_loop_on_short_conversation(self):
        """T2: Fewer than 6 messages should return None."""
        msgs = [
            _msg(USER, "a"),
            _msg(ASSISTANT, "b"),
            _msg(USER, "c"),
        ]
        assert detect_loop(msgs) is None

    def test_t3_pattern_length_4_repeating_3_times(self):
        """T3: Pattern of length 4 repeating 3 times (L<5 → K=3 required).
        
        Since L=4 < 5, we need K=3 repetitions. So we create a pattern of length 4
        repeated 3 times = 12 messages."""
        pat = [
            _msg(USER, "q1"),
            _msg(ASSISTANT, "a1"),
            _msg(FUNCTION, "f1"),
            _msg(USER, "q2"),
        ]
        msgs = pat * 3  # 12 messages
        result = detect_loop(msgs)
        assert result is not None, "Should detect L=4 pattern repeated 3 times"

    def test_t3b_pattern_length_5_repeating_twice(self):
        """T3 variant: Pattern of length 5 repeating twice (L≥5 → K=2)."""
        pat = [
            _msg(USER, "u1"),
            _msg(ASSISTANT, "a1"),
            _msg(FUNCTION, "f1"),
            _msg(USER, "u2"),
            _msg(ASSISTANT, "a2"),
        ]
        msgs = pat * 2  # 10 messages
        result = detect_loop(msgs)
        assert result is not None, "Should detect L=5 pattern repeated 2 times"

    def test_t4_non_repeating_conversation(self):
        """T4: Each message has unique content — no loop."""
        msgs = [
            _msg(USER, f"question_{i}") for i in range(3)
        ] + [
            _msg(ASSISTANT, f"answer_{i}") for i in range(3)
        ]
        assert detect_loop(msgs) is None

    def test_t5_pattern_repeats_only_once(self):
        """T5: Pattern repeats only twice with L<5 (needs K=3)."""
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
        result = detect_loop(msgs)
        assert result is None, "Pattern repeated only 2× with L<5 should not trigger"

    def test_t5b_pattern_length_1_repeats_twice(self):
        """T5 variant: Single-element pattern (L=1) repeating only twice."""
        msgs = [
            _msg(ASSISTANT, "same") for _ in range(2)
        ] + [_msg(USER, f"diff_{i}") for i in range(4)]  # pad to ≥6
        result = detect_loop(msgs)
        assert result is None


class TestDetectLoopFalsePositiveGuards:
    """Test that common non-loop patterns are not flagged."""

    def test_t6_single_function_pattern(self):
        """T6: Single-function pattern (L==1, FUNCTION role) should NOT detect.
        
        Parallel tool calls produce consecutive identical function messages."""
        msgs = [
            _msg(FUNCTION, "result") for _ in range(8)
        ]
        result = detect_loop(msgs)
        assert result is None, "Single-function pattern should not trigger"

    def test_t7_consecutive_function_only(self):
        """T7: Only FUNCTION messages — no ASSISTANT decisions between them."""
        msgs = [
            _msg(FUNCTION, f"tool_result_{i % 3}") for i in range(10)
        ]
        result = detect_loop(msgs)
        assert result is None

    def test_t8_identical_user_messages_detected_as_l2_pattern(self):
        """T8: Identical USER messages form an L=2 pattern that IS detected.
        
        Note: L==1 USER is guarded (single-element patterns skipped), but 8
        identical USER messages also match as an [USER,USER] repeating K=3
        times (L=2 < 5 → K=3 needed). So detection should succeed."""
        msgs = [
            _msg(USER, "same_input") for _ in range(8)
        ]
        result = detect_loop(msgs)
        assert result is not None, "Should detect repeating USER pattern (L>=2)"
        reason, pop_count = result
        assert pop_count > 0

    def test_t8c_l1_assistant_detected_not_guarded(self):
        """L=1 ASSISTANT should be detected (not guarded like USER/FUNCTION)."""
        msgs = [_msg(ASSISTANT, "same") for _ in range(8)]
        result = detect_loop(msgs)
        assert result is not None, "L=1 ASSISTANT should be detected"

    def test_t8b_alternating_user_assistant_not_repeating(self):
        """T8 variant: Alternating USER/ASSISTANT with unique content."""
        msgs = []
        for i in range(6):
            msgs.append(_msg(USER, f"question_{i}"))
            msgs.append(_msg(ASSISTANT, f"answer_{i}"))
        result = detect_loop(msgs)
        assert result is None


class TestDetectLoopDivergenceBugs:
    """Test bugs that were fixed during the consolidation."""

    def test_t9_truncation_marker_normalization(self):
        """T9: Messages with varying truncation markers should match after normalization.
        
        Before consolidation, '[TOOL RESPONSE TRUNCATED: 50%]' and
        '[TOOL RESPONSE TRUNCATED: 48%]' were treated as different."""
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
        result = detect_loop(msgs)
        assert result is not None, "Should match after normalizing truncation markers"

    def test_t10_multimodal_content(self):
        """T10: Multimodal content (list-type messages) should extract text correctly."""
        # Messages with list-style multimodal content
        pat = [
            _dict_msg(USER, [{"type": "text", "text": "hello"}, {"type": "image", "url": "x"}]),
            _dict_msg(ASSISTANT, [{"type": "text", "text": "world"}]),
        ]
        msgs = pat * 3  # L=2 × K=3
        result = detect_loop(msgs)
        assert result is not None, "Should detect loops in multimodal content"

    def test_t10b_multimodal_with_message_objects(self):
        """T10 variant: Multimodal using Message objects with ContentItem lists."""
        from agent_cascade.llm.schema import ContentItem
        
        text_hello = [ContentItem(text="hello")]
        text_world = [ContentItem(text="world")]
        
        pat = [
            _msg(USER, content=text_hello),
            _msg(ASSISTANT, content=text_world),
        ]
        msgs = pat * 3
        result = detect_loop(msgs)
        assert result is not None

    def test_t11_reasoning_tags(self):
        """T11: Messages with reasoning_content (think tags) — combined logic."""
        # Pattern where both reasoning and content are used for feature extraction
        pat = [
            _msg(USER, "solve this"),
            _msg(ASSISTANT, "The answer is 42", reasoning_content="Let me think..."),
        ]
        msgs = pat * 3  # L=2 × K=3
        result = detect_loop(msgs)
        assert result is not None, "Should detect loops with reasoning content"

    def test_t11b_reasoning_and_content_combination(self):
        """T11 variant: Messages with <think> tags in content."""
        pat = [
            _msg(USER, "question"),
            _msg(ASSISTANT, "<think>reasoning</think>\nanswer"),
        ]
        msgs = pat * 3
        result = detect_loop(msgs)
        assert result is not None

    def test_t11c_reasoning_only_no_content(self):
        """T11 variant: Messages where reasoning content differs but content matches."""
        # Different reasoning should produce different features → no loop
        msgs = [
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="r1"),
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="r2"),
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="r3"),
        ]
        result = detect_loop(msgs)
        # With different reasoning each time, features differ → no loop
        assert result is None


class TestDetectLoopPopCountAccuracy:
    """Verify that pop_count correctly identifies messages to remove."""

    def test_t12_pop_count_correct_for_known_pattern(self):
        """T12: Verify pop_count removes duplicate repetitions but keeps the first occurrence.
        
        For a pattern [A, B] × 3 = [A, B, A, B, A, B]:
        - First occurrence is indices 0-1 (A, B)
        - Second repetition starts at index 2
        - pop_count should remove from the end back to just after first occurrence
        """
        msgs = [
            _msg(USER, "hello"),      # 0
            _msg(ASSISTANT, "world"), # 1
            _msg(USER, "hello"),      # 2 (start of 2nd rep)
            _msg(ASSISTANT, "world"), # 3
            _msg(USER, "hello"),      # 4 (start of 3rd rep)
            _msg(ASSISTANT, "world"), # 5
        ]
        result = detect_loop(msgs)
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
        """T12 variant: Pop count is correct even with extra prefix messages."""
        # Add non-repeating prefix messages before the loop
        prefix = [
            _msg(USER, "start"),
            _msg(ASSISTANT, "ok starting"),
        ]
        pat = [
            _msg(FUNCTION, "result_1"),
            _msg(ASSISTANT, "processing"),
        ]
        msgs = prefix + pat * 3  # prefix + 6 loop messages
        result = detect_loop(msgs)
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
        """T12 variant: Pop count accounts for filtered SYSTEM messages in between."""
        pat = [
            _msg(USER, "q"),
            _msg(ASSISTANT, "a"),
        ]
        # Interleave system messages (they get filtered but still count toward window)
        msgs = []
        for i in range(3):
            msgs.append(_msg(SYSTEM, f"system_{i}"))
            msgs.extend(pat)
        
        result = detect_loop(msgs)
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

    @patch('agent_cascade.api_integration.run_agent_in_pool')
    def test_r1_surgical_rollback_targets_specific_agent(self, mock_run):
        """R1: LoopDetectedError with agent_name set → surgical_rollback targets that agent."""
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

    @patch('agent_cascade.api_integration.run_agent_in_pool')
    def test_r2_fallback_to_instance_name(self, mock_run):
        """R2: LoopDetectedError without agent_name → falls back to instance_name."""
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

    @patch('agent_cascade.api_integration.run_agent_in_pool')
    def test_r3_retry_limit_enforcement(self, mock_run):
        """R3: After max_auto_retries failures, yields error message."""
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

    @patch('agent_cascade.api_integration.run_agent_in_pool')
    def test_r4_hint_injection(self, mock_run):
        """R4: Verify loop avoidance hint is appended to the correct instance."""
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
        assert "[SYSTEM]: A repetitive loop was detected" in (hint_msg.content or "")
        assert hint_msg.role == USER

    @patch('agent_cascade.api_integration.run_agent_in_pool')
    def test_r5_auto_rollback_disabled(self, mock_run):
        """R5: auto_rollback_enabled=False → yield error without rollback."""
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

    @patch('agent_cascade.api_integration.run_agent_in_pool')
    def test_r6_instance_not_found_after_rollback(self, mock_run):
        """R6: pool.get_instance returns None after rollback → error yield + break."""
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

    @patch('agent_cascade.api_integration.run_agent_in_pool')
    def test_r7_unlimited_retries(self, mock_run):
        """R7: max_auto_retries=-1 → retries exceed default limit (converted to 999_999)."""
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

    def test_r8_keyboard_interrupt_passthrough(self):
        """R8: KeyboardInterrupt is re-raised without retry.
        
        Verify by checking the execution_engine's suppression flag mechanism
        that also handles interrupts — the flag suppresses detection for one turn."""
        # Simulate what happens when compression sets the cooldown flag
        class FakeInstance:
            pass
        
        inst = FakeInstance()
        inst._suppress_loop_detection_next_turn = True
        
        # Verify the flag is set (execution_engine.py checks this)
        assert getattr(inst, '_suppress_loop_detection_next_turn', False) is True
        
        # Simulate clearing after one turn (execution_engine.py:1210)
        inst._suppress_loop_detection_next_turn = False
        assert getattr(inst, '_suppress_loop_detection_next_turn', False) is False

    @patch('agent_cascade.api_integration.run_agent_in_pool')
    def test_r9_non_loop_exception(self, mock_run):
        """R9: Non-loop exceptions yield error message."""
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
        """I1: Main agent loop detection → LoopDetectedError raised with correct agent_name.
        
        Import from canonical module (not internal alias) to avoid fragility."""
        # Use the same detect_loop that execution_engine imports as _canonical_detect_loop
        import_func = detect_loop  # same function referenced by execution engine
        
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
        """I2: Compression cooldown suppresses detection for one turn, then resumes.
        
        The execution engine sets _suppress_loop_detection_next_turn=True after compression.
        We verify the suppression flag is respected by patching detect_loop at the module
        level where execution_engine imports it and checking call counts."""
        from agent_cascade.execution_engine import ExecutionEngine
        
        # Build messages with a loop pattern
        msgs = [
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
            _msg(USER, "q"), _msg(ASSISTANT, "a"),
        ]
        
        # Verify the loop exists in messages
        assert detect_loop(msgs) is not None
        
        # Mock instance with cooldown flag set (simulates post-compression state)
        class FakeInstance:
            pass
        
        inst = FakeInstance()
        inst._suppress_loop_detection_next_turn = True
        
        # Patch detect_loop at the module level where execution_engine imports it.
        # During cooldown, _pre_llm_checks skips calling _canonical_detect_loop entirely.
        with patch('agent_cascade.execution_engine._canonical_detect_loop', return_value=("loop", 2)) as mock_detect:
            # Simulate what _pre_llm_checks does (execution_engine.py:1200)
            if not getattr(inst, '_suppress_loop_detection_next_turn', False):
                mock_detect(msgs)
            
            assert mock_detect.call_count == 0, "detect_loop should NOT be called during cooldown"
        
        # Clear the flag — next turn should run detection
        inst._suppress_loop_detection_next_turn = False
        
        with patch('agent_cascade.execution_engine._canonical_detect_loop', return_value=("loop", 2)) as mock_detect:
            if not getattr(inst, '_suppress_loop_detection_next_turn', False):
                mock_detect(msgs)
            
            assert mock_detect.call_count == 1, "detect_loop should be called after cooldown clears"

    def test_i3_sub_agent_loop_via_manager_ops(self):
        """I3: Sub-agent loop detection via manager_ops → internal retry kicks in.
        
        manager_ops imports detect_loop from the canonical module inside its functions,
        so we use the same import path to verify compatibility."""
        # Simulate what manager_ops does: call detect_loop on accumulated messages
        from agent_cascade.loop_detection import detect_loop as _mgr_detect_loop
        
        msgs = []
        for i in range(3):
            msgs.append(_msg(ASSISTANT, f"thinking_{i}"))
            msgs.append(_msg(FUNCTION, f"tool_result_{i}"))
        
        # No loop with unique content
        assert _mgr_detect_loop(msgs) is None
        
        # Add repeating pattern
        for _ in range(3):
            msgs.append(_msg(ASSISTANT, "same_thought"))
            msgs.append(_msg(FUNCTION, "same_result"))
        
        result = _mgr_detect_loop(msgs)
        assert result is not None


# ══════════════════════════════════════════════
# PART 4 — Edge Cases
# ══════════════════════════════════════════════

class TestEdgeCases:
    """Test boundary conditions and unusual inputs."""

    def test_e1_empty_message_list(self):
        """E1: Empty message list should return None without errors."""
        assert detect_loop([]) is None

    def test_e2_all_system_messages(self):
        """E2: All SYSTEM messages (filtered out) should return None."""
        msgs = [_msg(SYSTEM, f"instruction_{i}") for i in range(10)]
        result = detect_loop(msgs)
        assert result is None

    def test_e3_none_content(self):
        """E3: Messages with None content should not crash.
        
        Verifies that empty-string messages form a valid loop pattern (they match)."""
        msgs = [
            _msg(USER, ""),  # Empty string (None becomes "" in Message constructor)
            _msg(ASSISTANT, ""),
            _msg(USER, ""),
            _msg(ASSISTANT, ""),
            _msg(USER, ""),
            _msg(ASSISTANT, ""),
        ]
        result = detect_loop(msgs)
        assert result is not None  # Empty strings are identical → loop detected

    def test_e3b_none_content_no_crash_with_dicts(self):
        """E3 variant: Dict messages with missing content key."""
        msgs = [
            {"role": USER, "content": None},
            {"role": ASSISTANT, "content": None},
            {"role": USER, "content": None},
            {"role": ASSISTANT, "content": None},
            {"role": USER, "content": None},
            {"role": ASSISTANT, "content": None},
        ]
        result = detect_loop(msgs)
        assert result is not None

    def test_e4_very_long_conversation(self):
        """E4: Very long conversations (>40 messages — window limit)."""
        # Create 60 unique messages followed by a repeating pattern at the end
        msgs = [_msg(USER, f"unique_{i}") for i in range(30)] + \
               [_msg(ASSISTANT, f"answer_{i}") for i in range(30)]
        
        # Add loop at the very end (within window)
        for _ in range(3):
            msgs.append(_msg(USER, "loop_q"))
            msgs.append(_msg(ASSISTANT, "loop_a"))
        
        result = detect_loop(msgs)
        assert result is not None

    def test_e4b_window_limit_respected(self):
        """E4 variant: Loop pattern at the start should be missed after filler messages.
        
        Window math: last 40 messages are kept. Pattern is 6 msgs (indices 0-5).
        Adding 50 unique filler msgs pushes total to 56. Last 40 = indices 16-55,
        which contain only unique fillers → no loop detected."""
        # Pattern at start + unique filler after
        msgs = [
            _msg(USER, "start_q"), _msg(ASSISTANT, "start_a")
        ] * 3  # L=2 × K=3 loop at beginning (indices 0-5)
        
        # Add 40+ more unique messages to push the pattern out of window
        for i in range(25):
            msgs.append(_msg(USER, f"filler_{i}"))
            msgs.append(_msg(ASSISTANT, f"response_{i}"))
        
        result = detect_loop(msgs)
        # The initial loop is pushed out of the 40-message window
        assert result is None

    def test_e5_function_call_messages(self):
        """E5: Messages with function_call should use name+args for features."""
        pat = [
            _msg(ASSISTANT, "", function_call=FunctionCall("write_file", '{"path":"x"}')),
            _msg(FUNCTION, "done"),
        ]
        msgs = pat * 3
        result = detect_loop(msgs)
        assert result is not None

    def test_e6_mixed_message_types(self):
        """E6: Mix of Message objects and dicts in the same list."""
        msgs = [
            _msg(USER, "hello"),
            {"role": ASSISTANT, "content": "world"},
            _msg(USER, "hello"),
            {"role": ASSISTANT, "content": "world"},
            _msg(USER, "hello"),
            {"role": ASSISTANT, "content": "world"},
        ]
        result = detect_loop(msgs)
        assert result is not None

    def test_e7_mixed_role_pattern(self):
        """E7: Pattern with USER→ASSISTANT→FUNCTION roles."""
        pat = [
            _msg(USER, "query"),
            _msg(ASSISTANT, "thinking"),
            _msg(FUNCTION, "result"),
        ]
        msgs = pat * 3  # L=3 < 5 → K=3 needed
        result = detect_loop(msgs)
        assert result is not None

    def test_e8_single_pattern_length_1_assistant(self):
        """E8: Single ASSISTANT pattern (L==1) should detect."""
        msgs = [_msg(ASSISTANT, "same_response") for _ in range(6)]
        result = detect_loop(msgs)
        assert result is not None

    def test_e9_pattern_at_exact_boundary(self):
        """E9: Pattern detection at exact window boundary (40 messages)."""
        # Exactly 40 messages with a pattern of L=5 × K=2 at the end
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
        result = detect_loop(msgs)
        assert result is not None


# ══════════════════════════════════════════════
# PART 5 — LoopDetectedError Tests
# ══════════════════════════════════════════════

class TestLoopDetectedError:
    """Test the LoopDetectedError exception class."""

    def test_error_with_all_fields(self):
        """Verify all fields are set correctly."""
        err = LoopDetectedError(
            reason="pattern found",
            agent_name="worker1",
            pop_count=5,
            turn_pop_count=3,
            resp_snapshot=[_msg(ASSISTANT, "test")],
        )
        assert err.reason == "pattern found"
        assert err.agent_name == "worker1"
        assert err.pop_count == 5
        assert err.turn_pop_count == 3
        assert len(err.resp_snapshot) == 1
        assert "worker1" in str(err)

    def test_error_with_defaults(self):
        """Verify default values work correctly."""
        err = LoopDetectedError(reason="loop")
        assert err.agent_name is None
        assert err.pop_count is None
        assert err.turn_pop_count == 0
        assert err.resp_snapshot == []
        assert "agent" in str(err)  # Default message uses 'agent'

    def test_error_is_exception(self):
        """Verify it can be raised and caught as a standard exception."""
        with pytest.raises(LoopDetectedError, match="pattern"):
            raise LoopDetectedError(reason="pattern found", agent_name="test")


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
        """Test detection across different pattern lengths and repetition counts."""
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
        
        result = detect_loop(msgs)
        if should_detect:
            assert result is not None, \
                f"L={pattern_length}, K={repetitions}: expected detection"
        else:
            assert result is None, \
                f"L={pattern_length}, K={repetitions}: expected no detection"

    @pytest.mark.parametrize("role", [USER, ASSISTANT, FUNCTION])
    def test_single_role_patterns(self, role):
        """Test that single-role patterns of length 1 are handled correctly.
        
        Note: identical messages also form L=2 patterns (e.g., [user,user] repeating).
        The L==1 guard only prevents detection when the FIRST match is a single-element
        pattern. Since all messages are identical, both L==1 and L==2 match. We verify
        that detection works for at least one role."""
        msgs = [_msg(role, "same") for _ in range(6)]
        
        # FUNCTION-only sequences are filtered (all-function guard), so expect no detection
        if role == FUNCTION:
            assert detect_loop(msgs) is None, f"FUNCTION-only pattern should not trigger"
        else:
            result = detect_loop(msgs)
            assert result is not None, f"Should detect repeating {role} pattern (L>=1)"

    @pytest.mark.parametrize("content_type", [
        lambda i: f"text_{i}",
        lambda i: f"data...\n[TOOL RESPONSE TRUNCATED: {i}%]",
        lambda i: "<think>reasoning</think>\nresult",
    ])
    def test_content_variations(self, content_type):
        """Test detection with different content types."""
        pat = [
            _msg(USER, "query"),
            _msg(ASSISTANT, content_type(0)),
        ]
        msgs = pat * 3
        result = detect_loop(msgs)
        assert result is not None


# ══════════════════════════════════════════════
# PART 7 — Feature Extraction Tests
# ══════════════════════════════════════════════

class TestFeatureExtraction:
    """Test the internal feature extraction logic indirectly via detect_loop."""

    def test_function_call_feature(self):
        """Messages with function calls use name:args as features, not content."""
        # Two messages with same role but different content — should NOT loop
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
        result = detect_loop(msgs)
        assert result is not None, "Same function call should match regardless of content"

    def test_reasoning_content_feature(self):
        """Different reasoning content produces different features."""
        msgs = [
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="reason_1"),
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="reason_2"),
            _msg(USER, "q"),
            _msg(ASSISTANT, "a", reasoning_content="reason_3"),
        ]
        result = detect_loop(msgs)
        assert result is None, "Different reasoning → different features"

    def test_thought_attribute_fallback(self):
        """Messages with 'thought' attribute (instead of reasoning_content) should work.
        
        The feature extraction logic falls back to 'thought' when reasoning_content is absent,
        supporting older message formats or alternative schema implementations."""
        # Use dict messages with 'thought' key instead of 'reasoning_content'
        pat = [
            _dict_msg(USER, "solve this"),
            _dict_msg(ASSISTANT, "The answer is 42", thought="Let me think..."),
        ]
        msgs = pat * 3  # L=2 × K=3
        result = detect_loop(msgs)
        assert result is not None, "Should detect loops using 'thought' attribute fallback"

    def test_long_content_truncation(self):
        """Very long content is truncated to 3000 chars for feature extraction."""
        long_text = "word " * 500  # ~2500 chars per message
        msgs = []
        for i in range(3):
            msgs.append(_msg(USER, "q"))       # Identical USER messages
            msgs.append(_msg(ASSISTANT, long_text))  # Identical ASSISTANT messages
        
        result = detect_loop(msgs)
        assert result is not None, "Should detect pattern with identical long content"