---
name: magicmock-autoattr-truthy-guard
description: Guard code that reads flags/collections off an object which may be a bare MagicMock in tests — auto-created attributes are truthy and mis-flag. Use when a new flag/lookup (esp. a live pool setting via getattr(settings, key, DEFAULT)) breaks existing tests using MagicMock pools.
source: auto-generated
version: "1.1.0"
triggers:
  - "MagicMock"
  - "auto-attribute"
  - "truthy mock"
  - "mock pool"
  - "getattr default ignored"
  - "isinstance dict guard"
  - "live pool setting"
  - "_get_pool_setting"
  - "feature flag default off"
generated_by: coder
generated_from_task: "shell_cmd restricted mode for system agents — _is_restricted helper broke 6 async tests using bare MagicMock pools"
---

## Goal
Prevent code that reads a flag or collection from an object (e.g. `pool.instances[name].some_flag`, `getattr(settings, key, DEFAULT)`) from silently mis-behaving when that object is a **bare `MagicMock`** in existing tests — because MagicMock auto-creates *any* attribute access as a new truthy mock, so `getattr(mock, 'flag', False)` returns a truthy mock (NOT the `False` default) and `.get(name)` on a mock collection returns a truthy mock.

## The trap
```python
# production: pool is usually a real object; in many existing tests it's MagicMock()
inst = pool.instances.get(agent_name)          # MagicMock → .get() returns a truthy MagicMock
return bool(getattr(inst, 'restricted_shell', False))  # auto-attr → truthy, default IGNORED

# High-frequency variant — the live-config read idiom (e.g. engine/core.py _get_pool_setting):
settings = getattr(self.pool, 'settings', None)
return getattr(settings, name, default) if settings else default  # MagicMock → default IGNORED
```
Result: every unsafe command on a mock-pool test gets wrongly rejected / mis-flagged; a feature gated on a new default-OFF pool setting silently runs as ON. Verified: `bool(getattr(MagicMock(), 'x', False)) == True`.

## Procedure
### Step 0 — Layer decision: production bug or test-harness bug?
Before choosing a fix, determine WHICH layer is wrong. Read the production default (e.g. `settings.py`).
- **Production default is correct** (e.g. `SOFT_TURN_LIMIT = False`) and the code path is a legitimate idiom (live-config read, dict lookup) → it is a **test-harness bug**: the test's mock never set the new attribute. Fix the TEST (Step 2a) — do NOT add a prod guard for a correct idiom.
- **Production code must be robust to a bare mock** (you can't/shouldn't touch many pre-existing tests, or the guard has real defensive value) → fix the PRODUCTION code with a type guard (Step 2b).
Confirm the default first — the "another agent's feature broke the suite" smell is often a test-harness defect, not a feature defect.

### Step 1 — Confirm it's the auto-attr trap (not a real bug)
When a new flag/lookup breaks existing tests that pass `MagicMock()` as the dependency: check whether the failing path reads an attribute or collection off that mock. Reproduce in isolation:
```python
from unittest.mock import MagicMock
print(bool(getattr(MagicMock(), 'restricted_shell', False)))  # True ← the trap
print(isinstance(MagicMock().instances, dict))                # False ← real pools are dicts
```

### Step 2a — Test-side fix (you OWN the fixture, few tests)
Set the new attribute explicitly on the mock to its production default, alongside the other explicit settings:
```python
pool = MagicMock()
pool.settings.auto_skill_enabled = True
pool.settings.soft_turn_limit = False   # ← MUST set; auto-attr would be truthy → soft mode silently ON
```
Find every affected mock-pool fixture: `grep -rn "pool = MagicMock" tests/` (and `MagicMock(spec=...)` variants). The new feature's OWN tests (which set the flag) pass; the breakage shows up in PRE-EXISTING tests using the same mock-pool pattern — **feature tests passing ≠ whole suite passing.**

### Step 2b — Production-side guard (many pre-existing tests / defensive value)
For a **collection** lookup, require the collection to be a real container:
```python
pool = getattr(self, 'agent_pool', None)
if not pool or not hasattr(pool, 'instances'):
    return False
instances = pool.instances
if not isinstance(instances, dict):   # MagicMock auto-attr is NOT a dict → degrade safely
    return False
inst = instances.get(agent_name)      # real dict: missing key → None → getattr default works
return bool(getattr(inst, 'restricted_shell', False))
```
For a **scalar flag** on the object itself, require the object to be a real type (e.g. `isinstance(obj, SomeClass)`), since you can't isinstance-check an auto-attribute.

### Step 3 — Add a regression test that pins the guard
A bare-mock-pool case MUST be in the suite so the guard (or the explicit mock setting) can't be removed later:
```python
def test_bare_magicmock_pool_is_not_restricted(self, tool):
    tool.agent_pool = MagicMock()          # .instances is an auto-attr mock, not a dict
    assert tool._is_restricted('test_agent') is False
```

## Tips
- **Never** `from unittest.mock import MagicMock` into production code just to `isinstance` against it — that couples prod to the test lib. A type check on the *real* expected container (`dict`, or the real class) is dependency-free.
- The `getattr(obj, 'flag', False)` default is **useless** against MagicMock — it only protects against a genuinely missing attribute on a *real* object.
- **Prefer the test-side fix (Step 2a) when you own the fixture** — it's smaller and keeps correct production idioms clean. Reach for the prod guard (Step 2b) only when you can't touch the many pre-existing tests or the guard has independent defensive value.
- Distinguish the two variants explicitly in your commit message / memory so a later reader doesn't "fix" the wrong layer.
- Related project memories: `.agent_lessons/magicmock-pool-setting-truthy-default.md` (BUG_0141 soft-turn-limit test-harness case), `.agent_lessons/magicmock-closed-attr-trap-kernel-shutdown.md`, `.agent_lessons/shell-cmd-restricted-mode-system-agents.md`.
