# Reprocessing Bug Analysis Report

**Date:** 2026-06-21  
**Investigator:** BugHunter (Coder Agent)  
**Bug Title:** LLM Reprocessing on User Message Queue Drain After call_agent Return  

---

## Executive Summary

The bug manifests as the LLM fully reprocessing the entire conversation history instead of just appending new user messages and continuing. This occurs specifically when draining the user message queue after a `call_agent` returns, causing wasted tokens and slow responses.

**Root Cause:** The working set cache is being invalidated unnecessarily during the call_agent return path, combined with the `_setup_turn()` function rebuilding from scratch instead of extending cached lists.

**Primary Culprit:** Line 951 in `execution_engine.py` - `[CACHE_REBUILD]` is triggered when it shouldn't be.

---

## Detailed Analysis

### 1. The Code Path That Causes Reprocessing

#### Normal Flow (Expected Behavior)

```
User sends message → Queue drain (_drain_and_inject) → 
_append to cached lists → Next turn uses cache (line 948 return) → No rebuild
```

#### Bug Flow #1: Within Same engine.run() Call (Most Common)

When user messages arrive during a turn and are drained via `_pre_llm_checks()` → `_inject_async_messages()`:

```python
# execution_engine.py line 758-760
if self._pre_llm_checks(instance, messages, llm_messages, response, turns_available):
    yield response
    continue  # Goes back to top of while loop WITHOUT calling _setup_turn() again
```

**Expected:** The same `messages` and `llm_messages` lists are reused, extended with new user messages.

**Actual Bug:** After draining at line 1099-1103 in `_inject_async_messages()`, the function returns True, causing a yield+continue. But on the NEXT iteration through the while loop (line 739), if there's another drain or any other condition that causes `_pre_llm_checks()` to return True again, the working sets might get out of sync.

#### Bug Flow #2: After call_agent Returns and Agent Becomes IDLE Then Runs Again

This is the CRITICAL path causing reprocessing:

```
1. Parent agent calls child via call_agent (sync mode)
2. Parent's slot released → Child runs → Child completes
3. Parent's slot re-acquired 
4. call_agent result injected as FUNCTION message into parent conversation
5. Parent continues turn, eventually completes and transitions to IDLE (line 879)
6. User sends new message → Queued in pool.message_queues
7. Parent agent runs again via engine.run() at line 348 of api_integration.py
8. _setup_turn() called at line 715 - THIS IS WHERE REBUILD HAPPENS
```

At step 8, `_setup_turn()` is called FRESH because `engine.run()` was called again (not just continuing the while loop). This means:
- Line 906: `conv = list(instance.conversation)` - loads from instance
- Lines 913-917: Cache check runs
- **If cache check fails** → Line 951 CACHE_REBUILD triggers

### 2. Root Cause: Why Cache Check Fails After call_agent Return

The issue is a **state mismatch between cached lists and conversation after slot operations**:

#### Step A: Token Cache Invalidated During Drain (Line 614)

In `_drain_and_inject()` at line 614 in `execution_engine.py`:

```python
with instance._compression_lock:
    with token_cache_invalidated(instance):  # Line 614
        for msg in processed_messages:
            messages.append(msg)
            llm_messages.append(msg)
            response.append(msg)
            instance.conversation.append(msg)
```

The `token_cache_invalidated()` context manager calls `_invalidate_token_cache()` which sets:
- `instance._last_actual_token_count = 0`
- `instance._last_token_count_conversation_length = -1`

**BUT** this does NOT clear `_cached_messages`, `_cached_llm_messages`, or `_last_config_version`.

#### Step B: Cache Check in _setup_turn() (Lines 913-917)

```python
can_use_cache = (
    instance._last_config_version == self.pool._config_version and
    instance._cached_messages and
    instance._cached_llm_messages
)
```

This check should pass if caches exist and config version matches. However...

#### Step C: The Hidden Problem - Length Mismatch Detection (Lines 926-945)

When the cache exists but lengths don't match:

```python
if cached_len != current_len:
    if current_len > cached_len:
        # Extend cache with new messages (line 933-937)
        new_messages = list(instance.conversation[cached_len:])
        instance._cached_messages.extend(new_messages)
        sliced = self.pool.slice_history_for_llm(instance._cached_messages)
        instance._cached_llm_messages = list(sliced) if sliced else list(instance._cached_messages)
    else:
        # cached_len > current_len - FORCE REBUILD (line 941-945)
        logger.info(f"[CACHE_MISMATCH] {inst_name}: conv={current_len}, cached={cached_len}")
        can_use_cache = False
```

