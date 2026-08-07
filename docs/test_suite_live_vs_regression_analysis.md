# AgentCascade Test Suite Analysis: Live API Tests vs Regression Tests

**Date:** 2026-08-05
**Analyst:** test_analyst
**Repo:** N:\work\WD\AgentCascade
**Purpose:** Identify which tests hit live APIs/network vs. mocked/unit tests, to support separating live endpoint tests from basic regression tests.

---

## Executive Summary

The AgentCascade test suite has **3 distinct categories** of tests:

1. **True regression/unit tests (mocked, offline)** — the majority (~70 files). These use mocks, in-process FastAPI TestClient, synthetic data, or stubbed LLM classes. They run fine in CI without any server.
2. **Live local-LLM integration tests (network-dependent)** — concentrated in `tests/llm/`, `tests/agents/`, `tests/examples/`, `tests/memory/`, `tests/tools/`. These call a real local LLM server (LM Studio/Ollama/llama.cpp) and are **intended** to be guarded by the `skip_if_no_local` marker + `local_llm_cfg` fixtures.
3. **Live external-service/API tests** — `tests/tools/test_tools.py` (AmapWeather, WebSearch/Serper, ImageGen, Retrieval), `tests/examples/test_examples.py` (external image URLs, DashScope), and script-style files (`test_llm.py`, `test_llm_local.py`, `test_state_restore_comparison.py`, `test_token_estimation.py`) that hit real localhost servers.

**⚠️ Critical finding:** The `skip_if_no_local` marker and `local_llm_cfg`/`local_vl_llm_cfg` fixtures **were deleted** from `tests/conftest.py` in commit `98a88eb` (2026-07-27, "isolate test config"). ~40 tests still reference them. These tests currently **fail with `fixture not found`** (not skip) when collected. The pytest.ini marker registration for `extra_examples`/`extra_tools`/`extra_vl` still works, but `skip_if_no_local` has no auto-skip hook anymore.

---

## 1. Test File Inventory (complete)

### 1.1 Root `tests/` directory (60 test files)

