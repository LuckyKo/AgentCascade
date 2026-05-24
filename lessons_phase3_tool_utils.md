# Phase 3 - Message Loop Unification: Lessons & Notes

## Step B: Shared __USE_PREV_ARG__ Resolver — Complete ✅

### File Created
- `agent_cascade/tool_utils.py` — shared utility for resolving `__USE_PREV_ARG__` placeholders

## Step C: Wire Resolver into Streaming Path — Complete ✅

### File Modified
- `agent_orchestrator.py` — added placeholder resolution to STREAMING_TOOLS dispatch path

### Changes Made (agent_orchestrator.py)
1. **Import** at line 56: `from config.unified import USE_UNIFIED_LOOP`
2. **Lines 1398-1407**: After `parsed_args` is obtained, calls `resolve_prev_arg_placeholders()` — gated by `USE_UNIFIED_LOOP`. When False (default), behavior unchanged.
3. **Lines 1409-1413**: If resolution fails, sets `tool_result = prev_arg_error` and skips sub-agent call. Error flows to generic truncation/post-execution handling.
4. **Lines 1444-1445, 1448-1450**: Changed `_stream_sub_agent_call()` calls from passing raw `tool_args` to resolved `parsed_args`.

### Key Design Decisions
- **Gate by USE_UNIFIED_LOOP**: When False, prev_arg_error is always None — no behavioral change. This allows safe testing with the flag off.
- **prev_arg_error initialized in both branches**: The else branch sets it to None when USE_UNIFIED_LOOP is False, preventing NameError.
- **All three execution paths receive resolved args**: parallel (submit_task), sequential-fallback (_stream_sub_agent_call), and synchronous-streaming (_stream_sub_agent_call).
- **Lazy import** of resolve_prev_arg_placeholders inside the conditional — avoids import overhead when unified mode is off.

### Review History
- Step B: 3 iterations, approved by reviewer_phase3b
- Step C: 2 iterations, approved by reviewer_phase3c
- Step D: 1 iteration, approved by reviewer_phase3d

## Step D: Replace Inline Resolver in Non-Streaming Path — Complete ✅

### File Modified
- `agent_orchestrator.py` — replaced inline placeholder resolution (lines 1467-1501) with shared resolver call in the non-streaming dispatch path

### What Changed
The ~30-line inline code block that scanned for `"__USE_PREV_ARG__"` values, looked up caches, and mutated `tool_args` in-place was replaced with a single call to `resolve_prev_arg_placeholders()`. The JSON parsing step (lines 1456-1465) and downstream tool execution/caching (lines 1482-1528) were untouched.

### Key Observations
- **Behavioral improvement:** Resolved values are now deepcopied instead of shallow-referenced. The original code mutated `tool_args` in-place, which meant the cache and caller shared mutable objects. The shared resolver returns a new dict via `copy.deepcopy()`, eliminating this risk.
- **More defensive instance_scope:** Changed from bare `self.session_name` to `self.session_name if hasattr(self, 'session_name') else 'root'` — consistent with the streaming path pattern (Step C).
- **skip_execution pattern preserved:** The downstream code still uses `skip_execution = True/False` to gate `_call_tool()` execution, so no changes to the tool execution block were needed.

### Key Design Decisions
1. **Lock is parameterized** (`lock: Optional[threading.Lock] = None`) — callers that already hold the lock pass `None` to avoid deadlock. This avoids the implicit deadlock risk of acquiring `_state_lock` inside the function when a caller might already hold it.

2. **Full deepcopy on input dict** — not just shallow copy. Resolved values from cache are also deepcopied. This prevents cache mutation via shared references (the original inline code had this bug).

3. **Return contract**: `(resolved_args, error_message)` tuple. On error, returns the UNMODIFIED original args — callers must check `error_message` before using the args.

4. **Non-dict passthrough** — if tool_args isn't a dict, it passes through unchanged with no error. Documented in docstring.

### What Was NOT Done (By Design)
- The inline code in `agent_orchestrator.py` lines 1433–1478 was **not** replaced yet — that's Step C.
- Write sites to `last_tool_args` (lines 1522-1528 in agent_orchestrator.py) are **not** lock-protected. This will be addressed when wiring up the function in Step C.

### Thread-Safety Status
| Operation | Protected? | Notes |
|-----------|-----------|-------|
| Read (with lock param) | ✅ Yes | When caller passes a lock |
| Read (no lock) | ❌ No | Documented limitation |
| Write (agent_orchestrator.py:1522-1528) | ❌ No | Needs fixing in Step C |
| Clear (agent_pool.py:682) | ❌ No | Needs fixing in Step C |

### Review History
- Iteration 1: 3 major issues (shallow copy, no lock, no type hints) → fixed
- Iteration 2: 2 critical + 3 major (dead code by design, wrong return type, deadlock risk, write site audit, fallback safety) → fixed
- Iteration 3: **APPROVED** ✅