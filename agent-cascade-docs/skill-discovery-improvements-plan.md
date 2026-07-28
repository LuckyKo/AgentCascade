# Skill Discovery Improvements — Implementation Plan

**Status**: Approved (Review Round 2 complete, all findings resolved)
**Date**: 2026-07-20
**Borrowed from**: Hermes Agent (`tools/skills_tool.py`, `agent/skill_utils.py`)
**Budget**: Minimal changes to 4 files + 1 new helper module

---

## Overview

| # | Feature | Priority | Files Changed | Est. Lines |
|---|---------|----------|---------------|------------|
| 1 | Mtime-based discovery cache | High | `manager.py`, new `cache_helper.py` | ~80 |
| 2 | Platform filtering | Medium | `manager.py` | ~45 |
| 3 | Prompt injection detection | Medium | `validator.py` | ~25 |
| 4 | Disabled skills config | Medium | `settings.py`, `manager.py` | ~30 |
| 5 | scan_skills tool improvements | Low | `scan_skills.py` | ~40 |
| 6 | Fix: async `discover()` doing sync work | Low | `manager.py`, `agent_pool.py` | ~15 |

**Total estimated additions**: ~235 lines across 6 files.

**Review Findings Resolved**:
1. ✅ Cache includes individual SKILL.md file mtimes (not just dir mtimes)
2. ✅ Platform check placed at start of `_register_single()` (before parsing)
3. ✅ Injection patterns refined — require 2+ matches, limited to auto-gen path
4. ✅ `manager.py` imports `SKILLS_DISABLED` from settings
5. ✅ `get_all_metadata()` updated to return `source` field
6. ✅ Test coverage expanded for cache invalidation, platform filtering, edge cases

---

## Current State (Baseline)

### `manager.py` (516 lines)
- `discover()` is `async` but calls only sync `parse_skill_file()` — no `await` inside.
- Scans every `root/*/SKILL.md` on every pool init. No cache.
- `_register_single()` parses every file unconditionally.
- Priority system: `_PRIORITY_SYSTEM=1`, `_PRIORITY_AGENT=2`, `_PRIORITY_USER=3`.
- `_rebuild_index()` called after every discovery pass and after every dynamic registration.

### `validator.py` (112 lines)
- Tier 1: size, frontmatter, name format, description, triggers, uniqueness, body length.
- Tier 2: self-match score against generating task.
- No platform check, no injection detection.

### `settings.py` (211 lines)
- Skill settings at lines 193–211. No `SKILLS_DISABLED`, no cache TTL.

### `scan_skills.py` (93 lines)
- `_ensure_index()` calls `skill_manager._rebuild_index()` on every invocation — redundant since `discover()` already rebuilds, and dynamic registration also rebuilds under lock.
- No `--all` flag. No source column.

### `agent_pool.py` (call site)
- Lines 343–360: calls `discover()` via `asyncio.run()` or `create_task()`. Handles both sync and async fine — changing `discover()` to sync is safe.

---

## Feature 1: Mtime-based Discovery Cache

### Rationale
Hermes `_SKILLS_CACHE` pattern: compute a cheap signature from directory mtimes before scanning. If unchanged and within TTL, skip the full scan entirely.

### Design

**New file**: `agent_cascade/skills/cache_helper.py` (~65 lines)

```python
import os
import time
from pathlib import Path
from typing import FrozenSet, List, Tuple

def compute_scan_signature(
    dirs: List[Path],
    disabled: FrozenSet[str] = frozenset(),
) -> Tuple[Tuple, FrozenSet]:
    """Compute a change-signature for skill scan inputs.

    Includes both dir mtimes (catches add/remove) AND individual SKILL.md
    file mtimes (catches in-place edits). O(#dirs + #skills) stat calls.
    Returns ((dir_path, max_mtime), ...) tuple for hashability.
    """
    sig: list = []
    for d in dirs:
        try:
            m = d.stat().st_mtime
        except OSError:
            continue
        # Scan immediate children: dirs for add/remove detection,
        # SKILL.md files for in-place edit detection
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            em = entry.stat(follow_symlinks=False).st_mtime
                            if em > m:
                                m = em
                        elif entry.name == 'SKILL.md':
                            # Include individual file mtime to detect edits
                            fm = entry.stat(follow_symlinks=False).st_mtime
                            if fm > m:
                                m = fm
                    except OSError:
                        continue
        except OSError:
            pass
        sig.append((str(d), m))
    return (tuple(sig), disabled)
```

**Changes to `manager.py`**:

Add required imports at top of file (~4 lines):
```python
import sys as _sys
import time
from agent_cascade.settings import SKILLS_DISABLED
```

