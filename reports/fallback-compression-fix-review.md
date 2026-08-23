# Independent Review: Fallback Compression Fix

**Date:** 2026-08-23
**Reviewer:** fallback-compression-fix-review (fresh, independent reviewer instance)
**Fix under review:** uncommitted changes to `agent_cascade/api_router_pkg/router.py`,
`agent_cascade/engine/compression_exec.py`, `agent_cascade/engine/llm_call.py`, and new tests in
`tests/test_fallback_compression.py`.
**Investigation reference:** `reports/fallback-compression-misclass-investigation.md`

---

## Verdict: PASS (with minor nits)

The fix correctly addresses the root cause of the silently-disabled fallback compression without
introducing new issues. All seven review concerns were explicitly verified. No blockers.

---

## Findings

### 1. Double-resolution side effect — NO BUG ✅ (Nit, verified safe)
`get_endpoint_chain(instance_name=...)` is **read-only**: it reads `_instance_endpoint_position`
under lock but never mutates it. The pre-check call (`llm_call.py:_pre_llm_checks`) and the real
call (`router.py:call_with_fallback`) both see the same cursor position, because no advancement
occurs until an error is classified (cursor advance at router.py ~1197/1204). The "assigned" limit
is therefore for the same endpoint that is actually called. **No consistency bug.**

### 2. Performance — ACCEPTABLE ✅ (Nit)
Both chain resolutions are read-only dict lookups + list manipulation under a short lock
(microseconds), negligible against LLM latency (seconds). No degradation expected.

### 3. Part 1 completeness — VERIFIED ✅ (Critical, resolved)
`_adjust_config_for_tokens` fully removed; grep confirms zero remaining references in Python code.
All `max_input_tokens` usages are now read/compare-only (gate, pre-check, storage). No reader
depended on the inflated value for capacity filtering/selection — removal is safe.

### 4. Gate correctness post-fix — CORRECT ✅ (Critical, verified)
With true limits restored, `_cfg_limit = llm_cfg.get('max_input_tokens')` at router.py ~1158 now
holds the endpoint's TRUE configured limit. Scenario: payload ~94k, assigned endpoint true limit
90k → `_estimated > _cfg_limit` → `_genuine_overflow=True` → `FallbackCompressionRequired` raised.
The gate logic is sound and will now correctly trigger compression on genuine overflow.

### 5. Edge cases in Part 2 — SAFE ✅ (Major, addressed)
All fall back safely to prior behavior (no crash, no over-compression):
- Empty chain → `_assigned_max_tokens` stays `None`.
- Invalid limit (0/None/not int) → `isinstance(_limit, int) and _limit > 0` fails → stays `None`.
- Exception during resolution → caught, logged at debug → stays `None`.
When `None`, `_check_and_trigger_compression` falls back to `_get_max_tokens(instance)`
(first-priority resolution = prior behavior). Backward compatible.

### 6. Slot invariant preserved — YES ✅ (Critical, confirmed)
Fix touches only `_pre_llm_checks`, `get_endpoint_chain`, and `_check_and_trigger_compression`.
No changes to `agent_invoker.py:217-224` or `compression/core.py:537`. The yield-before-spawn
compressor-slot invariant is fully preserved.

### 7. Test quality — GOOD ✅ (Minor)
New tests exercise real production code paths (not tautologies) and fail pre-fix / pass post-fix:
- `TestNoMaxInputTokensInflation` — a 90k endpoint behind a 165.5k head keeps 90k (red pre-fix).
- `TestGateClassifiesGenuineOverflowOnAssignedEndpoint` — genuine 400 on smaller assigned endpoint
  raises FCR (red pre-fix).
- Plus base.py overflow-guard and engine iterative-compression tests.

Minor gap: no explicit empty-chain / invalid-config test for the Part 2 fallback, though those paths
are covered by existing fallback-path tests. Acceptable.

---

## Required Changes
None. The fix is correct as implemented; ready to commit.

## Verification (performed by orchestrator after review)
- `pytest tests/test_fallback_compression.py tests/test_cursor_rotation_fallback_chain.py
  tests/test_endpoint_no_inheritance.py` → **83 passed, 0 failed** (default addopts, no live-server
  hammering).
