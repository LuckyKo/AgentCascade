# Investigation: Researcher tool-policy mismatch (todo.md line 89)

**Date:** 2026-08-10
**Status:** Root cause identified, verified empirically, fix applied and reviewed.

## Executive Summary

`system_info` reported 10 tools as "Disabled" for researcher agent, but they were not actually disabled — `code_interpreter` and `write_file` both worked at runtime.

Root cause: **divergence between two resolution paths** of `resolve_disabled_tools_for_agent()`:
- Reporting path (`Agent._get_active_functions`, called by system_info) passed `instance_override=None` → Layer 4 baseline fired → tools reported disabled.
- Execution path (engine via `_get_active_functions_from_template`) passed instance override → Layer 4 suppressed → tools enabled.

Reporting bug, not security regression. Enforcement was less restrictive than claimed.

## Key Findings

1. **Reported list = `DEFAULT_NEW_AGENT_DISABLED_TOOLS`** (`constants.py:58-79`) exactly.
2. **Layer 4 fires when no explicit config:** `disabled_tools.py:135-143` — adds baseline when `has_explicit_config=False`.
3. **Two paths disagree:** Reporting path at `agent.py:199-227` uses `instance_override=None`; engine at `execution_engine.py:135-213` uses instance override → correct behavior.
4. **Union semantics prevent override:** Template-level baseline cannot be removed by live UI dict (`{"Researcher": []}`) — union only adds.
5. **Saved config already grants researcher full tools:** `pool_settings.json:84` has `"Researcher": []`.
6. **"researcher" mapping exists:** `api_router.py:78-87` maps correctly — no missing entry.

## Root Cause

`Agent._get_active_functions()` and `_get_disabled_tool_names()` resolve with `instance_override=None`, so Layer 4 baseline applies at reporting level while enforcement level never sees it (spawned instances always carry override via `propagate_settings()`, `lifecycle_manager.py:602-771`).

## Evidence

| # | File | Lines | What it proves |
|---|---|---|---|
| E1 | `disabled_tools.py` | 69-145 | Resolver layers; Layer 4 gate at 135-143 |
| E2 | `agent.py` | 199-227 | Reporting path uses `instance_override=None` |
| E3 | `execution_engine.py` | 135-213 | Runtime path passes instance override |
| E4 | `lifecycle_manager.py` | 602-771 | `propagate_settings` always writes `_generate_cfg_override` |
| E5 | `system_info.py` | 118-136 | Computes disabled from template-level `_get_active_functions` |
| E6 | `constants.py` | 53-79 | Baseline = exactly the 10 reported tools |
| E7 | `pool_settings.json` | 47-105 | `"Researcher": []`, `"Coder": []`, `"Reviewer": []` |
| E8 | `ws_handlers.py` | 783-797 | Correct pattern: resolves with UI dict as instance_override |

## Fix Applied (Option A)

In `agent.py`: feed the live pool UI dict into resolver as instance_override instead of resolving template-only then unioning afterward. Extracts per-agent entry only — preserves Layer 4 for unknown agents. See `review_researcher_tool_policy_fix.md` and `.agent_lessons/researcher-tool-policy-mismatch.md`.

*Report saved: investigation_researcher_tool_policy.md*