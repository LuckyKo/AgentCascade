# SLEEPING State Refactor Plan

**Goal:** Single queue for all wakeups, any message wakes SLEEPING agents, no timeout-to-IDLE transition.

## Current Architecture (Problem)

- **Two queues:**
  - `_async_results` (AsyncResultBuffer): stores `(result_string, function_id)` tuples; used by async tool completions and shell heartbeats
  - `message_queues`: stores strings or multimodal lists; user messages go here
- **SLEEPING wakeup:** ONLY drains `_async_results` (line 4202); user messages NEVER wake sleeping agents
- **Timeout:** After 300s, transitions to COMPLETING→IDLE even if children still running

## Key Insight

Async tools already return launch confirmation immediately (satisfying tool_call_id). Actual results arrive later as marked user messages (`[Agent ... Completed]`, `⟨shell_cmd completed⟩`). There's no correctness reason to keep separate queues.

## Design Decision: How to Merge

**Chosen approach: Single-write migration (NOT dual-write)**

Async results are strings. User queue accepts strings. We enqueue async results directly into message_queues as strings, WITHOUT writing to _async_results simultaneously. AsyncResultBuffer is kept temporarily as a read-only fallback for existing drain consumers during transition, then removed.

**Why NOT dual-write:** Dual-write causes duplicate processing — if both queues receive the same message and both are drained, the agent processes it twice. Single-write avoids this entirely.

The only complication is `__wait`, which needs a blocking wait — we'll add a proper condition-variable-based implementation on the message queue.

## Changes Required

### 1. execution_engine.py — _handle_sleeping_state() (lines ~4165-4349)

**Remove:**
- Lines 4200-4202: async-only drain → replace with unified drain from message_queue only
- Lines 4264-4278: entire timeout-to-COMPLETING block
- Lines 4302-4314: stable-state async drain (no longer needed)

**Change wakeup logic to:**
```python
# Drain message queue — all wakeups now come through here (async results + user messages)
messages = self.pool.drain_queue(inst_name)

if messages:
    # Wake up on ANY message
    with instance._state_lock:
        if instance.state == AgentState.TERMINATED:
            return SleepAction.BREAK_LOOP, None
        instance._transition(AgentState.RUNNING)
        instance.sleeping_since = None
        instance._last_wakeup_log = time.monotonic()
        logger.debug("RESUMED from SLEEPING - %s (%d messages)", inst_name, len(messages))

    # Inject all drained messages as user-type messages (async results are already formatted strings)
    self._drain_and_inject(
        instance, inst_name, messages_list, llm_messages, response,
        items=messages,  # Pass pre-drained items directly
        factory=self._make_user_message,
    )

    # Re-acquire concurrency slot after waking from SLEEPING
    if not skip_slot_acquire:
        self._acquire_slot_with_logging(instance, "after_message_wakeup")
        if self._is_stopped(inst_name):
            return SleepAction.BREAK_LOOP, None

    return SleepAction.CONTINUE_LOOP, None
```

**Key point:** Async results are already formatted strings (`[Agent ... Completed]`, `⟨shell_cmd completed⟩`, etc.), so they can be injected via `_make_user_message` just like user messages. No special async injection path needed.

**Keep:**
- Periodic logging every `wakeup_interval` (useful for debugging)
- `has_pending()` check — still useful for logging context ("waiting for N tools")
- Stop check at line 4197

### 2. async_tools.py — AsyncToolRegistry._execute completion (line ~144)

**Change:** Instead of putting into `_async_results`, enqueue directly to message queue:
```python
# OLD:
self.pool._async_results.put(entry.agent_instance_name, result_msg, function_id=entry.function_id)

# NEW:
self.pool.enqueue_message(entry.agent_instance_name, result_msg)
```

### 3. async_shell.py — Heartbeat/completion delivery (lines ~908, 946, 1002, 1044)

**Change:** Same pattern — enqueue to message queue instead of `_async_results.put()`:
```python
# OLD:
self.pool._async_results.put(agent_name, msg_str, function_id=f"heartbeat_{tool_id}")

# NEW:  
self.pool.enqueue_message(agent_name, msg_str)
```

Also check line 1054 (`_enqueue` fallback method).

### 4. shell_cmd.py — __wait tool (lines ~362-395)

**Problem:** Currently calls `buffer.wait_for_next(agent_name, timeout=30.0)` on AsyncResultBuffer internals.

**Solution:** Add a condition-variable-based wait method to agent_pool that works on message_queues:

In agent_pool.py, add to AgentPool class:
```python
def __init__(self):
    # ... existing init ...
    self._message_condition = threading.Condition(self._queue_lock)

def enqueue_message(self, instance_name: str, text: str) -> None:
    with self._queue_lock:
        self.message_queues.setdefault(instance_name, []).append(text)
        self._mark_activity(instance_name)
        self._message_condition.notify_all()  # Wake any waiters

def wait_for_message(self, instance_name: str, timeout: float = 30.0) -> Optional[str]:
    """Block until a message is available for this instance, or timeout."""
    with self._message_condition:
        deadline = time.time() + timeout
        while True:
            msgs = self.message_queues.get(instance_name)
            if msgs and len(msgs) > 0:
                return msgs.pop(0)
            remaining = deadline - time.time()
            if remaining <= 0:
                # Clean up empty list
                if instance_name in self.message_queues and not self.message_queues[instance_name]:
                    del self.message_queues[instance_name]
                return None
            self._message_condition.wait(timeout=min(remaining, 1.0))
```

