# Skill Versioning & Activation Tracking — Implementation Plan

**Status:** Reviewed (ready for implementation)  
**Author:** skill_planner_1  
**Date:** 2026-08-03  
**Last Updated:** 2026-08-03 (post-review: batched metrics flushes, file locking, duplicate-name warnings)  

## Overview

Add semantic versioning and activation counting to the skills system with minimal changes to existing code. Existing skills without a `version` field continue to work unchanged.

---

## 1. Schema Changes

### 1.1 Frontmatter (SKILL.md)

Add one new **optional** frontmatter field:

```yaml
---
name: my-skill
description: What this skill does
triggers:
  - "keyword1"
version: "1.0.0"      # NEW — optional, semver X.Y.Z, defaults to "1.0.0" if omitted
---
```

**Rules:**
- Type: string
- Format: `X.Y.Z` (major.minor.patch), e.g., `"1.0.0"`, `"2.1.3"`
- Validation regex: `^\d+\.\d+\.\d+$`
- If missing or invalid → silently treated as `"1.0.0"` (backwards compatible)
- Not required for validation to pass

### 1.2 Metrics Storage Format

**File location:** `.qwen/skills-metrics.json` (workspace root, same level as `.qwen/skills/`)

**Structure:**

```json
{
  "schema_version": "1.0",
  "skills": {
    "self-augmentation": {
      "total_loads": 47,
      "by_version": {
        "1.0.0": 47
      }
    },
    "docker-best-practices": {
      "total_loads": 12,
      "by_version": {
        "1.0.0": 8,
        "1.1.0": 4
      }
    }
  }
}
```

**Design notes:**
- Flat JSON file — simple to read/write atomically
- `total_loads` is the sum across all versions (denormalized for easy queries)
- `by_version` tracks per-version counts so we can see if newer versions get adopted
- File created on first write; missing file = zero metrics

---

## 2. Code Changes Per File

### 2.1 `agent_cascade/skills/parser.py`

**Change:** Extract and normalize the `version` field during parsing.

Add a helper function:

```python
import re

_SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')

def normalize_version(raw) -> str:
    """Return valid semver string or default '1.0.0'."""
    if isinstance(raw, str) and _SEMVER_RE.match(raw):
        return raw
    return "1.0.0"
```

Modify `parse_skill_file()` to include version in result:

```python
def parse_skill_file(skill_path: Path) -> Dict[str, Any]:
    ...
    frontmatter, body = parse_frontmatter(content)

    result = {
        "frontmatter": frontmatter,
        "body": body,
        "path": str(skill_path),
        "version": normalize_version(frontmatter.get("version")),  # NEW
    }
    return result
```

**Rationale:** Version normalization happens at parse time so the rest of the system always sees a valid semver string. No downstream callers need to handle missing/invalid versions.

---

### 2.2 `agent_cascade/skills/manager.py`

This is where most changes happen, but they're small and localized.

#### A. Add metrics storage as a class attribute

In `__init__`:

```python
import os as _os
import fcntl as _fcntl

def __init__(self):
    ...
    self._metrics_file = Path(".qwen/skills-metrics.json")
    self._metrics: Dict[str, Dict[str, Any]] = {}  # skill_name -> {total_loads, by_version}
    self._metrics_lock = threading.Lock()
    self._pending_flush_count = 0       # NEW — tracks buffered increments
    self._last_flush_time = time.monotonic()  # NEW — for timer-based flush
    self._FLUSH_THRESHOLD = 5           # flush after N pending increments
    self._FLUSH_INTERVAL = 30.0         # flush every N seconds (whichever comes first)
    self._load_metrics()  # NEW — load on startup
```

#### B. Add metrics persistence methods (batched writes + file locking)

Add after `__init__`:

