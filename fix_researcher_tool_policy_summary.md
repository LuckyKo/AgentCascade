# Fix: Researcher Tool-Policy Reporting Mismatch

**Date:** 2026-08-10
**Fix Applied By:** fix_tools_89 (coder)
**Related:** todo.md line 89, investigation_researcher_tool_policy.md, review_researcher_tool_policy_fix.md
**Status:** Fixed and verified (security regression corrected)

## Problem

`system_info` reported 10 tools as "Disabled" for researcher/coder/reviewer agents
(`code_interpreter, copy_file, delete_file, edit_file, propose_skill, re_indent,
shell_cmd, web_extractor, web_search, write_file`), but these tools were actually
available at runtime. This was a reporting bug caused by divergent resolution paths.

## Root Cause

`Agent._get_active_functions(pool=...)` resolved disabled tools with
`instance_override=None`, causing Layer 4 (`DEFAULT_NEW_AGENT_DISABLED_TOOLS`) to fire
for any agent not explicitly listed in template config. The live pool UI dict
(`{"Researcher": []}`) was then unioned afterward — but union can only add, never
subtract, so the baseline persisted in reports even though enforcement (via engine's
instance-aware path) correctly suppressed it.

## Fix Applied (Option A, corrected)

**File:** `agent_cascade/agent.py` lines 190-247 (`_get_active_functions` and `_get_disabled_tool_names`)

Both methods changed to extract **only this agent's entry** from the live pool UI dict
before passing to the resolver as `instance_override`:

```python
# Extract ONLY this agent's entry from live pool UI dict.
# Passing the entire dict would set has_explicit_config=True for all agents,
# breaking Layer 4 fallback for unknown/dynamically discovered agents.
per_agent = {}
if pool is not None and hasattr(pool, '_ui_disabled_tools'):
    ui_dt = getattr(pool, '_ui_disabled_tools', {}) or {}
    if self.name in ui_dt:
        per_agent[self.name] = ui_dt[self.name]
    elif self.agent_type in ui_dt:
        per_agent[self.agent_type] = ui_dt[self.agent_type]

disabled = resolve_disabled_tools_for_agent(
    instance_override={'disabled_tools': per_agent} if per_agent else None,
    template_cfg=...,
    ...
)
```

This makes `has_explicit_config=True` **only** for agents with explicit UI entries,
suppressing Layer 4 baseline for them while preserving it for unknown agents.

### Security Correction (review_researcher_tool_policy_fix.md)

The initial implementation passed the entire `_ui_disabled_tools` dict as instance_override,
which caused `has_explicit_config=True` for ALL agents when any pool entries existed —
breaking Layer 4 protection for unknown/dynamically discovered agents. The corrected fix
extracts only the per-agent entry before passing to the resolver.

## Verification

All tests passed (7/7):

| Agent | Expected | Actual | Status |
|-------|----------|--------|--------|
| Researcher | (none) | (none) | PASS |
| Coder | (none) | (none) | PASS |
| Writer | shell_cmd | shell_cmd | PASS |
| Security | Layer 3 + UI | union applied | PASS |
| Compressor | Layer 3 + UI | union applied | PASS |
| **UnknownAgent** | **Layer 4 baseline** | **10 tools disabled** | **PASS** |
| NewSoulAgent | Layer 4 baseline | 10 tools disabled | PASS |

## Deployment Note

**The AgentCascade server must be restarted for this fix to take effect.** The change is in
`agent_cascade/agent.py`, which is loaded at startup and cached in the running process.
After restart, `system_info` called from a researcher agent should show no disabled tools
(or only genuinely disabled ones per config).

To verify after restart:
1. Spawn a researcher agent via call_agent
2. Have it run system_info
3. Confirm "Disabled Tools" line is absent or empty (no false 10-tool baseline)

## Preserved Behaviors

- Layer 4 acts as genuine fallback for agents without explicit UI entries (security intact)
- Security/Compressor Layer 3 defense-in-depth defaults always apply
- Template config (Layer 2) still merges with instance override (Layer 1)
- Matches existing pattern used in `ws_handlers.py:789-794` and execution engine
