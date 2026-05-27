# Phase 5 Compatibility Shims — Lessons Learned

**Date:** 2026-05-27  
**Agent:** Phase5Continuer  
**Task:** Add compatibility shims to new AgentPool for api_server.py backward compatibility  

---

## What Was Done

Added 14 compatibility shims to `agent_cascade/agent_pool.py` so that the existing `api_server.py` can work with the new unified pool without modification. These are thin wrappers — most are one-liners.

### Shims Added

| # | Shim | Type | Implementation |
|---|------|------|----------------|
| 1 | `is_halted(name)` | Method alias | Delegates to `is_instance_halted(name)` |
| 2 | `list_agents()` | Method | Returns `list(self.templates.keys())` |
| 3 | `reset()` | Method | Clears halted, compression_halted, terminated_instances, active_stack, last_tool_args |
| 4 | `active_stack` | Property | Delegates to `self._execution.active_stack` |
| 5 | `last_tool_args` | Attribute | Dict initialized in `__init__` (same type as old pool: `Dict[str, Dict[str, Dict[str, Any]]]`) |
| 6 | `rollback_to_snapshots(snapshots, soft, reason)` | Method | Truncates instance conversations to target lengths; notifies loggers |
| 7 | `capture_snapshots()` | Method | Returns `{name: len(inst.conversation) for name, inst in self.instances.items()}` |
| 8 | `load_session_from_log(log_input, target_instance)` | Method | Full implementation — reads JSONL, restores conversation, sets up logger |
| 9 | `refresh_agents()` | Method | Clears templates and re-calls `_discover_agents()` |
| 10 | `instance_classes` | Property | Derived: `{name: inst.agent_class for name, inst in self.instances.items()}` |
| 11 | `instance_loggers` | Property | Delegates to `self._logger._loggers` |
| 12 | `instance_summaries` | Attribute | Dict initialized in `__init__` (`Dict[str, str]`) |
| 13 | `_ws_loop` | Attribute | Set to `None` in `__init__`; api_server sets it at runtime |
| 14 | `agents` | Property | Alias for `self.templates` |

### Key Design Decisions

1. **load_session_from_log is the most complex shim** — It needed a full implementation because it reads JSONL files, parses messages, extracts compression summaries, and creates/updates AgentInstance objects. Adapted from old agent_pool.py with modifications to use new `self.instances` dict instead of `self.instance_conversations`.

2. **rollback_to_snapshots uses the new instance model** — Instead of truncating `instance_conversations[name]`, it truncates `instances[name].conversation`. Also notifies the logger via LoggerManager.

3. **reset() doesn't clear instances/conversations** — The old reset() cleared everything including conversations. Our version only clears halt state, active_stack, terminated_instances, and last_tool_args. This is intentional — the api_server.py calls reset() after a stop command, and we don't want to destroy instance data. If full cleanup is needed, the caller can use other methods.

4. **instance_summaries as mutable dict attribute** — api_server.py both reads from AND writes to this (e.g., `agent_pool.instance_summaries[name] = match.group(1).strip()`). Making it a property returning a new dict each time would break write operations. So it's a persistent attribute like the old model.

5. **_ws_loop is None by default** — api_server.py sets this at runtime in run_agent_thread. The dismissal callback reads it via `getattr(agent_pool, '_ws_loop', None)`.

## Verification

- All 14 shims verified present via pattern matching
- Python syntax check passed (ast.parse + py_compile)
- No modifications to ExecutionEngine or other modules as instructed

## Next Steps (Phase 5 continued)

- Switch pool import in api_server.py `__main__` block from old `agent_pool` to new `agent_cascade.agent_pool`
- Update pool constructor call (signature differs between old and new)
- Verify WebSocket handler works with unified pool
- Full integration testing