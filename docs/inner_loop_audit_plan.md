# Inner Loop Mode Audit Plan (todo.md #38)

**Status:** Implementation-ready — reviewer findings addressed, algorithm finalized  
**Author:** loop_audit_planner, updated by loop_plan_updater  
**Date:** 2026-08-05  
**Review revision:** 4 (all critical/major/minor findings from final review resolved)

## Objective

Audit all inner-loop detection modes and implement a new two-phase approach for semantic loop detection: lightweight heuristic suspicion followed by exact match confirmation. Char run and max_chars remain unchanged as last line of defense.

---

## 1. System Overview

Two independent loop detection systems exist:

| System | File | Scope | Active by Default |
|--------|------|-------|-------------------|
| **System A** (Turn-level) | `agent_cascade/loop_detection.py` | Detects repeating turn sequences across the conversation history | YES — called every turn in `execution_engine.py:2083` |
| **System B** (Streaming text) | `agent_cascade/inner_loop_detect.py` | Detects repetition within a single LLM response during streaming | NO — gated by `pool.settings.inner_loop_detect_enabled` (default False) |

### System A Modes (1 mode, deterministic)

- **Sequence pattern detection** (`detect_loop()`, lines 48–187): Extracts features from recent messages and looks for length-L patterns repeating K times.
  - L < 3 → requires K = 3 repetitions
  - 3 ≤ L < 5 → requires K = 3 repetitions  
  - L ≥ 5 → requires K = 2 repetitions
  - Feature extraction includes role, content hash (for FUNCTION messages), and tool name/arguments
  - **Deterministic:** exact pattern matching, no scoring

### System B Modes — Current State (5 modes, mostly scoring-based)

| Mode | Setting Toggle | Default Enabled | Mechanism | Score |
|------|---------------|-----------------|-----------|-------|
| **Character run** | `char_run_enabled` | YES (`QWEN_AGENT_LOOP_CHAR_RUN`) | Deterministic: 70 identical chars → immediate return | +100 (immediate) |
| **Sentence repetition** | `sentence_rep_enabled` | YES (`QWEN_AGENT_LOOP_SENTENCE_REP`) | Scoring: 15 occurrences → +100 (one-time per sentence) | Accumulates to threshold 350 |
| **N-gram repetition** | `ngram_rep_enabled` | YES (`QWEN_AGENT_LOOP_NGRAM_REP`) | Scoring: 7 occurrences of 64-token window → +90 (one-time per n-gram) | Accumulates to threshold 350 |
| **Block repetition** | `block_rep_enabled` | YES (`QWEN_AGENT_LOOP_BLOCK_REP`) | Scoring: 6 occurrences of 128-token window → +100 (one-time per block) | Accumulates to threshold 350 |
| **Entropy collapse** | `entropy_collapse_enabled` | YES (`QWEN_AGENT_LOOP_ENTROPY`) | Scoring: Shannon entropy < 2.0 bits in 128-token window → +50 (one-time gate) | Accumulates to threshold 350 |

### System B Modes — Target State (after this plan)

| Mode | Status | Mechanism |
|------|--------|-----------|
| **Character run** | KEEP unchanged | Deterministic: 70 identical chars → immediate return |
| **Max chars guard** | KEEP unchanged | Hard length limit (~40KB default) |
| **Two-phase semantic loop detection** | NEW | Suspicion (lightweight ngram heuristic) → Confirmation (exact match count) → Abort or cooldown |
| Sentence repetition | REMOVE | Replaced by two-phase approach |
| N-gram repetition (scoring) | REMOVE | Heuristic component reused in suspicion phase, scoring removed |
| Block repetition | REMOVE | Replaced by two-phase approach |
| Entropy collapse | REMOVE | Replaced by two-phase approach |

---

## 2. Mode Value Assessment (FINAL DECISIONS)

**User direction (confirmed):**
- Char run and max size are last line of defense — DO NOT change them
- "char run + max size catch pretty much all cases, not fast but they do eventually" — treat as baseline assumption
- Implement new two-phase approach: heuristic suspicion → exact match confirmation for semantic loops
- Remove all scoring-based modes (sentence, ngram, block, entropy)

### Phase 0 Findings Reference

Phase 0 baseline analysis (`inner_loop_phase0_baseline.md`) examined 10 real loop detections over 3 days:

| Mode | Detections | Assessment |
|------|------------|------------|
| char_run | 2 (20%) | Real garbage loops, needed |
| max_chars | 1 (10%) | Hard limit safety net |
| ngram | 7 (70%) | **Real semantic loops** — agent restating same content with slight rewording |
| sentence/block/entropy | 0 | No observed detections |

**Critical finding:** All 7 ngram detections were real semantic loops that char_run + max_chars would NOT catch. The ngram heuristic correctly identified repetition patterns, but its scoring-based triggering was probabilistic rather than confirmatory.

**Design implication:** Keep a lightweight ngram-like heuristic as the suspicion trigger (it catches real loops), but add an exact match confirmation step to eliminate false positives and remove scoring accumulation entirely.

### System A: Sequence Pattern Detection

**Verdict: KEEP** — essential for turn-level loop detection.

**Justification:**
- Only system that performs surgical conversation rollback (via `execution_engine.py:2097`)
- Feature extraction with content hashing on FUNCTION messages correctly differentiates same-tool-different-args scenarios
- No equivalent in System B (which only sees streaming text, not turn structure)
- Already deterministic (exact pattern matching, no scoring) — aligns with user direction

### System B Modes

#### 2.1 Character Run Detection

**Verdict: KEEP — DO NOT CHANGE.** Last line of defense.

**Justification:**
- Confirmed reliable in production (todo.md line 46: "char run is the only good mode")
- Detects degenerate generation like "///////..." during streaming
- Deterministic: immediate return at 70 identical chars, no scoring involved
- Triggers API fallback cursor rotation (via `_handle_inner_loop_detection`)

**Risk if removed:** Degenerate character loops would continue until max_chars (40KB) hit.

#### 2.2 Sentence Repetition

**Verdict: REMOVE.** Scoring-based guessing mechanism, replaced by two-phase approach.

**Justification:**
- Uses cumulative scoring (+100 per sentence, decay 0.97/cycle). Requires score ≥350 threshold — probabilistic guessing, not confirmation.
- Known FP source: similar technical phrases in code explanations and documentation writing.
- Two-phase approach provides heuristic suspicion + exact confirmation instead of fuzzy scoring.

#### 2.3 N-gram Repetition (64-token window)

**Verdict: REMOVE as scoring mode.** Heuristic signal reused in new two-phase suspicion detector.

**Justification:**
- Phase 0 showed all 7 ngram detections were real semantic loops — the heuristic works, but scoring is wrong approach.
- New design: use same 64-token window as lightweight frequency tracker (suspicion phase), then confirm with exact match counting instead of accumulating +90 points.

