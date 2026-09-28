"""
Skill Matcher — Keyword-based matching for AUTO mode skill resolution.

Builds an inverted index from skill names and descriptions, then scores
incoming queries against that index using simple keyword overlap.

Semantic embedding matching (Phase 3) can be layered on top of this class.
"""

import math
import re
from difflib import SequenceMatcher
from typing import Dict, FrozenSet, List, Optional, Set, Tuple, Union

from agent_cascade.log import logger

# Regex for tokenizing: alphanumeric + underscores/hyphens, case-insensitive matching
_TOKEN_RE = re.compile(r'[a-zA-Z0-9_]+(?:[-][a-zA-Z0-9_]+)*')

# G+C3 scorer knobs (todo 122+123 plan §1.6/§4): field weights, minimum indexed token
# length, minimum distinct matched terms, and the top-k cap returned by match().
# Frozen so no caller can mutate the scorer knobs at runtime (POLISH rev122 #1).
_FIELD_WEIGHTS = frozenset((('name', 3.0), ('desc', 1.0), ('trig', 1.5)))
_FIELD_WEIGHT_MAP = dict(_FIELD_WEIGHTS)  # derived once for O(1) per-field lookup in match()
_MIN_TOKEN_LEN = 2
_MIN_MATCHED_TERMS = 2
_TOP_K = 10


def skill_frontmatter_text(name: str, description: str,
                           triggers: Optional[Union[List[str], str]]) -> str:
    """Build the comparable frontmatter text for a skill.

    Concatenates only the fields the matcher indexes (name + description + triggers),
    normalizing whitespace. The body is deliberately excluded: it can be up to 15 KB and
    difflib on that is slow, while frontmatter is what identifies a skill's intent.

    ``triggers`` may be a list, a single string, or missing/None — all are normalized to
    plain text (missing → empty).
    """
    if triggers is None:
        trigger_text = ''
    elif isinstance(triggers, (list, tuple)):
        trigger_text = ' '.join(str(t) for t in triggers)
    else:
        trigger_text = str(triggers)
    raw = f"{name or ''} {description or ''} {trigger_text}"
    return re.sub(r'\s+', ' ', raw).strip()


def skill_similarity(text_a: str, text_b: str) -> float:
    """Return the difflib SequenceMatcher ratio (0.0–1.0) between two frontmatter texts."""
    return SequenceMatcher(None, text_a, text_b).ratio()


def find_similar_skills(proposed_name: str, proposed_description: str,
                        proposed_triggers: Optional[Union[List[str], str]],
                        existing_metadata: List[Dict], threshold: float,
                        exclude_names: Optional[List[str]] = None) -> List[Tuple[str, float]]:
    """Find existing skills whose frontmatter text is MORE similar than ``threshold``.

    Compares the proposal against every entry in ``existing_metadata`` (each a dict with
    'name'/'description'/'triggers'), skipping any name in ``exclude_names`` (used to let an
    UPDATE resemble its own incumbent). Returns (name, similarity) tuples sorted by
    similarity descending. Only skills with similarity strictly greater than the threshold
    are returned.
    """
    proposed_text = skill_frontmatter_text(proposed_name, proposed_description, proposed_triggers)
    exclude = set(exclude_names or [])
    collisions: List[Tuple[str, float]] = []
    for meta in existing_metadata:
        name = meta.get('name', '')
        if not name or name in exclude:
            continue
        other_text = skill_frontmatter_text(name, meta.get('description', ''), meta.get('triggers', []))
        sim = skill_similarity(proposed_text, other_text)
        if sim > threshold:
            collisions.append((name, sim))
    collisions.sort(key=lambda x: (-x[1], x[0]))
    return collisions


