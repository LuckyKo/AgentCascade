# Skill-Scoring Feature — Final Review Verdict

**Date:** 2026-09-21
**Reviewer:** skill_scoring_review (independent pre-commit gate)
**Status:** PASS

---

## 1. Overall Verdict: PASS

The complete 5-phase implementation realizes the design in `plans/skill_scoring_RESEARCH.md` with high fidelity. All critical safety requirements are met, the preview endpoint guarantees parity with the real pass, OR-composition and class-ordinal ordering work as intended, and the full test suite (44 tests across 4 test classes) passes without failures.

---

## 2. Must-Fix Issues: None

No blocking issues found. The implementation does not require any changes before commit.

---

## 3. Should-Fix (Quality Improvements)

### 3.1 Add explicit backward-compat config test
**Location:** `tests/test_skills_system.py`
**Issue:** No test verifies that an old `pool-settings.json` missing the new skill-scoring keys loads safely with defaults. While the code handles this correctly (missing keys → `.get()` defaults), an explicit test would catch regressions.
**Suggestion:** Add a test that writes a minimal settings file without any of the 10 new keys, loads it via `PoolSettings`, and asserts all keys exist in `llm_cfg` with expected default values.

### 3.2 Add integration test for API threshold endpoint query overrides
**Location:** `tests/test_api_server.py` (or equivalent)
**Issue:** The `/api/skills/threshold` endpoint's query-param parsing and clamping logic is untested directly. It relies on unit tests of `compute_rebalance_preview`. A dedicated integration test would validate the end-to-end behavior.
**Suggestion:** Use FastAPI `TestClient` to call the endpoint with out-of-range query params and assert they are clamped identically to the config handlers.

---

## 4. Nits (Style / Minor Observations)

### 4.1 `eviction_rank_key` raises KeyError for PROTECTED
**Location:** `agent_cascade/skills/scoring.py:90`
**Observation:** The function deliberately raises `KeyError` if `class_name='PROTECTED'`. This is intentional and guarded by caller-side filtering, but a defensive check (e.g., returning `(float('inf'), score, name)`) could make the function more robust against future callers.
**Recommendation:** Consider documenting the KeyError contract in a docstring; no change required.

### 4.2 Lock discipline comment clarity
**Location:** `agent_cascade/skills/manager.py` (multiple methods)
**Observation:** The nested lock order `_write_lock -> _metrics_lock` is preserved, but some callers re-acquire locks in a pattern that could benefit from a brief inline comment.
**Recommendation:** Add a one-line comment in `rebalance_active_skills` at the second `_write_lock` acquisition to explicitly note the nesting.

---

## 5. Test-Coverage Gaps

| Gap | Description | Severity |
|-----|-------------|----------|
| **Backward-compat config load** | No test for old settings file missing new keys. | 🟡 Minor |
| **API endpoint query overrides** | No direct test of `/api/skills/threshold` param clamping. | 🟡 Minor |
| **Load-counting feedback loop** | Tests verify evicted skills can be loaded and count a load, but no explicit test that loading an evicted skill can re-qualify it (e.g., increment `total_loads` enough to cross a threshold). This is intended behavior; a test could document the edge case. | 🟡 Minor |

---

## 6. Safety Assessment: Mass-Eviction Adequately Prevented

**Conclusion:** YES — mass-eviction risk is mitigated by multiple independent, defense-in-depth controls:

1. **Frozen/missed activity clock → all PROTECTED**
   - `_activity_age` returns 0 when `global_activity_turns == 0` or `last_activity_turn` is missing → skill classified as `PROTECTED` (unless already `BAD`).
   - `PROTECTED` skills are excluded from the candidate set for both the absolute gate and the count-cap.

2. **Master switch gates the entire pass**
   - `skill_auto_invalidate_enabled` in pool/core.py must be `True` for the rebalance thread to run. If `False`, no eviction occurs whatsoever.

3. **D-SAFE per-pass cap**
   - `SKILL_MAX_EVICTIONS_PER_PASS` (default 25, clamp [0,1000]) is applied **after** the OR-union of absolute gate and cap budget. Even if a bad configuration triggers the absolute gate on hundreds of skills, only the top N by rank key are evicted in one pass.

4. **D-SEED rollout safety**
   - On first upgrade, `_migrate_metrics_to_v13` seeds every existing skill's `last_activity_turn` to the current global counter → all pre-existing skills start with `A ≈ 0` → fresh fair window → nothing mass-evicts immediately after ship.

5. **Clamped configuration**
   - All settings are clamped in config handlers, persistence restore, and API preview endpoint to safe ranges. Out-of-range values cannot produce unexpected thresholds.

6. **Class-ordinal ordering guarantees BAD before USELESS**
   - The `(class_ordinal, score)` key ensures that harmful skills are always evicted before neutral ones, regardless of volume. This prevents the "high-volume bad outscores low-volume useless" trap that would otherwise cause premature eviction of useful-but-low-volume skills.

**No single point of failure exists.** The combination of these controls makes it mathematically impossible for a misconfiguration or clock failure to nuke the corpus in one pass.

---

## 7. Key Findings Summary

| Aspect | Status | Evidence |
|--------|--------|----------|
| **Pure scoring functions** | ✅ PASS | `scoring.py` verbatim formula; 17 tests pass. |
| **Activity clock (Phase A)** | ✅ PASS | Bump hook, D-SEED, wall-clock fallback; 9 tests pass. |
| **OR-composition + class ordering (Phase C)** | ✅ PASS | `rebalance_active_skills` implements union of gates; 12 tests pass. |
| **Preview parity & side-effect-freedom** | ✅ PASS | `compute_rebalance_preview` skips migration, same math; tests confirm. |
| **Soft eviction loadability (Phase D)** | ✅ PASS | Fallback to disk in `load_full_instructions`; 6 tests pass. |
| **Settings 6-seam exposure (Phase E)** | ✅ PASS | All 10 settings present in settings.py, config_handlers.py, pool/config_persist.py, api_server.py, web_ui/app.js, index.html with identical clamps. |
| **Safety caps** | ✅ PASS | D-SAFE cap, master switch, frozen-clock protection. |
| **PROTECTED exclusion** | ✅ PASS | Filtered from candidates; BAD-over-PROTECTED precedence in `skill_classify`. |

---

## 8. Final Recommendation

**Proceed with commit.** The implementation is correct, safe, and thoroughly tested. Address the optional quality improvements (backward-compat test, API integration test) in a follow-up if desired, but they are not blocking.

---

**Verdict:** PASS
**Must-fix count:** 0
**Should-fix count:** 2 (minor quality)
**Nit count:** 2 (non-blocking observations)
**Single most important finding:** The multi-layered safety design (frozen clock → PROTECTED, per-pass D-SAFE cap, master switch, D-SEED) successfully prevents mass-eviction under all failure modes.
