# Compression Agent Infinite Recursion — Investigation Notes

## Bug Description (2026-05-27)
The compression agent was hitting >95% context usage because it was doing tool work (read_file, code_map) instead of just summarizing. When the compression agent hit the forced compression threshold, it spawned child compressors in an infinite loop:

```
06:54:44 - Context usage at 116.3% for DesignStatusResearcher — FORCEFUL compression
06:54:49 - Compression agent starts doing read_file, code_map (filling its own context)
06:55:08 - Context usage at 96.8% for compression_agent — FORCEFUL compression
06:55:08 - Context usage at 170.0% for compression_agent_child1 — infinite loop
```

## Root Cause Analysis
The `exempt` list in `_inject_compression_warning_for_agent` (line ~796) exempts 'compression_agent' from being HALTED, but the method itself had no guard against triggering forced compression on compression agents.

## How It Was Already Fixed
The fix was already implemented at the **call site** in `hooked_call_llm` (agent_orchestrator.py line 2072):

```python
# Skip compression checks during Compression Agent execution
# Prevents nested compression (circular dependency)
# Exempt ALL compression agent instances (including spawned children like compression_agent_child1)
hook_forced = False
if not instance_name.startswith('compression_agent'):
    hook_forced = self._inject_compression_warning_for_agent(self_agent, instance_name, messages)
```

This guard prevents `_inject_compression_warning_for_agent` from being called for ANY agent whose name starts with 'compression_agent', including child instances like `compression_agent_child1`.

## Why Adding a Guard INSIDE the Method Was Wrong
Adding an exemption inside `_inject_compression_warning_for_agent` would be **dead code** because:

1. **Sub-agent path (line 2072):** The guard at the call site prevents the method from being called for compression agents.
2. **Orchestrator self-path (line 760):** Uses `self.session_name` which is never 'compression_agent'.
3. **Fallback path (direct comp_agent.run()):** Doesn't go through the orchestrator's LLM hook at all.

The proactive approach (prevent the call) is cleaner than the reactive approach (handle it inside the method), because:
- No side effects — no silent context resets that could lose state
- Clearer semantics — the hook is never invoked, so no confusion about what happens when it is
- Less code to maintain

## Test Coverage
Tests exist in `tests/test_compression.py` class `TestNestedCompressionGuard`:
- `test_orchestrator_skips_inject_for_compression_agent` — verifies guard for 'compression_agent'
- `test_orchestrator_calls_inject_for_other_agents` — verifies non-compression agents ARE checked
- `test_orchestrator_skips_inject_for_compression_agent_children` — verifies guard for 'compression_agent_child1'

## Actions Taken
1. Removed dead code addition from `_inject_compression_warning_for_agent` (reverted the original proposed fix)
2. Updated stale line references in test comments from 2049 → 2072 (3 occurrences)