---
name: zero-behavior-change-dedup-refactor
description: Procedure for deduplicating repeated validation/clamp logic into one source-of-truth table + helper while guaranteeing zero behavior change.
source: auto-generated
version: "1.0.0"
triggers:
  - "deduplicate clamp logic"
  - "centralize settings"
  - "zero behavior change refactor"
  - "single source of truth table"
  - "remove duplicated min(max"
generated_by: coder
generated_from_task: "Centralize 10 skill-scoring min(max(lo,val),hi) clamps duplicated across 4 Python files into one settings table + helper, zero behavior change, all tests stay green."
---

## Goal
Turn N copies of the same validation/clamp/default logic scattered across several call sites into ONE source-of-truth table + helper — with a provable guarantee that every input still produces the exact same output (pure deduplication, no semantic shift).

## Procedure

### Step 1 — Enumerate every call site and its EXACT semantics
Grep for the repeated pattern (e.g. `min(max(` or the setting-key names) across the whole repo. For EACH call site record not just the range but the *edge-case behavior*, because these are where dedup refactors silently break:
- missing/None input → default? skip (leave unset)? raise?
- unparseable input → default? skip?
- int vs float typing preserved?
- any `<`/`<=` invariant (e.g. "must stay < 1")

### Step 2 — Build the single source of truth
One dict `{key: {'default', 'lo', 'hi', 'type'}}` in the natural home module (usually where the defaults/constants already live). Add a helper that does the clamp + cast + fallback. Keep the per-setting docstrings/comments (especially any "why this range" notes) next to the table so they don't get lost.

### Step 3 — Refactor each call site to read from the table
Replace hardcoded numbers with table lookups. Preserve each site's *edge-case semantics* — if a site skips unparseable values but the helper defaults them, cast explicitly at that site (e.g. `helper(key, spec['type'](raw))` inside try/except) rather than "simplifying" it to call the helper directly.

### Step 4 — Prove zero behavior change
- Run the existing test suite (it's your behavior-preservation proof).
- Add an exhaustive old-vs-new comparison: for every key × a grid of inputs (out-of-range, negative, boundary, None, unparseable string), assert `old(key, v) == new(key, v)` AND types match. Zero mismatches = safe.
- Directly exercise the tricky edge cases (unparseable persisted value → skipped; missing keys → unset).

### Step 5 — Independent review before delivery
Delegate to a reviewer with the exact old semantics spelled out. Expect them to catch the subtle "helper swallows parse errors but this call site must skip" class of bug. Fix, re-test, get PASS.

## Tips
- The #1 hidden risk is that different call sites have DIFFERENT edge-case semantics (default vs skip) even though the happy-path clamp looks identical. Never assume a shared helper matches every site without checking the unparseable/None path per site.
- Prefer a generated loop over hand-writing N near-identical functions, but keep each registered handler discoverable (set `__name__`).
- If a call site needs to *skip* on bad input while the helper *defaults*, that's fine — just cast at the site and document WHY in a comment so nobody "simplifies" it back into a regression.
- Keep key names stable even if they're slightly odd (e.g. a `rflood` key mapping to an `r_floor` concept) — rename is a behavior/API change, out of scope for a dedup.
- Save a project memory noting the table location + the per-site semantic differences so future edits don't reintroduce the divergence.
