# Inner Loop Detection — Test Gap Analysis Report

**Date:** 2026-07-07  
**Scope:** `agent_cascade/inner_loop_detect.py` and its three test files

---

## 1. Current Test Coverage Summary

### Three Test Files, Different Purposes

| File | Tests | Purpose |
|------|-------|---------|
| `test_inner_loop_detect.py` | ~45 tests across 12 categories | Unit-level: each detection mechanism in isolation |
| `test_inner_loop_live_data.py` | ~8 tests | Integration-level: real agent JSONL logs + synthetic realistic-length data |
| `test_loop_regression.py` | Parametrized (one per sample) | Regression: known false-positive samples from `loop_samples/` directory |

### What IS Covered Well

| Detection Mechanism | Unit Tests | Live Data Tests | Regression Tests |
|---------------------|-----------|-----------------|------------------|
| Character runs (>70 chars) | ✅ Boundary + positive + negative | ✅ 120-char run, space runs | ✅ Via sample texts |
| Sentence repetition (≥7×) | ✅ Exact + case-insensitive + punctuation variants | ✅ Reasoning-style repeats | ✅ Via sample texts |
| N-gram repetition (≥5×) | ✅ Positive + varied vocabulary negative | ✅ 128-token loops | ✅ Via sample texts |
| Block repetition (≥4×) | ✅ Positive + unique blocks negative | ✅ 256-token block loops | ✅ Via sample texts |
| Low entropy (<2.0 bits) | ✅ 3-word text + high-entropy vocab | ❌ Not explicitly tested | ❌ Not explicitly tested |
| Score accumulation/decay | ✅ Accumulation, decay factor, threshold crossing | ❌ Indirect only | ❌ Not tested |
| min_chars gate | ✅ Heavy checks skipped below / active above | ✅ Via chunked streaming | ✅ Via sample texts |
| Memory boundedness | ✅ Token deque, all 3 counters, text buffer | ❌ Not tested long streams | ❌ Not tested |
| Edge cases (empty, whitespace, Unicode) | ✅ All handled | ✅ Extreme chunking (1 char at a time) | ✅ Via sample texts |
| Return format validation | ✅ Dict keys, types, None on no-loop | ✅ Result type check | ✅ Asserts None |

### Regression Test Sample Count

The regression tests pull from `SAMPLE_TEXTS` — 62 pre-collected agent output samples (from compressed conversation logs). These are real false-positive candidates that the detector must NOT flag.

---

## 2. Threshold Mismatch: Tests vs Production

**This is the most significant finding.** The unit tests use a helper `make_detector()` with dramatically lower thresholds than production defaults:

| Parameter | Test Default (`make_detector`) | Production Default | Ratio |
|-----------|-------------------------------|--------------------|-------|
| `ngram_size` | 16 tokens | **128 tokens** | ×8 |
| `block_size` | 16 tokens | **128 tokens** | ×8 |
| `entropy_window` | 16 tokens | **128 tokens** | ×8 |
| `char_run_limit` | 24 chars | **70 chars** | ×3 |
| `score_threshold` | 50 points | **200 points** | ×4 |
| `min_chars` | 500 chars | **4000 chars** | ×8 |
| `batch_interval` | 1 (every call) | 1 (same) | — |

### Why This Matters

Lower thresholds mean the detector fires MORE easily in tests. While this makes it easier to verify detection works, it means:

- **Detection patterns are validated at small scale**, but the interaction between multiple signals at production-scale windows is NOT tested
- A pattern that triggers at `ngram_size=16` might behave differently at `ngram_size=128` because the sliding window captures more context
- Score accumulation dynamics differ: with threshold=50, a single detection event (e.g., +80 for sentence repetition) crosses the bar immediately. With threshold=200, multiple signals must compound — and decay between them matters

### Live Data Tests Use Different Thresholds Too

The `feed_chunks()` helper in both live data tests and regression tests uses **default production parameters** (`InnerLoopDetector()` with no overrides), BUT it sets `min_chars=0`. This bypasses the min_chars gate that's critical to production behavior.

```python
# test_inner_loop_live_data.py line 113:
det = InnerLoopDetector(min_chars=0)

# test_loop_regression.py line 70:
det = InnerLoopDetector()  # default params, but no min_chars override → uses 4000
```

Wait — the regression tests actually use `InnerLoopDetector()` with defaults (min_chars=4000), which IS correct. But the live data tests explicitly set `min_chars=0`, meaning they test detection WITHOUT the gate that prevents false positives on short text. This is a gap: **the min_chars gate's interaction with chunked streaming at production thresholds isn't validated.**

---

## 3. Missing Test Scenarios That Could Cause False Positives

### Gap 1: No Tests at Production Thresholds with Accumulation Dynamics

**Risk:** Score accumulation + decay dynamics at threshold=200 aren't tested.

At production settings, a single detection event (+80 for sentence repetition) doesn't cross the bar (200). Multiple signals must compound across several feed calls while decaying by 3% each call. No test validates this compounding behavior at realistic thresholds.

