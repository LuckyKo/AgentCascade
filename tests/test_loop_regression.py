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

# Adversarial near-repetitive controls (BUG_0079). The shipped 44-text corpus only
# proves the detector stays quiet on plain prose; it cannot answer whether a *near*-
# repetitive control -- repeated headings, code blocks, JSON output, retry boilerplate --
# also stays quiet through Phase 1. These controls are the must-not-detect side of that
# question (the patterns a *widened* Phase-1 counter could plausibly trip on). Under the
# shipped thresholds, control_A/B stay entirely quiet (never even raise a suspicion);
# control_C is the discriminating one -- it DOES raise a Phase-1 suspicion but must NOT
# be mis-confirmed. See test_control_reaches_phase1_without_false_positive below.
_CONTROL_FILE = Path(__file__).parent / 'loop_controls_near_repetitive.json'
with open(_CONTROL_FILE, encoding='utf-8') as _c:
    _control_data = _json.load(_c)
CONTROL_TEXTS = [c['text'] for c in _control_data['controls']]
# The single control that reaches Phase-1 suspicion yet must not be confirmed.
ADVERSARIAL_CONTROL_TEXTS = [c['text'] for c in _control_data['controls']
                             if c.get('name') == 'control_C_policy_block_spacing']


def feed_chunks(text, chunk_size=100):
    det = InnerLoopDetector()
    for i in range(0, len(text), chunk_size):
        result = det.feed(text[i : i + chunk_size])
        if result:
            return result
    return None


def feed_phase1(text, min_chars=0, chunk_size=100):
    """Feed through the two-phase detector with the min_chars gate open.

    The shipped suite (inner_loop_detect.py:112 + default_min_chars=4000) opens the
    two-phase gate for these boundary fixtures only on the trailing chunk, so the
    n-gram counter is never exercised across the accumulating stream. With min_chars=0
    the gate is open from the start and Phase 1 (_check_suspicion) runs on every chunk.
    We instrument it to count BOTH invocations and raised-suspicion returns so the caller
    can prove Phase 1 was actually reached (calls > 0) and, for the discriminating control,
    that a suspicion was considered but not turned into a false positive.

    Returns (result, phase1_call_count, phase1_suspicion_count).
    """
    det = InnerLoopDetector(min_chars=min_chars)

    two_phase = det._two_phase_detector
    real_check = two_phase._check_suspicion
    calls = {'n': 0}
    suspicions = {'n': 0}

    def counting(new_text):
        calls['n'] += 1
        out = real_check(new_text)
        if out is not None:
            suspicions['n'] += 1
        return out

    two_phase._check_suspicion = counting  # scoped to this detector instance only

    for i in range(0, len(text), chunk_size):
        result = det.feed(text[i : i + chunk_size])
        if result:
            return result, calls['n'], suspicions['n']
    return None, calls['n'], suspicions['n']


class TestNoFalsePositivesOnSamples:
    """All sample texts from loop_samples should NOT trigger detection."""

    @pytest.mark.parametrize('idx', range(len(SAMPLE_TEXTS)))
    def test_no_false_positive(self, idx):
        """Each sample text should not trigger loop detection."""
        result = feed_chunks(SAMPLE_TEXTS[idx])
        assert result is None, f"Sample {idx + 1} triggered: " + str(result)


class TestPhase1ExercisedAndNoFalsePositivesOnSamples:
    """min_chars=0 variant: the 44 boundary samples MUST flow through Phase 1.

    The shipped suite opens the two-phase gate for these fixtures only on the trailing
    chunk (default_min_chars=4000 + 100-char chunks), so the n-gram counter is never
    exercised across the accumulating stream and its green result is not evidence about
    the counting path. This variant opens the gate from the start and proves (a)
    _check_suspicion is actually invoked across the whole stream (not vacuous), and (b)
    the 44 texts remain non-looping even when Phase 1/Phase 2 fully process them.
    """

    @pytest.mark.parametrize('idx', range(len(SAMPLE_TEXTS)))
    def test_phase1_invoked_and_no_false_positive(self, idx):
        result, susp_calls, _susp = feed_phase1(SAMPLE_TEXTS[idx])
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
        result, susp_calls, _susp = feed_phase1(CONTROL_TEXTS[cidx])
        assert susp_calls > 0, f"Control {cidx + 1}: Phase 1 never invoked"
        assert result is None, f"Control {cidx + 1} triggered (Phase-1 false positive): " + str(result)


class TestAdversarialControlRaisesSuspicionButNotFalsePositive:
    """Witness the adversarial Phase-1 -> Phase-2-rejection path.

    The discriminating control (control_C) is an identical >64-token block repeated 9x at
    deliberately VARIED spacing: it must raise a Phase-1 suspicion (proving the fixture
    actually reaches the counting/confirmation path -- otherwise it would be indistinguishable
    from plain prose) but must NOT be mis-confirmed as a loop (the varied spacing breaks byte
    identity). This is the only configuration that proves the controls aren't vacuous.
    """

    @pytest.mark.parametrize('cidx', range(len(ADVERSARIAL_CONTROL_TEXTS)))
    def test_control_reaches_phase1_without_false_positive(self, cidx):
        result, _calls, suspicions = feed_phase1(ADVERSARIAL_CONTROL_TEXTS[cidx])
        assert suspicions > 0, (
            f"Adversarial control {cidx + 1}: raised no Phase-1 suspicion -- it does not "
            f"stress the confirmation path (fixture is not adversarial)"
        )
        assert result is None, f"Adversarial control {cidx + 1} mis-flagged (Phase-1 false positive): " + str(result)
