# Phase 3 Implementation Sub-Plan — Pure-Move Refactor

> Scope: split 3 large single-module files into package directories, **moving symbols only**
> (no behavior/logic changes), while keeping facade modules that re-export the public API
> so that the **37+ production import sites** keep working unchanged.
>
> Master plan: `plans/module_split_cleanup_plan.md` (§0, §2.1, §5, §8, §11).
> v3 rule: tests that import/patch INTERNAL helpers get **re-targeted** to the symbol's
> TRUE new home; **production** keeps facades.
>
> **All facts below were verified against the actual source on 2026-08-19** (line numbers
> are current). Where this sub-plan corrects the master plan, it is flagged `[CORRECTION]`.

---

# A) `agent_cascade/api_router.py` → `api_router_pkg/`

## A.1 Top-level symbol → sub-module mapping (verified against file)

| Symbol | Type | Line | Target sub-module | Notes |
|---|---|---|---|---|
| `logger` | module global | 25 | `pkg/__init__.py` (or each submodule) | `logging.getLogger(__name__)` — **redefine per sub-module** so loggers keep distinct module names |
| `QUEUE_WAIT_TIMEOUT` | **re-export** | 34 | `pkg/scheduler.py` | `from agent_cascade.slot_queue import ...` — **NOT defined here** (see A.5) |
| `ENDPOINT_SLOT_ACQUIRE_TIMEOUT` | **re-export** | 28 | `pkg/scheduler.py` | from `agent_cascade.api_constants` |
| `ENDPOINT_COOLDOWN_SECONDS` | **re-export** | 29 | `pkg/scheduler.py` | from `agent_cascade.api_constants` |
| `ENDPOINT_FAILURE_CLEANUP_HOURS` | **re-export** | 30 | `pkg/scheduler.py` | from `agent_cascade.api_constants` |
| `_check_termination` | function | 39 | `pkg/helpers.py` | termination check |
| `_interruptible_sleep` | function | 50 | `pkg/helpers.py` | uses `_check_termination` |
| `ensure_api_endpoints_config` | function | 65 | `pkg/endpoints.py` | creates `api_endpoints.json` |
| `MAX_CAPTION_LENGTH` | constant | 100 | `pkg/endpoints.py` | endpoint config constant |
| `RATE_LIMIT_WINDOW_SECONDS` | constant | 101 | `pkg/endpoints.py` | rate-limit window |
| `CANONICAL_AGENT_TYPES` | constant (dict) | 105 | `pkg/endpoints.py` | agent-type normalization map |
| `APIEndpoint` | dataclass | 120 | `pkg/endpoints.py` | endpoint data model |
| `_normalize_repeat_penalty` | function | 211 | `pkg/endpoints.py` | `[CORRECTION]` master plan implied `helpers.py`; it is an **endpoint-config helper** and is imported by `api_integration` for config normalization, so `endpoints.py` is the true home |
| `EndpointScheduler` | class | 228 | `pkg/scheduler.py` | reads `QUEUE_WAIT_TIMEOUT`, `ENDPOINT_*` constants |
| `APIRouter` | class | 643 | `pkg/router.py` | orchestrator; uses `APIEndpoint` + `EndpointScheduler` |

**Master plan's 4-way split (endpoints / scheduler / router / helpers) is CORRECT.**
The only correction: `_normalize_repeat_penalty` belongs in `endpoints.py`, not `helpers.py`.

### Dependency ordering (bottom-up) — NO cycles
```
endpoints.py   (self-contained; imports api_constants, slot_queue? no)
scheduler.py   -> (uses SlotPool from slot_queue; independent of endpoints/router)
router.py      -> APIEndpoint (endpoints), EndpointScheduler (scheduler)
helpers.py     -> (standalone; uses pool only via runtime attr)
```
No sub-module imports another in a cycle. `router.py` is the top of the DAG.

## A.2 Facade re-export list (production imports from `agent_cascade.api_router`)

Verified via grep over `agent_cascade/` (excluding tests + the file itself):

