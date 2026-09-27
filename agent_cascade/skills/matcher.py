"""
Skill Matcher — Keyword-based matching for AUTO mode skill resolution.

Builds an inverted index from skill names and descriptions, then scores
incoming queries against that index using simple keyword overlap.

Semantic embedding matching (Phase 3) can be layered on top of this class.
"""

import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple, Union

from agent_cascade.log import logger

# Regex for tokenizing: alphanumeric + underscores/hyphens, case-insensitive matching
_TOKEN_RE = re.compile(r'[a-zA-Z0-9_]+(?:[-][a-zA-Z0-9_]+)*')


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
    """Keyword-based skill matcher using an inverted index.

    The inverted index maps each keyword (from skill names and descriptions)
    to a list of skill names that contain it. Matching scores based on how
    many query keywords overlap with indexed skill keywords.
    """

    def __init__(self):
        self._inverted_index: Dict[str, List[str]] = {}  # keyword -> [skill_names]

    # ── Index Building ───────────────────────────────────────────────────────

    def build_index(self, skills_metadata: List[Dict]) -> None:
        """Build inverted index from skill names + descriptions.

        Tokenizes each skill's name and description into keywords, then maps
        those keywords back to the skill name for fast lookup during matching.

        Atomic (BUG_0020): builds into a local dict and swaps it in only on
        success, so a mid-build failure leaves the PREVIOUS index intact rather
        than a cleared/partial one that silently disables all skill matching.
        Per-skill isolation: one malformed metadata entry is skipped + logged
        at WARNING instead of aborting the whole build.

        Args:
            skills_metadata: List of Tier 1 metadata dicts (from SkillManager.get_all_metadata).
                            Each dict should have 'name' and 'description' keys.
        """
        logger.debug('[SKILLS] Building inverted index from %d skills', len(skills_metadata))
        new_index: Dict[str, List[str]] = {}

        for meta in skills_metadata:
            try:
                skill_name = meta.get('name', '')
                if not skill_name:
                    continue

                description = meta.get('description', '')
                triggers = meta.get('triggers', [])
                # Root cause (BUG_0019) is fixed at the parser layer: _normalize_frontmatter guarantees
                # list[str] for parser-fed metadata. The container isinstance check + per-item str filter
                # remain only as a total-join guard for hand-built fixtures that call build_index directly,
                # bypassing the parser (a non-str item would otherwise make ' '.join raise).
                if isinstance(triggers, list):
                    trigger_text = ' '.join(t for t in triggers if isinstance(t, str))
                else:
                    trigger_text = ''
                text = f"{skill_name} {description} {trigger_text}"
                keywords = _TOKEN_RE.findall(text.lower())

                for kw in set(keywords):  # Deduplicate per-skill to avoid index bloat
                    if kw not in new_index:
                        new_index[kw] = []
                    if skill_name not in new_index[kw]:
                        new_index[kw].append(skill_name)
            except Exception as e:
                # Per-skill isolation (BUG_0020): one bad entry must not abort the build.
                logger.warning('[SKILLS] Skipping malformed skill metadata during index build '
                               '(name=%r): %s', meta.get('name') if isinstance(meta, dict) else None, e)

        # Atomic swap: only replace shared state once the build fully succeeded.
        self._inverted_index = new_index
        total_keywords = len(new_index)
        logger.debug('[SKILLS] Inverted index built: %d unique keywords', total_keywords)

    # ── Matching ─────────────────────────────────────────────────────────────

    def match(self, query: str) -> List[Tuple[str, float]]:
        """Match a query against indexed skills using keyword overlap scoring.

        Scores each skill by the fraction of its indexed keywords that appear
        in the query. This gives higher scores to more specific matches while
        still catching broad relevance.

        Args:
            query: The task text or context to match against.

        Returns:
            List of (skill_name, relevance_score) tuples sorted by score descending.
            Only skills with score > 0 are returned.
        """
        if not self._inverted_index:
            logger.debug('[SKILLS] Empty index — no matches possible')
            return []

        query_tokens = set(_TOKEN_RE.findall(query.lower()))
        if not query_tokens:
            return []

        # Count how many of each skill's keywords appear in the query
        scores: Dict[str, float] = {}
        for kw, skill_names in self._inverted_index.items():
            if kw in query_tokens:
                for name in skill_names:
                    scores[name] = scores.get(name, 0.0) + 1.0

        # Normalize by total matching keywords to avoid bias toward verbose skills
        results = [(name, min(score / max(len(query_tokens), 1), 1.0)) for name, score in scores.items() if score > 0]

        # Sort by relevance score descending, then by name ascending for stability
        # (deterministic output ensures KV cache prefix identity across retries)
        results.sort(key=lambda x: (-x[1], x[0]))

        logger.debug("[SKILLS] Match query '%s' → %d results (top=%s)", query[:80], len(results),
                     results[0][0] if results else 'none')
        return results
