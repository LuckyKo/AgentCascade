"""
Skill Manager — Central coordinator for skill discovery, loading and resolution.

Handles:
  - Scanning directories for SKILL.md files (discover)
  - Storing Tier 1 metadata in a registry
  - Loading full instructions on-demand (Tier 2)
  - Resolving load_skill arguments (list / AUTO / NONE)
"""

import sys as _sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agent_cascade.log import logger
from agent_cascade.settings import (
    AUTO_SKILL_AUTO_PROMOTE,
    AUTO_SKILL_EXTRA_TURNS,
    AUTO_SKILL_MAX_PER_SESSION,
    AUTO_SKILL_MIN_TOOL_CALLS,
    LOAD_SKILL_AUTO,
    LOAD_SKILL_NONE,
    SKILL_CACHE_TTL_SECONDS,
    SKILL_MATCH_THRESHOLD,
    SKILLS_DISABLED,
)

from .parser import parse_skill_file
from .matcher import SkillMatcher
from .validator import validate_skill
from .cache_helper import compute_scan_signature


# Priority levels for duplicate skill name resolution:
# Higher number = higher priority (wins over lower)
_PRIORITY_SYSTEM = 1       # System-level skills (.qwen/skills/)
_PRIORITY_AGENT = 2        # Agent-specific skills (agents/*/skills/)
_PRIORITY_USER = 3         # User-defined skills (workspace/skills/)

# Platform filtering: maps frontmatter platform names to sys.platform values
_PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}


def _skill_matches_platform(frontmatter: dict) -> bool:
    """Check if a skill is compatible with the current OS.

    If 'platforms' field is absent or empty, skill is compatible with all.
    """
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = _sys.platform
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = _PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


