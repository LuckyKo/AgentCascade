# Module-Split Cleanup Plan — AgentCascade

**Date:** 2026-08-19
**Author:** Maine (orchestrator)
**Status:** APPROVED v3 — ready for Phase 1 implementation (reviewed: plan_review_1, plan_review_2, plan_review_3; all findings resolved)
**Goal:** Split the bloated top-level modules into cohesive, well-named sub-modules. **No functional changes.** Production import surface preserved via facades; tests updated to reference true new locations where symbols move.

> **v3 note (user direction):** "Don't do a half-assed job out of fear of changing tests." We therefore update the ~37 test lines that import/patch *internal helpers* to point at their TRUE new home modules (§11), instead of keeping facade indirection just to avoid touching tests. Production facades stay (they serve ~100+ production import sites — good engineering, not test-fear). This makes `mock.patch` targets honest (patch where the symbol actually lives & is called) and removes the v2 facade-routing hack. The moved methods keep their natural bare-global calls in their new home.

---

## 0. Ground Rules (non-negotiable)

1. **Zero functional change.** This is a pure *move* refactor: code is relocated, never rewritten. Method bodies, control flow, side effects, ordering all stay identical.
2. **Import compatibility guaranteed.** Every symbol that is imported from an original module by *any* file (source or test) must remain importable from that same original module path. Achieved via **re-export shims** in the original modules (see §2).
3. **Test gate per phase.** Baseline is `1619 passed / 1 failed (env ZMQ) / 1 skipped`. After each phase, the suite must be green at parity (no new failures; the env failure may pass or fail independently — not a regression signal).
4. **Review gate per phase.** No phase's code is committed until an independent reviewer passes it.
5. **One concern per commit.** Each phase = one reviewed, tested, committed unit.
6. **Preserve existing circular-import workarounds.** The codebase already manages cycles via lazy imports and two-phase init (engine reference set post-construction). We must *not* disturb these; we only move code.

---

## 1. Scope & Priority

Target files (by size, all in `agent_cascade/`):

| File | Size | Verdict |
|------|------|---------|
| `execution_engine.py` | 284 KB (~5600 L) | **Phase 1** — biggest win, mostly self-contained helpers + one big class |
| `agent_pool.py` | 157 KB (~3200 L) | **Phase 2** — narrow public API (`AgentPool` only), clean helper extraction |
| `api_router.py` | 89 KB | **Phase 3a** — no circular risk, clean 4-way split |
| `api_integration.py` | 88 KB | **Phase 3b** — module-function hub + `_cache_mgr` singleton |
| `async_shell.py` | 72 KB | **Phase 3c** — low risk, tracker/task/windows split |
| `api_server.py` | 68 KB | **Phase 4a** — entry point; path/content helpers extractable |
| `ws_handlers.py` | 59 KB | **Phase 4b** — one class, many handlers; extract helper modules |

Deferred / out of scope for this pass:
- `security_handler.py` (49 KB) — tightly coupled to slot/endpoint machinery and has its own deadlock-sensitive logic. Review it separately later; splitting it risks the security flow. *Flagged, not scheduled.*
- `config_handlers.py`, `telemetry.py`, `lifecycle_manager.py`, `tool_dispatcher.py` (~30–40 KB) — already reasonably cohesive single-responsibility modules. No split needed.

**Phasing rationale:** Phase 1 & 2 are the two mega-files and carry the highest value with the most independent structure (fewer cross-module entanglements than the API tier). The API tier (Phase 3/4) has more inter-file coupling, so it comes after we've proven the re-export-shim approach works on the cleaner files.

---

## 2. Re-Export Shim Strategy (the safety mechanism)

### 2.1 mock.patch targets — tests updated to TRUE homes (v3 decision)

**Decision (user-approved):** We do NOT keep facades alive purely to avoid touching tests. Tests are ours; updating their import/patch paths to point at the *true new location* of a symbol is legitimate refactor work and yields a cleaner, more honest result. Production code keeps facades (§2.2) — that's for the ~100+ production import sites, not out of test-fear.

**Why this is better than the v2 facade-routing hack:**
`mock.patch('a.b.c')` swaps `c` on module `a.b`. A patch is only effective if the *code under test* looks up `c` in that same module's globals. In v2 we forced moved methods to call `_ee_facade._canonical_detect_loop(...)` so a patch on the facade would stick — artificial indirection. In v3 the method keeps its **natural bare-global call** (`_canonical_detect_loop(messages)`) inside its true home module, and the test patches that true home. Same object, same call, no shim gymnastics, and the patch target is now *correct by construction*.

**Consequence:** For every symbol that (a) moves to a new sub-module AND (b) is imported or `mock.patch`-ed by a test, we update the test to reference the new location. The full enumerable list is in **§11 (Test Change Set)**. Symbols that stay importable from the facade (all public classes/functions: `AgentPool`, `ExecutionEngine`, `APIRouter`, `create_app`, …) need **no** test change — the large majority of test references are to those and are untouched.