| File | Category | Network? |
|---|---|---|
| test_agent_orchestrator_state.py | Unit (mocked AgentPool) | No |
| test_agent_pool.py | Integration (mocked deps, no LLM) | No |
| **test_api_endpoints.py** | **In-process API tests** (FastAPI TestClient) | **No** (in-process, despite name) |
| test_async_result_handling.py | Unit (mocked router) | No |
| test_async_shell_cmd.py | Unit (mocked AsyncShellTracker) | No |
| test_async_shell_failure_scenarios.py | Unit | No |
| test_call_agent_sync_async_selection.py | Unit (mocked APIRouter) | No |
| test_chain_vs_pairs.py | Unit (compression logic) | No |
| test_code_interpreter_extra_mounts.py | Unit (mocked subprocess/docker) | No |
| test_compression.py | Unit (MockAgentPool) | No |
| test_compression_boundary_fix.py | Unit | No |
| test_compression_consistency.py | Unit | No |
| test_compression_no_duplication.py | Integration (real pool, no LLM) | No |
| test_compression_tool_pairs.py | Unit (mock invoke) | No |
| test_concurrency_dispatch.py | Unit | No |
| test_cursor_rotation_fallback_chain.py | Unit (MockLLM) | No |
| test_embedded_tool_call_detection.py | Unit | No |
| test_endpoint_scheduler_stress.py | Unit (mock API bases) | No |
| test_extract_text_function_call.py | Unit | No |
| test_generator_finalization.py | Unit (mock API bases) | No |
| test_grep_compare.py | Functional (subprocess grep/find on repo) | No (local FS) |
| test_grep_usability.py | Unit (greptool) | No |
| test_greptool.py | Functional (subprocess grep) | No (local FS) |
| test_heuristic_comment_fix.py | Unit | No |
| test_inner_loop_detect.py | Unit (synthetic text) | No |
| test_inner_loop_fp_simulation.py | Simulated (log-based, skipif) | No |
| **test_inner_loop_live_data.py** | **Live-data (reads real JSONL logs)** | **No network; disk-dependent** |
| test_inner_loop_regression.py | Unit (synthetic) | No |
| test_instance_separation.py | Unit | No |
| test_json_robustness.py | Unit | No |
| **test_llm.py** | **Live script (requests to localhost:5000)** | **YES — live localhost** |
| **test_llm_local.py** | **Live script (requests to localhost:5000)** | **YES — live localhost** |
| test_loop_chunk_sizes.py | Unit | No |
| test_loop_detection.py | Unit | No |
| test_loop_regression.py | Unit | No |
| test_loop_verify_catch.py | Unit | No |
| test_max_tokens_resolution.py | Unit (LLM stub) | No |
| test_nested_agent_calls.py | Unit (mocked) | No |
| test_oob_fixes.py | Unit | No |
| test_phase5_polish.py | Unit | No |
| test_rate_limiting_concurrency.py | Unit | No |
| test_read_file_dispatcher_perf.py | Perf (local FS) | No |
| test_read_file_perf.py | Perf (local FS) | No |
| test_reset_history_rewrite.py | Unit | No |
| test_retry_baseline.py | Unit (MockLLM, fake API bases) | No |
| test_retry_policy.py | Unit (MockLLM) | No |
| test_safe_shell_cmd.py | Unit (safety parser) | No |
| test_security_parser.py | Unit | No |
| test_session_load_dup_fix.py | Unit | No |
| test_session_load_regression.py | Unit | No |
| test_session_metadata_fix.py | Unit | No |
| test_shell_cmd_cwd_resolution.py | Unit | No |
| test_skill_generation.py | Unit (skill matcher) | No |
| test_skills_system.py | Unit (skill parser) | No |
| test_soul_loader.py | Unit | No |
| **test_state_restore_comparison.py** | **Live llama.cpp API test** | **YES — http://127.0.0.1:1234** |
| test_state_restore_comparison_results.json | Data file | — |
| test_streaming_tool_resolution.py | Unit | No |
| test_token_cache.py | Unit | No |
| **test_token_estimation.py** | **Hybrid: unit + optional live llama.cpp call** | **Partial — tries 127.0.0.1:1234, falls back** |
| test_token_overhead_fix.py | Unit | No |
| test_tool_chain_boundary.py | Unit | No |
| test_tool_utils.py | Unit | No |
| test_two_phase_loop_detect.py | Unit | No |
| test_unified_system.py | Integration (mocked LLM/API) | No |

### 1.2 `tests/agents/` (7 files — ALL live local-LLM integration)

| File | Category | Network? |
|---|---|---|
| test_article_agent.py | **Disabled** (`@pytest.mark.skip()`) | — |
| test_assistant.py | Live LLM (`local_llm_cfg`; 1× `extra_vl`) | **YES** (local LLM + external file URL) |
| test_custom_tool_object.py | Live LLM (`local_llm_cfg`) | **YES** (local LLM) |
| test_doc_qa.py | Live LLM (`local_llm_cfg`) | **YES** |
| test_parallel_qa.py | Live LLM (`local_llm_cfg`) | **YES** |
| test_react_chat.py | Live LLM (`local_llm_cfg`, `amap_weather`, `image_gen`, `code_interpreter`) | **YES** |
| test_router.py | Live LLM (`local_llm_cfg` + `local_vl_llm_cfg`, `extra_vl`) | **YES** |

### 1.3 `tests/llm/` (6 files — ALL live local-LLM integration)

| File | Category | Network? |
|---|---|---|
| test_continue.py | Live LLM (`skip_if_no_local`) | **YES** |
| test_dashscope.py | Live LLM (`skip_if_no_local`, 1× `extra_vl`) | **YES** |
| test_function_content.py | Live LLM (`skip_if_no_local`) | **YES** |
| test_local_llm.py | Live LLM (`skip_if_no_local`, 1× `extra_vl`) | **YES** |
| test_max_input_tokens_not_popped.py | **Unit** (`_DummyLLM` — no network) | **No** |
| test_oai.py | Live LLM (`skip_if_no_local`) | **YES** |

### 1.4 `tests/examples/` (3 files — ALL live, marked `extra_examples`)

| File | Category | Network? |
|---|---|---|
| test_examples.py | Live (local LLM + DashScope URLs + weather/tools; `extra_examples`, `extra_vl`) | **YES** |
| test_long_dialogue.py | Live (`extra_examples`, `skip_if_no_local`) | **YES** |
| test_vm_qa.py | Live (`extra_examples`, `skip_if_no_local`) | **YES** |

