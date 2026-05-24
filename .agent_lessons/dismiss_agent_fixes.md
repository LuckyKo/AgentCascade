# Dismiss Agent Fixes — Summary

## Date: 2026-05-22
## Author: DismissLogPathsFixer

---

## Issues Fixed

### 🔴 Issue 1: dismiss_agent doesn't return log paths for agent resurrection
**Root Cause:** `DismissAgent.call()` returned plain text strings with no structured data. The LLM had no way to know an agent's log file path after dismissal, preventing agent resurrection via `load_session_from_log`.

**Fix:** Changed return values to structured JSON that includes:
- `status`: "dismissed", "no_idle_agents", "not_found", or "error"
- `agent`: The instance name that was dismissed
- `log_path`: Full path to the agent's log file (captured BEFORE `clear_conversation` removes it)
- `message`: Human-readable status message

For `all_idle=true`, returns a JSON array with entries for each dismissed agent.

### 🔴 Issue 2: Real-time tab closing only works for UI-initiated dismissal, NOT LLM-initiated
**Root Cause:** When the LLM calls `dismiss_agent` tool, there was no mechanism to notify the WebSocket handler. Only UI-initiated dismissal (via `terminate_sub_agent`) triggered a state broadcast at line ~1902 of api_server.py.

**Fix:** Implemented a callback infrastructure:
1. Added `_on_dismissed_callbacks: list` and `on_dismissed()` / `_fire_on_dismissed()` to agent_pool.py
2. DismissAgent.call() fires callbacks after clearing each dismissed instance
3. API server registers a dismissal callback that pushes a signal onto send_queue via `run_coroutine_threadsafe`
4. `_sender_loop` detects "dismissal" signals and triggers a full state broadcast

This ensures both LLM-initiated and UI-initiated dismissals result in immediate tab removal.

---

## Files Modified

### 1. agent_cascade/tools/custom/manager_ops.py
- Added `import json`
- Rewrote `DismissAgent.call()`:
  - Captures log_path from `self.agent_pool.instance_loggers.get(instance_name)` BEFORE calling `clear_conversation`
  - Returns structured JSON (json.dumps) instead of plain strings
  - Fires `_fire_on_dismissed()` callback for each dismissed instance

### 2. agent_pool.py  
- Added `_on_dismissed_callbacks: list = []` to `__init__`
- Added `on_dismissed(callback)` method for registering callbacks
- Added `_fire_on_dismissed(instance_name, log_path)` method for firing callbacks (with error handling)
- Updated `dismiss_instance()`: captures log_path before clearing and fires callback

### 3. api_server.py
- In `startup()`: registers dismissal callback that pushes signal onto send_queue via `run_coroutine_threadsafe`
- In `run_agent_thread()`: stores event loop reference on agent_pool as `_ws_loop` for callback use
- In `_sender_loop()`: detects "dismissal" message type and triggers full state broadcast

---

## Key Technical Decisions

1. **Capture log_path BEFORE clear_conversation**: The `clear_conversation` method removes the logger from `instance_loggers`, so we must capture the path first.

2. **Thread-safe callback mechanism**: Since dismiss_agent runs on a tool thread (not the async event loop), we use `asyncio.run_coroutine_threadsafe()` to safely push onto send_queue.

3. **Lightweight signal + full state broadcast**: Instead of pushing full state from the tool thread, we push a "dismissal" signal and have _sender_loop build the full state in the correct context.

4. **Callback errors are silently swallowed**: `_fire_on_dismissed` catches all exceptions to ensure dismissal callbacks never disrupt agent execution.

5. **Event loop stored on agent_pool**: The event loop reference (`_ws_loop`) is set at the start of each generation cycle via `run_agent_thread`, ensuring the callback always has a valid loop reference.