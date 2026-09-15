# Plan Review: Better Feedback on API Endpoint Failures (REV 2)

**Review Date:** 2026-09-15 (REV 2)
**Reviewer:** plan-review-epf (Senior QA/Critic)
**Plan Location:** `N:\work\WD\AgentCascade\plans\api_failure_feedback_plan.md`
**Target Issue:** todo.md line 219 - improve feedback on API endpoint failures

## Executive Summary

The plan has been substantially improved in REV 2, directly addressing most of the critical findings from the first review. The fixes are well-reasoned and align with established codebase patterns. However, one significant performance concern remains unaddressed regarding the TracebackDedup pruning strategy. After verifying against the actual codebase, the plan is **close to approval** but needs refinement on the dedup performance design.

**VERDICT: NEEDS WORK (REV 2)** - The fallback text and structured attribute fixes are excellent; import audit is satisfactory. The pruning-on-every-call design requires optimization before implementation.

---

## REV 2 Fixes Verification

### ✅ Fix #1: TracebackDedup Bounded Memory (Partially Resolved)
- **Claim:** "should_log_full_tb prunes entries whose last-seen timestamp is older than PRUNE_AFTER (default 3600s) on every call — same pattern as router._cleanup_stale_failure_records."
- **Verification:** The plan now explicitly includes pruning. However, the implementation detail "on every call" is problematic.
- **Evidence:** `router.py:_cleanup_stale_failure_records` is called at specific decision points (L1307, L2407, L1861, L1939), NOT on every HTTP attempt. In contrast, `should_log_full_tb` will be invoked on every single retry attempt during an outage — potentially hundreds of times per second. Pruning by iterating over all dict keys on each call is O(n) and could become a performance bottleneck if n grows to thousands (many endpoints × models × failure types).
- **Performance Analysis:**
  - During a major outage, router might make 500 attempts/sec across 10 endpoints = 5000 calls/sec to `should_log_full_tb`.
  - If each call iterates over say 100 distinct keys (worst-case during mixed failures), that's 500,000 dict iterations/sec.
  - This adds measurable overhead to the retry path and could exacerbate latency issues during the very outages we're trying to log.
- **Fix Required:** Change from "prune on every call" to:
  - Option A: Prune periodically using a counter (e.g., prune every N calls) or a background thread that runs every M seconds.
  - Option B: Use a more efficient data structure (e.g., min-heap keyed by timestamp) for O(1) or O(log n) stale entry removal.
  - Option C: Keep the current approach but document and measure performance impact; add a maximum dict size cap as a safety net.
- **Severity:** 🟠 Major - Potential performance regression during high-load scenarios.

### ✅ Fix #2: Fallback Text Now 'Connection lost'
- **Claim:** `reason = classify_endpoint_failure(error) if error is not None else 'Connection lost'`
- **Verification:** **PASS.** The fallback text exactly matches the original "Connection lost", preserving backward compatibility. This resolves the behavioral change concern.
- **Severity:** ✅ Resolved - No issue.

### ✅ Fix #3: Structured `.endpoint_failures` Attribute
- **Claim:** Attach `exc.endpoint_failures = all_errors` to the RuntimeError at router.py:L2433-2436; llm_call reads via `getattr(e, 'endpoint_failures', None)`.
- **Verification:** **PASS.** The pattern is consistent with existing Python exception attribute usage (e.g., `FallbackCompressionRequired(original_error=...)`). No string parsing needed.
- **Evidence:**
  - `router.py:L2433-2436` shows the exact raise location.
  - `llm_call.py:L1150-1168` terminal path can read the attribute without breaking existing tests (which assert on substring "All API endpoints exhausted").
  - No other codebase locations catch this specific RuntimeError and print str(e) — verified via grep for `except RuntimeError` and `logger.*{e}`.
- **Severity:** ✅ Resolved - Excellent design improvement.

### ✅ Fix #4: Import Audit for error_reporting.py
- **Claim:** Only imports standard library + `agent_cascade.llm.base` (lazy inside functions); must NOT import from api_router_pkg, engine.*, or api_integration_pkg.
- **Verification:** **PASS.** The import audit is explicit and conservative. We verified that `agent_cascade/llm/base.py` does not import anything from router/engine at module level (checked lines 1-80). The plan uses duck-typing to avoid importing openai/httpx types, preventing transitive dependencies.
- **Severity:** ✅ Resolved - No circular import risk.

### ✅ Fix #5: Edge Cases in format_endpoint_error / classify_endpoint_failure
- **Claim:** Tests now include `None` input → safe fallback; message >160 chars → truncated; deeply nested `__cause__` chain (3+ levels) → root cause still found; non-Exception object → safe fallback.
- **Verification:** **PASS.** The test plan explicitly covers these edge cases.
- **Severity:** ✅ Resolved - Good defensive coding.

### ✅ Fix #6: Thread-Safety Implementation Details
- **Claim:** "a single `threading.Lock` guards the dict; it is held for the ENTIRE check-and-update in should_log_full_tb (atomic read-compare-write). The lock is never held while logging (release before any logger call — callers log outside the method). No nested locking."
- **Verification:** **PASS.** This is a clear and correct thread-safety design following the principle of minimizing lock duration and avoiding nested locks.
- **Severity:** ✅ Resolved - Sound concurrency model.

### ✅ Fix #7: Test Coverage Adequacy
- **Claim:** New `tests/test_error_reporting.py` includes pruning test, edge cases, router layer-1, llm_call terminal path, and regression suite (full 2610 tests).
- **Verification:** **PASS.** The test plan is comprehensive, including:
  - Pruning test (addresses memory growth)
  - Edge case tests
  - Regression testing of existing suite
