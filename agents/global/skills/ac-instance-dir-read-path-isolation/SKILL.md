---
name: ac-instance-dir-read-path-isolation
description: AgentCascade parallel-instance data isolation — every READ path that resolves a session/log directory must route through make_instance_dir(), or named instances (AGENT_CASCADE_INSTANCE_ID) silently scan the wrong /logs folder.
source: auto-generated
version: "1.0.0"
triggers:
  - "saved sessions empty for instance"
  - "make_instance_dir"
  - "AGENT_CASCADE_INSTANCE_ID"
  - "session log directory"
  - "parallel AC instances"
  - "logs_<instance>"
  - "instance isolation"
generated_by: orchestrator
generated_from_task: "todo.md line 142 — saved sessions folder for a separate instance of AC still points at /logs instead of its special named folder"
---

## Goal
Prevent the recurring AgentCascade bug where a NEW read/list/recovery path that resolves a session-log directory forgets to route through `make_instance_dir()`, so named instances (isolated via `AGENT_CASCADE_INSTANCE_ID`) scan plain `/logs` and get empty results — even though the WRITE path already saves to the correct folder.

## Background: how isolation works
- Parallel AC instances isolate data via env var `AGENT_CASCADE_INSTANCE_ID` (validated at startup in `start_api_server.py` / `start_multi_agent.py`).
- Helper `make_instance_dir(base_path)` in `agent_cascade/instance_id.py` appends `_<instance_id>` to the LEAF dir name: `'.../logs'` → `'.../logs_prod'`. **With no instance ID it returns the path unchanged** — so wrapping is always safe and legacy single-instance stays byte-for-byte identical.
- Session logs are WRITTEN to the instance dir: `agent_cascade/pool/logger_mgr.py:~29` does `make_instance_dir(str(self.workspace_dir / 'logs'))`.

## Procedure
### Step 1 — Recognize the symptom
"Saved sessions list is empty for a named instance" or "session not found on resume/recovery for a named instance", while plain single-instance works. This is almost always a READ path missing the wrapper, NOT a write bug. Confirm by checking where logs are actually written (`logger_mgr.py`) vs where the read path looks.

### Step 2 — Audit ALL read-side log-dir constructions
Grep for directory builds that hardcode `... / 'logs'` or reference `DEFAULT_WORKSPACE/'logs'` / `operation_manager.base_dir/'logs'` for SESSION data (jsonl agent logs, saved sessions). Known correct spots to model after: `api_server.py:375-378` (`_load_session_history`), `pool/logger_mgr.py:29` (write), `path_security.py:23`, `utils/media_utils.py:38`, `telemetry.py:43`. Known past gaps: `api_server.py api_list_sessions()` and `ws_handlers.py` resume/recovery fallback glob.

### Step 3 — Apply the canonical pattern
Match `api_server.py:375-378` exactly (lazy import to avoid circular deps; keep existing if/else):
```python
from agent_cascade.instance_id import make_instance_dir   # lazy, inside function
if hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
    log_dir = Path(make_instance_dir(str(agent_pool.operation_manager.base_dir / 'logs')))
else:
    log_dir = Path(make_instance_dir(str(Path(DEFAULT_WORKSPACE) / 'logs')))
```
Verify `Path` and `DEFAULT_WORKSPACE` are already in scope (module-level or earlier lazy import in the same function) — do NOT add redundant imports. Leave any PRIMARY path that already uses an instance-aware source (e.g. `logger_inst.log_path`) untouched; only fix the fallback/plain-dir construction.

### Step 4 — Do NOT touch what's already correct
Write path (`logger_mgr.py`), telemetry, media, and the console.log handler in `log.py` (uses `instance_id` directly on the FILENAME, a different mechanism) are already correct. Minimal surgical changes only.

### Step 5 — Add a regression test
Existing endpoint tests often only assert HTTP 200 + key presence, NOT the resolved dir. Add a test that:
- With `AGENT_CASCADE_INSTANCE_ID` set to a known value, a session placed ONLY in `logs_<id>/` is listed (and/or resolved path == `make_instance_dir(...)`).
- With it unset, a session in plain `logs/` is listed (legacy preserved).
Capture & restore the env var in try/finally (conftest.py sets it globally at import time). If the shared `test_app` fixture doesn't expose its pool, build a minimal real `AgentPool` with an operation_manager stub carrying only `.base_dir` pointed at `tmp_path`, and drive the production resolution path directly. Prove the test is genuine: removing the wrapper must make the suffixed-dir test FAIL.

## Tips
- Symptom asymmetry (writes fine, reads empty) is the tell — go straight to read paths.
- `make_instance_dir` is idempotent-safe for legacy mode; wrapping unconditionally never breaks single-instance.
- Lazy-import `make_instance_dir` at point of use — it lives in a module that participates in the `__init__ → agent → log → instance_id` import cycle.
- After fixing, re-grep both files to confirm NO other plain `/logs` session-dir construction was missed.
- Adjacent (out of scope but noted): `inner_loop_detect.py` loop samples still write to plain `logs/`, not instance-isolated — separate decision if that ever matters.