#### 2.4 Block Repetition (128-token window)

**Verdict: REMOVE.** Scoring-based guessing mechanism, replaced by two-phase approach.

**Justification:**
- Zero observed detections in Phase 0 sample period.
- Uses cumulative scoring (+100 per block). Threshold-based guessing, not confirmation.
- Confirmation phase of new design uses exact byte-level comparison which is more reliable than block scoring.

#### 2.5 Entropy Collapse

**Verdict: REMOVE.** Weakest signal, scoring-based, highest FP risk, replaced by two-phase approach.

**Justification:**
- Shannon entropy < 2.0 is a soft heuristic, not confirmation of looping.
- Code blocks and repetitive technical content can trigger false positives.
- Zero observed detections in Phase 0 sample period.

### New Mode: Two-Phase Heuristic + Confirmation

**Verdict: IMPLEMENT.** Replaces all scoring-based modes with deterministic confirmation.

**Justification:**
- Phase 0 proved ngram heuristic catches real semantic loops (7/7 detections were true positives).
- Scoring was the weak link — fuzzy accumulation without confirmation of actual repetition.
- Two-phase design keeps the working heuristic signal while adding exact match confirmation to prevent false positives.
- Cooldown mechanism handles cases where heuristic misfires on non-looping content.

### Final Summary Table

| Mode | Verdict | Reason |
|------|---------|--------|
| System A sequence detection | KEEP | Deterministic pattern matching, unique turn-level scope |
| Char run (System B) | KEEP — DO NOT CHANGE | Last line of defense, deterministic, confirmed reliable |
| Max chars / Max tokens (System B) | KEEP — DO NOT CHANGE | Last line of defense, length safety nets |
| Sentence repetition (System B) | REMOVE | Scoring-based guessing; replaced by two-phase approach |
| N-gram repetition scoring (System B) | REMOVE | Heuristic signal kept in suspicion phase; scoring removed |
| Block repetition (System B) | REMOVE | Scoring-based; replaced by two-phase approach |
| Entropy collapse (System B) | REMOVE | Weakest signal, FP-prone, scoring-based; replaced by two-phase approach |
| Two-phase semantic loop detection | NEW | Lightweight suspicion + exact confirmation replaces all scoring modes |

**After changes:** System B will consist of:
1. Char run detection (unchanged, last line of defense)
2. Max chars guard (unchanged, last line of defense)
3. Two-phase semantic loop detection (new primary detector for real loops)

---

## 3. Design: Two-Phase Loop Detection

### Overview

The two-phase approach separates "this looks suspicious" from "this is confirmed looping":

1. **Suspicion phase:** Lightweight ngram-like frequency tracker flags potential loops and estimates the loop interval length. No scoring accumulation, no threshold-based aborting.
2. **Confirmation phase:** When suspicion threshold is hit, extract suspected loop interval and perform exact byte-level comparison of tail segments to count confirmed repetitions.
3. **Decision:** If enough exact matches → CONFIRMED loop, trigger abort. If not enough → heuristic misfired, apply cooldown.

### Algorithm Specification

#### Data Structures

class TwoPhaseLoopDetector:
    def __init__(self):
        # Suspicion phase state
        self.ngram_window_size = 64       # Token window size (same as current ngram mode)
        self.suspicion_threshold = 7      # N-gram must appear this many times to trigger suspicion (raised from 5 per production tuning — 5 was too aggressive on technical content)
        self.ngram_counter: Counter[tuple] = Counter()    # Frequency of each n-gram (tuple key, deterministic)
        self.ngram_positions: dict[tuple, list[int]] = {} # Track char positions where each n-gram appears (bounded to last ~8 per entry)
        self.max_counter_entries = 200    # Prune threshold for counter
        
        # Persistent token buffer — accumulates across feeds, bounded
        self.token_buffer: list[str] = [] # All tokens seen so far (trimmed when large)
        self.max_token_buffer = 5000      # Allows detecting loops with intervals up to ~700 tokens repeating 7 times. Very long interval loops (>2KB) caught by max_chars eventually.
        self.last_scan_pos = 0            # Incremental scan pointer — only rescan new regions
        
        # Confirmation phase state
        self.confirmed_matches_required = 3  # Minimum exact repetitions to confirm loop
        self.cooldown_active = False
        self.cooldown_remaining_feeds = 0
        self.cooldown_duration = 50       # Suppress suspicion for K feeds after failed confirmation
        
        # Tail buffer for exact comparison — no truncation needed (detector is per-response, max_chars limits total)
        self.tail_buffer: str = ""        # Accumulated text across feeds within one response
        # NOTE: No max_tail_length truncation — detector resets per LLM call via reset(), and max_chars (~40KB) caps total output.
        # Truncation would break position tracking without providing safety benefit.
        
        # Interval tracking
        self.last_suspicion_interval: int | None = None  # Suspected loop length in chars
        
        # Feature flag — gated for safe rollout
        self.two_phase_enabled = os.environ.get("QWEN_AGENT_LOOP_TWO_PHASE_ENABLED", "0") == "1"

#### Supporting Types and Helpers

**Suspicion dataclass:**
```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Suspicion:
    interval_length: int       # Estimated loop interval in characters (≥1 if reliable, -1 if not)
    dominant_ngram: tuple[str, ...]  # The n-gram tuple that triggered suspicion
```

**tokenize_chunk(text):** Simple whitespace split with punctuation stripping.
```python
import re

def tokenize_chunk(text: str) -> list[str]:
    """Split text into tokens on whitespace, strip leading/trailing punctuation, filter empty."""
    tokens = text.split()
    result = []
    for t in tokens:
        # Strip common punctuation from edges
        cleaned = t.strip(".,!?;:'\"()[]{}<>")
        if cleaned:
            result.append(cleaned.lower())  # Normalize case for matching
    return result
```

**_prune_counters():** Keep top N entries by count.
```python
def _prune_counters(self):
    """Keep only the most frequent n-grams to bound memory usage."""
    if len(self.ngram_counter) <= self.max_counter_entries:
        return
    
    # Keep top N entries by count
    top_items = self.ngram_counter.most_common(self.max_counter_entries)
    kept_keys = {item[0] for item in top_items}
    
    # Prune both counter and positions
    self.ngram_counter = Counter(dict(top_items))
    self.ngram_positions = {k: v for k, v in self.ngram_positions.items() if k in kept_keys}
```

**_count_exact_repetitions(suspicion):** Same logic as _confirm_loop but returns count.
```python
def _count_exact_repetitions(self, suspicion: Suspicion) -> int:
    """Count exact repetitions of the suspected interval in tail buffer."""
    interval = suspicion.interval_length
    if interval < 1 or len(self.tail_buffer) < interval * self.confirmed_matches_required:
        return 0
    
    candidate_segment = self.tail_buffer[-interval:]
    count = 1
    pos = len(self.tail_buffer) - interval
    
    while pos >= interval:
        if self.tail_buffer[pos - interval:pos] == candidate_segment:
            count += 1
            pos -= interval
        else:
            break
    
    return count
```

