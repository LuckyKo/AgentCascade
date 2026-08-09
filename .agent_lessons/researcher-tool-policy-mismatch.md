---
tags: [disabled-tools, tool-policy, researcher, system-info, layer4]
aliases: [researcher-tool-policy-mismatch, system-info-disabled-tools-false-positive]
related: [[lessons-disabled-tool-auto-deny-fix]], [[lessons-realtime-tool-assignment]]
confidence: verified
---

# Researcher tool-policy mismatch: system_info reports baseline as disabled but tools actually work

**Fact:** For non-core agents (researcher, coder, reviewer, writer, generalist), `system_info`
reports a large "Disabled Tools" list (code_interpreter, write_file, edit_file, shell_cmd,
web_search, web_extractor, copy_file, delete_file, re_indent, propose_skill) that does NOT match
what is actually enforced at runtime. The tools actually work.

## Root cause

`resolve_disabled_tools_for_agent()` in `agent_cascade/utils/disabled_tools.py` (Layer 4,
lines 135-143) applies `DEFAULT_NEW_AGENT_DISABLED_TOOLS` (read-only baseline from
`agent_cascade/constants.py:58-79`) to ANY agent that:
- has NO explicit `disabled_tools` config from Layers 1-2 (`has_explicit_config == False`), AND
- is not orchestrator/security/compressor.

This Layer 4 is *by design* a static baseline that can never be undone — it is only suppressed
when an explicit config exists. The divergence happens because:

1. **Reporting path** (`tools/custom/system_info.py:118-136`): calls
   `template._get_active_functions(pool=self.agent_pool)` →
   `Agent._get_active_functions()` (`agent.py:199-211`) with `instance_override=None`.
   With no instance override and template.llm.generate_cfg having no `disabled_tools`,
   Layer 4 fires → baseline disabled. Then it adds live pool UI disabled tools on top
   (union semantics). Result: reporting shows baseline + UI tools = ~10 tools disabled.

2. **Execution path** (`execution_engine.py:3516` → `_get_active_functions_from_template`,
   lines 135-213): passes `instance._generate_cfg_override` (Layer 1). When a researcher
   child is spawned via `call_agent`, `lifecycle_manager.propagate_settings()`
   (`lifecycle_manager.py:602-771`) **always** sets `instance._generate_cfg_override`
   (at minimum `cfg['disabled_tools'] = list(merged)`), so `has_explicit_config=True`
   → Layer 4 suppressed → researcher tools all enabled.

So: **The Layer-4 baseline only "applies" at template/reporting level, where no instance
override exists.** Instances always have an override after propagation, so enforcement
never actually disables these tools for spawned researchers — but system_info lies.

## Empirical confirmation (2026-08-10, session investigator_tools_89, researcher class)

- `system_info` in-session reported exactly: `Disabled Tools: code_interpreter, copy_file,
  delete_file, edit_file, propose_skill, re_indent, shell_cmd, web_extractor, web_search,
  write_file` — this is `DEFAULT_NEW_AGENT_DISABLED_TOOLS` minus the `__all_mcp_tools__` sentinel.
- `write_file` probe to `N:\work\WD\AgentWorkspace\temp\researcher_write_file_probe.txt` succeeded.
- `code_interpreter` executed successfully (the simulation scripts above).
- Resolver test: `resolve(None, {}, 'Researcher','Researcher')` → baseline; 
  `resolve({'disabled_tools': ['forget_last']}, {}, ...)` → only ['forget_last'] (no baseline).
- Saved `config/pool_settings.json` has `"Researcher": []` (explicit empty = user grant),
  but template-level template-only resolution ignores that dict entirely.

## Secondary design flaw (worse)

`Agent._get_active_functions()` and `_get_disabled_tool_names()` resolve with
`instance_override=None` — the template-level query. The live UI dict is merged AFTER
resolution via `pool.get_ui_disabled_tools_for_agent()` using **union semantics**.
Union cannot REMOVE a tool that Layer 4 already disabled, so a user granting tools to a
non-core agent via the UI settings panel can never re-enable anything that Layer 4
disabled at the template level. This affects authoring flows (write_file/edit_file/web_search
for researcher/coder/reviewer etc.) where system prompt injection (`_build_resources_block`,
execution_engine.py:452-480) also uses template-level resolution → same false report.

## Recommended fix (minimal)

In `Agent._get_active_functions()` / `_get_disabled_tool_names()` (agent.py:199-227): stop
passing `instance_override=None`. Feed the live pool UI dict into the resolver call itself,
then drop the separate post-hoc union (or keep union but with the dict fed as override so
Layer 4 is suppressed when the UI explicitly lists the agent class). E.g.:

```python
from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent
live = {}
if pool is not None and hasattr(pool, 'get_ui_disabled_tools_for_agent'):
    live = pool._ui_disabled_tools  # or a snapshot getter
disabled = resolve_disabled_tools_for_agent(
    instance_override={'disabled_tools': live} if live else None,
    template_cfg=(getattr(self.llm, 'generate_cfg', None) or {}),
    agent_name=self.name,
    agent_type=getattr(self, 'agent_type', '') or '',
)
```

This keeps Layer 4 as a true fallback for agents with NO UI entry (security posture
intact — unknown/mystery classes still get read-only baseline) while honoring explicit
grants like `"Researcher": []`.

## Files touched

- `agent_cascade/agent.py:199-227` (reporting path — root of system_info false report)
- `agent_cascade/utils/disabled_tools.py:135-143` (Layer 4 semantics — could add doc note)
- `agent_cascade/execution_engine.py:135-213` (already correct — instance-aware)
- `tools/custom/system_info.py:118-136` (reporting — consumes `_get_active_functions`)
- `config/pool_settings.json:84` (`"Researcher": []` explicit grant exists)