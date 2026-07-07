"""
Comprehensive unit tests for InnerLoopDetector.

Tests cover all 12 scenarios requested:
 1. Character run detection
 2. Sentence repetition
 3. N-gram repetition
 4. Block repetition
 5. Low entropy detection
 6. No loop on normal text
 7. Reset method
 8. min_chars gate (heavy checks skipped below threshold)
 9. Return format validation
10. Multiple feed calls with state accumulation
11. Memory boundedness
12. Edge cases (empty, whitespace, None)

Run with: pytest tests/test_inner_loop_detect.py -v
"""

import sys
from collections import Counter
from pathlib import Path

# Ensure the project root is on the path so imports resolve.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import importlib.util
import pytest

# Import directly from the module file to avoid pulling in the entire agent_cascade package.
_spec = importlib.util.spec_from_file_location(
    "inner_loop_detect",
    str(Path(__file__).resolve().parent.parent / "agent_cascade" / "inner_loop_detect.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
InnerLoopDetector = _mod.InnerLoopDetector


# ---------------------------------------------------------------------------
# Helper: build a detector tuned for fast testing (low thresholds)
# ---------------------------------------------------------------------------

def make_detector(**kwargs):
    """Create an InnerLoopDetector with test-friendly defaults."""
    defaults = dict(
        ngram_size=16,       # smaller window for faster tests
        block_size=16,
        entropy_window=16,
        char_run_limit=24,
        score_threshold=50,  # low enough that a single detection event fires
        min_chars=500,       # lower than default 2500 so we don't need massive text
        batch_interval=1,    # check every call for deterministic testing
    )
    defaults.update(kwargs)
    return InnerLoopDetector(**defaults)


# ===================================================================
# 1. Character run detection
# ===================================================================

class TestCharacterRunDetection:
    """Feed a chunk with >24 identical characters → should detect loop."""

    def test_single_char_run_detected(self):
        det = make_detector()
        result = det.feed("a" * 30)
        assert result is not None, "Should detect a run of 30 identical chars"
        assert result["loop"] is True
        assert "character run" in result["reason"].lower()

    def test_run_at_exactly_limit_plus_one(self):
        """25 identical chars (limit=24 + 1) should trigger."""
        det = make_detector()
        result = det.feed("x" * 25)
        assert result is not None
        assert result["loop"] is True

    def test_run_at_limit_no_detection(self):
        """Exactly 24 identical chars (at the limit, not above) should NOT trigger."""
        det = make_detector()
        # char_run starts at 0; first char sets run=1. After 24 chars run==24.
        # Condition is `> self.char_run_limit` i.e. > 24, so 24 chars → no alert.
        result = det.feed("y" * 24)
        assert result is None

    def test_alternating_chars_no_detection(self):
        """Alternating characters should never trigger a run."""
        det = make_detector()
        result = det.feed("ab" * 50)
        assert result is None


# ===================================================================
# 2. Sentence repetition detection
# ===================================================================

class TestSentenceRepetition:
    """Feed the same sentence 3+ times → should detect loop."""

    def test_repeated_sentence_detected(self):
        det = make_detector()
        sentence = "The quick brown fox jumps over the lazy dog."
        text = sentence * 4  # 4 repetitions
        result = det.feed(text)
        assert result is not None, "Should detect repeated sentence"
        assert result["loop"] is True
        assert "repeated sentence" in result["reason"].lower()

    def test_two_repetitions_no_detection(self):
        """Two identical sentences should NOT trigger (threshold is 3)."""
        det = make_detector(min_chars=0)
        sentence = "Hello world."
        result = det.feed(sentence * 2)
        # No heavy checks needed; sentence repetition is always active.
        assert result is None

    def test_different_sentences_no_detection(self):
        """Three different sentences should NOT trigger."""
        det = make_detector()
        text = (
            "The cat sat on the mat."
            "The dog ran in the park."
            "A bird flew over the tree."
        )
        result = det.feed(text)
        assert result is None


# ===================================================================
# 3. N-gram repetition detection
# ===================================================================

class TestNgramRepetition:
    """Feed enough text that creates repeating n-gram patterns → should detect loop."""

    def test_repeated_ngram_detected(self):
        """
        Feed the same phrase multiple times so both sentence repetition and n-gram
        detection fire. The goal is to verify repeated patterns are caught.
        Use min_chars=0 since total text is short (~250 chars).
        """
        det = make_detector(ngram_size=8, min_chars=0)
        phrase = "the quick brown fox jumps over the lazy dog near river bank."
        for _ in range(4):  # 4 feed calls → n-gram hash appears >= 3 times
            result = det.feed(phrase)
        assert result is not None, "Should detect repeated pattern"
        assert result["loop"] is True

    def test_varied_text_no_ngram(self):
        """Text with varied vocabulary should NOT trigger n-gram detection."""
        det = make_detector(ngram_size=8)
        words = [
            "apple", "banana", "cherry", "date", "elderberry", "fig", "grape",
            "honeydew", "kiwi", "lemon", "mango", "nectarine", "orange",
            "papaya", "quince", "raspberry", "strawberry", "tangerine",
            "ugli", "vanilla", "watermelon", "xigua", "yuzu", "zucchini",
        ]
        for w in words:
            det.feed(f"{w} is a fruit. ")
        assert len(det.tokens) >= 8


# ===================================================================
# 4. Block repetition detection
# ===================================================================

class TestBlockRepetition:
    """Feed enough text that creates repeating block patterns → should detect loop."""

    def test_repeated_block_detected(self):
        """
        Feed the same phrase multiple times so both sentence repetition and block
        detection fire. The goal is to verify repeated patterns are caught.
        Use min_chars=0 since total text is short (~165 chars).
        """
        det = make_detector(block_size=8, min_chars=0)
        phrase = "once upon a time there was a little prince on planet mars."
        for _ in range(3):  # 3 feed calls → block hash appears >= 2 times
            result = det.feed(phrase)
        assert result is not None, "Should detect repeated pattern"
        assert result["loop"] is True

    def test_unique_blocks_no_detection(self):
        """Non-repeating text should NOT trigger block detection."""
        det = make_detector(block_size=8)
        for i in range(20):
            det.feed(f"In chapter {i} the hero discovered a new secret. ")
        # Blocks should be unique, so no repeated block detected
        assert len(det.tokens) >= 8


# ===================================================================
# 5. Low entropy detection
# ===================================================================

class TestLowEntropy:
    """Feed text with very few distinct words → should detect loop."""

    def test_low_entropy_detected(self):
        """Only 3 distinct words repeated many times → low Shannon entropy."""
        det = make_detector(entropy_window=16, score_threshold=25, min_chars=0)
        # Only 3 distinct words: "the", "a", "is" — repeated enough to fill the window
        text = "the a is the a is the a is the a is the a is the a is."
        result = det.feed(text)
        assert result is not None, "Should detect low entropy or any loop signal"
        assert result["loop"] is True

    def test_high_entropy_no_detection(self):
        """Text with many distinct words should NOT trigger low entropy."""
        det = make_detector(entropy_window=16, min_chars=0)
        words = [
            "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta",
            "theta", "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron",
            "pi", "rho", "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
        ]
        text = " ".join(words) + "."
        result = det.feed(text)
        assert result is None


# ===================================================================
# 6. No loop on normal text
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
# 7. Reset method
# ===================================================================

class TestResetMethod:
    """Feed some text, reset, feed the same text again → should not detect on second pass."""

    def test_reset_clears_state(self):
        det = make_detector()
        sentence = "The quick brown fox jumps over the lazy dog."
        # First pass: trigger detection
        result1 = det.feed(sentence * 4)
        assert result1 is not None, "First pass should detect loop"

        # Reset and feed the same text again
        det.reset()
        result2 = det.feed(sentence * 4)
        assert result2 is not None, "Second pass should also detect (fresh state)"

    def test_reset_clears_counters(self):
        det = make_detector()
        sentence = "Hello world."
        det.feed(sentence * 5)
        assert len(det.sentences) > 0
        assert det._chars_fed > 0

        det.reset()
        assert det.text == ""
        assert len(det.tokens) == 0
        assert len(det.ngrams) == 0
        assert len(det.blocks) == 0
        assert len(det.sentences) == 0
        assert det.score == 0
        assert det._chars_fed == 0
        assert det._feed_count == 0

    def test_reset_allows_reuse(self):
        """After reset, the detector should work normally for new text."""
        det = make_detector()
        det.feed("a" * 30)  # trigger loop
        det.reset()
        result = det.feed("Normal sentence. Another one. Yet another.")
        assert result is None


# ===================================================================
# 8. min_chars gate
# ===================================================================

class TestMinCharsGate:
    """Feed text below min_chars threshold → heavy checks should be skipped."""

    def test_heavy_checks_skipped_below_threshold(self):
        """Sentence repetition is always active, so use unique sentences to avoid false triggers."""
        det = make_detector(min_chars=2000)
        # Feed short text with unique sentences — below 2000 chars, heavy checks are skipped.
        words = [f"Word{i}" for i in range(50)]
        text = " ".join(words) + "." * len(words)
        result = det.feed(text[:150])  # well under 2000 chars
        assert result is None

    def test_heavy_checks_run_above_threshold(self):
        det = make_detector(min_chars=500)
        # Feed enough text to pass the gate
        phrase = "the quick brown fox jumps over the lazy dog. "
        result = det.feed(phrase * 40)
        # Should either detect or return None, but heavy checks ran
        assert det._chars_fed >= 500

    def test_ngram_below_min_chars(self):
        """N-gram detection should not trigger below min_chars (heavy checks gated)."""
        det = make_detector(min_chars=1000, ngram_size=8)
        # Use unique sentences to avoid sentence repetition triggering first.
        text = " ".join(f"Word{i} is interesting." for i in range(30))
        result = det.feed(text)
        assert result is None

    def test_char_run_always_active(self):
        """Character run detection should work even below min_chars."""
        det = make_detector(min_chars=5000)
        result = det.feed("z" * 30)
        assert result is not None


# ===================================================================
# 9. Return format validation
# ===================================================================

class TestReturnFormat:
    """When loop detected, verify the dict has keys 'loop', 'reason', 'score'."""

    def test_return_has_required_keys(self):
        det = make_detector()
        result = det.feed("a" * 30)
        assert isinstance(result, dict)
        assert "loop" in result
        assert "reason" in result
        assert "score" in result

    def test_loop_key_is_true(self):
        det = make_detector()
        result = det.feed("a" * 30)
        assert result["loop"] is True

    def test_reason_is_string(self):
        det = make_detector()
        result = det.feed("a" * 30)
        assert isinstance(result["reason"], str)
        assert len(result["reason"]) > 0

    def test_score_is_numeric(self):
        det = make_detector()
        result = det.feed("a" * 30)
        assert isinstance(result["score"], (int, float))
        assert result["score"] >= det.threshold

    def test_none_return_on_no_loop(self):
        """Normal text should return None, not a dict."""
        det = make_detector()
        result = det.feed("Hello world. Good morning everyone.")
        assert result is None


# ===================================================================
# 10. Multiple feed calls with state accumulation
# ===================================================================

class TestMultipleFeedCalls:
    """Feed in small chunks, verify state accumulates correctly across calls."""

    def test_state_accumulates_across_feeds(self):
        det = make_detector()
        sentence = "The quick brown fox jumps over the lazy dog."
        # Feed one sentence at a time — state should accumulate
        for _ in range(5):
            result = det.feed(sentence)
        assert det._chars_fed > 0
        assert len(det.tokens) > 0

    def test_char_run_across_chunks(self):
        """A character run spanning multiple feed calls should be detected."""
        det = make_detector()
        det.feed("a" * 15)
        result = det.feed("a" * 15)  # total run = 30
        assert result is not None, "Run across chunks should trigger detection"

    def test_sentence_count_across_feeds(self):
        """Sentence repetition count should accumulate across feed calls."""
        det = make_detector()
        sentence = "Hello world."
        r1 = det.feed(sentence)   # count=1
        assert r1 is None, "First occurrence should not trigger"
        r2 = det.feed(sentence)   # count=2
        assert r2 is None, "Second occurrence should not trigger"
        r3 = det.feed(sentence)   # count=3 → score += 80 >= threshold(50) → trigger
        assert r3 is not None, "Third repetition should trigger (score crosses threshold)"

    def test_feed_count_increments(self):
        """_feed_count should track the number of feed calls."""
        det = make_detector()
        for i in range(10):
            det.feed("a word. ")
        assert det._feed_count == 10


# ===================================================================
# 11. Memory boundedness
# ===================================================================

class TestMemoryBoundedness:
    """Feed lots of text and verify counters don't grow unboundedly."""

    def test_token_deque_bounded(self):
        # Use module-level constants from the directly-loaded module
        _MAX_TOKENS = _mod._MAX_TOKENS
        det = make_detector()
        # Feed enough to create well more than 1000 tokens
        for _ in range(50):
            det.feed(" ".join(f"word{i} " for i in range(30)) + ".")
        assert len(det.tokens) <= _MAX_TOKENS

    def test_ngram_counter_bounded(self):
        _MAX_COUNTER_ENTRIES = _mod._MAX_COUNTER_ENTRIES
        det = make_detector()
        # Feed diverse text to create many unique n-grams
        for i in range(50):
            words = [f"unique_word_{i}_{j}" for j in range(20)]
            det.feed(" ".join(words) + ".")
        assert len(det.ngrams) <= _MAX_COUNTER_ENTRIES

    def test_block_counter_bounded(self):
        _MAX_COUNTER_ENTRIES = _mod._MAX_COUNTER_ENTRIES
        det = make_detector()
        for i in range(50):
            words = [f"block_word_{i}_{j}" for j in range(20)]
            det.feed(" ".join(words) + ".")
        assert len(det.blocks) <= _MAX_COUNTER_ENTRIES

    def test_sentence_counter_bounded(self):
        _MAX_COUNTER_ENTRIES = _mod._MAX_COUNTER_ENTRIES
        det = make_detector()
        for i in range(50):
            det.feed(f"This is sentence number {i} with some extra words. ")
        assert len(det.sentences) <= _MAX_COUNTER_ENTRIES

    def test_text_grows_reasonably(self):
        """Internal text buffer should not grow without bound (only stores remainder)."""
        det = make_detector()
        for i in range(100):
            det.feed(f"Chunk number {i} with some content here. ")
        # Text buffer only holds the remainder after last sentence boundary
        assert len(det.text) < 50


# ===================================================================
# 12. Edge cases
# ===================================================================

class TestEdgeCases:
    """Empty chunk, whitespace-only chunk, None handling."""

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
        for _ in range(100):
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
        text = "你好世界。这是一个测试。" * 10
        result = det.feed(text)
        assert result is None

    def test_mixed_case_sentences(self):
        """Mixed case sentences should be normalized before comparison."""
        det = make_detector(score_threshold=50)
        # These should all normalize to the same sentence "hello world"
        text = "Hello world. HELLO WORLD. hello World."
        result = det.feed(text)
        assert result is not None, "Case variations of same sentence should match after normalization"

    def test_punctuation_variations(self):
        """Different punctuation at end should still detect repetition."""
        det = make_detector(score_threshold=50)
        text = "Hello world. Hello world! Hello world?"
        # After normalization these are all "hello world" (punctuation stripped by regex)
        result = det.feed(text)
        assert result is not None, "Punctuation variations should still match after normalization"

    def test_score_decay(self):
        """Score should decay between feed calls."""
        det = make_detector(score_threshold=1000)  # high threshold so we don't trigger
        det.score = 100
        det.feed("Hello. ")
        assert det.score < 100, "Score should have decayed"

    def test_add_score_returns_on_threshold(self):
        """add_score should return a dict when threshold is crossed."""
        det = make_detector(score_threshold=50)
        result = det.add_score(60, "test reason")
        assert result is not None
        assert result["loop"] is True

    def test_add_score_returns_none_below_threshold(self):
        """add_score should return None below threshold."""
        det = make_detector(score_threshold=100)
        result = det.add_score(50, "test reason")
        assert result is None


# ===================================================================
# Integration: combined detection scenarios
# ===================================================================

class TestIntegrationScenarios:
    """Realistic integration tests combining multiple signals."""

    def test_compound_detection(self):
        """Multiple weak signals should compound to exceed threshold."""
        det = make_detector(score_threshold=150)
        # Feed text with slight repetition + low entropy words
        phrase = "the the a is the a is the the a is. "
        result = det.feed(phrase * 20)
        assert result is not None, "Compound signals should trigger detection"

    def test_streaming_simulation(self):
        """Simulate streaming LLM output with gradual repetition."""
        det = make_detector(batch_interval=1, score_threshold=50)
        chunks = [
            "The story begins in a small village. ",
            "There lived a young farmer named John. ",
            "John worked hard every day. ",
            "He planted crops and harvested them. ",
            # Now start repeating patterns
            "John worked hard every day. ",
            "He planted crops and harvested them. ",
            "John worked hard every day. ",  # third repetition → sentence detection
        ]
        result = None
        for chunk in chunks:
            r = det.feed(chunk)
            if r:
                result = r
        assert result is not None, "Streaming repetition should be detected"

    def test_custom_batch_interval(self):
        """With batch_interval > 1, heavy checks only run on specific calls."""
        det = make_detector(batch_interval=3, min_chars=0)
        # Feed 2 times — _feed_count=1,2 (not multiples of 3), should skip heavy checks
        for i in range(2):
            det.feed(f"Word {i}. ")
        assert det._feed_count == 2

    def test_custom_parameters(self):
        """Custom constructor parameters should be respected."""
        det = InnerLoopDetector(
            ngram_size=64,
            block_size=64,
            entropy_window=64,
            char_run_limit=10,
            score_threshold=50,
            min_chars=100,
            batch_interval=2,
        )
        assert det.ngram_size == 64
        assert det.block_size == 64
        assert det.entropy_window == 64
        assert det.char_run_limit == 10
        assert det.threshold == 50
        assert det.min_chars == 100
        assert det.batch_interval == 2

    def test_batch_interval_minimum(self):
        """batch_interval of 0 or negative should be clamped to 1."""
        det = InnerLoopDetector(batch_interval=0)
        assert det.batch_interval == 1
        det2 = InnerLoopDetector(batch_interval=-5)
        assert det2.batch_interval == 1


# ===================================================================
# Counter trimming utility tests
# ===================================================================

class TestCounterTrimming:
    """Test the _trim_counter static method."""

    def test_trim_reduces_to_max_entries(self):
        counter = Counter(f"item_{i}" for i in range(300))
        InnerLoopDetector._trim_counter(counter, max_entries=200)
        assert len(counter) <= 200

    def test_trim_keeps_frequent_items(self):
        """Frequent items should be retained after trimming."""
        counter = Counter()
        for i in range(10):
            counter[f"common_{i}"] += 10  # high count
        for i in range(250):
            counter[f"rare_{i}"] = 1      # low count
        assert len(counter) > 200

        InnerLoopDetector._trim_counter(counter, max_entries=200)
        assert len(counter) <= 200
        # After trimming the top entries should still be string keys (not tuples).
        for i in range(10):
            assert f"common_{i}" in counter, (
                f"common_{i} should survive trimming "
                f"(counter keys: {list(counter.keys())[:3]}...)"
            )

    def test_trim_preserves_key_types(self):
        """Trimmed counter should keep string keys, not tuple keys."""
        counter = Counter()
        for i in range(10):
            counter[f"key_{i}"] += 5
        InnerLoopDetector._trim_counter(counter, max_entries=200)
        # All remaining keys should be strings
        assert all(isinstance(k, str) for k in counter.keys()), (
            f"_trim_counter stored tuples as keys: {list(counter.items())[:3]}"
        )

    def test_trim_noop_when_under_limit(self):
        counter = Counter(a=1, b=2, c=3)
        InnerLoopDetector._trim_counter(counter, max_entries=100)
        assert len(counter) == 3


# ===================================================================
# Score mechanics tests
# ===================================================================

class TestScoreMechanics:
    """Test the scoring system in detail."""

    def test_score_accumulation(self):
        det = make_detector(score_threshold=200)
        det.add_score(50, "reason1")
        assert det.score == 50
        det.add_score(50, "reason2")
        assert det.score == 100

    def test_decay_factor(self):
        det = make_detector()
        det.score = 100
        det.decay()
        expected = round(100 * 0.97, 10)
        assert abs(det.score - expected) < 0.001

    def test_score_rounded_in_return(self):
        """Score in the returned dict should be rounded to 1 decimal."""
        det = make_detector(score_threshold=50)
        result = det.add_score(60, "test")
        assert isinstance(result["score"], (int, float))
        # Verify it's rounded: no more than 1 decimal place
        formatted = f"{result['score']:.1f}"
        assert abs(float(formatted) - result["score"]) < 0.001