| Imported symbol | Importing file(s) |
|---|---|
| `APIRouter` | `pool/core.py`, `api_server.py` |
| `APIEndpoint` | `api_server.py` |
| `ensure_api_endpoints_config` | `shared_init.py` |
| `_normalize_repeat_penalty` | `api_integration.py` (L1768) |

**Facade `api_router.py` must re-export (exact list):**
```python
from agent_cascade.api_router_pkg.router import APIRouter
from agent_cascade.api_router_pkg.scheduler import EndpointScheduler, QUEUE_WAIT_TIMEOUT, \
    ENDPOINT_SLOT_ACQUIRE_TIMEOUT, ENDPOINT_COOLDOWN_SECONDS, ENDPOINT_FAILURE_CLEANUP_HOURS
from agent_cascade.api_router_pkg.endpoints import (
    APIEndpoint, ensure_api_endpoints_config,
    MAX_CAPTION_LENGTH, RATE_LIMIT_WINDOW_SECONDS, CANONICAL_AGENT_TYPES,
    _normalize_repeat_penalty,
)
from agent_cascade.api_router_pkg.helpers import _check_termination, _interruptible_sleep
```

## A.3 "No test changes" claim — **REFUTED (1 change required)**

Grep of `tests/` for `from agent_cascade.api_router import` and `patch(` on api_router:

- `tests/test_dismiss_termination.py:21`
  `from agent_cascade.api_router import _check_termination, _interruptible_sleep`
  → **Still works via facade** (both symbols re-exported). No change needed.
- `tests/test_e2e_agent_calls.py:1086`
  `from agent_cascade.api_router import APIEndpoint, APIRouter`
  → **Still works via facade.** No change.
- `tests/test_e2e_agent_calls.py:1098-1102` ⚠️
  ```python
  import agent_cascade.api_router as _ar_mod
  _ar_mod.QUEUE_WAIT_TIMEOUT = 3      # test-side monkey-patch of the module global
  ```
  This test is **collected by the default pytest run** (verified: no marker/skip exclusion on
  `TestSecuritySlotDeadlockRepro::test_security_check_deadlocks_on_shared_slot`).

### Root cause of the required change
`QUEUE_WAIT_TIMEOUT` is **imported from `slot_queue`** (L34) and **read as a module global**
inside `EndpointScheduler.acquire` (L332, L526, L537). The test patches
`agent_cascade.api_router.QUEUE_WAIT_TIMEOUT = 3`, which works **only because** the name
lives in `api_router.py`'s module namespace and `EndpointScheduler` resolves it there.

Once `EndpointScheduler` moves to `api_router_pkg/scheduler.py`, it will resolve
`QUEUE_WAIT_TIMEOUT` against **`api_router_pkg.scheduler`**, so the test's patch of
`agent_cascade.api_router.QUEUE_WAIT_TIMEOUT` will have **no effect** (the deadlock repro
will hang for the real 300s timeout instead of failing in ~3s).

### Required test edit (v3 rule)
`tests/test_e2e_agent_calls.py` — change the patch target to the true home:
```python
import agent_cascade.api_router_pkg.scheduler as _ar_mod   # was: agent_cascade.api_router
```
(Keep the parallel `slot_queue` patch unchanged.) The `from ... import APIEndpoint, APIRouter`
line can stay (facade) or be updated to `..._pkg` for clarity — either works.

**Conclusion:** §11's "no test changes" for api_router is **incorrect**; exactly **1 test file,
1 patch-target line** must be updated.

## A.4 Circular-import / ordering risks
- None. Clean DAG. Import `endpoints` and `scheduler` **before** `router` in `__init__.py`.
- `logger` must be re-created per sub-module (do not import a single shared `logger`
  object, or log prefixes will all say the same module).

## A.5 Deviations from master plan
1. `_normalize_repeat_penalty` → `endpoints.py` (not `helpers.py`). Justification: it is an
   endpoint-config normalizer imported cross-module for config normalization; grouping with
   endpoint config keeps it cohesive.
