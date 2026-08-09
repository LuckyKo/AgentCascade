# Investigation: Researcher tool-policy mismatch (todo.md line 89)

**Date:** 2026-08-10
**Investigator:** investigator_tools_89 (researcher class — the affected agent type)
**Scope:** `N:\work\WD\AgentCascade`
**Status:** Root cause identified, verified empirically, fix proposed (not yet applied)

---

## Executive Summary

`system_info` reports 10 tools as "Disabled" for the researcher agent
(`code_interpreter, copy_file, delete_file, edit_file, propose_skill, re_indent,
shell_cmd, web_extractor, web_search, write_file`), but these tools are **not actually
enforced as disabled** — `code_interpreter` and `write_file` both work at runtime.

The root cause is a **divergence between two resolution paths** of
`resolve_disabled_tools_for_agent()`:

1. **Reporting path (system_info)** resolves disabled tools **without an instance
   override** (`instance_override=None`), so the Layer-4 "default safe baseline"
   (`DEFAULT_NEW_AGENT_DISABLED_TOOLS`) fires and the tools are reported disabled.
2. **Execution path (engine)` resolves disabled tools **with the instance override**
   (populated by `propagate_settings()` for every spawned child), which suppresses
   Layer 4 — so the tools are actually available.

This is a **reporting bug**, not a genuine security regression: enforcement is
*less* restrictive than what system_info claims. However, it has a real secondary
impact: the same template-level resolution feeds the auto-injected system-prompt
resources block and UI tool listings, so researcher/coder/reviewer/writer agents get
told they lack write/file/web tools they actually possess, which degrades agent behavior.

---

## Key Findings

### Finding 1 — The reported list is exactly `DEFAULT_NEW_AGENT_DISABLED_TOOLS`

| Tool | In reported list | In `DEFAULT_NEW_AGENT_DISABLED_TOOLS` (constants.py:58-79) |
|---|---|---|
| code_interpreter | ✅ | ✅ |
| copy_file | ✅ | ✅ |
| delete_file | ✅ | ✅ |
| edit_file | ✅ | ✅ |
| propose_skill | ✅ | ✅ |
| re_indent | ✅ | ✅ |
| shell_cmd | ✅ | ✅ |
| web_extractor | ✅ | ✅ |
| web_search | ✅ | ✅ |
| write_file | ✅ | ✅ |

The 10 reported tools = `DEFAULT_NEW_AGENT_DISABLED_TOOLS` minus the MCP sentinel
`__all_mcp_tools__` (which never appears in a template's `function_map`).
Verified by direct resolver execution (see Evidence E5).

### Finding 2 — Layer 4 fires only when no explicit config exists

`resolve_disabled_tools_for_agent()` (`agent_cascade/utils/disabled_tools.py:135-143`):

```python
if not has_explicit_config and atype_lower not in ('orchestrator', 'security', 'compressor'):
    disabled |= DEFAULT_NEW_AGENT_DISABLED_TOOLS
