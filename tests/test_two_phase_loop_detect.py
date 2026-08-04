"""Comprehensive tests for TwoPhaseLoopDetector.

Tests all scenarios from the two-phase loop detection plan (Section 5):
 1. Basic suspicion → confirmation flow
 2. Non-loop content should NOT trigger confirmation
 3. Cooldown behavior
 4. Feature flag gating
 5. Reset behavior
 6. Edge cases
 7. [A,B,C,D,D,D] vs [A,D,B,C,D,E,D] discrimination

Run with: pytest tests/test_two_phase_loop_detect.py -v
"""

import sys
from pathlib import Path

# Ensure project root is on path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from agent_cascade.two_phase_loop_detect import TwoPhaseLoopDetector


def make_detector(**kwargs):
    """Create a TwoPhaseLoopDetector with test-friendly defaults.

    All tests set two_phase_enabled=True to bypass env var requirement.
    """
    d = TwoPhaseLoopDetector()
    # Override for faster testing while keeping algorithm behavior intact
    d.suspicion_threshold = 7
    d.confirmed_matches_required = 3
    d.cooldown_duration = 5
    d.ngram_window_size = 64
    d.two_phase_enabled = True
    # Apply any caller overrides
    for k, v in kwargs.items():
        setattr(d, k, v)
    return d


class TestFeatureFlagGating:
    """Test that the detector respects the feature flag (Scenario 4)."""

    def test_disabled_returns_none(self):
        """With two_phase_enabled=False, feed() always returns None."""
        d = make_detector()
        d.two_phase_enabled = False

        # Feed arbitrary content — should never detect anything
        for i in range(20):
            result = d.feed(f"This is chunk number {i} with unique content.")
            assert result is None, "Disabled detector must always return None"

    def test_disabled_no_state_change(self):
        """With two_phase_enabled=False, internal state should remain empty."""
        d = make_detector()
        d.two_phase_enabled = False

        d.feed("Some content here")
        assert len(d.token_buffer) == 0, "Disabled detector should not accumulate tokens"
        assert len(d.tail_buffer) == 0, "Disabled detector should not accumulate tail"


class TestBasicSuspicionConfirmationFlow:
    """Test the core suspicion → confirmation flow (Scenario 1)."""

    def test_short_interval_loop(self):
        """Feed a repeating block of ~200 chars multiple times → triggers loop.

        Simulates actual semantic loop: same text repeated at regular intervals.
        """
        d = make_detector()

        # Create a short repeating block (~200 chars)
        block = (
            "The system needs to validate the input parameters and ensure they are correct. "
            "After validation, we process the request through the pipeline and generate output."
        )
        assert 150 <= len(block) <= 250, f"Block size {len(block)} out of expected range"

        # Feed the block many times — should eventually trigger suspicion then confirm
        for i in range(30):
            result = d.feed(block)
            if result is not None:
                assert result["loop"] is True, f"Expected loop detection, got: {result}"
                assert "reason" in result
                assert "confirmed_repetitions" in result
                return

        pytest.fail("Loop was not detected after 30 repetitions of the same block")

    def test_medium_interval_loop(self):
        """Feed a repeating block of ~500 chars multiple times → triggers loop."""
        d = make_detector()

        # Create a medium repeating block (~500 chars)
        block = (
            "To implement this feature we need to consider several factors. First, the data model "
            "must support the new requirements without breaking existing functionality. Second, the API "
            "interface should remain backward compatible while exposing new capabilities. Third, error "
            "handling must be robust enough to deal with edge cases gracefully. Finally, we need to add "
            "appropriate logging and monitoring so that issues can be detected early in production."
        )
        assert 400 <= len(block) <= 600, f"Block size {len(block)} out of expected range"

        for i in range(30):
            result = d.feed(block)
            if result is not None:
                assert result["loop"] is True, f"Expected loop detection, got: {result}"
                return

        pytest.fail("Loop was not detected after 30 repetitions of the same block")