**Missing:** Test that feeds text with:
- Moderate sentence repetition (4×, scoring +80 but not enough alone)
- Slight n-gram overlap (+60)
- Low entropy window (+30)
- Across multiple feed calls with decay between them
- Verifying the score crosses 200 at the right time

### Gap 2: No Tests for Text Patterns Common in Agent Output

Real agent output has patterns not represented in tests:

| Pattern | In Tests? | Could Cause FP? |
|---------|-----------|-----------------|
| **Numbered lists** (Step 1, Step 2, ...) | Partially (filler uses this) | Low — sentences are unique |
| **Code blocks with indentation** | ❌ Not tested | Possible — repeated whitespace patterns |
| **Markdown formatting** (`**bold**`, `` `code` ``) | ❌ Not tested | Possible — repeated markers normalize to same tokens |
| **Reasoning chains** ("Let me check X. Let me verify Y.") | Partially | Moderate — similar sentence structures |
| **Self-correction loops** ("Wait, let me re-read... Actually...") | ❌ Not tested | High — agents often restart reasoning |
| **Tool call output** (JSON arrays, structured data) | ❌ Not tested | Possible — repeated JSON keys/structure |
| **Long paragraphs with anaphora** ("It is important to note that it is important...") | ❌ Not tested | Moderate — word repetition within sentences |
| **Mixed reasoning + content** (reasoning_content concatenated with content) | ✅ Live data tests cover this | Low — real data validates this |

### Gap 3: No Tests for Sentence Boundary Edge Cases

The sentence extraction regex `([^.?!]+[.?!]|[^.?!]+$)` has edge cases not tested:

| Scenario | Tested? | Risk |
|----------|---------|------|
| **Ellipsis** ("...") — no terminal punctuation captured by regex | ❌ | Moderate — trailing text accumulates in buffer |
| **Multiple punctuation** ("Wait! Really?!") | ❌ | Low — matches first boundary correctly |
| **Decimal numbers** ("3.14", "v2.0") creating false sentence breaks | ❌ | Moderate — splits at every decimal point |
| **Abbreviations** ("e.g.", "i.e.", "Mr.") | ❌ | Moderate — creates short sentences that could repeat |
| **Code with no punctuation** (entire paragraphs without `.`) | Partially | Low — handled by `[^.?!]+$` fallback |

### Gap 4: No Tests for Tokenization Edge Cases

The tokenization pipeline (`re.sub(r'\W+', ' ', sent.lower())` then `re.findall(r'\b\w+\b', norm)`) has untested behaviors:

| Scenario | Tested? | Risk |
|----------|---------|------|
| **Hyphenated words** ("well-known", "state-of-the-art") | ❌ Split into separate tokens | Low — just adds to vocabulary |
| **Numbers as tokens** ("v2.0" → "v2 0") | ❌ | Moderate — could create repeated token patterns in versioned text |
| **Mixed scripts** (English + CJK) | ✅ Unicode test exists but minimal | Low |
| **Very long sentences** (500+ words before period) | ❌ | Possible — single sentence dominates counters |

### Gap 5: No Tests for Score Decay Accumulation

The decay factor is 0.97 per feed call. Over many calls, this compounds significantly but no test verifies the actual compounding behavior at production scale:

```
After 100 feed calls: score × (0.97)^100 ≈ score × 0.48  (52% loss)
After 200 feed calls: score × (0.97)^200 ≈ score × 0.23  (77% loss)
```

**Missing:** Test that feeds text with repeated signals spread across many chunks to verify decay doesn't cause late detection or early detection.

### Gap 6: No Tests for Counter Pruning Interaction

The `_trim_counter` function prunes at 200 entries. At production scale (128-token n-grams), the counter fills faster. No test verifies that pruning doesn't discard relevant repeated patterns prematurely.

**Missing:** Test that feeds enough diverse text to trigger pruning, then repeats a pattern and verifies it's still detected despite counter trimming.

### Gap 7: No Tests for Chunking Artifacts at Production Parameters

The live data tests use `chunk_size=256` but the regression tests use `chunk_size=100`. At production parameters (ngram_size=128, block_size=128), chunk boundaries can split tokens in ways that affect detection:

**Missing:** Test that feeds text at various chunk sizes (50, 100, 256, 512) with production thresholds and verifies consistent behavior.

### Gap 8: No Tests for the "Heavy Checks" Path Only

The detector has two paths:
1. **Below min_chars**: decay only, no detection
2. **Above min_chars but not on batch_interval**: sentence check + decay
3. **Above min_chars AND on batch_interval**: full n-gram + block + entropy checks

**Missing:** Test that specifically exercises path #2 (sentence-only check) at production thresholds to verify it doesn't accumulate enough score from sentence repetition alone when there are 4–6 repeated sentences.

---

## 4. Specific Recommendations for New Tests

### Priority 1: Production-Threshold Integration Tests

