# Phase 6 Reviewer Fixes — Thread Safety & Defensive Copying

## Summary of Changes

Fixed 2 CRITICAL, 2 MAJOR, and several MINOR issues identified by the Phase 6 reviewer.

---

## Critical Fix #1: Lock protection on `agent_pool.add_message()` 
**File:** `agent_cascade/agent_pool.py` (lines ~748-759)

**Problem:** WebSocket handler calls `add_message()` from the async event loop thread while ExecutionEngine reads `inst.conversation` in a background thread — data race.

**Fix:** Wrapped `inst.conversation.append(message)` inside `with inst._compression_lock:`. The `_compression_lock` already exists on every `AgentInstance` (defined in `agent_cascade/agent_instance.py:54`).

---

## Critical Fix #2: Lock protection on edit/delete handlers
**File:** `api_server.py` — edit handler (~2098-2160) and delete handler (~2168-2201)

**Problem:** Both handlers read and mutate `inst.conversation` directly without lock, while the agent thread may be reading it concurrently.

**Fix (edit):** 
- Read: `with inst._compression_lock: history = list(inst.conversation)` — defensive copy under lock
- Write: `with inst._compression_lock: inst.conversation[:] = history` — in-place replace under lock
- Edit logic operates on the local `history` copy between read and write

**Fix (delete):** Same pattern — defensive copy under lock, mutate locally, write back under lock.

---

## Major Fix #3: Defensive copy in `_get_main_history()`
**File:** `api_server.py` (line ~93)

**Problem:** Returned `inst.conversation` directly — mutable reference that callers could accidentally modify.

**Fix:** Changed to `return list(inst.conversation)` with comment explaining why.

---

## Major Fix #4: Simplified `_load_session_history()` return type
**File:** `api_server.py` (lines ~464-501)

**Problem:** Returned `(loaded_history, loaded_summary)` tuple but the only caller at line 580 discarded these values because `agent_pool.load_session_from_log()` already populates the pool.

**Fix:** Changed return type to `bool` — `True` on success, `False` on failure. Removed redundant retrieval of history/summary from pool state.

---

## Minor Fix #5: Stale comments
**File:** `api_server.py`

- Line 578: Changed "(Phase 7)" to "(Phase 6)"
- Line ~2157: Removed stale "line 1180 does copy.deepcopy from pool" reference — that line no longer does that
- Line ~2198: Same stale comment fix

---

## Design Decisions

1. **Used `_compression_lock`** rather than adding a new lock — it already exists per-instance and protects the same critical section (conversation mutations). Adding a separate lock would risk deadlock if both locks are ever held simultaneously in different orders.

2. **In-place replace (`inst.conversation[:] = history`)** instead of reassigning (`inst.conversation = history`) — this ensures any other references to `inst.conversation` (like the `instance_conversations` property) still point at the same list object and see the updated content.

3. **Lock scope is surgical** — only the actual read/mutate/write operations are protected. The edit logic, compression marker handling, logger reset, and state sync all happen outside the lock to avoid holding it too long.