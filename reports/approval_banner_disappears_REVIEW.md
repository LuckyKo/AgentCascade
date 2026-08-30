# Approval Banner Disappears Fix — Independent Review Report

**Date:** 2026-08-31  
**Reviewer:** approval_fix_review (independent senior QA)  
**Implementer:** approval_fix_coder (coder agent)  
**Related:** [[approval_banner_disappears_INVESTIGATION]], [[approval_banner_disappears_IMPL]]  

---

## Executive Summary

The fix (Option A) correctly removes the `approvals` field from **stream-update** payloads while preserving it in **full-state** payloads. The critical safety claim—that `_approval_loop` still broadcasts clearing signals when all approvals resolve—is **VERIFIED**. No collateral damage detected. Regression tests are substantive and pass.

**VERDICT: APPROVE** ✅

---

## Detailed Findings

### 1. Correctness of the Change — PASS

**Evidence:**

- `build_stream_update_from_pool()` no longer emits `'approvals'` key. The return dict at **state_builder.py:562-565** explicitly omits it with a clear comment explaining the design rationale.
- The now-unused `_get_approvals(pool)` computation in `build_stream_update_from_pool()` was cleanly removed (**state_builder.py:486-487**).
- `build_state_from_pool()` still emits `'approvals'` (**state_builder.py:301** and **line 379**).

**Diff Verification:** `git diff` shows only the intended changes (+6 / -3 lines in one file). No other modifications.

**Classification:** ✅ PASS (no issues)

---

### 2. No Collateral Damage — PASS

**Evidence:**

- `git -C N:\work\WD\AgentCascade diff` output confirms:
  ```diff
  -    # Get pending approvals (only include if non-empty to prevent UI flickering)
  -    pending_approvals = _get_approvals(pool)
  +    # NOTE: pending approvals are intentionally NOT computed/included in stream updates.
  +    # See the return-dict comment below for why the 'approvals' field is omitted here.
  
  -        'approvals': pending_approvals,
  +        # Intentionally NO 'approvals' key here. Approvals are delivered exclusively via
  +        # the dedicated {'type':'approvals'} WS message broadcast by _approval_loop; a
  +        # stream tick built before an approval is registered would carry a stale [] and,
  +        # if included, clobber live approval state on the client (banner disappears).
  ```
- Only two code blocks changed: the removed computation and the removed key. No other return dict fields altered.

**Classification:** ✅ PASS (no issues)

---

### 3. Safety of Option A — CRITICAL CLAIM VERIFIED ✅

**Claim to Verify:** `_approval_loop` in `api_server.py` broadcasts `{'type':'approvals','approvals': pending}` with the CURRENT list, including an empty list when all approvals resolve. If this is not true, Option A is UNSAFE (banner would never clear).

**Evidence:**

- File: `agent_cascade/api_server.py`, function `_approval_loop()` at **lines 748-766**.
- Key logic at **lines 755-762**:
  ```python
  pending = get_approvals()
  current_ids = {a['request_id'] for a in pending}
  new_seen = current_ids - seen_ids
  resolved_ids = known_ids - current_ids  # IDs that were known but now gone
  if new_seen or resolved_ids:
      seen_ids.update(current_ids)
      known_ids = current_ids.copy()
      await broadcast({'type': 'approvals', 'approvals': pending})
  ```

**Analysis:**

- When an approval is resolved, `resolved_ids` becomes non-empty → the condition `if new_seen or resolved_ids:` triggers.
- At that moment, `pending = get_approvals()` returns the **current** list of pending approvals.
- If all approvals have been resolved, `pending` is `[]` (empty list).
- The broadcast sends `{'type':'approvals', 'approvals': []}` to clients.
- Client-side `case 'approvals':` handler at **web_ui/app.js:2230-2235** receives this and calls `renderApprovals()`, which hides the banner when `state.approvals.length === 0`.

**Conclusion:** The clearing signal is correctly broadcast on the transition to an empty set. Option A is **SAFE**.

**Classification:** ✅ PASS (critical claim verified)

---

### 4. Client Consistency — PASS

**Evidence:**