class TestNonLoopContentNoConfirmation:
    """Test that normal prose with repeated words/phrases does NOT confirm as loop (Scenario 2)."""

    def test_technical_prose_with_repeated_words(self):
        """Feed technical writing with repeated terminology → no loop confirmed.

        Technical text naturally repeats terms like 'the', 'function', 'returns', etc.
        Suspicion may fire, but confirmation should fail → cooldown applied.
        """
        d = make_detector()

        # Normal technical prose — repeated words/phrases in different contexts
        chunks = [
            "The function validate_input checks whether the provided parameters are correct. "
            "It returns an error if any validation fails.",
            "Next we call process_request which handles the main business logic. "
            "This function is responsible for transforming the input data.",
            "After processing, the system generates a response object containing the results. "
            "The response is then serialized and sent back to the client.",
            "Error handling in this module uses try-except blocks around critical operations. "
            "Any unhandled exceptions are logged and converted to appropriate error responses.",
            "The configuration manager loads settings from environment variables and config files. "
            "It provides a centralized way to access all configuration values throughout the application.",
        ]

        loop_detected = False
        for chunk in chunks:
            result = d.feed(chunk)
            if result is not None and result.get("loop"):
                loop_detected = True
                break

        # Should NOT detect a loop — this is normal prose with repeated terminology
        assert not loop_detected, "Normal technical prose should not be detected as a loop"

    def test_same_tool_names_different_contexts(self):
        """Feed text mentioning same tool names multiple times in different contexts → no loop.

        Simulates agent talking about tools: read_file called here and there for different purposes.
        """
        d = make_detector()

        chunks = [
            "I'll use read_file to examine the main module first. Let me check what's in src/main.py.",
            "Now I need to call code_interpreter to run a quick test of the logic.",
            "Let me use grep to search for all occurrences of that function name across the codebase.",
            "I'll call read_file again on the configuration file to see the settings.",
            "Using code_interpreter once more to verify the calculation results.",
            "Finally I'll use write_file to create the new module with the fix.",
            "Let me run grep one more time to make sure we caught all instances.",
            "I should call read_file on the test file to understand the existing coverage.",
        ]

        loop_detected = False
        for chunk in chunks:
            result = d.feed(chunk)
            if result is not None and result.get("loop"):
                loop_detected = True
                break

        assert not loop_detected, "Same tool names in different contexts should not be a loop"


