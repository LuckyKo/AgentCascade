---
name: live-config-change-test-traps
description: Test-harness traps when making a setting read live via getattr(settings, key, DEFAULT) — MagicMock auto-attributes and fake method signatures that silently break the suite.
source: auto-generated
version: "1.0.0"
triggers:
  - "getattr settings default"
  - "live config update test"
  - "MagicMock settings attribute"
  - "unexpected keyword argument"
  - "wire setting to UI live"
generated_by: coder
generated_from_task: "todo 148: wire AUTO_SKILL_MIN_TURNS to the HTML/JS UI with live update (no restart)"
---

## Goal
Keep the test suite green when you change a setting from an import-time constant to a live `getattr(pool.settings, 'key', DEFAULT)` read. Two silent breakages — each cost a separate regression failure in the task that generated this skill — plus one consumer-enumeration check. This is the TEST-SIDE complement to `runtime-live-config-verification` (the production-side check).

## Procedure
### Trap 1 — MagicMock auto-attribute breaks getattr-default numeric gates
When production does `if x <= getattr(settings, 'key', DEFAULT):` and a test's `settings` is a bare `MagicMock()`, the attribute `settings.key` is AUTO-CREATED as a child `MagicMock`. Comparing an int against that mock raises / misbehaves, so the gate silently mis-fires.
**Fix:** in every harness that builds a mock pool, set the attribute explicitly to a real value:
```python
pool = MagicMock()
pool.settings.auto_skill_min_turns = min_turns   # real int — not an auto-created child mock
```
A real dataclass (e.g. `PoolSettings()`) always has the field; mirror that in the mock. If the harness drives the gate by patching the module constant instead, that patch no longer reaches a live-read path — set the attribute rather than relying on the constant patch.

### Trap 2 — fakes of a method break when production adds a kwarg
If you thread a new value into an existing method (e.g. `auto_skill_qualifies(..., min_turns=...)`), every hand-written FAKE of that method with the old signature raises `TypeError: got an unexpected keyword argument 'min_turns'`. Real instances are fine; only fakes break.
**Fix:** grep for every fake definition and add the param (even if the fake ignores it):
```python
def auto_skill_qualifies(self, instance, current_turn, loaded_skill_names=None, min_turns=None):
    ...
```
Run `grep -rn "def <method_name>" tests/` to find all fakes in one pass.

### Step 3 — enumerate ALL consumers before declaring done
A setting is often gated in MORE than one place (a fast-path gate + a prompt/budget builder). Grep the constant name across `.py`; wire EVERY read site to the live value, not just the one the plan/research named. A plan can under-count consumers — re-grep yourself rather than trusting the scoping.

## Tips
- `getattr(obj, 'key', CONST)` is the safe live-read form (handles `obj is None` and a missing attr). Use it at every consumer.
- For a method gaining an optional param, resolve the fallback at CALL time (`if param is None: param = CONST`), NOT as a def-time default bound to the constant — def-time defaults do not see `patch()`ed module constants in tests.
- Run the seam regression suite (state_builder / config_handlers / engine / any file with fakes of the touched method) after wiring; Trap 1 and Trap 2 surface THERE, not in the feature's own new tests.
