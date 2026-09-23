---
name: stub-mock-attribute-parity-after-refactor
description: "When a refactor adds/renames a method or removes a private-field read on a class, update every SimpleNamespace/MagicMock test stub that shadows it — otherwise tests fail with AttributeError, and removing a 'defense-in-depth' filter can silently change behavior for fixtures that bypass the real entry point."
source: auto-generated
version: "1.0.0"
triggers:
  - "AttributeError: object has no attribute"
  - "SimpleNamespace stub"
  - "mock manager missing method"
  - "added public method broke tests"
  - "removed private field read"
  - "stub parity after refactor"
generated_by: coder
generated_from_task: "POLISH fix on skill re-enable/scan-all: added SkillManager.is_skill_disabled and removed a manual _disabled_names filter — broke SimpleNamespace stubs (AttributeError) and exposed a fixture that bypassed discover()."
---

## Goal
Keep hand-rolled test stubs (`SimpleNamespace`, `MagicMock`) in lockstep with production classes across a refactor, so the suite doesn't fail with `AttributeError` or silently pass on unrealistic fixtures.

## Procedure
### Step 1 — Grep the whole test tree for every stub that shadows the class
Before you finish a refactor that (a) adds/renames a method, or (b) replaces a private-field read (`obj._x`) with a public call (`obj.is_x()`), grep `tests/` for the class name AND for stub constructors:
```
grep -rn "SimpleNamespace(" tests/ | grep -i "<ClassName>"
grep -rn "_disabled_names\|_metrics\b" tests/   # private fields now accessed via a method
```
A stub built as `SimpleNamespace(load_full_instructions=..., _ensure_discovered=...)` is a **closed** surface — it exposes ONLY the attributes you listed. The moment production calls a new attribute, every such stub breaks with `AttributeError: 'types.SimpleNamespace' object has no attribute 'is_skill_disabled'`. Add the new attribute to each stub (a lambda returning the fixture-appropriate value), with a comment noting the stub must mirror the real surface.

### Step 2 — Distinguish "stub is missing an attribute" from "production regressed"
An `AttributeError` on a `SimpleNamespace` is almost always a **test-fixture** gap, not a production bug — the production object (a real class) has the method. Confirm by checking the failing frame: if it's `load_skill.py:177 → skill_manager.is_skill_disabled(name)` and `skill_manager` is a stub, fix the stub, not the code. Don't "fix" production to tolerate a missing attribute (e.g. re-adding `getattr(..., set())`) — that reintroduces the private-access coupling you just removed.

### Step 3 — Removing a "defense-in-depth" filter can change fixture behavior
A manual filter like `if active_only: skills = [s for s in skills if s['name'] not in disabled]` looks redundant (the underlying method already excludes those), but it is **only** redundant on the real production path. A test fixture that bypasses the real entry point (e.g. injects a disabled skill directly into `_skills_registry` instead of calling `discover()`) relies on that filter to hide it. When you remove the filter, such a test will now see the skill and fail.
- If the fixture is unrealistic (doesn't reflect production), **fix the fixture** or reword the assertion to document that it's a fixture artifact — do NOT keep the dead filter just to make an unrealistic test pass.
- Point the docstring at the REAL production-path test that genuinely covers the behavior (the one that drives `discover()`).

### Step 4 — Verify both directions after a semantics change
After removing redundancy or flipping a default, run the full affected files and confirm: (a) no new `AttributeError` from stubs, and (b) the production-path test still asserts the real behavior. A test that "passes" only because a fixture masks the bug is worse than a failing one — it hides the regression.

## Tips
- Prefer public methods over private-field reads in tools; but when you make that swap, treat every stub as a contract surface that must be updated in the same commit (see [[additive-flag-param-caller-migration]] for the caller-migration side of the same class of refactor).
- `MagicMock` auto-generates missing attributes (returns a Mock), so it hides this gap — but any test that *asserts on* the mock's return value will then get a truthy Mock and fail in a confusing way. Explicit `SimpleNamespace` stubs fail loudly, which is better; just remember they're closed surfaces.
- When rewording an assertion to "fixture artifact," keep it honest: name the specific production-path test that covers the real behavior so a future reader knows where the genuine guard lives.
- Run the full affected suite serially from the host shell (`-o addopts=""`), not code_interpreter — see [[pytest-ini-addopts-xdist-serial-run]].
