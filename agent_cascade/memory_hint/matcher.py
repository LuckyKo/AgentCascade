"""Memory matcher — TF-IDF + cosine similarity over per-vault lesson indexes.

Stdlib-only (``math`` / ``re``). This is a *heuristic* keyword matcher, not a
semantic one: it scores a turn's text against the frontmatter identity and body
of each lesson in the vaults. The plan (§3.3) specifies the exact weighting and
smoothing so results are deterministic and cheap to compute on small vaults.

Per-vault indexes are built by :class:`~agent_cascade.memory_hint.vault.VaultIndex`
and merged at match time by the manager (plan §4 / Q3). Each document is a single
lesson file with two weighted token fields:

* identity field (weight ``W_ID``) — frontmatter name/description/tags/aliases
* body field (weight ``W_BODY``) — markdown body, chunked so long lessons don't dominate

The query vector uses the body scale only (no identity boost on the query).
"""

import math
import re
from typing import Dict, List, Tuple

# Reuse the exact token pattern from skills/matcher.py:17 for consistency.
_TOKEN_RE = re.compile(r'[a-zA-Z0-9_]+(?:[-][a-zA-Z0-9_]+)*')

# Field weights (plan §3.3). Identity is weighted 3× so a query matching only a
# lesson's name/tags still ranks it above a body-only overlap of the same size.
W_ID = 3.0
W_BODY = 1.0

# Body chunking window (chars). A long lesson is split into overlapping-free
# windows so no single huge file dominates the cosine denominator.
_BODY_CHUNK_CHARS = 512


def _tokenize(text: str) -> List[str]:
    """Lowercased token list using the shared skill-matcher pattern."""
    return _TOKEN_RE.findall((text or '').lower())


class MemoryMatcher:
    """TF-IDF + cosine matcher over a set of per-vault indexes.

    A "document" is one lesson file, keyed by its vault-relative path (e.g.
    ``auto-skill-inloop-trigger-implementation.md``). The matcher holds the union
    of all documents across every vault; matching iterates them and merges scores.
    """

    def __init__(self):
        # rel_path -> {"vec": {token: weighted_tf}, "norm": float}
        self._docs: Dict[str, dict] = {}
        # token -> document frequency (number of docs containing the token)
        self._df: Dict[str, int] = {}
        self._n_docs: int = 0

    # ── Index building ───────────────────────────────────────────────────────

    def set_documents(self, documents: Dict[str, Tuple[str, str]]) -> None:
        """(Re)build the full index from ``{rel_path: (identity_text, body_text)}``.

        Called by the manager after a rescan. Rebuilding the whole union is cheap
        for the small vault sizes this feature targets; per-file mtime tracking
        happens in :class:`~agent_cascade.memory_hint.vault.VaultIndex`, which decides
        *whether* to call this.
        """
        self._docs.clear()
        self._df.clear()

        # Pass 1: build raw weighted term frequencies per document + global df.
        raw: Dict[str, Dict[str, float]] = {}
        for rel_path, (identity_text, body_text) in documents.items():
            counts: Dict[str, int] = {}
            for tok in _tokenize(identity_text):
                counts[tok] = counts.get(tok, 0) + 1
            for chunk in _chunk(body_text):
                for tok in _tokenize(chunk):
                    counts[tok] = counts.get(tok, 0) + 1
            raw[rel_path] = counts

        self._n_docs = len(raw)
        for counts in raw.values():
            for tok in counts:
                self._df[tok] = self._df.get(tok, 0) + 1

        # Pass 2: weight each doc's terms (identity × W_ID, body × W_BODY) and cache norm.
        # We recompute the identity/body split weighting by re-tokenizing so the
        # identity terms carry W_ID while body terms carry W_BODY. To do that we need
        # per-field counts, so rebuild with field awareness.
        for rel_path, (identity_text, body_text) in documents.items():
            vec = self._weighted_vector(identity_text, body_text)
            norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
            self._docs[rel_path] = {'vec': vec, 'norm': norm}

    def _idf(self, token: str) -> float:
        """Smoothed IDF: log((N + 1) / (df(t) + 1)) + 1 (plan §3.3)."""
        return math.log((self._n_docs + 1) / (self._df.get(token, 0) + 1)) + 1.0

    def _weighted_vector(self, identity_text: str, body_text: str) -> Dict[str, float]:
        """Build a document vector: identity tokens × W_ID, body tokens × W_BODY.

        tf(t, d) = count(t, d) / len(d); the weighted term weight is
        ``field_weight * tf * idf``. Identity and body are counted separately so
        each carries its own field weight.
        """
        vec: Dict[str, float] = {}

        def _add_field(text: str, weight: float) -> None:
            counts: Dict[str, int] = {}
            for chunk in _chunk(text):
                for tok in _tokenize(chunk):
                    counts[tok] = counts.get(tok, 0) + 1
            total = sum(counts.values())
            if not total:
                return
            for tok, cnt in counts.items():
                tf = cnt / total
                vec[tok] = vec.get(tok, 0.0) + weight * tf * self._idf(tok)

        _add_field(identity_text, W_ID)
        _add_field(body_text, W_BODY)
        return vec

    # ── Matching ─────────────────────────────────────────────────────────────

    def match(self, query: str) -> List[Tuple[str, float]]:
        """Score every document against ``query``. Returns ``(rel_path, cosine)`` desc.

        The query vector uses the body scale (W_BODY) only — no identity boost on
        the query side. Documents scoring 0 are omitted.
        """
        if not self._docs or not (query and query.strip()):
            return []

        q_counts: Dict[str, int] = {}
        for chunk in _chunk(query):
            for tok in _tokenize(chunk):
                q_counts[tok] = q_counts.get(tok, 0) + 1
        q_total = sum(q_counts.values())
        if not q_total:
            return []

        # Query vector (body scale).
        q_vec: Dict[str, float] = {}
        for tok, cnt in q_counts.items():
            tf = cnt / q_total
            q_vec[tok] = W_BODY * tf * self._idf(tok)
        q_norm = math.sqrt(sum(w * w for w in q_vec.values()))
        if q_norm == 0.0:
            return []

        results: List[Tuple[str, float]] = []
        for rel_path, doc in self._docs.items():
            dvec = doc['vec']
            # Dot product over the smaller map's keys.
            dot = 0.0
            if len(q_vec) < len(dvec):
                small, big = q_vec, dvec
            else:
                small, big = dvec, q_vec
            for tok, w in small.items():
                other = big.get(tok)
                if other:
                    dot += w * other
            score = dot / (q_norm * doc['norm'])
            if score > 0.0:
                results.append((rel_path, score))

        results.sort(key=lambda x: (-x[1], x[0]))
        return results


def _chunk(text: str) -> List[str]:
    """Split text into fixed-size windows (last window may be shorter)."""
    text = text or ''
    if not text:
        return []
    if len(text) <= _BODY_CHUNK_CHARS:
        return [text]
    return [text[i:i + _BODY_CHUNK_CHARS] for i in range(0, len(text), _BODY_CHUNK_CHARS)]
