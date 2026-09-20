---
name: regression-test-pool-bound-endpoint
description: How to write a regression test for an AgentCascade API endpoint that depends on the AgentPool (e.g. /api/sessions) when the shared test_app fixture does not expose its pool — build a minimal real pool + drive the production code path directly.
source: auto-generated
version: "1.0.0"
triggers:
  - "regression test api endpoint"
  - "test_app agent_pool attribute error"
  - "make_instance_dir logs_<id>"
  - "api/sessions instance aware"
  - "operation_manager base_dir stub"
generated_by: coder
generated_from_task: "Add regression test for todo.md:142 fix — /api/sessions must read from logs_<instance_id> for named AC instances."
---

## Goal
Write a self-contained regression test for an AgentCascade FastAPI endpoint whose behavior depends on the `AgentPool` (log-dir resolution, session listing, etc.), without being blocked by the shared `test_app` fixture that hides its pool.

## Why this is needed
In `tests/test_api_endpoints.py`, the module-scoped `test_app` fixture calls `create_app(agents=..., agent_pool=pool, ...)` and returns **only** the FastAPI app — it never sets `app.agent_pool`. So `test_app.agent_pool` raises `AttributeError: 'FastAPI' object has no attribute 'agent_pool'`. You cannot point that endpoint at a temp workspace through the shared fixture. The fix is to build your own minimal pool and drive the production code path directly.

## Procedure
### Step 1 — Build a minimal real AgentPool with a stub operation_manager
The endpoint typically reads only `pool.operation_manager.base_dir` to resolve paths. A stub exposing just that attribute is enough:
```python
from agent_cascade.agent_pool import AgentPool

class _StubOperationManager:
    base_dir = workspace  # tmp_path / 'workspace'

llm_cfg = {'model': 'test_model', 'model_server': 'http://127.0.0.1:1/v1',
           'api_key': 'EMPTY', 'model_type': 'qwenvl_oai', 'max_input_tokens': 8192}
pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'),
                 workspace_dir=str(workspace), operation_manager=_StubOperationManager())
```
A real `AgentPool` (not a MagicMock) is required because the load/scan paths touch many internal attributes (`_execution`, `_logger`, `_pool_lock`, `templates`, …). Use a fake `model_server` port — these tests never call the LLM.

### Step 2 — Load a fixture session via the real path
Write a minimal `.jsonl` (metadata header line + one user-role line), then load it with the production method so in-memory + on-disk state match:
```python
pool.load_session_from_log(str(session_file))
```

### Step 3 — Drive the same production code path the route uses
Don't re-implement the logic. Call the exact helpers the endpoint calls, e.g. for `/api/sessions`:
```python
from agent_cascade.instance_id import make_instance_dir
from agent_cascade.api_server import _scan_sessions_sync
log_dir = Path(make_instance_dir(str(pool.operation_manager.base_dir / 'logs')))
names = [s['name'] for s in _scan_sessions_sync(log_dir)] if log_dir.exists() else []
```
This exercises production code end-to-end without the HTTP layer (justified: the shared fixture can't be re-pointed).

### Step 4 — Manipulate AGENT_CASCADE_INSTANCE_ID safely
`conftest.py` sets `AGENT_CASCADE_INSTANCE_ID` globally at import time. Capture and restore in try/finally so you don't leak into other tests (esp. under xdist parallel workers):
```python
saved = os.environ.get('AGENT_CASCADE_INSTANCE_ID')
os.environ['AGENT_CASCADE_INSTANCE_ID'] = 'regress_inst'   # or pop() for the unset case
try:
    ...
finally:
    if saved is None: os.environ.pop('AGENT_CASCADE_INSTANCE_ID', None)
    else: os.environ['AGENT_CASCADE_INSTANCE_ID'] = saved
```

### Step 5 — Prove it's a real regression test
Temporarily remove the fix (e.g. drop `make_instance_dir` from the resolution), confirm the test FAILS, then revert. A test that passes with or without the fix is worthless. See [[regression-test-revert-proof]].

## Tips
- Use `tmp_path` per test; each test builds its own pool → no shared-state pollution across xdist workers.
- Assert BOTH directions: session in suffixed dir appears when env set, AND it would NOT appear if placed only in plain `logs/` (and vice-versa for the unset case).
- `_scan_sessions_sync` parses `<agent>_<instance>_<date>_<time>.jsonl` → stem split on `_`; name a fixture file with ≥3 underscore parts so the instance name resolves cleanly.
- Keep unrelated tests out of the regression class (reviewers will flag them), but don't move pre-existing ones as part of "add a test" scope.
- Run serial (`-n 0`) first for clean tracebacks, then default xdist to check for worker flakiness. See [[pytest-docker-subprocess-hang]] and [[pytest-ini-addopts-xdist-serial-run]].