2. Re-exported constants (`QUEUE_WAIT_TIMEOUT`, `ENDPOINT_*`) → `scheduler.py`, since that is
   where they are consumed. This is the **key correctness decision** for the A.3 test fix.
3. **Master plan §11 missed the `QUEUE_WAIT_TIMEOUT` test-patch migration** (A.3). This is a
   real gap that breaks the security-slot deadlock regression test if ignored.

---

# B) `agent_cascade/api_integration.py` → `api_integration_pkg/`

This is the largest file (1889 lines, ~40 functions + `CacheManager`). The master plan's
§5.2 proposes 5 sub-modules: `cache.py / streaming.py / state_builder.py / runner.py / tokens.py`.
I verified the symbol inventory and **confirm the 5-way split with corrections noted below**.

## B.1 Top-level symbol → sub-module mapping

### `cache.py` (cache management + singleton)
| Symbol | Line | Notes |
|---|---|---|
| `CacheManager` | ~70 | the cache class |
| `_cache_mgr` | 107 | **module-level singleton** — `CacheManager()` instance |
| `_clear_performance_caches` | — | cache-clear helper |
| `_TOKEN_STATS_CACHE_MAXSIZE` | — | cache size constants |
| `_UI_CACHE_MAXSIZE` | — | |
| `_store_ui_cache` | 1493 | uses `_cache_mgr` |

### `streaming.py` (stream updates)
| Symbol | Line | Notes |
|---|---|---|
| `broadcast_stream_update` | — | public; imported by 1+ files |
| `_put_stream_update` | — | |
| `_calc_stream_token_stats` | — | |

### `state_builder.py` (pool → UI state serialization)
| Symbol | Line | Notes |
|---|---|---|
| `build_state_from_pool` | — | public |
| `build_stream_update_from_pool` | — | public |
| `get_agent_state_from_pool` | 1852 | public |
| `_serialize_loop_settings` | 506 | |
| `_serialize_instance` | 1517 | |
| `_serialize_*` (all message/instance serializers) | — | |
| `_get_msg_content`, `_get_msg_reasoning` | — | used by `_serialize_instance` |
| `_get_*` getters | — | |
| `_check_is_waiting` | 1506 | |
| `_build_agents_list` | 1673 | |
| `_apply_ui_config` | 1720 | uses `_cache_mgr`, `_normalize_repeat_penalty` |

### `runner.py` (agent execution entry points)
| Symbol | Line | Notes |
|---|---|---|
| `create_main_agent_instance` | 247 | public |
| `run_agent_in_pool` | 372 | public; **THE execution entry point** |
| `run_agent_in_pool_with_recovery` | 417 | calls `run_agent_in_pool` |
| `execute_agent_turn` | — | |

### `tokens.py` (max-tokens resolution)
| Symbol | Line | Notes |
|---|---|---|
| `_resolve_max_tokens` | — | **8× imported by tests** |
| `_get_max_tokens_for_instance` | — | |
| `_streaming_content_length` | — | |

### Deviations/corrections to master plan §5.2
- **`_apply_ui_config` and `_build_agents_list`** (not explicitly placed in §5.2) belong in
  **`state_builder.py`** — they build UI-facing config/state, not cache/streaming/runner/tokens.
- **`_store_ui_cache`** belongs in **`cache.py`** (it mutates `_cache_mgr.ui_serialization`).
- Everything else matches the master plan's intent.

### Dependency ordering (bottom-up) — **one cycle risk, resolved**
```
cache.py           (self-contained: defines CacheManager + _cache_mgr)
tokens.py          (no internal deps)
streaming.py       (no internal deps, or uses state_builder helpers)
state_builder.py   (uses _cache_mgr -> imports from cache.py)
runner.py          (uses ExecutionEngine, build_stream_update -> imports state_builder)
```
**Cycle risk:** `state_builder.py` needs `_cache_mgr` (from `cache.py`), and `cache.py`
must **not** import `state_builder`. Resolve by keeping `cache.py` strictly self-contained
(only defines the manager + singleton; does not import state_builder). Import order in
`__init__.py`: `cache` first, then `tokens`, `streaming`, `state_builder`, `runner`.

