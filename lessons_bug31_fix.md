# Bug 31 Fix: Slow UI Updates — Token Stats Cache Eviction & Double-Calling

## Root Cause
The `_token_stats_cache` in `api_integration.py` had a hard cap of 100 entries with LRU eviction. During multi-instance execution, the cache fills up and evicts older entries, forcing expensive re-tokenization via `qwen_count` on every tick — causing multi-second delays between UI updates.

Additionally, `_get_max_tokens_for_instance()` was called TWICE per instance per tick (in both `build_state_from_pool` and `build_stream_update_from_pool`). And `slice_history_for_llm()` + `get_history_stats()` were called every tick even during active generation when the conversation hadn't changed.

## Fixes Applied

### Fix 1: Increased token stats cache size from 100 → 5000
- **File:** `agent_cascade/api_integration.py` (line ~37)
- Added `_TOKEN_STATS_CACHE_MAXSIZE = 5000`, changed eviction check to use the constant
- With typical sessions having 20-30 instances, this prevents premature cache eviction
- Each entry is ~200 bytes → 5000 entries ≈ 1 MB, negligible

### Fix 2: Cached `_get_max_tokens_for_instance` result per instance name
- **File:** `agent_cascade/api_integration.py` (lines ~39-42, ~318-324, ~480-484, ~808)
- Added `_max_tokens_cache: Dict[str, int] = {}` module-level cache
- Changed all 3 call sites to check cache before calling the expensive function
- Max tokens value never changes during a session, so caching is safe

### Fix 3: Reduced frontend render throttle from 100ms → 50ms
- **File:** `web_ui/app.js` (line ~1176)
- Changed `subThrottleContent` from 100 to 50
- Content-key check already prevents redundant DOM work

### Fix 4: Skip `slice_history_for_llm` + `get_history_stats` during active generation when conversation unchanged
- **File:** `agent_cascade/api_integration.py` (lines ~54-57, ~454-478)
- Added `_stream_token_stats_cache: Dict[str, tuple] = {}`
- Compute `current_version` early and compare against `_last_stream_versions.get(instance_name)`
- If unchanged, reuse cached `(h_stats, r_stats)` instead of calling expensive functions

### Fix 5: Cache cleanup on instance dismissal (reviewer finding)
- **File:** `agent_cascade/agent_pool.py` (lines ~417-426)
- Added cache cleanup in `remove_instance()` for all 4 module-level caches
- Prevents memory leaks and stale data when instances are dismissed/re-created

### Fix 6: Clarifying comment about `_last_stream_versions` dual purpose (reviewer finding)
- **File:** `agent_cascade/api_integration.py` (lines ~47-49)
- Added NOTE explaining the dual usage of `_last_stream_versions` in both Fix #3 and Fix #4

## Key Learnings
1. Module-level caches without eviction or cleanup WILL leak memory in long-running sessions with frequent agent turnover
2. Always add cache cleanup hooks in `remove_instance()` when using module-level dicts keyed by instance name
3. The version tuple `(msg_count, id(last_msg))` is a reliable indicator of conversation changes — during LLM streaming the conversation doesn't change, only partial streamed content does
4. `_get_max_tokens_for_instance` → `_resolve_max_tokens` does multiple lookups (router, template, LLM config) — caching this saves significant CPU cycles

## Files Modified
- `agent_cascade/api_integration.py` — 4 fixes + clarifying comment
- `agent_cascade/agent_pool.py` — cache cleanup on instance dismissal
- `web_ui/app.js` — render throttle reduction
- `todo.md` — marked bug as fixed