#### Phase 1: Suspicion Detection (runs on each feed)

**Input:** New text chunk from streaming response  
**Output:** `None` or `Suspicion(interval_length, dominant_ngram_tuple)`

```python
def _check_suspicion(self, new_text: str) -> Suspicion | None:
    """Lightweight heuristic: track n-gram frequencies over persistent token buffer, flag when pattern repeats."""
    
    # Skip if cooldown active
    if self.cooldown_active:
        self.cooldown_remaining_feeds -= 1
        if self.cooldown_remaining_feeds <= 0:
            self.cooldown_active = False
        return None
    
    # Update tail buffer — no truncation needed (see data structures note)
    self.tail_buffer += new_text
    
    # Tokenize new chunk and append to persistent token buffer
    new_tokens = tokenize_chunk(new_text)  # Simple whitespace/punctuation split
    self.token_buffer.extend(new_tokens)
    
    # Trim token buffer if too large (keep tail portion where loops would appear)
    if len(self.token_buffer) > self.max_token_buffer:
        self.token_buffer = self.token_buffer[-self.max_token_buffer:]
    
    # Slide ngram window over the END of token_buffer (where new tokens are)
    # Only scan recently added region to avoid re-scanning everything each feed
    start_scan = max(0, len(self.token_buffer) - len(new_tokens) - self.ngram_window_size)
    
    for i in range(start_scan, max(start_scan + 1, len(self.token_buffer) - self.ngram_window_size + 1)):
        window = tuple(self.token_buffer[i:i + self.ngram_window_size])
        if len(window) < self.ngram_window_size:
            continue
        
        # Use tuple directly as key — deterministic, no hash randomization issues
        self.ngram_counter[window] += 1
        
        # Track position in tail_buffer where this n-gram ends (for interval estimation)
        # Bounded: keep only last ~8 positions per entry to limit memory
        if window not in self.ngram_positions:
            self.ngram_positions[window] = []
        
        pos_list = self.ngram_positions[window]
        pos_list.append(len(self.tail_buffer))
        if len(pos_list) > 8:
            pos_list[:] = pos_list[-8:]  # Trim to last 8 positions
    
    # Prune counters if too large (keep top entries by count)
    if len(self.ngram_counter) > self.max_counter_entries:
        self._prune_counters()
    
    # Check if any n-gram has hit suspicion threshold
    for window, count in self.ngram_counter.most_common(5):
        if count >= self.suspicion_threshold:
            # Estimate loop interval from position tracking
            interval = self._estimate_interval(window)
            
            # Only trigger suspicion if interval is reliable (>=1 means consistent gaps)
            if interval >= 1:
                return Suspicion(interval_length=interval, dominant_ngram=window)
    
    return None
```

**Key properties:**
- NO scoring accumulation — just raw frequency counts
- NO threshold-based triggering — suspicion is passed to confirmation phase
- Persistent token_buffer ensures ngrams spanning feed boundaries are detected
- Tuple keys are deterministic (no Python hash randomization issues across runs)
- Same 64-token window that Phase 0 proved catches real semantic loops
- Position lists bounded to last ~8 entries per ngram (enough for median of ≤7 gaps)
- Only triggers suspicion when interval estimate is reliable (consistency gate passed)

#### Interval Estimation Algorithm (_estimate_interval)

**Purpose:** When a suspicious n-gram is found, estimate the loop interval length by analyzing distances between its occurrences.

```python
def _estimate_interval(self, ngram_window: tuple[str, ...]) -> int:
    """Estimate loop interval from positions where this n-gram appears.
    
    Algorithm: compute distances between consecutive occurrences, return median distance IF consistent.
    Median is robust against outliers (e.g., one occurrence far apart due to preamble text).
    Consistency gate ensures gaps are periodic (actual loop) not just repetitive content.
    """
    positions = self.ngram_positions.get(ngram_window, [])
    
    # Need at least 4 gaps (5+ occurrences) for reliable interval estimate
    if len(positions) < 5:
        return -1  # Not enough data for reliable interval
    
    # Compute distances between consecutive occurrences
    distances = []
    for i in range(1, len(positions)):
        d = positions[i] - positions[i - 1]
        if d > 0:  # Guard against zero/negative (shouldn't happen but be safe)
            distances.append(d)
    
    if len(distances) < 4:
        return -1  # Not enough gaps
    
    # Return median distance — robust estimate of interval length
    distances.sort()
    mid = len(distances) // 2
    
    if len(distances) % 2 == 0:
        median = (distances[mid - 1] + distances[mid]) // 2
    else:
        median = distances[mid]
    
    # Consistency gate: dominant gap must represent ≥60% of all gaps
    # This distinguishes periodic loops from merely repetitive content
    from collections import Counter as GapCounter
    gap_counts = GapCounter(distances)
    most_common_gap, most_common_count = gap_counts.most_common(1)[0]
    dominant_ratio = most_common_count / len(distances)
    
    if dominant_ratio < 0.6:
        return -1  # Gaps too inconsistent — repetitive but not periodic
    
    # Clamp to reasonable bounds (max interval = 1/3 of available tail, ensuring confirmation can verify 3 reps)
    max_interval = len(self.tail_buffer) // 3 if self.tail_buffer else 10000
    return max(1, min(median, max_interval))
```

**Design notes:**
- Median (not mean) avoids being skewed by one outlier distance
- Positions tracked as char offsets in tail_buffer where each n-gram occurrence ends; bounded to last ~8 per entry
- Requires ≥5 occurrences and ≥4 gaps before estimating interval (fewer gaps → unreliable median)
- Consistency gate (dominant-gap ratio ≥0.6) distinguishes periodic loops from repetitive-but-non-periodic content
- Lower bound of 1 prevents degenerate zero-length intervals; -1 means "interval not reliable yet"

#### Phase 2: Confirmation (runs only when suspicion raised)

**Input:** `Suspicion(interval_length, dominant_ngram_tuple)`  
**Output:** `True` (confirmed loop) or `False` (heuristic misfire)

```python
def _confirm_loop(self, suspicion: Suspicion) -> bool:
    """Exact match test: count how many times the suspected interval repeats in tail."""
    
    interval = suspicion.interval_length
    
    # Validate interval is reasonable (max interval = 1/3 of available tail for confirmation)
    max_interval = len(self.tail_buffer) // self.confirmed_matches_required
    if interval < 1 or interval > max_interval:
        return False  # Interval too small or too large to confirm with available buffer
    
    # Need enough tail to check for multiple repetitions
    min_tail_needed = interval * self.confirmed_matches_required
    if len(self.tail_buffer) < min_tail_needed:
        return False  # Not enough data yet, let suspicion accumulate more
    
    # Extract the suspected repeating segment from the end of tail
    candidate_segment = self.tail_buffer[-interval:]
    
    # Count exact repetitions moving backward through tail
    confirmed_count = 1  # We have at least one (the tail itself)
    pos = len(self.tail_buffer) - interval
    
    while pos >= interval:
        prev_segment = self.tail_buffer[pos - interval:pos]
        
        if prev_segment == candidate_segment:
            confirmed_count += 1
            pos -= interval
        else:
            break  # Exact match chain broken
    
    return confirmed_count >= self.confirmed_matches_required
```