### 1.5 `tests/memory/`, `tests/orchestrator/`, `tests/qwen_server/`, `tests/tools/`

| File | Category | Network? |
|---|---|---|
| memory/test_memory.py | Live LLM (`skip_if_no_local`, `local_llm_cfg`) | **YES** |
| orchestrator/test_double_compression.py | Unit (MagicMock) | No |
| qwen_server/test_database_server.py | Integration (local filesystem/db; URL only used as hash input) | No (no fetch) |
| tools/test_doc_parser.py | Unit | No |
| tools/test_edit_file_modes.py | Unit | No |
| tools/test_hybrid_search.py | Unit | No |
| tools/test_issue_repro.py | Unit | No |
| tools/test_keyword_search.py | Unit | No |
| tools/test_re_indent_all_modes.py | Unit | No |
| tools/test_simple_doc_parser.py | Unit | No |
| tools/test_tools.py | **Hybrid: 2 mocked web_search tests + live external tests** (`extra_tools`: AmapWeather, WebSearch/Serper, ImageGen, Retrieval w/ external PDF URL) | **Partial — 4 live, 3 mocked** |
| tools/test_vector_search.py | Live (`extra_tools`, `skip_if_no_local`, local embeddings) | **YES** (local embeddings) |

### 1.6 Non-test helper files in `tests/`
- `conftest.py` — session isolation fixture + MockAgentPool (NO local_llm fixtures — see Critical Finding)
- `loop_test_utils.py` — synthetic loop-pattern helpers (offline)
- `loop_samples.json` — test data
- `api_test_output.txt`, `retry_*_output*.txt`, `timeout_lock_test_results.md` — captured run outputs (from 2026-07-28; evidence of prior runs)
- `_fix_syntax.py` — utility

---

## 2. LIVE API / NETWORK-DEPENDENT TESTS (the ones to separate)

### 2.1 Live local-LLM server tests (need running LM Studio / Ollama / llama.cpp)

These all depend on the **missing** `local_llm_cfg`/`skip_if_no_local` mechanism — they are the core "live" group:

- `tests/llm/test_local_llm.py` — `test_local_llm_basic`, `test_local_llm_streaming`, `test_local_vl_llm_basic` (extra_vl), `test_models_available`, `test_retry_cfg`
- `tests/llm/test_oai.py` — `test_llm_oai` (8 combos), `test_llm_oai_basic`, `test_llm_oai_streaming`
- `tests/llm/test_dashscope.py` — `test_llm_dashscope`, VL tests, retry tests (`extra_vl` on VL ones)
- `tests/llm/test_continue.py` — `test_continue` (stream/delta combos)
- `tests/llm/test_function_content.py` — `test_function_content` (cfg 0/1 × gen_cfg variants)
- `tests/agents/test_assistant.py` — 4 tests (`test_assistant_system_and_tool`, `test_assistant_files`, `test_assistant_empty_query`, `test_assistant_vl` [extra_vl])
- `tests/agents/test_react_chat.py` — `test_react_chat`, `test_react_chat_with_file`
- `tests/agents/test_doc_qa.py`, `test_parallel_qa.py`, `test_custom_tool_object.py`, `test_router.py` (extra_vl)
- `tests/memory/test_memory.py` — `test_memory`
- `tests/examples/test_examples.py` — **all 15 tests** (`extra_examples`, several `extra_vl`)
- `tests/examples/test_long_dialogue.py`, `test_vm_qa.py` — `extra_examples`
- `tests/tools/test_vector_search.py` — `extra_tools` (local embeddings server)
- `tests/tools/test_tools.py::test_image_gen` — `extra_tools` + `local_llm_cfg`

### 2.2 Live external-service tests (marked `extra_tools` / `extra_examples`)

