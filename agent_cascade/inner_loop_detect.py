from collections import deque, Counter
import datetime
import hashlib
import json
import math
import os
import re


# Max entries per Counter before pruning stale keys (prevents unbounded memory growth).
_MAX_COUNTER_ENTRIES = 200

# Max tokens to keep in the sliding window (keeps token list bounded).
_MAX_TOKENS = 1000

# Default minimum characters to accumulate before activating full detection.
# Below this threshold we only track state — no hashing or counter work is done.
_DEFAULT_MIN_CHARS = 2500

# Default batch interval: run the heavy checks every N-th feed call instead of
# on every streaming chunk.  A value of 1 means "check every chunk".
_DEFAULT_BATCH_INTERVAL = 4


class InnerLoopDetector:
    def __init__(
        self,
        ngram_size=128,
        block_size=128,
        entropy_window=128,
        char_run_limit=24,
        score_threshold=120,
        min_chars=_DEFAULT_MIN_CHARS,
        batch_interval=_DEFAULT_BATCH_INTERVAL,
    ):
        self.text = ""
        # Bounded token storage: deque with maxlen to cap memory usage.
        self.tokens = deque(maxlen=_MAX_TOKENS)

        self.ngram_size = ngram_size
        self.block_size = block_size
        self.entropy_window = entropy_window
        self.char_run_limit = char_run_limit

        # Minimum characters to accumulate before running heavy checks.
        self.min_chars = min_chars
        # Run full detection only every batch_interval-th feed call.
        self.batch_interval = max(1, batch_interval)

        self.score = 0
        self.threshold = score_threshold

        self.ngrams = Counter()
        self.blocks = Counter()
        self.sentences = Counter()

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
        self._chars_fed = 0
        self._feed_count = 0

    # ── Scoring helpers ─────────────────────────────────────────────────

    def decay(self):
        """Gradually reduce score so transient repetitions don't accumulate forever."""
        self.score *= 0.97

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

    @staticmethod
    def _trim_counter(counter: Counter, max_entries: int = _MAX_COUNTER_ENTRIES) -> None:
        """Remove the least-representative keys when a Counter grows too large.

        Keeps the top-N by count, then fills remaining slots from the rest.
        This is O(k log k) where k = len(counter), called only when over budget.
        """
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

        ##################################################
        # Character repetition (fast path — per-char scan, always active)
        ##################################################

        for ch in chunk:
            if ch == self.last_char:
                self.char_run += 1
            else:
                self.last_char = ch
                self.char_run = 1

            if self.char_run > self.char_run_limit:
                return self.add_score(
                    100,
                    f"character run '{ch}' ({self.char_run})",
                )

        ##################################################
        # Accumulate text and tokenize into sentences
        ##################################################

        self.text += chunk
        self._chars_fed += len(chunk)
        self._feed_count += 1

        # Split accumulated text into sentence chunks in one pass.
        # Track the last match end position; if no sentences are found,
        # the entire buffer is preserved (handles code, poetry, etc.).
        last_end = 0
        for sent_match in re.finditer(r'([^.?!]*[.?!])', self.text):
            sent = sent_match.group(1)
            last_end = sent_match.end()

            # Tokenize with proper word-boundary handling instead of str.split().
            norm = re.sub(r'\W+', ' ', sent.lower()).strip()

            if norm:
                toks = re.findall(r'\b\w+\b', norm)
                self.tokens.extend(toks)

                # --- Sentence repetition check (always active, cheap) ---
                self.sentences[norm] += 1
                if self.sentences[norm] >= 3:
                    ev = self.add_score(80, "repeated sentence")
                    if ev:
                        return ev

        self.text = self.text[last_end:]

        ##################################################
        # Heavy checks — gated by min_chars AND batch_interval
        ##################################################

        # Skip expensive hashing/entropy until enough text has accumulated.
        if self._chars_fed < self.min_chars:
            self.decay()
            return None

        # Only run heavy checks every batch_interval-th call to reduce per-chunk cost.
        if self._feed_count % self.batch_interval != 0:
            self.decay()
            return None

        ##################################################
        # n-gram detection (deterministic hashing via md5)
        # Deque doesn't support slicing, so we convert only the tail window.
        ##################################################

        if len(self.tokens) >= self.ngram_size:
            ng = tuple(list(self.tokens)[-self.ngram_size:])
            h = hashlib.md5(str(ng).encode()).hexdigest()

            self.ngrams[h] += 1
            if self.ngrams[h] >= 3:
                ev = self.add_score(60, "repeated ngram")
                if ev:
                    return ev

        # Prune n-gram counter.
        self._trim_counter(self.ngrams)

        ##################################################
        # Block repetition (sha1 already deterministic)
        ##################################################

        if len(self.tokens) >= self.block_size:
            block = " ".join(list(self.tokens)[-self.block_size:])
            h = hashlib.sha1(block.encode()).hexdigest()

            self.blocks[h] += 1
            if self.blocks[h] >= 2:
                ev = self.add_score(70, "repeated block")
                if ev:
                    return ev

        # Prune block counter.
        self._trim_counter(self.blocks)

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
            window = list(self.tokens)[-self.entropy_window:]
            counts = Counter(window)

            entropy = 0.0
            for c in counts.values():
                p = c / len(window)
                entropy -= p * math.log2(p)

            if entropy < 2.0:
                ev = self.add_score(30, f"low entropy ({entropy:.2f})")
                if ev:
                    return ev

        # Prune sentence counter (done alongside heavy checks).
        self._trim_counter(self.sentences)

        # Gradual score decay prevents transient spikes from sticking forever.
        self.decay()

        return None


# ── Loop sample saving helper ────────────────────────────────────────────────

# Default path for loop samples: relative to the agent_cascade package directory.
_LOOP_SAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "loop_samples"
)


def save_loop_sample(text, reason, instance_name="", filepath=None):
    """Append a loop detection sample to a JSONL file for debugging and tuning.

    Each line is a JSON object with:
      - timestamp (ISO-8601 UTC), instance_name, reason, token_estimate, text

    Args:
        text: The generated text content that triggered the loop detection.
        reason: Human-readable explanation of why the loop was detected.
        instance_name: Name of the agent instance (e.g., "coder1").
        filepath: Override path for the JSONL file. If None, a daily file is used
            under the ``loop_samples/`` directory relative to this module.
    """
    if not text:
        return

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
    except OSError:
        pass  # Non-critical — don't fail execution over debug logging