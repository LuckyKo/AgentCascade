# Code Review: AUTO Skill Advisor Bug Fixes - Second Pass

**Review Target:** 6 commits (5 fixes + streaming feature) for the AUTO Skill Advisor in AgentCascade
**Previous Verdict:** REJECT (3 critical, 2 major issues identified)
**Fixes Applied:** All 3 critical blockers + 2 major issues resolved
**Test Results:** 49/49 tests passing (added 2 brace regression tests)
**Review Date:** 2026-08-30
**VERDICT: APPROVE_WITH_CHANGES**

---

## VERDICT SUMMARY

The fixes are **substantially correct and address all critical safety issues**. The code is now safe for production use. However, a minor documentation gap remains regarding potential brace over-escaping edge cases.

**Primary Recommendation:** Approve merging with one small documentation addition.

---

## BLOCKERS - VERIFICATION

### ✅ BLOCKER 1: Prompt Format String Injection — FIXED
- **File:** `agent_cascade/skills/advisor.py` lines 77-88
- **Fix:** Added `_esc()` helper that doubles braces in user content before `.format()`.
- **Verification:** 
  ```python
  def _esc(text: str) -> str:
      return text.replace('{', '{{').replace('}', '}}')
  
  return SKILL_ADVISOR_PROMPT.format(
      task_text=_esc(task_text or "..."),
      context_text=_esc(context_text or "..."),
      ...
  )
  ```
- **Tests:** Added `test_braces_in_task_text_do_not_crash` and `test_braces_in_context_text_do_not_crash`.
- **Assessment:** ✅ Correct. The escaping is minimal and sufficient. Even if user content already contains `{{`, it will become `{{{` after replacement, which `.format()` will interpret as a literal `{`. This is safe, though may produce unexpected double braces in output. Document this edge case.

### ✅ BLOCKER 2: Generator Cleanup on Early-Exit — FIXED
- **File:** `agent_cascade/advisor_runner.py` lines 149-219
- **Fix:** Bound generator to `_engine_gen`, wrapped loop in `try/finally` with `_engine_gen.close()`.
- **Verification:**
  ```python
  _engine_gen = engine.run(instance)
  try:
      for resp in _engine_gen:
          ...
          if verdict_detected:
              break
  finally:
      try:
          _engine_gen.close()
      except Exception:
          pass
  ```
- **Assessment:** ✅ Perfect. This matches the pattern used in `_create_and_run_agent` (core.py). Generator will be properly closed on all exit paths, preventing resource leaks and state corruption.

### ✅ BLOCKER 3: Context Mutation — FIXED
- **File:** `agent_cascade/engine/core.py` lines 3025-3027
- **Fix:** Changed `args['context'] = context_text` to `args = dict(args); args['context'] = context_text`.
- **Verification:**
  ```python
  if _advisor_task_notes and not _is_recall:
      args = dict(args)  # Copy before mutate
      args['context'] = context_text
  ```
- **Assessment:** ✅ Correct. This prevents mutating the caller's args dict. The shallow copy is sufficient because we're only modifying top-level keys, not nested structures. No new issues introduced.

---

## MAJOR ISSUES - VERIFICATION

### ✅ MAJOR 4: Pool Lock Access Safety — FIXED
- **File:** `agent_cascade/advisor_runner.py` line 192
- **Fix:** Added defensive `hasattr` checks before accessing `_execution._state_lock`.
- **Verification:**
  ```python
  if hasattr(pool, '_execution') and hasattr(pool._execution, '_state_lock'):
      with pool._execution._state_lock:
          ...
  ```
- **Assessment:** ✅ Good. This prevents AttributeError on pools without `_execution`. The try/except remains as a safety net, but the explicit check is cleaner.

### ✅ MAJOR 5: Multi-element List Documentation — FIXED
- **File:** `agent_cascade/engine/core.py` lines 2819-2820
- **Fix:** Added comment documenting behavior.
- **Verification:**
  ```python
  # Multi-element lists (e.g. ["AUTO", "docker"]) default to AUTO — the
  # presence of explicit skill names alongside AUTO means "auto + extras".
  load_skill_mode_upper = (...)
  ```
