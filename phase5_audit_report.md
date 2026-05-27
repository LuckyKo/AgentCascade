# Phase 5 Audit Report — Runtime Bug Analysis (FINAL)

**Date**: 2026-05-27  
**Auditor**: Phase5Auditor  
**Reviewed by**: Reviewer (phase5_reviewer)  
**Scope**: agent_cascade/agent_pool.py, api_server.py, agent_orchestrator.py  

---

## Summary

**CRITICAL bugs found: 8**  
**MAJOR bugs found: 3**  
**MINOR issues found: 2**

The two most severe issues are: (1) `_state_lock` missing from the new AgentPool — causing `AttributeError` on every sub-agent spawn, and (2) `active_stack` property returns a defensive copy — making all mutations silently no-op across 6 call sites in both api_server.py AND agent_orchestrator.py.

---

## CRITICAL Issues

### Issue #1: `agent_pool._state_lock` Does Not Exist — AttributeError at Runtime

- **Severity**: CRITICAL (Showstopper)
- **Location**: agent_orchestrator.py:1404, 1472, 1963, 2175, 2224; agent_pool.py (missing attribute)
- **Problem**: The new `AgentPool` class does NOT have a `_state_lock` attribute. The old pool had it at line 114 (`self._state_lock = threading.Lock()`), but the new pool delegates to `self._execution._state_lock`. All five call sites in agent_orchestrator.py reference `self.agent_pool._state_lock` directly.
- **Impact**: Every sub-agent spawn, every loop-detection rollback, and every sub-agent cleanup triggers an `AttributeError: 'AgentPool' object has no attribute '_state_lock'`. The entire sub-agent execution path is broken.
- **Suggested Fix**: Add a property delegation:
  ```python
  @property
  def _state_lock(self):
      return self._execution._state_lock
  ```

### Issue #2: `active_stack` Returns a Defensive Copy — All Mutations Silently No-Op

- **Severity**: CRITICAL
- **Location**: agent_pool.py:391; api_server.py:1566, 1774, 2068; agent_orchestrator.py:911, 1947, 2231
- **Problem**: The `active_stack` property (line 391) returns `list(self._execution.active_stack)` — a defensive copy. Any mutation on the returned list operates on the copy, not the actual stack.
- **Impact**: SIX call sites are affected across both files:

  | Location | Mutation | Impact |
  |----------|----------|--------|
  | api_server.py:1566 | `.clear()` in retry | Active stack never cleared during retry — stale entries remain, UI shows agents as "active" when not |
  | api_server.py:1774 | `.append(sec_state_key)` | Security advisor never added to active stack — no tab appears for security checks |
  | api_server.py:2068 | `.remove(sec_state_key)` | Cleanup silently fails (read of copy + mutation on copy) |
  | orchestrator.py:911 | `.clear()` in root turn | Root turn never clears stack — state leakage between turns |
  | orchestrator.py:1947 | `.append(instance_name)` | Sub-agents never added to stack — no UI tab for sub-agent execution |
  | orchestrator.py:2231 | `.pop(i)` on completion | Sub-agents never removed from stack on completion — tabs persist after done |

- **Suggested Fix**: Add mutation methods to AgentPool:
  ```python
  def active_stack_append(self, name: str):
      with self._execution._state_lock:
          self._execution.active_stack.append(name)
  
  def active_stack_remove(self, name: str):
      with self._execution._state_lock:
          if name in self._execution.active_stack:
              self._execution.active_stack.remove(name)
  
  def active_stack_clear(self):
      with self._execution._state_lock:
          self._execution.active_stack.clear()
  
  def active_stack_pop_at(self, index: int):
      with self._execution._state_lock:
          self._execution.active_stack.pop(index)
  ```

### Issue #3: `instance_conversations.pop()` in set_session_name Deletes the Agent Instance

