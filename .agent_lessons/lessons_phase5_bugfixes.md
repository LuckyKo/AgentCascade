# Lessons Learned — Phase 5 Bug Fixes

## Critical Pattern: Defensive Copy Properties Break Mutation Callers

When a property returns `list(self._data)` (defensive copy), any mutation on the returned list silently does nothing. The new AgentPool made `active_stack` a read-only property returning a copy, but 6 call sites across api_server.py and agent_orchestrator.py were still calling `.append()`, `.clear()`, `.remove()`, and `.pop()` on it.

**Fix**: Add explicit mutation methods (`active_stack_append`, `active_stack_remove`, `active_stack_clear`, `active_stack_pop_at`) that acquire the lock and mutate the underlying data. Update ALL call sites to use these methods.

**Lesson**: When refactoring from mutable attributes to read-only properties with defensive copies, grep for ALL mutation patterns (`.append(`, `.clear(`, `.remove(`, `.pop(`) on the attribute name across the entire codebase.

## Critical Pattern: Proxy Dicts Must Not Delete Underlying Objects

`_InstanceConversationMapping` bridges writes between `instance_conversations[name]` and `pool.instances[name].conversation`. Its `pop()`, `__delitem__()`, and `clear()` methods were deleting AgentInstances from `pool.instances`, which broke session rename and reset flows.

**Fix**: Changed all three methods to only clear the instance's conversation (not delete the instance). Session rename works because the instance survives pop+write-under-new-name. Reset works because instances survive but their conversations are cleared.

**Lesson**: When building a proxy dict that bridges two stores, deletion operations should only clear data — never delete the underlying objects they reference. The principle: "instance_conversations manages data, not lifecycle."

## Critical Pattern: List Reassignment Breaks External References

`ParallelAgentManager.submit_task()` had `self.active_stack = [n for n in self.active_stack if ...]` which replaces the list object entirely. Any external code holding a reference to the old list would have stale data.

**Fix**: Changed to `self.active_stack[:] = [...]` (in-place slice assignment) which preserves the list object identity.

**Lesson**: When managing shared mutable state, always mutate in-place (`list[:] = ...`, `dict.clear()`, etc.) rather than replacing objects. This prevents reference staleness bugs.

## Critical Pattern: Missing Locks After Decomposition

The old AgentPool had `self._state_lock` which agent_orchestrator.py used at 5 call sites. The new pool delegates to ParallelAgentManager which has its own `_execution._state_lock`, but the orchestrator references `agent_pool._state_lock` directly — causing AttributeError.

**Fix**: Added a property delegation: `@property def _state_lock(self): return self._execution._state_lock`. Using RLock (re-entrant) is intentional since compression can run in the same thread as execution.

**Lesson**: When decomposing a god-object, track ALL references to attributes being moved or removed. Use grep to find all call sites before removing any attribute. Comment with line number references for traceability.

## Feature: clear_conversation() Method Was Missing

agent_orchestrator.py calls `self.agent_pool.clear_conversation(instance_name)` at 2 locations (class mismatch cleanup and terminated instance cleanup) but the method didn't exist on the new pool.

**Fix**: Added the method that clears an instance's conversation while keeping the instance alive.

**Lesson**: When refactoring a class, audit ALL methods called by external modules. Missing methods cause AttributeError at runtime, not compile time.

## reset() Method Should Not Destroy Instances

The old `reset()` called `_instance_conversations.clear()` which called `self._pool.instances.clear()`, destroying ALL instances including the main orchestrator. After reset, the main session was gone.

**Fix**: Changed reset to use `active_stack_clear()` (mutation method) and clear conversations without deleting instances. The fallback path also only clears conversations.

**Lesson**: "Reset" should clear state, not destroy objects. Distinguish between "clear all state" and "destroy everything."