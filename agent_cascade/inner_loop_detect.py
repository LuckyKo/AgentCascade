import datetime
import json
import os

from agent_cascade.settings import (
    InnerLoopSettings, TOKEN_ESTIMATE_CHAR_DIVISOR, DEFAULT_WORKSPACE,
)
from agent_cascade.two_phase_loop_detect import TwoPhaseLoopDetector


class InnerLoopDetector:
    def __init__(
        self,
        char_run_limit: int | None = None,
        min_chars: int | None = None,
        max_chars: int | None = None,
        settings: InnerLoopSettings | None = None,
    ):
        """Initialize the inner-loop detector.

        After Phase 3 cleanup, this detector uses only:
        - Character run detection (last line of defense against degenerate output)
        - Max chars guard (last line of defense against runaway generation)
        - Two-phase semantic loop detector (replaces all scoring-based modes)

        Args:
            settings: Optional InnerLoopSettings instance providing defaults.
                      If omitted a default instance is used.
            char_run_limit, min_chars, max_chars: Override individual fields from settings.
                                                   Explicit values always win over settings.
        """
        if settings is None:
            settings = InnerLoopSettings()

        self.char_run_limit = char_run_limit if char_run_limit is not None else settings.char_run_limit
        self.min_chars = min_chars if min_chars is not None else settings.default_min_chars
        self.max_chars = max_chars if max_chars is not None else settings.default_max_chars

        # Cached reference to settings for toggle flags.
        self._settings = settings

        # Character run tracking (last line of defense).
        self.last_char = None
        self.char_run = 0

        # Internal: total chars fed so far (for max_chars guard).
        self._chars_fed = 0

        # Two-phase semantic loop detector — replaces all scoring-based modes.
        self._two_phase_detector = TwoPhaseLoopDetector(
            suspicion_threshold=settings.loop_suspicion_threshold,
            confirmed_matches_required=settings.loop_confirm_required,
            cooldown_duration=settings.loop_cooldown_feeds,
            enabled=settings.loop_two_phase_enabled,
        )

    # ── State management ────────────────────────────────────────────────

    def reset(self):
        """Clear all state so the detector can be reused for a new LLM call attempt."""
        self.last_char = None
        self.char_run = 0
        self._chars_fed = 0
        self._two_phase_detector.reset()

    # ── Main feed method (API unchanged: returns None or loop-event dict) ─

    def feed(self, chunk):
        """Feed newly generated text delta.

        Returns None on no-loop, or a dict {"loop": True, "reason": ..., "score": ...}.

        Detection modes (in order of priority):
        1. Max chars guard — hard limit that force-triggers detection.
        2. Character run detection — catches degenerate single-char repetition.
        3. Two-phase semantic loop detector — catches real semantic loops.
        """
        # Guard against empty or whitespace-only chunks (no-op, avoids accumulating junk).
        if not chunk or not chunk.strip():
            # Reset char_run so characters separated by whitespace don't falsely accumulate
            # into a run (e.g., "/" + "\n\n" + "/" → should not trigger char loop).
            self.char_run = 0
            return None

        # Accumulate and check max chars guard
        self._chars_fed += len(chunk)

        # Max char guard: force-trigger if output exceeds limit (gated by toggle).
        if self._settings.max_chars_enabled and self._chars_fed >= self.max_chars:
            return {
                "loop": True,
                "reason": f"max chars exceeded ({self._chars_fed}/{self.max_chars})",
                "score": 100,
            }

        # Character repetition (per-char scan — always runs to maintain state)
        for ch in chunk:
            if ch == self.last_char:
                self.char_run += 1
            else:
                self.last_char = ch
                self.char_run = 1

            if self._settings.char_run_enabled and self.char_run > self.char_run_limit:
                # Char runs are a strong signal — return immediately regardless of threshold.
                return {
                    "loop": True,
                    "reason": f"character run '{ch}' ({self.char_run})",
                    "score": 100,
                }

        # Two-phase semantic loop detection (gated by feature flag)
        two_phase_result = self._two_phase_detector.feed(chunk)
        if two_phase_result is not None:
            return two_phase_result

        return None


# Loop sample saving helper

# Default path for loop samples: under the workspace logs directory.
_LOOP_SAMPLES_DIR = os.path.join(DEFAULT_WORKSPACE, "logs", "loop_samples")


def save_loop_sample(text, reason, instance_name="", filepath=None):
    """Append a loop detection sample to a JSONL file for debugging and tuning.

    Each line is a JSON object with:
      - timestamp (ISO-8601 UTC), instance_name, reason, token_estimate, text

    Args:
        text: The generated text content that triggered the loop detection.
        reason: Human-readable explanation of why the loop was detected.
        instance_name: Name of the agent instance (e.g., "coder1").
        filepath: Override path for the JSONL file. If None, a daily file is used
            under the ``workspace/logs/loop_samples/`` directory (DEFAULT_WORKSPACE).
    """
    if not text:
        return None

    # Resolve output path — default to one file per day to avoid unbounded growth
    if filepath is None:
        os.makedirs(_LOOP_SAMPLES_DIR, exist_ok=True)
        date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        filepath = os.path.join(_LOOP_SAMPLES_DIR, f"samples_{date_str}.jsonl")

    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "instance_name": instance_name,
        "reason": reason,
        "token_estimate": max(1, len(text) // int(TOKEN_ESTIMATE_CHAR_DIVISOR)),
        "text": text[:8000],  # Cap at ~2K tokens to keep files manageable
    }

    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return filepath
    except OSError:
        return None  # Non-critical — don't fail execution over debug logging