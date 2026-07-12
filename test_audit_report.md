# AgentCascade Regression Test Suite — Audit Report

**Date:** 2025-07-12  
**Scope:** All test files in `tests/` plus `agent_cascade/test_agent.py` and `test_settings.py`  
**Total test files examined:** ~53 | **Individual test functions:** ~270+

---

## Table of Contents
1. [Outdated Tests](#1-outdated-tests)
2. [Redundant / Duplicate Tests](#2-redundant--duplicate-tests)
3. [Hardcoded Paths & Assumptions](#3-hardcoded-paths--assumptions)
4. [Missing Coverage Gaps](#4-missing-coverage-gaps)
5. [Recommendations](#5-recommendations)

---

## 1. Outdated Tests

### 1.1 `test_loop_regression.py` (177 KB — largest test file)
**Location:** `tests/test_loop_regression.py`  
**Issue:** Contains 20 sample text strings copied from debug sessions (~65 lines of raw text each). These are "known false positive" samples used to verify the loop detector doesn't trigger on normal conversation.

- **Overhead:** The file is ~177 KB, with ~90% being hardcoded sample texts rather than test logic.
- **Staleness risk:** Sample texts come from specific debug sessions and may drift as the detector thresholds change. If `min_chars` or `score_threshold` defaults are adjusted in production, these samples might pass for trivial reasons (too short) instead of testing actual false-positive resistance.
- **Recommendation:** Extract sample texts to a JSON file (`tests/loop_samples.json`) and load them dynamically. Add metadata per sample (source session, expected behavior).

### 1.2 `test_inner_loop_detect.py` & siblings — importlib.util loading
**Files affected:**
- `tests/test_inner_loop_detect.py` (~34 KB)
- `tests/test_inner_loop_live_data.py`
- `tests/test_loop_chunk_sizes.py`
- `tests/test_loop_regression.py`
- `tests/test_loop_verify_catch.py`

**Issue:** All 5 files use `importlib.util.spec_from_file_location()` to load modules directly from file paths instead of using standard Python imports. This was likely done to avoid pulling in the entire agent_cascade package or to test against specific file versions.

- The import chain is fragile: they manually set up `sys.modules["agent_cascade.settings"]` before loading.
- If module internal structure changes (e.g., settings moved), these tests break silently.
- **Recommendation:** Switch to standard imports (`from agent_cascade.inner_loop_detect import InnerLoopDetector`). The conftest already ensures the project root is on sys.path via pytest's working directory setup.

### 1.3 `test_oob_fixes.py` & `test_oob_fixes_focused.py` — standalone scripts
**Location:** `tests/test_oob_fixes.py`, `tests/test_oob_fixes_focused.py`  
**Issue:** These are NOT pytest test files — they're standalone Python scripts with a `main()` function and manual print-based assertions. They won't be discovered by `pytest tests/ -v`.

- Both test the same 3 OOB (out-of-bounds) fixes in `file_operations.py`:
  1. read_file start_line beyond EOF check
  2. re_indent dead negative-index code removal  
  3. re_indent bounds checking before clamping
- **Recommendation:** Convert to pytest classes or merge into the existing `test_agent_pool.py` suite (which already tests AgentPool operations).

### 1.4 `test_phase5_polish.py` — issue-number references
**Location:** `tests/test_phase5_polish.py`  
**Issue:** Tests reference "Phase 5" issues (#11, #13, #9) in docstrings. The tested code (`_InstanceConversationMapping`, `_sync_instance_conversations`) still exists and works, but the issue-number references are stale documentation that may confuse future maintainers.

- **Recommendation:** Update docstrings to describe what's being tested rather than referencing closed issues.

---

## 2. Redundant / Duplicate Tests

### 2.1 `test_oob_fixes.py` vs `test_oob_fixes_focused.py`
**Overlap:** Both test the exact same 3 OOB fixes with nearly identical MockOpMgr classes and test cases. The "focused" version has slightly fewer tests (F3.x, F1.x, F2.x naming) but covers the same ground.

- **test_oob_fixes.py:** 19 test checks (T1-T7, T9-T19)
- **test_oob_fixes_focused.py:** 14 test checks (F3.1-F3.5, F1.1-F1.3, F2.1-F2.5 + regression)

**Recommendation:** Keep only `test_oob_fixes.py` (more comprehensive). Delete or merge `test_oob_fixes_focused.py`.

### 2.2 Compression test overlap across 4 files
| File | Size | Focus | Overlap with Others |
|------|------|-------|-------------------|
| `test_compression.py` | 55 KB, ~60 tests | Full compression flow (helpers + core) | Core reference implementation |
| `test_compression_consistency.py` | 67 KB, ~32 tests | Pool/JSONL state consistency after compression | Re-implements MockInstance class |
| `test_compression_boundary_fix.py` | 26 KB, ~40 tests | Boundary logic for tool-call pairs | Tests same helpers (`_refine_tool_call_boundary`, `compute_discard_count`) as test_compression.py — uses dict-based messages (no MockInstance) |
| `test_chain_vs_pairs.py` | — | Chain vs independent pair detection | Tests `_refine_tool_call_boundary` too — uses dict-based messages (no MockInstance) |

**Specific overlaps:**
- **Fraction tests:** `test_compression.py` has 8 fraction tests (normal, zero, one, small set, force mode). `test_chain_vs_pairs.py` has its own `test_fraction_half`, `test_force_mode_independent_pairs`. These could share fixtures.
- **Empty active set:** Tested in both `test_compression.py` and `test_chain_vs_pairs.py` and `test_compression_boundary_fix.py` (3 times total).
- **MockInstance duplication:** Both `test_compression.py` (line 36) and `test_compression_consistency.py` (line 40) define their own MockInstance class with `_FakeLock`, `rebuild_conversation()`, etc. This should be a shared fixture in conftest.py. The other two compression test files use simple dict-based messages which is fine but inconsistent.

**Recommendation:** Consolidate compression tests into 2 files:
1. `test_compression_core.py` — unit tests for helpers and core flow (merge test_compression.py + boundary_fix)
2. `test_compression_integration.py` — pool/JSONL consistency end-to-end tests (keep test_compression_consistency.py as-is)

### 2.3 Loop detection test duplication
| File | Tests | Focus |
|------|-------|-------|
| `test_inner_loop_detect.py` | ~45 tests | Full detector unit tests (12 scenarios) |
| `test_inner_loop_live_data.py` | ~8 tests | Live data samples from debug sessions |
| `test_loop_chunk_sizes.py` | — | Chunk size sensitivity |
| `test_loop_regression.py` | 1 test class, 20 params | False positive regression on known samples |
| `test_loop_verify_catch.py` | — | Verify detection still catches real loops |

**Recommendation:** These are well-structured but could share the detector factory (`make_detector`) and sample text loader. Consider a shared conftest fixture for loop detectors.

---

## 3. Hardcoded Paths & Assumptions

### 3.1 Absolute path in OOB fix tests
```python
# test_oob_fixes.py line 8:
sys.path.insert(0, r"N:\work\WD\AgentCascade_unified")

# test_oob_fixes_focused.py line 6:
sys.path.insert(0, r"N:\work\WD\AgentCascade_unified")
```
**Issue:** Hardcoded Windows paths. These tests won't work on other machines or in CI.  
**Fix:** Use `Path(__file__).resolve().parent.parent` like most other test files do.

### 3.2 Additional hardcoded paths in loop detection tests
| File | Line(s) | Hardcoded Path | Impact |
|------|---------|---------------|--------|
| `test_loop_chunk_sizes.py` | 11, 24 | `CASCADE_ROOT = Path(r"N:\work\WD\AgentCascade_unified")` + sample file path | Falls back to hardcoded path if relative resolution fails; loads samples from dated JSONL file (`samples_2026-07-07.jsonl`) |
| `test_loop_verify_catch.py` | 20 | `SAMPLE_FILE = Path(r"N:\work\WD\AgentWorkspace\logs\loop_samples\samples_2026-07-07.jsonl")` | Loads samples from a different directory than the project root — will fail if logs folder doesn't exist |
| `test_grep_usability.py` | 5 | `sys.path.insert(0, r"N:\work\WD\AgentCascade")` | Wrong path (missing `_unified` suffix) — may silently pass with stale code |

**Issue:** These paths will break tests on other machines or in CI. The dated sample file (`samples_2026-07-07.jsonl`) is also a concern — if the detector thresholds change, old samples might not be representative.  
**Fix:** Standardize test data loading using relative paths from project root. Move sample JSONL files into `tests/loop_samples/` directory.

### 3.3 Private API imports in code interpreter tests
**File:** `tests/test_code_interpreter_extra_mounts.py` (~16 tests)  
**Issue:** Imports `_KERNEL_ACTIVITY` (module-level private dict) directly:
```python
from agent_cascade.tools.code_interpreter import _KERNEL_ACTIVITY
```
This is fragile — if the internal name changes, 7 test functions break. The tests themselves are valuable but depend on implementation details.

### 3.4 `test_session_metadata_fix.py` — no pytest class structure
**Issue:** Tests are standalone functions with a manual `if __name__ == "__main__"` runner at the bottom. While they work as pytest tests, the duplicate runner at the bottom is redundant and could silently hide failures if run via `python test_session_metadata_fix.py`.

### 3.5 Conftest fixture duplication
The shared fixtures in `conftest.py` (`_FakeInstance`, `_FakeCachePool`, `_FakeAgentPool`) are well-designed but several test files define their own MockInstance/MockAgentPool classes instead of using the conftest versions:
- `test_compression.py`: Own `MockInstance`, `MockAgentPool`
- `test_compression_consistency.py`: Own `MockInstance`
- `test_streaming_tool_resolution.py`: Uses `_FakeInstance` from conftest ✅ (good example)

---

## 4. Missing Coverage Gaps

### 4.1 Execution engine core flow
The execution_engine.py is ~206 KB but has no dedicated test file (`tests/test_execution_engine.py`). The engine's main loop, tool dispatching, and error recovery are only indirectly tested through:
- `test_unified_system.py` (import chain + basic initialization)
- `test_agent_orchestrator_state.py` (state building)

**Gap:** No tests for:
- Message streaming with concurrent modifications
- Tool resolution with `{USE_CACHED_ENTRY_N}` placeholders (only tested in isolation via `test_streaming_tool_resolution.py`)
- Retry logic and backoff handling
- Session save/load roundtrip

### 4.2 Agent factory / agent loading
**Gap:** No tests for:
- Dynamic agent class discovery from config
- Agent prompt template rendering with variable substitution  
- Multi-agent chain orchestration (beyond what `test_unified_system.py` covers)

### 4.3 Settings validation
**Gap:** The settings module (`agent_cascade/settings.py`) has no dedicated tests. Environment variable parsing, default value fallbacks, and config file loading are untested.

### 4.4 Tool dispatcher edge cases
**Gap:** `tool_dispatcher.py` is not directly tested. Tool resolution order, tool conflict handling, and tool argument validation are only indirectly covered.

---

## 5. Recommendations

### Priority 1 — Quick Wins (Low Effort, High Impact)
| # | Action | Files Affected |
|---|--------|---------------|
| 1 | Fix hardcoded paths in OOB tests | `test_oob_fixes.py`, `test_oob_fixes_focused.py` |
| 2 | Fix hardcoded paths in loop detection tests | `test_loop_chunk_sizes.py`, `test_loop_verify_catch.py`, `test_grep_usability.py` |
| 3 | Delete or merge duplicate OOB fix test | Remove `test_oob_fixes_focused.py` |
| 4 | Convert standalone scripts to pytest classes | `test_session_metadata_fix.py`, `test_phase5_polish.py` |
| 5 | Share MockInstance fixture across compression tests via conftest | `conftest.py` + all compression test files |

### Priority 2 — Consolidation (Medium Effort)
| # | Action | Files Affected |
|---|--------|---------------|
| 6 | Extract loop regression sample texts to JSON file | `test_loop_regression.py` → new `tests/loop_samples.json` |
| 7 | Switch importlib.util imports to standard imports in loop tests | 5 test files |
| 8 | Consolidate compression boundary tests into main compression suite | Merge `test_compression_boundary_fix.py` + `test_chain_vs_pairs.py` into `test_compression.py` |

### Priority 3 — Coverage Gaps (Higher Effort)
| # | Action | New Files Needed |
|---|--------|-----------------|
| 9 | Add execution engine core tests | `tests/test_execution_engine.py` |
|10| Add settings validation tests | `tests/test_settings_validation.py` |
|11| Add tool dispatcher edge case tests | `tests/tools/test_tool_dispatcher.py` |

### Priority 4 — Test Infrastructure
| # | Action | Benefit |
|---|--------|---------|
|12| Add pytest markers for test categories (`@pytest.mark.fast`, `@pytest.mark.slow`, `@pytest.mark.integration`) | Faster CI runs with selective execution |
|13| Add test coverage reporting (coverage.py) | Identify untested code paths systematically |

---

## Summary Statistics

| Category | Count | Details |
|----------|-------|---------|
| Total test files | ~53 | Including subdirectories |
| Test functions (`def test_`) | ~270+ | Across all files |
| Outdated tests found | 4 groups | Loop regression, importlib loading, OOB scripts, issue refs |
| Redundant tests found | 3 pairs | OOB duplicates, compression overlaps, empty set triple-test |
| Hardcoded paths | 5 files | OOB fixes + loop detection + grep usability |
| Missing coverage areas | 3 modules | Execution engine, settings, tool dispatcher |

---

*Report generated by TestAudit. All file paths relative to `N:\work\WD\AgentCascade_unified`.*