- **Assessment:** ✅ Acceptable. The comment clarifies intent. However, the actual behavior still defaults to AUTO for any list containing AUTO (not just multi-element). This is technically correct but could be more precisely documented. Not a blocker.

---

## NEW ISSUES FROM FIXES

### ⚠️ Minor: Potential Brace Over-Escaping
- **Location:** `agent_cascade/skills/advisor.py` `_esc()` function
- **Issue:** If user content already contains `{{` or `}}`, the escaping will produce `{{{` or `}}}`. While this is safe (`.format()` treats `{{` as literal `{`), it may result in unexpected double braces in the final prompt.
- **Impact:** Extremely unlikely in real usage. Even if it occurs, it's not a security issue—just cosmetic.
- **Recommendation:** Add a comment explaining that this is expected behavior and safe.

### ⚠️ Minor: Shallow Copy Assumption
- **Location:** `agent_cascade/engine/core.py` `args = dict(args)`
- **Issue:** The copy is shallow. If `args` contains nested mutable objects (e.g., lists in 'context'), modifying them would still affect the original. However, we're only replacing the top-level 'context' key, so this is not a problem.
- **Recommendation:** None needed—current approach is correct.

---

## TEST COVERAGE ANALYSIS

**49/49 tests passing** is excellent. The two new brace regression tests specifically verify the fix for BLOCKER 1.

**Missing Test (Minor):**
- A test to verify that `args` copy prevents mutation of the original dict in the full integration flow could be added, but the unit tests in `test_skill_advisor_integration.py` already cover the gate behavior end-to-end. Not critical.

---

## SECURITY & SAFETY VERIFICATION

### ✅ No New Attack Surface
- The `_esc()` function is simple and only affects string formatting.
- Generator cleanup uses standard Python patterns.
- Args copy doesn't introduce any new dependencies or logic.

### ✅ Error Handling Preserved
- All try/except blocks from the original code are retained where appropriate.
- The `finally` block for generator cleanup has its own try/except to avoid masking errors.

### ✅ Performance Impact Negligible
- `_esc()` adds O(n) string operations—acceptable for prompt building.
- `dict(args)` adds shallow copy overhead—minimal compared to LLM call costs.
- `hasattr` checks are negligible.

---

## FINAL VERDICT: APPROVE_WITH_CHANGES

**The fixes are correct, safe, and sufficient.** The code can be merged after adding a brief documentation note about brace over-escaping (optional but recommended).

### Required Changes Before Merge:
1. **None** - All critical and major issues resolved.

### Recommended Enhancements (Optional):
1. Add docstring to `_esc()` function explaining that it's defensive against user content containing braces, and that double-braces in input will produce triple-braces in the prompt but remain safe.
2. Consider adding a regression test that verifies `args` is not mutated (though existing tests already cover this indirectly).

### Post-Merge Actions:
1. Create a project memory in `.agent_lessons/` documenting this bug pattern (format string injection in prompts) to prevent future occurrences.
2. Update any related documentation about safe string formatting practices.

---

## COMPARISON TO INITIAL REVIEW

| Issue | Status (Initial) | Status (Pass 2) | Fix Quality |
|-------|------------------|-----------------|-------------|
| Prompt injection | 🔴 CRITICAL | ✅ RESOLVED | Excellent - minimal, correct escaping |
| Generator cleanup | 🔴 CRITICAL | ✅ RESOLVED | Excellent - matches production pattern |
| Context mutation | 🔴 CRITICAL | ✅ RESOLVED | Perfect - simple copy before mutate |
| Lock safety | 🟠 MAJOR | ✅ RESOLVED | Good - defensive `hasattr` checks |
| Documentation | 🟠 MAJOR | ✅ RESOLVED | Adequate - comment added |

**Overall Fix Quality:** 9.5/10. All fixes are minimal, focused, and correctly implemented.

---

*Report generated by advisor_fixes_review using systematic code analysis.*