- `tests/tools/test_tools.py::test_amap_weather` — Amap Weather API (AMAP_TOKEN), `extra_tools`
- `tests/tools/test_tools.py::test_web_search` — Serper API (SERPER_API_KEY), `extra_tools`
- `tests/tools/test_tools.py::test_retrieval` — **no marker**; fetches `https://qianwen-res.oss-cn-beijing.aliyuncs.com/QWEN_TECHNICAL_REPORT.pdf` ⚠️ **unmarked live test**
- `tests/tools/test_tools.py::test_image_gen` — local LLM + image gen, `extra_tools`
- `tests/examples/test_examples.py` — external images `https://dashscope.oss-cn-beijing.aliyuncs.com/...` (all `extra_examples`)

### 2.3 Live script-style tests (NO markers, NO fixtures — will always run)

- `tests/test_llm.py` — bare script: `requests.post("http://localhost:5000/v1/chat/completions")`, no test function, prints output. **Not a pytest test** (no `test_` function).
- `tests/test_llm_local.py` — same pattern, localhost:5000.
- `tests/test_state_restore_comparison.py` — **hits `http://127.0.0.1:1234`** (llama.cpp autoloader API): model load/unload, slot save/restore, chat completions. Run via `python tests/test_state_restore_comparison.py` (has `main()`; functions named `test_*` are *helpers*, not pytest-collected in normal runs — but pytest WILL collect them as tests if run with pytest!).
- `tests/test_token_estimation.py` — hybrid: pure unit comparisons + **optional** live call to `http://127.0.0.1:1234/v1` with graceful fallback to templated count (no assert on the live result — prints only).

### 2.4 Live-data (disk) tests — not network, but environment-dependent

- `tests/test_inner_loop_live_data.py` — reads real agent JSONL logs from `N:\work\WD\AgentWorkspace\logs` (or `/workspace/logs`). `skipif(LOG_DIR is None)`. Requires ≥1000 assistant messages to run the FP-rate assertion. **Will FAIL if log dir exists but has <1000 messages.**

### 2.5 Tests with names that SOUND live but are NOT

- **`tests/test_api_endpoints.py`** — despite the name, it's **in-process** FastAPI `TestClient`, no network ("Tests are fast... in-process, no network" — file docstring line 4). This is a **regression** test, not a live API test. (Historical note: `api_test_output.txt` from 2026-07-28 shows it previously crashed workers under xdist, but that's a resource issue, not network.)

---

## 3. Marker/Fixture Infrastructure Analysis

### pytest.ini (current)
```ini
addopts = -n auto --timeout=60 --durations=10 -m "not extra_examples and not extra_tools and not extra_vl"
markers =
    skip_if_no_local: skip when no local LLM server is available
    extra_examples: example integration tests (DashScope models, weather APIs, external services)
    extra_tools: tool tests requiring external APIs (SERPER_API_KEY, langchain, image_gen, amap_weather)
    extra_vl: vision-language model tests (VL models with image inputs)
```

**What works:** `extra_examples` / `extra_tools` / `extra_vl` are registered and excluded by default via `-m`. The `-n auto` (xdist) + `--timeout=60` settings apply globally.

**What's broken:** `skip_if_no_local` is registered as a marker name only. The auto-skip hook (`pytest_collection_modifyitems` in conftest) is **gone**.

### Critical Finding: conftest.py regression

- Commit `c3ca717` (2026-07-14) added the full local-LLM infrastructure to `tests/conftest.py` (241 lines): `_LocalLLMDetector`, `pytest_configure`, `pytest_collection_modifyitems` auto-skip, `local_llm_cfg`/`local_vl_llm_cfg`/`local_llm_cfg_with_retry` fixtures, `local_llm_models` etc. Documented in `.agent_lessons/test_fixes_summary.md` (78 passed / 9 skipped).
- Commit `98a88eb` (2026-07-27, "fix: isolate test config to prevent production config corruption") **deleted 530 lines** from `tests/conftest.py`, removing ALL of the above.
- Current `tests/conftest.py` (154 lines) only has: `isolated_config_dir` autouse fixture + `MockAgentPool`/`MockInstance` (compression test support).
- **Consequence:** any test requesting `local_llm_cfg` fails with `fixture 'local_llm_cfg' not found`; `skip_if_no_local` emits an unknown-marker warning and does NOT skip. The live-LLM suite is currently **unrunnable as intended**.
- Saved to `.agent_lessons/test_fixtures_removed_98a88eb.md`.

---

## 4. Categorization Summary

