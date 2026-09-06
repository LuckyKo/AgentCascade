# Code Review: AUTO Skill Advisor Bug Fixes

**Review Target:** 6 commits (5 fixes + streaming feature) for the AUTO Skill Advisor in AgentCascade
**Reviewer:** advisor_fixes_review
**Date:** 2026-08-30
**Status:** REJECT - Critical issues require immediate attention before shipping

---

## VERDICT: REJECT

This review identifies **critical vulnerabilities and safety issues** that must be addressed before the AUTO Skill Advisor feature can be safely deployed. The prompt injection vulnerability is particularly severe and could cause runtime failures in production.

---

## BLOCKERS (Must Fix Before Shipping)

### 1. **CRITICAL: Prompt Format String Injection Vulnerability**
- **Files:** `agent_cascade/skills/advisor.py` lines 77-83
- **Issue:** The `SKILL_ADVISOR_PROMPT.format()` method directly injects `task_text` and `context_text` without escaping braces. Any user content containing `{` or `}` will raise `KeyError` or `IndexError`, crashing the delegation process.
- **Evidence:** 
  ```python
  return SKILL_ADVISOR_PROMPT.format(
      skills_metadata=skills_metadata,
      task_text=task_text or "(no task text provided)",
      context_text=context_text or "(no additional context)",
      agent_class=agent_class,
      caller_name=caller_name or "unknown",
  )
  ```
- **Impact:** User-provided task text with braces (e.g., "Create {filename}.py") breaks the entire delegation flow.
- **Fix Required:** Use safer string formatting:
  - Option A: Switch to `str.replace()` for each placeholder individually
  - Option B: Use `%`-style formatting with proper escaping
  - Option C: Escape braces in user content before `.format()` (replace `{` with `{{`, `}` with `}}`)
- **Recommended:** Option C with a utility function to escape user content:
  ```python
  def _escape_format_fields(text: str) -> str:
      return text.replace('{', '{{').replace('}', '}}')
  
  return SKILL_ADVISOR_PROMPT.format(
      task_text=_escape_format_fields(task_text or "..."),
      context_text=_escape_format_fields(context_text or "..."),
      ...
  )
  ```

### 2. **CRITICAL: Early-Exit Breaks Generator Cleanup**
- **Files:** `agent_cascade/advisor_runner.py` lines 149-209
- **Issue:** The early-exit `break` statement (line 209) exits the `for resp in engine.run(instance)` loop WITHOUT binding the generator and calling `.close()`. This leaks the generator and may leave the advisor instance in RUNNING state.
- **Evidence:**
  ```python
  for resp in engine.run(instance):
      ...
      if _VERDICT_RE.search(...):
          break  # Generator not properly closed!
  ```
- **Comparison:** `_create_and_run_agent` in `core.py:3083-3134` has proper `try/finally` cleanup with generator close.
- **Impact:** Advisor instance may remain in RUNNING state, blocking re-use and potentially tripping L1 guard checks.
- **Fix Required:** Bind the generator and ensure `.close()` is called:
  ```python
  generator = engine.run(instance)
  try:
      for resp in generator:
          ...
          if _VERDICT_RE.search(...):
              break
  finally:
      generator.close()
  ```

### 3. **CRITICAL: Context Mutation Mutates Caller's Dict**
- **Files:** `agent_cascade/engine/core.py` lines 3023-3024
- **Issue:** Direct assignment `args['context'] = context_text` mutates the caller's args dictionary in place. If callers reuse the same args dict after `_create_and_run_agent` returns, they will see unexpected modifications.
- **Evidence:**
  ```python
  if _advisor_task_notes and not _is_recall:
      args['context'] = context_text  # IN-place mutation!
  ```
- **Impact:** Caller's original args dict is modified, potentially causing subtle bugs in retry logic or chained operations.
- **Fix Required:** Make a copy before mutation:
  ```python
  if _advisor_task_notes and not _is_recall:
      args = dict(args)  # Copy to avoid mutating caller
      args['context'] = context_text
  ```
  Or use `setdefault` pattern if appropriate.

---

## MAJOR (Should Fix - Significant Risk)

### 4. **Pool._execution._state_lock Access Safety**
- **Files:** `agent_cascade/advisor_runner.py` lines 190, 261; test fakes in `tests/test_skill_advisor*.py`
- **Issue:** Direct access to `pool._execution._state_lock` assumes `_execution` attribute exists. Test fakes expose this, but production code may have pools without `_execution`. The try/except is safe but the pattern is brittle.
- **Evidence:**
  ```python
  try:
      with pool._execution._state_lock:  # Line 190
          ...
  except Exception:
      pass
  ```
- **Impact:** If a pool type doesn't have `_execution`, AttributeError will be caught and silently ignored, potentially leaving state unupdated.
- **Recommendation:** Add defensive check before lock access:
  ```python
  if hasattr(pool, '_execution') and hasattr(pool._execution, '_state_lock'):
      with pool._execution._state_lock:
          ...
  ```