```python
def _load_metrics(self) -> None:
    """Load activation metrics from disk (best-effort)."""
    if not self._metrics_file.exists():
        return
    try:
        data = json.loads(self._metrics_file.read_text(encoding='utf-8'))
        self._metrics = data.get("skills", {})
        logger.debug("[SKILLS] Loaded metrics for %d skills", len(self._metrics))
    except Exception as e:
        logger.warning("[SKILLS] Failed to load metrics file: %s — starting fresh", e)
        self._metrics = {}

def _flush_metrics_to_disk(self) -> None:
    """Atomically write buffered metrics to disk with file-level locking.

    Uses fcntl.flock on POSIX for multi-process safety. On Windows, flock is a
    no-op — we accept the limitation and rely on os.replace() atomicity.
    Metrics are best-effort in multi-process scenarios.
    """
    try:
        tmp_path = self._metrics_file.with_suffix('.tmp')
        data = {"schema_version": "1.0", "skills": self._metrics}

        # Open temp file for writing with exclusive lock (POSIX only)
        fd = _os.open(str(tmp_path), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o644)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX)  # no-op on Windows
            _os.write(fd, json.dumps(data, indent=2).encode('utf-8'))
            _os.fsync(fd)
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
            _os.close(fd)

        # Atomic rename (cross-platform via os.replace)
        _os.replace(str(tmp_path), str(self._metrics_file))
    except Exception as e:
        logger.warning("[SKILLS] Failed to flush metrics to disk: %s", e)
        # Clean up orphaned temp file if it exists
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

def _increment_load_count(self, skill_name: str, version: str) -> None:
    """Increment load counter for a skill+version combo (buffered).

    Flushes to disk when pending count reaches threshold OR 30 seconds have elapsed.
    """
    with self._metrics_lock:
        entry = self._metrics.setdefault(skill_name, {"total_loads": 0, "by_version": {}})
        entry["total_loads"] += 1
        entry["by_version"][version] = entry["by_version"].get(version, 0) + 1

        self._pending_flush_count += 1
        now = time.monotonic()
        should_flush = (
            self._pending_flush_count >= self._FLUSH_THRESHOLD or
            (now - self._last_flush_time) >= self._FLUSH_INTERVAL
        )

        if should_flush:
            self._pending_flush_count = 0
            self._last_flush_time = now
            # Flush outside lock to avoid holding it during I/O
            flush_needed = True
        else:
            flush_needed = False

    if flush_needed:
        self._flush_metrics_to_disk()
```

#### C. Store version in registry entries

In `_register_single()`, add `version` to the stored dict:

```python
# After parsing frontmatter...
version = parsed.get("version", "1.0.0")  # Already normalized by parser

self._skills_registry[name] = {
    'name': name,
    'description': frontmatter.get('description', ''),
    'source': frontmatter.get('source', ''),
    'triggers': frontmatter.get('triggers', []),
    'version': version,                          # NEW
    'file_path': str(skill_file),
    '_priority': priority,
    '_parsed_data': parsed,
}
```

Same change in `register_skill_from_content()` where it creates registry entries:

```python
self._skills_registry[name] = {
    ...
    'version': parsed.get("version", "1.0.0"),  # NEW
    ...
}
```

#### D. Increment counter when loading full instructions

In `load_full_instructions()`, after confirming the skill exists and before returning body:

```python
def load_full_instructions(self, skill_name: str) -> Optional[str]:
    ...
    if reg is None:
        return None

    version = reg.get('version', '1.0.0')       # NEW

    # Lazy-load body logic unchanged...
    if parsed and 'body' in parsed:
        body = parsed['body']
    else:
        ...

    self._increment_load_count(skill_name, version)  # NEW — count every load
    return body
```

**Key point:** We increment on `load_full_instructions`, not on metadata queries. This counts actual activations (when the skill is really used), not just scans.

#### E. Expose metrics via public method

Add near `get_all_metadata()`:

```python
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
            return self._metrics.get(skill_name, {"total_loads": 0, "by_version": {}})
        return dict(self._metrics)

def get_skill_metadata(self, skill_name: str) -> Optional[Dict[str, Any]]:
    """Return Tier 1 metadata for a skill by name."""
    ...
```

#### F. Include version in `get_all_metadata()` output

Modify to include version:

```python
def get_all_metadata(self) -> List[Dict[str, Any]]:
    with self._write_lock:
        result = []
        for name, data in self._skills_registry.items():
            result.append({
                'name': data.get('name', name),
                'description': data.get('description', ''),
                'triggers': data.get('triggers', []),
                'source': data.get('source', 'system'),
                'version': data.get('version', '1.0.0'),  # NEW
            })
        return result
```

---

### 2.3 `agent_cascade/tools/custom/scan_skills.py`

**Change:** Show version and load counts in scan output.

Modify the listing sections to include version:

For "list all" (no query):

