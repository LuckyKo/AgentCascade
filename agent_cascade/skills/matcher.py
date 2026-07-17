"""
Skill Matcher — Keyword-based matching for AUTO mode skill resolution.

Builds an inverted index from skill names and descriptions, then scores
incoming queries against that index using simple keyword overlap.

Semantic embedding matching (Phase 3) can be layered on top of this class.
"""

import re
from typing import Dict, List, Tuple

from agent_cascade.log import logger


# Regex for tokenizing: alphanumeric + underscores/hyphens, case-insensitive matching
_TOKEN_RE = re.compile(r'[a-zA-Z0-9_]+(?:[-][a-zA-Z0-9_]+)*')


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

        Args:
            skills_metadata: List of Tier 1 metadata dicts (from SkillManager.get_all_metadata).
                            Each dict should have 'name' and 'description' keys.
        """
        self._inverted_index.clear()
        logger.debug("[SKILLS] Building inverted index from %d skills", len(skills_metadata))

        for meta in skills_metadata:
            skill_name = meta.get('name', '')
            if not skill_name:
                continue

            description = meta.get('description', '')
            text = f"{skill_name} {description}"
            keywords = _TOKEN_RE.findall(text.lower())

            for kw in set(keywords):  # Deduplicate per-skill to avoid index bloat
                if kw not in self._inverted_index:
                    self._inverted_index[kw] = []
                if skill_name not in self._inverted_index[kw]:
                    self._inverted_index[kw].append(skill_name)

        total_keywords = len(self._inverted_index)
        logger.debug("[SKILLS] Inverted index built: %d unique keywords", total_keywords)

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
            logger.debug("[SKILLS] Empty index — no matches possible")
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
        results = [(name, min(score / max(len(query_tokens), 1), 1.0))
                   for name, score in scores.items() if score > 0]

        # Sort by relevance score descending
        results.sort(key=lambda x: x[1], reverse=True)

        logger.debug("[SKILLS] Match query '%s' → %d results (top=%s)",
                     query[:80], len(results),
                     results[0][0] if results else 'none')
        return results