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

## Legacy Path — Already Migrated (ExecutionEngine is the main path)
The legacy execution path (`FnCallAgent._run()` → `FnCallAgent._call_tool()`) is **not**
the primary execution path. All agent execution in the unified system routes through
`ExecutionEngine.run()` which handles:
- LLM calls via `_execute_llm_call()`
- Tool calls via `_execute_tool()` → `template._call_tool()`

The full MRO dispatch chain for tool calls is:
```
ExecutionEngine._execute_tool() → template._call_tool() → FnCallAgent._call_tool() → Agent._call_tool() → tool.call()
```

### What's Truly Dead Code
- **Nothing in FnCallAgent is truly dead code** — its methods are all exercised via MRO.
  The only thing that changed is that ExecutionEngine no longer calls `_run()` directly;
  it has its own LLM/tool loop instead.

### What's Live (via MRO from ExecutionEngine)
| Method | Status | Reason |
|--------|--------|--------|
| `FnCallAgent._run()` | **Live** | Called by `Assistant._run() → super()._run()` when standalone Assistant instances are used outside ExecutionEngine (e.g., WriteFromScratch). Classes like ArticleAgent, ReActChat, TIRMathAgent define their own `_run()` and do NOT call `super()._run()`. |
| `FnCallAgent._call_tool()` | **Live** | In MRO chain when ExecutionEngine calls `template._call_tool()` on Assistant instances; file_access branch unused by standard tools |
| `Agent._call_tool()` | **Live** | Called via super() from FnCallAgent._call_tool() |
| `Agent._resolve_tool_args()` | **Live** | Called by Agent._call_tool() for __USE_PREV_ARG__ resolution |

### Classes That Inherit from FnCallAgent (still live)
- **Assistant** — used as AgentPool template; `_run()` also called when standalone instances created outside ExecutionEngine (e.g., WriteFromScratch creates them directly)
- **ReActChat** — ReAct chat agent
- **TIRMathAgent** — math reasoning agent

Note: ReActChat and TIRMathAgent are defined but never instantiated in the codebase.
They exercise `FnCallAgent._call_tool()` only if loaded as templates; their `_run()` methods
are independent (no `super()._run()` call).

These classes are instantiated as templates in AgentPool. ExecutionEngine uses their `llm`,
`function_map`, and `system_message` attributes directly — it never calls their `_run()` method.
However, `_run()` IS still exercised from non-ExecutionEngine code paths (e.g., WriteFromScratch
creates standalone Assistant instances and calls `.run()` on them).

### Migration Status
✅ Already migrated. The legacy path was superseded by the ExecutionEngine refactor (Phase 3).
The FnCallAgent class remains for backward compatibility (external code imports it) and its
methods are exercised via MRO from both ExecutionEngine and standalone agent paths.

## Future Cleanup
Consider whether `BasicAgent` is still needed — it may be dead code (check if any templates use it).
FnCallAgent should remain until external code stops importing it directly.