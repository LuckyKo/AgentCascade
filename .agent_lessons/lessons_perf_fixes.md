# Performance Fixes — Lessons Learned

## Fix #1: File Handle Caching in AgentInstanceLogger

### Problem
Opening, writing, and closing a file for every single log message = 3 syscalls per message on the hot path.

### Solution
- Added `_file_handle` field + `_ensure_file()` method to open file once and keep it open
- Every write is followed by `flush()` to prevent data loss on crash (replaces implicit flush-on-close)
- On write error, handle is set to `None` so `_ensure_file()` reopens a clean handle next time
- Before any operation that needs 'w' mode (rewrite/truncate), the cached handle is closed first
- Logger is properly closed during `remove_instance()` — save reference BEFORE popping from dict

### Key lesson
When caching file handles, always flush after writes and invalidate on error. The old `open/write/close` pattern had implicit durability via close; with a persistent handle you must be explicit.

## Fix #2: Token Counting Cache on AgentInstance

### Problem
`_count_history_tokens()` called every turn, iterating through ALL messages and tokenizing each individually. For 100 messages = 100 tokenization calls per turn.

### Solution
- Added `_cached_token_count` and `_last_token_count_conversation_length` fields to AgentInstance
- Cache uses `-1` sentinel for invalidation (not increment-based, which caused false cache hits)
- Cache check: only hit when `_last_token_count_conversation_length >= 0 AND len(messages) matches`
- `_count_history_tokens()` accepts `instance` as explicit parameter (thread-safe) with fallback to `_current_instance` for backward compat
- Invalidated at ALL conversation mutation sites:
  - Async message injection in `_pre_llm_checks`
  - Response append in `_process_response`
  - Auto-continue truncation in `_process_response`
  - Tool result messages in `_process_response`
  - Mid-tool urgent injection in `_process_response`
  - Forced compression success in `_force_compression`
  - `/compress` tool apply success in `_handle_compress_context`
  - `/compress` command handler in `_handle_compress_command`
  - Pool reset in `AgentPool.reset()`
  - `AgentPool.add_message()` — was missing, added (C1)
  - `AgentPool.surgical_rollback()` — was missing, added (C2)
  - Loop recovery hint append in `api_integration.py` — was missing, added (C3)
  - `/api/reset` endpoint in `api_server.py` — was missing, added
  - Retry re-append in `api_server.py` — was missing, added
  - WebSocket reset handler in `api_server.py` — was missing, added
  - `AgentPool.clear_conversation()` — was missing, added
  - `AgentPool.rollback_to_snapshots()` — was missing, added
  - `load_session_from_log()` existing instance path — was missing, added
  - `execute_agent_turn()` user message append — was missing, added
  - `_create_and_run_agent()` sub-agent init — was missing, added

### Key lesson
Cache invalidation is harder than caching itself. Use a sentinel value (`-1`) rather than increment-based approaches — increments can cause false cache hits when different message lists happen to have the same length. Always pass the instance explicitly for thread safety; don't rely on shared mutable state.

## Fix #3: Stale sub_agent_state Entry Cleanup (Partial)

### Problem
Every 5 turns, full conversation was serialized via `model_dump()` for every message. Dismissed instances left stale entries forever.

### Solution (Implemented)
- Added cleanup of `sub_agent_state[instance_name]` in `remove_instance()` — dismissed instances no longer leave stale state.

### Deferred: Lighter Snapshots
Replacing full conversation dumps with lightweight metadata was NOT implemented because the WebUI reads directly from `sub_agent_state['messages']`. Changing this would require updating the WebUI, which is a larger change than we want to risk in perf fix scope. Defer until a dedicated WebUI refactor.

### Key lesson
WebUI state doesn't need full conversation dumps — metadata is enough for display. But changing the data shape requires coordinated changes across consumers. Always clean up per-instance state when instances are removed to prevent memory leaks.