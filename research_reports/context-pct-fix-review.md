# Independent Review: Compression Display Correctness Fix (commit a9ea327)

**Verdict: PASS**

## Executive Summary
The fix successfully resolves the denominator mismatch between displayed percentage and printed token ratio in compression warnings. All three warning call sites now consistently use the same effective limit for both values. Trigger timing remains unchanged (display-only change). No regressions in tests.

## Detailed Findings

### 1. Correctness of the Fix

| Severity | Location | Issue | Suggested Fix |
|----------|----------|-------|---------------|
| 🔵 None | All warning paths | Verified: `%` and `A/B` now share denominator | - |

**Verification:**
- **Call site 1** (`compression_exec.py:194`): Passes `effective_limit` which was computed from `max_tokens_for_check - reserve_tokens` with fallback to raw max if ≤0. This matches the percentage calculation.
- **Call site 2** (`handler.py:696`): Uses `self.engine._get_effective_limit(instance)`.
- **Call site 3** (`handler.py:786`): Uses `self.engine._get_effective_limit(instance)`.

### 2. Consistency Across All Token-% Print Sites

| Severity | Location | Issue | Suggested Fix |
|----------|----------|-------|---------------|
| 🔵 None | `_inject_compression_warning` (compression_exec.py:285-289) | Verified: The warning string uses `max_tokens` parameter which now equals the effective limit at all call sites. | - |
| 🔵 None | `ContextWindowExceeded` raise (compression_exec.py:165-169) | Already consistent: uses `effective_limit` for both % and fraction. | - |
| 🔵 None | Post-compression feedback (handler.py:187) | Already self-consistent: uses raw `max_tokens` for both % and fraction. Not part of this bug (post-compression state). | - |

**Search results:** No other locations in `agent_cascade/` print a combination of percentage and token ratio that could exhibit the same mismatch.

### 3. The New Helper: `_get_effective_limit`

| Severity | Location | Issue | Suggested Fix |
|----------|----------|-------|---------------|
| 🟡 Minor | `core.py:3218` | Uses `or 0` which is defensive but safe. Setting is validated to be ≥500, so this is unnecessary but harmless. | None required. |
| 🔵 None | `core.py:3209-3220` | Logic matches inline computation in `compression_exec.py:152-155`. | - |
| 🔵 None | Edge case: reserve >= max_tokens | Correctly falls back to raw max tokens. | - |

The helper provides a single source of truth for display, eliminating the risk of drift between call sites. The inline `effective_limit` computation in `compression_exec.py` is necessary for calculating `usage_pct` before warning injection; this is not a duplicate source of truth but rather the computation that feeds the display.

### 4. No Behavior Change

| Severity | Location | Issue | Suggested Fix |
|----------|----------|-------|---------------|
| 🔵 None | Trigger timing | Unchanged: thresholds still compare `usage_pct` against effective limit. | - |
| 🔵 None | Reserve-based early trigger | Intentional design preserved; only the feedback string changed. | - |

### 5. Edge Cases

| Severity | Scenario | Outcome |
|----------|----------|---------|
| 🔵 Safe | `effective_limit` ≤ 0 | Falls back to raw max tokens (both in helper and inline code). |
| 🔵 Safe | Missing setting attribute | Returns 0 via `getattr(..., 0)`, then effective = max - 0 = max. |
| 🔵 Safe | Setting value is 0 or negative | `or 0` handles 0; negative would add to max but config validation prevents this (min 500). |

### 6. Test Results

- **Command:** `python -m pytest tests/compression/ tests/test_session_caption.py -q`
- **Result:** **89 passed** in 8.29s
- **No failures or regressions detected.**

## Explicit Confirmations

(a) **All 3 warning paths now consistent:** ✅ Verified via code inspection. All pass the effective limit (either directly or via helper).

(b) **No missed mismatch sites:** ✅ Comprehensive search of `agent_cascade/` reveals no other locations printing both a percentage and token ratio with potential inconsistency.

(c) **Trigger timing unchanged:** ✅ The fix is purely display; threshold comparisons still use effective limit-based `usage_pct`.

(d) **Test result:** ✅ 89 passed, no failures.

## Conclusion
The fix is correct, complete, and safe. It resolves the denominator mismatch without altering any behavioral logic. All warning paths are now consistent.

---
**Review completed by:** pct-fix-review (independent QA specialist)
**File created at:** `N:\work\WD\AgentCascade\research_reports\context-pct-fix-review.md`
