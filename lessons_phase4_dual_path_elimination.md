# Phase 4: Dual Path Elimination — Lessons & Notes

**Date:** 2026-05-26
**Module:** `agent_cascade/run_agent_unified.py`
**Status:** Implemented (pending review)

## What Was Done

Created `agent_cascade/run_agent_unified.py` — a drop-in replacement for the dual-path code in api_server.py. The old code had:

1. **Dual execution paths**: Main agent ran through `run_agent_thread() → agent_runner.run()` using `session['history']`, while sub-agents ran through `_stream_sub_agent_call()`.
2. **Dual state sources**: `build_state()` read from `session['history']` AND from pool for sub-agents, depending on a `USE_UNIFIED_STATE` flag.
3. **Split token counting**: Session-level incremental caches (`_cached_hist_stats`, `_cached_r_stats`) mixed with pool-level tracking.

The new code eliminates ALL of this:

1. **Single execution path**: `run_agent_thread_unified()` runs through `ExecutionEngine.run()` via `run_agent_in_pool_with_recovery()`.
2. **Single state source**: State building reads ONLY from `pool.instances[name].conversation` via `build_state_from_pool()` in api_integration.py (enhanced with extra frontend fields).
3. **Unified token counting**: `get_token_stats_unified()` calculates tokens on the working set (after compression slicing) using the LRU-cached `get_history_stats()`.

**Key structural decision**: Rather than having a separate `build_state_unified()` function (which was dead code), the extra frontend fields (agents, current_model, telemetry, default_workspace, is_waiting, api_router) were merged into `api_integration.py`'s `build_state_from_pool()`. This ensures a single source of truth — there is only ONE state-building function.

## Key Design Decisions

### 1. NO `session['history']` — Single Source of Truth
All state comes from `pool.instances[name].conversation`. The new code NEVER reads from session-level history. This is enforced at the architectural level — the functions don't even accept a session dict parameter.

### 2. NO Dual-Path Flags
The old code used `USE_UNIFIED_STATE` and `USE_UNIFIED_ARCHITECTURE` flags to toggle between old and new paths. The new code has no such flags — it IS the unified path. The old code remains in api_server.py until integration is complete.

### 3. Drop-in Replacement, Not Rewrite
The new functions are designed as drop-in replacements:
- `run_agent_thread_unified()` takes similar parameters to `run_agent_thread()` (pool, instance_name, config, send_queue, loop)
- `build_state_unified()` returns the same dict structure as `build_state()` for frontend compatibility
- Both maintain the same broadcasting pattern via asyncio send_queue

### 4. Simpler Token Counting
The old code had complex incremental caching at the session level:
- `_cached_hist_stats` with increment/decrement logic
- `_cached_r_stats` with delta estimation during streaming
- `_last_resp_content_len` tracking for streaming token estimation

The new approach is simpler: just call `get_history_stats()` on the active working set. This works because:
- `get_history_stats()` has built-in LRU caching (512 entries) for Message objects
- Dict messages cache `_tokens`/`_words` directly in place
- Repeated calls on unchanged messages are fast

Trade-off: Slightly more computation per tick, but MUCH simpler code and no risk of cache drift.

### 5. Throttled Loop Detection
Loop detection runs every 10 ticks (same as the old code). This is a secondary check — the primary loop detection happens inside `ExecutionEngine._pre_llm_checks()`. The streaming-level check catches loops during generation before they propagate to the engine.

## Function Reference

### `run_agent_thread_unified(pool, instance_name, system_message_content, ui_cfg, send_queue, loop)`
Drop-in replacement for `run_agent_thread()`. Runs the main agent through the unified ExecutionEngine with loop recovery. Creates the instance if it doesn't exist, applies UI config, runs the engine, and broadcasts state updates via the async send_queue.

### `build_state_unified(pool, primary_instance_name, responses=None, generating=False)`
**REMOVED** — Merged into api_integration's `build_state_from_pool()`. The extra frontend fields (agents, current_model, telemetry, default_workspace, is_waiting, api_router) were added to build_state_from_pool to maintain a single source of truth. Callers should use `build_state_from_pool()` from api_integration.

### `get_token_stats_unified(pool, instance_name)`
Unified token counting helper. Returns dict with: history_tokens, history_words, total_messages, active_messages, max_tokens. Uses LRU-cached `get_history_stats()` on the working set (after compression slicing).

### `get_token_usage_percentage(pool, instance_name)`
Convenience wrapper returning 0-100 percentage. Used for compression threshold checks (95% force, 85% warn).

## Important Patterns

### C3 Fix: Snapshot Before Iteration
All state building functions take `dict(pool.instances)` before iterating to prevent RuntimeError when agents are added/removed concurrently during execution.

### M1/M4 Fix: Derive Session Name from Root Instance
Session name is derived by finding the first instance with `parent_instance=None`, not stored in a separate session variable.

### Thread Safety via send_queue
State updates are sent via `asyncio.run_coroutine_threadsafe()` to the async send_queue, avoiding direct cross-thread WebSocket writes. The execution runs in a background thread while WebSocket operations happen on the event loop.

### Error Handling
- `run_agent_thread_unified` catches all exceptions (except KeyboardInterrupt/SystemExit), logs them, and sends an error state via the stream update path.
- Token stats calculation has graceful fallbacks — if `get_history_stats()` fails, uses rough estimation (`len * 100`).

## Differences from Old Code

