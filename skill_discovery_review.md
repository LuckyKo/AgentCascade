# Skill Discovery Improvements Plan - Critical Review

**Reviewer**: Main (Quality Assurance Specialist)  
**Date**: 2026-07-20  
**Plan**: `agent-cascade-docs/skill-discovery-improvements-plan.md`

## Executive Summary

**VERDICT: NEEDS WORK** — The plan is conceptually sound but contains **critical gaps and implementation risks** that must be addressed before proceeding. Several features have design flaws that could lead to bugs, security issues, or performance problems.

---

## Detailed Findings

### 🔴 CRITICAL Issues

#### 1. Mtime Cache Signature Design Flaw
**Plan says**: Include `disabled` set in cache signature for invalidation.

**Reality**: The signature computation is incomplete. The plan's `compute_scan_signature()` function:
- Computes max mtime of immediate children (dirs only)
- Does **NOT** include individual file mtimes within skill directories
- This means **in-place file edits won't trigger cache invalidation** unless the directory mtime updates

**Example**: If you edit a skill's content, the file mtime changes but the parent directory mtime often doesn't. The cache would remain stale for up to 30 seconds (TTL). While TTL bounds staleness, it's not ideal.

**Recommendation**: Either:
- Include all file mtimes in the signature (more accurate), OR
- Document that TTL is the only protection against in-place edits and accept 30s window of inconsistency

**Severity**: 🔴 Critical - Cache may not work as expected.

---

#### 2. Platform Filtering Missing from `_register_single()`
**Plan says**: Add platform check in both `discover()` and `_register_single()`.

**Reality**: The plan correctly identifies that dynamic registration needs the same check. However, it doesn't specify **where** exactly to place the check.

**Risk**: If `_register_single()` is called from other contexts (e.g., tool-based skill creation), platform filtering would be bypassed.

**Recommendation**: Place the check at the very start of `_register_single()`, before any parsing or registration logic. This ensures all code paths respect platform constraints.

**Severity**: 🟠 Major - Security/compatibility gap.

---

### 🟠 MAJOR Issues

#### 3. Injection Detection False Positive Risk
**Plan says**: Check for patterns like "ignore previous instructions" in skill content.

**Reality**: The pattern list is too broad. Existing legitimate skills may contain these phrases in examples, documentation, or comments. For example:
- A skill about "prompt engineering" might include "ignore previous instructions" as an example of what NOT to do
- A security-focused skill might reference these patterns in its description

**Risk**: Existing well-maintained skills could be rejected during auto-generation validation, or even worse, if applied retroactively to existing skills.

**Recommendation**: 
- Make the check **context-aware** (e.g., require multiple patterns)
- Or only apply to **newly proposed** skills via `validate_skill()`, not to existing installed skills
- The plan says "existing skills in .qwen/skills/ are not re-validated" — this is correct, but we need to ensure dynamic registration also skips validation for explicitly loaded skills

**Severity**: 🟠 Major - Could break legitimate skills.

---

#### 4. Async→Sync Change in agent_pool.py
**Plan says**: Simplify call site from `asyncio.run()`/`create_task()` to direct sync call.

**Reality**: The current code in `agent_pool.py` (lines 338-359) already handles both cases:
```python
if _loop is not None:
    _task = self.skill_manager.discover([_skills_dir])
    _created_task = _loop.create_task(_task)
else:
    _asyncio.run(self.skill_manager.discover([_skills_dir]))
```

**Problem**: If `discover()` becomes sync, this code becomes unnecessary complexity. However, the plan says to change `manager.py` first, then `agent_pool.py`. This is correct order.

**Risk**: There's a subtle race condition: if `discover()` is called while an event loop is running, the current async version runs as a background task. Making it sync could block the main thread during pool initialization. However, skill discovery is fast (<100ms) so this should be fine.

**Recommendation**: Add a comment in `agent_pool.py` explaining why we no longer need the async wrapper. Also consider adding a timeout mechanism if skill discovery ever becomes slow.

**Severity**: 🟠 Major - Threading model change.

---

#### 5. get_all_metadata() Missing Source Field
**Plan says**: Include `source` field in returned metadata.

**Reality**: The registry already stores `'source': frontmatter.get('source', '')` (line 133), but `get_all_metadata()` explicitly excludes it:
```python
result.append({
    'name': data.get('name', name),
    'description': data.get('description', ''),
    'triggers': data.get('triggers', []),
})
```

**Good news**: The plan correctly identifies this gap. The fix is trivial: add `'source'` to the dict.

**Severity**: 🔵 Minor - UI limitation only.

---

### 🟡 MINOR Issues

#### 6. Disabled Skills Not in Cache Signature
**Plan says**: Include `disabled` set in cache signature (line 111).

**Reality**: The plan correctly includes this, but the implementation detail is missing: `self._disabled_names` must be initialized before `discover()` is called. Since `discover()` is called from `__init__` of `SkillManager`? No, it's called from `agent_pool.py` later. So initialization order is fine.

**However**: The plan says to add `self._disabled_names: set = set(SKILLS_DISABLED)` in `manager.py __init__`. This assumes `SKILLS_DISABLED` is imported from settings. Is it? Let's check the imports in manager.py (line 17-25). Currently, it imports only skill-related settings, not `SKILLS_DISABLED`. **This needs to be added.**

