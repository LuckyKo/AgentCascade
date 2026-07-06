from collections import deque, Counter
import hashlib
import math
import re


class InnerLoopDetector:
    def __init__(
        self,
        ngram_size=128,
        block_size=128,
        entropy_window=128,
        char_run_limit=24,
        score_threshold=120,
    ):
        self.text = ""
        self.tokens = []

        self.ngram_size = ngram_size
        self.block_size = block_size
        self.entropy_window = entropy_window
        self.char_run_limit = char_run_limit

        self.score = 0
        self.threshold = score_threshold

        self.ngrams = Counter()
        self.blocks = Counter()
        self.sentences = Counter()

        self.last_char = None
        self.char_run = 0

    def decay(self):
        self.score *= 0.97

    def add_score(self, amount, reason):
        self.score += amount
        if self.score >= self.threshold:
            return {
                "loop": True,
                "reason": reason,
                "score": round(self.score, 1),
            }
        return None

    def feed(self, chunk):
        """
        Feed newly generated text.
        Returns None or a loop event.
        """

        event = None

        ##################################################
        # Character repetition
        ##################################################

        for ch in chunk:
            if ch == self.last_char:
                self.char_run += 1
            else:
                self.last_char = ch
                self.char_run = 1

            if self.char_run > self.char_run_limit:
                event = self.add_score(
                    100,
                    f"character run '{ch}' ({self.char_run})",
                )
                if event:
                    return event

        ##################################################

        self.text += chunk

        ##################################################
        # Sentence repetition
        ##################################################

        while True:
            m = re.search(r'([^.?!]*[.?!])', self.text)
            if not m:
                break

            sent = m.group(1)
            self.text = self.text[len(sent):]

            norm = re.sub(r'\W+', ' ', sent.lower()).strip()

            if norm:
                self.sentences[norm] += 1

                if self.sentences[norm] >= 3:
                    event = self.add_score(
                        80,
                        "repeated sentence",
                    )
                    if event:
                        return event

            toks = norm.split()
            self.tokens.extend(toks)

        ##################################################
        # n-gram detection
        ##################################################

        if len(self.tokens) >= self.ngram_size:

            ng = tuple(self.tokens[-self.ngram_size:])

            h = hash(ng)

            self.ngrams[h] += 1

            if self.ngrams[h] >= 3:
                event = self.add_score(
                    60,
                    "repeated ngram",
                )
                if event:
                    return event

        ##################################################
        # block repetition
        ##################################################

        if len(self.tokens) >= self.block_size:

            block = " ".join(self.tokens[-self.block_size:])

            h = hashlib.sha1(block.encode()).hexdigest()

            self.blocks[h] += 1

            if self.blocks[h] >= 2:
                event = self.add_score(
                    70,
                    "repeated block",
                )
                if event:
                    return event

        ##################################################
        # entropy collapse
        ##################################################

        if len(self.tokens) >= self.entropy_window:

            window = self.tokens[-self.entropy_window:]

            counts = Counter(window)

            entropy = 0

            for c in counts.values():
                p = c / len(window)
                entropy -= p * math.log2(p)

            if entropy < 2.0:
                event = self.add_score(
                    30,
                    f"low entropy ({entropy:.2f})",
                )
                if event:
                    return event

        self.decay()

        return None