- **Severity**: CRITICAL
- **Location**: api_server.py:2187; agent_pool.py:86-103
- **Problem**: When user renames a session via `set_session_name`, line 2187 does:
  ```python
  agent_pool.instance_conversations[new_name] = agent_pool.instance_conversations.pop(old_name)
  ```
  The `_InstanceConversationMapping.pop()` method (lines 86-103) removes from BOTH dict storage AND `pool.instances`:
  ```python
  super().pop(key, None)
  self._pool.instances.pop(key, None)
  ```
- **Impact**: Renaming the session DELETES the actual AgentInstance. The instance is gone from `pool.instances`, and while a new entry is created in dict storage under the new name, there's no corresponding AgentInstance. Subsequent operations that rely on `get_instance()` return None.
- **Suggested Fix**: Add a separate method for rename-only operations:
  ```python
  def move_key(self, old_key, new_key):
      """Move an entry from old_key to new_key without deleting the instance."""
      value = self[old_key]
      super().__delitem__(old_key)  # Only delete dict storage, not the instance
      super().__setitem__(new_key, value)
      # Update instance name if it exists
      inst = self._pool.instances.get(old_key)
      if inst is not None:
          del self._pool.instances[old_key]
          inst.instance_name = new_key
          self._pool.instances[new_key] = inst
  ```

### Issue #4: Security Advisor Instance Never Created — Writes to Orphan Dict Entry