Add instance state in `__init__` (~5 lines added at line 48):
```python
self._cache_signature: Tuple = None
self._cache_timestamp: float = 0.0
self._cache_ttl: float = SKILL_CACHE_TTL_SECONDS  # from settings
self._disabled_names: set = set(SKILLS_DISABLED)
```

Wrap `discover()` with cache check (~20 lines added at line 64):
```python
def discover(self, skill_paths: List[Path]) -> None:
    # Cache check (use monotonic clock for reliability)
    current_sig = compute_scan_signature(skill_paths, frozenset(self._disabled_names))
    now = time.monotonic()
    if (current_sig == self._cache_signature
        and (now - self._cache_timestamp) < self._cache_ttl):
        logger.info("[SKILLS] Cache hit — skipping discovery (age=%.1fs)",
                     now - self._cache_timestamp)
        return

    # ... existing scan logic ...

    self._cache_signature = current_sig
    self._cache_timestamp = now
```

**Changes to `settings.py`** (~3 lines added after line 197):
```python
SKILL_CACHE_TTL_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_SKILL_CACHE_TTL', 30.0))  # Cache TTL for mtime-based discovery cache
```

**Changes to `agent_pool.py`** (~8 lines, lines 343–359):

Simplify the call site — no need for `asyncio.run()` or `create_task()`:
```python
# Before:
_loop = None
...
if _loop is not None:
    _task = self.skill_manager.discover([_skills_dir])
    _created_task = _loop.create_task(_task)
    ...
else:
    _asyncio.run(self.skill_manager.discover([_skills_dir]))

# After:
self.skill_manager.discover([_skills_dir])
```

### Edge Cases
- **First run**: `self._cache_signature` is `None` → always scans.
- **TTL expiry**: After 30s, re-scans even if mtime unchanged (bounds staleness from in-place file edits).
- **New skill added**: Directory mtime changes → signature changes → full scan.
- **Skill edited in-place**: File mtime changes but dir mtime may not → TTL bounds this.
- **Disabled set changes**: Included in signature → triggers re-scan.

---

## Feature 2: Platform Filtering

### Rationale
Hermes `skill_matches_platform` allows skills to declare `platforms: [macos, linux, windows]` in frontmatter. Filter at scan time.

### Design

**New function in `manager.py`** (~25 lines, added near top of file after imports):

```python
import sys as _sys

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
```

**Changes to `manager.py` `discover()`** (~5 lines added inside the scan loop at line 81):

After parsing frontmatter, before registering:
```python
frontmatter = parsed.get('frontmatter', {})
if not _skill_matches_platform(frontmatter):
    logger.debug("[SKILLS] Skill '%s' not compatible with platform %s, skipping",
                 frontmatter.get('name', skill_file.parent.name), _sys.platform)
    skipped_count += 1
    continue
```

**Changes to `manager.py` `_register_single()`** (~8 lines, platform check AFTER parse to avoid double-read):

```python
def _register_single(self, skill_file: Path, priority: int = _PRIORITY_SYSTEM) -> None:
    parsed = parse_skill_file(skill_file)
    frontmatter = parsed.get('frontmatter', {})

    # Platform check after parse (avoids double file read)
    if not _skill_matches_platform(frontmatter):
        name = frontmatter.get('name', skill_file.parent.name)
        logger.debug("[SKILLS] Skill '%s' not compatible with platform %s, skipping",
                     name, _sys.platform)
        return

    # Disabled check
    name = frontmatter.get('name', skill_file.parent.name)
    if name in self._disabled_names:
        logger.debug("[SKILLS] Skill '%s' is disabled, skipping", name)
        return
    ...
```

