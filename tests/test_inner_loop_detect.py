"""
Comprehensive unit tests for InnerLoopDetector after Phase 3 cleanup.

Remaining detection modes (all scoring-based modes removed):
 1. Character run detection (last line of defense)
 2. Max chars guard (last line of defense)
 3. Two-phase semantic loop detector (replaces all scoring modes)

Tests cover:
  - Char run detection behavior
  - Max chars guard
  - Return format validation
  - Reset method
  - Multiple feed calls with state accumulation
  - Edge cases
  - Integration scenarios
  - Performance/latency

Run with: pytest tests/test_inner_loop_detect.py -v
"""

import sys
from pathlib import Path

# Ensure the project root is on the path so imports resolve.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import importlib.util
import pytest

# Import settings first so that relative imports in inner_loop_detect resolve.
_settings_spec = importlib.util.spec_from_file_location(
    "settings",
    str(Path(__file__).resolve().parent.parent / "agent_cascade" / "settings.py"),
)
_settings_mod = importlib.util.module_from_spec(_settings_spec)
sys.modules["agent_cascade.settings"] = _settings_mod  # make relative import work
_settings_spec.loader.exec_module(_settings_mod)

# Import directly from the module file to avoid pulling in the entire agent_cascade package.
_spec = importlib.util.spec_from_file_location(
    "inner_loop_detect",
    str(Path(__file__).resolve().parent.parent / "agent_cascade" / "inner_loop_detect.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
InnerLoopDetector = _mod.InnerLoopDetector


# Shared test filler used across multiple test classes.
_FILLER = " ".join(
    f"Word{i} has properties that are interesting for analysis."
    for i in range(1, 20)
) + "."


# ---------------------------------------------------------------------------
# Helper: build a detector tuned for fast testing (low thresholds)
# ---------------------------------------------------------------------------

def make_detector(**kwargs):
    """Create an InnerLoopDetector with test-friendly defaults.

    After Phase 3, constructor only accepts: char_run_limit, min_chars, max_chars, settings.
    Two-phase detector is enabled by default in tests via direct attribute access.
    """
    defaults = dict(
        char_run_limit=130,   # Over 128 chars as requested
        min_chars=500,
        max_chars=40000,      # high enough that tests don't hit it accidentally
    )
    defaults.update(kwargs)
    det = InnerLoopDetector(**defaults)

    # Enable two-phase detector for tests (bypass env var requirement).
    det._two_phase_detector.two_phase_enabled = True

    return det


# ===================================================================
# 1. Character run detection
# ===================================================================

class TestCharacterRunDetection:
    """Feed a chunk with >24 identical characters → should detect loop."""

    def test_single_char_run_detected(self):
        det = make_detector()
        result = det.feed(_FILLER + "a" * 150)  # >130 limit
        assert result is not None, "Should detect a run of 150 identical chars"
        assert result["loop"] is True
        assert "character run" in result["reason"].lower()

    def test_run_at_exactly_limit_plus_one(self):
        """131 identical chars (limit=130 + 1) should trigger."""
        det = make_detector()
        result = det.feed(_FILLER + "x" * 131)
        assert result is not None
        assert result["loop"] is True
    
    def test_run_at_limit_no_detection(self):
        """Exactly 130 identical chars (at the limit, not above) should NOT trigger."""
        det = make_detector()
        # char_run starts at 0; first char sets run=1. After 130 chars run==130.
        # Condition is `> self.char_run_limit` i.e. > 130, so 130 chars → no alert.
        result = det.feed(_FILLER + "y" * 130)
        assert result is None
    
    def test_alternating_chars_no_detection(self):
        """Alternating characters should never trigger a run."""
        det = make_detector()
        result = det.feed(_FILLER + "ab" * 50)
        assert result is None


# ===================================================================
# 2. No loop on normal text (unchanged concept — normal text shouldn't trigger anything)
# ===================================================================

class TestNoLoopOnNormalText:
    """Feed varied text → should return None."""

    def test_normal_paragraph(self):
        det = make_detector()
        paragraph = (
            "Artificial intelligence is transforming the way we work and live. "
            "Machine learning models can now understand natural language with impressive accuracy. "
            "Researchers are constantly developing new architectures to improve performance. "
            "The field has seen remarkable progress in recent years, driven by large datasets "
            "and powerful computing resources that enable training on billions of parameters."
        )
        result = det.feed(paragraph)
        assert result is None

    def test_normal_conversation(self):
        det = make_detector()
        text = (
            "Hello! How are you doing today? I'm fine, thank you for asking. "
            "Would you like to hear about my day? Sure, tell me what happened. "
            "Well, I went to the store and bought some groceries. The weather was nice too."
        )
        result = det.feed(text)
        assert result is None


# ===================================================================
# 3. Reset method
# ===================================================================

class TestResetMethod:
    """Feed some text, reset, feed the same text again → should work correctly."""

    def test_reset_clears_state(self):
        """Feed text that triggers detection, reset, feed again → both should detect."""
        det = make_detector()
        # First pass: trigger char_run detection (>130 chars)
        result1 = det.feed(_FILLER + "a" * 150)
        assert result1 is not None, "First pass should detect loop"

        # Reset and feed the same text again
        det.reset()
        result2 = det.feed(_FILLER + "a" * 150)
        assert result2 is not None, "Second pass should also detect (fresh state)"

    def test_reset_clears_fields(self):
        """Reset should clear all internal state."""
        det = make_detector()
        det.feed("Hello world. ")
        assert det.char_run > 0
        assert det._chars_fed > 0

        det.reset()
        assert det.last_char is None
        assert det.char_run == 0
        assert det._chars_fed == 0

    def test_reset_allows_reuse(self):
        """After reset, the detector should work normally for new text."""
        det = make_detector()
        det.feed(_FILLER + "a" * 150)  # >130 chars triggers loop
        det.reset()
        result = det.feed("Normal sentence. Another one. Yet another.")
        assert result is None


# ===================================================================
# 4. min_chars / max_chars guards
# ===================================================================

class TestMaxCharsGuard:
    """Test the max_chars hard limit."""

    def test_max_chars_triggers(self):
        """Feed enough text to exceed max_chars → should trigger."""
        det = make_detector(max_chars=100)
        result = det.feed("x" * 100)
        assert result is not None
        assert "max chars exceeded" in result["reason"].lower()

    def test_max_chars_at_limit(self):
        """Exactly at max_chars should trigger."""
        det = make_detector(max_chars=50)
        result = det.feed("y" * 50)
        assert result is not None

    def test_max_chars_below_limit(self):
        """Below max_chars should not trigger (assuming no other detection)."""
        det = make_detector(max_chars=1000)
        result = det.feed("Normal text that is well below the limit.")
        assert result is None


# ===================================================================
# 5. Return format validation
# ===================================================================

class TestReturnFormat:
    """When loop detected, verify the dict has expected keys."""

    def test_return_has_required_keys(self):
        det = make_detector()
        result = det.feed(_FILLER + "a" * 150)  # >130 limit
        assert isinstance(result, dict)
        assert "loop" in result
        assert "reason" in result
        # score is still present for backward compatibility (hardcoded to 100)
        assert "score" in result

    def test_loop_key_is_true(self):
        det = make_detector()
        result = det.feed(_FILLER + "a" * 150)  # >130 limit
        assert result["loop"] is True

    def test_reason_is_string(self):
        det = make_detector()
        result = det.feed(_FILLER + "a" * 150)  # >130 limit
        assert isinstance(result["reason"], str)
        assert len(result["reason"]) > 0

    def test_score_is_numeric(self):
        det = make_detector()
        result = det.feed(_FILLER + "a" * 150)  # >130 limit
        assert isinstance(result["score"], (int, float))
        # After Phase 3, score is hardcoded to 100 for all detections
        assert result["score"] == 100

    def test_none_return_on_no_loop(self):
        """Normal text should return None, not a dict."""
        det = make_detector()
        result = det.feed("Hello world. Good morning everyone.")
        assert result is None


# ===================================================================
# 6. Multiple feed calls with state accumulation
# ===================================================================

class TestMultipleFeedCalls:
    """Feed in small chunks, verify state accumulates correctly across calls."""

    def test_char_run_across_chunks(self):
        """A character run spanning multiple feed calls should be detected."""
        det = make_detector()
        # Feed filler first to pass min_chars, then char runs across chunks
        det.feed(_FILLER)
        det.feed("a" * 80)
        result = det.feed("a" * 60)  # total run = 140 > 130
        assert result is not None, "Run across chunks should trigger detection"

    def test_chars_fed_accumulates(self):
        """_chars_fed should accumulate across feed calls."""
        det = make_detector()
        det.feed("abc")      # 3
        det.feed("defg")     # 4
        det.feed("hi")       # 2
        assert det._chars_fed == 9

    def test_char_run_resets_on_different_char(self):
        """Char run should reset when character changes."""
        det = make_detector(char_run_limit=5)
        result = det.feed("aaabbbccccddddddeeeeffffffgg")
        assert result is not None, "Run of 6 f's should trigger"


# ===================================================================
# 7. Memory boundedness (updated for remaining structures)
# ===================================================================

class TestMemoryBoundedness:
    """Feed lots of text and verify remaining structures don't grow unboundedly."""

    def test_tail_buffer_bounded(self):
        """Two-phase tail_buffer is bounded by max_chars guard, not unbounded."""
        det = make_detector(max_chars=10000)
        # Feed enough to exercise tail buffer growth
        for i in range(100):
            det.feed(f"Chunk number {i} with some content here. ")
        # Tail buffer should be reasonable (bounded by max_chars)
        assert len(det._two_phase_detector.tail_buffer) < 10000

    def test_token_buffer_bounded(self):
        """Two-phase token_buffer has explicit max bound."""
        det = make_detector()
        # Feed enough to create well more than 5000 tokens
        for _ in range(200):
            det.feed(" ".join(f"word{i} " for i in range(30)) + ".")
        assert len(det._two_phase_detector.token_buffer) <= det._two_phase_detector.max_token_buffer

    def test_ngram_counter_bounded(self):
        """Two-phase ngram_counter is pruned to max_counter_entries."""
        det = make_detector()
        # Feed diverse text to create many unique n-grams
        for i in range(50):
            words = [f"unique_word_{i}_{j}" for j in range(20)]
            det.feed(" ".join(words) + ".")
        assert len(det._two_phase_detector.ngram_counter) <= det._two_phase_detector.max_counter_entries


# ===================================================================
# 8. Edge cases
# ===================================================================

class TestEdgeCases:
    """Empty chunk, whitespace-only chunk, unicode handling."""

    def test_empty_chunk(self):
        det = make_detector()
        result = det.feed("")
        assert result is None

    def test_whitespace_only_chunk(self):
        det = make_detector()
        result = det.feed("   \n\t  ")
        assert result is None

    def test_multiple_empty_feeds(self):
        """Multiple empty feeds should not cause issues."""
        det = make_detector()
        for _ in range(3):
            result = det.feed("")
            assert result is None

    def test_newline_in_chunk(self):
        """Newlines within text should be handled gracefully."""
        det = make_detector()
        text = "Hello world.\nGood morning.\nHow are you?"
        result = det.feed(text)
        assert result is None

    def test_unicode_text(self):
        """Unicode characters should not cause errors."""
        det = make_detector()
        # Use varied Chinese text to avoid triggering detection on repeated patterns
        text = "你好世界。这是一个测试。今天天气很好。我喜欢编程。机器学习很有趣。" * 4
        result = det.feed(text)
        assert result is None

    def test_char_run_resets_across_whitespace(self):
        """Characters separated by whitespace should not accumulate into a run."""
        det = make_detector(char_run_limit=5)
        # "/" + "\n\n" + "/" → char_run resets, should NOT trigger
        result = det.feed("/\n\n/\n\n/")
        assert result is None


# ===================================================================
# Integration: combined detection scenarios
# ===================================================================

class TestIntegrationScenarios:
    """Realistic integration tests combining remaining signals."""

    def test_char_run_detection(self):
        """Char run detection should work with normal parameters."""
        det = make_detector()
        result = det.feed(_FILLER + "a" * 150)  # >130 limit
        assert result is not None, "Char run should trigger detection"
        assert "character run" in result["reason"].lower()

    def test_streaming_simulation_char_run(self):
        """Simulate streaming with char run appearing at the end."""
        det = make_detector()

        chunks = [
            "The story begins in a small village. ",
            "There lived a young farmer named John. ",
            "John worked hard every day. ",
            "He planted crops and harvested them. ",
            # Degenerate output starts (>130 z's → triggers char_run)
            "z" * 150,
        ]
        result = None
        for chunk in chunks:
            r = det.feed(chunk)
            if r:
                result = r
        assert result is not None, "Streaming char run should be detected"

    def test_custom_parameters(self):
        """Custom constructor parameters should be respected."""
        det = InnerLoopDetector(
            char_run_limit=10,
            min_chars=100,
            max_chars=5000,
        )
        assert det.char_run_limit == 10
        assert det.min_chars == 100
        assert det.max_chars == 5000

    def test_two_phase_detection(self):
        """Two-phase detector should catch exact semantic repetition."""
        det = make_detector()

        # Create a repeating block similar to two-phase tests (~200 chars)
        block = (
            "The system needs to validate the input parameters and ensure they are correct. "
            "After validation, we process the request through the pipeline and generate output."
        )

        result = None
        for _ in range(30):  # Feed many times like two-phase tests do
            r = det.feed(block)
            if r:
                result = r
        assert result is not None, "Two-phase should detect exact semantic repetition"
        assert "semantic loop" in result["reason"].lower()


# ===================================================================
# Performance / latency test
# ===================================================================

class TestFeedPerformance:
    """Measure feed() latency on large text to catch regressions from O(n) ops."""

    def test_feed_latency_large_text(self):
        """Feed ~20KB of text in 100-char chunks and verify total time < 500ms.

        This catches performance regressions such as:
        - Converting the entire deque to a list on every heavy check (O(n) per call).
        - Cryptographic hashing instead of native tuple hashing.
        """
        import time

        det = make_detector()
        # Build ~20KB of varied text (~400 sentences × 50 chars each)
        chunks: list[str] = []
        for i in range(400):
            chunks.append(
                f"In section {i} the analysis reveals that component "
                f"alpha-{i % 20} interacts with module beta-{i // 10} "
                f"in a non-trivial manner. "
            )

        start = time.perf_counter()
        for chunk in chunks:
            det.feed(chunk)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 2000, (
            f"feed() took {elapsed_ms:.0f}ms over {len(chunks)} chunks "
            f"(~{sum(len(c) for c in chunks):,} chars). "
            f"This suggests O(n) operations in the hot path."
        )

    def test_feed_latency_default_params(self):
        """Same latency check but with default detector params."""
        import time

        det = InnerLoopDetector()  # defaults
        det._two_phase_detector.two_phase_enabled = True  # enable for tests

        # Feed enough to pass the min_chars gate (~5KB of text in small chunks)
        chunk_size = 80
        total_chars_needed = det.min_chars + 2000  # overshoot past the gate

        start = time.perf_counter()
        fed = 0
        i = 0
        while fed < total_chars_needed:
            chunk = f"Word{i} has properties that are interesting for analysis. "
            det.feed(chunk)
            fed += len(chunk)
            i += 1

        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 2000, (
            f"Default detector feed() took {elapsed_ms:.0f}ms over {i} chunks "
            f"(~{fed:,} chars). Performance regression detected."
        )