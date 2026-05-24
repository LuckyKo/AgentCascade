# Idle Agent Auto-Dismissal — Fix Summary (2026-05-23)

## Context
Multiple critical issues found in the idle agent auto-dismissal feature. All resolved via surgical edits.

---

### Issue 1: Memory Leak — `_last_activity` Not Cleaned in `dismiss_instance()`
**Severity**: 🔴 CRITICAL  
**File**: `agent_pool.py` → `dismiss_instance()` (line ~328)  

When `dismiss_instance()` called `clear_conversation()`, it removed the instance from conversations/classes/loggers but left a stale entry in `_last_activity`. This is an unbounded memory leak — every UI dismissal adds another orphaned timestamp.

**Fix**: Added `_last_activity.pop(instance_name, None)` inside `_activity_lock` after `clear_conversation()` in the else branch (inactive instances).

---

### Issue 2: No Runtime Configurability for `idle_timeout_seconds` / `idle_check_interval`
**Severity**: 🔴 MAJOR  
**Files**: `api_server.py`, `start_multi_agent.py`, `start_api_server.py`  

All entry points instantiated `AgentPool()` without passing these parameters, so the hardcoded defaults (300s / 60s) couldn't be changed at runtime.

**Fix**:
- **api_server.py**: Added CLI args `--idle-timeout` and `--idle-check-interval` to argparse, with fallback to env vars `QWEN_AGENT_IDLE_TIMEOUT` / `QWEN_AGENT_IDLE_CHECK_INTERVAL`. Priority: CLI > env var > default.
- **start_multi_agent.py** & **start_api_server.py**: Added env var support (same names), since neither uses argparse.

---

### Issue 3: Agents Dismissed Right After Long Runs
**Severity**: 🟠 MAJOR  
**File**: `agent_orchestrator.py` → `_stream_sub_agent_call()` finally block (line ~2168)  

`_mark_activity()` was only called once when the agent started. If an agent ran for 4+ minutes, its timestamp was still at dispatch time — so it immediately got flagged as idle and dismissed after completing a long run.

**Fix**: Added `_mark_activity(instance_name)` call in the `finally` block right after `active_stack.pop()`, using the same `hasattr()` guard pattern already used elsewhere in the file.

---

### Issue 4: TOCTOU Race Condition in `_is_agent_idle()`
**Severity**: 🟠 MAJOR  
**File**: `agent_pool.py` → `_is_agent_idle()` (line ~736)  

The method read from three separate data sources (`active_stack`, `_instance_halted`, `_last_activity`) with three different locks, creating a time-of-check/time-of-use window. An agent could start running between the active_stack check and the idle timeout check.

**Fix**: Read `active_stack` status under `_state_lock` (same lock that protects mutations to it), storing result in `is_active` before releasing the lock. This makes the active check atomic with respect to dispatch.

---

### Issue 5: `reset()` Didn't Stop Idle Checker Thread
**Severity**: 🟡 MINOR  
**File**: `agent_pool.py` → `reset()` (line ~674)  

The `reset()` method cleared `_last_activity` but didn't stop the background idle checker thread. This could cause the checker to operate on stale data or crash during reset.

**Fix**: Added `_stop_idle_checker()` at the beginning of `reset()`, then `_start_idle_checker()` at the end after all cleanup is complete.

---

## Configuration Options (After Fix)
- **CLI** (api_server.py only): `--idle-timeout=600 --idle-check-interval=120`
- **Env vars** (all entry points): `QWEN_AGENT_IDLE_TIMEOUT=600 QWEN_AGENT_IDLE_CHECK_INTERVAL=120`
- **Defaults**: 300 seconds timeout, 60 seconds interval