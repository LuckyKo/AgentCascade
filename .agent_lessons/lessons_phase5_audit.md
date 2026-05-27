# Lessons Learned — Phase 5 Audit

## Critical Pattern: Defensive Copy Properties Are NOT Mutable

When a property returns `list(self._data)` (defensive copy), any mutation on the returned list silently does nothing. The old AgentPool had `self.active_stack` as a mutable attribute, but the new pool makes it a read-only property returning a copy.

**Lesson**: When refactoring from mutable attributes to properties, check ALL mutation call sites. If mutation is needed, provide explicit mutation methods (`append()`, `clear()`, `remove()`) rather than exposing the underlying data structure.

## Critical Pattern: Dual-Sided Dicts Must Not Delete Underlying Objects

`_InstanceConversationMapping` syncs writes between `instance_conversations[name]` and `pool.instances[name].conversation`. But its `pop()` and `__delitem__` also DELETE the AgentInstance from pool.instances. This is destructive for rename patterns (`del old_key; set new_key = value`).

**Lesson**: When building a proxy dict that bridges two stores, deletion operations should only clear data — never delete the underlying objects they reference. Add a flag parameter like `remove_instance=False` to control this behavior.

## Critical Pattern: Missing Locks in Refactored Code

The old AgentPool had `self._state_lock = threading.Lock()` which was used by agent_orchestrator.py for protecting sub_agent_state writes. The new pool delegates active_stack management to ParallelAgentManager which has its own `_execution._state_lock`, but the orchestrator references `agent_pool._state_lock` directly.

**Lesson**: When decomposing a god-object, track ALL references to attributes that are being moved or removed. Use grep to find all call sites before removing any attribute. The comment in agent_orchestrator.py:2223 even says "(_state_lock protects both of these shared data structures per agent_pool.py:114)" — the line number reference makes it easy to track what changed.

## Feature Flags Create Divergent Code Paths

`USE_UNIFIED_STATE` creates two separate read paths in `get_session_history()` and `get_agent_state()`. The unified path reads from `sub_agent_state`, but this dict is only populated by the orchestrator for sub-agents — not for the root session. This means unified mode silently returns empty data for the main session.

**Lesson**: When introducing feature flags, ensure BOTH paths work before enabling the flag. Test with the flag ON to catch these issues early.

## sub_agent_state Is a Dual-Write Problem

Both agent_orchestrator.py AND api_server.py write to `sub_agent_state`. The orchestrator writes during sub-agent execution (lines 1964, 2176, 2225), and api_server.py writes for security advisor (line 1767) and manual syncs (lines 1578, 2132, 2168). There's no single source of truth — the dict can diverge from `pool.instances[name].conversation`.

**Lesson**: The Phase 6 cleanup must eliminate this dual-write pattern. Until then, document which code owns which keys in sub_agent_state.

## NoOpLogger Means Logger Recovery Is Broken

The new pool uses NoOpLogger as a placeholder (Phase 1). But the resume handler at api_server.py:1433-1496 tries to recover sub-agent conversations from log files via `agent_pool.instance_loggers.get(sa_name)`. NoOpLogger has no `log_path`, so recovery falls through to glob patterns.

**Lesson**: Placeholder implementations can break downstream code that depends on real behavior. Document known broken paths and add TODO markers.