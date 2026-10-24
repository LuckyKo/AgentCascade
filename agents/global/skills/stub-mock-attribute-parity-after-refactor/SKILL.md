---
name: stub-mock-attribute-parity-after-refactor
description: "When a refactor adds/renames a method, removes a private-field read, OR adds a call to an existing pool/engine method, update every SimpleNamespace/MagicMock test stub that shadows it — otherwise tests fail with AttributeError (closed stubs) or silently PASS on a MagicMock no-op (asserting on state the real method would have mutated). Covers both halves."
source: auto-generated
version: 1.0.1
triggers:
  - "AttributeError: object has no attribute"
  - "SimpleNamespace stub"
  - "mock manager missing method"
  - "added public method broke tests"
  - "removed private field read"
  - "stub parity after refactor"
  - "MagicMock no-op false pass"
  - "assert on timestamp state mock did not mutate"
  - "added call to existing pool method test double"
generated_by: coder
generated_from_task: "DMS post-return re-stamp fix (t293-followup): added self.pool._mark_activity(...) in engine/core.py reacquire_after_slot_yield and tool_dispatcher.execute_tool finally. Broke _BoundaryPool fake (AttributeError) AND a new test using a MagicMock pool silently no-opped the call so the last_activity assertion passed falsely until wired with a real side_effect."
---

## Goal
Keep hand-rolled test stubs (`SimpleNamespace`, `MagicMock`) in lockstep with production classes across a refactor, so the suite doesn't fail with `AttributeError` (closed stubs) or silently pass on a MagicMock that no-ops a newly-added call (asserting on state it never mutated).

## Procedure
### Step 1 — Grep the whole test tree for every stub that shadows the class
Before you finish a refactor that (a) adds/renames a method, (b) replaces a private-field read (`obj._x`) with a public call (`obj.is_x()`), or (c) **adds a new call to an existing method** (e.g. `self.pool._mark_activity(...)`), grep `tests/` for the class name AND for stub constructors:
```
grep -rn "SimpleNamespace(" tests/ | grep -i "<ClassName>"
grep -rn "_mark_activity\|_disabled_names" tests/   # methods now called that stubs may not define
```
A stub built as `SimpleNamespace(a=..., b=...)` or a hand-rolled class like `_BoundaryPool` is a **closed** surface — it exposes ONLY the attributes you listed. The moment production calls a new attribute, every such stub breaks with `AttributeError: '..._BoundaryPool' object has no attribute '_mark_activity'`. Add the method to each closed stub (a lambda or small method mirroring the real behavior), with a comment noting the stub must mirror the real surface.

### Step 2 — Distinguish "stub is missing an attribute" from "production regressed"
An `AttributeError` on a `SimpleNamespace`/closed class is almost always a **test-fixture** gap, not a production bug — the production object (a real class) has the method. Confirm by checking the failing frame: if it's `tool_dispatcher.py → self.pool._mark_activity(...)` and `pool` is your fake, fix the fake, not the code. Don't "fix" production to tolerate a missing attribute (e.g. re-adding `getattr(..., None)`) — that hides the real contract.

### Step 3 — THE FALSE-PASS HALF: a MagicMock silently no-ops a newly-added call
This is the higher-risk half and the one most often missed. A `MagicMock` pool auto-generates `_mark_activity` as a no-op Mock, so production code that calls it **succeeds** and your test's `ok is True` assertion passes — but the side effect you actually care about (bumping `last_activity`, marking active, clearing a flag) **never happened**. If you then assert on that state (`assert inst.last_activity > stale_before + 500`), it fails in a confusing way; if you only assert on the return value, it passes falsely and hides that your fix is inert.
- **Fix:** wire a real `side_effect` on the mock method so it mutates the object under test, then assert on BOTH the call AND the mutated state:
```python
pool = MagicMock()
pool._mark_activity.side_effect = lambda name: setattr(inst, 'last_activity', time.monotonic()) \
    if name == inst.instance_name else None
...
pool._mark_activity.assert_called_once_with('P')          # it was called
assert inst.last_activity > stale_before + 500.0          # AND it actually mutated state
```
- **Negative case matters too:** assert `pool._mark_activity.assert_not_called()` on the failure path (e.g. re-acquire returns False) to prove you don't wrongly stamp a non-holder. A fix that stamps on both success and failure is as broken as one that never stamps.

### Step 4 — Removing a "defense-in-depth" filter can change fixture behavior
A manual filter like `if active_only: skills = [s for s in skills if s['name'] not in disabled]` looks redundant but is **only** redundant on the real production path. A test fixture that bypasses the real entry point (injects a disabled skill directly into `_skills_registry` instead of calling `discover()`) relies on that filter. When you remove it, such a test now sees the skill and fails — fix the unrealistic fixture, don't keep the dead filter.

### Step 5 — Verify both directions after a semantics change
Run the full affected files serially (`-o addopts=""`) and confirm: (a) no new `AttributeError` from closed stubs, (b) no false-pass on MagicMock no-ops (assert on mutated state, not just return), and (c) the production-path test still asserts the real behavior. A test that "passes" only because a mock masks the bug is worse than a failing one — it hides the regression.

## Tips
- Prefer public methods over private-field reads in tools; but when you make that swap, treat every stub as a contract surface that must be updated in the same commit (see [[additive-flag-param-caller-migration]]).
- `SimpleNamespace`/closed-class stubs fail LOUDLY on a missing attribute (good); `MagicMock` fails SILENTLY by no-op-ing it (dangerous). When you add a call to an existing method and your test uses a MagicMock, assume the side effect did NOT happen until you wire a real `side_effect`.
- When rewording an assertion to "fixture artifact," keep it honest: name the specific production-path test that covers the real behavior.
- Run the full affected suite serially from the host shell (`-o addopts=""`), not code_interpreter — see [[pytest-ini-addopts-xdist-serial-run]].
