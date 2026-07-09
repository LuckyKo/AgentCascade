from collections import deque, Counter
import datetime
import json
import math
import os
import re

from agent_cascade.settings import InnerLoopSettings, DEFAULT_WORKSPACE

# Precompiled regex patterns (avoid recompilation on every feed call).
_SENTENCE_RE = re.compile(r'([^.?!]+[.?!]|[^.?!]+$)')
_NON_WORD_RE = re.compile(r'\W+')
_WORD_RE = re.compile(r'\b\w+\b')


class InnerLoopDetector:
    def __init__(
        self,
        ngram_size: int | None = None,
        block_size: int | None = None,
        entropy_window: int | None = None,
        char_run_limit: int | None = None,
        score_threshold: int | None = None,
        min_chars: int | None = None,
        batch_interval: int | None = None,
        settings: InnerLoopSettings | None = None,
    ):
        """Initialize the inner-loop detector.

        Args:
            settings:  Optional ``InnerLoopSettings`` instance providing defaults
                       for all tunable parameters.  If omitted a default instance
                       is used (all values match current production behaviour).
            ngram_size, block_size, … : Override individual fields from *settings*.
                                        Passing an explicit value always wins over
                                        whatever the ``settings`` object contains.
        """
        if settings is None:
            settings = InnerLoopSettings()
        self.text = ""
        # Bounded token storage: deque with maxlen to cap memory usage.
        self.tokens = deque(maxlen=settings.max_tokens)

        # Per-parameter overrides: explicit arg wins, otherwise fall back to settings default.
        self.ngram_size = ngram_size if ngram_size is not None else settings.ngram_size
        self.block_size = block_size if block_size is not None else settings.block_size
        self.entropy_window = entropy_window if entropy_window is not None else settings.entropy_window
        self.char_run_limit = char_run_limit if char_run_limit is not None else settings.char_run_limit

        # Minimum characters to accumulate before running heavy checks.
        self.min_chars = min_chars if min_chars is not None else settings.default_min_chars
        # Run full detection only every batch_interval-th feed call.
        self.batch_interval = max(1, batch_interval if batch_interval is not None else settings.default_batch_interval)

        self.score = 0
        self.threshold = score_threshold if score_threshold is not None else settings.score_threshold

        # Cached reference to settings for thresholds used in detection logic.
        self._settings = settings

        self.ngrams = Counter()
        self.blocks = Counter()
        self.sentences = Counter()

        # Track which items have already been scored (one-time scoring per threshold crossing).
        self._scored_sentences = set()
        self._scored_ngrams = set()
        self._scored_blocks = set()

        self.last_char = None
        self.char_run = 0

        # Internal: total chars fed so far (for min_chars gate) and feed counter.
        self._chars_fed = 0
        self._feed_count = 0

    # ── State management ────────────────────────────────────────────────

    def reset(self):
        """Clear all state so the detector can be reused for a new LLM call attempt."""
        self.text = ""
        self.tokens.clear()
        self.ngrams.clear()
        self.blocks.clear()
        self.sentences.clear()
        self.score = 0
        self.last_char = None
        self.char_run = 0
        self._scored_sentences.clear()
        self._scored_ngrams.clear()
        self._scored_blocks.clear()
        self._chars_fed = 0
        self._feed_count = 0

    # ── Scoring helpers ─────────────────────────────────────────────────

    def decay(self):
        """Gradually reduce score so transient repetitions don't accumulate forever."""
        self.score *= self._settings.score_decay_rate

    def add_score(self, amount, reason):
        """Add to the loop score; return an event dict if threshold is crossed."""
        self.score += amount
        if self.score >= self.threshold:
            return {
                "loop": True,
                "reason": reason,
                "score": round(self.score, 1),
            }
        return None

    # ── Counter maintenance (prune oldest entries when over budget) ─────

    def _trim_counter(self, counter: Counter, max_entries: int | None = None) -> None:
        """Remove the least-representative keys when a Counter grows too large.

        Keeps the top-N by count, then fills remaining slots from the rest.
        This is O(k log k) where k = len(counter), called only when over budget.
        """
        if max_entries is None:
            max_entries = self._settings.max_counter_entries
        if len(counter) <= max_entries:
            return
        # Sort by count descending; keep the most frequent entries first.
        sorted_items = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)
        counter.clear()
        # Build a dict from (key, value) pairs so Counter.update restores counts correctly.
        counter.update(dict(sorted_items[:max_entries]))

    # ── Main feed method (API unchanged: returns None or loop-event dict) ─

    def feed(self, chunk):
        """
        Feed newly generated text delta.

        Returns None on no-loop, or a dict {"loop": True, "reason": ..., "score": ...}.

        Heavy checks (n-grams, blocks, entropy) are gated behind two thresholds:
          1. min_chars   — skip until enough text has accumulated to be meaningful.
          2. batch_interval — only run every N-th feed call to reduce overhead per chunk.
        """
        # Guard against empty or whitespace-only chunks (no-op, avoids accumulating junk).
        if not chunk or not chunk.strip():
            return None

        ##################################################
        # Accumulate text and tokenize into sentences
        ##################################################

        self.text += chunk
        self._chars_fed += len(chunk)
        self._feed_count += 1

        # Split accumulated text into sentence chunks in one pass.
        # Track the last match end position; if no sentences are found,
        # the entire buffer is preserved (handles code, poetry, etc.).
        # The regex also captures trailing text without terminal punctuation
        # so that sentences ending mid-stream are still tokenized.
        last_end = 0
        for sent_match in _SENTENCE_RE.finditer(self.text):
            sent = sent_match.group(1)
            last_end = sent_match.end()

            # Tokenize with proper word-boundary handling instead of str.split().
            norm = _NON_WORD_RE.sub(' ', sent.lower()).strip()

            if norm:
                toks = _WORD_RE.findall(norm)
                self.tokens.extend(toks)
                # Accumulate sentence counts (checked later, gated by min_chars).
                self.sentences[norm] += 1

        self.text = self.text[last_end:]

        ##################################################
        # Character repetition (per-char scan — always runs to maintain state)
        # Detection gated by min_chars so we don't fire on tiny text.
        ##################################################

        for ch in chunk:
            if ch == self.last_char:
                self.char_run += 1
            else:
                self.last_char = ch
                self.char_run = 1

            if self.char_run > self.char_run_limit:
                # Char runs are a strong signal — return immediately regardless of threshold.
                return {
                    "loop": True,
                    "reason": f"character run '{ch}' ({self.char_run})",
                    "score": round(self.score + 100, 1),
                }

        ##################################################
        # All detection checks — gated by min_chars
        # Light checks (sentence) run every time once active.
        # Heavy checks (n-grams, blocks, entropy) also respect batch_interval.
        ##################################################

        # Skip until enough text has accumulated to be meaningful.
        if self._chars_fed < self.min_chars:
            self.decay()
            return None

        ##################################################
        # Sentence repetition — always runs after min_chars (cheap, no batching)
        ##################################################

        for norm, count in self.sentences.items():
            if count >= self._settings.sentence_repetition_threshold and norm not in self._scored_sentences:
                self._scored_sentences.add(norm)
                ev = self.add_score(80, "repeated sentence")
                if ev:
                    return ev

        ##################################################
        # Heavy checks — also respect batch_interval to save CPU per chunk
        ##################################################

        if self._feed_count % self.batch_interval != 0:
            self.decay()
            return None

        ##################################################
        # n-gram detection (tuple of strings as Counter key — no hashing needed)
        # Deque supports direct iteration; tuple(deque)[-n:] avoids O(n) list copy.
        ##################################################

        if len(self.tokens) >= self.ngram_size:
            ng = tuple(self.tokens)[-self.ngram_size:]  # O(k) where k=ngram_size, not O(N)
            self.ngrams[ng] += 1
            if self.ngrams[ng] >= self._settings.ngram_repetition_threshold and ng not in self._scored_ngrams:
                self._scored_ngrams.add(ng)
                ev = self.add_score(60, "repeated ngram")
                if ev:
                    return ev

        # Prune n-gram counter.
        self._trim_counter(self.ngrams)

        ##################################################
        # Block repetition (tuple of strings as Counter key)
        ##################################################

        if len(self.tokens) >= self.block_size:
            block = tuple(self.tokens)[-self.block_size:]  # O(k) where k=block_size, not O(N)
            self.blocks[block] += 1
            if self.blocks[block] >= self._settings.block_repetition_threshold and block not in self._scored_blocks:
                self._scored_blocks.add(block)
                ev = self.add_score(70, "repeated block")
                if ev:
                    return ev

        # Prune block counter.
        self._trim_counter(self.blocks)

        ##################################################
        # Sentence counter prune (done alongside heavy checks)
        ##################################################
        self._trim_counter(self.sentences)

        ##################################################
        # Entropy collapse
        #
        # Shannon entropy of the token distribution in a sliding window.
        # With 128-token windows over natural-language text, typical entropy is
        # ~3.5–4.5 bits (many distinct words). Below 2.0 bits means fewer than
        # ~4 equally-likely tokens dominate — a strong signal of repetition or
        # degenerate generation ("the the the" or repeating phrases).
        ##################################################

        if len(self.tokens) >= self.entropy_window:
            window = tuple(self.tokens)[-self.entropy_window:]  # O(k) not O(N)
            counts = Counter(window)

            entropy = 0.0
            for c in counts.values():
                p = c / len(window)
                entropy -= p * math.log2(p)

            if entropy < self._settings.entropy_threshold:
                ev = self.add_score(30, f"low entropy ({entropy:.2f})")
                if ev:
                    return ev

        # Gradual score decay prevents transient spikes from sticking forever.
        self.decay()

        return None


# ── Loop sample saving helper ────────────────────────────────────────────────

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
        "token_estimate": max(1, len(text) // 5),  # Consistent with project TOKEN_ESTIMATE_CHAR_DIVISOR (5.0)
        "text": text[:8000],  # Cap at ~2K tokens to keep files manageable
    }

    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return filepath
    except OSError:
        return None  # Non-critical — don't fail execution over debug logging