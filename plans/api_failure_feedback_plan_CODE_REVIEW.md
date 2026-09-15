# Code Review: Better Feedback on API Endpoint Failures

**Review Date:** 2026-09-15
**Reviewer:** code-review-epf (Senior QA/Critic)
**Implementation Agent:** [Not specified]
**Plan Reference:** `plans/api_failure_feedback_plan.md` (REV 3)

## Executive Summary

The implementation is largely well-executed and follows the plan closely. The new `error_reporting.py` module is a clean leaf module with proper duck-typing and defensive error handling. Tests are comprehensive and pass (27/27). However, a **critical bug** in the traceback deduplication logic will cause it to malfunction in production: the call sites omit the required `now` parameter, resulting in permanent deduplication (tracebacks never re-logged after first occurrence) and unbounded memory growth.

**VERDICT: FAIL** - Requires critical fix before merge.

---

## 1. Plan Conformance

### §3 Changes Implemented ✓
All specified changes from the plan are present:

| Plan Section | Implementation | Location |
|--------------|----------------|----------|
| 3.1 New helper module | `error_reporting.py` (526 lines) | NEW |
| 3.2 Layer-1 logging | Compact WARNING + deduped DEBUG | `router.py:2378-2388` |
| 3.3 Layer-2 logging | `summarize_exhaustion()` in retries & fatal | `llm_call.py:1240,1220` |
| 3.4 Terminal message | `build_terminal_message()` + `.endpoint_failures` | `llm_call.py:1173`; `router.py:2448` |
| 3.5 Transient message | `_make_retrying_message` with `error` param | `core.py:1541-1571` |

### Plan Review (REV 3) Status ✓
All issues from the plan review have been addressed in the implementation, including:
- Counter-based pruning (every 100th call, PRUNE_AFTER=3600s)
- Backward compatibility for error=None path
- Structured `.endpoint_failures` attribute (no string parsing)
- Import audit (leaf module, stdlib + lazy ModelServiceError)

---

## 2. Hard Constraints Verification

### (a) error_reporting.py is a leaf module ✓
- Module-level imports: **only stdlib** (`hashlib`, `re`, `threading`, `time`, `typing`)
- `ModelServiceError` imported **lazily inside functions** (lines 161, 469)
- **NO** imports from `api_router_pkg`, `engine.*`, `api_integration_pkg`
- openai/httpx types handled purely by duck-typing (class names + attributes)

### (b) ZERO control-flow changes in router retry/backoff/failover/breaker logic ✓
Diff audit confirms only **logging statements** modified. No changes to:
- Retry counts, backoff calculation, failover order, circuit breaker logic
- The except block at `router.py:2373-2390` retains identical control flow

### (c) First line of terminal RuntimeError unchanged ✓
```python
exc = RuntimeError(
    f"All API endpoints exhausted for agent type '{agent_type}'.\n"
    + '\n'.join(all_errors)
)
```
Preserved exactly as required. Existing tests assert on this prefix.

---

## 3. Test Results

### Full suite run (as requested)
```bash
python -m pytest tests/test_error_reporting.py tests/test_sticky_slot_assignment.py tests/test_retry_baseline.py -q
```
**Result: 76 passed in 14.69s** ✅

### Individual file results
- `test_error_reporting.py`: **27 passed** (including all edge cases, thread-safety, pruning)
- `test_sticky_slot_assignment.py`: regression suite passes
- `test_retry_baseline.py`: regression suite passes

---

## 4. Findings

### 🔴 Critical

#### 1. TracebackDedup production calls omit `now` parameter
**Location:** `router.py:2387`, `llm_call.py:1179`

**Issue:** Both call sites invoke:
```python
TB_DEDUP.should_log_full_tb(TB_DEDUP.get_tb_key(e))
```
without the required `now` argument. The method defaults to `now=0.0`. This causes:
- **Permanent deduplication:** All calls use time=0.0; the window check `(0.0 - 0.0) >= 60` is always False after first entry. Full tracebacks will **never** be re-logged, even after days of outage.
- **Unbounded memory growth:** Pruning compares `(0.0 - ts) >= 3600` which is always False; stale entries **never get removed**.
- **Violation of plan goal:** "once per 60s window" becomes "once ever".

**Evidence:** The method signature expects `now=0.0` for testing flexibility, but production must pass `time.time()`. Neither call site does this.

**Fix:** Change both call sites to:
```python
if TB_DEDUP.should_log_full_tb(TB_DEDUP.get_tb_key(e), time.time()):
```

---

### 🟠 Major (Potential)

#### 2. Message format change in RETRYING message
**Location:** `core.py:1570`

**Issue:** The new format uses a comma after the reason:
```python
f"[RETRYING] {reason}, retrying ({attempt}/{max_retries}) in {delay:.1f}s..."
```
Old format (per plan) was `"[RETRYING] Connection lost, retrying (n/N) in Xs..."` - wait, it also has a comma. Let me double-check the exact old format.

**Actual verification:** The test at `test_error_reporting.py:510` asserts:
```python
assert m.content == "[RETRYING] Connection lost, retrying (1/3) in 2.5s..."
```
This matches the implementation, so the change is **intentional** and tests pass. No issue.