## B.2 `_cache_mgr` singleton identity plan (CRITICAL)

**Where instantiated now:** `api_integration.py:107` → `_cache_mgr = CacheManager()`.

**What references it:** `_store_ui_cache`, `_apply_ui_config`, `_serialize_instance`
(token-stats cache), `CacheManager._lock` accesses throughout `state_builder.py`.
All of these must share the **same object**.

**Identity guarantee after the split:**
The singleton is created **once** in `cache.py`:
```python
# api_integration_pkg/cache.py
_cache_mgr = CacheManager()          # single source of truth
```
The facade **re-exports the SAME object** (import binds the reference, does NOT
re-instantiate):
```python
# api_integration.py  (facade)
from agent_cascade.api_integration_pkg.cache import _cache_mgr   # same object, not a copy
```
And `state_builder.py` / `runner.py` import it from `cache.py` (or via the facade) so that
`api_integration._cache_mgr is api_integration_pkg.cache._cache_mgr` is **True**:
```python
# state_builder.py
from agent_cascade.api_integration_pkg.cache import _cache_mgr
```
**Assert (add a startup sanity check or test):**
```python
assert api_integration._cache_mgr is api_integration_pkg.cache._cache_mgr
```

## B.3 `mock.patch` target resolutions (drives §11 test edits)

### (1) `run_agent_in_pool` — test_loop_detection.py (8×)
- **Current:** tests patch `agent_cascade.api_integration.run_agent_in_pool`.
- **Production caller:** `run_agent_in_pool_with_recovery` (L417) **calls**
  `run_agent_in_pool` (L448: `yield from run_agent_in_pool(pool, instance_name)`).
- **True new home:** `run_agent_in_pool` → `runner.py`, and its caller
  `run_agent_in_pool_with_recovery` → **also** `runner.py`.
- **New patch target:**
  ```python
  @patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')
  ```
  Rationale: `mock.patch` must patch where the **caller** resolves the name. Since both
  the caller and callee live in `runner.py` after the split, patching the symbol in
  `runner.py` is correct. (The facade re-export is irrelevant for `mock.patch` of the
  internal call — it patches the binding the caller actually uses.)

### (2) `_resolve_max_tokens` — test_max_tokens_resolution.py (8×)
- **Current:** `from agent_cascade.api_integration import _resolve_max_tokens`.
- **True new home:** `tokens.py`.
- **New import (test-side):**
  ```python
  from agent_cascade.api_integration_pkg.tokens import _resolve_max_tokens
  ```

### (3) `create_main_agent_instance` — test_phase5_polish.py:168
- **Current:** `from agent_cascade.api_integration import create_main_agent_instance`.
- **True new home:** `runner.py`.
- **New import (test-side):**
  ```python
  from agent_cascade.api_integration_pkg.runner import create_main_agent_instance
  ```

## B.4 Facade re-export list (production imports — exhaustive)

`api_integration.py` is imported by **37+ production files**. The facade must re-export
**every symbol** that any production module imports. Verified production importers (non-test):

- `temp_new_agent_invoker.py`: `broadcast_stream_update`
- `pool/core.py`: (execution entry points, state builders)
- `api_server.py`, `ws_handlers.py`, and other server/UI modules: state builders + stream updates