```python
# Current:
lines.append(f"- **{skill['name']}** [{source}]: {skill.get('description', 'No description')}")

# New:
version = skill.get('version', '1.0.0')
lines.append(f"- **{skill['name']}** [{source}] v{version}: {skill.get('description', 'No description')}")
```

For matched results (with query):

```python
meta = skill_manager.get_skill_metadata(name)
...
version = meta.get('version', '1.0.0') if meta else '1.0.0'
metrics = skill_manager.get_metrics(name)
loads = metrics.get('total_loads', 0)
lines.append(f"- **{name}** [{source}] v{version} (score: {score:.2f}, loads: {loads}): {desc}")
```

**Rationale:** Version is always visible. Load count only shown for matched results to keep the "list all" output clean but still informative during active skill selection.

---

### 2.4 `agent_cascade/tools/custom/load_skill.py`

**Change:** None required — counting happens inside `SkillManager.load_full_instructions()` which this tool already calls.

The tool's response could optionally include version info, but not necessary for MVP. If desired:

In the success path after loading body:

```python
meta = skill_manager.get_skill_metadata(name)
version = meta.get('version', '1.0.0') if meta else '1.0.0'
loaded.append(f"{name} (v{version})")
```

---

### 2.5 `agent_cascade/tools/custom/propose_skill.py`

**Change:** REQUIRED — warn when proposing a skill with the same name as an existing skill, and suggest incrementing the version.

In the `call()` method, before calling `register_skill_from_content()`, check for existing name:

```python
# Parse frontmatter to get proposed name
fm, _ = parse_frontmatter(skill_content)
proposed_name = fm.get('name', '') if fm else ''

if proposed_name and proposed_name in skill_manager.get_skill_names():
    existing_meta = skill_manager.get_skill_metadata(proposed_name)
    existing_version = existing_meta.get('version', '1.0.0') if existing_meta else '1.0.0'

    # Suggest next patch version
    parts = existing_version.split('.')
    suggested_version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"

    return (
        f"Skill '{proposed_name}' already exists (current version: v{existing_version}).\n\n"
        f"To update it, either:\n"
        f"1. Edit the existing SKILL.md file directly and increment its version field.\n"
        f"2. Use a different name (e.g., '{proposed_name}-v2' or '{proposed_name}-updated').\n\n"
        f"Suggested version for update: v{suggested_version}"
    )
```

**Rationale:** Agents currently get a generic "already exists" error from the validator. This gives actionable guidance instead, pointing them toward the correct workflow (edit existing file + bump version).

---

### 2.6 `agent_cascade/skills/validator.py`

**Change:** REQUIRED — add soft validation for version format (warning that still allows registration).

Add the semver regex at module level:

```python
_SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')
```

Add after triggers check in `validate_skill()`:

```python
# Version format check (soft — warns but allows registration, defaults to 1.0.0 if invalid)
version = frontmatter.get('version')
if version and not _SEMVER_RE.match(str(version)):
    errors.append(f"Version '{version}' is not valid semver (X.Y.Z) — will default to '1.0.0'")
```

**Behavior:** This is a warning-style error that still allows registration (`validate_skill` returns `True` but includes the warning in the error list). The agent sees the warning and can fix it, but registration isn't blocked. If version is missing entirely, no warning is emitted (silently defaults to "1.0.0").

**Future consideration (not in MVP):** Allow pre-release tags like `"1.0.0-beta"` or `"1.0.0+build123"`. Regex would become `^\d+\.\d+\.\d+(-[a-zA-Z0-9._]+)?(\+[a-zA-Z0-9._]+)?$`. Keeping it simple for now — basic semver covers all current use cases.

---

## 3. Metrics Persistence Strategy

### Location
`.qwen/skills-metrics.json` at workspace root (same parent as `.qwen/skills/`).

### Rationale
- Persists across sessions (file-based)
- Stays with the workspace (not tied to a specific agent instance)
- Same security model as skills themselves (workspace-local)
- Easy to inspect/edit manually if needed

### Write Policy (Batched Flushes)
- Load on `SkillManager.__init__` (at system startup)
- **Buffer increments in memory**, flush when:
  - Pending count reaches **5** increments, OR
  - **30 seconds** have elapsed since last flush (whichever comes first)
- Atomic write via `.tmp` + `os.replace()` (cross-platform atomic rename)
- Best-effort: log warning on failure but don't crash; up to 4 increments may be lost on hard crash

