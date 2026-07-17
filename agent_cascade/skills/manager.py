"""
Skill Manager — Central coordinator for skill discovery, loading and resolution.

Handles:
  - Scanning directories for SKILL.md files (discover)
  - Storing Tier 1 metadata in a registry
  - Loading full instructions on-demand (Tier 2)
  - Resolving load_skill arguments (list / AUTO / NONE)
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agent_cascade.log import logger
from agent_cascade.settings import LOAD_SKILL_AUTO, LOAD_SKILL_NONE, SKILL_MATCH_THRESHOLD

from .parser import parse_skill_file
from .matcher import SkillMatcher


# Priority levels for duplicate skill name resolution:
# Higher number = higher priority (wins over lower)
_PRIORITY_SYSTEM = 1       # System-level skills (.qwen/skills/)
_PRIORITY_AGENT = 2        # Agent-specific skills (agents/*/skills/)
_PRIORITY_USER = 3         # User-defined skills (workspace/skills/)


class SkillManager:
    """Manages skill discovery, registration and resolution.

    The registry stores Tier 1 metadata at startup for token efficiency.
    Full instructions (Tier 2) are loaded only when explicitly requested.
    """

    def __init__(self):
        self._skills_registry: Dict[str, Dict[str, Any]] = {}  # name -> parsed skill data
        self._matcher = SkillMatcher()

    # ── Discovery ────────────────────────────────────────────────────────────

    async def discover(self, skill_paths: List[Path]) -> None:
        """Scan directories for SKILL.md files and register their metadata.

        Walks each provided directory looking for `*/SKILL.md` patterns.
        Parses frontmatter (Tier 1) only — full body is loaded lazily.

        Duplicate names are resolved by priority: system < agent-specific < user-defined.

        Args:
            skill_paths: List of root directories to scan for skills.
        """
        logger.info("[SKILLS] Starting skill discovery across %d paths", len(skill_paths))
        
        found_count = 0
        skipped_count = 0

        for root in skill_paths:
            if not root.exists():
                logger.debug("[SKILLS] Skill directory does not exist, skipping: %s", root)
                continue

            # Walk for SKILL.md files (look one level deep: root/*/SKILL.md)
            try:
                for skill_dir in root.iterdir():
                    if not skill_dir.is_dir():
                        continue
                    skill_file = skill_dir / 'SKILL.md'
                    if not skill_file.exists():
                        continue

                    await self._register_single(skill_file, priority=_PRIORITY_SYSTEM)
                    found_count += 1
            except OSError as e:
                logger.warning("[SKILLS] Error scanning %s: %s", root, e)

        # Rebuild the matcher index after registration
        self._rebuild_index()
        logger.info(
            "[SKILLS] Discovery complete: %d skills registered, %d in registry",
            found_count, len(self._skills_registry),
        )

    async def _register_single(self, skill_file: Path, priority: int = _PRIORITY_SYSTEM) -> None:
        """Parse and register a single SKILL.md file.

        Args:
            skill_file: Path to the SKILL.md file.
            priority: Priority level for duplicate resolution.
        """
        try:
            parsed = parse_skill_file(skill_file)
        except (FileNotFoundError, OSError) as e:
            logger.warning("[SKILLS] Failed to read skill file %s: %s", skill_file, e)
            return

        frontmatter = parsed.get('frontmatter', {})
        name = frontmatter.get('name')
        if not name:
            # Fall back to directory name
            name = skill_file.parent.name
            logger.debug("[SKILLS] Skill file %s has no 'name' in frontmatter, using dir: %s",
                         skill_file, name)

        existing = self._skills_registry.get(name)
        if existing is not None:
            existing_priority = existing.get('_priority', _PRIORITY_SYSTEM)
            if priority <= existing_priority:
                logger.debug(
                    "[SKILLS] Duplicate skill '%s' (priority %d < %d), skipping",
                    name, priority, existing_priority,
                )
                return
            logger.debug("[SKILLS] Replacing skill '%s' with higher priority (%d > %d)",
                         name, priority, existing_priority)

        # Store parsed data in registry (Tier 1: frontmatter only; body is lazy-loaded)
        self._skills_registry[name] = {
            'name': name,
            'description': frontmatter.get('description', ''),
            'source': frontmatter.get('source', ''),
            'file_path': str(skill_file),
            '_priority': priority,
            # Keep a reference to the full parsed data for lazy loading
            '_parsed_data': parsed,
        }

    # ── Index Management ─────────────────────────────────────────────────────

    def _rebuild_index(self) -> None:
        """Rebuild the SkillMatcher inverted index from current registry."""
        try:
            metadata = self.get_all_metadata()
            self._matcher.build_index(metadata)
        except Exception as e:
            logger.debug("[SKILLS] Failed to rebuild matcher index: %s", e)

    # ── Tier 1 Queries (Metadata Only) ───────────────────────────────────────

    def get_skill_metadata(self, skill_name: str) -> Optional[Dict[str, Any]]:
        """Return Tier 1 metadata for a skill by name.

        Args:
            skill_name: The registered skill name.

        Returns:
            Metadata dict or None if not found.
        """
        return self._skills_registry.get(skill_name)

    def match_skills(self, query: str) -> List[Tuple[str, float]]:
        """Public interface for matching skills against a query.

        Rebuilds the matcher index if no skills are registered yet (lazy init).

        Args:
            query: The task text or context to match against.

        Returns:
            List of (skill_name, relevance_score) tuples sorted by score descending.
        """
        if not self._skills_registry and not self._matcher._inverted_index:
            self._rebuild_index()
        return self._matcher.match(query)

    def get_all_metadata(self) -> List[Dict[str, Any]]:
        """Return all Tier 1 metadata (for scan_skills tool).

        Returns a list of dicts with 'name' and 'description' keys, suitable
        for display or matching. Internal fields (_priority, _parsed_data) are excluded.
        """
        result = []
        for name, data in self._skills_registry.items():
            result.append({
                'name': data.get('name', name),
                'description': data.get('description', ''),
            })
        return result

    # ── Tier 2 Loading (Full Instructions) ───────────────────────────────────

    def load_full_instructions(self, skill_name: str) -> Optional[str]:
        """Load full SKILL.md body (Tier 2) for a skill.

        Args:
            skill_name: The registered skill name.

        Returns:
            Full markdown instructions string, or None if skill not found.
        """
        reg = self._skills_registry.get(skill_name)
        if reg is None:
            logger.warning("[SKILLS] Requested full instructions for unknown skill: %s", skill_name)
            return None

        # Try lazy load from parsed data first
        parsed = reg.get('_parsed_data')
        if parsed and 'body' in parsed:
            body = parsed['body']
            logger.debug("[SKILLS] Loaded Tier 2 instructions for '%s' (%d chars)",
                         skill_name, len(body))
            return body

        # Fallback: re-read from disk
        file_path = reg.get('file_path')
        if file_path:
            try:
                parsed = parse_skill_file(Path(file_path))
                reg['_parsed_data'] = parsed
                body = parsed.get('body', '')
                logger.debug("[SKILLS] Re-parsed '%s' from disk (%d chars)", skill_name, len(body))
                return body
            except (FileNotFoundError, OSError) as e:
                logger.warning("[SKILLS] Failed to re-parse '%s': %s", skill_name, e)

        return None

    # ── Resolution (load_skill argument handling) ────────────────────────────

    def resolve_load_skill(
        self,
        load_skill_value: Union[List[str], str, None],
        task_text: str = "",
        context_text: str = "",
    ) -> List[str]:
        """Resolve the load_skill argument value to actual skill content.

        Args:
            load_skill_value: One of:
                - list[str]: Named skills to load (e.g., ["httpx-connection-pooling"])
                - "AUTO": Auto-match relevant skills from task+context text
                - "NONE": No skill loading
                - None/omitted: Falls back to default behavior (AUTO)
            task_text: Task description for AUTO mode matching.
            context_text: Additional context for AUTO mode matching.

        Returns:
            List of full instruction strings (one per loaded skill).
        """
        # Handle NONE / empty (case-insensitive, whitespace-tolerant)
        if load_skill_value is None or (isinstance(load_skill_value, str) and load_skill_value.strip().upper() == LOAD_SKILL_NONE):
            return []

        # Handle explicit list of skill names
        if isinstance(load_skill_value, list):
            instructions = []
            for name in load_skill_value:
                body = self.load_full_instructions(name)
                if body:
                    instructions.append(body)
                else:
                    logger.debug("[SKILLS] Skill '%s' not found — silently skipping", name)
            return instructions

        # Handle AUTO mode (case-insensitive, whitespace-tolerant)
        if isinstance(load_skill_value, str):
            if load_skill_value.strip().upper() == LOAD_SKILL_AUTO:
                query = f"{task_text} {context_text}".strip()
                # Use public API (match_skills) which handles lazy index rebuild
                matches = self.match_skills(query)
                if not matches:
                    logger.debug("[SKILLS] AUTO mode — no matching skills for query")
                    return []

                # Load instructions for top matches above configured threshold
                instructions = []
                for name, score in matches:
                    if score < SKILL_MATCH_THRESHOLD:
                        continue
                    body = self.load_full_instructions(name)
                    if body:
                        logger.debug("[SKILLS] AUTO loaded skill '%s' (score=%.2f)", name, score)
                        instructions.append(body)

                return instructions

            # Unknown string value — treat as NONE
            logger.debug("[SKILLS] Unknown load_skill value: %s", load_skill_value)
            return []

        return []