```

`has_explicit_config` becomes True whenever Layers 1 or 2 contain a `disabled_tools`
key (lines 118-126). For a spawned researcher instance, `propagate_settings()`
(`agent_cascade/lifecycle_manager.py:602-771`) **always** writes
`instance._generate_cfg_override` with a `disabled_tools` list (line 770), so the
execution path never sees the baseline.

### Finding 3 — The two resolution paths disagree (the actual bug)

- **`Agent._get_active_functions(pool=...)`** (`agent_cascade/agent.py:199-211`) is
  called by `system_info.py:123` and by `Agent`-based tool flows. It passes
  `instance_override=None` (line 200). ⇒ Layer 4 applies at template level ⇒ tools
  reported disabled.
- **`_get_active_functions_from_template(template, instance, pool)`**
  (`agent_cascade/execution_engine.py:135-213`) is the real runtime path (used at
  `execution_engine.py:3516`). It passes
  `instance._generate_cfg_override` (lines 163-164). ⇒ Layer 4 suppressed ⇒ tools enabled.

### Finding 4 — Union semantics make Layer 4 impossible to override at template level

Both paths then union in the live pool UI config
(`pool.get_ui_disabled_tools_for_agent(...)`, agent.py:207-211 / execution_engine.py:194-197).
Because the resolver uses **union** of layers (layers accumulate), the Layer-4 baseline
that fired in the template-level query cannot be *removed* by the live UI dict
(`{"Researcher": []}`) — union can only add, never subtract. So even with an explicit
UI grant (`"Researcher": []`, saved in `config/pool_settings.json:84`), the template-level
report keeps showing the baseline.

### Finding 5 — The saved config already grants researcher full tools

`config/pool_settings.json:47-105` contains per-agent `disabled_tools`:
- `"Researcher": []` (line 84) — explicit empty grant.
- `"Coder": []`, `"Reviewer": []` — also empty grants.
- `"Writer": ["shell_cmd"]`, `"Generalist": ["call_agent","dismiss_agent"]`, etc.

So the *intended* policy per saved config is that researcher/coder/reviewer have **all
tools enabled**, and their system_info reports are plain wrong.

### Finding 6 — "researcher" IS part of the canonical agent-type mapping

`agent_cascade/api_router.py:78-87` maps `"researcher": "Researcher"`. The soul file
exists (`agents/researcher_soul.md`), loads via `_discover_agents()`
(`agent_pool.py:2827-2835`), and `create_agent_from_soul()` sets
`agent.agent_type = 'Researcher'` (soul_loader.py:346). There is **no missing map
entry** — the suspicion in the todo that "agent-class to tool-policy map missing
researcher" is **not** the cause. The cause is Layer-4 baseline semantics at the
template/reporting level.

---

## Root Cause (precise)

**Primary:** `Agent._get_active_functions()` (and `_get_disabled_tool_names()`) at
`agent_cascade/agent.py:199-227` resolve with `instance_override=None`, making
`has_explicit_config=False`. Any non-core agent (researcher, coder, reviewer, writer,
generalist, and any soul-discovered class) then gets `DEFAULT_NEW_AGENT_DISABLED_TOOLS`
applied at the *reporting* level, while the *enforcement* level (engine, instance-aware)
never applies it because spawned instances always carry an override.

**Contributing:** `has_explicit_config` is set based on *presence of the key*, not on
whether the per-agent lookup actually yielded any tools. A dict `{"Researcher": []}`
correctly yields an empty per-agent set — and yet the *template-level* call never
passes that dict at all, so it can't suppress Layer 4.

**Why the todo author believed write_file was disabled:** system_info says so, and the
auto-injected resources block (`execution_engine.py:452-480` → `_build_resources_block`
uses `_get_active_functions_from_template(template, instance, pool)`) — when instance
is provided it uses the instance override, but `system_info` passes the template only,
producing the false report. Writing the todo "via CI because write_file/edit_file show
as disabled" was the natural consequence of trusting the erroneous system_info output.
(write_file empirically works — see Finding 7.)

**Finding 7 — Empirical proof from the live session (2026-08-10):**
- `system_info` in this same session (researcher class) reported exactly the 10-tool
  disabled list.
- `write_file` to `N:\work\WD\AgentWorkspace\temp\researcher_write_file_probe.txt`
  **succeeded** (file created).
- `code_interpreter` executed resolver simulations **successfully**.
- Direct resolver tests reproduced the split (Evidence E5/E6).

---

## Supporting Evidence

| # | File | Lines | What it proves |
|---|---|---|---|
| E1 | `agent_cascade/utils/disabled_tools.py` | 69-145 | Resolver layers; Layer 4 baseline gate at 135-143; `has_explicit_config` set on key presence at 119-126 |
| E2 | `agent_cascade/agent.py` | 199-227 | Reporting path passes `instance_override=None` (200, 223); unions live UI after resolve (207-211) |
| E3 | `agent_cascade/execution_engine.py` | 135-213, 3516 | Real runtime path passes instance override (163-164); `_call_llm_with_injection` uses it (3516) |
| E4 | `agent_cascade/lifecycle_manager.py` | 602-771 | `propagate_settings` always writes `_generate_cfg_override` with `disabled_tools` (753-771) |
| E5 | `tools/custom/system_info.py` | 118-136 | system_info computes disabled = function_map − active from template-level `_get_active_functions`; prints at 136 |
| E6 | `agent_cascade/constants.py` | 53-79 | `DEFAULT_NEW_AGENT_DISABLED_TOOLS` = exactly the 10 reported tools (+MCP sentinel) |
| E7 | `config/pool_settings.json` | 47-105 | Saved UI grants: `"Researcher": []` (84), `"Coder": []`, `"Reviewer": []` |
| E8 | `agent_cascade/api_router.py` | 78-87 | `researcher → Researcher` canonical mapping EXISTS |
| E9 | `agent_cascade/soul_loader.py` | 294-353 | `agent.agent_type = role_name.title()` = 'Researcher' |
| E10 | `agent_cascade/agent_pool.py` | 2827-2835 | Souls auto-discovered; template key = file stem ('researcher') |
| E11 | git `3a73fbf` (2026-08-05) | — | Introduced Layer 4 + `DEFAULT_NEW_AGENT_DISABLED_TOOLS`; intent: "newly loaded agents start READ-ONLY until user grants more access" |
| E12 | `agent_cascade/ws_handlers.py` | 783-797 | Export path resolves with `instance_override={'disabled_tools': ui_disabled_tools}` — the *correct* pattern |

---

## Empirical reproductions (code_interpreter, 2026-08-10 01:13)

### E5 — Template-level vs instance-level resolution for Researcher

```python
# template-level (system_info path): instance_override=None
resolve(None, {}, 'Researcher', 'Researcher')
# → {'__all_mcp_tools__', 'code_interpreter', 'copy_file', 'delete_file', 'edit_file',
#    'propose_skill', 're_indent', 'shell_cmd', 'web_extractor', 'web_search', 'write_file'}