class SkillMatcher:
    """Keyword-based skill matcher using a field-aware inverted index (G+C3 scorer).

    The field index maps each keyword to the skills that contain it and WHICH
    fields (name / description / triggers) they appear in. Matching scores with
    coverage-normalized IDF weighting (todo 122+123, plan option G+C3):

      score(skill) = sum over matched query terms of
                     idf(term) * max(field weight for that term on the skill)
                     / (sum of idf over ALL query terms * max field weight)

    capped at 1.0. A skill must match at least ``_MIN_MATCHED_TERMS`` distinct
    query terms, and only the top ``_TOP_K`` results are returned. This replaces
    the old unweighted overlap fraction: it is IDF-aware (rare terms dominate),
    field-weighted (a name hit beats a description hit), coverage-normalized
    (query length no longer deflates scores) and volume-bounded (top-10).

    Semantic embedding matching (Phase 3) can be layered on top of this class.
    """

    def __init__(self):
        # keyword -> {skill_name: frozenset(fields)}; frozen so the index is shareable/immutable
        self._field_index: Dict[str, Dict[str, FrozenSet[str]]] = {}
        # keyword -> idf weight, log(1 + N / df) where N = indexed skill count, df = doc frequency
        self._idf: Dict[str, float] = {}

    # ── Index Building ───────────────────────────────────────────────────────

    def build_index(self, skills_metadata: List[Dict]) -> None:
        """Build the G+C3 field index + IDF table from skill metadata.

        Tokenizes each skill's name, description and triggers into keywords
        (minimum length ``_MIN_TOKEN_LEN``) and records, per keyword, which
        fields of which skills contain it. IDF is computed over the full corpus:
        ``log(1 + N / df)`` with N = number of indexed skills.

        Atomic (BUG_0020): builds into local dicts and swaps them in only on
        success, so a mid-build failure leaves the PREVIOUS index intact rather
        than a cleared/partial one that silently disables all skill matching.
        Per-skill isolation: one malformed metadata entry is skipped + logged
        at WARNING instead of aborting the whole build.

        Args:
            skills_metadata: List of Tier 1 metadata dicts (from SkillManager.get_all_metadata).
                            Each dict should have 'name' and 'description' keys; 'triggers'
                            (list[str]) is optional.
        """
        logger.debug('[SKILLS] Building field index from %d skills', len(skills_metadata))
        new_index: Dict[str, Dict[str, Set[str]]] = {}
        n_skills = 0

        for meta in skills_metadata:
            try:
                skill_name = meta.get('name', '')
                if not skill_name:
                    continue
                n_skills += 1

                triggers = meta.get('triggers', [])
                # Root cause (BUG_0019) is fixed at the parser layer: _normalize_frontmatter guarantees
                # list[str] for parser-fed metadata. The container isinstance check + per-item str filter
                # remain only as a total-join guard for hand-built fixtures that call build_index directly,
                # bypassing the parser (a non-str item would otherwise make ' '.join raise).
                if isinstance(triggers, list):
                    trigger_text = ' '.join(t for t in triggers if isinstance(t, str))
                else:
                    trigger_text = ''

                field_texts = (('name', skill_name),
                               ('desc', meta.get('description', '') or ''),
                               ('trig', trigger_text))
                for field, text in field_texts:
                    for kw in set(_TOKEN_RE.findall(text.lower())):
                        if len(kw) < _MIN_TOKEN_LEN:
                            continue
                        new_index.setdefault(kw, {}).setdefault(skill_name, set()).add(field)
            except Exception as e:
                # Per-skill isolation (BUG_0020): one bad entry must not abort the build.
                logger.warning('[SKILLS] Skipping malformed skill metadata during index build '
                               '(name=%r): %s', meta.get('name') if isinstance(meta, dict) else None, e)

        # Freeze per-skill field sets and derive IDF over the full corpus.
        frozen: Dict[str, Dict[str, FrozenSet[str]]] = {
            kw: {name: frozenset(fields) for name, fields in names.items()}
            for kw, names in new_index.items()
        }
        idf = {kw: math.log(1 + n_skills / len(names)) for kw, names in new_index.items()}

        # Atomic swap: only replace shared state once the build fully succeeded.
        self._field_index = frozen
        self._idf = idf
        logger.debug('[SKILLS] Field index built: %d unique keywords over %d skills',
                     len(frozen), n_skills)

    # ── Matching ─────────────────────────────────────────────────────────────

    def match(self, query: str, k: Optional[int] = None) -> List[Tuple[str, float]]:
        """Match a query against indexed skills using the G+C3 scorer.

        Coverage-normalized IDF scoring with field weights (todo 122+123, plan
        option G+C3): for each matched query term, add idf(term) * the highest
        field weight that term carries on the skill; divide by the sum of
        idf over ALL query terms * max field weight (so a long generic query
        cannot inflate or deflate scores); cap at 1.0. Skills matching fewer
        than ``_MIN_MATCHED_TERMS`` distinct query terms are dropped, and only
        the top ``_TOP_K`` results are returned.

        Args:
            query: The task text or context to match against.
            k: Optional result cap overriding ``_TOP_K`` for this call (e.g. the
                skill advisor asks for an uncapped ranking so its own 20-candidate
                pre-filter stays satisfiable). ``None`` keeps the default top-10.

        Returns:
            List of (skill_name, relevance_score) tuples sorted by score descending,
            capped at ``k`` (default ``_TOP_K``). Only skills with score > 0 are returned.
        """
        if not self._field_index:
            logger.debug('[SKILLS] Empty index — no matches possible')
            return []

        query_tokens = set(_TOKEN_RE.findall(query.lower()))
        if not query_tokens:
            return []

        # Normalizer: total IDF mass of the query * max field weight. A skill that
        # matched every query term in its strongest field scores exactly 1.0.
        wmax = max(w for _, w in _FIELD_WEIGHTS)
        total = sum(self._idf.get(kw, 0.0) for kw in query_tokens) * wmax
        if total <= 0:
            return []

        # Accumulate per-skill score + distinct matched-term count over the QUERY
        # tokens (not the whole index) — O(query_terms), independent of corpus size.
        scores: Dict[str, float] = {}
        term_counts: Dict[str, int] = {}
        for kw in query_tokens:
            w = self._idf.get(kw, 0.0)
            if w == 0.0:
                continue
            for name, fields in self._field_index[kw].items():
                scores[name] = scores.get(name, 0.0) + w * max(
                    _FIELD_WEIGHT_MAP.get(f, 1.0) for f in fields)
                term_counts[name] = term_counts.get(name, 0) + 1

        # Min-2 gate: a single shared word (e.g. "test") must not qualify a skill.
        results = [(name, min(score / total, 1.0))
                   for name, score in scores.items() if term_counts[name] >= _MIN_MATCHED_TERMS]

        # Sort by relevance score descending, then by name ascending for stability
        # (deterministic output ensures KV cache prefix identity across retries)
        results.sort(key=lambda x: (-x[1], x[0]))
        if k is not None and k >= 0:
            results = results[:k]
        else:
            results = results[:_TOP_K]

        logger.debug("[SKILLS] Match query '%s' → %d results (top=%s)", query[:80], len(results),
                     results[0][0] if results else 'none')
        return results
