---
name: fullstack-data-feature-plumbing
description: Add a new metric/field/table end-to-end in a fullstack app — thread it through backend collector → API endpoint → web UI table, with thread-safety and verification pitfalls.
source: auto-generated
version: "1.0.0"
triggers:
  - "telemetry"
  - "add a table"
  - "new metric"
  - "web ui tab"
  - "api endpoint field"
---

## Goal

Add a new data field/metric/table that must surface from the backend all the way to the web UI, done correctly (thread-safe, verified) with minimal safe changes.

## The data path (know it before touching code)

A new surfaced value travels exactly these four roles:
1. **Collector/backend** — the class that accumulates state and exposes a getter.
2. **Call site** — where the event is recorded; this is where per-instance context is available to thread in.
3. **API** — the route handler for the endpoint. Add the new key to BOTH the success branch and the empty-fallback dict so shape stays stable.
4. **Web UI** — the table markup (a unique `tbody` id) plus a renderer function called from inside the existing frontend `fetch()` handler.

## Procedure

1. **Locate seams with grep, not guessing.** `grep "class .*Collector"` for the collector; `grep "<new concept>"` across the frontend for the render + fetch functions. Read the API route handler fully — note every return branch you must extend.
2. **Backend: accumulate under the lock, expose a SNAPSHOT getter.** If the module has a coarse RLock, mutate shared state ONLY inside that lock. Getter methods build and return a **fresh list/dict snapshot**, not the live internal dicts — callers then iterate without holding the lock. Thread new per-entity dimensions through the record method signature as an optional param (default empty) so old call sites/tests don't break. If you change a grouping key, keep the return TYPE stable so any downstream keying/export stays compatible.
3. **API: extend every return branch.** Add the new key to each `return {...}`. Missing it in the fallback/empty branch is a common source of frontend `undefined`.
4. **Frontend: markup + renderer + wiring.** Add the table section with a unique `tbody` id and a sensible empty-state row (`colspan` = column count). Add a renderer that guards `if (!rows || rows.length === 0)`, builds one `<tr>` per row, and null-safes optional numeric fields (a value may be `null`). Wire it into the EXISTING fetch handler (`if (data.<key>) render(data.<key>);`) — do not add a second fetch.
5. **Verify helpers exist before using them.** Grep the frontend for existing formatters/class helpers; reuse them, don't reinvent. Reusing an existing style helper keeps color-coding consistent.
6. **Tests + full suite.** Extend the collector's unit test file: one test per new getter (happy path + empty/null edge). If you changed a grouping key, add a "same X → same fingerprint; different X → different" test. Run the targeted file first (`python -m pytest <collector_test_file> -q`), then the full suite.

## Tips / pitfalls

- **Orphaned constants:** changing a grouping key can orphan a settings constant or helper function. `grep` the whole repo; if zero references remain, remove it — but confirm via git history that THIS change orphaned it, not a prior one.
- **Dirty working tree:** repos often carry unrelated uncommitted changes from other tasks. Review with `git diff <specific files>` scoped to your task, and commit only your files. Don't assume the whole diff is yours.
- **Windows shell:** pipes like `| tail` are auto-rejected by shell_cmd — run the base command; output spillover handles truncation.
- **Don't let the coder self-review be the only review.** Independently re-read the diff, confirm helpers exist, and run the suite yourself before marking done.
- Keep the change additive: optional params, stable return shapes, no breaking existing JSONL schema.