#### 3. Potential inconsistency in _action_hint classification
**Location:** `error_reporting.py:370-398`

**Issue:** `_action_hint` uses `_label_from_line()` to classify text lines, while `classify_endpoint_failure` classifies exceptions. The two classification paths may produce different labels for similar failures because they use slightly different heuristics (e.g., text patterns vs exception types). This could lead to inconsistent action hints.

**Severity:** 🟡 Minor - not a bug, just a design nuance. The plan accepts this trade-off.

---

### 🟡 Minor

#### 4. Default parameter semantics for `now`
**Location:** `error_reporting.py:491`

**Issue:** The default `now=0.0` is misleading for production use. A cleaner design would be `now=None` with automatic `time.time()` when None, or making the parameter required (no default) to force explicit calls. However, this is a minor API design nit.

#### 5. Missing import of `time` in call sites
**Location:** `router.py`, `llm_call.py`

**Issue:** `time` is already imported in both files, but the call sites don't use it. This is not an error - just a reminder that the fix in #1 will require using the existing import.

---

### 🔵 Nitpicks

#### 6. Tiebreaker in dominant label calculation
**Location:** `error_reporting.py:390`

```python
dominant = max(labels.items(), key=lambda kv: (kv[1], kv[0]))[0]
```
Using alphabetical order as a tiebreaker is deterministic but arbitrary. Consider adding a comment or using insertion order preservation (Python 3.7+).

#### 7. Test fixture naming inconsistency
**Location:** `tests/test_error_reporting.py`

Some fixtures use `_disable_probe` while others don't. This is fine but could be standardized.

---

## 5. Required Changes Before PASS

1. **Fix critical bug in all production call sites of `should_log_full_tb`:**
   - `agent_cascade/api_router_pkg/router.py:2387` → add `time.time()`
   - `agent_cascade/engine/llm_call.py:1179` → add `time.time()`

2. **Re-run all tests after fix** to confirm no regressions.

---

## 6. Final Verdict

```
VERDICT: FAIL
```

**Reason:** The traceback deduplication will not function as intended in production due to missing `time.time()` arguments. This is a critical bug that undermines the core purpose of the change (bounded logging). The implementation must be corrected before merge.

---

*Review completed by code-review-epf. All claims backed by direct code inspection and test execution.*

---

## REV 2: Fix Verification & Final Verdict

**Date:** 2026-09-15 (post-fix re-check)
**Trigger:** Critical fix round for finding #1

### Changes Applied by Implementation Agent

1. **`error_reporting.py:491-507`** — Method signature updated to `now: Optional[float] = None`. Resolves to `time.time()` **before acquiring the lock** if omitted. Docstring explicitly documents safe-by-default behavior and that tests should pass explicit values.

2. **Production call sites** (`router.py:2387`, `llm_call.py:1179`) — Intentionally omit `now`; the new default handles wall-clock time correctly.

3. **Two new regression tests** in `tests/test_error_reporting.py`:
   - `test_omitted_now_uses_wall_clock` — Verifies dedup works with omitted `now` across a sliding window (would fail with old `now=0.0` default).
   - `test_omitted_now_pruning_uses_wall_clock` — Verifies pruning fires on real wall-clock time when `now` is omitted.

### Verification of Fix #1

✅ **Fix fully resolves the critical bug**

- The method now defaults to `time.time()` when `now` is omitted, so production call sites (which omit it) get correct wall-clock behavior.
- No remaining path where production uses `t=0`.
- All call sites verified: only `router.py` and `llm_call.py` invoke `should_log_full_tb`; both now rely on the safe default.
- Tests confirm the fix: the two new regression tests would **fail** against the old `now=0.0` default, proving they catch the bug.

### Disposition of Other Findings

| Finding | Status | Reason |
|---------|--------|--------|
| #2 (RETRYING message format) | ✅ Resolved | Intentional design; tests pass. |
| #3 (_action_hint classification) | 🟡 Deferred | Minor design nuance; plan accepts trade-off. |
| #4 (Default parameter semantics for `now`) | ✅ **Resolved by Fix #1** | The same fix that changed default from `0.0` to `None`. |
| #5 (Missing time import) | ✅ N/A | No longer relevant; `time.time()` used via new default. |
| #6 (Tiebreaker comment) | 🔵 **Deferred** | Cosmetic nitpick; acceptable to defer to future polish. |
| #7 (Test fixture naming) | 🔵 **Deferred** | Minor inconsistency; doesn't affect correctness. |

### Regression Test Confirmation

- **Targeted run:** `pytest tests/test_error_reporting.py tests/test_sticky_slot_assignment.py tests/test_retry_baseline.py -q` → **78 passed** (was 76, +2 new tests)
- **Full suite:** `pytest tests/ -q` → **2849 passed, 4 skipped, 0 failures**

No regressions introduced. All critical functionality works as intended.

### Final Verdict (REV 2)

```
VERDICT: PASS
```

**Reason:** The critical bug has been fixed. The traceback deduplication now correctly uses wall-clock time by default, ensuring proper sliding-window deduplication and bounded memory growth in production. All tests pass, including new regression tests that verify the fix. No other blocking issues remain.

---

*Final re-check completed by code-review-epf. The implementation is now ready for merge.*
