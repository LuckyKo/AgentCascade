# Phase 3: API Integration — Lessons & Notes

**Date:** 2026-05-26
**Module:** `agent_cascade/api_integration.py`
**Status:** Completed (reviewed and approved)

## What Was Done

Created `agent_cascade/api_integration.py` — a thin bridge module between the API server (WebSocket/REST) and the new unified ExecutionEngine. This eliminates the dual-path execution model where:
- Main agent ran through `run_agent_thread() → agent_runner.run()` using `session['history']`
- Sub-agents ran through `_stream_sub_agent_call()`

## Key Design Decisions

### 1. NO `session['history']` — Single Source of Truth
All state comes from `pool.instances[name].conversation`. The API integration module NEVER holds its own copy of conversation history. It always reads from the pool.

### 2. Main Agent is Just Another Instance
`create_main_agent_instance()` creates the orchestrator with `parent_instance=None` — no special execution path. Same `ExecutionEngine.run()` loop for everyone.

### 3. Thin Bridge, Not a Rewrite
The module provides functions that the API server CAN call, but doesn't replace api_server.py yet. The old code remains functional during transition. Phase 4 will do the actual replacement in api_server.py.

## Function Reference

### `create_main_agent_instance(pool, instance_name, system_message_content, ...)`
Creates the main agent as an AgentInstance with parent_instance=None. If conversation is provided (session restore), system message should already be present. Otherwise, it prepends a SYSTEM message.

### `run_agent_in_pool(pool, instance_name)`
Core execution function. Creates ExecutionEngine, calls engine.run(instance), yields List[Message]. Raises KeyError if instance not found. Propagates LoopDetectedError for recovery at caller level.

### `run_agent_in_pool_with_recovery(pool, instance_name, max_auto_retries=3, ...)`
Wrapper with automatic loop detection recovery. Catches LoopDetectedError, does surgical rollback (deletes pop_count messages), injects a hint, and retries. Also catches non-loop exceptions (LLM failures, tool crashes) — yields error state instead of crashing the generator.

### `build_state_from_pool(pool, instance_name, responses=None, generating=False)`
Builds full state snapshot for initial broadcast. Reads from pool.instances[name].conversation. Takes snapshot of pool.instances to avoid RuntimeError during concurrent add/remove (C3 fix). Returns dict with messages, sub_agents, active_stack, approvals, tokens, etc.

### `build_stream_update_from_pool(pool, instance_name, responses=None)`
Builds lightweight streaming delta. Only serializes changing response messages — history is already on client. Includes sub_agents, current_model, and telemetry fields for frontend compatibility with the old build_stream_update() format.

### `execute_agent_turn(pool, instance_name, user_message_content, ui_cfg=None)`
End-to-end WebSocket message handler: appends user message → applies UI config → runs engine → yields responses. Thread-safe via `_compression_lock` around conversation append.

### `get_agent_state_from_pool(pool, instance_name)`
Query current state for any agent instance. Replaces get_agent_state() which had dual-track logic (root → session['history'], sub-agent → pool.sub_agent_state).

## Important Patterns

### C3 Fix: Snapshot Before Iteration
All state building functions take `dict(pool.instances)` before iterating to prevent RuntimeError when agents are added/removed concurrently.

### M1/M4 Fix: Derive Session Name from Root Instance
Session name is derived by finding the first instance with parent_instance=None, not stored in a separate session variable.

### Token Calculation
Uses `pool.slice_history_for_llm()` to get the active working set (post-compression), then calculates tokens on that slice — same as build_state did before.

### UI Config Sanitization
`_apply_ui_config()` sanitizes numeric values and filters out non-LLM keys before applying to template.llm.generate_cfg. Uses `copy.deepcopy()` to avoid mutating shared template state (prevents multi-session interference). max_turns is in both the ints list (for sanitization) AND NON_LLM_KEYS (filtered from LLM, applied separately to instance.max_turns).

### Thread Safety
`execute_agent_turn` uses `instance._compression_lock` around conversation append. This protects against concurrent reads during `_setup_turn()` snapshot. Lock scope is limited to the append — the full execution flow runs outside the lock (acceptable for single-threaded async model, but design debt if multi-threaded WebSocket handling is ever introduced).

### Performance
`_get_max_tokens_for_instance()` is a module-level helper that avoids creating ExecutionEngine instances in the hot path of state building. Single snapshot (`msgs`) reused for both message list and `slice_history_for_llm`.

## Review Findings & Fixes Applied

| Issue | Fix | Status |
|-------|-----|--------|
| Unhandled exceptions in recovery wrapper | Added `except Exception` handler | ✅ Fixed |
| Missing fields (sub_agents, current_model, telemetry) in stream update | Added all three fields | ✅ Fixed |
| Shared template state mutation in _apply_ui_config | Used copy.deepcopy() | ✅ Fixed |
| Redundant ExecutionEngine instantiation | Added _get_max_tokens_for_instance helper | ✅ Fixed |
| Thread safety around conversation append | Added _compression_lock guard | ✅ Fixed |
| seed silently discarded in NON_LLM_KEYS | Removed 'seed' from NON_LLM_KEYS | ✅ Fixed |
| LoopDetectedError lazy import | Moved to top-level import | ✅ Fixed |
| Duplicate list copy for slice_history_for_llm | Reused msgs snapshot | ✅ Fixed |

## For Phase 4 (Actual api_server.py Replacement)

The actual replacement in api_server.py requires:
1. Replace `run_agent_thread()` calls with `execute_agent_turn()` or `run_agent_in_pool_with_recovery()`
2. Replace `build_state()` calls with `build_state_from_pool()`
3. Replace `build_stream_update()` calls with `build_stream_update_from_pool()`
4. Remove `session['history']` entirely — all state from pool
5. Handle the transition: old code uses feature flags (USE_UNIFIED_STATE, USE_UNIFIED_ARCHITECTURE)

## Dependencies

- `agent_cascade/execution_engine.py` — The ExecutionEngine class (use _fixed.py version as reference)
- `agent_cascade/agent_pool.py` — The AgentPool class and its methods
- `agent_cascade/agent_instance.py` — AgentInstance dataclass, LoopDetectedError
- `agent_cascade/utils/utils.py` — get_history_stats() for token counting
- `agent_cascade/utils/tokenization_qwen.py` — count_tokens() for token estimation

## Design Doc References

- DESIGN_REWRITE.md §3.1 — Execution Engine design
- DESIGN_REWRITE.md §4.2 — Results Flow to UI (build_state examples)
- DESIGN_REWRITE.md §5.1 — Single Source of Truth
- DESIGN_REWRITE.md §5.2 — API Server State Broadcasting