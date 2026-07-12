"""Regression test: verify loop detector doesn't trigger on known false positive samples."""
from pathlib import Path
import json as _json

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from agent_cascade.inner_loop_detect import InnerLoopDetector

import pytest

# Load sample texts from external JSON data file
_SAMPLE_FILE = Path(__file__).parent / "loop_samples.json"
with open(_SAMPLE_FILE, encoding="utf-8") as _f:
    _sample_data = _json.load(_f)
SAMPLE_TEXTS = [s["text"] for s in _sample_data["samples"]]


def feed_chunks(text, chunk_size=100):
    det = InnerLoopDetector()
    for i in range(0, len(text), chunk_size):
        result = det.feed(text[i : i + chunk_size])
        if result:
            return result
    return None


class TestNoFalsePositivesOnSamples:
    """All sample texts from loop_samples should NOT trigger detection."""

    @pytest.mark.parametrize("idx", range(len(SAMPLE_TEXTS)))
    def test_no_false_positive(self, idx):
        """Each sample text should not trigger loop detection."""
        result = feed_chunks(SAMPLE_TEXTS[idx])
        assert result is None, f"Sample {idx + 1} triggered: " + str(result)
