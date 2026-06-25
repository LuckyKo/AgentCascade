# Compressor disabled_tools Bug Investigation Notes

## Trace Summary (as of 2025-06-25)

### Call Chain for Tool Filtering

```
engine.run(comp_instance)
  → _setup_turn(instance, ...)       [line 873]
      → template = pool.get_template(instance.agent_class)  [line 937]
      → _build_resources_block(pool, template, instance)   [line 990]
          → resolve_disabled_tools_for_agent(...)           [lines 259-264]
  → _call_llm_with_injection(instance, llm_messages)       [line ~1527]
      → template = pool.get_template(instance.agent_class) [line 1542]
      → _get_active_functions_from_template(template, instance)  [line 1548]
          → resolve_disabled_tools_for_agent(...)           [lines 119-124]
```

### Key Functions

#### 1. `_get_active_functions_from_template(template, instance)` — execution_engine.py:81-137
- Reads `instance._generate_cfg_override` (Layer 1)
- Reads `template.llm.generate_cfg` (Layer 2)
- Falls back to `instance.agent_class` if `template.agent_type` is empty (lines 114-117)
- Calls `resolve_disabled_tools_for_agent()` which applies Layer 3 defense-in-depth

#### 2. `_build_resources_block(pool, template, instance)` — execution_engine.py:236-291
- Same resolution logic as above BUT does NOT have the `instance.agent_class` fallback
- This is only used for the system prompt's tool list display (not actual filtering)

#### 3. `resolve_disabled_tools_for_agent()` — utils/disabled_tools.py:68-129
Three layers accumulated top-down:
- Layer 1: instance `_generate_cfg_override['disabled_tools']`
- Layer 2: template `llm.generate_cfg['disabled_tools']`
- Layer 3: Class defaults (Security → ALL_USER_APPROVAL_TOOLS, Compressor → ALL_USER_APPROVAL_TOOLS + call_agent/dismiss_agent/list_agents)

### Creation Flow for Compressor

```
agent_invoker.invoke_compression_agent(...)
  → engine._create_system_agent('Compressor', ...)   [line 150]
      → lifecycle.find_or_create_instance(..., force_fresh=True)
      → lifecycle.propagate_settings(inst, caller, 'Compressor')  [line 2904]
          → sets instance._generate_cfg_override with max_input_tokens + disabled_tools
  → agent_invoker reads caller's UI disabled_tools    [lines 168-189]
  → merge_disabled_tools_for_auto_agent(...)          [lines 192-197]
  → cfg['disabled_tools'] = merged                    [line 199]
  → comp_instance._generate_cfg_override = cfg        [line 202] ← OVERWRITES propagate_settings()
```

### Known Issues Found

#### Issue A: agent_invoker overwrites propagate_settings (not a bug, by design)
- `propagate_settings()` sets `_generate_cfg_override` at lines 531-564
- `agent_invoker.py` creates a fresh cfg from template and OVERRIDES it at line 202
- This is intentional (comment at line 157-160) but means propagate_settings' work is discarded

#### Issue B: _build_resources_block missing agent_class fallback
- `_get_active_functions_from_template` has a fallback: if `template.agent_type` is empty, 
  it reads `instance.agent_class` (lines 114-117)
- `_build_resources_block` does NOT have this fallback (line 262)
- If Compressor template lacks `agent_type`, the system prompt tool list won't include 
  Layer 3 defense-in-depth defaults, but actual filtering WILL still work

#### Issue C: propagate_settings doesn't set _generate_cfg_override if no propagated_max
- Lines 533-536: `if propagated_max:` — only sets override when max_input_tokens is truthy
- If caller has no max_input_tokens config, `_generate_cfg_override` stays None after 
  the first block but gets properly set in the second block (lines 557-564)

## DEFAULT_COMPRESSOR_DISABLED_TOOLS
Contains ALL_USER_APPROVAL_TOOLS + call_agent/dismiss_agent/list_agents:
- shell_cmd, code_interpreter, write_file, edit_file, delete_file, copy_file, move_file
- call_agent, dismiss_agent, list_agents

Total: 10 tools disabled by default.

## Constants (agent_cascade/constants.py)
- `ALL_USER_APPROVAL_TOOLS`: shell_cmd, code_interpreter, write_file, edit_file, delete_file, copy_file, move_file
- `DEFAULT_COMPRESSOR_DISABLED_TOOLS` = ALL_USER_APPROVAL_TOOLS | {call_agent, dismiss_agent, list_agents}
- `NON_LLM_KEYS`: ['max_turns', 'disabled_tools', '_on_token_count'] — stripped before LLM API call

## Fix Applied 2026-06-25: propagate_settings() Per-Agent Dict Resolution

### Root Cause
`propagate_settings()` in lifecycle_manager.py only resolved disabled tools FOR THE CALLER agent,
then propagated that flat list to the child. It never looked up per-agent entries from the caller's
dict (e.g., `{'Compressor': ['dismiss_agent', 'simple_doc_parser']}`).

### Two Code Paths for Compressor Creation
1. **System path**: `invoke_compression_agent()` → `_create_system_agent()` → reads caller's UI config directly, extracts per-agent entries (agent_invoker.py lines 175-203) — WORKS
2. **User call_agent path**: `call_agent(agent_class='compressor')` → `_create_and_run_agent()` → `propagate_settings()` — BROKE: only resolved caller's own tools

### Fix
Added second `resolve_disabled_tools_for_agent()` call in propagate_settings() (lifecycle_manager.py lines 560-568) that resolves disabled tools FOR THE CHILD AGENT using the child's name and agent_type. This extracts per-agent entries like `'Compressor': [...]` from the caller's dict.

### Key Changes
- lifecycle_manager.py: Added target_name/target_type resolution + fallback to instance.agent_class
- Both calls are idempotent — defense-in-depth Layer 3 defaults are set unions, so double-application is harmless