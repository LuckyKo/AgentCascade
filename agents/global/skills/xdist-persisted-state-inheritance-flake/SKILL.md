---
name: xdist-persisted-state-inheritance-flake
description: Diagnose and fix pytest/xdist test flakes where a class __init__ loads persisted config state from a session-scoped shared temp dir, so later tests inherit earlier tests' mutations (agent_priorities, endpoint cooldowns) and exact retry/backoff count assertions break nondeterministically under parallel load. Covers the "passes in isolation" bisection and the factory-level file-wipe fix for AgentCascade APIRouter test helpers.
source: auto-generated
version: "1.0.0"
triggers:
  - "xdist flake passes in isolation"
  - "expected exactly N backoff got 0"
  - "test pollution persisted config agent_priorities"
  - "shared temp dir test state APIRouter"
  - "router inherits prior test endpoint priorities"
generated_by: orchestrator
generated_from_task: "Run regression tests on AgentCascade; one xdist-only failure in test_retry_baseline backoff counts where a later router inherited persisted agent_priorities from earlier tests in the same worker's shared temp config dir"
---

## Goal
Fix intermittent parallel-test failures caused by a constructor loading persisted state from a shared per-worker temp dir, so each test starts from a clean slate.

## Procedure

### Step 1 — Confirm it's an isolation bug, not a regression
- Re-run the failing test in isolation: `python -m pytest <file>::<test> -o addopts="" --timeout=60` (override addopts if pytest.ini hard-codes `-n auto`; see `pytest-ini-addopts-xdist-serial-run`).
- Also run the whole file serially and a few repeats in one process. If it passes there but fails under `-n auto`, the production code is likely fine — suspect cross-test state pollution.

### Step 2 — Find the shared-state channel
- Check conftest for session-scoped autouse fixtures that set an env var or temp dir (e.g. `AGENT_CASCADE_TEST_CONFIG_DIR` via `tmp_path_factory.mktemp`). Under xdist, `tmp_path_factory.mktemp('name')` is unique PER WORKER but SHARED by every test in that worker's process — so N tests in one worker share the same file/dir.
- Grep the class under test for `_load()` / persistence reads in `__init__`, and for methods that write (add/update/set_*). If a test's setup mutates persisted state, EVERY later constructor in the same worker inherits it.

### Step 3 — Trace the inheritance to the failing assertion
- Work out which inherited field changes behavior: e.g. persisted `agent_priorities['coder']` from an earlier test makes `get_endpoint_chain()` resolve a different endpoint than the one this test only `add_endpoint()`'d (no `set_agent_priorities`). The test's own endpoint never enters the chain → exact retry/backoff counts become nondeterministic.
- Verify by reading the chain-resolution code path, not by guessing: confirm the inherited value is actually consulted before the assertion-relevant decision.

### Step 4 — Fix at the helper level (minimal)
- In the module's shared factory (e.g. `make_router()`), delete the persisted file inside the test-only env-var dir BEFORE constructing the object:
  ```python
  import os
  from pathlib import Path
  d = os.environ.get('AGENT_CASCADE_TEST_CONFIG_DIR')
  if d:
      stale = Path(d) / 'api_endpoints.json'
      if stale.exists():
          stale.unlink()
  return APIRouter(default_llm_cfg=cfg)
  ```
- Guard with the env var so production config is never touched. No cross-worker race: each xdist worker has its own temp dir, and tests run sequentially within a worker.

### Step 5 — Verify
- Re-run the full suite under `-n auto`; the previously flaky test must pass under parallel load (not just serially).

## Tips
- The signature is "passes in isolation / whole-file serial, fails only under xdist" — always bisect that way before touching production code.
- Exact-count assertions (`== 1`) are what expose this; `>=` assertions mask it. Prefer the factory-level wipe over per-test `set_agent_priorities` additions (fixes all current AND future tests in the module).
- Docstring the WHY in the helper — "clean slate regardless of what other tests did earlier in the same worker process" — so nobody reverts it as dead code.
- Related: `xdist-shared-tree-test-isolation` (blanket-delete race on a shared tree), `constructor-loads-state-before-redirect-leak` (init reads state before redirect can take effect).