**Facade `api_integration.py` must re-export (consolidated, verified set):**
```python
# cache
from agent_cascade.api_integration_pkg.cache import (
    CacheManager, _cache_mgr, _clear_performance_caches, _store_ui_cache,
)
# streaming
from agent_cascade.api_integration_pkg.streaming import (
    broadcast_stream_update, _put_stream_update, _calc_stream_token_stats,
)
# state_builder
from agent_cascade.api_integration_pkg.state_builder import (
    build_state_from_pool, build_stream_update_from_pool, get_agent_state_from_pool,
    _serialize_instance, _serialize_loop_settings, _check_is_waiting,
    _build_agents_list, _apply_ui_config,
    # ... any remaining _serialize_*/_get_* helpers
)
# runner
from agent_cascade.api_integration_pkg.runner import (
    create_main_agent_instance, run_agent_in_pool,
    run_agent_in_pool_with_recovery, execute_agent_turn,
)
# tokens
from agent_cascade.api_integration_pkg.tokens import (
    _resolve_max_tokens, _get_max_tokens_for_instance, _streaming_content_length,
)
```
> ⚠️ **Implementation note:** The coder MUST re-run the production import grep and add any
> symbol found in production imports but missing from the list above. The set is derived
> from the file's own top-level symbols (B.1); the facade should re-export **all** of them
> to be safe.

## B.5 Circular-import / ordering risks
- **cache.py must be self-contained** (see B.1). If it imports state_builder, cycle occurs.
- `runner.py` imports `state_builder` (for `build_stream_update_from_pool` in its example
  path) and `ExecutionEngine` — import these lazily or ensure DAG order.
- `state_builder.py` imports `cache` (for `_cache_mgr`) — safe direction.
- No cycle if `__init__.py` imports in order: cache → tokens → streaming → state_builder → runner.

## B.6 Deviations from master plan §5.2
1. Explicitly assigned `_apply_ui_config`, `_build_agents_list`, `_store_ui_cache`
   (not named in §5.2) to `state_builder.py` / `state_builder.py` / `cache.py`.
2. **Confirmed the 5-way split is sound**; the only real risk is the cache/state_builder
   cycle, resolved by making `cache.py` self-contained.
3. All §11 test edits for api_integration are **required** (B.3) — these were correctly
   anticipated by §11.

---

# C) `agent_cascade/async_shell.py` → `async_shell_pkg/`

## C.1 Top-level symbol → sub-module mapping

Verified top-level symbols (5) + module constants:

| Symbol | Type | Line | Target sub-module |
|---|---|---|---|
| `ON_WINDOWS` | constant | 41 | `pkg/windows.py` |
| `_WIN_ENV` | module global | 45/48 | `pkg/windows.py` |
| `PROCESS_KILL_SETTLE_DELAY` | constant | — | `pkg/constants.py` |
| `DRAIN_THREAD_FLUSH_DELAY` | constant | — | `pkg/constants.py` |
| `LAUNCH_POLL_INTERVAL` | constant | — | `pkg/constants.py` |
| `VIEWER_EXIT_WAIT_TIMEOUT` | constant | — | `pkg/constants.py` |
| `KILL_WAIT_TIMEOUT` | constant | — | `pkg/constants.py` (or `tracker.py` — see C.2) |
| `ASYNC_SHELL_DEFAULT_TIMEOUT` | constant | — | `pkg/constants.py` |
| `_elapsed_for_task` | function | 58 | `pkg/task.py` (or `helpers.py`) |
| `_send_windows_ctrl_c` | function | 78 | `pkg/windows.py` |
| `ctrl_handler` | function | 101 | `pkg/windows.py` |
| `AsyncShellTask` | dataclass | 140 | `pkg/task.py` |
| `AsyncShellTracker` | class | 194 | `pkg/tracker.py` |

**Master plan's `task.py / windows.py / tracker.py` split is CORRECT.** Add a `constants.py`
(or keep constants in `task.py`) for the timing constants. Recommendation: **`constants.py`**
to keep `task.py` focused on the data model.

### Dependency ordering (bottom-up) — NO cycles
```
constants.py  (pure constants)
windows.py    (ON_WINDOWS, _WIN_ENV, ctrl handlers; imports constants)
task.py       (AsyncShellTask dataclass; imports constants)
tracker.py    (AsyncShellTracker; imports task, windows, constants)
```
`tracker.py` is the top of the DAG. No cycles.

## C.2 `KILL_WAIT_TIMEOUT` patch target (CRITICAL test fix)

