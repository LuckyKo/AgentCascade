# Review: Researcher Tool-Policy Reporting Fix

**Date:** 2026-08-10  
**Reviewer:** review_tools_89 (quality assurance specialist)  
**Task:** Review fix for todo.md line 89 (researcher tool-policy reporting mismatch)  
**Files Reviewed:**
1. `investigation_researcher_tool_policy.md` — investigation report with Option A details
2. `agent_cascade/agent.py` lines 190-240 — the actual fix
3. `fix_researcher_tool_policy_summary.md` — coder's summary

---

## Executive Summary

The fix **correctly identifies the root cause** and **implements Option A in principle**, but contains a **CRITICAL implementation flaw** that breaks Layer 4 fallback protection for unknown agents. The fix is **NOT minimal** and **overreaches** by feeding the entire UI dictionary as instance_override instead of extracting only the per-agent entry.

**Verdict: FAIL** — The fix must be corrected before deployment due to security regression.

---

## Detailed Findings

### 1. Root Cause Analysis ✅ CORRECT
**Severity:** 🟢 N/A (Analysis was correct)

The investigation correctly identified that:
- `system_info` and reporting path use `instance_override=None`, causing Layer 4 baseline to fire
- Execution engine uses per-instance override, suppressing Layer 4
- This creates a divergence where reported disabled tools don't match actual enforcement

**Evidence:** Investigation report findings 1-7 all accurate. The fix targets the right methods (`_get_active_functions` and `_get_disabled_tool_names`).

### 2. Implementation of Option A ⚠️ PARTIALLY INCORRECT
**Severity:** 🔴 Critical

The fix in `agent_cascade/agent.py` lines 200-210 and 225-235 does this:

```python
live = {}
if pool is not None and hasattr(pool, 'get_ui_disabled_tools_for_agent'):
    live = dict(getattr(pool, '_ui_disabled_tools', {}) or {})

disabled = resolve_disabled_tools_for_agent(
    instance_override={'disabled_tools': live} if live else None,
    ...
)
```

**The Bug:** When `pool._ui_disabled_tools` is non-empty (which it always is after loading pool_settings.json), `live` becomes a truthy dict. This sets `has_explicit_config=True` in the resolver **for every agent**, regardless of whether that specific agent has an entry in the UI dictionary.

**Why This Matters:** The resolver's Layer 4 fallback logic:
```python
if not has_explicit_config and atype_lower not in ('orchestrator', 'security', 'compressor'):
    disabled |= DEFAULT_NEW_AGENT_DISABLED_TOOLS
```

With the current fix, `has_explicit_config` is always `True` when a pool is provided and `_ui_disabled_tools` contains any entries. This means:

- ✅ Known agents with explicit UI entries (Researcher, Coder, Reviewer) work correctly
- ✅ Known agents with restrictive entries (Writer, Security, Compressor) work correctly  
- ❌ **Unknown agents lose Layer 4 protection** — they get ALL tools enabled instead of starting read-only

### 3. Layer 4 Fallback Broken ❌
**Severity:** 🔴 Critical

The investigation report claims: *"Keeps Layer 4 as genuine fallback for classes with NO UI entry (unknown agents stay read-only)."*

This is **false** with the current implementation. Testing shows:

```python
# With non-empty _ui_disabled_tools:
resolve({'disabled_tools': full_ui_dict}, {}, 'UnknownAgent', 'UnknownAgent')
# Returns: set()  # NO tools disabled — Layer 4 SKIPPED
```

The security model assumes unknown/dynamically discovered agents start read-only until explicitly granted more access. The fix inverts this: unknown agents become unrestricted.

### 4. Edge Case: Feeding Live UI Dict as instance_override ⚠️
**Severity:** 🟠 Major

Passing the **entire** `_ui_disabled_tools` dictionary as `instance_override` breaks the assumption that instance overrides are per-agent. The resolver treats any non-None `instance_override` with a `disabled_tools` key as explicit configuration for ALL agents.

This is inconsistent with how `execution_engine._get_active_functions_from_template` uses instance overrides — it passes `_generate_cfg_override` which is **per-instance**, not a global dictionary of all agents.

### 5. Backward Compatibility ⚠️
**Severity:** 🟡 Minor (but tied to critical issue)

The fix improves consistency between reporting and enforcement for known agents, which is good. However, the security regression for unknown agents is a breaking change in behavior that could affect:

- Custom soul-discovered agents not in pool_settings.json
- Future agent classes added without updating config
- Any code path that creates agents outside the known set

### 6. Minimal and Surgical? ❌
**Severity:** 🟠 Major

The fix is **not minimal**. It should extract only the per-agent entry from `_ui_disabled_tools` instead of passing the whole dictionary. The investigation report's recommended code snippet is itself flawed for the same reason it claims to preserve Layer 4.

---

## Required Changes

### Change 1: Extract Per-Agent Entry Only
**File:** `agent_cascade/agent.py` (both `_get_active_functions` and `_get_disabled_tool_names`)

