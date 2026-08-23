# Regression Risk Review: 2026-08-23 Commits

**Scope:** 4 commits affecting production code in `agent_cascade/api_router_pkg/router.py`, `agent_cascade/engine/compression_exec.py`, `agent_cascade/engine/llm_call.py`, `web_ui/app.js`
**Reviewer:** regression-review-0823 (reviewer agent)
**Date:** 2026-08-23

## FINDINGS

### 1. Removal of `_adjust_config_for_tokens` — NO REMAINING REFERENCES ✅ [INFO]
- `grep -r "_adjust_config_for_tokens"` returns zero matches in Python code (only historical .md reports).
- No remaining readers of the inflated `max_input_tokens`. **PASS.**

### 2. API compatibility: `allocated_tokens` parameter 🟠 [MINOR — non-blocking]
- `get_endpoint_chain()` and `call_with_fallback()` still accept `allocated_tokens`; docstrings state it no longer inflates any cfg's `max_input_tokens`.
- All internal call sites use keyword arguments (verified by repo-wide grep; e.g. router.py:943-944). No positional usage found → no breakage.
- Recommendation: add a deprecation warning if `allocated_tokens` is non-None.

### 3. `_pre_llm_checks` best-effort limit resolution ✅ [INFO]
- llm_call.py:113-121: wrapped in try/except, degrades to None (old behavior) on any failure. ✅
- Thread safety: `get_assigned_max_tokens` → `get_endpoint_chain` acquires the router's own lock; caller holds no lock. Safe. ✅
- Edge cases: empty chain and missing key handled gracefully (returns None). ✅

### 4. `_check_and_trigger_compression` signature ✅ [INFO]
- compression_exec.py:54-60: `assigned_max_tokens=None` default preserved; only call site (llm_call.py:123) passes it explicitly. No signature-order breakage. ✅

### 5. Router breaker changes (824c4e3) ✅ [INFO]
- Constants in settings.py:179-186 (`BREAKER_BASE_WINDOW_SECONDS`, `BREAKER_MAX_WINDOW_SECONDS`, `BREAKER_WINDOW_GROWTH`, `SERVER_BUSY_WAIT_CAP_SECONDS`), imported at router.py:24-31. Consistent. ✅
- Single-probe recovery fix implemented via `_breaker_claim_probe()` / `_breaker_should_skip()`; covered by tests/test_router_cascade_breaker.py (exactly-one-winner concurrency, probe success closes breaker, probe failure grows window, guard released on failure). State-machine semantics unchanged beyond the intended fix. ✅

### 6. Interaction between compression and router changes ✅ [INFO]
- Context-exceeded gate (~router.py:1153-1178) now reads TRUE configured `max_input_tokens` (inflation removed), consistent with `assigned_max_tokens` sizing in compression_exec.py. No compounding risk identified.

### 7. Uncommitted `tests/conftest.py` change ✅ [INFO]
- Adds `_local_tests_opted_in()` + `AGENT_CASCADE_RUN_LOCAL_TESTS` opt-in gate for live local-LLM tests (production-safety: the "local" server may be the production single-GPU box). Syntactically sound; does not alter non-local test behavior. Live tests correctly skipped in today's full run ("Local LLM found (LM Studio) — live tests SKIPPED").

### 8. web_ui/app.js fix ✅ [INFO]
- Line ~3623: `logger.debug(...)` → `console.debug(...)`. Fixes ReferenceError on undefined `logger`. Trivial, correct.

## TEST EVIDENCE
- New coverage in tests/test_fallback_compression.py (~275 lines added): verifies no inflation of max_input_tokens, gate classifies genuine overflow on the assigned endpoint, and pre-send compression is endpoint-truthful.
- tests/test_router_cascade_breaker_stress.py (new) + test_router_cascade_breaker.py cover breaker concurrency.

## NON-BLOCKER RECOMMENDATIONS
1. Add deprecation warning for `allocated_tokens` in `get_endpoint_chain()` / `call_with_fallback()`.
2. Add a thread-safety test for concurrent `get_assigned_max_tokens` calls.
3. Document the new `assigned_max_tokens` parameter of `_check_and_trigger_compression`.

## VERDICT: PASS
No BLOCKER or MAJOR issues. The four commits are coherent and introduce no secondary regressions in production code. Full regression suite run separately confirmed: **1789 passed, 1 skipped** (live local-LLM test, opt-in gate).