"""Regression tests for InnerLoopDetector loop detection capability.

Verifies the detector still catches real loops with production settings after
threshold tuning. These tests use synthetic loop patterns to ensure threshold
changes don't break actual loop detection.

Run with: pytest tests/test_inner_loop_regression.py -v
"""

import random
import sys
from pathlib import Path
from typing import Optional

# Ensure tests directory is on path for importing loop_test_utils
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from loop_test_utils import (
    feed_streaming,
    feed_streaming_loop_test,
    make_unique_filler,
)


class TestCharRunStillWorks:
    """Verify char_run detection still works correctly with realistic streaming."""

    def test_char_run_a_100_chars(self):
        """Feed unique filler (>4000 chars) + 'a'*100 in 20-char chunks. Should detect."""
        filler = make_unique_filler(4500)
        text = filler + "a" * 100

        result = feed_streaming(text, "fixed", 20)

        assert result is not None, (
            f"Should detect char run of 100 'a' chars; total text had {len(text)} chars"
        )
        assert "character run" in result["reason"].lower(), (
            f"Expected character run detection, got reason: {result['reason']}"
        )

    def test_char_run_slashes_80_chars(self):
        """Feed unique filler + '/'*80 in 20-char chunks. Common in code fences."""
        filler = make_unique_filler(4500)
        text = filler + "/" * 80

        result = feed_streaming(text, "fixed", 20)

        assert result is not None, (
            f"Should detect char run of 80 '/' chars; total text had {len(text)} chars"
        )
        assert "character run" in result["reason"].lower(), (
            f"Expected character run detection, got reason: {result['reason']}"
        )

    def test_char_run_underscore_80_chars(self):
        """Feed unique filler + '_'*80 in 20-char chunks. Common in separators/markdown."""
        filler = make_unique_filler(4500)
        text = filler + "_" * 80

        result = feed_streaming(text, "fixed", 20)

        assert result is not None, (
            f"Should detect char run of 80 '_' chars; total text had {len(text)} chars"
        )
        assert "character run" in result["reason"].lower()


class TestLoopDetectionRegression:
    """Verify the detector still catches real loops with production settings.

    These tests use two-phase semantic loop detection (replaced scoring-based modes)
    with calibrated thresholds for regression testing:
    - suspicion_threshold=3, confirmed_matches_required=2
    - Larger chunk sizes (50 chars) to avoid word fragmentation breaking ngram matching
    """

    def test_single_sentence_loop_detected(self):
        """A single sentence repeated many times should trigger two-phase detection."""
        filler = make_unique_filler(4500)
        loop_text = " ".join(["the function takes three parameters for input processing."] * 40)
        text = filler + loop_text

        result = feed_streaming_loop_test(text, "fixed", 50)

        assert result is not None, (
            f"Should detect single-sentence loop; total text had {len(text)} chars"
        )
        assert "loop" in result["reason"].lower(), (
            f"Expected loop detection, got: {result['reason']}"
        )

    def test_short_phrase_loop_detected(self):
        """A short phrase repeating should trigger two-phase detection."""
        filler = make_unique_filler(4500)
        loop_text = " ".join(["checking module integrity and validating output"] * 30)
        text = filler + loop_text

        result = feed_streaming_loop_test(text, "fixed", 50)

        assert result is not None, (
            f"Should detect short phrase loop; total text had {len(text)} chars"
        )

    def test_char_run_with_random_chunks(self):
        """Char run detection should work with variable chunk sizes."""
        filler = make_unique_filler(4500)
        text = filler + "a" * 100

        result = feed_streaming(text, "random", 20)

        assert result is not None, (
            f"Should detect char run with random chunks; total text had {len(text)} chars"
        )
        assert "character run" in result["reason"].lower()

    def test_paragraph_loop_detected(self):
        """A paragraph repeating should trigger two-phase detection."""
        filler = make_unique_filler(4500)
        paragraph = (
            "The implementation follows the standard pattern for this type of operation. "
            "Each step validates its inputs before proceeding to the next phase. "
            "Error handling is centralized in the main processing loop."
        )
        loop_text = " ".join([paragraph] * 12)
        text = filler + loop_text

        result = feed_streaming_loop_test(text, "fixed", 50)

        assert result is not None, (
            f"Should detect paragraph loop; total text had {len(text)} chars"
        )

    def test_loop_without_filler(self):
        """A pure loop with no filler should still be detected after min_chars."""
        loop_text = " ".join(["checking module integrity and validating output"] * 100)

        result = feed_streaming_loop_test(loop_text, "fixed", 50)

        assert result is not None, (
            f"Should detect pure loop without filler; total text had {len(loop_text)} chars"
        )

    def test_alternating_pattern_loop_detected(self):
        """Two alternating sentences repeating should trigger detection."""
        filler = make_unique_filler(4500)
        s1 = "the code looks correct here"
        s2 = "no issues found in this section"
        loop_text = " ".join([s1, s2] * 20)
        text = filler + loop_text

        result = feed_streaming_loop_test(text, "fixed", 50)

        assert result is not None, (
            f"Should detect alternating pattern loop; total text had {len(text)} chars"
        )