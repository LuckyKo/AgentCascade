# Audit: session-log dir paths missing `make_instance_dir()` (todo.md line 142)

**Mode:** Investigative (root-cause / evidence-based inventory)
**Confidence:** Confirmed (all findings read directly from source; cross-checked against the canonical resolver and project memory `[[session-logs-are-workspace-scoped]]`).
**Scope:** every place that constructs a session-log / `logs` directory for reading or writing SESSION data (per-agent `.jsonl` logs, saved sessions) and is missing the instance-dir wrapper.

## Background (verified)

Parallel AC instances isolate data via `AGENT_CASCADE_INSTANCE_ID`. The canonical resolver is
`make_instance_dir(base_path)` in `agent_cascade/instance_id.py:58`: it appends `_<instance_id>` to the
leaf dir name (`logs` → `logs_prod`); empty instance ID = legacy single-instance, returns path unchanged.

- Session logs are **written** to the instance dir: `pool/logger_mgr.py:29` does
  `make_instance_dir(str(self.workspace_dir / 'logs'))`.
- The session-**load** path `_load_session_history()` (`api_server.py:375-378`) already uses `make_instance_dir(...)`. ✅

## CONFIRMED GAPS — need the `make_instance_dir()` wrapper (2)

### 1. `api_server.py` — `api_list_sessions()` (`@app.get('/api/sessions')`)  ← the todo.md:142 bug
- **Lines:** 1065-1068
- **What it does:** scans the `logs` dir for session `.jsonl` files via `_scan_sessions_sync(log_dir)` (line 1074) → this is the UI "saved sessions" listing.
- **Current code:**
  ```python
  async def api_list_sessions():
      if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
          log_dir = agent_pool.operation_manager.base_dir / 'logs'
      else:
          log_dir = Path(DEFAULT_WORKSPACE) / 'logs'
  ```
- **Bug:** for a named instance it scans the plain `…/logs` folder instead of `…/logs_<instance_id>`, so the listing shows the wrong (empty/shared) corpus. This is exactly todo.md line 142 ("saved sessions folder … still points at /logs").
- **Corrected** (matches established pattern at `api_server.py:375-378`; keep the `agent_pool and` guard — here `agent_pool` can be None, unlike `_load_session_history` which early-returns):
  ```python
  async def api_list_sessions():
      from agent_cascade.instance_id import make_instance_dir
      if agent_pool and hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
          log_dir = Path(make_instance_dir(str(agent_pool.operation_manager.base_dir / 'logs')))
      else:
          log_dir = Path(make_instance_dir(str(Path(DEFAULT_WORKSPACE) / 'logs')))
  ```
- **Import note:** `DEFAULT_WORKSPACE` is already module-level (`api_server.py:47`). `make_instance_dir` is NOT module-level — it's lazily imported inside `_load_session_history` (line 373). Add the same lazy import inside this function.

### 2. `ws_handlers.py` — agent-pool recovery fallback glob (resume path)
- **Lines:** 477-481
- **What it does:** on resume, for each sub-agent instance it recovers conversation from its log file. Primary path uses `logger_inst.log_path` (line 474) which IS instance-aware; the **fallback** globs `{agent_class}_{sa_name}_*.jsonl` in the plain `logs` dir when no live logger exists.
- **Current code:**
  ```python
  if hasattr(self.agent_pool, 'operation_manager') and self.agent_pool.operation_manager:
      log_dir = self.agent_pool.operation_manager.base_dir / 'logs'
  else:
      log_dir = Path(DEFAULT_WORKSPACE) / 'logs'
  pattern = f"{agent_class}_{sa_name}_*.jsonl"
  ```