| Feature | Old (api_server.py) | New (run_agent_unified.py) |
|---------|-------------------|--------------------------|
| State source | `session['history']` + pool | ONLY `pool.instances[name].conversation` |
| Execution path | `agent_runner.run()` + `_stream_sub_agent_call()` | `ExecutionEngine.run()` via api_integration |
| Token counting | Incremental session-level caches | Simple LRU-cached `get_history_stats()` |
| Dual-path flags | `USE_UNIFIED_STATE`, `USE_UNIFIED_ARCHITECTURE` | None — single path |
| Loop recovery | Manual retry loop with surgical rollback | Via `run_agent_in_pool_with_recovery()` |
| UI config | Inline sanitize_cfg() + NON_LLM_KEYS filter | Delegated to `_apply_ui_config()` in api_integration |

## For Phase 5 (Integration into api_server.py)

The actual integration requires:
1. Replace `run_agent_thread()` calls with `run_agent_thread_unified()`
2. Replace `build_state()` calls with `build_state_unified()`
3. Remove `session['history']` assignment and usage
4. Remove the `USE_UNIFIED_STATE` / `USE_UNIFIED_ARCHITECTURE` feature flags
5. Clean up the old incremental token caching code (`_cached_hist_stats`, etc.)
6. Test WebSocket streaming end-to-end

## Dependencies

- `agent_cascade/api_integration.py` — Phase 3 bridge module (create_main_agent_instance, run_agent_in_pool_with_recovery, build_state_from_pool, build_stream_update_from_pool)
- `agent_cascade/execution_engine.py` or `execution_engine_fixed.py` — The ExecutionEngine class
- `agent_cascade/agent_pool.py` — The AgentPool class and its methods
- `agent_cascade/agent_instance.py` — AgentInstance dataclass, LoopDetectedError
- `agent_cascade/utils/utils.py` — get_history_stats() with LRU caching
- `agent_cascade/loop_detection.py` — detect_loop() function (created in Phase 4)

## Review Findings & Fixes Applied

| Issue | Fix | Status |
|-------|-----|--------|
| Missing loop_detection module | Created agent_cascade/loop_detection.py as standalone module per DESIGN_REWRITE §7.1 | ✅ Fixed |
| Race condition: active_stack.clear() without lock | Wrapped in pool._execution._state_lock | ✅ Fixed |
| Thread-unsafe _last_resp_len via function attribute | Changed to local mutable dict exec_state | ✅ Fixed |
| Dead code: current_stack variable | Removed entirely | ✅ Fixed |
| _detect_loop_in_instance only logged, no action | Now raises LoopDetectedError (requires explicit handler in run_agent_thread_unified to reach recovery wrapper) | ✅ Fixed |
| LoopDetectedError caught by wrong handler | Added explicit `except LoopDetectedError: raise` before generic Exception handler | ✅ Fixed |
| Duplicate messages in loop detection during streaming | Use instance.conversation directly since responses already appended after Phase 4 yield | ✅ Fixed |
| Dead except LoopDetectedError block in _detect_loop_in_instance | Removed — just let the exception propagate | ✅ Fixed |
| COMPRESSION_MARKER/re imported inside functions | Moved to module-level imports | ✅ Fixed |
| Regex-based compression summary extraction | Now prefers instance.compression_summary, regex fallback only if empty | ✅ Fixed |
| Silent asyncio.run_coroutine_threadsafe failures | Added explanatory comment about intentional design | ✅ Fixed |
| Error handling flow unclear | Added clarifying comment about LoopDetectedError vs other errors | ✅ Fixed |
| Missing thread safety reading instance.conversation | Added _compression_lock in _detect_loop_in_instance | ✅ Fixed |
| **LoopDetectedError swallowed by generic handler** (CRITICAL) | Added explicit `except LoopDetectedError: raise` before generic Exception in _detect_loop_in_instance — was silently caught and logged as debug, breaking streaming loop detection entirely | ✅ Fixed |
| **build_state_unified dead code** (dual-function confusion) | Merged extra fields (agents, current_model, telemetry, default_workspace, is_waiting, api_router) into api_integration's build_state_from_pool. Removed build_state_unified entirely | ✅ Fixed |
| **Duplicate utility functions** (_serialize_instance_simple, _get_approvals_safe, _build_agents_list) | Removed from run_agent_unified.py; now imported from api_integration.py (added _build_agents_list there) | ✅ Fixed |
| **C3 race condition in streaming path** (lines 164-172) | Take snapshot of pool.instances before iterating in run_agent_thread_unified | ✅ Fixed |
| **Final broadcast type mismatch** (sent 'state' instead of 'done') | Changed to 'type': 'done' + added 'instance_halted' field for frontend compatibility | ✅ Fixed |
| **Dead code: sub_agents_data computed but never used** | Removed — build_stream_update_from_pool handles sub-agents internally | ✅ Fixed |
| Unused imports (copy, re, Iterator, SYSTEM, USER, AgentInstance) | Cleaned up all unused imports | ✅ Fixed |

## Design Doc References

- DESIGN_REWRITE.md §3.1 — Execution Engine design
- DESIGN_REWRITE.md §4.2 — Results Flow to UI (build_state examples)
- DESIGN_REWRITE.md §5.1 — Single Source of Truth
- DESIGN_REWRITE.md §5.2 — API Server State Broadcasting
- DESIGN_REWRITE.md §7.2 — Unified Loop Recovery