- **Severity**: MAJOR (reclassified from CRITICAL — largely a symptom of Issue #2)
- **Location**: api_server.py:1767, agent_pool.py:67-72
- **Problem**: The security advisor is registered in `sub_agent_state` (line 1767) and `instance_conversations` (line 1772), but there's NO corresponding `create_instance('security_advisor', ...)` call. Writes go only to dict storage — not synced to any AgentInstance.
- **Impact**: The security advisor tab shows up in UI via sub_agent_state, but it's never added to the active_stack (because of Issue #2). Even if Issue #2 is fixed, `instance_classes` won't include it and `get_instance('security_advisor')` returns None.
- **Suggested Fix**: Either create a proper instance for the security advisor before writing to sub_agent_state, or accept that security advisor uses a different lifecycle path (document this explicitly).

### Issue #5: `__delitem__` Deletes Agent Instance — Breaking rename and cleanup patterns

- **Severity**: CRITICAL
- **Location**: agent_pool.py:74-77; api_server.py:2187 (indirectly via pop)
- **Problem**: `_InstanceConversationMapping.__delitem__` does:
  ```python
  def __delitem__(self, key: str) -> None:
      super().__delitem__(key)
      self._pool.instances.pop(key, None)
  ```
  Deleting from instance_conversations also DELETES the AgentInstance.
- **Impact**: If any code deletes a key from instance_conversations, it also permanently removes the agent instance. This is especially dangerous in the `reset()` flow (line 1058).
- **Suggested Fix**: Do NOT delete instances from `__delitem__` — only clear conversation data:
  ```python
  def __delitem__(self, key: str) -> None:
      super().__delitem__(key)
      inst = self._pool.instances.get(key)
      if inst is not None:
          inst.conversation.clear()
  ```

### Issue #6: `clear_conversation()` Method Does Not Exist — AttributeError at Runtime

- **Severity**: CRITICAL (Showstopper) **[New — found by reviewer]**
- **Location**: agent_orchestrator.py:1781, 2245; agent_pool.py (missing method)
- **Problem**: Two call sites in agent_orchestrator.py call `self.agent_pool.clear_conversation(instance_name)` but this method does not exist on the new AgentPool. The old pool had it, but the new pool doesn't implement it.
- **Impact**: 
  - Line 1781: Triggered during class mismatch detection — when a sub-agent is reused with a different agent class, the conversation should be cleared. Instead, an `AttributeError` crashes the execution.
  - Line 2245: Triggered during sub-agent completion cleanup — when an instance was marked for termination (dismissed from UI), the conversation should be cleared on completion. Instead, an `AttributeError` crashes the completion path.
- **Suggested Fix**: Add the method to AgentPool:
  ```python
  def clear_conversation(self, instance_name: str):
      """Clear an agent's conversation while keeping the instance alive."""
      inst = self.instances.get(instance_name)
      if inst:
          inst.conversation.clear()
      # Also remove from dict storage in the mapping
      if hasattr(self, '_instance_conversations'):
          try:
              del self._instance_conversations[instance_name]
          except KeyError:
              pass
  ```

### Issue #7: `reset()` Method Clears ALL Instances Including Main Session

- **Severity**: MAJOR (elevated from original assessment due to interaction with Issue #5)
- **Location**: api_server.py:1630; agent_pool.py:367-385
- **Problem**: The reset handler (line 1630) calls `agent_pool.reset()`. The new reset method clears `_instance_conversations` which calls `clear()` on the mapping — and that clears BOTH dict storage AND pool.instances. This means a "reset" command destroys ALL instances including the main orchestrator.
- **Impact**: After a reset, `agent_pool.get_instance('Maine')` returns None. The next user message will try to add a message to a non-existent instance via `agent_pool.add_message(instance_name, user_msg)` — which silently fails because `inst = self.instances.get(instance_name)` returns None at line 637-639.
- **Suggested Fix**: Restructure reset() to preserve the main session instance:
  ```python
  def reset(self):
      # ...clear halted/terminated state...
      # Clear conversations but DON'T delete instances
      for inst in self.instances.values():
          inst.conversation.clear()
      if hasattr(self, '_instance_conversations'):
          super(_InstanceConversationMapping, self._instance_conversations).clear()
  ```

### Issue #8: `ParallelAgentManager.submit_task()` Replaces active_stack Object — Stale References

- **Severity**: CRITICAL (threading/data-staleness bug) **[New — found by reviewer]**
- **Location**: agent_pool.py:933
- **Problem**: In the `task_wrapper()` finally block, line 933 does:
  ```python
  self.active_stack = [n for n in self.active_stack if n != instance_name]
  ```
  This creates a **new list object** and assigns it to `self.active_stack`. Any external reference to the old list (held by callers of the `active_stack` property before this assignment) now points to stale data.
- **Impact**: Concurrent reads via the `active_stack` property could return a snapshot from either the old or new list depending on timing. While the lock is held during this replacement, the fundamental issue is that object identity changes break any caller holding a reference.
- **Suggested Fix**: Mutate in-place instead of replacing:
  ```python
  with self._state_lock:
      self.active_stack[:] = [n for n in self.active_stack if n != instance_name]
  ```

---

## MAJOR Issues

### Issue #9: `sub_agent_state` Is Never Populated for Main Session

- **Severity**: MAJOR
- **Location**: api_server.py:495; agent_pool.py:215
- **Problem**: In unified mode (`USE_UNIFIED_STATE=True`), `get_session_history()` reads from `agent_pool.sub_agent_state.get('root', {})`. But `sub_agent_state` is only populated by the orchestrator for sub-agents. The main session ('Maine'/'root') is never registered in `sub_agent_state`.
- **Impact**: When `USE_UNIFIED_STATE=1`, the root agent history returns empty from `get_session_history()`, causing the UI to show no conversation for the main session.
- **Suggested Fix**: After creating the main instance, also populate sub_agent_state:
  ```python
  agent_pool.sub_agent_state['root'] = {
      'active': False,
      'agent_name': 'Maine (OrchestratorAgent)',
      'messages': [],
  }
  ```

### Issue #10: `instance_loggers` Returns NoOpLogger Dict — Sub-Agent Recovery Unreliable

- **Severity**: MAJOR
- **Location**: api_server.py:1449; agent_pool.py:578-580
- **Problem**: The resume handler tries to recover sub-agent conversations from log files via `agent_pool.instance_loggers.get(sa_name)`. But the new pool's `instance_loggers` returns NoOpLogger instances with no `log_path` attribute. Recovery falls through to glob patterns.
- **Impact**: Sub-agent conversation recovery during resume is unreliable — depends on file system glob patterns rather than actual logger state. If log files are missing or named differently, recovery silently fails.

### Issue #11: `_InstanceConversationMapping.__contains__` vs `.keys()`/`.items()` Divergence

- **Severity**: MAJOR **[New — found by reviewer]**
- **Location**: agent_pool.py:86-140
- **Problem**: `__iter__` and `__contains__` check both `pool.instances` AND dict storage (for renamed entries). But `.keys()`, `.items()`, and `.values()` only iterate over `pool.instances`. If a session rename happens, iteration via `.items()` would miss the renamed entry even though `__contains__` says it exists.
- **Impact**: Inconsistent behavior can cause subtle bugs in any code that iterates `instance_conversations.items()`. For example, if `set_session_name` creates a dict-only entry for the new name, `.items()` won't see it but `'new_name' in mapping` returns True.
- **Suggested Fix**: Make `.keys()`, `.items()`, and `.values()` also check dict storage:
  ```python
  def keys(self):
      seen = set()
      for name in self._pool.instances:
          yield name
          seen.add(name)
      for key in super().keys():
          if key not in seen:
              yield key
  
  def items(self):
      seen = set()
      for name, inst in self._pool.instances.items():
          yield name, inst.conversation
          seen.add(name)
      for key in super().keys():
          if key not in seen:
              yield key, super().__getitem__(key)
  ```

---

## MINOR Issues

### Issue #12: `agent_pool.stopped` Property Uses Event — Reset Sets It to True Permanently

- **Severity**: MINOR
- **Location**: api_server.py:1629; agent_pool.py:230-240
- **Problem**: After reset, `agent_pool.stopped = True` is set. If the user never sends a message after reset, the stopped event remains set. Any background thread checking `pool.stopped` will think everything should stop.

### Issue #13: `_sync_instance_conversations()` Only Called Once — Stale After Instance Creation

- **Severity**: MINOR
- **Location**: agent_pool.py:652-654; api_server.py:2347
- **Problem**: The `instance_conversations` property checks `hasattr(self, '_instance_conversations')` and syncs only if the attribute doesn't exist. If instances are created AFTER the first access (e.g., sub-agents spawned during execution), the mapping's dict storage won't include them.

---

## Cross-Cutting Concerns

### Thread Safety Gap
The new pool's `_execution._state_lock` is an RLock, but the orchestrator references a separate `_state_lock` that doesn't exist (Issue #1). Even if we add it via property delegation, there are two locks protecting overlapping data structures — `sub_agent_state` is protected by `_state_lock` in the orchestrator, while `active_stack` is protected by `_execution._state_lock`. If they're the same lock (via delegation), the existing code works. If they're different, deadlocks can occur.

### Backward Compatibility Debt
The `sub_agent_state` dict is maintained by both agent_orchestrator.py AND api_server.py, creating a dual-write problem. The comment at line 2129-2130 acknowledges this: "sub_agent_state[name]['messages'] may be a separate reference and won't reflect in-place edits to instance_conversations". This means the two stores can diverge.

---

## Recommended Fix Priority

1. **Fix #6** (`clear_conversation()` missing) — blocks sub-agent cleanup/completion paths
2. **Fix #1** (`_state_lock` missing) — blocks ALL sub-agent execution (but fix is trivial: add property)
3. **Fix #2** (`active_stack` copy mutation) — breaks security advisor, retry, and all sub-agent UI tabs
4. **Fix #8** (`active_stack` object replacement in submit_task) — threading data-staleness
5. **Fix #3** (`pop()` deletes instance) — breaks session rename
6. **Fix #5** (`__delitem__` deletes instance) — breaks reset and cleanup
7. **Fix #7** (reset clears instances) — related to #5
8. **Fix #4** (security advisor no instance) — cosmetic once #2 is fixed