# Phase 2 Compliance Fixes — Lessons Learned

## Date: 2026-05-26
## Agent: Phase2Fixer

## Summary of Fixes Applied

### P0 — CRITICAL: Compression rollback path silently fails (core.py → core_fixed.py)

**Root Cause**: `agent_pool.instance_conversations` is a **read-only property** that returns a **new dict** every time it's accessed. Any read or write to the returned dict is silently lost.

**Fixes (3 locations)**:
1. Line ~256: Changed `agent_pool.instance_conversations.get(target_agent_name)` → `agent_pool.instances[target_agent_name].conversation`
2. Line ~275: Changed `agent_pool.instance_conversations[target_agent_name] = new_history` → `agent_pool.instances[target_agent_name].conversation = new_history`
3. Line ~327 (rollback): Same pattern as #2

**Lesson**: Always check if a property returns a cached reference or creates a new object each call. Read-only properties that return new objects are particularly dangerous because writes appear to succeed silently.

### P1 — Missing `caller_history` parameter in `_execute_agent_sync` (execution_engine.py)

**Fixes (2 locations)**:
4. Line ~775: Added `caller_history: List[Message]` to signature per DESIGN_REWRITE.md §3.2
5. Line ~609: Updated call site to pass `messages` as caller_history argument

**Lesson**: Always cross-reference implementation with design spec when adding new parameters. The parameter was in the spec but missing from the implementation.

### P1 — Missing `agent_obj=self` and removing `messages=messages` (execution_engine.py)

**Fixes (1 location)**:
6. Line ~552-557: Added `agent_obj=self` to `_call_tool` invocation per design spec
   - **IMPORTANT**: Initially removed `messages=messages` per the spec, but this caused a regression — file-access tools (fncall_agent.py:128) assert on its presence
   - **Resolution**: Kept BOTH `agent_obj=self` AND `messages=messages` — the design spec was incomplete; both parameters are needed at runtime

**Lesson**: Design specs can be incomplete. Always test changes against all tool types, especially those with file access requirements. The DESIGN_REWRITE.md didn't account for the fact that some tools need `messages` in kwargs.

### P2 — `latest_marker_index` never updated after compression (execution_engine.py)

**Fixes (1 location)**:
7. Lines ~230-239: After successful compression, iterate conversation to find `<context_summary>` marker and set `instance.latest_marker_index = idx`

### P2 — Fragile summary extraction (execution_engine.py)

**Fixes (1 location)**:
8. Lines ~215-244: Changed from calling `_handle_compress_context` (returns string, parses tags) to calling `compress_context` directly (returns CompressResult with `result.summary_text` and `result.success`)

**Lesson**: Use structured return types when available instead of parsing strings/tags. The compression module already computes summary_text — no need to re-extract it from conversation messages.

## Files Modified
- `agent_cascade/compression/core_fixed.py` (3 changes)
- `agent_cascade/execution_engine_fixed.py` (5 changes + 1 regression fix)

## Review Status: ✅ PASSED by reviewer_phase2fixer