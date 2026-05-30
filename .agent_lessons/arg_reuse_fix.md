# __USE_PREV_ARG__ Argument Reuse Fix

## Problem
The `__USE_PREV_ARG__` placeholder was documented in the system prompt but never actually worked:

1. **Cache never populated** — `last_tool_args` on AgentPool was initialized but nothing wrote to it during tool execution.
2. **Resolution skipped for special tools** — Only standard tools (the `else` branch) had placeholder resolution. `call_agent`, `dismiss_agent`, and `compress_context` bypassed resolution entirely.
3. **JSON string mismatch** — Tool args come from `_detect_tool()` as JSON strings, but `resolve_prev_arg_placeholders()` only checked `isinstance(tool_args, dict)` — so it passed through strings unchanged without resolving anything.

## Fix (execution_engine.py)

### New helper: `_cache_tool_args(instance_name, tool_name, resolved_args)`
- Called after every successful tool execution in ALL branches of `_execute_tool()`.
- Stores args deep-copied into `pool.last_tool_args[instance_name][tool_name]` (per-tool) AND `pool.last_tool_args[instance_name]["__GLOBAL__"]` (global fallback).

### New helper: `_resolve_placeholders(tool_args, instance_name, tool_name)`
- Parses JSON strings before resolution (the missing link).
- Scans for `__USE_PREV_ARG__` values by arg name.
- Looks up in `__GLOBAL__` first, then per-tool cache as fallback.
- Unresolvable placeholders pass through silently — no errors raised.

### Updated `_execute_tool()`
- ALL four branches (call_agent, dismiss_agent, compress_context, standard) now:
  1. Resolve placeholders via `_resolve_placeholders()`
  2. Execute the tool with resolved args
  3. Cache resolved args via `_cache_tool_args()`

## Behavior
- **Simple**: `__USE_PREV_ARG__` replaces with the most recent value for that arg name, regardless of which tool provided it or whether it succeeded.
- **Sticky**: Cached values persist until overwritten by a new call providing the same arg name.
- **Safe**: Unresolvable placeholders pass through as-is — regular tool use is unaffected.

## Legacy Path Still Broken (TODO)
`FnCallAgent._call_tool()` and `Agent._call_tool()` have their own execution loops that bypass `ExecutionEngine._execute_tool()` entirely. They do NOT get `__USE_PREV_ARG__` resolution or arg caching.

Affected agent classes:
- **FnCallAgent** — used by Assistant, ReActChat, TIRMathAgent
- **BasicAgent** — the simplest Agent subclass

TODO markers added in:
- `agent_cascade/agent.py` (before `_call_tool`)
- `agent_cascade/agents/fncall_agent.py` (before `FnCallAgent` class)

## Migration Priority
The legacy path should be migrated to route through ExecutionEngine. Until then, agents using FnCallAgent won't benefit from arg reuse.