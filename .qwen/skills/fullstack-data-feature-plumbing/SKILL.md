---
name: fullstack-data-feature-plumbing
description: Add a new metric/field/table end-to-end in AgentCascade — thread it through backend collector → /api endpoint → web UI table, with thread-safety and verification pitfalls.
source: auto-generated
version: "1.0.0"
triggers:
  - "telemetry"
  - "add a table"
  - "new metric"
  - "web ui tab"
  - "api endpoint field"
  - "agent class usage"
  - "config fingerprint"
generated_by: orchestrator
generated_from_task: "todo.md line 205 — change telemetry config fingerprint to model-only and add an agent_class/tool_usage_accuracy/total_time/tokens_generated table in the web UI telemetry tab."
---

## Goal

Add a new data field, metric, or table that must surface from the backend all the way to the web UI in AgentCascade, done correctly (thread-safe, verified) and with minimal safe changes.

## The data path (know it before touching code)

In this codebase a new surfaced value travels exactly:
1. **Collector/backend** — e.g. `agent_cascade/telemetry.py` (`TelemetryCollector`). Accumulate state + expose a getter.
2. **Call site** — where the event is recorded, e.g. `agent_cascade/engine/core.py` turn-start block. This is where per-instance context (e.g. `instance.agent_class`) is available to thread in.
3. **API** — `agent_cascade/api_server.py` endpoint (e.g. `/api/telemetry`). Add the new key to BOTH the success branch and the empty-fallback dict so shape stays stable.
4. **Web UI** — `web_ui/index.html` (table markup + a unique `tbody` id) and `web_ui/app.js` (a renderer function + a call inside the existing `fetch()` handler).

## Procedure

### Step 1 — Locate the exact seams with grep, not guessing
- `grep "def get_.*summary|class .*Collector"` to find the collector.
- `grep "<new concept>" web_ui/*.js` and `*.html` to find the render + fetch functions (e.g. `fetchTelemetry`, `updateTelemetry*Table`).
- Read the API endpoint handler fully — note every return branch you must extend.

### Step 2 — Backend: accumulate under the lock, expose a SNAPSHOT getter
- If the module has a coarse RLock (telemetry uses module-level `_telemetry_lock`), mutate shared state ONLY inside `with _telemetry_lock:`.
- Getter methods should build and return a **fresh list/dict snapshot**, not the live internal dicts — callers then iterate without holding the lock.
- Thread new per-entity dimensions through the record method signature as an optional param (default empty) so old call sites/tests don't break.
- If you change a grouping key (e.g. fingerprint), keep the return TYPE stable (string) so `_config_stats` keying and JSONL export stay compatible.

### Step 3 — API: extend every return branch
Add the new key to each `return {...}` in the endpoint. Missing it in the fallback/empty branch is a common source of frontend `undefined`.

### Step 4 — Frontend: markup + renderer + wiring
- Add the table section in `index.html` with a unique `tbody` id and a sensible empty-state row (`colspan` = column count).
- In `app.js`, add `update*Table(rows)` that guards `if (!rows || rows.length === 0)`, builds `<tr>` per row, and null-safes optional numeric fields (e.g. accuracy may be `null`).
- Wire it into the EXISTING fetch handler (`if (data.<key>) update*Table(data.<key>);`) — do not add a second fetch.

### Step 5 — Verify helpers exist before using them
`grep "function formatNumber|function formatMs|function getSuccessClass" web_ui/*.js`. Reuse existing formatters; don't reinvent. Color-coding via an existing class helper (e.g. `getSuccessClass`) keeps style consistent.

### Step 6 — Tests + full suite
- Extend the collector's unit test file: one test per new getter (happy path + empty/null edge). If you changed a grouping key, add a "same X → same fingerprint; different X → different" test.
- Run the targeted file first (`python -m pytest tests/test_telemetry.py -q`), then the full suite.

## Tips / pitfalls

- **Orphaned constants:** changing a grouping key can orphan a settings constant or helper (e.g. `SYSTEM_PROMPT_HASH_MAX_CHARS`, `_normalize_system_prompt`). `grep` the whole repo for references; if zero remain, remove it — but confirm via git history that THIS change orphaned it, not a prior one.
- **Dirty working tree:** this repo often has unrelated uncommitted changes (windows.py, streaming.py, etc.). Review with `git diff <specific files>` scoped to your task, and commit only your files. Don't assume the whole diff is yours.
- **Windows shell:** pipes like `| tail` are auto-rejected by shell_cmd — run the base command; output spillover handles truncation.
- **Don't let the coder self-review be the only review.** Independently re-read the diff, confirm helpers exist, and run the suite yourself before marking done.
- Keep the change additive: optional params, stable return shapes, no breaking existing JSONL schema.
