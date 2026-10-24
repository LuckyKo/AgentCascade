---
name: additive-flag-param-caller-migration
description: Safely add a boolean range/filter OR index (which-element) parameter to a widely-called method so new behavior is opt-in as intended, while every pre-existing caller and test mock keeps working.
source: auto-generated
version: 1.1.0
triggers:
  - "add parameter to method"
  - "occurrence"
  - "which occurrence"
  - "include_active_only"
  - "opt-in flag"
  - "caller migration"
  - "disambiguate duplicate"
generated_by: coder
generated_from_task: "Add occurrence parameter to edit_file tool (BUG_0055)"
---

## Goal
Safely add a range/filter OR index (which-element) parameter to a widely-called method so the NEW behavior is opt-in-or-opt-out as intended, while every pre-existing caller and test mock keeps working.

## Procedure
### Step 1 — Decide the default BEFORE touching code; write it in the docstring
The single most error-prone decision is which side the default lands on. If you flip a default (e.g. from "registry-only" to "include disabled"), the method's *contract* changes and every caller that silently relied on the old behavior must be migrated — not just the one you're editing. State the chosen semantics in the docstring and list which callers are expected to pass the flag.

### Step 2 — Grep for EVERY hidden caller, not just the obvious one
`grep 'method_name\('` across the source tree AND `tests/`. Callers that relied on the old default and now need it preserved must pass the explicit flag. Missing one = a silent behavior change in production or a `TypeError`.

### Step 3 — Update mock test doubles' signatures too
Test fakes that shadow the method (e.g. `MockClass.method(self)`) will raise `TypeError: unexpected keyword argument` once production calls with the new kwarg. Add the param (usually a no-op default in the mock). This failure is easy to miss because the mock file isn't the one you're editing.

### Step 4 — Keep the new branch strictly additive and lock-safe
- Default path = the old code, byte-identical (guard with a snapshot test if feasible).
- New work goes OUTSIDE any lock; only take the in-memory snapshot under the lock.
- Dedup by `lower()` on both sides when merging two name sources, matching codebase casing conventions.

### Step 5 — Thread the flag through the tool AND the LLM-facing schema
For AgentCascade: the LLM sees `prompts/dna.py::TOOL_METADATA`, NOT the tool class `parameters`. A param that exists in the tool class but not in `TOOL_METADATA` is dead to the LLM. Update both, and add a schema test asserting the new param is present.

## Tips
- **Locate the ACTUAL mutation site, not just the match/position-tracking variable.** For a "which element" index param (e.g. `occurrence`), the variable that computes WHERE the match is is often feedback/logging-only (its comment may even say "for the feedback message"); the real change (e.g. `content.replace(old, new, 1)`) is a SEPARATE site further down the function. Updating only the position variable silently no-ops — the FIRST match is still changed. Grep for the assignment / `replace` / write that mutates the content and update it too, guarded by the new param.
- **Nth-occurrence selection must match `str.count()` semantics.** Advance the search by `len(pattern)` per step (non-overlapping), not by 1, so the reachable count equals `str.count()` and your range check is consistent.
- **Out-of-range index → a DISTINCT error.** Return e.g. "occurrence=5 out of range — pattern found 3 times (valid: 1-3)", NOT the generic "found N times" ambiguity message. Use "time(s)" (not "times") if the count can be 1.
- **Coerce the index defensively.** LLMs may send the string "2"; wrap in `int()` and return a clear error on non-numeric input.
- If you flip a default, search for tests that assert the OLD default behavior — they'll now be inverted and need rewriting (not just deleted).
- Prefer a single explicit index/range param over an ambiguous boolean; name it for the mode it selects, not the negation of a state.
- A "defense-in-depth" manual filter that duplicates the method's own exclusion is fine to keep (it's a no-op in production) but comment WHY it stays so a future reader doesn't delete or duplicate it.
- Run the full affected test files serially from the host shell (`-o addopts=""`), not code_interpreter.