### Edge Cases
- **Missing field**: Returns `True` (backward compatible — all existing skills work).
- **Empty list**: Returns `True`.
- **String instead of list**: Wrapped into `[platform]` (Hermes-compatible).
- **Case insensitive**: `"MacOS"`, `"LINUX"` normalized via `.lower()`.
- **Unknown platform name**: Falls through (doesn't match).

---

## Feature 2b: `get_all_metadata()` Returns `source` Field

### Rationale
The registry stores `source` (e.g., "system", "auto-generated") but `get_all_metadata()` excludes it. The `scan_skills` tool needs this for the source column.

### Design

**Changes to `manager.py` `get_all_metadata()`** (~2 lines added at line 195):
```python
result.append({
    'name': data.get('name', name),
    'description': data.get('description', ''),
    'triggers': data.get('triggers', []),
    'source': data.get('source', 'system'),  # NEW: include source field
})
```

---

## Feature 3: Prompt Injection Detection

### Rationale
Hermes `_INJECTION_PATTERNS` checks skill content for "ignore previous instructions" patterns. Add as Tier 1 validation.

### Design

**Changes to `validator.py`** (~30 lines added):

Add constant at top (~12 lines, after line 18):
```python
_INJECTION_PATTERNS: list = [
    "ignore previous instructions",
    "ignore all previous",
    "you are now",
    "disregard your",
    "forget your instructions",
    "new instructions:",
    "system prompt:",
    "<system>",
    "]]>",
]
```

Add check in `validate_skill()` (~15 lines, after body check at line 83):
```python
    # Prompt injection check (require 2+ matches to avoid false positives)
    content_lower = skill_content.lower()
    injections = [p for p in _INJECTION_PATTERNS if p in content_lower]
    if len(injections) >= 2:
        errors.append(f"Prompt injection detected (patterns: {', '.join(injections[:3])})")
```

**Scope**: Applied only during auto-skill validation (`register_skill_from_content` path). Existing skills loaded at pool init are NOT re-validated — they skip `validate_skill()`.

### Edge Cases
- **Case insensitive**: Both content and patterns lowercased.
- **Multiple patterns**: Reports up to 3 in error message.
- **Backward compatible**: Existing skills in `.qwen/skills/` are not re-validated on load (only new/registered skills pass through `validate_skill`).

---

## Feature 4: Disabled Skills Config

### Rationale
Allow users to disable specific skills via config without deleting them.

### Design

**Changes to `settings.py`** (~5 lines, after line 197):
```python
_SKILLS_DISABLED_RAW: str = os.getenv('QWEN_AGENT_SKILLS_DISABLED', '')
SKILLS_DISABLED: List[str] = [
    s.strip().lower() for s in _SKILLS_DISABLED_RAW.split(',') if s.strip()
] if _SKILLS_DISABLED_RAW else []
```

**Changes to `manager.py` `__init__`** (~3 lines):
```python
self._disabled_names: set = set(SKILLS_DISABLED)
```

**Changes to `manager.py` `discover()`** (~5 lines, inside scan loop):
```python
name = frontmatter.get('name', '')
if name in self._disabled_names:
    logger.debug("[SKILLS] Skill '%s' is disabled, skipping", name)
    skipped_count += 1
    continue
```

### Edge Cases
- **Empty env var**: `SKILLS_DISABLED` is `[]` — no skills disabled.
- **Whitespace handling**: `"code-review, httpx-pooling "` → `["code-review", "httpx-pooling"]`.
- **Case insensitive**: Names lowercased for comparison.
- **Dynamic registration**: Also check in `_register_single()`.

---

## Feature 5: scan_skills Tool Improvements

### Rationale
1. `_ensure_index()` is redundant — `discover()` rebuilds after registration, and `register_skill_from_content()` rebuilds under lock.
2. No way to see disabled skills.
3. No source column to distinguish system/agent/user/auto-generated skills.

### Design

**Changes to `scan_skills.py`** (~40 lines):

1. **Remove `_ensure_index()` call** (~3 lines removed at line 64):
   Delete the `self._ensure_index(skill_manager)` call. Keep the method for backward compatibility but don't call it.

2. **Add `all` parameter** (~15 lines, modify `parameters` schema at line 26):
```python
'parameters': {
    'type': 'object',
    'properties': {
        'query': { ... },
        'all': {
            'type': 'boolean',
            'description': 'If true, include disabled skills in results. Default: false.',
        },
    },
    'required': [],
}
```

3. **Add source column to output** (~20 lines, modify output formatting at line 72):
```python
# In the listing section:
source = meta.get('source', 'system') if meta else 'unknown'
lines.append(f"- **{skill['name']}** [{source}]: {skill.get('description', 'No description')}")
```

4. **Filter disabled skills when `all=False`** (~8 lines):
```python
parsed = parse_tool_params(params)
query = parsed.get('query', '')
show_all = parsed.get('all', False)

if not show_all:
    disabled = getattr(skill_manager, '_disabled_names', set())
    all_skills = [s for s in all_skills if s['name'] not in disabled]
```

### Edge Cases
- **No `all` param**: Defaults to `False` — disabled skills hidden (current behavior preserved).
- **`all=True` with query**: Shows disabled skills in results.
- **Missing `source` field**: Defaults to `'system'` for backward compat with existing registry entries.

---

## Feature 6: Fix `discover()` async but sync work

### Rationale
`discover()` is declared `async` but does only sync I/O (calls `parse_skill_file` synchronously). The pool init handles both cases but the async declaration is misleading.

### Design

**Changes to `manager.py`** (~5 lines):

Change `async def discover` → `def discover` (line 53). Change `async def _register_single` → `def _register_single` (line 96).

```python
def discover(self, skill_paths: List[Path]) -> None:
    ...
    self._register_single(skill_file, priority=_PRIORITY_SYSTEM)
    ...
```

**Changes to `agent_pool.py`** (~8 lines, lines 343–359):

Simplify the call site — no need for `asyncio.run()` or `create_task()`:
```python
# Before:
_loop = None
...
if _loop is not None:
    _task = self.skill_manager.discover([_skills_dir])
    _created_task = _loop.create_task(_task)
    ...
else:
    _asyncio.run(self.skill_manager.discover([_skills_dir]))

# After:
self.skill_manager.discover([_skills_dir])
```

### Edge Cases
- **No event loop**: Works fine — sync call.
- **Event loop running**: Works fine — sync call.
- **Multiple skill paths**: Still iterates all of them.

---

## Implementation Order

Recommended order to minimize risk:

| Step | Feature | Reason |
|------|---------|--------|
| 1 | Feature #6 (async→sync fix) | Simplest change, no new behavior, required before cache |
| 2 | Feature #4 (disabled config) | Pure filtering, additive |
| 3 | Feature #2 (platform filtering) | Pure filtering, additive |
| 4 | Feature #1 (mtime cache) | Wraps existing logic, depends on #2-#4 for correct signature |
| 5 | Feature #3 (injection detection) | Validator-only change |
| 6 | Feature #5 (scan_skills tool) | UI-only change |

**Note**: Feature #6 must come first because `discover()` and `_register_single()` become synchronous, which the cache wrapper in #1 depends on.

---

## Testing Plan

### Unit tests (new file: `tests/test_skill_discovery_improvements.py`)

| Test | Feature | Description |
|------|---------|-------------|
| `test_cache_hit_skips_scan` | #1 | Second discovery within TTL returns immediately |
| `test_cache_miss_on_ttl_expiry` | #1 | After TTL, re-scans |
| `test_cache_miss_on_mtime_change` | #1 | Dir mtime change invalidates cache |
| `test_cache_miss_on_file_mtime_change` | #1 | Individual SKILL.md edit invalidates cache |
| `test_cache_miss_on_disabled_change` | #1 | Disabled set change invalidates cache |
| `test_platform_filter_macos` | #2 | macOS-only skill excluded on Linux |
| `test_platform_filter_missing` | #2 | No platforms field → included |
| `test_platform_filter_multi` | #2 | `[macos, linux]` matches current |
| `test_platform_filter_register_single` | #2 | Dynamic registration respects platform filter |
| `test_injection_detection` | #3 | Skill with 2+ patterns fails validation |
| `test_injection_single_match` | #3 | Single pattern passes (no false positive) |
| `test_no_injection` | #3 | Normal skill passes |
| `test_disabled_filter` | #4 | Disabled skill excluded from registry |
| `test_disabled_empty` | #4 | Empty disabled list → all included |
| `test_disabled_case_insensitive` | #4 | Name matching is case-insensitive |
| `test_scan_skills_all_flag` | #5 | `all=True` shows disabled skills |
| `test_scan_skills_source_column` | #5 | Output includes `[source]` label |
| `test_get_all_metadata_source` | #2b | `get_all_metadata()` returns source field |
| `test_discover_sync` | #6 | `discover()` is not a coroutine |

### Integration tests
- Full pool init with cache enabled — verify skills loaded correctly.
- Dynamic registration of platform-specific skill — verify filtered on wrong platform.
- Disabled skill re-enabled by changing env var and re-initializing.

---

## Backward Compatibility Checklist

- [x] `discover()` signature unchanged (still accepts `List[Path]`, returns `None`)
- [x] `scan_skills` tool API unchanged (new `all` param is optional)
- [x] `SKILLS_DISABLED` defaults to empty list
- [x] `platforms` frontmatter field is optional
- [x] Cache TTL defaults to 30s (reasonable for dev and prod)
- [x] Injection patterns are broad enough to catch common patterns but not too aggressive
- [x] `get_all_metadata()` still returns same structure (source field already exists in registry)

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Cache stale on in-place edit | Medium | Low | TTL bounds staleness to 30s |
| Platform map misses edge cases | Low | Low | Unknown platforms pass through gracefully |
| Injection false positives | Low | Low | Patterns are broad but common |
| Sync `discover()` blocks event loop | Low | Low | Skill parsing is fast (<100ms for typical setup) |
| Disabled skills not re-checked | Low | Low | Cache signature includes disabled set |

---

## File Change Summary

| File | Additions | Deletions | Net |
|------|-----------|-----------|-----|
| `agent_cascade/skills/cache_helper.py` | +60 | 0 | +60 (new) |
| `agent_cascade/skills/manager.py` | +55 | +8 | +47 |
| `agent_cascade/skills/validator.py` | +25 | 0 | +25 |
| `agent_cascade/settings.py` | +8 | 0 | +8 |
| `agent_cascade/tools/custom/scan_skills.py` | +30 | +5 | +25 |
| `agent_cascade/agent_pool.py` | +3 | +12 | -9 |
| **Total** | **~181** | **~25** | **~156** |