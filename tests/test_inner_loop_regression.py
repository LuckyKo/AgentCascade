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

    These tests use production defaults (score_threshold=350, etc.) to ensure
    threshold tuning didn't break actual loop detection capability.

    Repetition counts are calibrated to what actually triggers detection given:
    - token buffer limit of 1000 tokens (older content gets evicted)
    - ngram_size=64, block_size=128 (windows need enough repetitions to cross threshold)
    - sentence_repetition_threshold=15 (needs many identical normalized sentences)
    """

    def test_single_sentence_loop_detected(self):
        """A single sentence repeated many times should trigger ngram detection.

        With filler occupying ~60% of the token buffer, we need enough loop
        repetitions so that 64-token windows repeat >=7 times to cross
        ngram_repetition_threshold and accumulate score above 350.
        """
        filler = make_unique_filler(4500)
        # 40 repeats ensures enough 64-token windows of identical content survive
        # in the token buffer to trigger ngram detection (+90 each for multiple
        # distinct repeating windows, accumulating past score_threshold=350).
        loop_text = " ".join(["the function takes three parameters for input processing."] * 40)
        text = filler + loop_text

        result = feed_streaming(text, "fixed", 20)

        assert result is not None, (
            f"Should detect single-sentence loop; total text had {len(text)} chars"
        )
        assert "repeated" in result["reason"].lower(), (
            f"Expected repetition detection, got: {result['reason']}"
        )

    def test_short_phrase_loop_detected(self):
        """A short phrase repeating should trigger ngram detection.

        Short phrases pack more repetitions into the token buffer, so they
        trigger faster than longer sentences.
        """
        filler = make_unique_filler(4500)
        # 30 repeats of a short distinctive phrase fills enough of the token
        # buffer that multiple 64-token windows repeat >=7 times.
        loop_text = " ".join(["checking module integrity and validating output"] * 30)
        text = filler + loop_text

        result = feed_streaming(text, "fixed", 20)

        assert result is not None, (
            f"Should detect short phrase loop; total text had {len(text)} chars"
        )

    def test_char_run_with_random_chunks(self):
        """Char run detection should work with variable chunk sizes.

        Char runs are detected per-character regardless of chunk boundaries,
        so this works even with random chunking.
        """
        filler = make_unique_filler(4500)
        text = filler + "a" * 100

        result = feed_streaming(text, "random", 20)

        assert result is not None, (
            f"Should detect char run with random chunks; total text had {len(text)} chars"
        )
        assert "character run" in result["reason"].lower()

    def test_paragraph_loop_detected(self):
        """A paragraph repeating should trigger sentence detection.

        Each paragraph contains multiple sentences ending with punctuation,
        so they get counted individually. With enough repeats, individual
        sentences cross the sentence_repetition_threshold of 15.
        """
        filler = make_unique_filler(4500)
        paragraph = (
            "The implementation follows the standard pattern for this type of operation. "
            "Each step validates its inputs before proceeding to the next phase. "
            "Error handling is centralized in the main processing loop."
        )
        # 12 repeats means each of the 3 sentences appears ~12 times, crossing
        # sentence_repetition_threshold (15) after accounting for decay, triggering
        # +100 per unique repeating sentence.
        loop_text = " ".join([paragraph] * 12)
        text = filler + loop_text

        result = feed_streaming(text, "fixed", 20)

        assert result is not None, (
            f"Should detect paragraph loop; total text had {len(text)} chars"
        )

    def test_loop_without_filler(self):
        """A pure loop with no filler should still be detected after min_chars.

        This verifies detection works when the loop starts from the beginning
        of generation (no unique prefix needed).
        """
        # Enough repeats to exceed min_chars=4000 and trigger ngram detection
        loop_text = " ".join(["checking module integrity and validating output"] * 60)

        result = feed_streaming(loop_text, "fixed", 20)

        assert result is not None, (
            f"Should detect pure loop without filler; total text had {len(loop_text)} chars"
        )

    def test_alternating_pattern_loop_detected(self):
        """Two alternating sentences repeating should trigger detection.

        This tests the scenario where an LLM alternates between two similar
        reasoning patterns (e.g., "The code looks correct." / "No issues found.")
        Both patterns accumulate scores independently, crossing threshold faster.
        """
        filler = make_unique_filler(4500)
        s1 = "the code looks correct here"
        s2 = "no issues found in this section"
        # Alternate 20 times each — both sentences repeat enough for ngram detection
        loop_text = " ".join([s1, s2] * 20)
        text = filler + loop_text

        result = feed_streaming(text, "fixed", 20)

        assert result is not None, (
            f"Should detect alternating pattern loop; total text had {len(text)} chars"
        )