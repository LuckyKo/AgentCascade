"""Positive regression tests for the two-phase inner-loop detector.

Counterpart to ``tests/test_loop_regression.py`` (which only guards FALSE positives).
These assert the detector actually CATCHES real byte-exact reasoning loops, well
before the max-chars fallback would force a trigger — i.e. that the fix works, not
merely that it does not crash.

Fixtures are vendored under ``tests/loop_positive_samples/`` (copied from the
runtime ``logs/loop_samples/`` captures) so the test is self-contained and host-path
free. The ``.jsonl`` fixture is plain text despite its extension.

Run with: pytest tests/test_inner_loop_positive.py -o addopts="" -v
"""
import sys
from pathlib import Path
import json
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.inner_loop_detect import InnerLoopDetector
from agent_cascade.settings import InnerLoopSettings

_SETTINGS = InnerLoopSettings()
_MAX_CHARS = _SETTINGS.default_max_chars
_DETECT_DEADLINE = int(_MAX_CHARS * 0.75)  # detection must fire well before the max-chars guard
_CONFIRM_REQUIRED = _SETTINGS.loop_confirm_required
_PRE_FIXB_CAP = 200  # pre-FIX-B literal, pinned so this test isolates eviction POLICY, not cap SIZE.

# ── Vendored real fixtures (plain text; the .jsonl one is plain text despite its name) ──
_SAMPLE_DIR = Path(__file__).parent / 'loop_positive_samples'
FIXTURES = {
    'sample_txt': _SAMPLE_DIR / 'sample.txt',
    'samples_2026_10_16': _SAMPLE_DIR / 'samples_2026-10-16.jsonl',
}

# Negative-suite fixture (the same 44 prose samples the shipped suite uses). Loaded at
# module level so a missing/empty file fails loudly instead of silently yielding zero tests.
_NEG_SAMPLES = Path(__file__).parent / 'loop_samples.json'
with open(_NEG_SAMPLES, encoding='utf-8') as _f:
    _NEG_TEXTS = [s['text'] for s in json.load(_f)['samples']]
assert _NEG_TEXTS, 'loop_samples.json is empty'

# 1024 deliberately excluded: known MISS on both fixtures, tracked separately (plan §8)
CHUNKS = (64, 100, 256, 512)
_CAP_BINDING_CHUNK = 256


def _feed(det, text, chunk):
    """Feed ``text`` through ``det`` in ``chunk``-sized pieces; return ``(result, chars_fed)``.

    Folds the two-phase enable into the shared helper as a guard against a settings
    override.
    """
    det._two_phase_detector.two_phase_enabled = True
    for i in range(0, len(text), chunk):
        result = det.feed(text[i:i + chunk])
        if result is not None:
            return result, det._chars_fed
    return None, det._chars_fed


class TestPositiveLoopDetection:
    """Each real fixture, at each chunk size, must be caught as a semantic loop."""

    @pytest.mark.parametrize('fixture_name', sorted(FIXTURES))
    @pytest.mark.parametrize('chunk', CHUNKS)
    def test_semantic_loop_detected(self, fixture_name, chunk):
        text = FIXTURES[fixture_name].read_text(encoding='utf-8')
        # Production InnerLoopSettings defaults (see _SETTINGS); no test-only relaxation is applied.
        det = InnerLoopDetector()
        result, chars_fed = _feed(det, text, chunk)

        # (a) detection fires at all
        assert result is not None, (
            f"{fixture_name} @ chunk {chunk}: no detection — the detector missed a "
            f"real byte-exact loop (the D1/D2 bug)")

        # (b) it is the SEMANTIC loop path, not the max-chars / char-run fallback.
        #     Load-bearing: without this the test would pass trivially via the guard.
        assert result['reason'].startswith('semantic loop'), (
            f"{fixture_name} @ chunk {chunk}: reason={result['reason']!r} is not the "
            f"semantic-loop path")

        # (c) it fired well before the max-chars guard would have forced a trigger
        assert chars_fed < _DETECT_DEADLINE, (
            f"{fixture_name} @ chunk {chunk}: fired at {chars_fed} chars, "
            f">= {_DETECT_DEADLINE} — only the fallback caught it")

        # (d) the confirmation actually met the required exact repetitions
        assert result.get('confirmed_repetitions', 0) >= _CONFIRM_REQUIRED, (
            f"{fixture_name} @ chunk {chunk}: confirmed_repetitions="
            f"{result.get('confirmed_repetitions', 0)} < {_CONFIRM_REQUIRED}")


def _make_cap_binding_fixture():
    """Fully-novel filler followed by a byte-exact loop.

    The novel filler creates far more distinct 64-token windows than the counter cap,
    pinning the cap before the loop ever appears. Under the OLD ``most_common``
    eviction policy the loop window (a fresh singleton) loses every tie against the
    older novel entries and is evicted on each feed, so it never accumulates and the
    loop is MISSED. Under FIX C (evict singletons oldest-first) the novel noise is
    dropped and the loop window survives to threshold. This is the only construction
    that separates FIX C from a bare cap raise.
    """
    # Two load-bearing constraints: the filler must exceed the 200-slot cap (so the
    # counter binds), and the block must exceed the 64-token window (so a periodic
    # window is actually tracked).
    filler_words = [f"novel{i}_{j}" for i in range(120) for j in range(15)]
    filler = ' '.join(filler_words) + '. '
    block_words = [f"loopword{k}" for k in range(96)]
    block = ' '.join(block_words) + '. '
    return filler + block * 20


class TestCapBindingPolicy:
    """A cap-binding stream must still be detected — this locks in FIX C's policy."""

    def test_cap_binding_loop_detected(self):
        text = _make_cap_binding_fixture()
        # Bind the counter cap explicitly (small) while keeping the token buffer
        # UNSATURATED, so the (unfixed D3) scan stays correct and we isolate the
        # eviction POLICY rather than the cap SIZE. max_chars is raised so the
        # semantic path can reach the loop; this is a test-only override.
        det = InnerLoopDetector(max_chars=_MAX_CHARS * 10)
        det._two_phase_detector.max_counter_entries = _PRE_FIXB_CAP
        result, _ = _feed(det, text, _CAP_BINDING_CHUNK)
        assert result is not None, (
            "cap-binding loop not detected — FIX C's singleton-eviction policy is "
            'required to keep the periodic candidate alive under a binding cap')
        assert result['reason'].startswith('semantic loop'), (
            f"cap-binding: reason={result['reason']!r} is not the semantic-loop path")


class TestNoFalsePositivesMinCharsZero:
    """Strengthened negative suite: the 44 real prose samples with min_chars=0.

    The shipped ``tests/test_loop_regression.py`` never exercises Phase 1 (all samples
    are 3999-4000 chars < default_min_chars=4000, so two-phase detection is skipped).
    Forcing min_chars=0 makes Phase 1 actually process them. With the fix, Phase 1
    fires suspicions more often, so this is the real guard against new false positives.
    Phase 2 (byte-identical contiguous repeats) must still reject them.
    """

    @pytest.mark.parametrize('idx', range(len(_NEG_TEXTS)))
    @pytest.mark.parametrize('chunk', (100, 256))  # subset: negative suite only needs representative chunking
    def test_no_false_positive_min_chars_zero(self, idx, chunk):
        text = _NEG_TEXTS[idx]
        det = InnerLoopDetector(min_chars=0)
        result, _ = _feed(det, text, chunk)
        assert result is None, (
            f"sample {idx} @ chunk {chunk}, min_chars=0: false positive {result}")
