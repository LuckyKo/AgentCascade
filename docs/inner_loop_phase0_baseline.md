# Inner Loop Audit — Phase 0 Baseline Report

**Date**: 2026-08-04  
**Auditor**: loop_baseline_analyzer  
**Purpose**: Empirical baseline before any mode removal decisions.

---

## Executive Summary

| Finding | Result |
|---------|--------|
| Total loop detections sampled (3 days) | 10 |
| char_run detections | 2 (20%) — real loops, needed |
| max_chars detections | 1 (10%) — hard limit safety net |
| ngram detections | 7 (70%) — **real semantic loops**, not caught by simpler modes |
| sentence/block/entropy detections | 0 observed in samples |
| Non-char_run/non-max_chars detections that were needed | 7/7 (100%) |
| False positives in sample set | 0 confirmed |
| Overhead of scoring modes at 40KB | ~50x vs char_run only (batch_interval=1) |

**Key conclusion**: ngram mode is catching real semantic loops that char_run + max_chars would miss. Removing it would allow agents to loop indefinitely restating the same content in slightly different wording. However, the performance cost at large outputs is significant (~750 µs/feed vs ~15 µs/feed for char_run only).

---

## Step 0.1 — Detection Distribution

Source: `workspace/logs/loop_samples/samples_2026-08-0[2-4].jsonl` (10 detections over 3 days)

| Mode | Count | Percentage |
|------|-------|------------|
| ngram | 7 | 70.0% |
| char_run | 2 | 20.0% |
| max_chars | 1 | 10.0% |
| sentence | 0 | 0% |
| block | 0 | 0% |
| entropy | 0 | 0% |

### Raw detections:

```
[2026-08-02T19:00:41] Maine                → ngram (score=450.0) — repeated explanation of pool_settings
[2026-08-02T19:00:50] Maine                → ngram (score=416.6) — same topic, rephrased
[2026-08-02T19:01:00] Maine                → ngram (score=418.6) — same topic, rephrased again
[2026-08-02T19:32:02] settings_fix_coder   → char_run '/' (71 chars) — actual garbage loop
[2026-08-03T20:28:39] kv_cache_investigator→ max_chars (60960/60960) — runaway verbose analysis
[2026-08-04T00:14:27] Maine                → char_run '/' (71 chars) — actual garbage loop
[2026-08-04T20:19:20] Maine                → ngram (score=433.1) — restating todo item
[2026-08-04T20:20:42] Maine                → ngram (score=426.4) — repeated call_agent with similar task
[2026-08-04T20:20:50] Maine                → ngram (score=450.0) — repeated call_agent variant
[2026-08-04T20:20:59] Maine                → ngram (score=426.4) — repeated call_agent variant
```

---

## Step 0.2 — Coverage Analysis: Would char_run + max_chars suffice?

### Hypothesis tested
> All non-char_run, non-max_chars detections are either false positives or would have been caught by max_chars (40KB limit) anyway.

### Result: Hypothesis REJECTED

All 7 ngram detections were **needed**:

| Detection | Text Length | Would char_run catch? | Would max_chars catch? | Assessment |
|-----------|-------------|----------------------|----------------------|------------|
| Aug-02 Maine x3 | ~500 chars each | No | No (<< 40KB) | Real loop: agent restating same explanation 3x in ~20s |
| Aug-04 Maine (todo) | 589 chars | No | No | Real loop: agent repeating todo item verbatim |
| Aug-04 Maine x3 (call_agent) | 328-468 chars each | No | No | Real loop: agent repeatedly spawning researchers with same task |

### Pattern identified: Semantic loops

The ngram mode catches **semantic repetition across separate LLM generations**, not within-text repetition. The detector's state (ngram Counter) persists across feed() calls within a single generation, accumulating evidence that the same token patterns keep appearing.

Example from Aug-04:
- Turn 1: `call_agent({"instance_name":"loop_auditor_1","task":"Perform a full audit..."})`
- Turn 2: `call_agent({"instance_name":"loop_audit_researcher","task":"Investigate the inner loop detection system..."})`
- Turn 3: `call_agent({"instance_name":"loop_auditor","task":"Perform a full audit of the inner loop detection modes..."})`

Same semantic action repeated with slight rewording. char_run sees no issue. max_chars would allow ~8K tokens of this looping before triggering. ngram catches it at <1KB per generation.

### Verdict on each mode's necessity:

| Mode | Observed detections | Needed? | Reasoning |
|------|--------------------|---------|-----------|
| char_run | 2 | **YES** | Catches garbage loops (///////) instantly, zero overhead |
| max_chars | 1 | **YES** | Hard safety net for verbose runaway output |
| ngram | 7 | **YES** | Only mode catching semantic loops. All 7 detections were real loops caught well before max_chars would trigger |
| sentence | 0 | Unclear | May be redundant with ngram; no observed detections in 3 days |
| block | 0 | Unclear | Larger-window version of ngram; may be redundant |
| entropy | 0 | Unclear | May catch degenerate output patterns not seen in samples |

---

## Step 0.3 — Performance Benchmark

### Methodology
- Realistic streaming chunks: ~50 chars per feed() call
- Text: mixed prose + code-like content (similar to agent outputs)
- Runs: 3 iterations averaged
- Environment: same machine as production, Python sandbox

### Results: Latency per feed() call

| Size | All modes (batch=1) | All modes (batch=5) | char_run + sentence | char_run only |
|------|--------------------|--------------------|---------------------|---------------|
| 1 KB | 14.4 µs/feed | 14.2 µs/feed | 12.4 µs/feed | 11.3 µs/feed |
| 10 KB | 54.4 µs/feed | 50.7 µs/feed | 16.1 µs/feed | 14.0 µs/feed |
| 40 KB | 750.9 µs/feed | 155.1 µs/feed | 18.4 µs/feed | 14.5 µs/feed |

### Overhead analysis

| Comparison | At 1KB | At 10KB | At 40KB (worst case) |
|------------|--------|---------|---------------------|
| All modes vs char_run only | +2.9 µs/feed | +40.4 µs/feed | **+736.4 µs/feed** (~50x) |
| char_run+sentence vs char_run only | +1.1 µs/feed | +2.1 µs/feed | +3.9 µs/feed (negligible) |
| All modes vs batch=5 | +0.2 µs/feed | +3.7 µs/feed | **+595.8 µs/feed** |

### Key observations:

1. **batch_interval=1 is the problem**: With default settings, heavy checks (ngram/block sliding windows) run on EVERY feed call. At 40KB that's ~820 feeds, each rescanning token windows.

2. **char_run + sentence is cheap**: Adding only sentence detection over char_run adds <4 µs/feed even at 40KB. The sentence mode uses simple Counter lookups, not sliding windows.

3. **ngram/block are O(n²) at scale**: The sliding window over the token buffer (up to 1000 tokens) with incremental scanning still degrades badly as more tokens accumulate. At 40KB (~8K tokens), only ~1000 fit in the buffer, but the scan range grows with each feed.

4. **batch_interval=5 is a sweet spot**: Reduces 40KB latency from 751 µs to 155 µs per feed while still running heavy checks frequently enough to catch loops.

---

## Recommendations for Phase 1

Based on Phase 0 data:

### Safe to consider removing:
- **block mode**: Zero observed detections in 3 days; ngram (smaller window) catches the same patterns. Block is likely redundant.
- **entropy mode**: Zero observed detections; may be useful as a theoretical safety net but no evidence it's needed in practice.

### Should keep (with optimization):
- **ngram mode**: All 7 of its detections were real loops that char_run + max_chars would miss. However, consider:
  - Increasing batch_interval from 1 to 3-5 (reduces overhead ~5x at large outputs)
  - Or gating it behind min_chars more aggressively

### Should definitely keep:
- **char_run**: Zero overhead, catches real garbage loops instantly.
- **max_chars**: Hard safety net for verbose runaway output.
- **sentence mode**: Cheap to run (<4 µs overhead), may catch patterns ngram misses.

### Quick wins (low-risk optimizations):
1. Set `default_batch_interval = 5` instead of 1 — cuts worst-case overhead from ~750 µs/feed to ~155 µs/feed
2. Remove block mode — zero detections, redundant with ngram
3. Consider removing entropy mode — zero detections, adds per-feed computation

### What we need before final decisions:
- Longer sample period (current data is only 3 days, 10 detections)
- Controlled test: disable ngram temporarily and observe if agents loop more often
- Review whether sentence mode actually contributes unique detections or overlaps with ngram

---

## Appendix: Default settings reference

From `agent_cascade/settings.py` (InnerLoopSettings):

```python
max_counter_entries = 200          # Max entries per Counter before pruning
max_tokens = 1000                  # Max tokens in sliding window
default_min_chars = 4000           # Min chars before full detection
default_batch_interval = 1         # Heavy checks every N-th feed (CURRENTLY 1)
default_max_chars = 40960          # Hard limit (~8K tokens)
ngram_size = 64                    # Token window for n-gram repetition
block_size = 128                   # Token window for block repetition
entropy_window = 128               # Token window for entropy calculation
char_run_limit = 70                # Max consecutive identical chars
score_threshold = 350              # Cumulative score to trigger
sentence_repetition_threshold = 15 # Sentence count to flag
ngram_repetition_threshold = 7     # N-gram count to flag
block_repetition_threshold = 6     # Block count to flag
entropy_threshold = 2.0            # Shannon entropy below which loop suspected
```