### 5. **Gate Normalization Edge Cases**
- **Files:** `agent_cascade/engine/core.py` lines 2805-2821
- **Issue:** Multi-element list with AUTO mixed in falls through to "AUTO" default at lines 2819-2821. This behavior is not documented and may be unintended.
- **Evidence:**
  ```python
  elif isinstance(load_skill_value, list) and len(load_skill_value) == 1:
      _single = str(load_skill_value[0]).strip().upper()
      if _single in (LOAD_SKILL_AUTO, LOAD_SKILL_NONE):
          load_skill_value = _single
  load_skill_mode_upper = (
      load_skill_value.strip().upper() if isinstance(load_skill_value, str) else "AUTO"
  )
  ```
- **Impact:** `load_skill=["AUTO", "some-skill"]` would not be normalized to just "AUTO", potentially causing unexpected behavior.
- **Recommendation:** Either document this edge case or normalize multi-element lists to "AUTO" if AUTO is present:
  ```python
  elif isinstance(load_skill_value, list):
      if LOAD_SKILL_AUTO in (str(s).upper() for s in load_skill_value):
          load_skill_value = LOAD_SKILL_AUTO
  ```

### 6. **Streaming Broadcast Order with Early-Exit**
- **Files:** `agent_cascade/advisor_runner.py` lines 176-209
- **Issue:** Verdict detection happens AFTER broadcast_stream_update. If the verdict appears in the last message, that broadcast includes the verdict. However, if the loop breaks immediately after, there's no guarantee the final UI state is consistent.
- **Evidence:** The broadcast at line 176 updates the UI, then early-exit check at line 209 breaks. This seems correct but should be verified.
- **Impact:** Could leave UI with stale data if broadcast fails or if verdict message is not fully rendered.
- **Recommendation:** Add explicit test to verify final stream update contains verdict before break.

---

## MINOR (Nice to Have - Low Risk)

### 7. **Test Coverage Gaps**
- **Files:** `tests/test_skill_advisor*.py`
- **Missing Tests:**
  - DENY path surfacing through full `call_agent` path
  - Pool stop during advisor run
  - Prompt with braces in task_text (regression test for fix #1)
  - Multi-element `["AUTO","skill"]` normalization
  - Generator cleanup on early-exit (test that generator is closed)
- **Impact:** Potential regressions undetected.

### 8. **Memory/Documentation**
- **Files:** Project memories should document the prompt injection vulnerability and its fix.
- **Recommendation:** Create memory file in `.agent_lessons/` documenting this bug pattern to prevent future occurrences.

---

## PRE-VERIFIED LEADS CONFIRMATION

### ✅ Lead #1: Prompt .format() vulnerability - CONFIRMED CRITICAL
The prompt uses `.format()` with direct user content injection. This is a confirmed critical security issue.

### ✅ Lead #2: Early-exit generator cleanup - CONFIRMED CRITICAL
The `break` statement exits the loop without closing the generator. Confirmed bug matching pattern in `_create_and_run_agent`.

### ✅ Lead #3: Context mutation - CONFIRMED CRITICAL
Direct assignment to `args['context']` mutates caller's dict. Confirmed risk of side effects.

### ⚠️ Lead #4: pool._execution._state_lock access - MAJOR RISK
The lock access is wrapped in try/except, but test fakes expose it consistently. Production pools may vary.

### ⚠️ Lead #5: Gate normalization edge cases - MAJOR RISK
Multi-element list handling is unclear. Not documented, could be unintended.

### ✅ Lead #6: DENY path message role - MINOR RISK
Role changed from FUNCTION to ASSISTANT. Need to verify `extract_instance_output` handles it correctly.

### ⚠️ Lead #7: Streaming safety - MINOR RISK
`broadcast_stream_update` uses `run_coroutine_threadsafe` + `put_nowait`. Should be safe but verify ordering.

### ✅ Lead #8: Test gaps - CONFIRMED MINOR ISSUES
Significant test coverage gaps identified that could allow regressions.

---

## SUMMARY OF REQUIRED CHANGES

### MUST FIX (Blocking):
1. Escape user content in `build_skill_advisor_prompt` to prevent `.format()` injection
2. Properly close generator after early-exit in `run_lightweight_advisor`
3. Copy args dict before mutating context in `_create_and_run_agent`

### SHOULD FIX (Before Release):
4. Add defensive check for `pool._execution._state_lock` existence
5. Document or fix multi-element load_skill normalization
6. Verify streaming broadcast order with explicit test
7. Expand test coverage for edge cases

---

## FINAL RECOMMENDATION

**DO NOT MERGE** until critical issues are fixed and verified. The prompt injection vulnerability alone is sufficient to block shipping, as it will cause runtime failures in production when users include braces in task descriptions. The generator cleanup issue could cause resource leaks and state corruption.

**Estimated Fix Time:** 4-6 hours for all critical + major issues
**Testing Required:** Regression tests for each fix, plus integration tests for the full flow

---

*Report generated by advisor_fixes_review using systematic code analysis.*
