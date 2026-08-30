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


def _is_token_boundary(ch: str) -> bool:
    """Check if a character is whitespace or punctuation that terminates a token."""
    return ch.isspace() or ch in ".,!?;:'\"()[]{}<>"


class StreamingTokenizer:
    """Incremental tokenizer that buffers partial words across chunk boundaries.

    Guarantees identical tokenization regardless of where chunk boundaries fall,
    by only emitting tokens when a word is fully received (terminated by whitespace/punctuation).
    """

    def __init__(self) -> None:
        self.pending: str = ""  # Partial word waiting for completion

    def tokenize_chunk(self, text: str) -> list[str]:
        """Tokenize a chunk of text, buffering any incomplete trailing word.

        Returns list of completed tokens. Any partial word at the end is held
        in pending and will be completed when the next chunk arrives.
        """
        tokens: list[str] = []
        buffer = self.pending + text
        self.pending = ""

        current_start = 0
        i = 0
        while i < len(buffer):
            if _is_token_boundary(buffer[i]):
                # End of a word — emit token if non-empty
                word = buffer[current_start:i].strip(".,!?;:'\"()[]{}<>").lower()
                if word:
                    tokens.append(word)
                current_start = i + 1
            i += 1

        # Any remaining characters form an incomplete word — buffer them
        if current_start < len(buffer):
            self.pending = buffer[current_start:]

        return tokens

    def reset(self) -> None:
        """Clear pending state."""
        self.pending = ""


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

        # Streaming tokenizer — buffers partial words across chunks for consistent tokenization
        self.tokenizer = StreamingTokenizer()

        # Persistent token buffer — accumulates across feeds, bounded
        self.token_buffer: list[str] = []
        self.max_token_buffer = 5000

        # Parallel array: char_end_offset[i] = character position in tail_buffer where token i ends.
        # Used for accurate interval estimation (token-based suspicion → char-based confirmation).
        self.token_char_offsets: list[int] = []

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
        self.token_char_offsets.clear()
        self.ngram_counter.clear()
        self.ngram_positions.clear()
        self.cooldown_active = False
        self.cooldown_remaining_feeds = 0
        self.tail_buffer = ""
        self.tokenizer.reset()

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

        Uses minimum gap as the interval estimate when gaps show a harmonic pattern
        (some are multiples of others due to skipped positions from chunk boundaries).
        Falls back to median if gaps are consistent without harmonics.
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

        # Use the minimum positive gap as the interval estimate.
        # Rationale: when chunk boundaries cause some positions to be skipped, we get
        # harmonic gaps (e.g., [48, 96, 48, 53]) where larger gaps are multiples of the true interval.
        min_gap = min(distances)

        # Consistency gate: check if most gaps are close to min_gap or small integer multiples of it.
        tolerance = max(4, min_gap // 6)
        consistent_count = 0
        for d in distances:
            # Gap is consistent if it's close to min_gap or a multiple of it (1x-4x)
            for mult in range(1, 5):
                target = mult * min_gap
                if abs(d - target) <= tolerance:
                    consistent_count += 1
                    break

        consistent_ratio = consistent_count / len(distances)

        if consistent_ratio < 0.6:
            return -1  # Gaps too inconsistent — repetitive but not periodic

        # Clamp to reasonable bounds (max interval = tail/confirmed_matches_required, ensuring confirmation can verify)
        max_interval = len(self.tail_buffer) // self.confirmed_matches_required if self.tail_buffer else 10000
        return max(1, min(min_gap, max_interval))

    def _check_suspicion(self, new_text: str) -> Suspicion | None:
        """Lightweight heuristic: track n-gram frequencies over persistent token buffer, flag when pattern repeats."""

        # Skip if cooldown active
        if self.cooldown_active:
            self.cooldown_remaining_feeds -= 1
            if self.cooldown_remaining_feeds <= 0:
                self.cooldown_active = False
            return None

        # Update tail buffer — no truncation needed (see class docstring)
        chars_before_new = len(self.tail_buffer)
        self.tail_buffer += new_text

        # Tokenize using streaming tokenizer (buffers partial words across chunks)
        new_tokens = self.tokenizer.tokenize_chunk(new_text)
        first_new_token_idx = len(self.token_buffer)

        # Record exact char offsets for each new token.
        # Since tokens are emitted in order from the input, we can compute cumulative positions.
        pos_in_chunk = 0
        for token in new_tokens:
            # Find where this token appears in new_text starting from current position
            idx = new_text.find(token, pos_in_chunk)
            if idx < 0:
                # Token might be lowercased/stripped version — find original form
                idx = self._find_token_in_text(new_text, token, pos_in_chunk)
            char_end_offset = chars_before_new + idx + len(token)
            self.token_char_offsets.append(char_end_offset)
            pos_in_chunk = idx + len(token)

        self.token_buffer.extend(new_tokens)

        # Trim token buffer if too large (keep tail portion where loops would appear)
        if len(self.token_buffer) > self.max_token_buffer:
            trim_count = len(self.token_buffer) - self.max_token_buffer
            self.token_buffer = self.token_buffer[trim_count:]
            self.token_char_offsets = self.token_char_offsets[trim_count:]

        # Slide ngram window over the END of token_buffer (where new tokens are)
        # Only scan recently added region to avoid re-scanning everything each feed
        start_scan = max(0, first_new_token_idx - self.ngram_window_size)

        for i in range(start_scan, len(self.token_buffer) - self.ngram_window_size + 1):
            window = tuple(self.token_buffer[i : i + self.ngram_window_size])
            if len(window) < self.ngram_window_size:
                continue

            # Use tuple directly as key — deterministic, no hash randomization issues
            self.ngram_counter[window] += 1

            # Track position using EXACT char offset from token_char_offsets.
            # The position is where this n-gram window ENDS in tail_buffer.
            # Only record if far enough from last position (avoids overlapping-window noise).
            exact_char_pos = self.token_char_offsets[i + self.ngram_window_size - 1]

            if window not in self.ngram_positions:
                self.ngram_positions[window] = []

            pos_list = self.ngram_positions[window]
            last_pos = pos_list[-1] if pos_list else None
            min_gap = max(8, self.ngram_window_size // 4)  # Minimum char gap between recorded positions

            if last_pos is None or (exact_char_pos - last_pos) >= min_gap:
                pos_list.append(exact_char_pos)
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

    def _find_token_in_text(self, text: str, token: str, start: int) -> int:
        """Find the original (case/punctuation preserved) form of a token in text."""
        # Search for any substring that matches when lowercased and stripped
        remaining = text[start:]
        min_len = len(token)
        max_len = min(len(remaining), min_len + 10)  # Allow up to 10 extra chars for punctuation

        for length in range(min_len, max_len + 1):
            if start + length > len(text):
                break
            candidate = text[start:start + length]
            if candidate.lower().strip(".,!?;:'\"()[]{}<>") == token:
                return start

        # Fallback: search from start position for the lowercased token
        idx = remaining.lower().find(token)
        return start + idx if idx >= 0 else start

    def _confirm_loop(self, suspicion: Suspicion) -> int:
        """Exact match test: count how many times the suspected interval repeats in tail.

        Returns the number of confirmed repetitions (0 if not enough data or no loop).
        Caller should compare against confirmed_matches_required to decide on abort.

        Searches around the estimated interval to tolerate small position estimation errors
        from token-to-char mapping (typically ±1-2 tokens worth of characters).
        """
        estimated_interval = suspicion.interval_length
        max_interval = len(self.tail_buffer) // self.confirmed_matches_required
        if estimated_interval < 1 or estimated_interval > max_interval:
            return 0

        # Search radius: allow ±15% tolerance for interval estimation error from token mapping
        search_radius = max(8, estimated_interval // 6)
        best_count = 0
        best_interval = estimated_interval

        for interval in range(
            max(1, estimated_interval - search_radius),
            min(max_interval, estimated_interval + search_radius) + 1
        ):
            min_tail_needed = interval * self.confirmed_matches_required
            if len(self.tail_buffer) < min_tail_needed:
                continue

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

            if confirmed_count > best_count:
                best_count = confirmed_count
                best_interval = interval

        return best_count

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
                    "score": 100,
                }
            else:
                self._apply_cooldown()

        return None  # No loop detected