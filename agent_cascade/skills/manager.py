"""
Skill Manager — Central coordinator for skill discovery, loading and resolution.

Handles:
  - Scanning directories for SKILL.md files (discover)
  - Storing Tier 1 metadata in a registry
  - Loading full instructions on-demand (Tier 2)
  - Resolving load_skill arguments (list / AUTO / NONE)
"""

import copy as _copy
import json as _json
import os as _os
import sys as _sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # No-op on Windows; we accept the limitation

from agent_cascade.log import logger
from agent_cascade.prompts.dna import AUTO_SKILL_REFLECTION_PROMPT
from agent_cascade.settings import (AUTO_SKILL_AUTO_PROMOTE, AUTO_SKILL_MIN_TURNS, CANDIDATE_EVAL_INTERVAL_SECONDS,
                                    CANDIDATE_MIN_RATINGS, LOAD_SKILL_AUTO, LOAD_SKILL_NONE, MAX_AUTO_SKILLS_PER_CALL,
                                    SKILL_CACHE_TTL_SECONDS, SKILL_MATCH_THRESHOLD, SKILL_RATING_INITIAL, SKILLS_DISABLED)

from .cache_helper import compute_scan_signature
from .matcher import SkillMatcher
from .parser import parse_skill_file
from .validator import validate_skill

# Priority levels for duplicate skill name resolution:
# Higher number = higher priority (wins over lower)
_PRIORITY_SYSTEM = 1  # System/global skills (agents/global/skills/)
_PRIORITY_AGENT = 2  # Agent-specific skills (agents/*/skills/)
_PRIORITY_USER = 3  # User-defined skills (workspace/skills/)
_PRIORITY_CANDIDATE = 4  # Upgrade candidates (agents/global/candidates/) — strictly highest;
# a candidate ALWAYS wins the serving race while it exists, until the rating-based
# decision gate promotes or discards it.

# Candidate storage: stable name-keyed dirs under agents/global/candidates/<name>/SKILL.md.
_CANDIDATES_DIR = Path('agents/global/candidates')

# Retries for deleting a candidate dir (Windows file-lock scenarios).
_CANDIDATE_DIR_RETRIES = 3


def _atomic_write_text(dst: Path, content: str) -> None:
    """Atomically write ``content`` to ``dst`` via a sibling .tmp + os.replace.

    NOT a plain rename: os.replace is Windows-safe when the target already exists.
    """
    tmp_out = dst.with_suffix('.tmp')
    tmp_out.write_text(content, encoding='utf-8')
    _os.replace(str(tmp_out), str(dst))


def _priority_for_root(root: Path) -> int:
    """Map a scan root directory to its skill-tier priority.

    Derives the tier from path components (most specific check first):
      - .../agents/global/candidates → _PRIORITY_CANDIDATE (upgrade candidates, highest)
      - .../agents/global/skills     → _PRIORITY_SYSTEM (shared global store)
      - .../agents/<name>/skills     → _PRIORITY_AGENT
      - .../workspace/skills         → _PRIORITY_USER
      - anything else                → _PRIORITY_SYSTEM (default)

    Matching on path parts (rather than an explicit tier list) keeps
    ``discover(skill_paths)``'s signature unchanged and degrades gracefully to
    the system tier for unknown roots. The shared global store lives under
    agents/ but must keep SYSTEM (lowest) priority so per-agent and user skills
    still override it on name collision.
    """
    parts = [p.lower() for p in root.parts]
    if 'agents' in parts and parts[-1] == 'candidates':
        # agents/global/candidates holds live-serving upgrade candidates; the
        # candidate tier must beat every production tier while a candidate exists.
        return _PRIORITY_CANDIDATE
    if 'agents' in parts and parts[-1] == 'skills':
        # agents/global/skills is the shared global store, not an agent-specific one.
        if 'global' in parts:
            return _PRIORITY_SYSTEM
        return _PRIORITY_AGENT
    if 'workspace' in parts and parts[-1] == 'skills':
        return _PRIORITY_USER
    return _PRIORITY_SYSTEM


# Platform filtering: maps frontmatter platform names to sys.platform values
_PLATFORM_MAP = {
    'macos': 'darwin',
    'linux': 'linux',
    'windows': 'win32',
}


def _skill_matches_platform(frontmatter: dict) -> bool:
    """Check if a skill is compatible with the current OS.

    If 'platforms' field is absent or empty, skill is compatible with all.
    """
    platforms = frontmatter.get('platforms')
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


def rating_sort_key(name: str, avg: Optional[float]) -> tuple:
    """Canonical sort key for skill lists ordered by average rating.

    Rated skills first (highest average first), unrated last; name ascending tiebreak.
    """
    if avg is None:
        return (1, 0.0, name.lower())
    return (0, -avg, name.lower())