- **Severity:** ✅ Resolved - Good coverage.

---

## Additional Verification for REV 2

### Other Code That Might Print str(e) Again?
- **Search:** Grep for `except.*:.*logger\.(error|warning).*{e}` and `except RuntimeError` patterns.
- **Finding:** No other places in the call stack catch the terminal RuntimeError and log its message. The only logging of this error occurs in `llm_call.py:L1165` and `L1200`, which are already being replaced with compact digests per the plan.
- **Collision Check:** No existing `.endpoint_failures` attribute found in the codebase (grep returned 0 matches). Safe to use.

### Test Assertions on Old Formats
- **Search:** Grep tests for "All API endpoints exhausted" and "[RETRYING] Connection lost".
- **Finding:** Tests assert on "All API endpoints exhausted" substring, which will still be present in str(e) (now includes compact lines). No test asserts on the exact "[RETRYING] Connection lost" text.
- **Conclusion:** Existing tests should pass with the new format. Regression testing plan is sufficient.

---

## Remaining Issue & Required Changes

### 🟠 Major: TracebackDedup Performance Optimization Needed

**Problem:** Pruning by iterating over all keys on every call to `should_log_full_tb` introduces O(n) overhead that could become significant under high retry rates during outages.

**Evidence:**
- Router retry loop may invoke `should_log_full_tb` hundreds of times per second.
- If distinct failure keys grow to hundreds or thousands, each call incurs linear scan cost.
- This adds latency to the critical retry path, potentially worsening outage recovery.

**Recommended Fix (Choose One):**

1. **Periodic Pruning with Counter:**
   ```python
   class TracebackDedup:
       def __init__(self):
           self._dict = {}
           self._lock = threading.Lock()
           self._prune_counter = 0
           self._prune_every = 100  # prune every 100 calls

       def should_log_full_tb(self, key, now=0.0):
           with self._lock:
               self._prune_counter += 1
               if self._prune_counter % self._prune_every == 0:
                   self._prune_old_entries(now)
               # ... rest of logic
   ```

2. **Background Thread Cleanup:**
   - Start a daemon thread that runs every 60 seconds to prune the dict.
   - Similar to how `router.py` might use a separate cleanup mechanism.

3. **LRU Cache with Max Size:**
   - Use `functools.lru_cache` style maxsize, or implement a bounded dict with LRU eviction.
   - Simpler than timestamp-based pruning and guarantees memory bounds.

4. **Document and Benchmark:**
   - If keeping "prune on every call", must benchmark with realistic failure patterns to prove acceptable performance. Provide metrics showing no regression in retry latency.

**Must Do Before Implementation:** Either implement a more efficient pruning strategy (Option 1, 2, or 3) OR provide benchmark data proving the current approach is performant under expected load.

---

## Conclusion

REV 2 has made excellent progress on all critical issues:
- ✅ Fallback text preserved exactly
- ✅ Structured attribute eliminates brittle parsing
- ✅ Import audit prevents circular dependencies
- ✅ Edge cases covered in tests
- ✅ Thread-safety clearly specified
- ✅ Comprehensive test plan with regression suite

The **only** remaining blocker is the performance design of TracebackDedup pruning. This is a major (not critical) issue that could impact production reliability during outages.

**VERDICT: NEEDS WORK (REV 2)** - Implement a more efficient pruning strategy (or provide benchmark justification) before approval.

---

## REV 3 Final Verification (Performance Fix #1)

**Claim:** Counter-based pruning every PRUNE_EVERY_NTH_CALLS=100th call, removing entries older than PRUNE_AFTER=3600s. Counter guarded by same lock.

**Verification:** **PASS.** This is exactly the recommended solution from REV 2 (Option 1).

- ✅ Pruning occurs at most once per 100 calls, not every call
- ✅ At high retry rates (e.g., 500 attempts/sec), pruning runs ~5 times/sec
- ✅ Each O(n) sweep operates on bounded dict (entries only from last 3600s)
- ✅ Lock is held for the entire check-and-update including counter increment and prune
- ✅ No risk of deadlock (same lock as `should_log_full_tb`, no nested locks)

This design provides **bounded memory** with **minimal overhead** during high-load scenarios. The performance concern from REV 2 is fully resolved.

---

## Required Final Change

1. **None.** All issues addressed.

**VERDICT: PASS** - The plan is now ready for implementation.

---

## Summary of All Resolved Issues

| Issue | REV 1 | REV 2 | REV 3 |
|-------|-------|-------|-------|
| #1 TracebackDedup pruning perf | 🔴 Critical (no cleanup) | 🟠 Major (prune every call) | ✅ PASS (counter-based) |
| #2 Fallback text `'Connection lost'` | 🔴 Critical (changed) | ✅ Resolved | ✅ Confirmed |
| #3 Structured `.endpoint_failures` attribute | 🔴 Critical (brittle parsing) | ✅ Resolved | ✅ Confirmed |
| #4 Import audit for error_reporting.py | 🔴 Critical (circular risk) | ✅ Resolved | ✅ Confirmed |
| #5 Edge cases in error functions | 🟠 Major (missing) | ✅ Resolved | ✅ Confirmed |
| #6 Thread-safety details | 🟠 Major (vague) | ✅ Resolved | ✅ Confirmed |
| #7 Test coverage adequacy | 🟠 Major (insufficient) | ✅ Resolved | ✅ Confirmed |

**Final Verdict: PASS** - All critical and major issues resolved. Implementation can proceed.