**Decision rule — when does an INTERNAL helper get re-targeted vs. stay facade-importable?**
A moved internal symbol's test references are **re-targeted to the true home** when:
- it is used as a **mock-patch target** (the patch MUST resolve where the calling code looks up the name — there's no safe "facade" alternative), OR
- it is a **free function / constant** imported directly by tests (re-exporting a helper just to avoid editing 1–3 test lines adds facade bloat for zero benefit).

An internal symbol may **stay facade-importable** (no test change) only when:
- it is a **class** that tests instantiate or patch *by class identity* and the facade re-export is already needed for production anyway (e.g. `_InstanceConversationMapping` — a helper class; keeping it on the facade is one line and avoids churning ~16 test refs), OR
- re-exporting it is genuinely simpler than updating many test sites AND there's no mock-interception concern.

This rule is applied **consistently**: every exception from "re-target" must be named in §11 with its justification. (Currently the only such exception: `_InstanceConversationMapping` — class, facade re-export already serves production, ~16 test refs saved.)

> Reviewer must verify per phase: for each moved symbol in §11, confirm the corresponding test import/patch path was updated AND that the new patch target is the module where the calling code actually resolves the name (so the mock still intercepts). And confirm any "stay facade-importable" exception is explicitly listed + justified.

### 2.2 Facade re-exports

For each original module `M.py` that we split, we keep `M.py` as a **thin compatibility facade**:

```python
# execution_engine.py  (after split — becomes a facade)
"""Facade: preserves the historical import surface for execution_engine.
Real implementations now live in agent_cascade/engine/* sub-modules."""
from agent_cascade.engine.core import ExecutionEngine          # noqa: F401
# NOTE: methods moved into mixins are NOT re-exported as free functions —
# they live on ExecutionEngine (via inheritance). Only module-level names
# (helpers, constants) and the class itself are re-exported here.
from agent_cascade.engine.compression_exec import (            # noqa: F401
    FALLBACK_COMPRESSION_MAX_ROUNDS,
    FALLBACK_COMPRESSION_INITIAL_FRACTION,
    _COMPRESSOR_WINDOW_SAFETY_FACTOR,
)
from agent_cascade.engine.helpers import (                     # noqa: F401
    _extract_tool_calls_from_text,
    _build_resources_block,           # imported by tests/test_nested_agent_calls.py
    _get_active_functions_from_template,  # imported by tests/test_nested_agent_calls.py + agent_factory
    _inject_self_augmentation_skill,
    _build_session_metadata,
)
# NOTE (v3): we do NOT bind mock.patch targets here. Tests that patch internal
# helpers are updated to point at the TRUE home module (§2.1, §11). The facade
# only re-exports names that PRODUCTION code imports from this path.
__all__ = [...]  # full list of preserved names (production import surface)


**Why facades instead of updating every import site:**
- ~86 files import `AgentPool`, dozens import `ExecutionEngine`, and tests import *private* symbols (`_extract_tool_calls_from_text`, `_resolve_max_tokens`, `_handle_max_auto_rollbacks`, `_is_path_allowed`, etc.). Updating all import sites is a huge, error-prone diff with zero benefit.
- Facades keep the diff localized to the module being split + new sub-modules. Importers are untouched → minimal regression surface.
- This is the standard, safe pattern for a pure-move refactor.

**Naming convention:** New sub-packages use a plural-ish namespace that mirrors the concern: `engine/`, `pool/`, `api_router_pkg/` (see below for exact names). Original `.py` files stay at package root as facades.

> **Decision needed from user (§7):** sub-package layout vs flat modules. This plan assumes **sub-packages** (option b) because the mega-files have many cohesive groups; it keeps `agent_cascade/` root uncluttered. If you prefer flat, we rename accordingly — the shim strategy is identical either way.

---

## 3. Phase 1 — `execution_engine.py` → `agent_cascade/engine/`

### 3.1 Target structure
```
agent_cascade/engine/
├── __init__.py            # re-exports ExecutionEngine + public constants (facade content)
├── helpers.py             # ~25 module-level pure/near-pure functions (see 3.2)
├── llm_call.py            # LLM call, retry, error classification, merged cfg
├── compression_exec.py    # engine-side compression triggering/slicing/rollback
├── tool_execution.py      # _execute_detected_tools + image handling
└── core.py                # ExecutionEngine class: lifecycle, run loop, state, slots, streaming, telemetry
```

### 3.2 `helpers.py` — module-level functions (moved verbatim)
These are defined at module scope today and are either pure or only depend on imports + `logger`. Move them whole; they keep their names.
- `_get_active_functions_from_template`, `_make_token_count_callback`, `_make_usage_callback`, `_invalidate_token_cache`
- `_normalize_gemma_thought_tags`, `_normalize_thinking_blocks`, `_extract_tool_calls_from_text`, `_check_message_truncation`, `_is_incomplete_state`
- `_build_resources_block`, `_build_skills_block`, `_inject_skills_to_system_message`, `_inject_self_augmentation_skill`
- `_get_supervisor_log_filename`, `_build_session_metadata`, `_replace_section`, `_replace_resources_block`
- `SleepAction` (small dataclass/enum, internal)

**Re-exported from facade (must remain importable from `agent_cascade.execution_engine`):**
`_extract_tool_calls_from_text`, `_build_resources_block`, `_get_active_functions_from_template`, `_inject_self_augmentation_skill`, `_build_session_metadata` (all verified imported by tests/other modules).

### 3.3 `llm_call.py` — LLM call cluster
Move these methods out of the class into a mixin or free functions that take the engine as first arg? **No.** See §6 "Method-move technique". For Phase 1 we use the **mixin** approach: define classes in each sub-module that the `ExecutionEngine` in `core.py` inherits from. This keeps `self.x` attribute access identical (no rewrites), which is what guarantees zero functional change.

```
class LLMCallMixin:      # llm_call.py
    _pre_llm_checks, _execute_llm_call_with_retry, _execute_llm_call,
    _call_llm_with_injection, _build_merged_cfg, _store_allocated_max_input_tokens,
    _classify_llm_error, _make_retrying_message, _make_error_message

class CompressionExecMixin:  # compression_exec.py
    _check_and_trigger_compression, _force_compression, _proactive_compression_check,
    _rebuild_working_set, _find_compression_slice, _is_suspended_by_compression,
    _wait_for_compression_to_clear, _inject_compression_warning, _inline_rollback_and_hint

class ToolExecMixin:      # tool_execution.py
    _execute_detected_tools, _has_images, _ensure_image_captions
```

### 3.4 `core.py` — the remaining `ExecutionEngine`
```python
class ExecutionEngine(LLMCallMixin, CompressionExecMixin, ToolExecMixin):
    # __init__, initialize, run, _setup_turn, _telemetry, slot mgmt,
    # streaming, loop-detection, message building, state transitions, etc.
```
Plus the module-level constants that belong to core: `MAX_TEXT_LENGTH_FOR_REGEX`, `MIN_OUTPUT_LENGTH`, `SLEEPING_LOOP_BACKOFF`, `_COMPRESSION_WAIT_TIMEOUT`, `SAMPLING_AND_LIMIT_KEYS`.

**Constants:** `FALLBACK_COMPRESSION_MAX_ROUNDS`, `FALLBACK_COMPRESSION_INITIAL_FRACTION`, `FALLBACK_COMPRESSION_MIN_SLICE_FRACTION`, `_COMPRESSOR_WINDOW_SAFETY_FACTOR` move to `compression_exec.py`. Tests that import them are updated to the new home (§11). The facade may still re-export them for any production importer (none currently, but harmless).

### 3.5 Test updates for Phase 1 (per §2.1 / §11)
Moved internal helpers → update the corresponding test imports/patches to the TRUE home:
- `_canonical_detect_loop`: called at line 2167 inside **`_pre_llm_checks`** (def at line 2109), which is in the LLM-call cluster and moves to `llm_call.py`. It keeps its natural bare-global call there (`_canonical_detect_loop(messages)`). `llm_call.py` gets the import `from agent_cascade.loop_detection import detect_loop as _canonical_detect_loop`. **Test change:** the 12 `patch('agent_cascade.execution_engine._canonical_detect_loop', ...)` in `test_loop_detection.py` → `patch('agent_cascade.engine.llm_call._canonical_detect_loop', ...)`.
- `compute_discard_count`: called in `_find_compression_slice` (line ~2509), which moves to `compression_exec.py`. Natural bare-global call kept; `compression_exec.py` imports it. **Test change:** `patch("agent_cascade.execution_engine.compute_discard_count", ...)` in `test_fallback_compression.py:608` → `patch("agent_cascade.engine.compression_exec.compute_discard_count", ...)`.
- `_extract_tool_calls_from_text`, `_build_resources_block`, `_get_active_functions_from_template`, `_build_session_metadata`: move to `helpers.py`. **Test changes:** update imports in `test_embedded_tool_call_detection.py:8`, `test_nested_agent_calls.py:16`, `test_session_metadata_fix.py:12` to `from agent_cascade.engine.helpers import ...`.
- `FALLBACK_COMPRESSION_*` / `_COMPRESSOR_WINDOW_SAFETY_FACTOR`: move to `compression_exec.py`. **Test changes:** update imports in `test_fallback_compression.py:351,437,744` to `from agent_cascade.engine.compression_exec import ...`.

> No facade-routing hacks. The moved methods keep natural bare-global calls; tests patch the true home. This is the ONLY non-move edit in Phase 1 (besides the mechanical test-path updates).

### 3.6 Pre-implementation gates (mandatory)
Before wiring the facade and before any test run, the coder MUST:
1. **MRO name-collision check:** collect all method names across `core.py` + the three mixins and assert no duplicates (a duplicate = ambiguous MRO). Small script or grep. Also confirm `ExecutionEngine` has no existing base classes / metaclass / dataclass decorator that would complicate multiple inheritance (verified: plain class at line 812 — coder re-confirms).
2. **Acyclicity proof:** after creating the sub-modules but BEFORE finalizing the facade, run in a clean interpreter:
   `python -c "import agent_cascade.engine.helpers; import agent_cascade.engine.llm_call; import agent_cascade.engine.compression_exec; import agent_cascade.engine.tool_execution; import agent_cascade.engine.core; import agent_cascade.execution_engine"`
   Any `ImportError`/cycle here is a hard stop — fix the import direction before proceeding. This converts the "assume DAG" claim into verified evidence.
3. **Test-path updates applied:** all Phase-1 test changes in §3.5 / §11 made. Then run the targeted mock-patch tests (§3.8) to confirm `_canonical_detect_loop` / `compute_discard_count` patches still intercept at their new true-home targets.

### 3.7 Circular-import handling
- `engine/*` sub-modules import from the same set of modules the old file did (`settings`, `llm.schema`, `retry_policy`, `tool_utils`, `compression.handler`, `tool_dispatcher`, etc.). No new cycles introduced because we only *relocate* existing imports into the sub-module that uses them.
- **Two-phase init preserved:** `engine.core.ExecutionEngine` still receives lifecycle_manager/tool_dispatcher/compression_handler references post-construction; those modules' TYPE_CHECKING imports of `ExecutionEngine` continue to point at the facade (`agent_cascade.execution_engine`) which re-exports it. No change needed there.
- **Ordering care:** `helpers.py` must not import from `core.py`; `llm_call.py`/`compression_exec.py`/`tool_execution.py` may import from `helpers.py`. `core.py` imports all three mixins + helpers. One-directional DAG → no cycle.

### 3.8 Phase 1 verification
- Production import surface check: `python -c "import agent_cascade.execution_engine as ee; assert ee.ExecutionEngine and ee._extract_tool_calls_from_text and ee.FALLBACK_COMPRESSION_MAX_ROUNDS"` (facade still serves production importers).
- True-home check: `python -c "from agent_cascade.engine.llm_call import _canonical_detect_loop; from agent_cascade.engine.compression_exec import compute_discard_count, FALLBACK_COMPRESSION_MAX_ROUNDS; from agent_cascade.engine.helpers import _extract_tool_calls_from_text"`
- MRO collision check passed (§3.6).
- **Mock-patch smoke test (highest-risk for this phase):** run `pytest tests/test_loop_detection.py -k "loop" tests/test_fallback_compression.py` — these exercise the RE-TARGETED `_canonical_detect_loop`/`compute_discard_count` patches at their new true-home locations. If a patch target is wrong, these tests fail or mock nothing (catches §3.5 errors immediately).
- Full pytest run → parity with baseline (1619/1/1; env failure excluded from regression signal).
- Reviewer pass (see §8).

---

## 4. Phase 2 — `agent_pool.py` → `agent_cascade/pool/`

### 4.1 Public API is narrow
Only `AgentPool` and `_InstanceConversationMapping` are imported externally (verified: 86 files import `AgentPool`; only `tests/test_phase5_polish.py` imports `_InstanceConversationMapping`). This makes Phase 2 the *safest* split of the two mega-files.

### 4.2 Target structure
```
agent_cascade/pool/
├── __init__.py            # re-exports AgentPool, _InstanceConversationMapping (facade content)
├── conversation_map.py    # _InstanceConversationMapping (takes pool ref; self-contained)
├── parallel_manager.py    # ParallelAgentManager (constructor-injected with pool)
├── logger_manager.py      # LoggerManager (constructor-injected with pool)
├── idle_manager.py        # IdleManager (constructor-injected with pool)
├── lifecycle.py           # AgentPool mixin: create/dismiss/terminate/halt/resume/remove/stop_session
├── conversation.py        # AgentPool mixin: history, compression target sets, slice_history_for_llm
├── message_queue.py       # AgentPool mixin: enqueue/drain/wait/active_stack/has_pending
├── slots.py               # AgentPool mixin: _acquire_slot, pause/resume, register_async_call
├── config_persist.py      # AgentPool mixin: save/load pool settings, disabled tools, notify_config_changed
├── rollback.py            # AgentPool mixin: snapshots, surgical_rollback, marker find/count
└── session_io.py          # AgentPool mixin: load_session_from_log, save/restore instance state, template hash
```

### 4.3 Approach
- The four helper classes (`_InstanceConversationMapping`, `ParallelAgentManager`, `LoggerManager`, `IdleManager`) move **verbatim** into their own files (they only receive a `pool` reference — clean).
- `AgentPool` is split via **mixins** exactly as Phase 1: each sub-module defines a mixin class; `core` (or `__init__`) composes them:
  ```python
  # agent_cascade/pool/core.py
  class AgentPool(LifecycleMixin, ConversationMixin, MessageQueueMixin,
                  SlotsMixin, ConfigPersistMixin, RollbackMixin, SessionIOMixin):
      def __init__(...): ...   # unchanged body
      # properties (agents, instance_classes, etc.) + small methods stay here
  ```
- **Lazy imports preserved:** all the in-function lazy imports (`APIRouter`, `AsyncShellTracker`, `SkillManager`, `_cache_mgr`, `ExecutionEngine`, `run_child_core`, `save_instance_state`, etc.) move with their host method into the appropriate mixin, unchanged. This keeps the existing cycle-mitigation intact.

### 4.4 Circular-import handling
- Known lazy cycle: `agent_pool` ↔ `logger/tail_sync_check` (both lazy). We do **not** break it (no functional change) — we just carry the lazy import into whichever mixin hosts `surgical_rollback`. The facade `agent_cascade.agent_pool` still re-exports `AgentPool`, so `tail_sync_check`'s `from agent_cascade.agent_pool import AgentPool` keeps working.
- `agent_pool` imports `_cache_mgr`/`_clear_performance_caches` from `api_integration` lazily — carried into the relevant mixin, unchanged.

### 4.5 Phase 2 verification
- Import check for `AgentPool` + `_InstanceConversationMapping`.
- Full pytest → parity.
- Reviewer pass.

### 4.6 Documented placement decision (marker staticmethods)
The three marker helper staticmethods — `find_last_marker`, `count_markers`, `find_all_marker_indices` — call `AgentPool._msg_field(...)` by **bare class name** (`AgentPool` was a module global in the original monolith). After the split they cannot stay in `rollback.py` and reference `AgentPool` without `from .core import AgentPool` — but `core.py` already does `from .rollback import RollbackMixin`, so that would create a **core↔rollback circular import** (a real functional change, worse than the alternative).

**Decision:** host these 3 statics in `core.py`, where `AgentPool` is a native module global. Their bodies are **byte-identical** to the original (verified programmatically against `git show HEAD~1:agent_cascade/agent_pool.py`). This is the minimal change that preserves verbatim bodies AND avoids introducing a cycle. It's an organizational placement choice, not a functional change — recorded here so it isn't mistaken for drift in a future review.

---

## 5. Phases 3 & 4 — API tier (lower risk, more inter-file coupling)

These are planned at a lighter level now; each gets its own detailed sub-plan drafted *just before* implementation once Phase 1/2 prove the pattern. Summary of intended splits:

### 5.1 `api_router.py` → `agent_cascade/api_router_pkg/` (Phase 3a)
- No circular risk (verified). Clean split:
  - `endpoints.py`: `APIEndpoint` (+ `_normalize_repeat_penalty`)
  - `scheduler.py`: `EndpointScheduler`
  - `router.py`: `APIRouter`
  - `helpers.py`: `_check_termination`, `_interruptible_sleep`, `ensure_api_endpoints_config`
- Facade re-exports: `APIEndpoint`, `APIRouter`, `EndpointScheduler`, `_check_termination`, `_interruptible_sleep`, `ensure_api_endpoints_config`, `_normalize_repeat_penalty`.

### 5.2 `api_integration.py` → `agent_cascade/api_integration_pkg/` (Phase 3b)
- Module-function hub (~40 funcs) + `CacheManager`/`_cache_mgr` singleton.
  - `cache.py`: `CacheManager` + `_cache_mgr` singleton + `_clear_performance_caches`
  - `streaming.py`: `broadcast_stream_update`, `_put_stream_update`, `_calc_stream_token_stats`
  - `state_builder.py`: `build_state_from_pool`, `build_stream_update_from_pool`, `_serialize_*`, `_build_active_stack`, etc.
  - `runner.py`: `create_main_agent_instance`, `run_agent_in_pool`, `run_agent_in_pool_with_recovery`, `execute_agent_turn`
  - `tokens.py`: `_resolve_max_tokens`, `_get_max_tokens_for_instance`, `_streaming_content_length`
- **Critical:** `_cache_mgr` must be a single shared instance. Move it to `cache.py`; the facade re-exports `_cache_mgr` so `agent_pool`'s lazy import still resolves to the same object. (Verify identity: `agent_cascade.api_integration._cache_mgr is agent_cascade.api_integration_pkg.cache._cache_mgr`.)
- Facade re-exports all imported names incl. `_resolve_max_tokens`, `run_agent_in_pool_with_recovery`, `broadcast_stream_update`, `_apply_ui_config`, `_clear_performance_caches`, `_put_stream_update`, `build_stream_update_from_pool`, `create_main_agent_instance`.

### 5.3 `async_shell.py` → `agent_cascade/async_shell_pkg/` (Phase 3c)
- Low risk. Split:
  - `task.py`: `AsyncShellTask` (+ `_elapsed_for_task`)
  - `windows.py`: Windows process helpers (`_send_windows_ctrl_c`, `_get_windows_descendant_pids`, `_check_windows_pids_alive`, `_kill_process_tree`, `_kill_viewer_process`)
  - `tracker.py`: `AsyncShellTracker`
- Facade re-exports: `AsyncShellTracker`, `AsyncShellTask`, `KILL_WAIT_TIMEOUT` (and any other exported constants).

### 5.4 `api_server.py` (Phase 4a) — selective extraction only
- `create_app` is the entry point and is large but cohesive; **do not** fragment it. Extract only clearly separable pure helpers:
  - `path_security.py`: `_is_path_allowed`, `_get_allowed_file_roots` (imported by tests)
  - `content_parse.py`: `_parse_multimodal_content`, `_extract_system_message` (imported by ws_handlers + tests)
- Keep `create_app` and its request handlers in `api_server.py`. Facade re-exports the moved helpers.

### 5.5 `ws_handlers.py` (Phase 4b) — extract shared helper modules, keep the class
- One class with 31 handlers. The handlers themselves stay in `WsMessageHandler` (splitting a single dispatch-table class into mixins is high-risk for little gain). Instead extract *pure* module-level helpers it uses:
  - Move `_clear_caches_safely`, `_validate_disabled_tools` and any other free functions into a small `ws_helpers.py`.
- **Note the existing cycle:** `ws_handlers` imports from `api_server` (line ~182) while `api_server` imports `WsMessageHandler`. This works today via import ordering. We do NOT restructure it (no functional change); we only ensure the moved helpers keep their import paths valid. If Phase 4a moves `_parse_multimodal_content`/`_extract_system_message` out of `api_server`, update `ws_handlers`' import to pull them from the new helper module — a mechanical, reviewable change.

---

## 6. Method-Move Technique (why mixins, not free functions)

The mega-files' classes have ~90–120 methods each that freely access `self.<attr>` and call each other. Two options:

- **(A) Free functions** taking engine/pool as first arg → requires rewriting every `self.x` reference and every inter-method call. Huge diff, high risk of subtle behavior change. **Rejected.**
- **(B) Mixins** → move method *bodies verbatim* into mixin classes; the main class inherits them. `self` still resolves to the same object; attribute access and cross-mixin calls work unchanged (Python MRO handles it). Diff is "cut methods, paste into new class in new file" — minimal and reviewable. **Chosen.**

**Mixin safety checks (reviewer enforces):**
1. No name collisions between mixins (two mixins defining the same method → MRO ambiguity). Reviewer greps for duplicate method names across a phase's mixins.
2. `__init__` stays in the core class only (mixins must not define `__init__`).
3. Any method that references another mixin's method still works (MRO) — verified by running the full test suite, not just import checks.
4. Properties/attributes initialized in `__init__` are accessible from all mixins (they're on the same instance) — no change needed.

---

## 7. Decision Points (need user sign-off before implementation)

1. **Layout:** sub-packages (`engine/`, `pool/`, …) vs flat modules (`execution_engine_helpers.py`, …)? *Plan assumes sub-packages.*
2. **Scope of this pass:** all 7 files across 4 phases, or stop after Phase 1+2 (the two mega-files) and re-evaluate? *Recommendation: do all, but each phase is independently committed/reviewed so you can stop at any gate.*
3. **`security_handler.py`:** confirmed out of scope for now (deferred). OK?
4. **The `MODULE_SPLIT_DEPENDENCY_REPORT.md`** the researcher dropped in repo root — keep it as a reference artifact, or move it under `plans/` / delete after we fold its findings into this plan? *Recommendation: move to `plans/`.*

---

## 8. Review Cycle (per phase)

For each phase, before committing:
1. **Coder** implements the split (move-only production code) + applies the §11 test-path updates for that phase, and runs the **MRO pre-check** (§3.6 / equivalent for pool) + acyclicity proof + import smoke check before handing off.
2. **Reviewer (fresh instance)** checks:
   - No functional change in production code (method bodies byte-identical modulo relocation; no logic edits). Permitted non-move edits: (a) §11 test-path updates, (b) mechanical import updates in `ws_handlers` (Phase 4b). Nothing else.
   - Facade re-exports **every** symbol that PRODUCTION code imports from the old path (cross-check against import lists in §3/§4/§5).
   - Every §11 test edit is correct: new import/patch path points at the module where the symbol actually lives AND (for patches) where the calling code resolves it — so the mock still intercepts.
   - No mixin name collisions; `__init__` only in core.
   - Lazy imports carried intact; two-phase init untouched.
   - `_cache_mgr` identity preserved (Phase 3b) — reviewer asserts `api_integration._cache_mgr is api_integration_pkg.cache._cache_mgr`.
   - No accidental circular-import introduction (import the package fresh in a clean interpreter).
3. **Test gate:** targeted mock-patch smoke tests for the phase (§3.8 pattern) FIRST, then full pytest → parity with baseline (1619/1/1, env failure excluded from regression signal).
4. Only on explicit reviewer PASS + green tests → commit.

---

## 9. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Subtle behavior change from a "move" that isn't pure | Medium | Mixin approach (no `self` rewrites); reviewer byte-diffs moved methods; full test suite gate |
| **Broken mock.patch targets** → tests fail or mock nothing | **High if unaddressed** | v3: moved methods keep natural bare-global calls in true home; §11 enumerates every test path to re-target; targeted smoke test per phase (§3.8) catches a wrong target immediately; reviewer verifies each new patch target is where the code resolves the name |
| Missed production re-export → ImportError | Medium | Enumerate every PRODUCTION import per file (done in §3/§4/§5); facade lists them explicitly; clean-interpreter import check |
| Missed test-path update → test AttributeError | Low-Medium | §11 is the complete enumerable list; reviewer cross-checks table vs actual diff per phase; full-suite gate catches stragglers |
| MRO collision between mixins | Low | Mandatory pre-check (§3.6) before writing mixins; reviewer re-verifies |
| Disturbing existing lazy-import cycle workaround | Low | We carry lazy imports verbatim; do not "fix" cycles (no functional change) |
| `_cache_mgr` becomes two instances | Medium (Phase 3b) | Single definition in `cache.py`; identity assertion in test gate |
| Scope creep into logic changes | Medium | Hard rule §0.1 + reviewer rejects any non-move edit (only §2.1 routing + Phase-4b import updates allowed) |

---

## 10. Deliverable Sequence

1. User answers §7 decision points.
2. This plan goes through a **plan review** (fresh reviewer) — check feasibility, catch missed imports/patch-targets, sanity-check mixin strategy. (Done: `plan_review_1`; v2 addresses its findings.)
3. Phase 1 implement → MRO pre-check → review → mock-patch smoke + full test gate → commit.
4. Phase 2 implement → MRO pre-check → review → mock-patch smoke + full test gate → commit.
5. **Draft detailed sub-plans for Phases 3a/3b/3c** (exact per-file symbol moves, patch-target routing, `_cache_mgr` identity) → quick reviewer pass on the sub-plans → implement each → review → test → commit each.
6. **Draft detailed sub-plan for Phase 4a/4b** (esp. the `ws_handlers`↔`api_server` import-ordering preservation) → reviewer pass → implement → review → test → commit each.
7. Final regression review + cleanup of temp artifacts (the researcher's root-level report file, etc.).

> Rationale for per-phase sub-plans (addresses review warning #8): Phases 1/2 are fully specified here because they're the highest-risk and most independent. The API tier has more inter-file coupling, so its detailed symbol-by-symbol moves are drafted right before implementation and reviewed at that point rather than up front — keeps this master plan focused while still gating every phase behind review.

---

## 11. Test Change Set (v3 — the complete enumerable list)

Per user direction, tests that import/patch **internal helpers** (not public classes/functions) are updated to reference the symbol's TRUE new home. Public names (`AgentPool`, `ExecutionEngine`, `APIRouter`, `APIEndpoint`, `EndpointScheduler`, `create_app`, `AsyncShellTracker`, `AsyncShellTask`) stay facade-importable → their test references are **untouched**. Only the lines below change.

### Phase 1 — `execution_engine` internals (9 files, ~24 lines)
| Test file:line | Current | New |
|----------------|---------|-----|
| `test_embedded_tool_call_detection.py:8` | `from agent_cascade.execution_engine import _extract_tool_calls_from_text` | `from agent_cascade.engine.helpers import _extract_tool_calls_from_text` |
| `test_nested_agent_calls.py:16` | `from agent_cascade.execution_engine import (_get_active_functions_from_template, _build_resources_block, ...)` | `from agent_cascade.engine.helpers import _get_active_functions_from_template, _build_resources_block` (keep `ExecutionEngine` from facade) |
| `test_session_metadata_fix.py:12` | `from agent_cascade.execution_engine import _build_session_metadata` | `from agent_cascade.engine.helpers import _build_session_metadata` |
| `test_fallback_compression.py:351` | `... import FALLBACK_COMPRESSION_INITIAL_FRACTION` | `from agent_cascade.engine.compression_exec import FALLBACK_COMPRESSION_INITIAL_FRACTION` |
| `test_fallback_compression.py:437` | `... import FALLBACK_COMPRESSION_MAX_ROUNDS` | `from agent_cascade.engine.compression_exec import FALLBACK_COMPRESSION_MAX_ROUNDS` |
| `test_fallback_compression.py:608` | `patch("agent_cascade.execution_engine.compute_discard_count", ...)` | `patch("agent_cascade.engine.compression_exec.compute_discard_count", ...)` |
| `test_fallback_compression.py:744` | `... import _COMPRESSOR_WINDOW_SAFETY_FACTOR` | `from agent_cascade.engine.compression_exec import _COMPRESSOR_WINDOW_SAFETY_FACTOR` |
| `test_loop_detection.py` (12 lines: 629,639,1008,1019,1029,1055,1078,1101,1125) | `patch('agent_cascade.execution_engine._canonical_detect_loop', ...)` | `patch('agent_cascade.engine.llm_call._canonical_detect_loop', ...)` (caller `_pre_llm_checks` → `llm_call.py`) |

### Phase 3b — `api_integration` internals (3 files, ~17 lines)
| Test file:line | Current | New |
|----------------|---------|-----|
| `test_loop_detection.py` (8 lines: 365,395,421,449,477,498,526,549) | `@patch('agent_cascade.api_integration.run_agent_in_pool')` | `@patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')` |
| `test_max_tokens_resolution.py` (8 lines: 99,113,127,141,155,164,177,195) | `from agent_cascade.api_integration import _resolve_max_tokens` | `from agent_cascade.api_integration_pkg.tokens import _resolve_max_tokens` |
| `test_phase5_polish.py:168` | `from agent_cascade.api_integration import create_main_agent_instance` | `from agent_cascade.api_integration_pkg.runner import create_main_agent_instance` |

### Phases 2 / 3a / 4 — NO test changes expected (Phase 3c has one)
- **Phase 2 (`agent_pool`):** only `AgentPool` + `_InstanceConversationMapping` are referenced by tests; both stay facade-importable. The 16 `patch("agent_cascade.agent_pool.AgentPool")` in `test_memory_consolidation.py` and all `from agent_cascade.agent_pool import AgentPool` lines are **untouched** (facade re-exports `AgentPool`). `_InstanceConversationMapping` is the **one documented exception** to re-targeting (§2.1 rule): it's a helper *class*, its facade re-export already serves production, and keeping it saves ~16 test edits with no mock-interception risk.
- **Phase 3a (`api_router`):** tests reference only public `APIRouter`/`APIEndpoint`/`EndpointScheduler` + `_check_termination`/`_interruptible_sleep` (the latter two stay in the facade's `helpers.py` and are re-exported). `patch('agent_cascade.api_router.APIRouter')` untouched.
- **Phase 3c (`async_shell`):** tests import public `AsyncShellTracker`/`AsyncShellTask` (untouched). `KILL_WAIT_TIMEOUT` is a constant that `test_async_shell_kill.py:122` patches as a value (`patch('agent_cascade.async_shell.KILL_WAIT_TIMEOUT', 0.3)`). Per §2.1 rule, a patched constant MUST resolve where the reading code looks it up → **re-target** to the sub-module that hosts the kill-wait logic (likely `async_shell_pkg/kill.py`). **Test change (Phase 3c):** `patch('agent_cascade.async_shell.KILL_WAIT_TIMEOUT', ...)` → `patch('<true-home-submodule>.KILL_WAIT_TIMEOUT', ...)`. Exact sub-module confirmed at implementation time from where the constant is actually read. (This corrects an earlier inconsistency — a patched value has the same interception constraint as any patch target.)
- **Phase 4 (`api_server`/`ws_handlers`):** tests reference only public `create_app`, `_is_path_allowed`, `_get_allowed_file_roots`, `_parse_multimodal_content` — all stay facade-re-exported. No test changes.

**Total test edits: ~42 lines across ~13 files** (Phase 1: ~24, Phase 3b: ~17, Phase 3c: 1). Fully enumerable; reviewer cross-checks this table against the actual diff per phase. Line numbers are from the current tree and will be re-confirmed at implementation time (files may shift slightly as earlier phases land — but each phase's test edits are applied in that same phase, so they're always current).

> **Safety net:** if any symbol's true home ends up different from the table above during implementation (e.g. a helper lands in a differently-named sub-module), the coder updates BOTH the production move and the matching test line to match, and flags it for reviewer. The table is the plan; the code is the source of truth at commit time.
