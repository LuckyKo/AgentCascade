# SQLite+FTS Log Index — Read-Only Recon

**Date:** 2026-08-29
**Scope:** De-risk an implementation brief for an isolated, additive `agent_cascade/log_index/` package (read-only SQLite + FTS5 index over per-agent JSONL session logs). All paths below are repo-relative to `N:\work\WD\AgentCascade`.
**Confidence:** Confirmed (all claims verified against source + real log files).

> Session logs live in the **WORKSPACE** logs dir (`N:\work\WD\AgentWorkspace\logs/`), NOT the repo's own `logs/`.

---

## 1. Log write path — `agent_cascade/logger/agent_instance_logger.py`

**Single-line writer (the one the indexer hooks into):**
- `_append_line(self, data: Dict)` — `agent_instance_logger.py:221-229`.
  ```python
  def _append_line(self, data: Dict):
      self._ensure_file()
      self._file_handle.write(json.dumps(data, ensure_ascii=False) + '\n')
      self._file_handle.flush()
  ```
  This is the **only** method that appends one line. `data` is a fully-formatted message dict (output of `_format_message`).

**Public append/rewrite methods:**
- `log_message(self, message: Any)` — `:304-310`. Public entrypoint for a single message: `_format_message` → append to in-memory `data["history"]` → `_append_line`.
- `update_history(self, history: List[Any])` — `:371-492`. Additive delta-sync (append new tail OR surgical rewrite via `rewrite_log_with_history`).
- `rewrite_log_with_history(self, new_history: List[Any], allow_shrink=False, caller="unknown") -> bool` — `:496-572`. Full-file atomic rewrite (temp file + `os.replace`) used by session load / edit / delete / compression.
- `reset_history(self, new_history: List[Any], rewrite=False)` — `:801-850`. `rewrite=True` → `_sync_marker_single_write` (`:574-683`); else appends compression-baseline tail.
- `_consolidate_markers_in_jsonl(self, new_pool_state)` — `:685-799`.
- `rollback(self, count, soft=False, reason=None)` — `:865-912`; `truncate_to` — `:914-919`.
- `insert_compression_marker` — `:354-367` is a **deprecated no-op** (do not use).

**Callback / hook / event mechanism:** **None exists.** There is no observer, listener, callback, or on-write signal on `AgentInstanceLogger`. `insert_compression_marker` is a dead no-op. **The indexer must add an optional hook** (e.g. an optional `on_write`/`on_line` callback parameter to `AgentInstanceLogger.__init__` invoked from `_append_line` and the rewrite paths) OR, cleaner and non-invasive, index **from disk** (poll/scan the JSONL files) rather than hooking the writer. Recommendation: scan-based (no logger change, keeps JSONL the single source of truth).

**Log file path construction:** `agent_instance_logger.py:63-76` (`__init__`).
```python
if log_path:
    self.log_path = log_path
else:
    timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
    filename = f"{self.agent_class}_{instance_name}_{timestamp}.jsonl"
    self.log_path = os.path.join(log_dir, filename)
```
`log_dir` is injected by `LoggerManager` (see Q2). So the per-instance file is `<log_dir>/{agent_class}_{instance_name}_{YYYYmmdd_HHMMSS}.jsonl`. The directory (not the filename) is what the indexer needs.

---

## 2. Workspace / instance path resolution

**Workspace root (single source of truth):**
- `agent_cascade/settings.py:81` — `DEFAULT_WORKSPACE: str = _resolve_default_workspace()`; resolver at `:43-77`. Priority: (1) `QWEN_AGENT_DEFAULT_WORKSPACE` env, (2) Docker `/workspace` mount (if `/.dockerenv`), (3) sibling `AgentWorkspace` dir next to project root, (4) `<project_root>/workspace`.
- `agent_cascade/shared_init.py:22-69` — `detect_workspace_dir(project_root)` re-resolves and **sets** `os.environ['QWEN_AGENT_DEFAULT_WORKSPACE']` (line 68) so downstream modules agree.

**Instance separation (`AGENT_CASCADE_INSTANCE_ID`):**
- `agent_cascade/instance_id.py:46-52` — `get_instance_id()` reads the env var.
- `agent_cascade/instance_id.py:61-79` — `make_instance_dir(base_path)`: if an instance ID is set, appends `_<id>` to the dir name (`logs` → `logs_prod`); otherwise returns `base_path` unchanged. **This is the single canonical "right logs dir" resolver.**

**Per-instance log dir (where `logs/` is joined):**
- `agent_cascade/pool/logger_mgr.py:25` — `self.workspace_dir = Path(workspace_dir) if workspace_dir else Path(DEFAULT_WORKSPACE)`.
- `agent_cascade/pool/logger_mgr.py:27-28` —
  ```python
  instance_log_base = make_instance_dir(str(self.workspace_dir / "logs"))
  self.log_dir = Path(instance_log_base)
  ```
