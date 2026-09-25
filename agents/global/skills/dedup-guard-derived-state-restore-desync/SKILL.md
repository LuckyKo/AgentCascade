---
name: dedup-guard-derived-state-restore-desync
description: Diagnose "duplicate injection / idempotency guard not firing" regressions where a guard keys on a derived per-instance state field (cache) that gets desynced from the actual persisted content across a lifecycle boundary (session restore / external load), because a fresh instance object is built with the field at its default while the content survives in the conversation.
source: auto-generated
version: "1.0.0"
triggers:
  - "duplicate injection"
  - "dedup guard not firing"
  - "idempotency regression"
  - "_loaded_skill_names"
  - "session restore desync"
  - "state field reset on recall"
generated_by: researcher
generated_from_task: "todo.md:148 — duplicate 'Apply the above guidelines' after load_skill; b2b8501 dedup guard reported NOT FIXED"
---

## Goal
Find the real root cause of a "duplicate / double-injection / guard-didn't-fire" regression when the guard keys on a **derived state field** that a lifecycle boundary can reset even though the underlying content persists.

## Procedure

### Step 1 — Treat the guard's INPUT as the suspect, not the guard
The guard logic is usually correct; the defect is its input. Identify the field the guard reads (e.g. `inst._loaded_skill_names`) and treat it as a **derived cache**, not ground truth. The ground truth is the persisted artifact (the conversation / JSONL log).

### Step 2 — Grep for EVERY writer of the tracked field (not just the guard)
`grep -rn "field_name\s*(=|\.clear\(|=\s*\[|=\s*None)" agent_cascade/`. Enumerate all assignment sites and note which run on **fresh-instance construction** vs **live mutation**. In this codebase there were exactly three: a rebuild overwrite (`engine/core.py:3435`), an init/restore seed (`engine/helpers.py:578`), and the live-run recorder (`tools/custom/load_skill.py:224`). No `.clear()`/`= None` in the recall path.

### Step 3 — Map every "rebuild-from-history" chokepoint; check whether it re-derives
For each path that (re)builds an instance's conversation from persisted history, ask: does it re-populate the tracked field from that content?
- **Session restore / external load** (`pool/session_io.py::load_session_from_log`, `api_integration_pkg/runner.py::create_main_agent_instance`, direct API/WS resume) → builds a **fresh AgentInstance** (field = dataclass default `None`) and seeds only init-time/self-aug skills → prior runtime content is **NOT re-derived**. This is the desync.
- **Pure recall/reuse** (`lifecycle_manager.py::find_or_create_instance`) → same object, field retained → **safe**. Distinguish these two; the bug needs the fresh-instance boundary, not recall.

### Step 4 — Confirm single vs multiple injectors of the duplicated artifact
Grep for the literal duplicated string (e.g. the closing line). If exactly one code path produces it, the duplicate is that path being **called twice across the boundary**, not a second injector. A skill body or doc that happens to contain the phrase is a red herring — check role/format (a SYSTEM `## Active Skills` block vs a USER message).

### Step 5 — Git-timeline: latent gap vs recent regression
`git log --oneline -S "<field>" -- agent_cascade/`. If no commit ever made restore re-derive the field, it's a **latent gap in the guard's assumption** (the invariant held for a live run but was never true across restore) — not a regression from a specific recent commit. Do NOT force a "recent commit broke it" narrative.

### Step 6 — Fix: re-establish sync at the chokepoint(s); keep fresh-create byte-identical
Add one shared pure helper that re-derives the field from the persisted content (match the exact marker the injector writes, e.g. USER messages whose content STARTS with `## Loaded Skill: <name>`). Fold it into each rebuild-from-history chokepoint as a **merge** (`prior + existing`), so a brand-new instance (`prior == []`) is byte-identical to today. Fixing the field (not just the guard) also fixes every other consumer of that field (reflection list, memory-hint dedup).

## Tips
- The tell-tale test gap: existing dedup tests pre-populate the field or repeat within one call; **none model "content present in conversation but absent from the field."** Add exactly that as the revert-proof regression test.
- Prefer fixing the shared field over patching the guard — a guard-only band-aid leaves sibling consumers stale. An optional guard-level fallback (consult the persisted content) is legitimate defense-in-depth, not the primary fix.
- Match the injector's marker via a shared constant to avoid format drift; require the message to START with the header and be USER-role so a skill body containing the phrase can't false-match.
- Complements [[verify-reported-bug-still-reproduces]] (does it still reproduce / is the trigger right) — this adds "if it's a state-tracking guard, does the tracked state survive lifecycle boundaries?" — and [[regression-commit-attribution-asof]] (which commit broke it).