class TestCooldownBehavior:
    """Test cooldown after failed confirmation (Scenario 3)."""

    def test_cooldown_applied_after_failed_confirmation(self):
        """After suspicion fires but confirmation fails → cooldown becomes active.

        We directly invoke _apply_cooldown to verify its state changes, since
        triggering natural suspicion+failed confirmation is content-dependent.
        """
        d = make_detector()

        # Verify initial state: no cooldown
        assert d.cooldown_active is False
        assert d.cooldown_remaining_feeds == 0

        # Apply cooldown (simulates failed confirmation)
        d._apply_cooldown()

        # Verify cooldown state changes
        assert d.cooldown_active is True, "Cooldown should be active after _apply_cooldown()"
        assert d.cooldown_remaining_feeds == d.cooldown_duration, \
            f"Cooldown remaining should equal duration ({d.cooldown_duration})"
        # Ngram tracking state cleared on cooldown
        assert len(d.ngram_counter) == 0, "ngram_counter should be cleared on cooldown"
        assert len(d.ngram_positions) == 0, "ngram_positions should be cleared on cooldown"

    def test_cooldown_suppresses_detection(self):
        """During cooldown, suspicious content should be suppressed.

        We manually activate cooldown to verify suppression behavior, since
        triggering suspicion+failed confirmation depends on content patterns.
        """
        d = make_detector()

        # Manually activate cooldown (simulates failed confirmation)
        d._apply_cooldown()

        assert d.cooldown_active is True, "Cooldown should be active after _apply_cooldown()"

        # Feed content during cooldown — all should return None without checking suspicion
        suppressed_count = 0
        for i in range(10):
            result = d.feed(f"Content during cooldown number {i}.")
            if result is None:
                suppressed_count += 1

        assert suppressed_count == 10, f"All feeds during cooldown should return None, got {suppressed_count}"
        # Verify cooldown decremented and deactivated after duration exceeded
        assert d.cooldown_remaining_feeds <= 0, \
            "Cooldown remaining feeds should reach 0 or below after enough feeds"

    def test_cooldown_expires_and_detection_resumes(self):
        """After cooldown duration expires → detection resumes normally.

        We manually activate cooldown, then verify it expires and a new loop is detected.
        """
        d = make_detector(cooldown_duration=3)  # Short cooldown for testing

        # Manually activate cooldown (simulates failed confirmation on prior content)
        d._apply_cooldown()
        assert d.cooldown_active is True, "Cooldown should be active"

        # Feed exact loop content — first few feeds suppressed by cooldown
        exact_block = "REPEAT: This is an exact repeating block for testing purposes only.\n"

        # During cooldown: feeds return None (suppressed)
        for i in range(3):
            result = d.feed(exact_block)
            assert result is None, f"Feed {i} during cooldown should be suppressed"

        # Cooldown should now be expired or expiring
        # Continue feeding the exact same block — detection should resume and trigger
        for i in range(30):
            result = d.feed(exact_block)
            if result is not None:
                assert result["loop"] is True, \
                    f"After cooldown expires, loop should be detected. Got: {result}"
                return

        pytest.fail("Loop was not detected after cooldown expired")


class TestResetBehavior:
    """Test that reset() clears all state (Scenario 5)."""

    def test_reset_clears_all_state(self):
        """Call reset() → all internal buffers and counters cleared."""
        d = make_detector()

        # Feed some content to build up state
        for i in range(10):
            d.feed(f"Building state with chunk {i}.")

        assert len(d.token_buffer) > 0, "Token buffer should have content before reset"
        assert len(d.tail_buffer) > 0, "Tail buffer should have content before reset"

        # Reset
        d.reset()

        # Verify all state cleared
        assert len(d.token_buffer) == 0, "token_buffer not cleared by reset()"
        assert len(d.ngram_counter) == 0, "ngram_counter not cleared by reset()"
        assert len(d.ngram_positions) == 0, "ngram_positions not cleared by reset()"
        assert d.cooldown_active is False, "cooldown_active not cleared by reset()"
        assert d.cooldown_remaining_feeds == 0, "cooldown_remaining_feeds not cleared by reset()"
        assert d.tail_buffer == "", "tail_buffer not cleared by reset()"

    def test_reset_allows_fresh_detection(self):
        """After reset(), detector can start fresh and detect new loops."""
        d = make_detector()

        # Feed non-loop content
        for i in range(10):
            d.feed(f"Non-loop content number {i} with unique information.")

        # Reset
        d.reset()

        # Now feed an actual loop — should be detected from fresh state
        block = "Fresh loop block that repeats exactly the same way every time.\n"
        for i in range(30):
            result = d.feed(block)
            if result is not None:
                assert result["loop"] is True, "Should detect loop after reset with fresh content"
                return

        pytest.fail("Loop not detected after reset")