Then in shell_cmd.py __wait handler:
```python
# OLD: buffer.wait_for_next(agent_name, timeout=30.0)
# NEW:
result = self.agent_pool.wait_for_message(agent_name, timeout=timeout)
if result is None:
    return f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No message received within {timeout}s"
return result
```

This preserves the blocking semantics without busy-waiting, and reuses the existing _queue_lock.

### 5. agent_pool.py — Cleanup paths

**Update:**

1. **dismiss_instance() (line ~959):** Must drain message_queue before removing instance to prevent orphaned messages:
```python
# Before removing instance from self.instances:
with self._queue_lock:
    self.message_queues.pop(instance_name, None)
```

2. **terminate_instance():** Same — drain message_queue.

3. **clear_async() (line ~1178):** Remove AsyncResultBuffer recreation after transition complete.

4. **drain_async_results():** Keep as no-op during transition for backward compat, then remove.

5. **Ensure `enqueue_message()` is thread-safe:** Already uses `_queue_lock` ✓

6. **Add condition variable:** See section 4 above — add `_message_condition` to support `__wait`.

### 6. Other execution_engine.py consumers of drain_async_results

**Update these to use drain_queue instead:**
- Line 1898: `_pre_llm_drain()` — already drains user queue; async results now come through same path
- Line 4015: `_drain_post_generation_messages()` safety drain — change to `drain_queue`
- Line 1497: exit cleanup — change to `drain_queue`

### 7. Settings (optional cleanup)

- `AGENT_SLEEPING_TIMEOUT` / `sleeping_timeout` in settings.py:89-90 can be deprecated or removed since timeout path is gone
- `AGENT_SLEEPING_WAKEUP_INTERVAL` stays — still used for periodic logging

## Risk Assessment

**High risk:**
- Breaking async result delivery → agents never wake up from children
- Race conditions with concurrent puts to message queue
- __wait tool breaking shell_cmd workflows

**Mitigation:**
- Single-write migration (no dual-write) avoids duplicate processing
- Use existing _queue_lock for thread safety (already proven in production)
- Add condition variable for __wait instead of polling
- Add explicit logging at each delivery point for debugging
- Test with known async patterns: call_agent + wait, shell_cmd async + __status

**Medium risk:**
- Idle dismissal edge case: agent goes IDLE but has pending async tools → results arrive for dismissed agent
- Current code already skips SLEEPING agents in idle checker; IDLE agents are fair game
- Mitigation: remove timeout-to-IDLE so agents stay SLEEPING while waiting; add message_queue drain to dismiss_instance()

**Low risk:**
- Multimodal content: async results are strings, user queue handles strings fine
- Tool_call_id pairing: already satisfied by launch confirmation

## Testing Checklist

1. Spawn async researcher, verify Maine sleeps then wakes on completion
2. User sends message while Maine sleeping → verify Maine wakes and processes both user msg and async result
3. Async shell_cmd with heartbeats → verify heartbeats arrive as messages
4. __wait tool still works (polling-based)
5. No agents dismissed while waiting for async children
6. Multiple nested async calls → all results delivered

## Implementation Order (Revised)

**Phase 0 — Safety first:**
1. Update `dismiss_instance()` and `terminate_instance()` to drain message_queue before removing instance
2. Add `_message_condition` condition variable to agent_pool for __wait support

**Phase 1 — Change producers (single-write, no dual-write):**
3. async_tools.py: change `_async_results.put()` → `enqueue_message()` in AsyncToolRegistry._execute completion
4. async_shell.py: change all `_async_results.put()` calls → `enqueue_message()` for heartbeats/completions

**Phase 2 — Change consumer (SLEEPING wakeup):**
5. Update `_handle_sleeping_state()` to drain message_queue only, wake on any message
6. Remove timeout-to-COMPLETING block (lines 4264-4278)
7. Test core flow: spawn async child → verify parent sleeps and wakes on completion

**Phase 3 — Cleanup remaining consumers:**
8. Update `_pre_llm_drain()` to remove `drain_async_results` call (async results now in message_queue, already drained there)
9. Update `_drain_post_generation_messages()` safety drain → use `drain_queue` instead
10. Update exit cleanup (line 1497) → use `drain_queue`
11. Update other consumers identified by grep

**Phase 4 — Fix __wait:**
12. Implement `agent_pool.wait_for_message()` with condition variable
13. Update shell_cmd.py __wait handler to use new method

**Phase 5 — Remove dead code:**
14. Remove AsyncResultBuffer class entirely
15. Remove `drain_async_results()`, `clear_async()`, `_async_results` from agent_pool
16. Deprecate/remove `AGENT_SLEEPING_TIMEOUT` setting (keep `AGENT_SLEEPING_WAKEUP_INTERVAL` for logging)

**Phase 6 — Tests:**
17. Update test mocks that reference `_async_results.put()`
18. Add regression tests for unified wakeup behavior