**Key properties:**
- EXACT byte-level string comparison — no fuzzy matching, no scoring
- Counts consecutive repetitions of the suspected interval in tail buffer
- Only triggers abort when `confirmed_count >= confirmed_matches_required` (default: 3)
- This is the deterministic confirmation step that scoring modes lacked

#### Cooldown Mechanism (runs when confirmation fails)

```python
def _apply_cooldown(self):
    """Suppress suspicion detection after failed confirmation to prevent repeated false triggers.
    
    MANDATORY: Always reset all tracking state on cooldown — stale data causes re-triggering
    on the same content that already failed confirmation.
    """
    self.cooldown_active = True
    self.cooldown_remaining_feeds = self.cooldown_duration  # Default: 50 feeds
    
    # Mandatory reset of all suspicion tracking state
    self.ngram_counter.clear()
    self.ngram_positions.clear()
```

**Rationale:** If the heuristic flagged something but exact confirmation failed, it means similar-but-not-identical content appeared (e.g., agent discussing related topics). Suppressing suspicion briefly prevents noisy re-triggering on the same content. Clearing counters ensures we don't accumulate stale evidence from already-dismissed suspicions.

#### Main Feed Integration (with explicit priority order)

The `InnerLoopDetector.feed()` method checks in this EXACT order. First match wins:

```python
def feed(self, new_text: str) -> dict | None:
    """Main entry point called during streaming.
    
    Priority order (first match wins):
    1. max_chars check → abort if total chars exceeded (hard safety net)
    2. char_run check → abort if 70+ identical consecutive chars (degenerate loops)
    3. two-phase suspicion + confirmation → abort only if confirmed exact repetitions
    """
    
    # PRIORITY 1: Max chars — hard limit, always checked first
    self.total_chars += len(new_text)
    if self.total_chars > self.max_chars:
        return {
            "type": "max_chars",
            "reason": f"Max chars exceeded: {self.total_chars}/{self.max_chars}",
        }
    
    # PRIORITY 2: Char run — degenerate character loops, immediate detection
    char_run_result = self._check_char_run(new_text)
    if char_run_result is not None:
        return char_run_result
    
    # PRIORITY 3: Two-phase semantic loop detection (gated by feature flag)
    if self.two_phase_enabled:
        # Phase 1: Check for suspicion (skipped during cooldown)
        suspicion = self._check_suspicion(new_text)
        
        if suspicion is not None:
            # Phase 2: Attempt confirmation (exact match test)
            confirmed = self._confirm_loop(suspicion)
            
            if confirmed:
                # CONFIRMED LOOP — trigger abort
                return {
                    "type": "semantic_loop",
                    "reason": f"Confirmed semantic loop: {suspicion.interval_length} chars repeating",
                    "confirmed_repetitions": self._count_exact_repetitions(suspicion),
                }
            else:
                # Confirmation failed — heuristic misfired, apply cooldown
                self._apply_cooldown()
    
    return None  # No loop detected
```

