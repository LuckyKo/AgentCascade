---
name: tool-inheritance-audit
description: Systematic verification of tool set inheritance when agents spawn sub-agents, focusing on disabled_tools propagation and auto-skill reflection edge cases.
source: auto-generated
version: "1.0.0"
triggers:
  - "tool inheritance"
  - "disabled tools"
  - "call_agent"
  - "auto-skill"
  - "sub-agent spawn"
generated_by: reviewer
generated_from_task: "Verify tool inheritance during auto-skill reflection turns in AgentCascade"
---

## Goal

Provide a repeatable, evidence-based process to audit how tool configurations (especially `disabled_tools`) are inherited from parent agents to spawned sub-agents, ensuring the intended access model is enforced.

## Procedure

### Step 1 — Locate All Writes to `_generate_cfg_override['disabled_tools']`
- **Action:** Grep for `disabled_tools.*=` in core files (`engine/core.py`, `lifecycle_manager.py`).
- **Goal:** Identify every place where the override dict is mutated.
- **Check:** There should be exactly one write that sets it to all tools (line 908 in core.py) and one pop/deletion (line 1002). Any additional writes indicate a bug.

### Step 2 — Trace the Auto-Skill Gate Logic
- **Action:** Read `_auto_skill_gates_met` and confirm it returns `False` when `_auto_skill_proposed=True`.
- **Goal:** Verify that the reflection's own final turn will disable all tools, preventing spawns with full access.
- **Check:** `_auto_skill_proposed` is set to `True` in `_try_auto_skill_extension` (core.py:370). This should block further tool disabling on subsequent final turns.

### Step 3 — Verify Propagate Settings Merges Caller's Config
- **Action:** Follow `propagate_settings()` in `lifecycle_manager.py`. Confirm it reads `caller_inst._generate_cfg_override` and merges via `merge_disabled_tools` into the child.
- **Goal:** Ensure child's tool set depends on caller's live override at spawn time.
- **Check:** Lines 620-666: `caller_disabled = resolve_disabled_tools_for_agent(instance_override=caller_inst._generate_cfg_override, ...)`.

### Step 4 — Check Agent Class Defaults
- **Action:** Examine `constants.py` for Layer 3 defaults (`DEFAULT_*_DISABLED_TOOLS`).
- **Goal:** Identify which agent classes disable `shell_cmd` by default.
- **Check:** Writer, Security, Compressor all disable `shell_cmd`. Generalist, Orchestrator, Reviewer do not. Coder/Researcher fall through to Layer 4 unless they have explicit config.

### Step 5 — Confirm Instance Reuse Clears Override
- **Action:** Check both `create_instance` reuse branch and `initialize_conversation`.
- **Goal:** Ensure no stale `disabled_tools` leak from previous runs.
- **Check:** `create_instance` (lines 134-166) does NOT clear `_generate_cfg_override`, but `initialize_conversation` (line 363) sets it to `None` for reused instances.

### Step 6 — Validate Layer 4 Applies Correctly
- **Action:** Review `resolve_disabled_tools_for_agent` lines 142-151.
- **Goal:** Confirm that agents without explicit config and not in the exclusion list get `DEFAULT_NEW_AGENT_DISABLED_TOOLS`.
- **Check:** Exclusion list includes `orchestrator`, `security`, `compressor`, `generalist`, `reviewer`, `writer`. Coder/Researcher are NOT excluded → they get Layer 4 defaults (disables `shell_cmd`).

## Tips

### Best Practices
- Always verify the actual source code; historical notes (e.g., `todo.md`) may be outdated.
- When auditing, read the entire call path from spawn to propagation.
- Remember that `_generate_cfg_override` is a per-instance mutable state — check all lifecycle points where it could persist across runs.

### Common Pitfalls
- **Pitfall 1:** Assuming all agent types have full tool access. Writer, Security, Compressor, and (by Layer 4) Coder/Researcher disable `shell_cmd` by default.
- **Pitfall 2:** Overlooking that terminal stop paths may skip the cleanup pop, leaving `disabled_tools` set on a terminated instance. This doesn't affect new spawns if reuse clears it.
- **Pitfall 3:** Forgetting that Layer 4 applies to any agent type not explicitly listed as a core system agent. Add explicit config or change agent_type to exclude.

### Debugging Commands
```bash
# Find all writes to disabled_tools override
grep -n "disabled_tools.*=" engine/core.py lifecycle_manager.py utils/disabled_tools.py

# Check for _auto_skill_proposed assignments
grep -n "_auto_skill_proposed.*=" agent_cascade/engine/core.py

# Verify template agent types
grep -A5 "agent_type\s*=" agents/*_soul.md  # or check soul_loader.py line 350
```

## Related Skills
- `disabled-tools-propagation-audit` (project memory)
- `cross-file-logic-verification`