class SkillManager:
    """Manages skill discovery, registration and resolution.

    The registry stores Tier 1 metadata at startup for token efficiency.
    Full instructions (Tier 2) are loaded only when explicitly requested.
    """

    def __init__(self):
        self._skills_registry: Dict[str, Dict[str, Any]] = {}  # name -> parsed skill data
        self._matcher = SkillMatcher()
        self._write_lock = threading.RLock()
        self._cache_signature: Tuple = None
        self._cache_timestamp: float = 0.0
        self._cache_ttl: float = SKILL_CACHE_TTL_SECONDS  # from settings
        self._disabled_names: set = set(SKILLS_DISABLED)
        self._skill_paths: List[Path] = []  # stored for _ensure_discovered()

    # ── Discovery ────────────────────────────────────────────────────────────

    def _ensure_discovered(self) -> None:
        """Trigger discovery if cache expired or paths changed (cache-respecting).

        Safe to call from tools — skips if within TTL. Does nothing if no paths configured.
        """
        if not self._skill_paths:
            return
        self.discover(self._skill_paths)

    def discover(self, skill_paths: List[Path]) -> None:
        """Scan directories for SKILL.md files and register their metadata.

        Walks each provided directory looking for `*/SKILL.md` patterns.
        Parses frontmatter (Tier 1) only — full body is loaded lazily.

        Duplicate names are resolved by priority: system < agent-specific < user-defined.

        Args:
            skill_paths: List of root directories to scan for skills.
        """
        # Cache check (use monotonic clock for reliability)
        current_sig = compute_scan_signature(skill_paths, frozenset(self._disabled_names))
        now = time.monotonic()
        if (current_sig == self._cache_signature
                and (now - self._cache_timestamp) < self._cache_ttl):
            logger.debug("[SKILLS] Cache hit — skipping discovery (age=%.1fs)",
                         now - self._cache_timestamp)
            return

        logger.info("[SKILLS] Starting skill discovery across %d paths", len(skill_paths))

        # Store paths for _ensure_discovered() hot-reload support
        self._skill_paths = list(skill_paths)

        # Phase 1: Scan and parse outside lock (I/O bound)
        collected: list = []
        found_count = 0
        skipped_count = 0

        for root in skill_paths:
            if not root.exists():
                logger.debug("[SKILLS] Skill directory does not exist, skipping: %s", root)
                continue

            try:
                for skill_dir in root.iterdir():
                    if not skill_dir.is_dir():
                        continue
                    skill_file = skill_dir / 'SKILL.md'
                    if not skill_file.exists():
                        continue

                    try:
                        parsed = parse_skill_file(skill_file)
                    except (FileNotFoundError, OSError) as e:
                        logger.warning("[SKILLS] Failed to read skill file %s: %s",
                                       skill_file, e)
                        skipped_count += 1
                        continue

                    frontmatter = parsed.get('frontmatter', {})
                    name = frontmatter.get('name', skill_dir.name)

                    if name.lower() in self._disabled_names:
                        logger.debug("[SKILLS] Skill '%s' is disabled, skipping", name)
                        skipped_count += 1
                        continue

                    if not _skill_matches_platform(frontmatter):
                        logger.debug("[SKILLS] Skill '%s' not compatible with platform %s, skipping",
                                     name, _sys.platform)
                        skipped_count += 1
                        continue

                    collected.append((skill_file, parsed))
                    found_count += 1
            except OSError as e:
                logger.warning("[SKILLS] Error scanning %s: %s", root, e)

        # Phase 2: Clear stale + register + rebuild atomically under lock
        with self._write_lock:
            self._skills_registry.clear()
            self._matcher._inverted_index.clear()

            for skill_file, parsed in collected:
                self._register_single(skill_file, priority=_PRIORITY_SYSTEM, parsed=parsed)

            self._rebuild_index()

        logger.info(
            "[SKILLS] Discovery complete: %d found, %d skipped, %d in registry",
            found_count, skipped_count, len(self._skills_registry),
        )

        self._cache_signature = current_sig
        self._cache_timestamp = now

    def _register_single(
        self,
        skill_file: Path,
        priority: int = _PRIORITY_SYSTEM,
        parsed: Optional[dict] = None,
    ) -> None:
        """Parse and register a single SKILL.md file.

        Args:
            skill_file: Path to the SKILL.md file.
            priority: Priority level for duplicate resolution.
            parsed: Pre-parsed skill data (optional; skips re-parsing if provided).
        """
        if parsed is None:
            try:
                parsed = parse_skill_file(skill_file)
            except (FileNotFoundError, OSError) as e:
                logger.warning("[SKILLS] Failed to read skill file %s: %s",
                               skill_file, e)
                return

        frontmatter = parsed.get('frontmatter', {})
        name = frontmatter.get('name')
        if not name:
            # Fall back to directory name
            name = skill_file.parent.name
            logger.debug("[SKILLS] Skill file %s has no 'name' in frontmatter, using dir: %s",
                         skill_file, name)

        # Platform & disabled checks — skip when caller already filtered
        if parsed is not None:
            pass  # discover() already checked these before calling us
        else:
            if not _skill_matches_platform(frontmatter):
                logger.debug("[SKILLS] Skill '%s' not compatible with platform %s, skipping",
                             name, _sys.platform)
                return

            if name.lower() in self._disabled_names:
                logger.debug("[SKILLS] Skill '%s' is disabled, skipping", name)
                return

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
            'triggers': frontmatter.get('triggers', []),
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
        with self._write_lock:
            return self._skills_registry.get(skill_name)

    def get_skill_names(self) -> List[str]:
        """Return a list of all registered skill names.

        Returns:
            List of skill name strings.
        """
        return list(self._skills_registry.keys())

    def match_skills(self, query: str) -> List[Tuple[str, float]]:
        """Public interface for matching skills against a query.

        Rebuilds the matcher index if no skills are registered yet (lazy init).

        Args:
            query: The task text or context to match against.

        Returns:
            List of (skill_name, relevance_score) tuples sorted by score descending.
        """
        with self._write_lock:
            if not self._skills_registry and not self._matcher._inverted_index:
                self._rebuild_index()
            return self._matcher.match(query)

    def get_all_metadata(self) -> List[Dict[str, Any]]:
        """Return all Tier 1 metadata (for scan_skills tool).

        Returns a list of dicts with 'name' and 'description' keys, suitable
        for display or matching. Internal fields (_priority, _parsed_data) are excluded.
        """
        with self._write_lock:
            result = []
            for name, data in self._skills_registry.items():
                result.append({
                    'name': data.get('name', name),
                    'description': data.get('description', ''),
                    'triggers': data.get('triggers', []),
                    'source': data.get('source', 'system'),
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

    # ── Dynamic Registration ─────────────────────────────────────────────

    def register_skill_from_content(
        self,
        skill_content: str,
        source: str = "auto-generated",
        task_text: str = "",
        auto_promote: bool = True,
    ) -> Tuple[bool, List[str]]:
        """Register a skill from raw SKILL.md content.

        Writes to a temp pending location, parses and validates, then promotes
        to the final skills directory. Rebuilds the matcher index under lock.

        Args:
            skill_content: Full SKILL.md content string (frontmatter + body).
            source: Provenance label (default "auto-generated").
            task_text: Optional task text for self-match validation (Tier 2).
            auto_promote: If True, move validated skill to .qwen/skills/.

        Returns:
            Tuple of (success, error_messages).
        """
        logger.info("[SKILLS] Registering skill from content (source=%s)", source)

        skill_id = uuid.uuid4().hex
        pending_dir = Path(f".qwen/pending-skills/{skill_id}")
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_file = pending_dir / "SKILL.md"
        pending_file.write_text(skill_content, encoding='utf-8')

        try:
            # 2. Parse
            parsed = parse_skill_file(pending_file)
            frontmatter = parsed.get('frontmatter', {})
            name = frontmatter.get('name', '')
            if not name:
                name = pending_file.parent.name

            # Extract generated_from_task for self-match validation (if task_text not provided)
            validation_task = task_text or frontmatter.get('generated_from_task', '')

            # 3. Validate BEFORE modifying registry
            existing = set(self._skills_registry.keys())
            passed, errors = validate_skill(skill_content, name, existing, validation_task, check_injection=True)
            if not passed:
                logger.debug("[SKILLS] Validation failed for '%s': %s", name, errors)
                # Clean up pending file
                if pending_file.exists():
                    pending_file.unlink()
                if pending_dir.exists() and not any(pending_dir.iterdir()):
                    pending_dir.rmdir()
                return False, errors

            # 4. Register + promote under lock (atomic write path)
            with self._write_lock:
                # Duplicate resolution (check again under lock)
                existing_entry = self._skills_registry.get(name)
                if existing_entry is not None:
                    existing_priority = existing_entry.get('_priority', _PRIORITY_SYSTEM)
                    if _PRIORITY_SYSTEM <= existing_priority:
                        # Clean up pending file
                        if pending_file.exists():
                            pending_file.unlink()
                        if pending_dir.exists() and not any(pending_dir.iterdir()):
                            pending_dir.rmdir()
                        return False, [f"Skill '{name}' already exists in registry"]

                self._skills_registry[name] = {
                    'name': name,
                    'description': frontmatter.get('description', ''),
                    'source': source,
                    'triggers': frontmatter.get('triggers', []),
                    'file_path': str(pending_file),
                    '_priority': _PRIORITY_SYSTEM,
                    '_parsed_data': parsed,
                }

                # Promote if validated
                if auto_promote and AUTO_SKILL_AUTO_PROMOTE:
                    target_dir = Path(f".qwen/skills/{name}")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target_file = target_dir / "SKILL.md"
                    if target_file.exists():
                        target_file.unlink()
                    pending_file.rename(target_file)
                    self._skills_registry[name]['file_path'] = str(target_file)
                    logger.info("[SKILLS] Promoted skill '%s' to .qwen/skills/%s/", name, name)
                else:
                    logger.info("[SKILLS] Skill '%s' validated, staying in pending (auto_promote=%s)",
                                name, auto_promote)

                # Rebuild index
                self._rebuild_index()

            return True, []

        except Exception as e:
            logger.warning("[SKILLS] Failed to register skill from content: %s", e)
            # Clean up pending file on error
            if pending_file.exists():
                pending_file.unlink()
            if pending_dir.exists() and not any(pending_dir.iterdir()):
                pending_dir.rmdir()
            return False, [f"Registration failed: {e}"]

    # ── Auto-skill trigger hook ──────────────────────────────────────────────

    def trigger_auto_skill_reflection(
        self,
        inst,
        total_tool_calls: int,
        task_text: str,
        instance_name: str,
        append_fn,
        run_turn_fn,
        state_idle_fn,
        snapshot_length: int = -1,
        rollback_fn=None,
        check_skill_created_fn=None,
    ) -> List[str]:
        """Check conditions and fire auto-skill reflection turns with rollback.

        Shared trigger hook used by both execution_engine and run_agent_unified.

        Flow:
          1. Snapshot conversation length (passed as snapshot_length)
          2. Run AUTO_SKILL_EXTRA_TURNS turns freely
          3. Compute pop_count from delta (robust against compression)
          4. Rollback using pop_count via existing _rollback_instance logic
          5. Reset agent state to IDLE if in SLEEPING/COMPLETING
          6. Return the names of any skills created on disk

        Args:
            inst: The agent instance.
            total_tool_calls: Cumulative tool call count.
            task_text: Task description text for skill matching.
            instance_name: Human-readable instance label for logging.
            append_fn: Callable(msg) -> None to append a user message.
            run_turn_fn: Callable() -> Optional[result] to execute one extra turn.
            state_idle_fn: Callable() -> bool to check if the instance is IDLE.
            snapshot_length: Conversation length before extra turns.
            rollback_fn: Callable(pop_count) -> None to remove N messages
                         from end using existing _rollback_instance logic.
            check_skill_created_fn: Callable() -> List[str] returning names of
                                    newly registered skills since snapshot.

        Returns:
            List of skill names created during the reflection turns.
            Empty list if no reflection was triggered or no skills were created.
        """
        if getattr(inst, '_auto_skill_proposed', False):
            return []

        proposed_count = getattr(inst, '_auto_skill_proposed_count', 0)
        if proposed_count >= AUTO_SKILL_MAX_PER_SESSION:
            return []

        matches = self.match_skills(task_text) if task_text else []
        logger.debug("[AUTO-SKILL] Check: tool_count=%d, matches=%d",
                     total_tool_calls, len(matches))

        if total_tool_calls < AUTO_SKILL_MIN_TOOL_CALLS:
            return []
        if matches and matches[0][1] > SKILL_MATCH_THRESHOLD:
            return []
        if not state_idle_fn():
            return []

        creator = self.load_full_instructions("skill-creator")
        if not creator:
            return []

        logger.info("[AUTO-SKILL] Trigger fired for %s (%d tools)", instance_name, total_tool_calls)
        prompt = (f"## Skill Reflection\n\n{creator}\n\n"
                  f"You completed a task using {total_tool_calls} tool calls. "
                  f"If the approach could help future similar tasks, "
                  f"propose a reusable skill by calling propose_skill.")

        try:
            append_fn(prompt)
            for turn_i in range(AUTO_SKILL_EXTRA_TURNS):
                run_turn_fn()
                logger.debug("[AUTO-SKILL] Extra turn %d/%d for %s",
                             turn_i + 1, AUTO_SKILL_EXTRA_TURNS, instance_name)
        except Exception as e:
            logger.warning("[AUTO-SKILL] Extra turn error for %s: %s", instance_name, e)

        # Compute pop_count from delta (robust against compression during extra turns)
        rollback_ok = True
        if snapshot_length >= 0:
            try:
                current_len = len(inst.conversation)
                pop_count = max(0, current_len - snapshot_length)
                if pop_count > 0:
                    rollback_fn(pop_count)
                    logger.debug("[AUTO-SKILL] Rolled back %d messages for %s",
                                 pop_count, instance_name)
                elif current_len < snapshot_length:
                    logger.debug("[AUTO-SKILL] Compression removed %d messages during extra turns for %s",
                                 snapshot_length - current_len, instance_name)
                # Reset agent state to IDLE if in SLEEPING or COMPLETING
                current_state = getattr(inst, 'state', None)
                if current_state is not None:
                    state_name = getattr(current_state, 'name', None)
                    if state_name in ('SLEEPING', 'COMPLETING'):
                        from agent_cascade.agent_instance import AgentState
                        inst._transition(AgentState.IDLE)
                        logger.debug("[AUTO-SKILL] State reset to IDLE for %s", instance_name)
            except Exception as e:
                logger.warning("[AUTO-SKILL] Rollback error for %s: %s", instance_name, e)
                rollback_ok = False

        # Discover which skills were created (only if rollback succeeded)
        created_skills = []
        if rollback_ok and check_skill_created_fn is not None:
            try:
                created_skills = check_skill_created_fn()
            except Exception as e:
                logger.debug("[AUTO-SKILL] Skill check error for %s: %s", instance_name, e)

        if created_skills:
            inst._auto_skill_proposed = True
            inst._auto_skill_proposed_count = proposed_count + 1
            logger.info("[AUTO-SKILL] Created skills: %s", created_skills)

        return created_skills