# BUG_0356 — Skill-hint ledger counts auto-loads into `total_loads`, defeating the "shown-often-but-not-loaded" demotion

**Status:** OPEN
**Filed:** 2026-11-19
**Found in:** `todo.md:129` (methodological worry), verified against live code 2026-11-19
**Severity:** medium (degraded self-tuning — the precision-demotion layer is defeated for exactly the generic auto-loaded skills it targets; no crash / data loss; workaround = manual `SKILL_PRECISION_WEIGHT` tuning)
**Component:** `agent_cascade/skills/manager.py` (`_increment_load_count` :1242, `load_full_instructions(count_load=True)` :1933, call sites :1978/:1997/:2009), `agent_cascade/memory_hint/manager.py` (precision demote :1470-1495, Thompson bandit :1431-1467, superset-clip note :200-203), `agent_cascade/settings.py` (`SKILL_PRECISION_WEIGHT` :1035, `SKILL_BANDIT_WEIGHT` :1056)

## Symptom
A **generic** skill (one that loosely matches many queries) gets **auto-loaded** by the harness (init AUTO / advisor / basic keyword auto-match) about as often as it is **hinted**. Because the per-skill `total_loads` ledger counts those auto-loads, the skill's `loads ≈ hints`, so the precision demotion

```
w = max(FLOOR, min(1, (loads+K)/(hints+K)))      # memory_hint/manager.py:1490-1494 (default-active)
```

stays ≈ 1 → the skill is **never demoted** → it keeps getting hinted. This is the feedback loop `todo.md:129` warns about: a generic skill "games" the auto-loading, gets retained/promoted more, and keeps bothering us even when it is irrelevant.

## Evidence
- `total_loads` is a **superset** of hint-driven loads — documented in three places:
  - `memory_hint/manager.py:200-203`: *"total_loads is a SUPERSET of hint-driven loads (it also counts init AUTO + advisor + explicit load_skill loads)"*.
  - `api_server.py:345-347` and `:2396-2398`: *"loads is the durable total_loads ledger, a SUPERSET of runtime load_skill (counts init-time AUTO/advisor/explicit loads too)"*.
- **Writer side** — auto-load paths call `load_full_instructions(..., count_load=True)` (the default), so auto-loads DO increment the ledger:
  - advisor path `engine/system_agent.py:290`
  - `inject_skill` `pool/core.py:813`
  - both funnel into `_increment_load_count` (`skills/manager.py:1242`, invoked at :1978 / :1997 / :2009 when `count_load` is True).
- The demotion is the **default-active** re-rank: `SKILL_PRECISION_WEIGHT=1` (`settings.py:1035`), `SKILL_PRECISION_K=1.0` (:1039), `SKILL_PRECISION_FLOOR=0.35` (:1043).
- The Thompson bandit (default **OFF**, `SKILL_BANDIT_WEIGHT=0.0`, `settings.py:1056`) makes it **worse**: its clip `s=min(loads,hints)` (`manager.py:1435`) gives a heavily auto-loaded skill a *perfect* success rate → high θ → **promoted**, not demoted.

## Root cause
The ledger has a **single** `total_loads` counter with no distinction between *auto-loads* (harness-injected) and *agent-selected loads* (the agent deliberately chose the skill for relevance). Both the precision demote and the bandit read `total_loads`, so auto-loads inflate the "loaded" side and defeat the "shown-often-but-not-loaded" penalty. The `count_load` flag (`load_full_instructions`, `manager.py:1933`) only distinguishes *real load* from *loadability-check* — it does **not** distinguish auto from agent-selected, so the fix is not a simple flag flip.

## Fix (chosen direction — not implemented)
**Additive `agent_selected_loads` field (Option A), NOT a `count_load` flip (Option D).**

Call-graph finding that decides it: the agent-explicit and auto paths **share** the load functions —
- `inject_skill` (`pool/core.py:772`) is "the single source of truth shared by the `load_skill` tool and the memory-hint auto-loader" (BUG_0263): called by `tools/custom/load_skill.py:132` (agent-explicit) **and** `memory_hint/manager.py:1103` (auto).
- `resolve_load_skill` / `_load_skill_bodies` (`skills/manager.py:2138-2159`) is shared by the caller-explicit `call_agent(load_skill=[...])` list **and** the basic keyword auto-match.

So Option D ("flip `count_load` at the auto sites") would have to thread a flag through *both* shared functions — which already carry delicate `count_load=False` invariants (loadability checks at `:2064`/`:2089`). Larger blast radius than it looks. The additive field is lower-risk and matches the codebase's established pattern (BUG_0312 evolution ledger: "additive; no schema bump — readers use `.get(field, default)`").

Plan:
1. **Add `agent_selected_loads`** to `_default_metrics_entry` (`skills/manager.py:152`) — additive, default 0, `.get()`-tolerant on read. **No schema bump / no migration** (mirrors BUG_0312).
2. **Increment it at the single unambiguous agent-explicit site:** the `load_skill` tool (`tools/custom/load_skill.py:132`), on a successful `inject_skill`. Do **NOT** count the advisor (`system_agent.py:290`), the memory-hint auto-loader (`manager.py:1103`), or the basic auto-match — those stay on `total_loads`.
   - *Deferred:* the caller-explicit `call_agent(load_skill=[...])` path is shared with auto-match in `_load_skill_bodies`, so it is NOT counted for now (the `load_skill` tool is the pure signal). Revisit if needed.
3. **Switch the readers:** the precision demote (`memory_hint/manager.py:1490-1494`) and the Thompson bandit (`:1431-1467`) read `agent_selected_loads` in place of `total_loads`; the bandit's `s=min(loads,hints)` clip becomes unnecessary.
4. **Keep `total_loads`** for the API `action_rate` display (`api_server.py:345`, `:2396`) — it remains the legitimate superset.

Regression test: a skill with high `hints` + high auto-loads + **zero** `agent_selected_loads` must be demoted (w→FLOOR / low θ); a control skill the agent deliberately `load_skill`-ed is NOT demoted.

## Verification
(To be filled on fix.) Targeted unit test on the ledger + a re-rank test proving a high-auto-load / zero-agent-selected skill is demoted; record before/after `w` (and θ, if the bandit is enabled).

## Related
- `todo.md:129` — the originating methodological worry.
- [[bug0189-skill-hint-precision-ledger-wiring]] — BUG_0189 #5, the precision demotion this defeats.
- BUG_0316 / BUG_0317 — the Thompson bandit (same superset contamination, worse via the clip).
- BUG_0226 — `skill_gate.py` stopword noise filter (a separate, coarser precision layer).