- `agent_cascade/pool/core.py:110` — `self._logger = LoggerManager(self, workspace_dir)` (pool ctor takes `workspace_dir` at `core.py:43-51`).
- `agent_cascade/api_server.py:236` (and `:898`) — independent runtime copy of the same resolution used by REST endpoints:
  ```python
  log_dir = Path(make_instance_dir(str(Path(DEFAULT_WORKSPACE) / 'logs')))
  ```

**Cleanest resolution for the indexer (no hard-coding):**
```python
from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.instance_id import make_instance_dir
log_dir = Path(make_instance_dir(str(Path(DEFAULT_WORKSPACE) / "logs")))
```
This mirrors `logger_mgr.py:27` and `api_server.py:236` exactly and honors instance isolation. (If the indexer has access to the live `AgentPool`, prefer `agent_pool.operation_manager.base_dir` → `Path(base_dir)/"logs"` → `make_instance_dir(...)`, the same pattern at `api_server.py:233-236`.)

---

## 3. JSONL line format

**`_parse_json_line` — `agent_cascade/pool/session_io.py:398-430`** (static method):
```python
@staticmethod
def _parse_json_line(line: str) -> dict:
    result = {"messages": [], "metadata": {}}
    item = json.loads(line)
    if isinstance(item, dict):
        if "metadata" in item:
            # metadata wrapper (line 1) OR a message that happens to carry metadata
            if isinstance(item["metadata"], dict):
                result["metadata"].update(item["metadata"])
            elif not item.get("event"):
                result["messages"].append(item)
        elif "event" in item:
            pass                       # skip COMPRESSION/ROLLBACK event markers
        else:
            result["messages"].append(item)
    elif isinstance(item, list):
        result["messages"].extend([m for m in item if isinstance(m, dict)])
    return result
```
So the parser's line taxonomy: **(a)** metadata wrapper line, **(b)** event marker line (skipped), **(c)** plain message line, **(d)** inline-list line.

**Actual key set (verified against real logs in `AgentWorkspace/logs/`):**
- **Line 1 (metadata):** `{"metadata": {agent_class, instance_name, start_timestamp, last_update, current_log_path, working_dir, supervisor}}` — built at `agent_instance_logger.py:78-89`, written by `_initial_save` (`:295`).
- **Message lines (2+):**
  - `role` — `system` | `user` | `assistant` | `function` (constants `agent_cascade/llm/schema.py:21-29`)
  - `content` — main text (may be empty when `function_call` present)
  - `reasoning_content` — optional (assistant)
  - `name` — present on `function` results (tool name)
  - `function_call` — optional dict `{name, arguments}` on assistant tool-call lines
  - `extra` — optional dict `{finish_reason, function_id, tool_success, ...}`
  - `timestamp` — ISO-8601, assigned by `_format_message` (`agent_instance_logger.py:160-217`)

**FTS schema implication:** index `role` + `content` (and optionally `function_call` serialized + `reasoning_content`) as the searchable text; carry `instance_name`, `agent_class`, `timestamp` as structured columns (from line-1 metadata + filename). Skip lines whose top-level key is `metadata` or `event` (matches `session_io.py:413-421` and `tail_sync_check.py:81,99`).

---

## 4. Config / flag mechanism

**Settings object = `PoolSettings` dataclass — `agent_cascade/agent_instance.py:717-837`** (`@dataclass` at `:717`).
- Example bool toggle: `enable_agent_budgeting: bool = False` — `:800`.
- `to_dict()` — `:809-822`; `from_dict(cls, data)` — `:824-837` (filters unknown keys → safe to add a field).
- Instantiated on the pool: `agent_cascade/pool/core.py:89` — `self.settings = PoolSettings()`.

**Live-update + persistence path:**
- `agent_cascade/config_handlers.py:29-79` — `POOL_SETTINGS_KEYS` frozenset (add the new key here so UI edits persist).
- `agent_cascade/config_handlers.py:92-97` — `register_config_handler(key)` decorator.
- Example handler: `config_handlers.py:717-721`
  ```python
  @register_config_handler('enable_agent_budgeting')
  def _handle_enable_agent_budgeting(ui_cfg, agent_pool, agents):
      if agent_pool is not None and hasattr(agent_pool, 'settings'):
          agent_pool.settings.enable_agent_budgeting = bool(ui_cfg['enable_agent_budgeting'])
  ```
- Persisted to `pool_settings.json` via `agent_cascade/pool/config_persist.py` (`save` `:15-74`, `load` `:77-198`).

**Where to add `enable_log_index` consistently:**
1. Add field `enable_log_index: bool = False` to `PoolSettings` (`agent_instance.py`, e.g. near `:800`).
2. Add `'enable_log_index'` to `POOL_SETTINGS_KEYS` (`config_handlers.py:29-79`).
3. Register a handler `@register_config_handler('enable_log_index')` mirroring `:717-721`.
4. (Optional) a module-level default in `settings.py` if the indexer must also run standalone without a live pool.
Reading it at runtime: `agent_pool.settings.enable_log_index` (guarded with `getattr(..., False)`).

---

## 5. REST surface — `agent_cascade/api_server.py`