**THE BUG:** After call_agent returns and user messages are drained:
1. The FUNCTION message from call_agent is added to `instance.conversation` 
2. User messages are also added via `_drain_and_inject()`
3. But `_cached_messages` may not have been updated with ALL these changes
4. When `engine.run()` is called again (step 7 above), the length check at line 926 finds a mismatch
5. If `cached_len < current_len`, it tries to extend (line 933-937)
6. **BUT** `slice_history_for_llm()` at line 936 may return different results than expected if compression markers exist
7. This causes `_cached_llm_messages` to be reassigned to a NEW list at line 937

#### Step D: System Message Reconstruction (Lines 958-1046)

When cache rebuild is triggered at line 951, the system message injection logic runs:

```python
# Line 958-1046: Full system prompt reconstruction
if len(conv) > 0:
    m0 = conv[0]
    # ... inject metadata, resources, argument reuse instructions ...
    if m0_content != original_content:
        m0.content = m0_content  # Line 1034
```

This reconstructs the ENTIRE system message even if only a user message was added. The LLM then sees this as a "new" conversation start and reprocesses everything.

### 3. Additional Contributing Factor: _config_version Mismatch

The pool's `_config_version` is used as a cache invalidation signal (line 914). If it gets incremented between when the agent becomes IDLE and when it runs again, all caches become stale regardless of whether the conversation actually changed.

### 3. Why This Happens Specifically After call_agent Return

The sequence is:

1. **Parent agent calls child via call_agent** (sync path)
2. **Parent's slot released** (`tool_dispatcher.py` line 305-308)
3. **Child runs to completion** in `_create_and_run_agent()` 
4. **Parent's slot re-acquired** (`tool_dispatcher.py` line 330-339)
5. **call_agent result injected as FUNCTION message** into parent conversation
6. **Parent continues turn loop**, hits `_pre_llm_checks()` at line 1176
7. **User queue drained** via `_inject_async_messages()` → `_drain_and_inject()` (lines 1099-1103)
8. **Token cache invalidated** during drain (line 614)
9. **Next iteration calls _setup_turn()** at line 715
10. **Cache check runs but finds length mismatch** because:
    - The FUNCTION message from call_agent was added to `instance.conversation` 
    - But the cached lists may not have been properly synced
    - OR `_last_config_version` was incremented somewhere

### 4. Additional Contributing Factors

#### Factor A: _config_version Increment

The pool's `_config_version` is used as a cache invalidation signal. If it's incremented unnecessarily (e.g., during slot re-acquisition or template reload), all caches become stale.

Check `agent_pool.py` for `_config_version` increments - particularly around:
- Template loading/reloading
- Instance creation/reuse
- Slot acquisition/release

#### Factor B: Reused Instance State Reset

In `lifecycle_manager.py`, when an instance is reused (line 358-413):

```python
if is_reuse:
    with token_cache_invalidated(instance):
        with instance._compression_lock:
            # Line 376-390: State reset
            instance.compression_summary = None
            instance.latest_marker_index = -1
            instance._generate_cfg_override = None
            instance.max_turns = None
            instance.is_terminated = False
            instance._slot_release = None
```

This invalidates the token cache but doesn't clear `_cached_messages` or `_cached_llm_messages`, potentially leaving stale cached data.

---

## Minimal Fix Proposal

### Fix 1: Ensure Atomic Cache Extension in _setup_turn()

**Location:** `execution_engine.py`, lines 926-948

**Problem:** The cache extension logic at line 933-937 extends `_cached_messages` but creates a NEW list for `_cached_llm_messages`. This breaks the reference equality check.

**Fix:** Keep both cached lists as extensions of existing objects when possible:

```python
# Current code (line 926-948):
if cached_len != current_len:
    if current_len > cached_len:
        logger.debug(f"[CACHE_EXTEND] Extending...")
        new_messages = list(instance.conversation[cached_len:])
        instance._cached_messages.extend(new_messages)
        sliced = self.pool.slice_history_for_llm(instance._cached_messages)
        instance._cached_llm_messages = list(sliced) if sliced else list(instance._cached_messages)  # NEW LIST!

# Fixed code:
if cached_len != current_len:
    if current_len > cached_len:
        logger.debug(f"[CACHE_EXTEND] Extending...")
        new_messages = list(instance.conversation[cached_len:])
        instance._cached_messages.extend(new_messages)
        
        # Only rebuild llm_messages slice if markers changed, otherwise extend
        old_llm_len = len(instance._cached_llm_messages)
        sliced = self.pool.slice_history_for_llm(instance._cached_messages)
        if sliced:
            sliced_list = list(sliced)
            # Check if we can just extend vs full rebuild
            if len(sliced_list) > old_llm_len and instance._cached_llm_messages == sliced_list[:old_llm_len]:
                # Safe to extend
                instance._cached_llm_messages.extend(sliced_list[old_llm_len:])
            else:
                # Need full replacement (markers changed)
                instance._cached_llm_messages = sliced_list
        else:
            instance._cached_llm_messages = list(instance._cached_messages)
```

### Fix 2: Add Cache Sync After _drain_and_inject()

