# Security & Compressor Agent Regularization - Implementation Complete

## Date: 2026-06-12

---

## Executive Summary

Both issues have been addressed:

1. **Security agent auto-ask stall** — Fixed with 3 targeted patches to the auto-ask flow
2. **Security/Compressor regularization** — Both agents now follow the standard AgentInstance lifecycle like Coder, Researcher, Writer, etc., with proper frontend tabs, API point allocation, and IDLE state management

---

## Part 1: Auto-Ask Stall Fixes

### Fix 1A: Missing UI Broadcast After Auto-Apply
**File:** `agent_cascade/api_server.py`

When auto_apply=True and Security returns [YES]/[NO], the approval was processed but NOT broadcast to the UI. The approval card stayed visible, making it look "stalled." Added broadcast of updated approvals list after both approve and reject operations, matching the pattern already used for timeout handling.

### Fix 1B: Timeout Watchdog for Blocking API Reads
**File:** `agent_cascade/api_server.py`

The `for partial in run_gen:` loop blocked indefinitely if the LLM API stalled between chunks. Added a watchdog timer that resets on each successful chunk arrival and closes the generator if no chunk arrives within SECURITY_ADVISOR_TIMEOUT_SECONDS.

### Fix 1C: Semaphore Safety Net
**File:** `agent_cascade/api_server.py`

Added defensive semaphore state check in the finally block as an extra safeguard for edge cases where generator.close() might fail to propagate GeneratorExit through the wrapper chain.

### Critical Bug Fix: Verdict Parsing Indentation Error
**File:** `agent_cascade/api_server.py`, lines 1943-2000

During refactoring, `is_yes = False`, `is_no = False`, `justification = ""` were placed OUTSIDE the try/except block (same indentation as `except`), causing them to ALWAYS execute after the verdict was parsed — overwriting any successfully found [YES]/[NO]. 

**Fix:**
- Moved these defaults INSIDE the except block (44 spaces of indentation)
- Added initialization BEFORE the try block so defaults exist even when no exception occurs but no verdict is found

---

## Part 2: Security Agent Regularization

### Before
The Security agent was invoked via `_security_check()` in api_server.py which:
- Loaded the template only (no AgentInstance created)
- Called LLM directly via `api_router.call_with_fallback()`
- Manually tracked state in `instance_state` dict
- No frontend tab, no proper API point allocation

### After
The Security agent now:
- Creates a proper AgentInstance via `_create_system_agent()` (lines ~1848-1856)
- Executes through `engine.run(sec_instance)` (lines ~1898-1931) — same as all other agents
- Gets output extracted via `extract_instance_output()` (line ~1931)
- Appears in frontend with its own tab (via pool.instances registration)
- Transitions to IDLE when done (via `_transition_instance_state`)
- Always starts fresh — never reuses conversation history between checks

### Infrastructure Added in execution_engine.py

**`_create_system_agent()` method** (lines 2930-3056):
- Creates fresh AgentInstance (never reuses existing)
- Registers in `pool.instances`
- Initializes `pool.instance_state` for UI visibility
- Sets up active_stack tracking
- Broadcasts stream_update for immediate frontend tab appearance

**`force_fresh` parameter** on `_create_and_run_agent()` (line 2419):
- When True, skips the instance reuse logic
- Used by Security and Compressor agents that should always start fresh
- Backward compatible (default False)

---

## Part 3: Compressor Agent Regularization

### Before
Compressor had two paths:
- **Path A** (via call_agent): Already created proper instances ✓
- **Path B** (direct engine call): Loaded template, manually set state, called `comp_agent.run()` directly ✗

### After
- **Path A**: Now uses `force_fresh=True` for Compressor agents
- **Path B**: Replaced with `_create_system_agent()` + `engine.run()` pattern — same as Security agent
- Settings propagation added (LLM config inheritance from session)
- Timeout handling preserved (300 seconds)

---

## Part 4: Reviewer-Identified Fixes

### Fix #1 (Critical): Compressor Double active_stack Removal
**File:** `agent_cascade/compression/agent_invoker.py`

Outer cleanup block used unprotected `active_stack.remove()`. Replaced with thread-safe `active_stack_remove()` method.

### Fix #2 (Critical): Security Agent active_stack Cleanup Without Lock  
**File:** `agent_cascade/api_server.py`

Finally block directly mutated `active_stack` without holding `_state_lock`. Replaced with `agent_pool.active_stack_remove(sec_state_key)`.

### Fix #3 (Major): Consistent API in _create_system_agent()
**File:** `agent_cascade/execution_engine.py`

Replaced direct `active_stack.append()` with `self.pool.active_stack_append()`.

### Fix #4 (Major): Settings Propagation for Compressor Path B
**File:** `agent_cascade/compression/agent_invoker.py`

Added LLM config inheritance logic matching Security agent pattern, including session settings filtering and max_turns = 10.

### Fix #5 (Minor): Redundant max_turns Assignment
**File:** `agent_cascade/api_server.py`

Removed redundant `sec_instance.max_turns = 50` since engine.run() already defaults to 50.

---

## Files Modified

| File | Changes |
|------|---------|
| `agent_cascade/api_server.py` | Auto-ask stall fixes (1A, 1B, 1C), Security regularization, verdict parsing bug fix, active_stack cleanup fix, redundant max_turns removal |
| `agent_cascade/execution_engine.py` | `_create_system_agent()` method, `force_fresh` parameter, consistent API usage |
| `agent_cascade/compression/agent_invoker.py` | Compressor Path B regularization, settings propagation, thread-safe cleanup |

---

## Testing Recommendations

1. **Auto-apply mode**: Trigger a security check with auto_apply=True — verify the approval card disappears after Security returns [YES]/[NO]
2. **Frontend tabs**: Verify Security and Compressor tabs appear during execution and show IDLE state afterward
3. **Timeout handling**: Test that Security times out correctly and auto-rejects
4. **Concurrent execution**: Test multiple security checks in parallel — verify no race conditions on active_stack
5. **Fresh instances**: Verify that repeated Security checks don't carry over conversation history

---

## Additional Fixes After Final Review

### Removed Non-Existent Method Call
**File:** `agent_cascade/api_server.py`

Removed the call to `agent_pool._transition_instance_state(sec_state_key, 'IDLE')` which was wrapped in try/except and always failed silently (method doesn't exist). The `engine.run()` already handles IDLE state transition internally.

### Lock Scope Fix — Critical Performance Issue
**File:** `agent_cascade/api_server.py`, lines 1897-1995

The `with app.security_check_lock:` block was holding the lock during `engine.run(sec_instance)` — meaning the lock was held for 30-120 seconds during LLM processing, blocking ALL other security checks. 

**Fix:** Dedented lines 1897-1995 by 4 spaces so engine.run() and verdict parsing execute OUTSIDE the lock. The lock now only covers setup operations (prompt building, _create_system_agent, config) which take <1 second.

**Impact:** Multiple security checks can now run concurrently without blocking each other.

### Defensive Pre-Initialization
**File:** `agent_cascade/api_server.py`, lines 1826-1827

Added `sec_instance = None` and `engine = None` pre-initialization before the lock block to prevent UnboundLocalError if `_create_system_agent()` fails inside the lock.

### Duplicate Cleanup Removal
**File:** `agent_cascade/compression/agent_invoker.py`, lines 241-243

Removed duplicate `active_stack_remove(comp_state_key)` from inner finally block — outer finally at line 294 already handles cleanup for both Path A and Path B.

---

## Status: ✅ IMPLEMENTATION COMPLETE, REVIEWED, EDGE-CASE AUDITED, READY FOR TESTING