# instance-level (execution path): override present
resolve({'disabled_tools': ['forget_last']}, {}, 'Researcher', 'Researcher')
# → {'forget_last'}   (Layer 4 suppressed)
```

### E6 — Live saved config behavior

```python
dt = json.load(open('config/pool_settings.json'))['disabled_tools']  # 'Researcher': []
# Live UI dict fed as override → Layer 4 suppressed:
resolve({'disabled_tools': dt}, {}, 'Researcher', 'Researcher')  # → set()
# Template-only (no override, no dict) → baseline still fires regardless of saved file:
resolve(None, {}, 'Researcher', 'Researcher')  # → 10-tool baseline (+ sentinel)
```

### E7 — Fix validation

```python
# With proposed pattern (feed live UI dict as the instance override):
resolve({'disabled_tools': dt}, {}, 'Researcher', 'Researcher')  # → set()
resolve({'disabled_tools': dt}, {}, 'Writer', 'Writer')          # → {'shell_cmd'}
resolve({'disabled_tools': dt}, {}, 'Generalist', 'Generalist')  # → {'call_agent','dismiss_agent'}
resolve(None, {}, 'MysteryAgent', 'MysteryAgent')                # → 10-tool baseline (security intact)
# Security/Compressor class defaults still enforced regardless:
resolve({'disabled_tools': dt}, {}, 'Security', 'Security')      # → UI ∪ DEFAULT_SECURITY_DISABLED_TOOLS
```

---

## Confidence Level

**High Confidence (verified).** Root cause reproduced via direct resolver execution,
confirmed by live-session empirical test (write_file succeeded on researcher class),
and consistent with git history (Layer 4 introduced 2026-08-05).

---

## Open Questions / Secondary Issues

1. **No-LLM or non-spawned reads**: Some flows (e.g. `get_agent_info()` in
   `agent_pool.py:1139-1159`, UI agent listing in `api_server.py:883-889`) rely on
   `Agent._get_active_functions(pool=...)` template-level output — they will keep
   showing the false baseline until agent.py is fixed.
2. **`has_explicit_config` granularity**: An agent class *not* present in the UI dict
   still cannot suppress Layer 4 (by design — "unknown agents start read-only"). This
   is correct security posture; the bug is only that *known configured* classes like
   Researcher with `[]` are reported wrong at template level.
3. **Non-authoring flows**: `web_search`/`shell_cmd` similarly reported disabled but
   actually available for researcher. Confirm desired policy: researcher should have
   web_search per its soul's `source_priority` tier 4; currently the false report may
   make the agent avoid them.

---

## Recommended Fix

### Option A (recommended, minimal, correct) — `agent_cascade/agent.py`

Make `Agent._get_active_functions()` and `Agent._get_disabled_tool_names()` feed the
live pool UI dict **into** the resolver as the instance override, rather than resolving
template-only and then unioning live results afterward:

```python
def _get_active_functions(self, pool=None) -> list:
    from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent
    live = {}
    if pool is not None and hasattr(pool, 'get_ui_disabled_tools_for_agent'):
        live = dict(getattr(pool, '_ui_disabled_tools', {}) or {})
    disabled = resolve_disabled_tools_for_agent(
        instance_override={'disabled_tools': live} if live else None,
        template_cfg=(getattr(self.llm, 'generate_cfg', None) or {}),
        agent_name=self.name,
        agent_type=getattr(self, 'agent_type', '') or '',
    )
    return [func.function for name, func in self.function_map.items() if name not in disabled]