**Severity**: 🟡 Minor - Import oversight.

---

#### 7. Platform Mapping Completeness
**Plan says**: Use `_PLATFORM_MAP` with keys "macos", "linux", "windows".

**Reality**: The mapping is:
```python
_PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}
```

But `sys.platform` can return:
- `darwin` for macOS
- `linux` for Linux
- `win32` for Windows
- Also `cygwin`, `java`, etc.

**Issue**: The code uses `current.startswith(mapped)` which means:
- For macOS: `darwin.startswith("darwin")` ✓
- For Linux: `linux.startswith("linux")` ✓
- For Windows: `win32.startswith("win32")` ✓

This seems correct. However, what about case sensitivity? The plan says `.lower()` is used, so `"MacOS"` in frontmatter becomes `"macos"` and maps to `"darwin"`. Then we check `current.startswith("darwin")`. If `sys.platform` is `"darwin"`, it matches. Good.

**Severity**: 🔵 Minor - Should work but edge cases exist.

---

## Implementation Order Review

The plan's recommended order is:
1. Fix async→sync (simplest, no new behavior)
2. Disabled config (pure filtering)
3. Platform filtering (pure filtering)
4. Mtime cache (wraps existing logic)
5. Injection detection (validator-only)
6. scan_skills tool (UI-only)

**Critique**: This is reasonable. However, I'd suggest moving injection detection earlier because it's a security feature and shouldn't depend on other changes. Also, the cache implementation depends on disabled set being available, so disabled config must come before cache.

**Revised order**:
1. Fix async→sync (foundational)
2. Disabled config + platform filtering (both are filters, can be done together)
3. Injection detection (security-critical)
4. Mtime cache (depends on #2)
5. scan_skills tool (UI polish)

---

## Testing Plan Evaluation

The plan proposes 12 unit tests. Coverage seems adequate but has gaps:

**Missing tests**:
- Test that cache invalidates when disabled set changes (not just mtime or TTL)
- Test platform filtering in `_register_single()` specifically
- Test injection detection doesn't false-positive on normal content
- Test that `discover()` being sync doesn't block event loop

**Recommendation**: Expand test coverage to include these edge cases.

---

## Backward Compatibility Assessment

The plan's checklist is mostly correct, but I'd add:

**Potential breaking changes**:
1. **Changing `discover()` from async to sync** — This breaks any code that awaits it or treats it as a coroutine. The only call site is `agent_pool.py`, which will be updated. But what about external users of the SkillManager class? They might be awaiting `discover()`. We need to consider if this is a public API.

   **Check**: Is `SkillManager` part of the public API? Looking at imports, it's imported in `agent_pool.py` and likely used by other internal modules. There's no `__all__` or explicit public API boundary. To be safe, we should consider deprecation: keep async but mark as deprecated, or provide a sync wrapper.

   **Recommendation**: Since the plan says "minimal changes", assume this is an internal-only change. Document that `SkillManager` is not part of the stable public API.

2. **Adding injection detection** — This could cause previously valid auto-generated skills to be rejected. However, the plan says it only applies to `validate_skill()`, which is used for new skills. Existing skills in `.qwen/skills/` are not re-validated. So this should be safe.

3. **Platform filtering** — Skills with unsupported platforms will be silently skipped. This could cause "missing" skills that users expect to work. Need clear documentation and logging.

---

## Risk Assessment (Updated)

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Cache stale on in-place edit | High | Low | TTL bounds to 30s; document limitation |
| Platform map misses edge cases | Low | Low | Unknown platforms pass through |
| Injection false positives on legitimate skills | Medium | Medium | Only apply to new auto-generated skills; not existing ones |
| Sync `discover()` blocks event loop | Low | Low | Skill parsing is fast; consider timeout if needed |
| Disabled set not in cache signature | High | Medium | Include in signature (plan says yes) |
| Breaking changes for external users | Low | High | Document internal API; provide migration path |

---

## Final Verdict: NEEDS WORK

**The plan is fundamentally sound but requires refinement before implementation.** Specifically:

### Must Fix Before Implementation:
1. **Clarify cache invalidation strategy** — Decide if TTL-only is acceptable or if we need full file mtimes.
2. **Add platform check in `_register_single()`** — Ensure all registration paths respect platform constraints.
3. **Refine injection detection patterns** — Make them more specific to avoid false positives on legitimate content.
4. **Update imports in `manager.py`** — Add `SKILLS_DISABLED` from settings.
5. **Consider async→sync impact** — Verify no external code depends on async `discover()`.

### Recommended Changes to Plan:
- Move injection detection earlier (security first)
- Expand test plan with edge case coverage
- Add deprecation warning for async `discover()` if it's public API
- Document the cache limitation (TTL-based staleness)

**Overall**: The plan shows good understanding of the codebase and identifies key improvements. With the above refinements, it could be implemented successfully.

---

## Required Actions

1. **Revise cache design** to address in-place edit staleness
2. **Add platform filtering** to `_register_single()` explicitly
3. **Tighten injection patterns** or limit scope to auto-generated skills only
4. **Update manager.py imports** to include `SKILLS_DISABLED`
5. **Expand unit tests** to cover identified gaps
6. **Document breaking changes** if any

**Estimated additional effort**: ~1-2 days of design and testing work before coding begins.