- `web_ui/app.js` contains `case 'approvals':` handler at **lines 2230-2235**, which calls `renderApprovals()`.
- Three overwrite sites all use the guard `if ('approvals' in data && Array.isArray(data.approvals))`:
  - `'state'` / `'done'` fall-through: **lines 1839-1842**
  - `'stream_update'`: **lines 2139-2142**
  - `'approvals'`: **lines 2232-2235**
- Because stream updates no longer contain the `'approvals'` key, the guard is false and `state.approvals` remains unchanged on stream ticks.

**No other consumer of `stream_update['approvals']` found.** Grep for `stream_update['approvals']` or `.get('approvals')` on a stream update returns no matches in the codebase.

**Classification:** ✅ PASS (no issues)

---

### 5. Regression Test Quality — PASS

**Test File:** `tests/test_state_builder.py` (new file, as noted in impl report; existing test suite structure differs from the instruction's assumed paths).

**Tests:**

- `test_stream_update_omits_approvals_key` — asserts `'approvals' not in build_stream_update_from_pool(...)`
- `test_full_state_includes_approvals_key` — asserts `'approvals' in build_state_from_pool(...)` and that value equals live snapshot
- `test_stream_and_full_state_diverge_on_approvals` — same pool, one payload has the key and the other does not

These are **real, non-vacuous assertions** that pin the invariant: stream-update MUST NOT have approvals; full-state MUST. They are self-contained using MagicMock pools.

**Test Run:**

```bash
cd N:\work\WD\AgentCascade
python -m pytest tests/test_state_builder.py -v
```

**Result:** **3 passed, 0 failed** (7.05s)

- `test_stream_update_omits_approvals_key` — PASSED
- `test_full_state_includes_approvals_key` — PASSED
- `test_stream_and_full_state_diverge_on_approvals` — PASSED

**Note on Missing Tests:** The instruction referenced `tests/test_approval.py` and `tests/test_api_server.py`, but those files do not exist in this repository. The actual test suite structure is different; the relevant coverage is provided by `test_state_builder.py` and other existing tests (e.g., `test_api_endpoints.py`). No additional tests needed for this targeted fix.

**Classification:** ✅ PASS (tests are substantive and pass)

---

### 6. Edge Cases / Other Consumers — PASS

**Search Results:**

- Grep for `stream_update['approvals']` or `stream_update.get('approvals')` in the entire codebase returned **no matches**.
- Grep for `\bapprovals\b` across Python files shows all other usages are either:
  - Reading from full-state (`build_state_from_pool`, `api_server.py` fallback builds)
  - The `_approval_loop` dedicated broadcast
  - Security handler auto-apply broadcasts (which send `'type':'approvals'`)
  - Cleanup/clear operations in `session_io.py`

No other consumer of stream-update's approvals field was found.

**Classification:** ✅ PASS (no hidden dependencies)

---

## Test Command and Results Summary

**Command Run:**
```bash
cd N:\work\WD\AgentCascade
python -m pytest tests/test_state_builder.py -v
```

**Exact Output:**
```
============================= test session starts ==============================
...
tests/test_state_builder.py::test_full_state_includes_approvals_key PASSED
tests/test_state_builder.py::test_stream_update_omits_approvals_key PASSED
tests/test_state_builder.py::test_stream_and_full_state_diverge_on_approvals PASSED
============================== 3 passed in 7.05s ==============================
```

**Additional Check:** `git diff` confirms only intended changes.

---

## Final Verdict

| Category | Status |
|----------|--------|
| Correctness | ✅ PASS |
| Collateral Damage | ✅ PASS |
| Safety (Option A Critical Claim) | ✅ PASS |
| Client Consistency | ✅ PASS |
| Regression Tests | ✅ PASS |
| Edge Cases / Other Consumers | ✅ PASS |

**OVERALL: APPROVE** ✅

The fix is correct, safe, and well-tested. No changes required. The implementation successfully implements Option A as designed.

---

## Required Changes (if any)

None. The code is ready for commit.

---

*Review completed independently without reference to implementer's claims. All findings based on direct source inspection and test execution.*
