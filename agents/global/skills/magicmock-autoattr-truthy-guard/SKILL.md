---
name: magicmock-autoattr-truthy-guard
description: Guard production code that reads flags/collections off an object which may be a bare MagicMock in tests — auto-created attributes are truthy and mis-flag. Use when a new flag/lookup breaks existing tests using MagicMock pools.
source: auto-generated
version: "1.0.0"
triggers:
  - "MagicMock"
  - "auto-attribute"
  - "truthy mock"
  - "mock pool"
  - "getattr default ignored"
  - "isinstance dict guard"
generated_by: coder
generated_from_task: "shell_cmd restricted mode for system agents — _is_restricted helper broke 6 async tests using bare MagicMock pools"
---

## Goal
Prevent production code that reads a flag or collection from an object (e.g. `pool.instances[name].some_flag`) from silently mis-behaving when that object is a **bare `MagicMock`** in existing tests — because MagicMock auto-creates *any* attribute access as a new truthy mock, so `getattr(mock, 'flag', False)` returns a truthy mock (NOT the `False` default) and `.get(name)` on a mock collection returns a truthy mock.

## The trap
```python
# production: pool is usually a real object; in many existing tests it's MagicMock()
inst = pool.instances.get(agent_name)          # MagicMock → .get() returns a truthy MagicMock
return bool(getattr(inst, 'restricted_shell', False))  # auto-attr → truthy, default IGNORED
```
Result: every unsafe command on a mock-pool test gets wrongly rejected / mis-flagged. Verified: `bool(getattr(MagicMock(), 'x', False)) == True`.

This is NOT a "fix the test" problem when many existing tests already use bare mocks — the production guard must be robust to them, or you break N unrelated tests.

## Procedure
### Step 1 — Confirm it's the auto-attr trap (not a real bug)
When a new flag/lookup breaks existing tests that pass `MagicMock()` as the dependency: check whether the failing path reads an attribute or collection off that mock. Reproduce in isolation:
```python
from unittest.mock import MagicMock
print(bool(getattr(MagicMock(), 'restricted_shell', False)))  # True ← the trap
print(isinstance(MagicMock().instances, dict))                # False ← real pools are dicts
```

### Step 2 — Guard on TYPE, not presence (do NOT import mock into production)
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
A bare-mock-pool case MUST be in the suite so the guard can't be removed later:
```python
def test_bare_magicmock_pool_is_not_restricted(self, tool):
    tool.agent_pool = MagicMock()          # .instances is an auto-attr mock, not a dict
    assert tool._is_restricted('test_agent') is False
```

## Tips
- **Never** `from unittest.mock import MagicMock` into production code just to `isinstance` against it — that couples prod to the test lib. A type check on the *real* expected container (`dict`, or the real class) is dependency-free and matches existing precedent (e.g. `_has_real_wait_for_message` uses `isinstance(pool, MessageQueueMixin)`).
- The `getattr(obj, 'flag', False)` default is **useless** against MagicMock — it only protects against a genuinely missing attribute on a *real* object. The type guard is what protects against mocks.
- Real production objects (a real `Dict[str, Instance]`, a real instance) pass the guard and behave correctly; only bare mocks degrade to the safe default. This is why the fix is "robust production code", not "fix every test's mock".
- Distinguish from the *test-side* variant (set `mock.flag = False` explicitly on the mock) — that works when YOU own the fixture, but fails when you must not touch many pre-existing tests. Prefer the prod guard in that case.
- Related project memory: `.agent_lessons/magicmock-closed-attr-trap-kernel-shutdown.md` (same trap, `getattr(kc,'_closed',False)` on a mock kernel client) and `.agent_lessons/shell-cmd-restricted-mode-system-agents.md`.
