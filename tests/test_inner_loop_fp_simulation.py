"""False positive rate tests for InnerLoopDetector with realistic streaming chunks.

Measures FP rates using real assistant log data fed through the detector with
small chunk sizes (20 chars) matching actual token-by-token streaming, rather
than the 256-char chunks used in older live-data tests.

Run with: pytest tests/test_inner_loop_fp_simulation.py -v
"""

import random
import sys
from pathlib import Path
from typing import Optional

# Ensure tests directory is on path for importing loop_test_utils
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from loop_test_utils import (
    LOG_DIR,
    feed_streaming,
    get_assistant_texts,
)


class TestRealisticChunkSizes:
    """Measure FP rate when feeding real assistant texts in small chunks.

    Uses a sample of 30 texts to stay within CI time limits. FP rate is measured
    empirically; smaller samples have higher variance but still detect major regressions.
    """

    _SAMPLE_SIZE = 30  # Conservative for CI speed; ±6% MOE, sufficient for detecting major FP regressions

    @pytest.mark.skipif(LOG_DIR is None, reason="No log directory found")
    def test_fp_rate_20_char_chunks(self):
        """Feed assistant texts in 20-char chunks. FP rate should be < 3%."""
        texts = get_assistant_texts()
        if len(texts) < 500:
            pytest.skip(f"Need ≥ 500 texts for meaningful FP rate; got {len(texts)}")

        rng = random.Random(123)
        sample = rng.sample(texts, min(self._SAMPLE_SIZE, len(texts)))

        fp_count = sum(1 for t in sample if feed_streaming(t, "fixed", 20) is not None)
        rate = fp_count / len(sample) * 100

        assert rate < 3.0, (
            f"FP rate too high with 20-char chunks: {rate:.1f}% "
            f"({fp_count}/{len(sample)} messages triggered)"
        )


class TestPerModeFP:
    """Count FPs by detection mode to identify which modes are too aggressive.

    Feeds the sample once and checks all three modes from that single pass,
    then splits into separate assertions for clarity.
    """

    _SAMPLE_SIZE = 30

    @pytest.mark.skipif(LOG_DIR is None, reason="No log directory found")
    def test_per_mode_fp_rates(self):
        """Single-pass FP rate check for sentence/ngram/block modes."""
        texts = get_assistant_texts()
        if len(texts) < 500:
            pytest.skip(f"Need ≥ 500 texts; got {len(texts)}")

        rng = random.Random(123)
        sample = rng.sample(texts, min(self._SAMPLE_SIZE, len(texts)))

        sentence_fps = ngram_fps = block_fps = other_fps = 0
        for t in sample:
            result = feed_streaming(t, "fixed", 20)
            if not result:
                continue
            reason_lower = result.get("reason", "").lower()
            if "repeated sentence" in reason_lower:
                sentence_fps += 1
            elif "repeated ngram" in reason_lower:
                ngram_fps += 1
            elif "repeated block" in reason_lower:
                block_fps += 1
            else:
                other_fps += 1

        # Check each mode separately with consistent naming
        sentence_rate = sentence_fps / len(sample) * 100
        ngram_rate = ngram_fps / len(sample) * 100
        block_rate = block_fps / len(sample) * 100

        assert sentence_rate < 5.0, (
            f"Sentence mode FP rate too high: {sentence_rate:.1f}% "
            f"({sentence_fps}/{len(sample)} messages)"
        )
        assert ngram_rate < 5.0, (
            f"N-gram mode FP rate too high: {ngram_rate:.1f}% "
            f"({ngram_fps}/{len(sample)} messages)"
        )
        assert block_rate < 5.0, (
            f"Block mode FP rate too high: {block_rate:.1f}% "
            f"({block_fps}/{len(sample)} messages)"
        )