| Category | Count | Files |
|---|---|---|
| **A. Offline regression/unit (safe for dev loop)** | ~55 files | All root tests except 4 script/live ones; `orchestrator/`, most of `tools/`, `qwen_server/`, `llm/test_max_input_tokens_not_popped.py` |
| **B. Live local-LLM integration (needs server; currently broken fixtures)** | ~15 files / ~40 tests | `llm/` (5 files), `agents/` (6 files), `memory/`, `examples/` (3 files), `tools/test_vector_search.py`, `tools/test_tools.py::test_image_gen` |
| **C. Live external API (marked extra_*)** | ~2 files | `tools/test_tools.py` (4 tests), `examples/test_examples.py` |
| **D. Unmarked live scripts (run regardless of markers)** | 4 files | `test_llm.py`, `test_llm_local.py`, `test_state_restore_comparison.py`, `test_token_estimation.py` (partial) |
| **E. Environment/disk-dependent (not network)** | 1 file | `test_inner_loop_live_data.py` (+ `test_inner_loop_fp_simulation.py` sim) |

---

## 5. Recommendations

1. **Restore the local-LLM fixture/marker infrastructure** before splitting: re-apply the `pytest_configure`/`pytest_collection_modifyitems` auto-skip hook and `local_llm_cfg`/`local_vl_llm_cfg` fixtures from `git show c3ca717:tests/conftest.py` (adapted to the current isolation logic). Without this, category B tests are hard-broken, not skip-able.
2. **Move category D (unmarked live scripts) into a live-only folder** (e.g., `tests/live/`) or add markers: `test_llm.py`, `test_llm_local.py`, `test_state_restore_comparison.py`, `test_token_estimation.py` (keep the unit portion, gate the llama.cpp call).
3. **Mark `tests/tools/test_tools.py::test_retrieval` with `extra_tools`** — it currently fetches a remote PDF with no marker/exclusion.
4. **Add a `live` (or reuse `integration`) marker** for the local-LLM group and extend `pytest.ini`'s `-m` exclusion to cover it: `-m "not extra_examples and not extra_tools and not extra_vl and not live"`, or invert to a `-m "not live"` default with an explicit `-m live` CI job.
5. **Keep `tests/test_api_endpoints.py` in the regression set** — it is in-process and does not hit the network despite the name; renaming or re-documenting it would reduce confusion.
6. **Handle `test_inner_loop_live_data.py` separately** — it's disk/log dependent, not network. It has a natural guard (`skipif(LOG_DIR is None)`) but also an implicit ≥1000-message requirement that can fail in fresh environments; consider marking it `live` too.
7. Consider a CI job split: `pytest -m "not live"` for fast dev feedback vs `pytest -m live` for the integration suite.

---

## 6. Confidence Levels

- **Confirmed:** conftest fixture regression (git-verified), file inventory, marker usage, `extra_*` exclusion behavior, live endpoints in category D.
- **High:** category B tests all require a local LLM server (based on code inspection of each test body + fixtures).
- **Moderate:** `test_token_estimation.py` behavior — the live call is wrapped in try/except and is print-only (no assert), so it degrades gracefully offline.

## 7. Open Questions / Unknowns

- Was removal of the fixtures in `98a88eb` intentional (deferred redesign) or accidental collateral damage? The commit message ("isolate test config") suggests the fixtures were removed as part of that refactor without a replacement plan.
- Are there plans to re-introduce `skip_if_no_local` behavior (e.g., via a plugin or CI-side skip)?
- Does the team want a single `live` marker or keep the three `extra_*` markers plus a new one?

## 8. Supporting Evidence (key refs)

- `pytest.ini` — markers + default exclusion
- `tests/conftest.py` — current content (154 lines)
- `git show c3ca717:tests/conftest.py` — original fixture/marker implementation
- `git show 98a88eb --stat` — 17 insertions / 530 deletions on conftest.py
- `.agent_lessons/test_fixes_summary.md` — prior working state (2026-07-14)
- `.agent_lessons/test_fixtures_removed_98a88eb.md` — new memory saved this session
- Test file docstrings: `test_api_endpoints.py` (in-process), `test_unified_system.py` (mocked), `test_async_shell_cmd.py` (no network), `test_inner_loop_live_data.py` (log-based)