**App construction / route registration:**
- `create_app(agents, agent_pool, config=None, auto_security=True)` — `api_server.py:155`.
- `app = FastAPI(title="AgentCascade API")` — `:173`.
- All routes are `@app.<verb>("/api/...")` **closures defined inside `create_app`** (not a separate APIRouter). App is instantiated in `start_api_server.py:148` (`create_app(all_agents, agent_pool, chatbot_config, auto_security=...)`).

**Representative simple GET (house style):** `api_server.py:822-833`
```python
@app.get("/api/agents")
async def api_list_agents():
    return [
        {'name': getattr(a, 'name', f'Agent-{i}'), 'index': i, ...}
        for i, a in enumerate(agents)
    ]
```
Other patterns to match:
- `@app.get("/api/sessions")` — `:893-931` → returns `{"sessions": [...]}` (note: it already resolves `log_dir` via `make_instance_dir` at `:896-898`).
- `@app.get("/api/telemetry")` — `:958-967` → returns a dict; empty-dict fallback when `agent_pool` is absent.
- **Query-param pattern:** `api_get_status(token: str = None)` — `:801-802`; `api_serve_file(path: str)` — `:933-956`; `api_reject(request_id: str, reason: str = "Rejected by user")` — `:878-883`. FastAPI binds query strings to plain function params, so `/api/search?q=...` is simply:
```python
@app.get("/api/search")
async def api_search(q: str, limit: int = 20):
    ...
    return {"query": q, "results": [...]}
```
- Error shape is a dict `{"status": "error", "message": ...}` (see `:876,883,947`) or `JSONResponse(status_code=..., content=...)` (see `:947,991,1008`).

---

## 6. SQLite availability

- **`sqlite3` is NOT in `requirements.txt`** (`requirements.txt:1-40`) — but it is a **Python standard-library** module, so it is always importable; no new dependency needed.
- **No existing `import sqlite3` anywhere in `agent_cascade/`** (verified by grep). The `Storage` tool (`agent_cascade/tools/storage.py:28-97`) is a **file-based** key-value store, *not* SQLite — do not mistake it for a SQLite reference.
- **FTS5:** no prior usage to reference; it is a fresh use. FTS5 ships enabled in standard CPython builds (the runtime here is **Python 3.12.6**, per log metadata). It is very likely available, but **verify at runtime** with `sqlite3.connect(':memory:').execute("CREATE VIRTUAL TABLE t USING fts5(x)")` and gracefully fall back (e.g. plain `LIKE` index) if the build lacks FTS5.

---

## 7. Existing test pattern

- **Runner:** `pytest.ini:10` — `addopts = -n auto --timeout=60 --durations=10 -m "not live_api and not skip_if_no_local and not extra_examples and not extra_tools and not extra_vl and not stress"`; `testpaths = tests` (`:13`).
- **Location:** unit tests live under `tests/` (top-level `test_*.py`) plus subdirs by area (`tests/compression/`, `tests/agents/`, `tests/llm/`, `tests/tools/`). Shared fixtures in `tests/conftest.py`; sample data in `tests/fixtures/`.
- **Representative isolated-module example:** `tests/test_dismiss_logger_close.py` (191 lines) — the closest analog for a logger-adjacent module:
  - Explanatory module docstring citing root cause (`:1-14`).
  - `@pytest.fixture def agent_pool(tmp_path)` (`:29-62`) builds a real `AgentPool` with `OperationManager`/`TelemetryCollector`/`APIRouter` **mocked** via `unittest.mock.patch`/`MagicMock`, using a `tmp_path` workspace so the logger opens a real file handle **with no LLM calls** — fast + deterministic.
  - A `make_instance(...)` helper (`:65-78`) constructs minimal `AgentInstance` objects.
- For `log_index/`, match this: a `tests/test_log_index.py` that (a) writes a few synthetic JSONL lines to a `tmp_path` logs dir, (b) builds the SQLite+FTS index over them, (c) asserts FTS5 queries return the expected rows — no LLM, no network, `tmp_path`-only. Logger-specific precedent also in `tests/test_reset_history_rewrite.py` and `tests/test_jsonl_destruction_fix.py`.

---

## Notes / open items for the brief
- **Non-invasive indexing is preferred** over hooking `_append_line`: the JSONL files are append-only and atomic-rewritten (`os.replace`), so a periodic/on-demand scan of `log_dir` is safe and keeps the writer untouched (aligns with "JSONL stays source of truth; SQLite is disposable"). If a live-updating index is required, add an *optional* `on_write` callback to `AgentInstanceLogger.__init__` and fire it from `_append_line` (`:221`) and `rewrite_log_with_history` (`:496`).
- The index DB should live **in the workspace logs dir or a sibling** (e.g. `log_dir / ".index"` or `log_dir / "index.sqlite3"`) so it is disposable and instance-isolated via `make_instance_dir`.
- FTS5 field split recommendation: table `messages(id, agent_class, instance_name, timestamp, role, content)` + `fts_messages` FTS5 table over `content` (and `role` optional); populate by skipping `metadata`/`event` lines (Q3).