class TestEdgeCases:
    """Test edge cases and boundary conditions (Scenario 6)."""

    def test_very_short_intervals_not_confirmed(self):
        """Very short intervals (<10 chars) should not confirm as meaningful loop.

        Single-character feeds cannot form 64-token ngrams, so suspicion never fires.
        Verifies no crash and no false positive detection on trivial repetition.
        """
        d = make_detector()

        for i in range(50):
            result = d.feed("x ")
            assert result is None, f"Very short repeat should not confirm as loop, got: {result}"

    def test_empty_feeds_no_errors(self):
        """Empty string feeds should not cause errors."""
        d = make_detector()

        for i in range(10):
            result = d.feed("")
            assert result is None, f"Empty feed should return None, got: {result}"

    def test_whitespace_only_feeds_no_errors(self):
        """Whitespace-only feeds should not cause errors."""
        d = make_detector()

        for i in range(10):
            result = d.feed("   \n\t  ")
            assert result is None, f"Whitespace feed should return None, got: {result}"

    def test_single_character_repeated(self):
        """Single character repeated many times should not crash or produce false positives."""
        d = make_detector()

        for i in range(20):
            result = d.feed("a")
            # Single chars can't form 64-token ngrams, so no suspicion → no detection
            assert result is None, f"Single char repeat should not detect loop, got: {result}"


class TestLoopVsNonLoopDiscrimination:
    """Test [A,B,C,D,D,D] vs [A,D,B,C,D,E,D] discrimination (Scenario 7).

    This is the KEY test: distinguish actual loops from scattered repetition.
    """

    def test_actual_loop_triggers_confirmation(self):
        """[A,B,C,D,D,D] pattern: repeating exact same block at end → triggers confirmation.

        Simulates: preamble text followed by repeated identical output (actual loop).
        """
        d = make_detector()

        # Phase A: Some unique preamble content
        d.feed("Starting analysis of the system architecture and design patterns.\n")
        d.feed("The main components include the controller, service layer, and data access.\n")

        # Phase B: More unique setup content
        d.feed("Now examining the error handling strategy and logging configuration.\n")
        d.feed("All exceptions should be caught at the boundary and converted to responses.\n")

        # Phase C: Transition content
        d.feed("Beginning detailed review of each module in sequence.\n")

        # Phase D,D,D: Exact same block repeated (actual loop)
        loop_block = (
            "The read_file tool returns the contents of a specified file path. "
            "It supports line range selection and handles both text and binary files. "
            "For large files, content is truncated with details on how to read more.\n"
        )

        for i in range(10):
            result = d.feed(loop_block)
            if result is not None:
                assert result["loop"] is True, \
                    f"[A,B,C,D,D,D] pattern should trigger loop confirmation, got: {result}"
                return

        pytest.fail("[A,B,C,D,D,D] actual loop was not detected")

    def test_scattered_repetition_does_not_confirm(self):
        """[A,D,B,C,D,E,D] pattern: same tool mentioned with different args scattered → no loop.

        Simulates: agent using the same tools throughout but for different purposes (not looping).
        """
        d = make_detector()

        chunks = [
            # A: Start with read_file
            "I'll use read_file to examine src/main.py and understand the entry point.\n",

            # D variant 1: Use code_interpreter
            "Now calling code_interpreter to run a quick test of the main function.\n",

            # B: Some analysis content
            "The code structure looks clean. There are three main classes defined here.\n",

            # C: More unique content
            "Let me check the configuration by reading the settings file next.\n",

            # D variant 2: Use code_interpreter again (different context)
            "I'll use code_interpreter once more to validate the configuration parsing logic.\n",

            # E: Different tool usage
            "Running grep to find all references to the config class across the project.\n",

            # D variant 3: Use code_interpreter third time (yet different context)
            "Finally, code_interpreter will help me verify the edge case handling works correctly.\n",
        ]

        loop_detected = False
        for chunk in chunks:
            result = d.feed(chunk)
            if result is not None and result.get("loop"):
                loop_detected = True
                break

        assert not loop_detected, \
            "[A,D,B,C,D,E,D] scattered repetition should NOT trigger loop confirmation"

    def test_identical_tool_calls_are_loop(self):
        """If the EXACT same tool call description repeats → it IS a loop."""
        d = make_detector()

        # Preamble
        d.feed("Analyzing the codebase structure.\n")

        # Exact repeated block (simulating agent stuck in a loop describing same action)
        block = (
            "I will call read_file on tests/conftest.py to examine the test fixtures. "
            "This file contains pytest configuration and shared fixtures for all tests.\n"
        )

        for i in range(15):
            result = d.feed(block)
            if result is not None:
                assert result["loop"] is True, \
                    f"Identical repeated tool call descriptions should be a loop, got: {result}"
                return

        pytest.fail("Identical repeated block was not detected as loop")


