---
name: additive-flag-param-caller-migration
description: "Add a boolean range/filter param to a shared method that many callers depend on, without breaking them. Covers the default-includes/opt-out pattern, migrating every hidden caller, and updating mock test doubles."
source: auto-generated
version: "1.0.0"
triggers:
  - "add parameter to shared method"
  - "include_disabled"
  - "filter flag param"
  - "default includes disabled"
  - "migrate callers new kwarg"
  - "mock test double signature"
generated_by: coder
generated_from_task: "scan_skills include-disabled + load_skill re-enable (todo 156): get_all_metadata gained include_active_only; default flipped to include disabled; hidden callers + advisor mocks had to migrate."
---

## Goal
Safely add a boolean range/filter parameter to a widely-called method so the NEW behavior is opt-in-or-opt-out as intended, while every pre-existing caller and test mock keeps working.

## Procedure
### Step 1 — Decide the default BEFORE touching code; write it in the docstring
The single most error-prone decision is which side the default lands on. If you flip a default (e.g. from "registry-only" to "include disabled"), the method's *contract* changes and every caller that silently relied on the old behavior must be migrated — not just the one you're editing. State the chosen semantics in the docstring and list which callers are expected to pass the flag.

### Step 2 — Grep for EVERY hidden caller, not just the obvious one
`grep 'get_all_metadata\('` across `agent_cascade/` AND `tests/`. Callers that relied on the old default and now need it preserved must pass the explicit flag (e.g. `include_active_only=True`). In this task: `_rebuild_index`, `advisor.py`, and `propose_skill.py` all had to be migrated, plus two test mock doubles. Missing one = a silent behavior change in production or a `TypeError`.

### Step 3 — Update mock test doubles' signatures too
Test fakes that shadow the method (e.g. `MockSkillManager.get_all_metadata(self)`) will raise `TypeError: unexpected keyword argument` once production calls with the new kwarg. Add the param (usually a no-op default in the mock). This failure is easy to miss because the mock file isn't the one you're editing.

### Step 4 — Keep the new branch strictly additive and lock-safe
- Default path = the old loop, byte-identical (guard with a snapshot test if feasible).
- New work (e.g. a disk walk to re-surface disabled skills) goes OUTSIDE any registry lock; only take the in-memory snapshot under the lock.
- Dedup by `lower()` on both sides when merging two name sources (registry vs disk), matching codebase casing conventions.

### Step 5 — Thread the flag through the tool AND the LLM-facing schema
For AgentCascade: the LLM sees `prompts/dna.py::TOOL_METADATA`, NOT the tool class `parameters`. A param that exists in the tool class but not in `TOOL_METADATA` is dead to the LLM (see [[agentcascade-tool-feature-dig-verification]]). Update both, and add a schema test asserting the new param is present.

## Tips
- If you flip a default, search for tests that assert the OLD default behavior — they'll now be inverted and need rewriting (not just deleted). In this task `TestSkillInvalidationScanMarker` had to flip from "default hides inactive" to "default shows inactive".
- Prefer a single explicit range param (`active`) over an ambiguous boolean like `all`; name it for the mode it selects, not the negation of a state.
- A "defense-in-depth" manual filter that duplicates the method's own exclusion is fine to keep (it's a no-op in production) but comment WHY it stays so a future reader doesn't delete or duplicate it.
- Run the full affected test files serially from the host shell (`-o addopts=""`), not code_interpreter — see [[pytest-ini-addopts-xdist-serial-run]].
