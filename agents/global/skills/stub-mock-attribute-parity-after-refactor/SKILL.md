---
name: stub-mock-attribute-parity-after-refactor
description: "When a refactor adds/renames a method, removes a private-field read, OR drops a defensive getattr, update every test stub that shadows it — otherwise tests fail with AttributeError (closed stubs) or silently PASS on a MagicMock no-op"
source: auto-generated
version: 1.0.3
triggers:
  - "MagicMock attribute parity"
  - "test stub missing new method"
  - "AttributeError in test after refactor"
  - "silent pass on mock no-op"
  - "getattr defensiveness removed"
generated_by: coder
generated_from_task: "BUG_0081/BUG_0080 fixes in AgentCascade regression triage"
---

## Goal

Keep every test double (SimpleNamespace, MagicMock, custom stub class, `cls.__new__(cls)`
skeleton) in parity with the production interface it shadows, so a refactor surfaces as an
explicit test failure rather than a silent wrong-behavior pass or a crash on an auto-created
attribute.

## Procedure

### Step 1 — Find every stub that shadows the changed surface
When you add/rename/remove a method or field on a class (e.g. `AgentPool`, `ExecutionEngine`,
`OperationManager`), grep tests for both the class name and the member name:

```bash
grep -rn "operation_manager" tests/ --include="*.py"
grep -rn "SimpleNamespace(" tests/ | grep -i pool
```

Stubs come in four shapes — check all of them:
- `SimpleNamespace(...)` / `types.SimpleNamespace` — CLOSED stubs: missing attribute → `AttributeError` at runtime (loud failure).
- Bare `MagicMock()` — OPEN stubs: every attribute auto-creates as a truthy MagicMock (silent misbehavior, see Step 3).
- Custom stub classes (`class FakePool:`) — closed like SimpleNamespace but often copied between test files.
- **`cls.__new__(cls)` skeletons** — bypass `__init__` entirely, so *nothing* the constructor
  would have set exists. Any attribute the method now reads directly blows up.

### Step 1b — Removing a defensive `getattr(obj, 'x', default)` is a stub-parity change
The guard was hiding the fact that the stub never set `x`. Two valid fixes — pick by intent:

```python
# WRONG for real instances, RIGHT for a bare stub: keep the guard
if getattr(self, '_dirty_endpoints', False):   # defensive noise if __init__ always sets it
```

- If the attribute really is always set by `__init__` (real instances): drop the guard in
  production, and **add the attribute to every `__new__`-based stub** in the same commit:

```python
router = APIRouter.__new__(APIRouter)
router._config_path = tmp_path / 'api_endpoints.json'
# APIRouter.__new__ bypasses __init__, so set the flags _save() reads directly.
router._dirty_endpoints = False
router._dirty_llm_cfg = False
```

- If the attribute can legitimately be absent on real objects: keep the guard in production and
  say why in a comment, so the next refactor does not "clean it up" again.

Grep for the bypass idiom to find the affected stubs: `grep -rn "__new__(" tests/`.

### Step 2 — Update each stub for the new/renamed member
For a NEW method the production code now calls: add it to every closed stub with a realistic return shape (not just a no-op):

```python
stub_pool = SimpleNamespace(
    acquire_slot=lambda **kw: None,
    release_slot=lambda **kw: None,   # added in the same commit as the production change
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
- Re-run the full suite if the changed surface is widely shadowed (grep hit count > 5 files).
- If the stub writes files, confirm it writes into `tmp_path` and not into a mock-derived path — mocks stringify as their repr and materialize junk directories in the repo root (see `stub-magicmock-path-pollution`).

## Tips
- **Closed stubs fail loud, open mocks fail silent.** A new AttributeError in tests after a refactor = closed stub missing the member (easy fix). A test that passes but production misbehaves = MagicMock no-op or truthy auto-attribute (harder — instrument with call counts).
- **Never add `isinstance(x, MagicMock)` guards to production code** — use `try/except AttributeError` + `getattr(..., None)` fallbacks instead; they cover custom stub classes too.
- **When writing a new mock-fixture helper in this repo, copy the most recent one verbatim** (e.g. `_make_update` in test_telegram_bridge.py) rather than reinventing — parity with sibling fixtures is what catches branch-routing traps at review time.
- **The fixture fix and the production guard are complementary, not alternatives**: pinning `photo=None` fixes the test; the None-guard on `operation_manager` fixes a real production state (pool constructed without an advisor). Both belong in the same commit only when they came from the same regression run — otherwise split them.
- **Cite the sibling idiom in a comment** (`# same idiom as tests/test_telegram_bridge.py::_make_update`) so the next refactor knows the pinning is load-bearing, not incidental.

## Anti-patterns
| Pattern | Why it fails |
|---|---|
| Removing `getattr(x, 'flag', False)` without touching `__new__` stubs | The guard was masking a stub that never ran `__init__` |
| Keeping a defensive `getattr` on an attribute `__init__` always sets | Hides future stub/parity bugs instead of surfacing them |
| Adding a method to one stub and not grepping for the others | The failure reappears in a different test file |
| Asserting only "no exception raised" | A mock no-op satisfies it while the real behaviour is untested |
| Letting a stub's path come from a MagicMock | Creates `MagicMock/<repr>/…` junk in the repo root |
