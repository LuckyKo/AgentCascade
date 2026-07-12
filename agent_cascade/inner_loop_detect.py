from collections import deque, Counter
import datetime
import json
import math
import os
import re

from agent_cascade.settings import (
    InnerLoopSettings, TOKEN_ESTIMATE_CHAR_DIVISOR, DEFAULT_WORKSPACE,
)

# Precompiled regex patterns (avoid recompilation on every feed call).
_SENTENCE_RE = re.compile(r'([^.?!]+[.?!]|[^.?!]+$)')
_NON_WORD_RE = re.compile(r'\W+')
_WORD_RE = re.compile(r'\b\w+\b')
# Common file extensions whose dots should not trigger sentence splits.
# Word boundary \b at the end prevents matching mid-word (e.g., ". H" in "Hello world. HELLO...").
# Single-letter extensions 'c' and 'h' removed — too short, they overlap with common words.
_FILE_EXT_RE = re.compile(
    r'\.\s*(py|js|ts|jsx|tsx|mjs|cjs|css|scss|less|html|htm|json|yaml|yml|'
    r'toml|xml|md|rst|txt|csv|tsv|rb|go|rs|java|kt|kts|cpp|hpp|cc|cxx|'
    r'sh|bash|zsh|ps1|r|R|ipynb|pdf|png|jpg|jpeg|gif|svg|webp|php|swift|scala)\b',
    re.IGNORECASE,
)


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

        # Cached reference to settings for thresholds and toggle flags used in detection logic.
        self._settings = settings

        self.ngrams = Counter()
        self.blocks = Counter()
        self.sentences = Counter()

        # Track which items have already been scored (one-time scoring per threshold crossing).
        self._scored_sentences = set()
        self._scored_ngrams = set()
        self._scored_blocks = set()

        # One-time entropy gate: prevents +30 from firing every cycle while entropy stays low.
        # Resets to False when entropy recovers above threshold, allowing re-scoring if it drops again.
        self._entropy_scored = False

        self.last_char = None
        self.char_run = 0

        # Internal: total chars fed so far (for min_chars gate) and feed counter.
        self._chars_fed = 0
        self._feed_count = 0

        # Sentence decay: tracks unique sentences added since last halving.
        # When it reaches a threshold, all sentence counts are halved to prevent
        # old entries from permanently dominating the counter.
        self._sentence_decay_counter = 0

        # Track token count at last heavy-check scan so we only slide windows
        # over newly-added tokens, avoiding O(N²) rescanning of old n-grams/blocks.
        self._last_scan_token_count = 0

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
        self._entropy_scored = False
        self._chars_fed = 0
        self._feed_count = 0
        self._sentence_decay_counter = 0
        self._last_scan_token_count = 0

    def _activation_factor(self) -> float:
        """Return 0.0-1.0 indicating how 'active' detection should be.

        At 0 chars fed → 0.0 (detection dormant).
        At min_chars   → 1.0 (full detection strength).
        Linear ramp between the two extremes so that short loops can still
        trigger early when enough repetition accumulates quickly.
        """
        if self._chars_fed >= self.min_chars:
            return 1.0
        # Guaranteed < 1.0 here (the >= case returns above)
        return self._chars_fed / max(self.min_chars, 1)

    # ── Scoring helpers ─────────────────────────────────────────────────

    def decay(self):
        """Gradually reduce score so transient repetitions don't accumulate forever."""
        self.score *= self._settings.score_decay_rate

    def add_score(self, amount, reason):
        """Add to the loop score; return an event dict if threshold is crossed."""
        self.score += amount
        # Hard cap prevents unbounded growth from scoring bugs or edge cases.
        self.score = min(self.score, self._settings.max_score)
        if self.score >= self.threshold:
            return {
                "loop": True,
                "reason": reason,
                "score": round(self.score, 1),
            }
        return None

    # ── Counter maintenance (prune oldest entries when over budget) ─────

    def _trim_counter(
        self,
        counter: Counter,
        max_entries: int | None = None,
        decay: bool = True,
        scored_set: set | None = None,
    ) -> None:
        """Remove least frequent entries when a Counter exceeds capacity.

        Keeps only the top-N by count, optionally halving their values so that
        old entries gradually fade rather than persisting at full strength.
        This is O(k log k) where k = len(counter), called only when over budget.

        Args:
            counter: The Counter to trim.
            max_entries: Maximum entries to keep (defaults to settings).
            decay: If True, halve all remaining counts during pruning.
            scored_set: Optional set of already-scored items; cleared alongside
                the counter when trimming occurs so items can be re-scored.
        """
        if max_entries is None:
            max_entries = self._settings.max_counter_entries
        if len(counter) <= max_entries:
            return
        # Sort by count descending; keep the most frequent entries first.
        sorted_items = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)
        counter.clear()
        if decay:
            # Halve counts during pruning so old entries don't permanently dominate.
            trimmed = {k: max(1, v // 2) for k, v in sorted_items[:max_entries]}
            counter.update(trimmed)
        else:
            # Build a dict from (key, value) pairs so Counter.update restores counts correctly.
            counter.update(dict(sorted_items[:max_entries]))
        # Clear scored set so items can be re-scored after decay/trimming.
        if scored_set is not None:
            scored_set.clear()

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

        # Normalize file extension dots before sentence splitting to avoid
        # "execution_engine.py" being split into separate fragments.
        normalized_text = _FILE_EXT_RE.sub('', self.text)

        # Split accumulated text into sentence chunks in one pass.
        # Track the last match end position; if no sentences are found,
        # the entire buffer is preserved (handles code, poetry, etc.).
        # The regex also captures trailing text without terminal punctuation
        # so that sentences ending mid-stream are still tokenized.
        last_end = 0
        for sent_match in _SENTENCE_RE.finditer(normalized_text):
            sent = sent_match.group(1)
            last_end = sent_match.end()

            # Tokenize with proper word-boundary handling instead of str.split().
            norm = _NON_WORD_RE.sub(' ', sent.lower()).strip()

            if norm:
                toks = _WORD_RE.findall(norm)
                self.tokens.extend(toks)
                # Accumulate sentence counts (checked below via activation factor).
                self.sentences[norm] += 1
                self._sentence_decay_counter += 1

        # Apply periodic halving: counter increments per-sentence (line 240),
        # but the halving check fires at most once per feed() call.
        if self._sentence_decay_counter >= 30:
            self._sentence_decay_counter = 0
            # Efficient in-place halving via Counter.update (avoids for-loop overhead)
            self.sentences.update({k: max(1, v // 2) for k, v in self.sentences.items()})
            # Clear scored set only for sentences that dropped below threshold.
            thresh = self._settings.sentence_repetition_threshold
            to_remove = {s for s in self._scored_sentences if self.sentences[s] < thresh}
            self._scored_sentences -= to_remove

        # Prune sentence counter early to prevent fragment accumulation (cheap check).
        if len(self.sentences) > self._settings.max_counter_entries:
            self._trim_counter(self.sentences, scored_set=self._scored_sentences)

        self.text = self.text[last_end:]

        ##################################################
        # Character repetition (per-char scan — always runs to maintain state)
        ##################################################

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
                    "score": round(self.score + 100, 1),
                }

        ##################################################
        # All detection checks — thresholds scale with activation factor.
        # Light checks (sentence) run every time once active.
        # Heavy checks (n-grams, blocks, entropy) also respect batch_interval.
        ##################################################

        # Compute gradual activation factor (0.0–1.0). At 0 chars fed, detection is
        # dormant — but empty chunks are already filtered above, so factor > 0 here.
        factor = self._activation_factor()

        # Apply decay once per cycle before any scoring. All paths get exactly
        # one decay — early returns from detection blocks don't need a second call.
        self.decay()

        ##################################################
        # Sentence repetition — always runs after min_chars (cheap, no batching)
        ##################################################

        if self._settings.sentence_rep_enabled:
            # Effective sentence threshold scales with activation factor —
            # higher at full activation (8) to require genuine repetition,
            # floored at 7 early on to prevent false positives from chunked fragments.
            _eff_sent_threshold = max(7, round(self._settings.sentence_repetition_threshold * factor))
            for norm, count in self.sentences.items():
                if count >= _eff_sent_threshold and norm not in self._scored_sentences:
                    self._scored_sentences.add(norm)
                    ev = self.add_score(100, "repeated sentence")
                    if ev:
                        return ev

        ##################################################
        # Heavy checks — also respect batch_interval to save CPU per chunk
        # Effective interval scales DOWN with activation so heavy checks run
        # more frequently when text is scarce (catching small loops early).
        ##################################################

        _effective_interval = max(1, int(self.batch_interval * factor))
        if self._feed_count % _effective_interval != 0:
            # Decay already applied above at line ~280 — no double decay needed.
            return None

        ##################################################
        # n-gram detection — true sliding window across all tokens
        #
        # Instead of only checking the last ngram_size tokens (which misses
        # alternating loops like A B C | D E F | A B C | D E F), we slide a
        # window of size ngram_size over every position in the token buffer.
        # To avoid O(N²) rescanning, we only process windows that include at
        # least one newly-added token since the last heavy-check scan.
        ##################################################

        # Compute tokens_list once per cycle — reused by both n-gram and block checks.
        # Avoids creating two separate list copies of the deque on every feed call.
        if self._settings.ngram_rep_enabled or self._settings.block_rep_enabled:
            tokens_list = list(self.tokens)
        else:
            tokens_list = None

        if self._settings.ngram_rep_enabled and len(self.tokens) >= self.ngram_size:
            total = len(tokens_list)

            # When deque is full (len == maxlen), old tokens were evicted, so we must
            # rescan the entire window to catch n-grams formed by new content.
            if self._last_scan_token_count >= total:
                start_idx = 0
            else:
                start_idx = max(0, self._last_scan_token_count - self.ngram_size + 1)

            # Build n-grams using tuple slicing (fast for small windows).
            ng_threshold = self._settings.ngram_repetition_threshold
            ng_counter = self.ngrams
            scored_ng = self._scored_ngrams
            for end in range(start_idx + self.ngram_size - 1, total):
                ng = tuple(tokens_list[end - self.ngram_size + 1:end + 1])
                new_count = ng_counter[ng] + 1
                ng_counter[ng] = new_count
                if new_count >= ng_threshold and ng not in scored_ng:
                    scored_ng.add(ng)
                    ev = self.add_score(90, "repeated ngram")
                    if ev:
                        return ev

        # Prune n-gram counter, clearing scored set so items can be re-scored.
        if len(self.ngrams) > self._settings.max_counter_entries:
            self._trim_counter(self.ngrams, scored_set=self._scored_ngrams)

        ##################################################
        # Block repetition — true sliding window across all tokens
        # Same incremental approach as n-grams but with the larger block_size.
        ##################################################

        if self._settings.block_rep_enabled and len(self.tokens) >= self.block_size:
            # Reuse the shared tokens_list computed above (single deque→list conversion per cycle)
            total = len(tokens_list)

            if self._last_scan_token_count >= total:
                start_idx = 0
            else:
                start_idx = max(0, self._last_scan_token_count - self.block_size + 1)

            blk_threshold = self._settings.block_repetition_threshold
            blk_counter = self.blocks
            scored_blk = self._scored_blocks
            for end in range(start_idx + self.block_size - 1, total):
                blk = tuple(tokens_list[end - self.block_size + 1:end + 1])
                new_count = blk_counter[blk] + 1
                blk_counter[blk] = new_count
                if new_count >= blk_threshold and blk not in scored_blk:
                    scored_blk.add(blk)
                    ev = self.add_score(100, "repeated block")
                    if ev:
                        return ev

        # Prune block counter, clearing scored set so items can be re-scored.
        if len(self.blocks) > self._settings.max_counter_entries:
            self._trim_counter(self.blocks, scored_set=self._scored_blocks)

        # Advance scan pointer so next cycle only processes newly-added tokens.
        self._last_scan_token_count = len(self.tokens)

        

        ##################################################
        # Entropy collapse
        #
        # Shannon entropy of the token distribution in a sliding window.
        # With 128-token windows over natural-language text, typical entropy is
        # ~3.5–4.5 bits (many distinct words). Below 2.0 bits means fewer than
        # ~4 equally-likely tokens dominate — a strong signal of repetition or
        # degenerate generation ("the the the" or repeating phrases).
        ##################################################

        if self._settings.entropy_collapse_enabled and len(self.tokens) >= self.entropy_window:
            window = tuple(self.tokens)[-self.entropy_window:]  # O(k) not O(N)
            counts = Counter(window)

            entropy = 0.0
            for c in counts.values():
                p = c / len(window)
                entropy -= p * math.log2(p)

            if entropy < self._settings.entropy_threshold:
                # One-time gate: only score once per low-entropy period.
                # Resets when entropy recovers, allowing re-scoring on new dips.
                if not self._entropy_scored:
                    self._entropy_scored = True
                    ev = self.add_score(50, f"low entropy ({entropy:.2f})")
                    if ev:
                        return ev
            else:
                # Reset gate when entropy recovers — allows re-scoring next time it drops.
                self._entropy_scored = False

        # Decay already applied before scoring at line ~271 — no double decay needed.
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
        "token_estimate": max(1, len(text) // int(TOKEN_ESTIMATE_CHAR_DIVISOR)),
        "text": text[:8000],  # Cap at ~2K tokens to keep files manageable
    }

    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return filepath
    except OSError:
        return None  # Non-critical — don't fail execution over debug logging