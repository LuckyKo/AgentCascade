"""Regression test: verify loop detector doesn't trigger on known false positive samples."""
from pathlib import Path
import json as _json

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from agent_cascade.inner_loop_detect import InnerLoopDetector

import pytest

# Load sample texts from external JSON data file
_SAMPLE_FILE = Path(__file__).parent / 'loop_samples.json'
with open(_SAMPLE_FILE, encoding='utf-8') as _f:
    _sample_data = _json.load(_f)
SAMPLE_TEXTS = [s['text'] for s in _sample_data['samples']]

# Adversarial near-repetitive controls (BUG_0079): longer non-looping technical prose
# (repeated headings, code blocks, JSON output, retry boilerplate) that stresses the
# Phase-1 n-gram counter. These are NOT real-output false-positive fixtures -- they are
# hand-constructed to be near-repetitive yet genuinely non-looping, which is the one
# question the shipped 44-text corpus cannot answer at any length.
_CONTROL_FILE = Path(__file__).parent / 'loop_controls_near_repetitive.json'
with open(_CONTROL_FILE, encoding='utf-8') as _c:
    _control_data = _json.load(_c)
CONTROL_TEXTS = [c['text'] for c in _control_data['controls']]


def feed_chunks(text, chunk_size=100):
    det = InnerLoopDetector()
    for i in range(0, len(text), chunk_size):
        result = det.feed(text[i : i + chunk_size])
        if result:
            return result
    return None


def feed_phase1(text, min_chars=0, chunk_size=100):
    """Feed through the two-phase detector, forcing Phase 1 on every chunk.

    The shipped suite short-circuits two-phase detection until min_chars is reached
    (inner_loop_detect.py:112), so it never exercises the n-gram counting path. With
    min_chars=0 that gate is open immediately, and Phase 1 (_check_suspicion) runs on
    each chunk. We instrument it to count invocations so the caller can assert Phase 1
    was actually exercised -- not merely that the result was None (which is vacuously
    true when the gate stays closed).

    Returns (result, phase1_call_count).
    """
    det = InnerLoopDetector(min_chars=min_chars)

    two_phase = det._two_phase_detector
    real_check = two_phase._check_suspicion
    calls = {'n': 0}

    def counting(new_text):
        calls['n'] += 1
        return real_check(new_text)

    two_phase._check_suspicion = counting  # scoped to this detector instance only

    for i in range(0, len(text), chunk_size):
        result = det.feed(text[i : i + chunk_size])
        if result:
            return result, calls['n']
    return None, calls['n']


class TestNoFalsePositivesOnSamples:
    """All sample texts from loop_samples should NOT trigger detection."""

    @pytest.mark.parametrize('idx', range(len(SAMPLE_TEXTS)))
    def test_no_false_positive(self, idx):
        """Each sample text should not trigger loop detection."""
        result = feed_chunks(SAMPLE_TEXTS[idx])
        assert result is None, f"Sample {idx + 1} triggered: " + str(result)


class TestPhase1ExercisedAndNoFalsePositivesOnSamples:
    """min_chars=0 variant: the 44 boundary samples MUST flow through Phase 1.

    The shipped suite above never reaches the two-phase detector for any of these texts
    (every fixture sits at the min_chars boundary -- see BUG_0079), so its green result
    is not evidence about the n-gram counting path. This variant opens the gate and
    proves (a) _check_suspicion is actually invoked, and (b) the 44 texts remain non-looping
    even when Phase 1/Phase 2 process them.
    """

    @pytest.mark.parametrize('idx', range(len(SAMPLE_TEXTS)))
    def test_phase1_invoked_and_no_false_positive(self, idx):
        result, susp_calls = feed_phase1(SAMPLE_TEXTS[idx])
        assert susp_calls > 0, (
            f"Sample {idx + 1}: Phase 1 never invoked "
            f"({susp_calls} _check_suspicion calls) -- the min_chars gate is still short-circuiting"
        )
        assert result is None, f"Sample {idx + 1} triggered: " + str(result)


class TestPhase1NoFalsePositivesOnNearRepetitiveControls:
    """Near-repetitive technical prose must stay non-looping through Phase 1.

    These controls (repeated headings/code/JSON/retry boilerplate) are the patterns a
    widened Phase-1 counter could plausibly trip on. A loop here is a REAL detector false
    positive to be reported -- not papered over by weakening the fixture.
    """

    @pytest.mark.parametrize('cidx', range(len(CONTROL_TEXTS)))
    def test_control_not_looping_phase1(self, cidx):
        result, susp_calls = feed_phase1(CONTROL_TEXTS[cidx])
        assert susp_calls > 0, f"Control {cidx + 1}: Phase 1 never invoked"
        assert result is None, f"Control {cidx + 1} triggered (Phase-1 false positive): " + str(result)