def _build_auto_skill_reflection_prompt(loaded_skill_names: Optional[List[str]],
                                        skill_creator_body: str,
                                        skill_manager=None) -> str:
    """Render the auto-skill reflection prompt (dna.py template) for injection.

    ``loaded_skill_names`` is rendered as one line per entry, or "(none)" when empty/None.
    Each line shows ONLY the skill name ("- name") — ratings are deliberately omitted so
    the model rates each skill on its actual performance this run rather than anchoring on
    a pre-existing average (see user directive to avoid rating bias in the reflection prompt).
    The full skill-creator body is embedded verbatim (same as the previous inline prompt).
    """
    if loaded_skill_names:
        loaded_list = '\n'.join(f'- {name}' for name in loaded_skill_names)
    else:
        loaded_list = '(none)'
    # Two-pass replacement instead of str.format(): the embedded skill-creator body
    # is arbitrary SKILL.md text and may contain literal braces (code blocks, JSON
    # examples), which would raise KeyError/ValueError inside format().
    return AUTO_SKILL_REFLECTION_PROMPT.replace('{loaded_skills}',
                                                loaded_list).replace('{skill_creator_body}', skill_creator_body)


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

        # Metrics infrastructure (batched activation tracking)
        self._metrics_file = Path('agents/global/skills-metrics.json')
        self._metrics: Dict[str, Dict[str, Any]] = {}  # skill_name -> {total_loads, by_version}
        self._metrics_lock = threading.Lock()
        self._pending_flush_count = 0  # tracks buffered increments
        self._last_flush_time = time.monotonic()  # for timer-based flush
        self._FLUSH_THRESHOLD = 5  # flush after N pending increments
        self._FLUSH_INTERVAL = 30.0  # flush every N seconds (whichever comes first)
        self._load_metrics()  # load on startup

    # ── Metrics Persistence (Batched Writes) ────────────────────────────────

    def _load_metrics(self) -> None:
        """Load activation metrics from disk (best-effort)."""
        if not self._metrics_file.exists():
            return
        try:
            data = _json.loads(self._metrics_file.read_text(encoding='utf-8'))
            # Tolerate older schema versions (1.0/1.1): entries without
            # ratings_by_version fall back to the aggregate ratings at read time;
            # the next flush bumps the stored file to 1.2.
            self._metrics = data.get('skills', {})
            logger.debug('[SKILLS] Loaded metrics for %d skills (schema %s)', len(self._metrics),
                         data.get('schema_version', 'unknown'))
        except Exception as e:
            logger.warning('[SKILLS] Failed to load metrics file: %s — starting fresh', e)
            self._metrics = {}

    def _flush_metrics_to_disk(self) -> None:
        """Atomically write buffered metrics to disk with file-level locking.

        Uses fcntl.flock on POSIX for multi-process safety. On Windows, flock is a
        no-op — we accept the limitation and rely on os.replace() atomicity.
        Metrics are best-effort in multi-process scenarios.
        """
        try:
            # Ensure parent directory exists before writing
            self._metrics_file.parent.mkdir(parents=True, exist_ok=True)

            tmp_path = self._metrics_file.with_suffix('.tmp')
            # Snapshot under lock (deep copy: nested per-skill dicts are shared with live state).
            # The tree is small JSON; copying is far cheaper than risking a torn write.
            with self._metrics_lock:
                # Schema 1.2 adds per-version rating history (ratings_by_version) so the
                # candidate decision gate can compare versions; older files are bumped here.
                data = {'schema_version': '1.2', 'skills': _copy.deepcopy(self._metrics)}

            # Open temp file for writing with exclusive lock (POSIX only)
            fd = _os.open(str(tmp_path), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o644)
            try:
                if _fcntl is not None:
                    _fcntl.flock(fd, _fcntl.LOCK_EX)  # no-op on Windows
                _os.write(fd, _json.dumps(data, indent=2).encode('utf-8'))
                _os.fsync(fd)
            finally:
                if _fcntl is not None:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                _os.close(fd)

            # Atomic rename (cross-platform via os.replace)
            _os.replace(str(tmp_path), str(self._metrics_file))
        except Exception as e:
            logger.warning('[SKILLS] Failed to flush metrics to disk: %s', e)
            # Clean up orphaned temp file if it exists
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def prune_stale_metrics(self) -> None:
        """Remove metrics entries for skills that no longer exist anywhere.

        Builds the live set of skill names (registry keys + a recursive walk of every
        configured root, capturing both each SKILL.md's frontmatter name and its
        containing directory name), then deletes any ``_metrics`` key whose lowercase
        form is not in that live set. Disabled, platform-incompatible, INACTIVE-subfolder
        and candidate skills are all KEPT because their on-disk artifacts still exist.

        Safe to call when nothing is stale: it is a no-op and does NOT flush. A scan
        error while walking an EXISTING root aborts the prune entirely (nothing deleted)
        so a partial scan can never trigger deletion.
        """
        # Step 1: build the live name set. The registry snapshot is taken under the write
        # lock; the I/O-bound disk walk runs OUTSIDE it (before steps 2-3 acquire it again).
        live_set_lower = set()
        with self._write_lock:
            # Registry snapshot (names as registered, incl. candidates/disabled survivors).
            for name in self._skills_registry.keys():
                live_set_lower.add(str(name).lower())

        for root in self._skill_paths:
            if not root.exists():
                logger.debug('[SKILLS] Metrics prune: skill root missing, skipping: %s', root)
                continue
            try:
                # Recursive walk (rglob follows symlinks via is_dir()/exists(), matching
                # discover() behavior). A real I/O error aborts the whole prune.
                for skill_file in root.rglob('SKILL.md'):
                    if not skill_file.is_file():
                        continue
                    try:
                        parsed = parse_skill_file(skill_file)
                    except Exception as e:  # noqa: BLE001 — one bad file must not abort the scan
                        logger.warning('[SKILLS] Metrics prune: failed to parse %s: %s', skill_file, e)
                        continue
                    frontmatter = parsed.get('frontmatter', {})
                    dir_name = skill_file.parent.name
                    fm_name = frontmatter.get('name') or dir_name
                    live_set_lower.add(str(fm_name).lower())
                    live_set_lower.add(dir_name.lower())
            except OSError as e:
                logger.warning('[SKILLS] Metrics prune aborted — scan error on %s: %s', root, e)
                return

        # Steps 2-3: determine stale keys + delete. The compute AND the deletion must both
        # run under _metrics_lock (nested inside _write_lock, preserving the established
        # _write_lock -> _metrics_lock order): other methods mutate _metrics under
        # _metrics_lock only, so iterating self._metrics.keys() without it risks a
        # "dictionary changed size during iteration" RuntimeError in production.
        stale_keys = []
        with self._write_lock:
            with self._metrics_lock:
                stale_keys = [k for k in self._metrics.keys() if str(k).lower() not in live_set_lower]
                for key in stale_keys:
                    del self._metrics[key]
                    logger.info("[SKILLS] Pruned stale metrics for skill '%s'", key)

        # Step 4: flush only if something was removed.
        if stale_keys:
            self._flush_metrics_to_disk()

    def _increment_load_count(self, skill_name: str, version: str) -> None:
        """Increment load counter for a skill+version combo (buffered).

        Flushes to disk when pending count reaches threshold OR 30 seconds have elapsed.
        """
        with self._metrics_lock:
            entry = self._metrics.setdefault(skill_name, {'total_loads': 0, 'by_version': {}})
            entry['total_loads'] += 1
            entry['by_version'][version] = entry['by_version'].get(version, 0) + 1

            self._pending_flush_count += 1
            now = time.monotonic()
            should_flush = (self._pending_flush_count >= self._FLUSH_THRESHOLD or
                            (now - self._last_flush_time) >= self._FLUSH_INTERVAL)

            if should_flush:
                self._pending_flush_count = 0
                self._last_flush_time = now
                # Flush outside lock to avoid holding it during I/O
                flush_needed = True
            else:
                flush_needed = False

        if flush_needed:
            self._flush_metrics_to_disk()

    def _record_rating(self, skill_name: str, rating: float) -> None:
        """Record a quality rating for a skill (buffered, same flush path as load counts).

        Per-skill entry gains a compact ``ratings`` sub-dict supporting an average:
        ``{"count": int, "sum": float, "latest": float, "last_version": str}`` — kept
        unchanged for backward compatibility. Schema 1.2 additionally records the rating
        under ``ratings_by_version[<current version>]`` so the candidate decision gate can
        compare per-version histories (the version is resolved from the registry entry, so
        a serving candidate accrues ratings to its own version key). Reuses the metrics
        lock + pending-flush counter.
        """
        with self._metrics_lock:
            entry = self._metrics.setdefault(skill_name, {'total_loads': 0, 'by_version': {}})
            ratings = entry.get('ratings') or {'count': 0, 'sum': 0.0, 'latest': None, 'last_version': ''}
            ratings['count'] += 1
            ratings['sum'] = round(ratings['sum'] + rating, 4)
            ratings['latest'] = rating
            version = self._skills_registry.get(skill_name, {}).get('version', '')
            if version:
                ratings['last_version'] = version
                # Per-version history (schema 1.2): the serving version is what gets rated.
                by_version_ratings = entry.setdefault('ratings_by_version', {})
                vr = by_version_ratings.get(version) or {'count': 0, 'sum': 0.0, 'latest': None}
                vr['count'] += 1
                vr['sum'] = round(vr['sum'] + rating, 4)
                vr['latest'] = rating
                by_version_ratings[version] = vr
            entry['ratings'] = ratings

            self._pending_flush_count += 1
            now = time.monotonic()
            should_flush = (self._pending_flush_count >= self._FLUSH_THRESHOLD or
                            (now - self._last_flush_time) >= self._FLUSH_INTERVAL)

            if should_flush:
                self._pending_flush_count = 0
                self._last_flush_time = now
                flush_needed = True
            else:
                flush_needed = False

        if flush_needed:
            self._flush_metrics_to_disk()

    def record_rating(self, skill_name: str, rating: float) -> None:
        """Public wrapper: validate a 0-10 rating and persist it via ``_record_rating``.

        Raises ValueError for out-of-range ratings so callers can surface the error.
        """
        if not (0.0 <= float(rating) <= 10.0):
            raise ValueError(f"rating must be in [0, 10], got {rating}")
        self._record_rating(skill_name, float(rating))
        # Ratings are rare, deliberate events — batch them and a short session
        # (<=4 ratings, exit before the 30s interval) silently loses data.
        # Flush immediately; load counters keep their batched behavior.
        self._flush_metrics_to_disk()

    # ── Discovery ────────────────────────────────────────────────────────────

    def invalidate_cache(self) -> None:
        """Invalidate the discovery cache so the next scan re-reads from disk.

        Resets both the TTL timestamp and the scan signature under the write lock,
        forcing ``discover()`` to bypass its early-return checks on the next call.
        Call this whenever the registry is cleared or mutated outside of
        ``discover()`` (e.g. when a config handler empties it) so that a later
        rediscovery is not short-circuited by a stale cache hit.
        """
        with self._write_lock:
            self._cache_signature = None
            self._cache_timestamp = 0.0

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
        # Cache check: TTL first (cheap), then signature (expensive)
        now = time.monotonic()
        if (now - self._cache_timestamp) < self._cache_ttl:
            logger.debug('[SKILLS] Cache hit — skipping discovery (age=%.1fs)', now - self._cache_timestamp)
            return
        current_sig = compute_scan_signature(skill_paths, frozenset(self._disabled_names))
        if current_sig == self._cache_signature:
            logger.debug('[SKILLS] Cache hit — signature unchanged (age=%.1fs)', now - self._cache_timestamp)
            return

        logger.info('[SKILLS] Starting skill discovery across %d paths', len(skill_paths))

        # Store paths for _ensure_discovered() hot-reload support
        self._skill_paths = list(skill_paths)

        # Phase 1: Scan and parse outside lock (I/O bound)
        collected: list = []
        found_count = 0
        skipped_count = 0

        for root in skill_paths:
            if not root.exists():
                logger.debug('[SKILLS] Skill directory does not exist, skipping: %s', root)
                continue

            # Derive the tier priority for every skill found under this root.
            root_priority = _priority_for_root(root)

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
                        logger.warning('[SKILLS] Failed to read skill file %s: %s', skill_file, e)
                        skipped_count += 1
                        continue

                    frontmatter = parsed.get('frontmatter', {})
                    name = frontmatter.get('name', skill_dir.name)

                    if name.lower() in self._disabled_names:
                        logger.debug("[SKILLS] Skill '%s' is disabled, skipping", name)
                        skipped_count += 1
                        continue

                    if not _skill_matches_platform(frontmatter):
                        logger.debug("[SKILLS] Skill '%s' not compatible with platform %s, skipping", name,
                                     _sys.platform)
                        skipped_count += 1
                        continue

                    collected.append((skill_file, parsed, root_priority))
                    found_count += 1
            except OSError as e:
                logger.warning('[SKILLS] Error scanning %s: %s', root, e)

        # Phase 2: Clear stale + register + rebuild atomically under lock
        with self._write_lock:
            self._skills_registry.clear()
            self._matcher._inverted_index.clear()

            for skill_file, parsed, priority in collected:
                self._register_single(skill_file, priority=priority, parsed=parsed)

            self._rebuild_index()

        logger.info(
            '[SKILLS] Discovery complete: %d found, %d skipped, %d in registry',
            found_count,
            skipped_count,
            len(self._skills_registry),
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
                logger.warning('[SKILLS] Failed to read skill file %s: %s', skill_file, e)
                return

        frontmatter = parsed.get('frontmatter', {})
        name = frontmatter.get('name')
        if not name:
            # Fall back to directory name
            name = skill_file.parent.name
            logger.debug("[SKILLS] Skill file %s has no 'name' in frontmatter, using dir: %s", skill_file, name)

        # Platform & disabled checks — skip when caller already filtered
        if parsed is not None:
            pass  # discover() already checked these before calling us
        else:
            if not _skill_matches_platform(frontmatter):
                logger.debug("[SKILLS] Skill '%s' not compatible with platform %s, skipping", name, _sys.platform)
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
                    name,
                    priority,
                    existing_priority,
                )
                return
            logger.debug("[SKILLS] Replacing skill '%s' with higher priority (%d > %d)", name, priority,
                         existing_priority)

        # Store parsed data in registry (Tier 1: frontmatter only; body is lazy-loaded)
        version = parsed.get('version', '1.0.0')  # Already normalized by parser
        self._skills_registry[name] = {
            'name': name,
            'description': frontmatter.get('description', ''),
            'source': frontmatter.get('source', ''),
            'triggers': frontmatter.get('triggers', []),
            'version': version,
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
            logger.debug('[SKILLS] Failed to rebuild matcher index: %s', e)

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

    def get_skill_chars(self, skill_name: str) -> int:
        """Return the body character count for a skill (0 if unavailable).

        Reads from the in-memory ``_parsed_data`` cache populated during discovery.
        No disk I/O — returns 0 if the skill is unknown or its body hasn't been parsed yet.
        """
        with self._write_lock:
            entry = self._skills_registry.get(skill_name)
            if not entry:
                return 0
            parsed = entry.get('_parsed_data')
            return len(parsed.get('body', '')) if parsed else 0

    def get_skill_names(self) -> List[str]:
        """Return a list of all registered skill names.

        Reads under the write lock (same as ``get_skill_metadata``) so a concurrent
        re-scan in ``discover()`` cannot observe the registry mid clear/rebuild and
        return an empty/partial name list.

        Returns:
            List of skill name strings.
        """
        with self._write_lock:
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

        Returns a list of dicts with 'name', 'description', and 'chars' keys, suitable
        for display or matching. Internal fields (_priority, _parsed_data) are excluded.
        """
        with self._write_lock:
            result = []
            for name, data in self._skills_registry.items():
                parsed = data.get('_parsed_data')
                body_len = len(parsed.get('body', '')) if parsed else 0
                result.append({
                    'name': data.get('name', name),
                    'description': data.get('description', ''),
                    'triggers': data.get('triggers', []),
                    'source': data.get('source', 'system'),
                    'version': data.get('version', '1.0.0'),
                    'chars': body_len,
                })
            return result

    def get_metrics(self, skill_name: Optional[str] = None) -> Dict[str, Any]:
        """Return activation metrics.

        Args:
            skill_name: If provided, return only that skill's metrics.
                        If None, return all metrics.
        Returns:
            Metrics dict matching the JSON structure.
        """
        with self._metrics_lock:
            if skill_name:
                return self._metrics.get(skill_name, {'total_loads': 0, 'by_version': {}})
            return dict(self._metrics)

    def get_rating_info(self, skill_name: str) -> tuple:
        """Return (average or None, count) for a skill's ratings under one lock acquisition."""
        with self._metrics_lock:
            ratings = (self._metrics.get(skill_name) or {}).get('ratings') or {}
            count = ratings.get('count', 0)
            if not count:
                return None, 0
            return round(ratings['sum'] / count, 2), count

    def get_rating_average(self, skill_name: str) -> Optional[float]:
        """Return the average quality rating for a skill (0-10), or None if unrated.

        Average = sum / count from the per-skill ``ratings`` sub-dict. Returns None
        when the skill has no ratings entry or its count is zero ("unrated").
        """
        return self.get_rating_info(skill_name)[0]

    # ── Tier 2 Loading (Full Instructions) ───────────────────────────────────

    def load_full_instructions(self, skill_name: str, count_load: bool = True) -> Optional[str]:
        """Load full SKILL.md body (Tier 2) for a skill.

        Supports case-insensitive matching to tolerate LLM input variations.

        Args:
            skill_name: The registered skill name.
            count_load: When True (default), increment the global per-skill load
                metric on a successful load. Pass False for pure loadability checks
                (e.g. ``_resolve_skill_names``) that must not inflate the metric —
                the real body-load in ``resolve_load_skill`` is what counts.

        Returns:
            Full markdown instructions string, or None if skill not found / not loadable.
        """
        with self._write_lock:
            # Exact match first
            reg = self._skills_registry.get(skill_name)
            if reg is not None:
                pass
            else:
                # Case-insensitive fallback (handles LLM capitalizing names like "Self-Augmentation")
                lower = skill_name.lower()
                for key, entry in self._skills_registry.items():
                    if key.lower() == lower:
                        reg = entry
                        logger.debug("[SKILLS] load_full_instructions: case-insensitive match '%s' -> '%s'", skill_name,
                                     key)
                        break
            if reg is None:
                logger.debug("[SKILLS] load_full_instructions: skill '%s' not in registry (registry has %d skills)",
                             skill_name, len(self._skills_registry))
                return None

            version = reg.get('version', '1.0.0')

            # Try lazy load from parsed data first
            parsed = reg.get('_parsed_data')
            if parsed and 'body' in parsed:
                body = parsed['body']
                logger.debug("[SKILLS] Loaded Tier 2 instructions for '%s' (%d chars)", skill_name, len(body))
                if count_load:
                    self._increment_load_count(skill_name, version)
                return body or None

            # Fallback: re-read from disk
            file_path = reg.get('file_path')
            if file_path:
                try:
                    parsed = parse_skill_file(Path(file_path))
                    reg['_parsed_data'] = parsed
                    body = parsed.get('body', '')
                    logger.debug("[SKILLS] Re-parsed '%s' from disk (%d chars)", skill_name, len(body))
                    if count_load:
                        self._increment_load_count(skill_name, version)
                    return body or None
                except (FileNotFoundError, OSError) as e:
                    # Non-loadable skills are expected during AUTO matching — keep this
                    # at debug level to match the silent-skip behavior of _resolve_skill_names.
                    logger.debug("[SKILLS] Failed to re-parse '%s': %s", skill_name, e)

            return None

    # ── Resolution (load_skill argument handling) ────────────────────────────

    def _resolve_skill_names(
        self,
        load_skill_value: Union[List[str], str, None],
        task_text: str = '',
        context_text: str = '',
    ) -> List[str]:
        """Compute the names of skills that WILL actually load for a given
        ``load_skill`` value, filtered by loadability.

        This is the single source of truth shared by both ``resolve_load_skill``
        (which loads bodies) and ``resolve_load_skill_names`` (which returns
        names), so the two can never drift apart. Loadability checks use
        ``load_full_instructions(..., count_load=False)`` so they do NOT increment
        the global per-skill load metrics (the real body-load in
        ``resolve_load_skill`` does).

        Returns:
            List of skill names that will be loaded (empty if none).
        """
        # Coerce JSON-encoded string arrays into real lists. LLMs sometimes emit
        # load_skill as a JSON-encoded string (e.g. "[\"AUTO\"]") instead of a
        # native list; without this it falls through to the "Unknown string"
        # branch and skills are silently dropped. Bare "AUTO"/"NONE" strings do
        # not start with '[' so they are left untouched.
        if isinstance(load_skill_value, str) and load_skill_value.strip().startswith('['):
            try:
                _decoded = _json.loads(load_skill_value)
            except (ValueError, TypeError):
                _decoded = None  # Not valid JSON — keep original value unchanged
            if isinstance(_decoded, list):
                load_skill_value = _decoded

        # Ensure skills have been discovered before resolving
        self._ensure_discovered()

        # Handle NONE / empty (case-insensitive, whitespace-tolerant)
        if load_skill_value is None or (isinstance(load_skill_value, str) and
                                        load_skill_value.strip().upper() == LOAD_SKILL_NONE):
            return []

        # Handle explicit list of skill names — keep only those that are loadable.
        if isinstance(load_skill_value, list):
            names = []
            for name in load_skill_value:
                body = self.load_full_instructions(name, count_load=False)
                if body:
                    names.append(name)
                else:
                    logger.debug("[SKILLS] Skill '%s' not found — silently skipping", name)
            return names

        # Handle AUTO mode (case-insensitive, whitespace-tolerant)
        if isinstance(load_skill_value, str):
            if load_skill_value.strip().upper() == LOAD_SKILL_AUTO:
                query = f"{task_text} {context_text}".strip()
                # Use public API (match_skills) which handles lazy index rebuild
                matches = self.match_skills(query)
                if not matches:
                    logger.debug('[SKILLS] AUTO mode — no matching skills for query')
                    return []

                # matches is already sorted by score descending — cap at the top N.
                names = []
                for name, score in matches:
                    if len(names) >= MAX_AUTO_SKILLS_PER_CALL:
                        break
                    if score < SKILL_MATCH_THRESHOLD:
                        continue
                    body = self.load_full_instructions(name, count_load=False)
                    if body:
                        logger.debug("[SKILLS] AUTO loaded skill '%s' (score=%.2f)", name, score)
                        names.append(name)

                return names

            # Unknown string value — treat as NONE
            logger.debug('[SKILLS] Unknown load_skill value: %s', load_skill_value)
            return []

        return []

    def resolve_load_skill_names(
        self,
        load_skill_value: Union[List[str], str, None],
        task_text: str = '',
        context_text: str = '',
    ) -> List[str]:
        """Return the names of skills that ``resolve_load_skill`` would load for
        the given arguments (loadability-filtered). Additive helper used by the
        telemetry capture points; shares name-computation with
        ``resolve_load_skill`` so it always matches what is actually injected.
        """
        return self._resolve_skill_names(load_skill_value, task_text, context_text)

    def _load_skill_bodies(
        self,
        load_skill_value: Union[List[str], str, None],
        task_text: str = '',
        context_text: str = '',
    ) -> List[Tuple[str, str]]:
        """Load full instruction bodies for the skills named by ``_resolve_skill_names``.

        Shared body-loading loop behind both ``resolve_load_skill`` and
        ``resolve_load_skill_pairs`` so the two can never drift apart. Each
        successful body-load increments the global per-skill load metric (via
        ``load_full_instructions`` with its default ``count_load=True``).

        Returns:
            List of ``(skill_name, full_instruction_body)`` tuples, one per
            successfully loaded skill (unresolvable names are dropped).
        """
        pairs: List[Tuple[str, str]] = []
        for name in self._resolve_skill_names(load_skill_value, task_text, context_text):
            body = self.load_full_instructions(name)
            if body:
                pairs.append((name, body))
        return pairs

    def resolve_load_skill(
        self,
        load_skill_value: Union[List[str], str, None],
        task_text: str = '',
        context_text: str = '',
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
        # Name-computation and body-loading are shared with resolve_load_skill_pairs
        # via _load_skill_bodies so the two can never drift apart.
        return [body for _name, body in self._load_skill_bodies(load_skill_value, task_text, context_text)]

    def resolve_load_skill_pairs(
        self,
        load_skill_value: Union[List[str], str, None],
        task_text: str = '',
        context_text: str = '',
    ) -> List[Tuple[str, str]]:
        """Like :meth:`resolve_load_skill` but returns ``(name, body)`` pairs.

        The name is preserved alongside each skill's instruction body so callers
        (e.g. system-prompt injection) can label skills by their real name rather
        than a positional index. Name-computation shares the same single source of
        truth as ``resolve_load_skill`` / ``resolve_load_skill_names``, so the two
        never drift apart.

        Returns:
            List of ``(skill_name, full_instruction_body)`` tuples (one per loaded skill).
        """
        return self._load_skill_bodies(load_skill_value, task_text, context_text)

    # ── Dynamic Registration ─────────────────────────────────────────────

    def register_skill_from_content(
        self,
        skill_content: str,
        source: str = 'auto-generated',
        task_text: str = '',
        auto_promote: bool = True,
    ) -> Tuple[bool, List[str]]:
        """Register a skill from raw SKILL.md content.

        Writes to a temp pending location, parses and validates, then promotes
        to the final skills directory. Rebuilds the matcher index under lock.

        Args:
            skill_content: Full SKILL.md content string (frontmatter + body).
            source: Provenance label (default "auto-generated").
            task_text: Optional task text for self-match validation (Tier 2).
            auto_promote: If True, move validated skill to agents/global/skills/.

        Returns:
            Tuple of (success, error_messages).
        """
        logger.info('[SKILLS] Registering skill from content (source=%s)', source)

        skill_id = uuid.uuid4().hex
        # Overridable pending root (mirrors _candidates_dir / _production_skills_dir): tests point
        # this at a per-test tmp dir so xdist workers never share/blanket-delete the same tree.
        pending_root = getattr(self, '_pending_dir', None) or Path('agents/global/pending-skills')
        pending_dir = pending_root / skill_id
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_file = pending_dir / 'SKILL.md'
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

            # 3. Validate BEFORE modifying registry. An existing name in ANY tier is an
            # UPGRADE proposal (candidate flow) — the validator's uniqueness check must not
            # reject it, so only truly-new names are passed as "existing".
            with self._write_lock:
                existing_entry = self._skills_registry.get(name)
            upgrade = existing_entry is not None
            existing = set(self._skills_registry.keys())
            if upgrade:
                existing.discard(name)
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
                    # Name exists in ANY tier (production or candidate) → UPGRADE proposal.
                    # Write/replace the candidate file and register it at _PRIORITY_CANDIDATE
                    # (highest), so it immediately takes over serving from whatever version
                    # was winning before. If a candidate already exists its file is replaced
                    # (newest proposal wins); old per-version ratings stay in metrics history.
                    if self._register_candidate_upgrade(name, pending_file, parsed, frontmatter, source):
                        return True, []
                    # Candidate write failed (I/O error) — do NOT fall through to the
                    # new-skill path: that would overwrite the existing production file
                    # and bypass the candidate flow. Clean up and report the failure.
                    if pending_file.exists():
                        try:
                            pending_file.unlink()
                        except OSError:
                            pass
                    return False, ['Candidate upgrade registration failed (see log)']

                self._skills_registry[name] = {
                    'name': name,
                    'description': frontmatter.get('description', ''),
                    'source': source,
                    'triggers': frontmatter.get('triggers', []),
                    'version': parsed.get('version', '1.0.0'),
                    'file_path': str(pending_file),
                    '_priority': _PRIORITY_SYSTEM,
                    '_parsed_data': parsed,
                }

                # Promote if validated (new skills only — upgrades go through the candidate flow).
                # Use _candidate_dirs()[1] as the production root so both registration paths
                # share one source of truth; in production this is agents/global/skills/.
                if auto_promote and AUTO_SKILL_AUTO_PROMOTE:
                    _, production_root = self._candidate_dirs()
                    target_dir = production_root / name
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target_file = target_dir / 'SKILL.md'
                    # Atomic tmp+replace (NOT rename): the pending staging dir is CWD-relative
                    # while production_root may be overridden to another drive in tests — a
                    # cross-drive rename raises WinError 17. Copying via a same-dir temp file
                    # is Windows-safe and matches the candidate-flow write path.
                    tmp_out = target_file.with_suffix('.tmp')
                    tmp_out.write_text(pending_file.read_text(encoding='utf-8'), encoding='utf-8')
                    _os.replace(str(tmp_out), str(target_file))
                    self._skills_registry[name]['file_path'] = str(target_file)
                    # The copy (not a move) leaves the pending staging file behind — clean it up.
                    try:
                        if pending_file.exists():
                            pending_file.unlink()
                        if pending_dir.exists() and not any(pending_dir.iterdir()):
                            pending_dir.rmdir()
                    except OSError:
                        pass  # Best-effort cleanup
                    logger.info("[SKILLS] Promoted skill '%s' to %s/", name, production_root / name)
                else:
                    logger.info("[SKILLS] Skill '%s' validated, staying in pending (auto_promote=%s)", name,
                                auto_promote)

                # Rebuild index
                self._rebuild_index()

            # Record the initial rating (single source of truth in the manager). New skills start
            # at SKILL_RATING_INITIAL so they have a baseline before any agent rates them.
            try:
                self._record_rating(name, SKILL_RATING_INITIAL)
            except Exception as e:
                logger.warning('[SKILLS] Failed to record initial rating for %s: %s', name, e)

            return True, []

        except Exception as e:
            logger.warning('[SKILLS] Failed to register skill from content: %s', e)
            # Clean up pending file on error
            if pending_file.exists():
                pending_file.unlink()
            if pending_dir.exists() and not any(pending_dir.iterdir()):
                pending_dir.rmdir()
            return False, [f"Registration failed: {e}"]


    # ── Candidate flow (live-serving upgrade candidates) ────────────────────

    def _candidate_dirs(self):
        """Return (candidates_root, production_skills_root) for the candidate flow.

        Both default to the CWD-relative agents/global/ layout; tests may override
        via the ``_candidates_dir`` / ``_production_skills_dir`` attributes.
        """
        candidates = getattr(self, '_candidates_dir', None) or _CANDIDATES_DIR
        production = getattr(self, '_production_skills_dir', None) or Path('agents/global/skills')
        return Path(candidates), Path(production)

    def get_candidate_names(self) -> List[str]:
        """Return the names of skills whose registry winner is a candidate file.

        Used by scan_skills to mark pending-decision lines in its no-query listing.
        """
        with self._write_lock:
            return [n for n, e in self._skills_registry.items() if e.get('_priority') == _PRIORITY_CANDIDATE]

    def _register_candidate_upgrade(self, name: str, pending_file: Path, parsed: dict, frontmatter: dict,
                                    source: str) -> bool:
        """Register an upgrade proposal as a live-serving candidate (caller holds _write_lock).

        Writes/replace agents/global/candidates/<name>/SKILL.md atomically, registers the
        candidate at _PRIORITY_CANDIDATE (so it immediately takes over serving), backfills
        the incumbent's legacy metrics with ratings_by_version[<incumbent version>], and
        triggers evaluate_candidates() immediately.

        Called while the caller holds _write_lock; evaluate_candidates() re-acquires it
        via RLock reentrancy, so no deadlock risk.

        Returns True on success (caller returns early); False means the candidate file
        write failed — the caller must NOT fall through to the new-skill path.
        """
        candidates_root, production_root = self._candidate_dirs()
        candidate_dir = candidates_root / name
        candidate_file = candidate_dir / 'SKILL.md'
        try:
            candidate_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(candidate_file, pending_file.read_text(encoding='utf-8'))
        except OSError as e:
            logger.warning('[SKILLS] Failed to write candidate file for %s: %s', name, e)
            return False

        # Resolve the incumbent version from its PRODUCTION file (the registry entry is
        # about to be replaced by the candidate, so the registry alone can't provide it).
        # Fallback: the pre-existing registry entry's version (covers test setups where the
        # production root isn't the one holding the file on disk).
        prod_file = production_root / name / 'SKILL.md'
        prod_version = self._skills_registry.get(name, {}).get('version') or '1.0.0'
        if prod_file.exists():
            try:
                prod_version = parse_skill_file(prod_file).get('version', prod_version)
            except (FileNotFoundError, OSError):
                pass

        # Legacy incumbent backfill (D4): a schema-1.1 metrics entry lacks
        # ratings_by_version — seed it with the aggregate so the comparison baseline and
        # discard-revert are exact even for pre-1.2 incumbents. Always ensure the key
        # exists (even empty) so per-version lookups never fall back to the aggregate.
        with self._metrics_lock:
            entry = self._metrics.get(name)
            if entry is not None and 'ratings_by_version' not in entry:
                by_version_ratings = {}
                agg = entry.get('ratings')
                if agg and agg.get('count'):
                    by_version_ratings[prod_version] = {
                        'count': agg['count'],
                        'sum': round(agg['sum'], 4),
                        'latest': agg.get('latest')
                    }
                entry['ratings_by_version'] = by_version_ratings

        new_version = parsed.get('version', '1.0.0')
        if new_version == prod_version:
            logger.warning(
                '[SKILLS] Candidate %s reuses incumbent version %s — per-version ratings will collide (gate compares avg_cand vs avg_prod from the same ratings_by_version key)',
                name, new_version)
        old_winner = self._skills_registry.get(name, {}).get('version', prod_version)
        self._skills_registry[name] = {
            'name': name,
            'description': frontmatter.get('description', ''),
            'source': source,
            'triggers': frontmatter.get('triggers', []),
            'version': new_version,
            'file_path': str(candidate_file),
            '_priority': _PRIORITY_CANDIDATE,
            '_parsed_data': parsed,
        }
        self._rebuild_index()

        # Clean up the (now moved) pending staging dir.
        try:
            if pending_file.exists():
                pending_file.unlink()
            if pending_file.parent.exists() and not any(pending_file.parent.iterdir()):
                pending_file.parent.rmdir()
        except OSError:
            pass  # Best-effort cleanup

        logger.info("[SKILLS] Registered '%s' as candidate v%s, now serving in place of v%s "
                    '(decision gate active)', name, new_version, old_winner)

        # Immediate decision trigger (D5b): a pending candidate + new proposal must be
        # decided without waiting for the timer. The caller's _write_lock is still held;
        # evaluate_candidates() re-enters it via RLock and is idempotent.
        try:
            self.evaluate_candidates()
        except Exception as e:  # noqa: BLE001 — a gate hiccup must not fail registration
            logger.warning('[SKILLS] Immediate candidate evaluation failed for %s: %s', name, e)

        return True

    def _version_rating_avg(self, skill_name: str, version: str):
        """Return (avg or None, count) for one version's rating history.

        Reads ratings_by_version[version]; legacy entries without it fall back to the
        aggregate ratings block (D3). Returns (None, 0) when unrated.
        """
        with self._metrics_lock:
            entry = self._metrics.get(skill_name) or {}
            history = (entry.get('ratings_by_version') or {}).get(version)
            if history is None:
                history = entry.get('ratings') or {}  # legacy fallback
            count = history.get('count', 0)
            if not count:
                return None, 0
            return round(history['sum'] / count, 2), count

    def _remove_candidate_dir(self, name: str) -> None:
        """Delete candidates/<name>/ (best-effort; Windows-safe retries)."""
        candidates_root, _ = self._candidate_dirs()
        candidate_dir = candidates_root / name
        for attempt in range(_CANDIDATE_DIR_RETRIES):
            try:
                if candidate_dir.exists():
                    for child in list(candidate_dir.iterdir()):
                        try:
                            child.unlink()
                        except OSError:
                            pass
                    if not any(candidate_dir.iterdir()):
                        candidate_dir.rmdir()
                return
            except OSError:
                if attempt < _CANDIDATE_DIR_RETRIES - 1:
                    time.sleep(0.1 * (attempt + 1))
        logger.warning('[SKILLS] Could not delete candidate dir for %s', name)

    def _promote_candidate(self, name: str, cand_file: Path, parsed: dict, prod_version: str) -> None:
        """Promote a candidate over the incumbent (caller holds _write_lock).

        Copies the candidate file over the production file (atomic tmp+replace), deletes
        the candidate dir, updates the registry entry in place back to SYSTEM priority,
        and transfers metrics: total_loads kept, aggregate ratings swapped from the winner,
        per-version history intact for both versions.
        """
        candidates_root, production_root = self._candidate_dirs()
        prod_dir = production_root / name
        prod_file = prod_dir / 'SKILL.md'

        frontmatter = parsed.get('frontmatter', {})
        new_version = parsed.get('version', '1.0.0')
        try:
            prod_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(prod_file, cand_file.read_text(encoding='utf-8'))
        except OSError as e:
            logger.warning('[SKILLS] Candidate promotion file copy failed for %s: %s', name, e)
            return

        self._remove_candidate_dir(name)

        # Update the registry entry in place, restoring SYSTEM priority and the production path.
        reg = self._skills_registry.get(name)
        if reg is not None:
            reg.update({
                'description': frontmatter.get('description', reg.get('description', '')),
                'version': new_version,
                'triggers': frontmatter.get('triggers', reg.get('triggers', [])),
                'file_path': str(prod_file),
                '_priority': _PRIORITY_SYSTEM,
                '_parsed_data': parsed,
            })
        self._rebuild_index()

        # Metrics transfer on gate: aggregate ratings mirror the promoted version's history;
        # total_loads and per-version history are untouched.
        with self._metrics_lock:
            entry = self._metrics.get(name)
            if entry is not None:
                winner = (entry.get('ratings_by_version') or {}).get(new_version)
                if winner:
                    entry['ratings'] = {
                        'count': winner['count'],
                        'sum': round(winner['sum'], 4),
                        'latest': winner.get('latest'),
                        'last_version': new_version
                    }

        avg_cand, cand_n = self._version_rating_avg(name, new_version)
        avg_prod, prod_n = self._version_rating_avg(name, prod_version)
        logger.info("[SKILLS] Candidate '%s' v%s promoted over v%s (avg %s×%d vs %s×%d)", name, new_version,
                    prod_version, avg_cand, cand_n, avg_prod, prod_n)

    def _discard_candidate(self, name: str, prod_version: str) -> None:
        """Discard a candidate (caller holds _write_lock).

        Deletes candidates/<name>/ and removes its registry entry; the next discovery
        restores the incumbent to service. Metrics: aggregate ratings revert to the
        incumbent's history (legacy fallback: existing aggregate); total_loads untouched;
        the candidate's per-version entry is kept as evidence.
        """
        self._remove_candidate_dir(name)

        with self._write_lock:
            reg = self._skills_registry.get(name)
            if reg is not None and reg.get('_priority') == _PRIORITY_CANDIDATE:
                del self._skills_registry[name]
                self._rebuild_index()

        with self._metrics_lock:
            entry = self._metrics.get(name)
            if entry is not None:
                by_version = entry.get('ratings_by_version') or {}
                incumbent_hist = by_version.get(prod_version)
                if incumbent_hist:
                    entry['ratings'] = {
                        'count': incumbent_hist['count'],
                        'sum': round(incumbent_hist['sum'], 4),
                        'latest': incumbent_hist.get('latest'),
                        'last_version': prod_version
                    }
                # else: legacy fallback — keep the existing aggregate as-is

        logger.info("[SKILLS] Candidate '%s' discarded (incumbent v%s restored on next discovery)", name, prod_version)

    def evaluate_candidates(self) -> None:
        """Run the rating-based decision gate for all live-serving candidates.

        Triggers: pool startup after discovery, every new candidate registration, and a
        30s safety-net timer (catches orphaned candidates after manual file deletions and
        out-of-band rating changes). Forces a discovery refresh first so the registry is
        current regardless of cache TTL state. All registry reads/mutations run under
        _write_lock; every gate action re-verifies disk state so concurrent/duplicate
        triggers cannot double-promote or double-discard (idempotent).

        Decision per candidate: orphan check (incumbent file missing → discard; a disabled
        incumbent still keeps its candidate pending), then once the candidate has >=
        CANDIDATE_MIN_RATINGS ratings, compare averages (2dp): candidate >= incumbent →
        promote; else discard.
        """
        # Force a discovery refresh first (D5) — registry must be current regardless of TTL.
        try:
            self.invalidate_cache()
            self._ensure_discovered()
        except Exception as e:  # noqa: BLE001 — proceed with the current registry on failure
            logger.warning('[SKILLS] Candidate eval discovery refresh failed: %s', e)

        candidates_root, production_root = self._candidate_dirs()

        with self._write_lock:
            candidate_names = [n for n, e in self._skills_registry.items() if e.get('_priority') == _PRIORITY_CANDIDATE]

        for name in candidate_names:
            try:
                prod_file = production_root / name / 'SKILL.md'
                cand_dir = candidates_root / name
                cand_file = cand_dir / 'SKILL.md'

                with self._write_lock:
                    reg = self._skills_registry.get(name)
                    # Idempotency: re-verify the candidate still exists and is registered.
                    if reg is None or reg.get('_priority') != _PRIORITY_CANDIDATE:
                        continue
                    if not cand_file.exists():
                        # Candidate file vanished out-of-band — drop the stale registry entry.
                        del self._skills_registry[name]
                        self._rebuild_index()
                        logger.info("[SKILLS] Candidate '%s' file missing; removed stale registry entry", name)
                        continue
                    candidate_version = reg.get('version', '1.0.0')

                # 0. Orphan check — disk-based: a candidate only exists to upgrade something.
                # A DISABLED incumbent is still an incumbent (it just isn't served), so only
                # a missing production file orphans the candidate.
                # NOTE: this must run BEFORE the rating gate — an orphaned candidate has no
                # incumbent to compare against and would otherwise stay pending forever,
                # shadowing nothing but never being cleaned up.
                if not prod_file.exists():
                    with self._write_lock:
                        if self._skills_registry.get(name, {}).get('_priority') != _PRIORITY_CANDIDATE:
                            continue  # already decided by a concurrent trigger
                        self._discard_candidate(name, '1.0.0')
                    logger.info("[SKILLS] Candidate '%s' orphaned (incumbent file missing) — discarded", name)
                    continue

                # 1. Resolve versions: candidate from registry, incumbent parsed from its file.
                try:
                    prod_version = parse_skill_file(prod_file).get('version', '1.0.0')
                except (FileNotFoundError, OSError):
                    prod_version = '1.0.0'
                avg_cand, cand_n = self._version_rating_avg(name, candidate_version)
                avg_prod, prod_n = self._version_rating_avg(name, prod_version)
                if avg_prod is None:
                    avg_prod = 0.0  # unrated incumbent → any rated candidate promotes

                # 2. Gate on minimum ratings.
                if cand_n < CANDIDATE_MIN_RATINGS:
                    # logger.debug("[SKILLS] Candidate '%s' pending decision (%d/%d ratings)", name, cand_n,
                    #             CANDIDATE_MIN_RATINGS)
                    continue

                # 3-5. Compare averages (2dp): better or equal → promote; worse → discard.
                with self._write_lock:
                    reg = self._skills_registry.get(name)
                    if reg is None or reg.get('_priority') != _PRIORITY_CANDIDATE:
                        continue  # concurrent trigger already decided this candidate
                    if not cand_file.exists():
                        del self._skills_registry[name]
                        self._rebuild_index()
                        continue
                    parsed = parse_skill_file(cand_file)
                    if avg_cand is None or avg_cand >= avg_prod:
                        self._promote_candidate(name, cand_file, parsed, prod_version)
                    else:
                        self._discard_candidate(name, prod_version)
                        logger.info("[SKILLS] Candidate '%s' v%s worse than incumbent v%s "
                                    '(avg %s×%d vs %s×%d)', name, candidate_version, prod_version, avg_cand, cand_n,
                                    avg_prod, prod_n)
            except Exception as e:  # noqa: BLE001 — one bad candidate must not stop the rest
                logger.warning('[SKILLS] Candidate evaluation failed for %s: %s', name, e)

        # Best-effort cleanup of metrics entries for skills that no longer exist anywhere.
        # Runs AFTER the gate so a pending candidate (still on disk) is never pruned.
        try:
            self.prune_stale_metrics()
        except Exception as e:  # noqa: BLE001 — cleanup must never break the gate
            logger.warning('[SKILLS] Metrics prune failed (non-critical): %s', e)

    # ── Auto-skill trigger hook ──────────────────────────────────────────────

    def auto_skill_qualifies(
        self,
        inst,
        turns_effectuated: int,
        loaded_skill_names: Optional[List[str]] = None,
        min_turns: Optional[int] = None,
    ) -> Optional[str]:
        """Pure qualification check + prompt builder for the in-loop auto-skill trigger.

        Returns the built reflection prompt string when all gates pass, else ``None``.
        This method has NO side effects: it does not append anything to the conversation
        and does not set any flag — the engine loop (core.py) owns prompt injection
        (via ``_append_and_log`` + ``llm_messages.append``) and sets the one-shot
        ``inst._auto_skill_proposed`` flag after a successful trigger.

        Gates (all must pass):
          - ``not inst._auto_skill_proposed`` (one-shot; never reset).
          - ``turns_effectuated > min_turns`` (strictly greater — the turns-based gate that
            replaced the legacy tool-call-count and match-score gates). ``min_turns`` is the
            live UI-editable threshold when supplied by core.py, else the import-time
            ``AUTO_SKILL_MIN_TURNS`` constant.
          - skill-creator is loadable from the registry.

        Args:
            inst: The agent instance (flag read under its compression lock).
            turns_effectuated: Number of turns effectuated on this run (inst._current_turn).
            loaded_skill_names: Names of skills loaded for this run (for the prompt's list).
            min_turns: Live turn threshold from pool.settings (UI-editable, no restart).
                ``None`` → fall back to the import-time ``AUTO_SKILL_MIN_TURNS`` constant.

        Returns:
            The reflection prompt string when qualified, else None.
        """
        # Live-read threshold: core.py passes the pool.settings value so a UI change takes
        # effect without restart. Falls back to the import-time constant when not supplied
        # (direct callers / tests that patch the module constant).
        if min_turns is None:
            min_turns = AUTO_SKILL_MIN_TURNS

        with inst._compression_lock:
            if getattr(inst, '_auto_skill_proposed', False):
                return None

        # Gate: strictly greater than the configured turn threshold. Independent of loaded skills
        # and match score (the old tool-call + top-match conditions are intentionally removed).
        if turns_effectuated <= min_turns:
            return None

        creator = self.load_full_instructions('skill-creator', count_load=False)
        if not creator:
            return None

        logger.debug('[AUTO-SKILL] Qualification passed: turns=%d (min=%d)', turns_effectuated, min_turns)
        return _build_auto_skill_reflection_prompt(loaded_skill_names, creator, self)