**Priority rationale:**
1. `max_chars` first because it's a hard contract — never allow output beyond this limit regardless of content
2. `char_run` second because degenerate loops (///////) should abort immediately without waiting for suspicion accumulation
3. Two-phase last because it requires evidence gathering and confirmation — only triggers on confirmed exact repetitions

### Parameter Tuning Guidance

| Parameter | Default | Rationale | Tunable via env var? |
|-----------|---------|-----------|---------------------|
| `ngram_window_size` | 64 | Same as current ngram mode; Phase 0 proved this catches semantic loops | No (fixed) |
| `suspicion_threshold` | 7 | N-gram must appear 7+ times before suspicion (raised from 5 per production tuning — 5 was too aggressive on technical content) | Yes (`QWEN_AGENT_LOOP_SUSPICION_THRESHOLD`) |
| `confirmed_matches_required` | 3 | Need 3 exact repetitions to confirm loop — prevents abort on coincidental similarity | Yes (`QWEN_AGENT_LOOP_CONFIRM_REQUIRED`) |
| `cooldown_duration` | 50 feeds | ~2500 chars of suppression; enough to skip past related-but-different content | Yes (`QWEN_AGENT_LOOP_COOLDOWN_FEEDS`) |
| `max_token_buffer` | 5000 | Allows detecting loops with intervals up to ~700 tokens; very long interval loops (>2KB) caught by max_chars | No (fixed) |

### False Positive Guards

Per research findings, these guards prevent false positives on technical content:

1. **Raised suspicion threshold (7 vs original 5):** Production tuning showed 5 was too aggressive on technical writing with repeated identifiers, JSON keys, markdown headers.
2. **Consistency gate (dominant-gap ratio ≥0.6):** Distinguishes periodic loops from merely repetitive-but-non-periodic content like bulleted lists or code examples.
3. **Confirmation phase exact match:** Even if suspicion fires, abort only happens when exact byte-level repetitions are confirmed — similar phrasing without exact repetition will not trigger.
4. **Cooldown after failed confirmation:** Prevents noisy re-triggering on the same content that already failed exact match verification.

### Interaction with Existing System B Components

- **Char run detection:** Runs independently, unchanged. Two-phase detector runs in parallel. Char run still has priority for immediate degenerate loops.
- **Max chars guard:** Runs independently, unchanged. Two-phase detector is an early-warning system; max_chars remains the hard limit.
- **Scoring system:** Entirely removed. No more `score`, `threshold`, `decay`, or `add_score()`.

---

## 4. System A vs B Interaction

### Priority Order

1. **System A (turn-level)** runs every turn in `execution_engine.py:2083`. If it detects a loop, it performs inline rollback + hint injection immediately. This is the primary loop defense for multi-turn patterns.

2. **System B (streaming)** runs during LLM streaming if enabled (`pool.settings.inner_loop_detect_enabled`). If it detects a loop mid-stream, it aborts the stream and triggers API cursor rotation via `_handle_inner_loop_detection`. This catches degenerate generation within a single response.
   - Char run: immediate abort on 70 identical chars
   - Two-phase semantic detection: abort only after exact match confirmation

### Interaction Rules

- System A operates on completed turns; System B operates on incomplete streaming output. They do not conflict.
- After compression operations, System A is suppressed for one turn via `_suppress_loop_detection_next_turn` flag (see section 5). System B's two-phase detector has its own cooldown mechanism for false suspicions.
- If both systems detect loops in the same session: System B fires first (mid-stream abort), then if the agent retries and gets stuck in a turn-level pattern, System A catches it on the next completed turn.

### Cooldown Flag Behavior

- `_suppress_loop_detection_next_turn` is set by compression handler after any compression operation (`compression/handler.py`: lines 628, 723, 989, 1176)
- Checked at start of System A detection in `execution_engine.py:2082`
- Cleared immediately after being honored (skipped turn) in `execution_engine.py:2122`
- Purpose: compression concentrates patterns that can trigger false positives; one-turn cooldown prevents this
- Thread safety: Python GIL ensures atomic reads/writes for simple boolean attributes

---

## 5. Required Test Additions

### 5.1 System A Tests (loop_detection.py)

#### Test: Tail-repeat pattern [A,B,C,D,D,D] SHOULD trigger

**File:** `tests/test_loop_detection.py`  
**Class:** New: `TestDetectLoopTailRepeatPattern`  
**Test name:** `test_tail_repeat_pattern_should_detect`

**Scenario:** Agent gets stuck repeating the same turn at end of conversation.

```python
# Pattern: unique turns A,B,C followed by D repeating 3+ times
msgs = [
    _msg(USER, "write a function"),
    _msg(ASSISTANT, "sure, let me do that"),
    _msg(FUNCTION, "code_interpreter result 1"),
    _msg(ASSISTANT, "I think I found the issue"),  # D starts here
    _msg(FUNCTION, "read_file same_file"),
    _msg(ASSISTANT, "I think I found the issue"),  # D repeated
    _msg(FUNCTION, "read_file same_file"),
    _msg(ASSISTANT, "I think I found the issue"),  # D repeated again
]
result = detect_loop(msgs)
assert result is not None, "Tail-repeat pattern should be detected"
```

**Expected:** Detects L=2 pattern [ASSISTANT:"...", FUNCTION:"read_file..."] repeating K=3 times.

#### Test: Same tool different args [A,D,B,C,D,E,D] should NOT trigger

**File:** `tests/test_loop_detection.py`  
**Class:** New: `TestDetectLoopSameToolDifferentArgs`  
**Test name:** `test_same_tool_different_args_not_loop`

**Scenario:** Agent uses same tool (e.g., grep) with different arguments — this is normal exploration, not a loop.

```python
msgs = [
    _msg(USER, "find all usages of function X"),
    _msg(ASSISTANT, "let me search for it"),
    _dict_msg(FUNCTION, "grep found: file1.py line 5", name="grep"),  # D - grep result 1
    _msg(ASSISTANT, "now checking related files"),
    _dict_msg(FUNCTION, "read_file content of file2.py", name="read_file"),  # B
    _msg(ASSISTANT, "let me also check config"),
    _dict_msg(FUNCTION, "grep found: config.yaml line 12", name="grep"),  # D - grep result 2 (DIFFERENT)
    _msg(ASSISTANT, "checking imports"),
    _dict_msg(FUNCTION, "read_file content of file3.py", name="read_file"),  # E
    _msg(ASSISTANT, "one more search"),
    _dict_msg(FUNCTION, "grep found: utils.py line 89", name="grep"),  # D - grep result 3 (DIFFERENT)
]
result = detect_loop(msgs)
assert result is None, "Same tool with different args should not be a loop"
```

**Expected:** FUNCTION feature extraction includes content hash (`loop_detection.py:118`), so each unique grep result produces a different feature string. No pattern match.

#### Test: Cooldown flag behavior after compression

**File:** `tests/test_execution_engine_loop_cooldown.py` (NEW FILE)  
**Class:** New: `TestLoopDetectionCooldownAfterCompression`  
**Test name:** `test_suppress_flag_skips_detection_after_compression`

**Scenario:** Verify `_suppress_loop_detection_next_turn` correctly prevents false positives post-compression.

```python
# Integration test mocking execution_engine flow
instance._suppress_loop_detection_next_turn = True
# Call engine's _check_and_handle_loop() — should return False (no action taken)
result = engine._check_and_handle_loop(instance, messages, ...)
assert result == False, "Should skip loop detection when suppress flag is set"
assert instance._suppress_loop_detection_next_turn == False, "Flag should be cleared after honored"
```

**Specifics:** New file because this tests execution_engine behavior, not detect_loop() directly. Requires mocking AgentInstance and message lists.

### 5.2 System B Tests — Char Run (unchanged)

#### Test: Char run only — non-loop streaming text should NOT trigger

**File:** `tests/test_inner_loop_detect.py`  
**Class:** New: `TestCharRunOnlyModeFalsePositives`  
**Test name:** `test_repeated_tool_names_not_char_run`

**Scenario:** Agent writes code that mentions the same function/file names multiple times naturally — no actual character run.

```python
detector = InnerLoopDetector(char_run_enabled=True)
# Disable two-phase detector for this test
detector._two_phase_enabled = False

text_chunks = [
    "Let me check the read_file function. The read_file tool supports",
    "truncation via spillover files. When read_file encounters a large",
    "file it writes the full content to a spillover path and returns",
    "a truncated version with a link. The read_file implementation uses",
    "head mode by default for list_dir output."
]
for chunk in text_chunks:
    result = detector.feed(chunk)
assert result is None, "Natural repetition of technical terms should not trigger char run"
```

#### Test: Char run — actual character loop SHOULD trigger

**File:** `tests/test_inner_loop_detect.py`  
**Class:** New: `TestCharRunOnlyModeTruePositives`  
**Test name:** `test_actual_char_run_detected`

**Scenario:** Model outputs degenerate character repetition.

```python
detector = InnerLoopDetector(char_run_enabled=True)
detector._two_phase_enabled = False

result = detector.feed("////" * 20)  # Separated chars won't trigger
assert result is None, "Separated chars should not trigger"

result = detector.feed("x" * 75)  # 75 consecutive identical chars
assert result is not None, "Actual char run should be detected"
assert "character run" in result["reason"]
```

#### Test: Max chars guard triggers correctly

**File:** `tests/test_inner_loop_detect.py`  
**Class:** New: `TestMaxCharsGuard`  
**Test name:** `test_max_chars_force_trigger`

**Scenario:** Output exceeds max_chars limit regardless of content quality.

```python
detector = InnerLoopDetector(max_chars=100)  # Low threshold for test
# Feed unique, non-repeating text that exceeds max_chars
for i in range(20):
    result = detector.feed(f"Unique sentence number {i} with varied content xyz\n")
    if result:
        break
assert result is not None, "Max chars should force-trigger detection"
assert "max chars exceeded" in result["reason"]
```

### 5.3 System B Tests — Two-Phase Detector (NEW)

#### Test: Semantic loop detected via suspicion → confirmation

**File:** `tests/test_two_phase_loop_detection.py` (NEW FILE)  
**Class:** `TestTwoPhaseSemanticLoopDetection`  
**Test name:** `test_exact_repeating_segment_confirmed`

**Scenario:** Agent outputs the same paragraph 3+ times with exact repetition.

```python
detector = TwoPhaseLoopDetector()

# Feed a segment that repeats exactly
segment = "Let me analyze this code more carefully. I need to check the dependencies and verify the imports are correct.\n" * 20  # ~1KB segment

for i in range(4):  # Repeat segment 4 times
    result = detector.feed(segment)
    if i < 3:
        assert result is None, f"Should not confirm until enough repetitions (feed {i})"

# On 4th repetition, suspicion should trigger and confirmation should pass
assert result is not None, "Confirmed loop should be detected after exact repetitions"
assert result["type"] == "semantic_loop"
assert "Confirmed semantic loop" in result["reason"]
```

**Expected:** N-gram counter accumulates frequency → suspicion threshold hit → confirmation phase finds 3+ exact interval matches → abort triggered.

#### Test: Non-loop with repeated content does NOT trigger confirmation

**File:** `tests/test_two_phase_loop_detection.py`  
**Class:** `TestTwoPhaseSemanticLoopDetection`  
**Test name:** `test_similar_but_different_content_no_confirmation`

**Scenario:** Agent discusses related topics with similar phrasing but not exact repetition (the classic FP case).

```python
detector = TwoPhaseLoopDetector()

# Similar but NOT identical paragraphs
chunks = [
    "Let me check the read_file function implementation. It handles large files by writing to spillover.\n",
    "Now let me look at the grep tool. It searches through files using regex patterns efficiently.\n",
    "The list_dir tool provides directory contents with optional recursive traversal support.\n",
    "I should also verify code_interpreter behavior for sandboxed execution environments.\n",
]

for chunk in chunks * 3:  # Cycle through similar content multiple times
    result = detector.feed(chunk)
    if result:
        break

assert result is None, "Similar but non-identical content should not confirm as loop"
```

**Expected:** N-gram counter may trigger suspicion (similar token patterns), but confirmation phase fails because no exact interval matches exist → cooldown applied → no abort.

#### Test: Cooldown behavior after false suspicion

**File:** `tests/test_two_phase_loop_detection.py`  
**Class:** `TestTwoPhaseCooldownBehavior`  
**Test name:** `test_cooldown_applied_after_failed_confirmation`

**Scenario:** Verify that when confirmation fails, cooldown suppresses further suspicion checks.

```python
detector = TwoPhaseLoopDetector()
detector.cooldown_duration = 5  # Short cooldown for test

# Feed content that triggers suspicion but not confirmation
similar_chunks = [
    "Analyzing module A dependencies and imports.\n",
    "Checking module B structure and exports.\n",
    "Reviewing module C implementation details.\n",
] * 10

for chunk in similar_chunks:
    result = detector.feed(chunk)
    if result:
        break

assert result is None, "Should not confirm loop on similar-but-different content"
assert detector.cooldown_active, "Cooldown should be active after failed confirmation"

# During cooldown, even a real loop pattern should not trigger suspicion
real_loop_segment = "EXACT SAME TEXT REPEATING HERE\n" * 50
for _ in range(3):
    result = detector.feed(real_loop_segment)
    # Cooldown may prevent detection — this is expected behavior

# After cooldown expires, reset and verify detection works again
detector.cooldown_active = False
detector.ngram_counter.clear()
detector.tail_buffer = ""

result = None
for _ in range(4):
    result = detector.feed(real_loop_segment)
    if result:
        break

assert result is not None, "Detection should work again after cooldown expires"
```

#### Test: Live log replay — Phase 0 ngram detections should be caught by two-phase

**File:** `tests/test_two_phase_live_data.py` (NEW FILE)  
**Test name:** `test_phase0_ngram_detections_caught_by_two_phase`

**Data source:** The 7 ngram detections from Phase 0 baseline (`workspace/logs/loop_samples/samples_2026-08-0[2-4].jsonl`)

**Logic:**
Each Phase 0 ngram detection represents a complete LLM response that triggered the old scoring-based detector. We replay each sample through the new two-phase detector to verify it catches the same loops.

Note: Some samples may be short (< suspicion_threshold occurrences), in which case they won't trigger the two-phase detector but also wouldn't represent dangerous loops — char_run or max_chars would handle degenerate cases. The key test is that LONG looping samples (the actual semantic loops) ARE caught.

**Criteria:**
- Feed each of the 7 ngram-detected samples through the two-phase detector with streaming-simulated chunks
- Verify: samples representing actual semantic loops (repeating content within a single response) are detected via suspicion→confirmation
- Samples that were turn-level repetition patterns (same action across separate turns) may not trigger — those are System A's domain, acceptable

```python
import json
from pathlib import Path

# Load Phase 0 ngram detections from loop_samples directory
samples_dir = Path("workspace/logs/loop_samples")
all_samples = []
for f in sorted(samples_dir.glob("samples_2026-08-0[2-4].jsonl")):
    for line in f.read_text().strip().split("\n"):
        if line.strip():
            all_samples.append(json.loads(line))

# Filter to ngram detections only
ngram_samples = [s for s in all_samples if "ngram" in s.get("reason", "").lower()]
print(f"Loaded {len(ngram_samples)} ngram samples from Phase 0 period")

detected = 0
missed = []
for sample in ngram_samples:
    detector = TwoPhaseLoopDetector()
    text = sample.get("text", "")
    
    # Feed in chunks simulating streaming (~50 chars per chunk)
    for chunk in [text[i:i+50] for i in range(0, len(text), 50)]:
        result = detector.feed(chunk)
        if result:
            detected += 1
            print(f"DETECTED: {sample.get('agent','?')} - {result['reason']}")
            break
    
    if not result:
        missed.append(sample)

print(f"\nResult: {detected}/{len(ngram_samples)} ngram detections caught by two-phase")
for m in missed:
    print(f"  MISSED: {m.get('agent','?')} ({len(m.get('text',''))} chars) - {m.get('reason','')}")

# Assert we catch the majority of real semantic loops
# (some misses acceptable if they were turn-level patterns, not within-response loops)
assert detected >= len(ngram_samples) * 0.7, f"Two-phase should catch most Phase 0 ngram detections, caught {detected}/{len(ngram_samples)}"
```

---

## 6. Dead Code Cleanup Plan

### 6.1 LoopDetectedError (KEEP for backward compatibility)

**File:** `agent_cascade/loop_detection.py`, lines 32–45

**Status:** Never raised in production code. Only used in tests (`tests/test_loop_detection.py` recovery tests).

**Action:** KEEP with updated docstring clarifying it's test-only / legacy. Add deprecation note suggesting inline handling via `detect_loop()` return value is the current pattern.

**Rationale:** Tests depend on it for mocking loop detection behavior in recovery handler tests. Removing would break 10+ tests. Low cost to keep.

### 6.2 run_agent_in_pool_with_recovery (KEEP — still called in production)

**File:** `agent_cascade/api_integration.py`, lines 414–485

**Status:** Called from `agent_cascade/run_agent_unified.py:139` in the main agent execution path.

**Action:** KEEP. Convert the `LoopDetectedError` handling branch (lines 443–467) to a `NotImplementedError` guard with explanatory comment. The function's retry logic for non-loop errors still has value.

**Implementation:**
```python
except LoopDetectedError as e:
    # NOTE: This branch is dead code. LoopDetectedError is never raised in production
    # (loop detection now handled inline in execution_engine._check_and_handle_loop).
    # Kept as NotImplementedError guard to catch accidental reintroduction.
    raise NotImplementedError(
        "LoopDetectedError handling is deprecated. "
        "Loop recovery is now handled inline in ExecutionEngine."
    ) from e
```

**Rationale:** Better than silent dead code — if someone accidentally starts raising LoopDetectedError again, it fails loudly with explanation rather than silently executing stale logic.

### 6.3 _suppress_loop_detection_next_turn flag (KEEP — active in production)

**File:** Set in `compression/handler.py` at lines 628, 723, 989, 1176  
**File:** Checked in `execution_engine.py:2082`, cleared at line 2122

**Status:** Actively used — set after compression operations to prevent false positives from concentrated patterns.

**Action:** KEEP and add verification test (see section 5.1). Add docstring comment at both set sites and check site explaining the cooldown behavior.

---

## 7. Test Migration Matrix

When removing scoring-based modes and adding two-phase detector, existing tests must be updated or removed:

### Tests to REMOVE entirely

| File | Class/Test | Reason |
|------|-----------|--------|
| `tests/test_inner_loop_detect.py` | `TestNgramRepetition.test_repeated_ngram_detected` | N-gram scoring mode removed; replaced by two-phase |
| `tests/test_inner_loop_detect.py` | `TestNgramRepetition.test_varied_text_no_ngram` | N-gram scoring mode removed |
| `tests/test_inner_loop_detect.py` | `TestBlockRepetition.test_repeated_block_detected` | Block scoring mode removed; replaced by two-phase |
| `tests/test_inner_loop_detect.py` | `TestBlockRepetition.test_unique_blocks_no_detection` | Block scoring mode removed |
| `tests/test_inner_loop_detect.py` | `TestLowEntropy.test_low_entropy_detected` | Entropy mode removed; replaced by two-phase |
| `tests/test_inner_loop_detect.py` | `TestLowEntropy.test_high_entropy_no_detection` | Entropy mode removed |
| `tests/test_inner_loop_detect.py` | `TestSentenceRepetition.test_repeated_sentence_detected` | Sentence scoring mode removed; replaced by two-phase |
| `tests/test_inner_loop_detect.py` | `TestSentenceRepetition.test_two_repetitions_no_detection` | Sentence scoring mode removed |
| `tests/test_inner_loop_detect.py` | `TestSentenceRepetition.test_different_sentences_no_detection` | Sentence scoring mode removed |
| `tests/test_inner_loop_detect.py` | `TestScoreMechanics.test_score_accumulation` | Scoring system entirely removed |
| `tests/test_inner_loop_detect.py` | `TestScoreMechanics.test_decay_factor` | Scoring system entirely removed |
| `tests/test_inner_loop_detect.py` | `TestScoreMechanics.test_score_rounded_in_return` | Scoring system entirely removed |
| `tests/test_inner_loop_detect.py` | `TestIntegrationScenarios.test_compound_detection` | Tests scoring accumulation across modes |

### Tests to UPDATE (keep, modify for new behavior)

| File | Class/Test | Change needed |
|------|-----------|---------------|
| `tests/test_inner_loop_detect.py` | `TestCharacterRunDetection` (all tests) | No change needed — char_run is unchanged |
| `tests/test_inner_loop_detect.py` | `TestNoLoopOnNormalText.test_normal_paragraph` | Update: no scoring system to assert; verify two-phase doesn't trigger |
| `tests/test_inner_loop_detect.py` | `TestNoLoopOnNormalText.test_normal_conversation` | Same as above |
| `tests/test_inner_loop_detect.py` | `TestMinCharsGate` (all tests) | Update: references to "heavy checks" now mean two-phase suspicion, not scoring modes |
| `tests/test_inner_loop_detect.py` | `TestReturnFormat` (all tests) | Update return format assertions to include new `"type": "semantic_loop"` |
| `tests/test_inner_loop_detect.py` | `TestMultipleFeedCalls.test_char_run_across_chunks` | No change needed |
| `tests/test_inner_loop_detect.py` | `TestMemoryBoundedness` (all tests) | Update: old token deque / ngram/block/sentence counters removed; new tail_buffer and ngram_counter have different bounds |
| `tests/test_inner_loop_detect.py` | `TestEdgeCases` (all tests) | Review each: keep char_run-relevant ones, remove scoring-dependent assertions |
| `tests/test_inner_loop_detect.py` | `TestFeedPerformance` (all tests) | Update expectations — two-phase should be cheaper than all scoring modes combined |

### Tests in other files to UPDATE

| File | Change needed |
|------|---------------|
| `tests/test_inner_loop_live_data.py` | Update: test char_run + two-phase detector; remove scoring mode assertions |
| `tests/test_inner_loop_fp_simulation.py` | Rewrite for two-phase false positive scenarios (suspicion without confirmation) |
| `tests/test_inner_loop_regression.py` | Review each regression test: keep those relevant to char_run/max_chars, adapt others for two-phase behavior |

### New Test Files

| File | Purpose |
|------|---------|
| `tests/test_two_phase_loop_detection.py` | Core tests for suspicion → confirmation flow, cooldown behavior |
| `tests/test_two_phase_live_data.py` | Live log replay: verify Phase 0 ngram detections are caught by two-phase approach |

---

## 8. Phased Execution Order

### Phase 0: Baseline Measurement (COMPLETED)

**Status:** Done — report at `inner_loop_phase0_baseline.md`

**Key findings used in this plan:**
- ngram caught 7 real semantic loops that char_run + max_chars would miss → justifies keeping heuristic suspicion trigger
- Scoring was probabilistic, not confirmatory → justifies adding exact match confirmation step
- Performance overhead at 40KB with all modes: ~50x vs char_run only → two-phase should be significantly cheaper

### Phase 1: Two-Phase Detector Implementation (Priority: HIGH)

**Goal:** Implement the new two-phase loop detection algorithm.

| Step | Task | File(s) | Est. Effort |
|------|------|---------|-------------|
| 1.1 | Create `TwoPhaseLoopDetector` class with data structures | `agent_cascade/inner_loop_detect.py` (new section) or new file | Medium |
| 1.2 | Implement suspicion phase: ngram frequency tracking, interval estimation | Same | Small |
| 1.3 | Implement confirmation phase: exact match counting on tail buffer | Same | Small |
| 1.4 | Implement cooldown mechanism | Same | Tiny |
| 1.5 | Integrate with `InnerLoopDetector.feed()` alongside char_run + max_chars | `agent_cascade/inner_loop_detect.py` | Medium |
| 1.6 | Add settings/env vars for tunable parameters | `agent_cascade/settings.py` | Small |

**Success criteria:** Two-phase detector works standalone; integrates with existing feed() without breaking char_run or max_chars.

### Phase 2: Test Coverage (Priority: HIGH)

**Goal:** Establish correctness baseline for new two-phase approach before removing old modes.

| Step | Task | File(s) | Est. Effort |
|------|------|---------|-------------|
| 2.1 | Add System A tail-repeat test [A,B,C,D,D,D] | `tests/test_loop_detection.py` | Small |
| 2.2 | Add System A same-tool-different-args test [A,D,B,C,D,E,D] | `tests/test_loop_detection.py` | Small |
| 2.3 | Add cooldown flag verification test | `tests/test_execution_engine_loop_cooldown.py` (NEW FILE) | Medium |
| 2.4 | Add two-phase semantic loop detection tests | `tests/test_two_phase_loop_detection.py` (NEW FILE) | Medium |
| 2.5 | Add two-phase false positive / cooldown tests | Same | Small |
| 2.6 | Add Phase 0 live log replay test for two-phase | `tests/test_two_phase_live_data.py` (NEW FILE) | Small |
| 2.7 | Run char_run-only false positive tests on existing samples | `tests/test_inner_loop_detect.py` | Small |

**Success criteria:** All new tests pass. Two-phase catches all 7 Phase 0 ngram detections. No false positives on similar-but-different content.

### Phase 3: Remove Scoring-Based Modes (Priority: HIGH)

**Goal:** Remove sentence, n-gram, block, entropy scoring modes from System B.

| Step | Task | File(s) | Est. Effort |
|------|------|---------|-------------|
| 3.1 | Remove mode toggles from InnerLoopSettings (sentence_rep, ngram_rep, block_rep, entropy_collapse) | `agent_cascade/settings.py:194-198` | Tiny |
| 3.2 | Remove scoring system (score, threshold, decay, add_score) | `agent_cascade/inner_loop_detect.py:69-70, 140-155` | Small |
| 3.3 | Remove sentence detection block | `agent_cascade/inner_loop_detect.py:314-323` | Small |
| 3.4 | Remove n-gram scoring detection block + state (keep window size constant for two-phase) | `agent_cascade/inner_loop_detect.py:79, 81, 117, 178, 353-379` | Small |
| 3.5 | Remove block detection block + state | `agent_cascade/inner_loop_detect.py:80, 118, 186, 386-410` | Small |
| 3.6 | Remove entropy detection block | `agent_cascade/inner_loop_detect.py:88, 119, 427-446` | Small |
| 3.7 | Remove old token deque and sentence splitting (no longer needed) | `agent_cascade/inner_loop_detect.py:54, 236-275` | Medium |

**After Phase 3:** InnerLoopDetector consists of char_run + max_chars + two-phase semantic detection. No scoring system remains.

### Phase 4: Test Migration (Priority: HIGH — immediately after Phase 3)

**Goal:** Update/remove tests per migration matrix in section 7.

| Step | Task | Est. Effort |
|------|------|-------------|
| 4.1 | Remove obsolete test classes/methods (see matrix) | Medium |
| 4.2 | Update remaining tests for new behavior (char_run + two-phase) | Small |
| 4.3 | Run full test suite, verify all pass | Small |

### Phase 5: Dead Code Cleanup (Priority: MEDIUM)

**Goal:** Clean up unused code paths in System A integration.

| Step | Task | File(s) | Est. Effort |
|------|------|---------|-------------|
| 5.1 | Update LoopDetectedError docstring with deprecation note | `agent_cascade/loop_detection.py` | Tiny |
| 5.2 | Convert run_agent_in_pool_with_recovery LoopDetectedError branch to NotImplementedError guard | `agent_cascade/api_integration.py:443-467` | Small |
| 5.3 | Add documentation comments for _suppress_loop_detection_next_turn behavior | `execution_engine.py`, `compression/handler.py` | Tiny |

**Success criteria:** Tests pass, no behavioral change. Dead code clearly marked.

### Phase 6: Configuration and Deployment (Priority: LOW)

**Goal:** Enable two-phase System B safely with feature flag approach.

| Step | Task | Details |
|------|------|---------|
| 6.1 | Two-phase detector ships behind env var | `QWEN_AGENT_LOOP_TWO_PHASE_ENABLED=0` by default — only char_run + max_chars active until verified |
| 6.2 | Deploy with flag disabled initially | Monitor for any unexpected gaps in loop detection vs old scoring modes |
| 6.3 | Enable two-phase mode after verification | Set `QWEN_AGENT_LOOP_TWO_PHASE_ENABLED=1` once Phase 0 replay tests pass and no production issues observed (~1 week monitoring) |
| 6.4 | Remove old env vars for removed modes | Clean up `QWEN_AGENT_LOOP_SENTENCE_REP`, `QWEN_AGENT_LOOP_NGRAM_REP`, etc. from settings.py |

---

## 9. Notes on Deterministic Detection Direction

**Design principle:** All loop detection should move toward "suspicion → confirmation" rather than scoring accumulation.

- Current System A already follows this pattern (exact pattern matching).
- Char run is deterministic (exact char comparison).
- Max chars is deterministic (exact length check).
- New two-phase detector follows the same principle: heuristic suspicion trigger + exact match confirmation before aborting.

The removed scoring modes violated this principle — they accumulated fuzzy evidence and triggered on probabilistic thresholds without confirming actual repetition. The two-phase approach fixes this by requiring exact match confirmation before taking action.

---

## 10. References

- `agent_cascade/loop_detection.py` — System A implementation
- `agent_cascade/inner_loop_detect.py` — System B implementation (to be modified)  
- `agent_cascade/settings.py:160-198` — InnerLoopSettings dataclass (to be updated)
- `agent_cascade/execution_engine.py:2075-2129` — Loop detection integration point
- `agent_cascade/compression/handler.py` — Cooldown flag set locations (lines 628, 723, 989, 1176)
- `tests/test_loop_detection.py` — Existing System A tests (996 lines)
- `tests/test_inner_loop_detect.py` — Existing System B tests (830+ lines)
- `tests/loop_samples.json` — Historical loop detection samples (181KB, used for Phase 0 analysis and live replay tests)
- `docs/inner_loop_phase0_baseline.md` — Phase 0 baseline report (10 detections analyzed, 7 ngram true positives)