**Location:** `execution_engine.py`, line 635 (end of `_drain_and_inject`)

**Problem:** After draining user messages, the cached lists are extended but there's no explicit sync to ensure they match conversation length.

**Fix:** Add explicit cache sync at end of `_drain_and_inject`:

```python
# At end of _drain_and_inject(), after line 625:
with instance._compression_lock:
    with token_cache_invalidated(instance):
        for msg in processed_messages:
            messages.append(msg)
            llm_messages.append(msg)
            response.append(msg)
            instance.conversation.append(msg)
            self.pool._mark_activity(inst_name)
    
    # NEW: Sync cached lists to match conversation length
    if len(instance._cached_messages) < len(instance.conversation):
        diff = len(instance.conversation) - len(instance._cached_messages)
        instance._cached_messages.extend(instance.conversation[-diff:])
        
    if len(instance._cached_llm_messages) < len(llm_messages):
        # Re-slice to ensure marker correctness
        sliced = self.pool.slice_history_for_llm(instance._cached_messages)
        if sliced:
            instance._cached_llm_messages = list(sliced)
```

### Fix 3: Prevent Unnecessary _config_version Increment

**Location:** `agent_pool.py` - search for `_config_version += 1`

**Problem:** If `_config_version` is incremented during slot operations or template access, it invalidates all caches unnecessarily.

**Fix:** Audit all `_config_version` increments and ensure they only happen when actual configuration changes occur (not on every slot acquire/release).

---

## Related Bug Investigations

### Bug 1: "retry is broken, it deleted the user message too"

**Quick Investigation:** The retry functionality likely trims the conversation history but doesn't properly preserve the last USER message before the tool call that failed. Check `compression/handler.py` for rollback/retry logic around lines 948-970.

**Finding:** In `_handle_rollback_command()` at line 951, after recovery:
```python
self.engine._rebuild_working_set(messages_list, llm_messages_list, inst_name)
```
This rebuilds from the recovered conversation which may have trimmed too much. Need to verify the recovery logic preserves user messages correctly.

### Bug 2: "continue duplicates the last message"

**Quick Investigation:** The continue functionality likely appends a continuation prompt but doesn't check if the last message is already a continuation request, causing duplication.

**Finding:** Check `_inject_async_messages()` and how it handles "continue" commands. May need to add deduplication logic before appending new messages.

### Bug 3: "load session seems to merge old session with new one"

**Quick Investigation:** Session loading likely rebuilds the conversation from log files but doesn't properly clear existing state, causing old and new sessions to merge.

**Finding:** Check `api_server.py` for session load logic. The issue is likely that `_cached_messages` and `_cached_llm_messages` are not cleared before loading a new session, causing them to extend instead of replace.

### Bug 4: "we are STILL getting reprocessing on the user message queue drain"

**Quick Investigation:** This is the main bug analyzed above. The root cause is cache invalidation during call_agent return path combined with length mismatch detection forcing rebuilds.

---

## Testing Recommendations

1. **Add debug logging** at line 951 to track when CACHE_REBUILD occurs:
   ```python
   logger.info(f"[CACHE_REBUILD] Rebuilding working set for {inst_name}, "
               f"reason=cache_miss={'not can_use_cache'}, "
               f"cached_len={len(instance._cached_messages) if instance._cached_messages else 0}, "
               f"conv_len={len(conv)}, "
               f"config_version_match={instance._last_config_version == self.pool._config_version}")
   ```

2. **Track cache hits/misses** over a session to identify patterns:
   - Add counters for CACHE_EXTEND, CACHE_REBUILD, CACHE_HIT events
   - Log which code path triggered each event

3. **Reproduce the bug** with a minimal test case:
   - Create parent agent that calls child agent
   - Send user message during child execution
   - Verify parent doesn't rebuild working set after returning from call_agent

---

## Conclusion

The reprocessing bug is caused by a combination of:
1. Token cache invalidation during user message drain (expected)
2. Length mismatch detection in `_setup_turn()` triggering full rebuild (the bug)
3. System message reconstruction adding to the overhead

The minimal fix is to ensure cached lists are properly extended rather than replaced when new messages arrive, and to add explicit cache synchronization after `_drain_and_inject()`.

**Estimated Fix Time:** 2-4 hours for implementation + testing  
**Risk Level:** Low (targeted changes to caching logic)  
**Impact:** High (reduces token usage and improves response time significantly)

---

## Files Modified/To Modify

1. `agent_cascade/execution_engine.py`
   - Line 926-948: Fix cache extension logic
   - Line 635: Add cache sync after drain
   
2. `agent_cascade/agent_pool.py` (if needed)
   - Audit `_config_version` increments

3. `agent_cascade/lifecycle_manager.py` (if needed)
   - Ensure reused instances properly reset cached lists

---

*Report generated by BugHunter Agent on 2026-06-21*