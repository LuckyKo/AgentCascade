# Inner Loop Detector: False Positive Reduction Plan

## Problem Statement (todo.md line 40)

Inner loop detector generates too many false positives. Only "char run" mode is reliably useful. Need tests that simulate real streaming and use existing logs to measure false positive rate.

## Evidence from Real Logs

From `logs/loop_samples/` (183 triggers across ~20 days):

| Mode | Count | Assessment |
|------|-------|------------|
| char_run | 76 | Mostly legitimate (code fences, separators) |
| max_chars | 53 | Legitimate hard limit |
| sentence_rep | 33 | Many false positives on technical prose |
| ngram_rep | 18 | False positives on repetitive analysis patterns |
| block_rep | 14 | False positives on long reasoning chains |
| entropy | 0 | Never triggered (good) |

**Key insight**: ~65 triggers from repetition modes (sentence/ngram/block) are predominantly false positives on normal verbose technical writing.

## Root Causes Identified

1. **Chunk size mismatch in tests**: Existing live-data tests use chunk_size=256, but real streaming produces 10-30 char chunks. Small chunks fragment sentences across boundaries, causing normalized fragments to accumulate counts incorrectly.

2. **Sentence threshold too aggressive for fragmented text**: `sentence_repetition_threshold=9` with effective minimum of 7 is too low when sentences are split into fragments by small chunks.

3. **One-time scoring accumulates across many patterns**: Each distinct repeated sentence/ngram scores +100/+90 once. A long technical response with many similar phrases (e.g., "Now I have a clear picture...", "Let me analyze...") can accumulate enough score to trigger without any actual loop.

4. **Activation factor allows early triggering**: `min_chars=4000` ramps detection from 0 chars, so partial activation still scores at reduced thresholds.

## Proposed Changes

### Phase 1: Realistic Streaming Simulation Tests (Primary)

Create new test module `tests/test_inner_loop_streaming_simulation.py`:

- Extract assistant messages from real logs
- Feed through detector using **realistic chunk sizes** sampled from actual streaming behavior:
  - Primary: chunk_size=20 chars (typical token-by-token streaming)
  - Secondary: chunk_size=50 chars (larger batches)
  - Tertiary: variable chunks using `random.randint(10, 40)` to simulate jitter
- Use production settings (`min_chars=4000`, not bypassed)
- Measure FP rate per detection mode separately
- Target: **< 2% overall FP rate**, with sentence/ngram/block modes each < 3%

### Phase 2: Threshold Tuning Based on Test Results

Based on empirical FP rates from Phase 1 tests, adjust thresholds:

**Conservative starting adjustments:**
- `sentence_repetition_threshold`: 9 → **15** (reduce sensitivity to fragmented similar phrases)
- `ngram_repetition_threshold`: 5 → **7** (require stronger repetition signal)
- `block_repetition_threshold`: 4 → **6** (blocks of 128 tokens repeating 4x is too aggressive)
- `score_threshold`: 350 → **450** (require more accumulated evidence)

**Keep unchanged:**
- `char_run_limit = 70` — works well, legitimate signal
- `entropy_threshold = 2.0` — never triggers currently, leave as-is
- `max_chars = 40960` — legitimate hard limit

### Phase 3: Per-Mode Reporting Enhancement

Update detection result to include which mode(s) contributed to the score, so we can:
- Track FP rates per mode in production
- Allow users to disable problematic modes via env vars (already supported, but now with data)

## Implementation Details

### File Changes

1. **New**: `tests/test_inner_loop_streaming_simulation.py` (~200 lines)
   - Reuse import pattern from `test_inner_loop_live_data.py`
   - New helper: `feed_streaming(text, chunk_sizes)` that feeds chunks from a size list
   - Test classes:
     - `TestRealisticChunkSizes`: FP rate with 10-40 char chunks
     - `TestPerModeFP`: Break down FPs by mode (sentence/ngram/block)
     - `TestCharRunStillWorks`: Verify char_run detection unaffected

2. **Modified**: `agent_cascade/settings.py`
   - Update InnerLoopSettings defaults for thresholds listed above

3. **Optional Modified**: `agent_cascade/inner_loop_detect.py`
   - Add mode tracking to returned dict (e.g., `"modes": ["sentence", "ngram"]`)

### Test Run Commands

```bash
# Run new streaming simulation tests
pytest tests/test_inner_loop_streaming_simulation.py -v

# Compare with existing live-data tests
pytest tests/test_inner_loop_live_data.py -v

# All loop-related tests together
pytest tests/test_inner_loop*.py -v
```

## Success Criteria

1. New streaming simulation tests pass with FP rate < 2% overall
2. Char run detection still catches all synthetic char run cases (regression test)
3. Existing sentence/ngram/block detection tests still pass (may need adjustment counts to match new thresholds)
4. No increase in actual loop misses (verified by checking that synthetic loop tests still trigger)

## Rollout Strategy

1. Implement tests first — run against current settings to establish baseline FP rate with realistic streaming
2. Adjust thresholds based on empirical results
3. Deploy updated settings
4. Monitor `logs/loop_samples/` for 1-2 weeks to verify reduction in repetition-mode triggers