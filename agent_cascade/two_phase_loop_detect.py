"""Two-phase semantic loop detection for System B (streaming text).

Replaces scoring-based modes with:
  Phase 1 — Suspicion: lightweight ngram frequency counter flags potential loops.
  Phase 2 — Confirmation: exact byte-level comparison confirms or rejects the suspicion.

This detector is ADDITIVE to char_run and max_chars; it does not replace them.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Suspicion:
    """Result of the suspicion phase."""

    interval_length: int  # Estimated loop interval in characters (>=1 if reliable, -1 if not)
    dominant_ngram: tuple[str, ...]  # The n-gram tuple that triggered suspicion


def tokenize_chunk(text: str) -> list[str]:
    """Split text into tokens on whitespace, strip leading/trailing punctuation, filter empty."""
    return [t.strip(".,!?;:'\"()[]{}<>").lower() for t in text.split() if t.strip(".,!?;:'\"()[]{}<>")]


class TwoPhaseLoopDetector:
    """Two-phase semantic loop detector.

    Phase 1 (suspicion): Lightweight ngram frequency tracker over a persistent token buffer.
    When an n-gram appears enough times with consistent spacing, emit a Suspicion.

    Phase 2 (confirmation): Exact byte-level comparison of tail segments using the
    suspected interval. Only triggers abort when confirmed_matches_required exact
    repetitions are found.

    Cooldown: On failed confirmation, all suspicion state is cleared and detection
    is suppressed for cooldown_duration feeds to prevent noisy re-triggering.
    """

    def __init__(self, suspicion_threshold=None, confirmed_matches_required=None, cooldown_duration=None, enabled=None) -> None:
        # Suspicion phase parameters
        self.ngram_window_size = 64  # Token window size (same as current ngram mode)
        self.suspicion_threshold = suspicion_threshold or int(
            os.environ.get("QWEN_AGENT_LOOP_SUSPICION_THRESHOLD", "7")
        )
        self.max_counter_entries = 200  # Prune threshold for counter

        # Persistent token buffer — accumulates across feeds, bounded
        self.token_buffer: list[str] = []
        self.max_token_buffer = 5000

        # Ngram tracking
        self.ngram_counter: Counter[tuple[str, ...]] = Counter()
        self.ngram_positions: dict[tuple[str, ...], list[int]] = {}

        # Confirmation phase parameters
        self.confirmed_matches_required = confirmed_matches_required or int(
            os.environ.get("QWEN_AGENT_LOOP_CONFIRM_REQUIRED", "3")
        )

        # Cooldown state
        self.cooldown_active = False
        self.cooldown_remaining_feeds = 0
        self.cooldown_duration = cooldown_duration or int(
            os.environ.get("QWEN_AGENT_LOOP_COOLDOWN_FEEDS", "50")
        )

        # Tail buffer for exact comparison — no truncation needed (detector is per-response, max_chars limits total)
        self.tail_buffer: str = ""

        # Feature flag — gated for safe rollout (env var fallback if not explicitly set)
        if enabled is not None:
            self.two_phase_enabled = enabled
        else:
            self.two_phase_enabled = os.environ.get("QWEN_AGENT_LOOP_TWO_PHASE_ENABLED", "0") == "1"

    def reset(self) -> None:
        """Clear all state so the detector can be reused for a new LLM call attempt."""
        self.token_buffer.clear()
        self.ngram_counter.clear()
        self.ngram_positions.clear()
        self.cooldown_active = False
        self.cooldown_remaining_feeds = 0
        self.tail_buffer = ""

    def _prune_counters(self) -> None:
        """Keep only the most frequent n-grams to bound memory usage."""
        if len(self.ngram_counter) <= self.max_counter_entries:
            return

        # Keep top N entries by count
        top_items = self.ngram_counter.most_common(self.max_counter_entries)
        kept_keys = {item[0] for item in top_items}

        # Prune both counter and positions
        self.ngram_counter = Counter(dict(top_items))
        self.ngram_positions = {k: v for k, v in self.ngram_positions.items() if k in kept_keys}

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
        distances: list[int] = []
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

        # Consistency gate: dominant gap must represent >=60% of all gaps
        # This distinguishes periodic loops from merely repetitive content
        gap_counts = Counter(distances)
        most_common_gap, most_common_count = gap_counts.most_common(1)[0]
        dominant_ratio = most_common_count / len(distances)

        if dominant_ratio < 0.6:
            return -1  # Gaps too inconsistent — repetitive but not periodic

        # Clamp to reasonable bounds (max interval = tail/confirmed_matches_required, ensuring confirmation can verify)
        max_interval = len(self.tail_buffer) // self.confirmed_matches_required if self.tail_buffer else 10000
        return max(1, min(median, max_interval))

    def _check_suspicion(self, new_text: str) -> Suspicion | None:
        """Lightweight heuristic: track n-gram frequencies over persistent token buffer, flag when pattern repeats."""

        # Skip if cooldown active
        if self.cooldown_active:
            self.cooldown_remaining_feeds -= 1
            if self.cooldown_remaining_feeds <= 0:
                self.cooldown_active = False
            return None

        # Update tail buffer — no truncation needed (see class docstring)
        self.tail_buffer += new_text

        # Tokenize new chunk and append to persistent token buffer
        new_tokens = tokenize_chunk(new_text)
        self.token_buffer.extend(new_tokens)

        # Trim token buffer if too large (keep tail portion where loops would appear)
        if len(self.token_buffer) > self.max_token_buffer:
            self.token_buffer = self.token_buffer[-self.max_token_buffer:]

        # Slide ngram window over the END of token_buffer (where new tokens are)
        # Only scan recently added region to avoid re-scanning everything each feed
        start_scan = max(0, len(self.token_buffer) - len(new_tokens) - self.ngram_window_size)

        for i in range(start_scan, max(start_scan + 1, len(self.token_buffer) - self.ngram_window_size + 1)):
            window = tuple(self.token_buffer[i : i + self.ngram_window_size])
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

    def _confirm_loop(self, suspicion: Suspicion) -> int:
        """Exact match test: count how many times the suspected interval repeats in tail.

        Returns the number of confirmed repetitions (0 if not enough data or no loop).
        Caller should compare against confirmed_matches_required to decide on abort.
        """
        interval = suspicion.interval_length
        max_interval = len(self.tail_buffer) // self.confirmed_matches_required
        if interval < 1 or interval > max_interval:
            return 0

        min_tail_needed = interval * self.confirmed_matches_required
        if len(self.tail_buffer) < min_tail_needed:
            return 0

        candidate_segment = self.tail_buffer[-interval:]
        confirmed_count = 1
        pos = len(self.tail_buffer) - interval

        while pos >= interval:
            prev_segment = self.tail_buffer[pos - interval : pos]
            if prev_segment == candidate_segment:
                confirmed_count += 1
                pos -= interval
            else:
                break

        return confirmed_count

    def _apply_cooldown(self) -> None:
        """Suppress suspicion detection after failed confirmation to prevent repeated false triggers.

        MANDATORY: Always reset all tracking state on cooldown — stale data causes re-triggering
        on the same content that already failed confirmation.
        """
        self.cooldown_active = True
        self.cooldown_remaining_feeds = self.cooldown_duration  # Default: 50 feeds

        # Mandatory reset of all suspicion tracking state
        self.ngram_counter.clear()
        self.ngram_positions.clear()

    def feed(self, new_text: str) -> dict | None:
        """Main entry point called during streaming.

        Returns None on no-loop, or a dict {"loop": True, "reason": ..., ...}.

        Two-phase process:
          1. Suspicion phase (skipped during cooldown) — lightweight ngram heuristic
          2. Confirmation phase — exact match verification only when suspicion raised
        """
        if not self.two_phase_enabled:
            return None

        # Phase 1: Check for suspicion (skipped during cooldown)
        suspicion = self._check_suspicion(new_text)

        if suspicion is not None:
            confirmed_count = self._confirm_loop(suspicion)

            if confirmed_count >= self.confirmed_matches_required:
                return {
                    "loop": True,
                    "reason": f"semantic loop ({suspicion.interval_length} chars repeating)",
                    "confirmed_repetitions": confirmed_count,
                }
            else:
                self._apply_cooldown()

        return None  # No loop detected