### Thread Safety
- `_metrics_lock` protects in-memory state and flush decision logic
- Flush I/O happens **outside** the lock to avoid blocking concurrent increments
- Timer check is cheap (monotonic time comparison)

### Multi-Process Concurrency
- **POSIX:** Uses `fcntl.flock()` for exclusive file-level locking during writes. Prevents corruption when multiple processes write concurrently.
- **Windows:** `fcntl.flock()` is a no-op on Windows. We rely on `os.replace()` atomicity — last writer wins. Potential for lost increments in true multi-process scenarios.
- **Decision:** Metrics are best-effort in multi-process deployments. This is acceptable because:
  - Primary use case is single-process (one AgentCascade instance per workspace)
  - Lost increments don't affect functionality, only observability
  - Adding proper Windows file locking adds significant complexity for a monitoring feature

**Documentation note:** Add a comment in the code that metrics may be inaccurate if multiple processes share the same metrics file.

---

## 4. Version Increment Workflow

### Current Behavior (unchanged)
- New skills proposed via `propose_skill` → validated → registered with version from frontmatter (or "1.0.0")
- Skills are identified by `name` only; duplicate names are rejected

### Version Increment Scenarios

#### Scenario A: Agent proposes an improved version of an existing skill

**Current flow:** Registration fails because name already exists.

**Desired behavior (MVP):** Same as current — registration fails. Agent must either:
1. Use a different name (e.g., `docker-best-practices-v2`)
2. Manually edit the existing SKILL.md file to update content and version

**Future enhancement (not in MVP):** Detect that a skill with the same name exists, read its current version, and auto-increment patch version if the agent explicitly requests an update. This would require:
- A new `update_skill` tool or a flag on `propose_skill` like `{"skill_content": "...", "overwrite_existing": true}`
- Version comparison logic to decide increment type (patch vs minor)

#### Scenario B: Human manually updates a skill file

1. Edit SKILL.md, change version from `"1.0.0"` → `"1.1.0"`
2. Next discovery run picks up new version
3. New loads count under `by_version["1.1.0"]`
4. Old counts preserved under `by_version["1.0.0"]`

This is the primary versioning workflow — human-driven updates with automatic tracking.

---

## 5. Metrics Exposure

### Via `scan_skills` Tool (primary)

Already planned in section 2.3:
- Version shown for all skills
- Load counts shown for matched results

Example output:

```
## Skills Matching Query: 'docker container'

- **docker-best-practices** [system] v1.1.0 (score: 0.85, loads: 23): Guidelines for writing Dockerfiles...
- **container-security** [user] v1.0.0 (score: 0.62, loads: 5): Security checklist for containerized apps...
```

### Via `SkillManager.get_metrics()` (programmatic)

Available to any code that has access to the skill_manager:

```python
# All metrics
metrics = skill_manager.get_metrics()

# Single skill
docker_metrics = skill_manager.get_metrics("docker-best-practices")
# {"total_loads": 23, "by_version": {"1.0.0": 18, "1.1.0": 5}}
```

### Optional: Dedicated Tool (future)

If needed later, a `get_skill_metrics` tool can be added that wraps `get_metrics()`. Not required now — `scan_skills` covers the main use case and programmatic access exists via the manager API.

---

## 6. Implementation Order

1. **parser.py** — Add `normalize_version()` and include version in parse result
2. **manager.py** — Add metrics infrastructure (`_load_metrics`, `_flush_metrics_to_disk`, `_increment_load_count`) with batched flushes
3. **manager.py** — Store version in registry entries, increment counter on load
4. **validator.py** — Add soft version format validation (REQUIRED)
5. **propose_skill.py** — Add duplicate-name warning with version increment suggestion (REQUIRED)
6. **scan_skills.py** — Display version and load counts in output

Total estimated changes: ~120 lines of new code across 5 files. No breaking changes.

---

## 7. Backwards Compatibility Checklist

- [x] Existing skills without `version` field → default to "1.0.0"
- [x] Metrics file missing on first run → created automatically
- [x] `load_skill` tool behavior unchanged (counting is transparent)
- [x] `scan_skills` output enhanced but still valid markdown list
- [x] `propose_skill` works without version field in frontmatter
- [x] Registry structure extended but existing keys preserved
- [x] No changes to public API signatures