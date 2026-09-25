---
name: fastapi-closure-var-monkeypatch-in-tests
description: Monkeypatch a closure-bound dependency (e.g. agent_pool) inside FastAPI create_app() routes from test code by scanning route endpoint __closure__ cells — no production hook needed.
source: auto-generated
version: "1.0.0"
triggers:
  - "create_app closure variable unreachable in tests"
  - "monkeypatch dependency bound by reference in fastapi routes"
  - "app.state has no agent_pool"
  - "test hermetic endpoint with shared pool object"
  - "agent_pool closure var cannot import or patch"
  - "route endpoint __closure__ cell_contents isinstance"
  - "mock real agent_pool from test without production hook"
generated_by: coder
generated_from_task: "Phase 1 AC Telegram bridge REST endpoints in api_server.py — needed to mock the real agent_pool (a closure var of create_app) from tests/test_api_endpoints.py hermetically, without adding any production hook or app attribute."
---

## Goal
Make a dependency that `create_app()` binds into route closures by reference (e.g. an `AgentPool`) monkeypatchable from hermetic tests, WITHOUT adding any production hook or app attribute.

## Why it bites
`def create_app(agents, agent_pool, ...)` closes over `agent_pool`; every nested `@app.post(...)` endpoint references it via closure. It is NOT on `app.state`, so `client.app.state.agent_pool` raises `AttributeError` (that's Starlette request-state). You cannot import or patch the variable directly — it lives only in each endpoint's `__closure__`.

## Procedure
### Step 1 — Recover the real object from a route closure
The same object is shared by reference across every route, so grab it once:

```python
from agent_cascade.agent_pool import AgentPool

def recover_pool(app):
    for route in app.router.routes:
        fn = getattr(route, 'endpoint', None)
        if fn is None or not fn.__closure__:
            continue
        for cell in fn.__closure__:
            try:
                val = cell.cell_contents
            except ValueError:          # empty cell
                continue
            if isinstance(val, AgentPool):
                return val
    raise RuntimeError('no AgentPool found in route closures')
```

### Step 2 — Swap attributes on the real object, restore in finally
Endpoints read attributes off the pool at call time, so `setattr` your fakes onto the recovered object and always restore (tests must not leak state):

```python
pool = recover_pool(client.app)
orig = {a: getattr(pool, a, None) for a in ('instances', 'operation_manager')}
try:
    pool.instances = {...}
    pool.operation_manager = fake_om
    resp = client.post('/api/stop', params={'token': tok}, headers=auth_hdrs)
finally:
    for a, v in orig.items():
        setattr(pool, a, v)
```

### Step 3 — Verify the endpoint actually used your fake
Assert on a side effect you can observe (a call counter on the fake), not just status code. e.g. `assert fake_pool.stop_session_calls == 1`.

## Tips
- **Fake op-manager must expose every attribute the state builder reads.** In this repo `build_state()`'s fallback path reads `agent_pool.operation_manager.base_dir` for `default_workspace`; if it's missing, `/api/status` and every `_broadcast_state()` 500 even though your endpoint logic is correct. Give fakes a `base_dir`.
- **A module-level logger must exist** if any except-path you exercise references it — a function-local `from ... import logger` is invisible to sibling functions. Check for `NameError: <name>` in test output; that's the tell.
- **`isinstance` gate is essential**: closures also hold ints/strings/locks; without it you may return the wrong cell.
- This complements (not replaces) "extract a faithful core" testing — use it when you want to drive the REAL endpoint path end-to-end with fakes rather than re-implementing logic in the test.
- For pure closure *functions* (not objects, e.g. a nested `broadcast()`), this does NOT work — there's no object to grab; extract-the-core is the only option. See [[broadcast-closure-untestable-from-tests]] project memory for the full contrast.