class TestReturnFormat:
    """Test that detection results have correct format."""

    def test_loop_result_format(self):
        """When loop is detected, result dict has expected keys and types."""
        d = make_detector()

        block = "This block repeats exactly to form a detectable semantic loop pattern.\n"
        for i in range(20):
            result = d.feed(block)
            if result is not None:
                assert isinstance(result, dict), "Result should be a dict"
                assert result.get("loop") is True, "loop key should be True"
                assert "reason" in result, "reason key should be present"
                assert isinstance(result["reason"], str), "reason should be string"
                assert "confirmed_repetitions" in result, "confirmed_repetitions key should be present"
                assert isinstance(result["confirmed_repetitions"], int), \
                    "confirmed_repetitions should be integer"
                assert result["confirmed_repetitions"] >= d.confirmed_matches_required, \
                    f"confirmed_repetitions ({result['confirmed_repetitions']}) should be >= required ({d.confirmed_matches_required})"
                return

        pytest.fail("Loop not detected to verify format")


class TestSuspicionIntervalEstimation:
    """Test the interval estimation logic."""


class TestTokenization:
    """Test the tokenize_chunk helper."""

    def test_tokenize_basic(self):
        """Basic tokenization splits on whitespace and strips punctuation."""
        from agent_cascade.two_phase_loop_detect import tokenize_chunk

        tokens = tokenize_chunk("Hello, world! This is a test.")
        assert tokens == ["hello", "world", "this", "is", "a", "test"]

    def test_tokenize_empty(self):
        """Empty string returns empty list."""
        from agent_cascade.two_phase_loop_detect import tokenize_chunk

        tokens = tokenize_chunk("")
        assert tokens == []

    def test_tokenize_whitespace_only(self):
        """Whitespace-only string returns empty list."""
        from agent_cascade.two_phase_loop_detect import tokenize_chunk

        tokens = tokenize_chunk("   \n\t  ")
        assert tokens == []

    def test_tokenize_case_normalized(self):
        """Tokens are lowercased."""
        from agent_cascade.two_phase_loop_detect import tokenize_chunk

        tokens = tokenize_chunk("Hello WORLD Test")
        assert tokens == ["hello", "world", "test"]

    def test_tokenize_strips_punctuation(self):
        """Leading/trailing punctuation is stripped from tokens."""
        from agent_cascade.two_phase_loop_detect import tokenize_chunk

        tokens = tokenize_chunk('"quoted" (parenthesized) [bracketed]')
        assert tokens == ["quoted", "parenthesized", "bracketed"]


class TestMemoryBoundedness:
    """Test that internal structures stay bounded."""

    def test_token_buffer_bounded(self):
        """Token buffer does not grow beyond max_token_buffer."""
        d = make_detector()

        # Feed enough content to exceed the buffer limit
        for i in range(100):
            d.feed(f"Chunk number {i} with some unique tokens to fill the buffer up.\n")

        assert len(d.token_buffer) <= d.max_token_buffer, \
            f"Token buffer ({len(d.token_buffer)}) exceeds max ({d.max_token_buffer})"

    def test_ngram_positions_bounded(self):
        """Position lists per ngram are bounded to last 8 entries."""
        d = make_detector()

        # Feed repetitive content that will create many positions for same ngram
        block = "Repeated block with enough tokens to form stable ngrams across feeds.\n"
        for i in range(50):
            d.feed(block)

        # Check all position lists are bounded
        for ngram, positions in d.ngram_positions.items():
            assert len(positions) <= 8, \
                f"Ngram {ngram} has {len(positions)} positions, exceeds limit of 8"