Replace:
```python
live = {}
if pool is not None and hasattr(pool, 'get_ui_disabled_tools_for_agent'):
    live = dict(getattr(pool, '_ui_disabled_tools', {}) or {})

disabled = resolve_disabled_tools_for_agent(
    instance_override={'disabled_tools': live} if live else None,
    ...
)
```

With:
```python
per_agent_override = {}
if pool is not None and hasattr(pool, '_ui_disabled_tools'):
    ui_dt = getattr(pool, '_ui_disabled_tools', {}) or {}
    # Extract ONLY the entry for this specific agent
    if self.name in ui_dt:
        per_agent_override[self.name] = ui_dt[self.name]
    elif self.agent_type in ui_dt:
        per_agent_override[self.agent_type] = ui_dt[self.agent_type]

instance_override = {'disabled_tools': per_agent_override} if per_agent_override else None

disabled = resolve_disabled_tools_for_agent(
    instance_override=instance_override,
    ...
)
```

### Change 2: Verify Security for Unknown Agents
**File:** Add test in `tests/test_disabled_tools_resolution.py` (or similar)

```python
def test_unknown_agent_layer4_preserved():
    """Unknown agents should still get Layer 4 baseline even when pool has entries."""
    ui_disabled_tools = {
        "Researcher": [],
        "Coder": [],
        "Writer": ["shell_cmd"],
    }
    # Simulate the fixed code extracting per-agent entry
    per_agent = {}
    agent_name = "UnknownAgent"
    agent_type = "UnknownAgent"
    if agent_name in ui_disabled_tools:
        per_agent[agent_name] = ui_disabled_tools[agent_name]
    elif agent_type in ui_disabled_tools:
        per_agent[agent_type] = ui_disabled_tools[agent_type]
    
    instance_override = {'disabled_tools': per_agent} if per_agent else None
    
    disabled = resolve_disabled_tools_for_agent(
        instance_override=instance_override,
        template_cfg=None,
        agent_name=agent_name,
        agent_type=agent_name,
    )
    assert DEFAULT_NEW_AGENT_DISABLED_TOOLS.issubset(disabled)
```

### Change 3: Update Investigation Report
Document that Option A as originally proposed contains this subtle bug and needs the per-agent extraction fix.

---

## Verification Steps After Fix

1. **Test Researcher agent:** Should have NO disabled tools (empty set)
2. **Test Writer agent:** Should have `shell_cmd` disabled
3. **Test Security/Compressor:** Should have Layer 3 defaults + UI union
4. **Test UnknownAgent:** Should have Layer 4 baseline (10 tools + sentinel)
5. **Run system_info from a Researcher agent:** Should not report false disabled tools

---

## Re-Review: Corrected Fix (Post-Security Regression Fix)

**Date:** 2026-08-10  
**Reviewer:** review_tools_89  

### Changes Verified

The fix in `agent_cascade/agent.py` lines 190-247 has been corrected to extract **only the per-agent entry** from `_ui_disabled_tools` before passing to the resolver:

```python
per_agent = {}
if pool is not None and hasattr(pool, '_ui_disabled_tools'):
    ui_dt = getattr(pool, '_ui_disabled_tools', {}) or {}
    if self.name in ui_dt:
        per_agent[self.name] = ui_dt[self.name]
    elif self.agent_type in ui_dt:
        per_agent[self.agent_type] = ui_dt[self.agent_type]

disabled = resolve_disabled_tools_for_agent(
    instance_override={'disabled_tools': per_agent} if per_agent else None,
    ...
)
```

This directly addresses the critical flaw identified in the initial review.

### Verification Results

All 9 test scenarios passed:

| Agent | Expected | Actual | Status |
|-------|----------|--------|--------|
| Researcher | (none) | (none) | ✅ PASS |
| Coder | (none) | (none) | ✅ PASS |
| Writer | shell_cmd | shell_cmd | ✅ PASS |
| Security | Layer 3 + UI | union applied | ✅ PASS |
| Compressor | Layer 3 + UI | union applied | ✅ PASS |
| **UnknownAgent** | **Layer 4 baseline** | **10 tools disabled** | **✅ PASS** |
| **NewSoulAgent** | **Layer 4 baseline** | **10 tools disabled** | **✅ PASS** |
| Researcher (empty pool) | Layer 4 baseline | 10 tools disabled | ✅ PASS |
| UnknownAgent (non-empty pool) | Layer 4 baseline | 10 tools disabled | ✅ PASS |

### Security Regression Status

✅ **FIXED** — Layer 4 fallback is now correctly preserved for agents without explicit UI entries. The fix no longer breaks the defense-in-depth model.

### Remaining Concerns

None. All previous concerns have been addressed:
- Per-agent extraction implemented correctly
- Layer 4 fallback preserved for unknown agents
- Backward compatibility maintained
- Minimal and surgical implementation

## Final Verdict

**PASS** — The corrected fix successfully resolves the reporting mismatch while maintaining security boundaries.

**Approved for deployment.** After restart, `system_info` from a Researcher agent will show no false disabled tools.

---

*Initial review: FAIL (security regression)*  
*Re-review: PASS (regression fixed)*  
*Review completed by review_tools_89. Report saved to N:\work\WD\AgentCascade\review_researcher_tool_policy_fix.md*
