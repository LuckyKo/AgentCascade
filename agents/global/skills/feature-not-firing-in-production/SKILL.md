---
name: feature-not-firing-in-production
description: Diagnose why a production feature/trigger never fires even though its unit tests pass — locate the real gate that decides whether the code path is reached, and expose test mocks that hide it.
source: auto-generated
version: "1.0.0"
triggers:
  - "feature not firing"
  - "trigger not working in production"
  - "why didn't it trigger"
  - "extended run not launched"
  - "tests pass but doesn't work live"
generated_by: researcher
generated_from_task: "Investigate why the AgentCascade auto-skill extended-turns trigger did not fire in a real session despite AUTO_SKILL_MIN_TURNS being lowered to 20."
---

## Goal
Find the REAL reason a production feature/trigger never fires when its unit tests pass — by tracing the actual gate that decides whether the code path is reached, and exposing the test mocks (or state resets) that hide the real behavior.

## Procedure

### Step 1 — Read the PRODUCTION call site, not the tests
Locate where the feature is actually invoked in the live loop (grep for the trigger function name + `if` guard). Note the EXACT condition(s) that must be true for it to run and the exact point it's evaluated (e.g. only at natural end of a loop, only once per run). Do NOT start from the test suite — tests are often written against mocks of the very gate you need.

### Step 2 — Enumerate every gate in order
For each `if ... return False / continue / break` between the loop and the trigger, list it: (a) the predicate, (b) what value/state feeds it, (c) where that value is SET and RESET. The bug is almost always one of these inputs, not the trigger function's own logic.

### Step 3 — Check state lifetime vs. the gate's assumption
The classic trap: a counter/flag the gate compares against is scoped to something SHORTER than the user assumes. E.g. `instance._current_turn` reset to 0 on every instance recycle (`lifecycle_manager`) means "the session ran 200 turns" does NOT mean `_current_turn > 20`. Grep for every assignment/reset of that state variable and map its lifetime (per-run vs per-instance-lifetime vs global). If the gate needs a cumulative value but only sees a per-run one, it can never fire in long-but-multi-run sessions.

### Step 4 — Check strictness + evaluation point
Off-by-one lives here: `>` vs `>=` (min=20 needs ≥21), and "trigger evaluated ONLY at natural completion" means a still-running or short run never gets the chance. Confirm both against the code, not the docs.

### Step 5 — Disprove the config/stale-import red herring fast
A lowered setting that "didn't take effect" is usually NOT import-time staleness if the process was started after the edit. Verify the process actually saw the new value (env var? fresh import?) before spending time on the frozen-constant theory. The user often already knows the effective value — ask/confirm, don't speculate.

### Step 6 — Confirm with a real-loop reproduction
Write ONE test that drives the REAL production loop (not a mocked gate) with a scripted input that SHOULD satisfy every gate, and assert the trigger fires. If it doesn't, the failing assertion points at the exact broken gate. Use the project's existing real-loop harness if one exists (grep for tests that drive `engine.run()` / the main generator).

## Tips
- "Tests pass but it never works live" almost always means the tests mock the deciding gate (e.g. `_post_turn_checks` stubbed to return the completion value) so the production decision logic is never exercised. Find and name that mock.
- Distinguish three non-obvious causes: (1) state resets before the gate sees it, (2) strict `>` + trigger-only-at-end, (3) config not actually in effect. Rank by evidence; don't stack all three as "the" answer.
- When a user says "it's not a mystery," stop theorizing and go straight to the state-lifetime check (Step 3) — that's usually the simple truth.
- Do NOT modify code while investigating unless asked; deliver the ranked root cause with file:line evidence first.