```

Same change in `_get_disabled_tool_names()` (agent.py:215-227). This:
- Lets `{"Researcher": []}` set `has_explicit_config=True` → Layer 4 suppressed for
  explicitly-granted classes → report matches reality (all tools enabled).
- Keeps Layer 4 as genuine fallback for classes with NO UI entry (unknown agents stay
  read-only).
- Preserves Security/Compressor defense-in-depth (Layer 3 always applies).
- Matches the pattern already used in `ws_handlers.py:789-794` and the execution engine.

### Option B (alternative) — fix the reporting layer only

Change `tools/custom/system_info.py` (line 123) to resolve through
`_get_active_functions_from_template(template, instance, pool=...)` with a synthetic
instance carrying the live UI override. More surgical but leaves `_get_active_functions`
inconsistent internally with engine behavior, and doesn't fix `get_agent_info`/UI lists.

### Option C (structural) — resolver API

Add a parameter to `resolve_disabled_tools_for_agent()` like
`explicit_agent_names: Optional[Set[str]] = None` that treats a named agent as
"explicitly configured" even when the per-agent lookup yields an empty set, decoupling
"has config" from "has non-empty list". Most invasive; not needed for this bug.

### Tests to add

1. `test_researcher_explicit_empty_grant`: saved dict `{"Researcher": []}` →
   resolver returns `set()` for agent_name='Researcher' (no Layer 4).
2. `test_template_only_still_baseline_for_unlisted_class`: unknown class
   ('MysteryAgent') → still returns `DEFAULT_NEW_AGENT_DISABLED_TOOLS` (security intact).
3. `test_system_info_consistency`: `_get_active_functions(pool=pool)` output for
   Researcher contains write_file/code_interpreter when live dict has `[]`.

---

## Suggested Next Actions

1. Apply Option A in `agent_cascade/agent.py:199-227`.
2. Add the 3 tests above to `tests/` (e.g. `tests/test_disabled_tools_resolution.py`).
3. Re-run session; verify `system_info` now shows only genuinely-disabled tools for
   researcher (expected: `forget_last` inherited or empty, per saved config).
4. Consider updating todo.md line 89 to `[x]` after the fix is verified by the
   Reviewer agent.
5. (Optional) Audit whether researcher *should* retain `web_search`/`web_extractor`
   per `agent_cascade/utils/disabled_tools.py` Layer 4 intent — currently they are
   enforced-available but reported-disabled; the saved UI grant (`[]`) is the user's
   explicit decision.

---

*Report saved: `investigation_researcher_tool_policy.md`*
*Memory saved: `.agent_lessons/researcher-tool-policy-mismatch.md`*