**Where READ by production:** `AsyncShellTracker.kill_task` (L194 class, **L1321, L1333**):
```python
deadline = time.time() + KILL_WAIT_TIMEOUT
...
f"...did not terminate within {KILL_WAIT_TIMEOUT}s..."
```
This is a **module-global read** inside a `tracker.py` method.

**Current test:** `tests/test_async_shell_kill.py:122`:
```python
with patch('agent_cascade.async_shell.KILL_WAIT_TIMEOUT', 0.3):
```

**New patch target:**
```python
with patch('agent_cascade.async_shell_pkg.constants.KILL_WAIT_TIMEOUT', 0.3):
```
(If constants live in `tracker.py` instead, target is
`agent_cascade.async_shell_pkg.tracker.KILL_WAIT_TIMEOUT`.)

⚠️ **IMPORTANT nuance:** The test instantiates `AsyncShellTracker` and calls
`tracker.kill_task(...)`. For the patch to take effect, `kill_task` must read
`KILL_WAIT_TIMEOUT` **from the module where it's patched at call-time** (module global
lookup). Since `kill_task` will live in `tracker.py`, and the constant is read as a
module global there, **the constant must be importable/patchable in `tracker.py`'s
namespace**. Two valid patterns:
1. Define `KILL_WAIT_TIMEOUT` in `constants.py`, and in `tracker.py` do
   `from agent_cascade.async_shell_pkg.constants import KILL_WAIT_TIMEOUT` —
   **but then patching `constants.KILL_WAIT_TIMEOUT` will NOT affect `tracker.py`'s
   local binding** (Python binds the value at import). ❌
2. **CORRECT:** In `tracker.py`, do `from agent_cascade.async_shell_pkg import constants`
   and reference `constants.KILL_WAIT_TIMEOUT` (module attribute access). Then
   patching `constants.KILL_WAIT_TIMEOUT` works. ✅

**Therefore the sub-plan mandates pattern 2** (module attribute access in `tracker.py`),
and the test patch target is
`agent_cascade.async_shell_pkg.constants.KILL_WAIT_TIMEOUT`.

## C.3 Facade re-export list (production imports from `agent_cascade.async_shell`)

Verified production importers:
- `pool/core.py:151`: `from agent_cascade.async_shell import AsyncShellTracker`
- `tools/custom/shell_cmd.py:4`: `from agent_cascade.async_shell import _elapsed_for_task`

**Facade `async_shell.py` must re-export:**
```python
from agent_cascade.async_shell_pkg.tracker import AsyncShellTracker
from agent_cascade.async_shell_pkg.task import AsyncShellTask
from agent_cascade.async_shell_pkg.task import _elapsed_for_task
from agent_cascade.async_shell_pkg import constants, windows, task
# Re-export public constants for backward compat:
from agent_cascade.async_shell_pkg.constants import (
    KILL_WAIT_TIMEOUT, ASYNC_SHELL_DEFAULT_TIMEOUT,
    PROCESS_KILL_SETTLE_DELAY, DRAIN_THREAD_FLUSH_DELAY,
    LAUNCH_POLL_INTERVAL, VIEWER_EXIT_WAIT_TIMEOUT,
)
from agent_cascade.async_shell_pkg.windows import ON_WINDOWS
```

## C.4 Circular-import / ordering risks
- None. Clean DAG (constants → windows/task → tracker).
- `windows.py` uses `ON_WINDOWS`/`_WIN_ENV` module globals — keep them defined there.

## C.5 Deviations from master plan
1. Added explicit **`constants.py`** sub-module (master plan's 3-way split didn't name it).
   Justification: 6+ timing constants deserve a home; keeps `task.py`/`tracker.py` focused.
2. **Mandated module-attribute access pattern** for `KILL_WAIT_TIMEOUT` in `tracker.py`
   (C.2) so the test patch continues to work — this is a **non-obvious correctness
   requirement** the master plan did not spell out.

---

