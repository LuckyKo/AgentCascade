# Phase 1 Compression Foundation — Fix Notes

## Date: 2026-05-21
## Status: All fixes applied and reviewed ✅

---

## Summary of Issues Fixed

### 🔴 CRITICAL #1: `get_compression_target_set()` doesn't exist
**Status:** Method already existed in agent_pool.py (line 562). No change needed for the method itself.

**Additional improvement:** Extracted duplicated marker-finding logic into a shared `find_last_marker()` static method in AgentPool. Both `slice_history_for_llm` and `get_compression_target_set` now use this shared helper instead of duplicating backward-scan code.

### 🔴 CRITICAL #2: Use `call_agent` pattern instead of direct `comp_agent.run()`
**File:** `agent_cascade/compression/agent_invoker.py`
**Fix:** Rewrote `invoke_compression_agent()` to accept an optional `orchestrator` parameter. When provided and has `_stream_sub_agent_call`, uses the call_agent pattern (generator iteration via `_stream_sub_agent_call`). Falls back to direct `comp_agent.run()` when no orchestrator is available (e.g., forced compression from API server).

**Key design decisions:**
- Added `hasattr(orchestrator, '_stream_sub_agent_call')` guard — prevents AttributeError if orchestrator lacks the method
- Added polling safeguard (`max_polls = 1000`) to prevent infinite loops if sub_agent_state isn't populated
- Captures `StopIteration.value` as fallback for summary extraction when polling misses state updates
- Falls back gracefully through three layers: final_msgs → subagent_return_value → RuntimeError

### 🔴 CRITICAL #3: Logger `insert_compression_marker` missing
**File:** `agent_cascade/compression/core.py`, lines 249-259
**Fix:** Added `hasattr(logger_inst, 'insert_compression_marker')` guard before calling the method. Logs a warning with agent name if unavailable. Compression proceeds normally — marker logging is non-fatal.

### 🟠 MAJOR #4: Thread-safety documentation
**File:** `agent_cascade/compression/core.py`, lines 212-213
**Fix:** Added comment at pool mutation block documenting single-threaded design and the halt-all-agents mechanism that enforces it.

### 🟠 MAJOR #5: Unreachable force override code
**File:** `agent_cascade/compression/core.py`, lines 134-136
**Fix:** Replaced dead `if force and target_discard_count <= 0` block with assertion: `assert target_discard_count >= 1, ...`. The assertion documents the contract that `compute_discard_count(force=True)` guarantees ≥1.

### 🟠 MAJOR #6: Multi-modal summary extraction
**File:** `agent_cascade/compression/core.py`, lines 149-170
**Fix:** Replaced fragile raw content extraction with `extract_text_from_message()` which handles both string and multi-modal list content via `format_as_text_message`.

### 🟡 MINOR #7: Fraction validation
**File:** `agent_cascade/compression/core.py`, lines 50-60
**Fix:** Added early check: `if not 0.0 <= fraction <= 1.0` returns failure CompressResult with descriptive error.

### 🟡 MINOR #10: Agent name in logger warning
**File:** `agent_cascade/compression/core.py`, lines 263-265
**Fix:** Changed warning to include `target_agent_name`.

### 🟡 MINOR #12: Return annotation on rebuild_working_set
**Status:** Already present — no change needed.

---

## Import Consolidation (Reviewer Finding)
Moved `extract_text_from_message` import from two inline locations (inside try/except blocks) to module level in core.py (line 10). This eliminates duplicate imports and makes the dependency explicit.

---

## Files Modified
1. `agent_cascade/compression/core.py` — Main compression entry point
2. `agent_cascade/compression/agent_invoker.py` — Compression agent invocation (major rewrite)
3. `agent_pool.py` — Added `find_last_marker()` static method, updated callers
4. `agent_cascade/compression/helpers.py` — No changes needed (Minor #12 already fixed)

---

## Important Architecture Notes

### The call_agent Pattern
- `_stream_sub_agent_call` is a generator that yields intermediate state for WebUI visibility
- It handles session tracking, async message injection, and auto-continue
- For compression, we iterate it synchronously (not via `yield from`) since `compress_context()` is not itself a generator
- The polling loop reads `sub_agent_state[comp_state_key]['messages']` to capture final messages

### Fallback Strategy
The code has three layers of fallback for compression agent invocation:
1. **call_agent pattern** (preferred) — when orchestrator available with `_stream_sub_agent_call`
2. **Direct comp_agent.run()** — when no orchestrator (e.g., API server forced compression)
3. **subagent_return_value** — when polling misses state updates during call_agent path

### Thread Safety
Compression is single-threaded by design. Forced compression calls `halt_all_instances()` before running, preventing concurrent pool mutations. This is documented at the mutation block in core.py.