```python
# Add to test_inner_loop_detect.py or a new file:

class TestProductionThresholds:
    """Tests using actual production default parameters."""

    def _make_prod_detector(self):
        return InnerLoopDetector()  # All defaults: ngram=128, block=128, threshold=200, min_chars=4000

    def test_compound_signals_cross_threshold(self):
        """Multiple moderate signals should compound to exceed threshold=200."""
        det = self._make_prod_detector()
        # Feed ~5KB of text with:
        # - 4 repeated sentences (+80 each × 2 detections = +160)
        # - Low entropy window (+30)
        # Should cross threshold=200

    def test_score_decay_over_many_chunks(self):
        """Score should decay enough over 100+ feed calls to prevent stale accumulation."""
        det = self._make_prod_detector()
        # Feed 150 chunks of varied text with slight repetition in each
        # Score should not accumulate beyond threshold

    def test_counter_pruning_preserves_detection(self):
        """After counter pruning (>200 entries), repeated patterns should still be detected."""
        det = self._make_prod_detector()
        # Feed enough diverse text to fill counters, then repeat a pattern
```

### Priority 2: Real-World Text Pattern Tests

```python
class TestRealWorldPatterns:
    """Test patterns common in actual agent output."""

    def test_numbered_list_no_fp(self):
        """Numbered lists (Step 1, Step 2, ...) should not trigger detection."""

    def test_code_block_with_indentation(self):
        """Code blocks with repeated indentation should not trigger char runs."""

    def test_markdown_formatting_repetition(self):
        """Repeated **bold** and `code` markers should not trigger n-gram detection."""

    def test_reasoning_chain_pattern(self):
        """Self-correction reasoning chains should be detected as loops when excessive."""

    def test_json_structured_output(self):
        """Tool call JSON output with repeated keys should not trigger detection."""

    def test_ellipsis_and_decimal_numbers(self):
        """Ellipsis (...) and decimal numbers (3.14) should not create false sentence boundaries."""
```

### Priority 3: Threshold Sensitivity Tests

```python
class TestThresholdSensitivity:
    """Verify that production thresholds are appropriate for real data."""

    def test_fp_rate_at_production_thresholds(self):
        """False positive rate with default params on live data should be <1%."""
        # Similar to existing test but explicitly documents the threshold being tested

    def test_chunk_size_invariance(self):
        """Detection results should not vary significantly across chunk sizes 50-512."""
```

### Priority 4: Edge Case Tests for Tokenization

```python
class TestTokenizationEdges:
    """Test sentence extraction and tokenization edge cases."""

    def test_ellipsis_handling(self):
        """Ellipsis should not create empty sentences in the counter."""

    def test_decimal_number_splits(self):
        """Numbers like 3.14 should split correctly without creating noise tokens."""

    def test_hyphenated_words(self):
        """Hyphenated words should tokenize into separate components."""

    def test_very_long_sentence(self):
        """A sentence with 500+ words before a period should be handled correctly."""
```

### Priority 5: min_chars Gate Interaction Tests

```python
class TestMinCharsGateInteraction:
    """Test the min_chars gate behavior at production thresholds (4000 chars)."""

    def test_detection_after_gate(self):
        """Loop detection should work correctly after passing the 4000-char threshold."""

    def test_char_run_below_min_chars(self):
        """Character runs should fire immediately, even below min_chars=4000."""
        # Already partially tested but not at production min_chars value

    def test_sentence_accumulation_during_gate(self):
        """Sentence counts should accumulate during the gate period and be available after."""
```

---

## 5. Quick Wins (Low Effort, High Value)

1. **Add one integration test at production defaults** — Create a single test that uses `InnerLoopDetector()` with no parameter overrides and feeds realistic-length text (~6KB). This validates the full detection pipeline at actual thresholds.

2. **Test the compound scoring path** — Feed text with moderate repetition across 3 detection categories (sentence + n-gram + entropy) to verify they sum correctly at threshold=200.

3. **Add a "no false positive on code" test** — Feed a Python function definition and verify no detection triggers. Code output is one of the most common agent outputs.

4. **Regression sample freshness check** — The 62 regression samples are from old conversation logs. Consider periodically regenerating them from recent runs to catch new patterns.

---

## Appendix: Detection Threshold Reference

| Check | Condition | Score Added | Production Gate |
|-------|-----------|-------------|-----------------|
| Char run | `> char_run_limit` (70) | Immediate return (+100) | Always active |
| Sentence repetition | Count ≥ 7 per normalized sentence | +80 each | After min_chars (4000) |
| N-gram repetition | Same 128-token window ≥ 5× | +60 each | After min_chars AND batch_interval |
| Block repetition | Same 128-token block ≥ 4× | +70 each | After min_chars AND batch_interval |
| Low entropy | Shannon < 2.0 bits in 128-token window | +30 each | After min_chars AND batch_interval |
| Score threshold | Accumulated score ≥ 200 | — Triggers detection — | — |

---

*Report generated by InnerLoopTestAnalysis on 2026-07-07.*