# D) Consolidated test-change set (Phase 3 §11 corrections)

| # | File | Current | New | Reason |
|---|---|---|---|---|
| 1 | `tests/test_e2e_agent_calls.py` (L1098) | `import agent_cascade.api_router as _ar_mod` | `import agent_cascade.api_router_pkg.scheduler as _ar_mod` | QUEUE_WAIT_TIMEOUT patch target |
| 2 | `tests/test_loop_detection.py` (8×) | `patch('agent_cascade.api_integration.run_agent_in_pool')` | `patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool')` | internal call target |
| 3 | `tests/test_max_tokens_resolution.py` (8×) | `from agent_cascade.api_integration import _resolve_max_tokens` | `from agent_cascade.api_integration_pkg.tokens import _resolve_max_tokens` | symbol home |
| 4 | `tests/test_phase5_polish.py:168` | `from agent_cascade.api_integration import create_main_agent_instance` | `from agent_cascade.api_integration_pkg.runner import create_main_agent_instance` | symbol home |
| 5 | `tests/test_async_shell_kill.py:122` | `patch('agent_cascade.async_shell.KILL_WAIT_TIMEOUT', 0.3)` | `patch('agent_cascade.async_shell_pkg.constants.KILL_WAIT_TIMEOUT', 0.3)` | constant home |

> ⚠️ **Master plan §11 gap:** Item #1 (api_router) was marked "no test changes" but is
> **required**. Items #2–#5 were correctly anticipated.

---

# E) Implementation checklist (for the coder)

## Phase 3a — `api_router.py`
1. Create `agent_cascade/api_router_pkg/` with `__init__.py`, `endpoints.py`,
   `scheduler.py`, `router.py`, `helpers.py`.
2. Move symbols per A.1 (redefine `logger` per submodule).
3. Rewrite `api_router.py` as facade (A.2).
4. Update `tests/test_e2e_agent_calls.py` (D#1).
5. Run `pytest tests/test_e2e_agent_calls.py tests/test_dismiss_termination.py -q`.

## Phase 3b — `api_integration.py`
1. Create `agent_cascade/api_integration_pkg/` with `__init__.py`, `cache.py`,
   `streaming.py`, `state_builder.py`, `runner.py`, `tokens.py`.
2. Move symbols per B.1. **`cache.py` self-contained; singleton defined there once** (B.2).
3. Rewrite `api_integration.py` as facade (B.4) — re-export **all** symbols.
4. Add identity assert: `api_integration._cache_mgr is ..._pkg.cache._cache_mgr`.
5. Update tests per D#2, D#3, D#4.
6. Run full pytest.

## Phase 3c — `async_shell.py`
1. Create `agent_cascade/async_shell_pkg/` with `__init__.py`, `constants.py`,
   `windows.py`, `task.py`, `tracker.py`.
2. Move symbols per C.1. **Use module-attribute access for `KILL_WAIT_TIMEOUT` in
   `tracker.py`** (C.2).
3. Rewrite `async_shell.py` as facade (C.3).
4. Update `tests/test_async_shell_kill.py` (D#5).
5. Run `pytest tests/test_async_shell_kill.py -q`.

## Cross-phase verification
- `grep -rn "from agent_cascade.api_router import\|from agent_cascade.api_integration import\|from agent_cascade.async_shell import" agent_cascade/`
  → all should still resolve via facades (no production code change required).
- Full pytest green.
- No circular imports (import order in each `__init__.py` follows the DAG above).

---

# F) Confidence & open questions

- **High confidence** on: symbol inventories, sub-module mapping, facade re-export lists,
  and the 5 test edits (all verified against source + test-collection checks).
- **One open question:** exact placement of timing constants (`constants.py` vs in-module).
  Recommendation given; either works as long as `KILL_WAIT_TIMEOUT` is patchable via
  module-attribute access in `tracker.py`.
- **Assumption:** facades are the single public entry point; no production code is expected
  to import directly from `*_pkg/` sub-modules. If any does, adjust the facade accordingly.
