---
name: stub-mock-attribute-parity-after-refactor
description: When a refactor adds/renames a method, removes a private-field read, OR adds a call to an existing pool/engine method, update every SimpleNamespace/MagicMock test stub that shadows it — otherwise tests fail with AttributeError (closed stubs) or silently PASS on a MagicMock no-op (asserting on state the real method would have mutated). Covers both halves.
source: auto-generated
version: 1.0.2
triggers:
  - "MagicMock attribute parity"
  - "test stub missing new method"
  - "AttributeError in test after refactor"
  - "silent pass on mock no-op"
generated_by: orchestrator
generated_from_task: "BUG_0081/BUG_0080 fixes in AgentCascade regression triage"
---

## Goal
Keep every test double (SimpleNamespace, MagicMock, custom stub class) in parity with the production interface it shadows, so a refactor surfaces as an explicit test failure rather than a silent wrong-behavior pass or a crash on an auto-created mock attribute.

## Procedure

### Step 1 — Find every stub that shadows the changed surface
When you add/rename/remove a method or field on a class (e.g. `AgentPool`, `ExecutionEngine`, `OperationManager`), grep tests for both the class name and the member name:

```bash
grep -rn "operation_manager" tests/ --include="*.py"
grep -rn "SimpleNamespace(" tests/ | grep -i pool
```

Stubs come in three shapes — check all of them:
- `SimpleNamespace(...)` / `types.SimpleNamespace` — CLOSED stubs: missing attribute → `AttributeError` at runtime (loud failure).
- Bare `MagicMock()` — OPEN stubs: every attribute auto-creates as a truthy MagicMock (silent misbehavior, see Step 3).
- Custom stub classes (`class FakePool:`) — closed like SimpleNamespace but often copied between test files.

### Step 2 — Update each stub for the new/renamed member
For a NEW method the production code now calls: add it to every closed stub with a realistic return shape (not just a no-op):

```python
# Before refactor: stub had .acquire_slot; production now also calls .release_slot
stub_pool = SimpleNamespace(
    acquire_slot=lambda **kw: None,
    release_slot=lambda **kw: None,   # ← added in the same commit as the production change
)
```

For a RENAMED member: update stub AND keep a one-line comment citing the rename so the next refactor knows it's load-bearing.

### Step 3 — Audit OPEN MagicMock paths for truthy auto-attribute traps
A bare `MagicMock` returns a truthy mock for ANY attribute read, including ones that are `None`/absent on the real object. Two failure modes:

1. **Branch-routing trap** (BUG_0080 class): production does `if getattr(obj, 'photo', None):` to detect media. On a bare MagicMock this is ALWAYS truthy, so text-only test updates get routed into the media branch and crash on numeric fields (`file_size > MAX_BYTES` → `TypeError: '>' not supported between 'MagicMock' and 'int'`). **Fix in the fixture**: pin every attribute the production code probes to its real absent-value:

```python
update.message.photo = None
update.message.document = None
update.message.caption = None
```

   Idiom already established in this repo: `tests/test_telegram_bridge.py::_make_update` — when writing a new `_make_update`-style helper, mirror it verbatim.

2. **Method-call trap** (BUG_0081 class): production calls `pool.operation_manager.evaluate_shell_command_safety(...)`. If `operation_manager` is legitimately `None` on the real object (constructor default) or is a MagicMock stub lacking the method, the call crashes with AttributeError. **Fix in production**: guard with the same tolerance the sync path already has —

```python
_op_mgr = getattr(self.agent_pool, 'operation_manager', None) if self.agent_pool else None
if _op_mgr is not None:
    try:
        _decision = _op_mgr.evaluate_shell_command_safety(command, cwd, agent_name, resolved_cwd=resolved_cwd)
    except AttributeError:
        _decision = (strict, [])   # stub lacking the widened API → strict fallback
else:
    _decision = (strict, [])
```

   Prefer `try/except AttributeError` over `isinstance(x, MagicMock)` in production code — it handles every stub shape without importing unittest.mock into prod modules.

### Step 4 — Verify both directions
- Run the affected test file(s) post-change: closed-stub updates must pass; open-mock traps must no longer route into the wrong branch (assert on a call-count or log line, not just "no exception").
- Re-run the files that shadow the changed surface (grep for the changed names; hit count > 5 files = wide); full suite only on supervisor instruction (see scoped-tests-default-full-suite-on-instruction).

## Tips
- **Closed stubs fail loud, open mocks fail silent.** A new AttributeError in tests after a refactor = closed stub missing the member (easy fix). A test that passes but production misbehaves = MagicMock no-op or truthy auto-attribute (harder — instrument with call counts).
- **Never add `isinstance(x, MagicMock)` guards to production code** — use `try/except AttributeError` + `getattr(..., None)` fallbacks instead; they cover custom stub classes too.
- **When writing a new mock-fixture helper in this repo, copy the most recent one verbatim** (e.g. `_make_update` in test_telegram_bridge.py) rather than reinventing — parity with sibling fixtures is what catches branch-routing traps at review time.
- **The fixture fix and the production guard are complementary, not alternatives**: pinning `photo=None` fixes the test; the None-guard on `operation_manager` fixes a real production state (pool constructed without an advisor). Both belong in the same commit only when they came from the same regression run — otherwise split them.
- **Cite the sibling idiom in a comment** (`# same idiom as tests/test_telegram_bridge.py::_make_update`) so the next refactor knows the pinning is load-bearing, not incidental.