- **Bug:** same class — the fallback glob targets plain `…/logs` instead of `…/logs_<instance_id>`, so a named instance's sub-agent logs (written under the instance dir by `logger_mgr.py`) are not found on resume-fallback.
- **Corrected** (matches pattern; note indentation is deeper here — inside nested try/for):
  ```python
  from agent_cascade.instance_id import make_instance_dir
  if hasattr(self.agent_pool, 'operation_manager') and self.agent_pool.operation_manager:
      log_dir = Path(make_instance_dir(str(self.agent_pool.operation_manager.base_dir / 'logs')))
  else:
      log_dir = Path(make_instance_dir(str(Path(DEFAULT_WORKSPACE) / 'logs')))
  pattern = f"{agent_class}_{sa_name}_*.jsonl"
  ```
- **Import note:** `DEFAULT_WORKSPACE` is already lazily imported in the same function scope (`ws_handlers.py:453`). `make_instance_dir` is NOT imported anywhere in `ws_handlers.py` — add the lazy import at the top of this block.

## ALREADY CORRECT (use `make_instance_dir`) — do not touch
| File | Line(s) | What | Status |
|---|---|---|---|
| `api_server.py` | 375-378 | `_load_session_history()` (session load) | ✅ correct |
| `pool/logger_mgr.py` | 29 | instance log base — the **write** path | ✅ correct |
| `path_security.py` | 23 | media dir (feeds `/api/file` via `_is_path_allowed`) | ✅ correct |
| `utils/media_utils.py` | 38 | media root (`get_images_dir`) | ✅ correct |
| `telemetry.py` | 43 | telemetry log dir | ✅ correct |

## NOT GAPS (different mechanism / not session data) — excluded, with rationale
- **`pool/session_io.py`** — path-agnostic. `load_session_from_log(log_input)` receives a fully-qualified path from its caller (`_load_session_history`, already instance-aware); `_parse_json_input` only resolves *relative* inputs against `workspace_dir`. It does NOT construct a `logs` dir. Correct as-is.
- **`api_integration_pkg/streaming.py:113`** — `stream_probe_backend` debug logger. Uses an instance-suffixed **filename** (`stream_probe_backend_<id>.log`) in the plain `logs` dir — same convention as the console.log mechanism (uses `get_instance_suffix()`). Not session data. Correctly excluded per task.
- **`log.py:187`** — console.log file handler. **Explicitly excluded by the task.**
- **`inner_loop_detect.py:126`** — `_LOOP_SAMPLES_DIR = os.path.join(DEFAULT_WORKSPACE, 'logs', 'loop_samples')`. Loop-detection samples (module-level constant), NOT session/conversation data. Out of scope for this bug class. *(Observation only: it does live in the plain `logs` dir and is not instance-isolated — flag separately if loop-sample isolation ever matters; low confidence it's a real problem.)*
- **`operation_manager/file_operations.py`** (178, 842, 1358, 1694, 2120, 2262, 2343) — file-op **backups** under `base_dir/logs/backups/<agent>`. Not session data. Out of scope.
- **`llm/oai.py:551, 839`** — API debug dumps under `logs/debug`. Not session data. Out of scope.
- **`tool_utils.py:251`** — spillover dir under `base_dir/logs/spillover`. Not session data. Out of scope.
- **`agent_server/{assistant,database,workstation}_server.py`** — separate legacy subsystem using `work_space_root` + `history/` dirs (not the AC instance-isolated `logs` mechanism). Out of scope.
- **`web_ui/app.js:967`** — frontend client; just `fetch('/api/sessions')`. No logs-dir construction in the UI. (Fixing gap #1 fixes the UI.)

## Recommendation
Apply both corrected snippets above (gaps 1 & 2). Both are read-side lookups that mirror the already-correct write path (`logger_mgr.py:29`) and load path (`api_server.py:375-378`), so they will now point at `logs_<instance_id>` for named instances and remain unchanged for legacy single-instance (empty ID → `make_instance_dir` returns path unchanged).

## Open questions / remaining unknowns
- None blocking. The inventory is complete against all `…/'logs'` constructions, all `.jsonl` glob/`scandir` patterns, and the UI-facing dirs (`agent_server/`, `web_ui/`).
- If loop-sample or debug-dump instance isolation is desired later, that's a separate (non-session) change — not part of todo.md:142.
