---
name: fullstack-data-feature-plumbing
description: Add or rename a data field / metric / UI setting end-to-end in a fullstack app — thread it through backend collector → settings pipeline → API → web UI, with the seams that break silently if missed and verification pitfalls.
source: auto-generated
version: 2.1.0
triggers:
  - "add a setting"
  - "rename a config key"
  - "settings pipeline"
  - "new metric to UI"
  - "plumb through backend and frontend"
  - "field not showing up in UI"
  - "undefined in the frontend"
generated_by: coder
generated_from_task: "Make hint cooldown a UI setting + rename memory_hint_max_chars→max_entries across full settings pipeline; extended after the llama-autoloader Strata bridge-card build; extended after the save_state_max_keep retention-setting build"
---

## Goal

Add or rename a data field/metric/UI-setting that must surface from the backend all the way to the web UI, done correctly (thread-safe, verified) with minimal safe changes — and catch the seams that break *silently* if you miss one.

## The data path (know it before touching code)

A new surfaced value travels these roles:
1. **Collector/backend** — the class that accumulates state and exposes a getter.
2. **Call site** — where the event is recorded; per-instance context available to thread in.
3. **API** — the route handler. Add the new key to BOTH the success branch and the empty-fallback dict so shape stays stable.
4. **Web UI** — table markup (unique `tbody` id) + a renderer called from inside the existing frontend `fetch()` handler.

### The SETTINGS-PIPELINE variant (add/rename a config key)
A setting is NOT just collector→API→UI. It must be wired through **SIX seams**, and it spans BOTH Python and JS/HTML. Missing any one breaks silently (value never reaches the consumer, or the UI control has no backing key). For AgentCascade-style apps the six are:
1. `constants.py` — the broadcast tuple (e.g. `POOL_SETTINGS_TO_BROADCAST`).
2. `config_handlers.py` — the persistence key list **AND** a `@register_config_handler('<key>')` fn with a sane clamp + default.
3. `pool/config_persist.py` — the save list **AND** the restore `data.pop(...)` block (each on its own line).
4. `api_server.py` — the `initial_llm_cfg` seed defaults.
5. `web_ui/app.js` — FOUR sites: settings registry, `saveSettings`, restore block, and the cfg-build (`getGenerateCfg`). All four must use the same DOM id + key.
6. `web_ui/index.html` — the `<input>` with a matching `id`.

For a RENAME, treat it as remove-old + add-new in every seam; then grep for the old key to prove zero remain (see gotcha below).

### The GENERIC-APP variant (FastAPI + static WebUI)
Non-AgentCascade stacks have a **different, and more dangerous, seam set**. For a repo shaped like
`server.py` (FastAPI, one big module) + `static/app.js` + `static/index.html`, a new per-item
field the UI reads must pass **FOUR** places:

1. **Dataclass / pydantic field** on the item config.
2. **Manager-level serializer** — e.g. `ModelManager.list_models()` builds a per-item dict.
3. **HTTP route projection** — e.g. `GET /v1/models` re-projects every field **key-by-key**.
4. **Frontend field whitelist** — vanilla-JS apps often have an explicit mapping function
   (`refreshAll()`, `loadModels()`) that copies API fields into `modelsCache`.

### ⚠️ THE #1 TRAP: two parallel serializers + a whitelist = three places to miss
Seams 2 and 3 are **duplicated logic**. A plan or brief that says "these fields are already
returned" is almost always reading seam 2 (the manager method) while the UI consumes seam 3 (the
HTTP projection). Add the field to seam 2 only and the UI gets `undefined` — silently, with no
error, because a missing JSON key is not a failure in JS.

Then seam 4 bites independently: even a correct API response yields `undefined` if the frontend
copies an explicit list of fields into its cache.

**Verification one-liner** (run before declaring done):
```bash
grep -rn "<field_name>" --include=*.py --include=*.js
```
Expect **≥4 hits** (dataclass, manager serializer, HTTP handler, JS whitelist) for a UI-visible
field. Fewer ⇒ you missed a seam. Note that a legitimate multi-hit-per-file result is normal —
count *distinct* sites, not lines.

A plan saying "don't refactor the serializer" is usually about the **write** path's response body
(`return {"id": mid}`), NOT about the read projection. Do not let it block adding read keys.

## Procedure

1. **Locate seams with grep, not guessing.** Grep the concept name across `.py`, `.js`, `.html` separately (see grep gotcha). Read the API route handler fully — note every return branch you must extend. Then find the *manager-level* serializer too — it is a separate copy of the field list.
2. **Backend: accumulate under the lock, expose a SNAPSHOT getter.** Mutate shared state ONLY inside the module's coarse RLock. Getter returns a fresh list/dict snapshot. Thread new per-entity dimensions through as optional params (default empty) so old call sites/tests don't break. Keep return TYPE stable.
3. **API: extend every return branch.** Missing the fallback/empty branch is a common source of frontend `undefined`.
4. **Frontend: markup + renderer + wiring.** Unique `tbody` id, sensible empty-state row (`colspan` = column count), a renderer that guards `if (!rows || rows.length === 0)` and null-safes optional numerics. Wire into the EXISTING fetch handler — do not add a second fetch.
5. **Settings pipeline: edit all six seams with IDENTICAL names.** After editing, grep each extension for the NEW key (should appear in every seam) AND the OLD key on renames (should be zero). Generic-app stacks use the FOUR-seam variant above instead.
6. **Verify helpers exist before using them.** Grep the frontend for existing formatters/class helpers; reuse, don't reinvent.
7. **Tests + full suite.** One test per new getter (happy + empty/null edge). For a "top-N by score" cap: assert exactly N of N+1 survive with the last excluded. Run the targeted file first, then the full suite.

## Byte-identical-UI constraint (generic-app stacks only)

Many vanilla-JS apps promise "card X renders byte-identically to before". Additions are only safe
if they are **provably inert** for the legacy path. Checklist:
- Any new CSS rule is scoped to a class/attr only ever added on the NEW path (`.strata [data-x]`),
  or defaults to the legacy rendering (`[data-new-only]{display:none}`).
- Any new branch in an existing template is a **ternary defaulting to the old expression**, not an
  `if` that rebuilds the block — an `esc()` wrapped around an already-plain value is a no-op; a
  `querySelector(...).remove()` on the new path is NOT.
- New inputs/fields that the legacy form would also submit must be **stripped on submit**, or the
  backend silently persists junk (e.g. a `ctx_size` written onto a non-llama card).
- **Repeat-render safety:** an in-place update path (`updateFoo`) that queries and removes a node
  will throw on the *second* render, because the node was already removed during initial creation.
  Reuse the already-fetched element reference and null-guard it; do not re-query.

## Procedure — testing gotchas for generic-app stacks

- **Existing tests may assert the OLD shape.** When a persistence layer intentionally gains a key,
  a test asserting `data == {...exact dict...}` fails by design. Update it deliberately and note
  the change in your report — do not quietly delete the assertion.
- **When a status code is the thing under test, call the endpoint coroutine directly** rather than
  driving `TestClient`. `await server.my_ep(Body(...))` + `pytest.raises(HTTPException)` asserts
  the status code with zero portal-loop risk, and needs only `monkeypatch.setattr(server, "manager", mgr)`
  to close over the module global.
- **Confirm the canonical test command before trusting a run.** Bare `pytest` may collect ad-hoc
  scripts that make live HTTP calls at import time and abort collection — which prints an
  error and reports **0 tests run**, easily mistaken for a broken feature. Ask for or grep
  `pytest.ini` (`testpaths`/`norecursedirs`) and the root `conftest.py` `collect_ignore`.
- Record the baseline pass count **before** editing, and compare counts after — not exit codes.
- **Quoted baselines go stale.** A plan's "baseline is N passed" may predate other merged test
  work. If your post-change count ≠ N + your-new-tests, run `pytest --collect-only -q` to find
  the true pre-change count, and report the discrepancy instead of "fixing" tests to match the
  stale number.

## Tips / pitfalls

- **Combined multi-extension grep glob is unreliable here.** `grep ... include="*.py,*.js,*.html"` can return "No matches found" even when matches exist (it silently treats the comma list as one bad pattern). Grep each extension SEPARATELY (`include="*.py"`, then `"*.js"`, then `"*.html"`) — or verify a known-present key first to confirm the glob is working before trusting a "no matches" result.
- **Rename = breaking change for persisted state.** An old persisted key isn't auto-migrated; it falls back to the default on next load. That's usually acceptable, but flag it in your summary so the reviewer/orchestrator can decide on a one-time migration.
- **Orphaned constants:** changing a grouping key can orphan a settings constant or helper. Grep the whole repo; if zero references remain, confirm via git history that THIS change orphaned it before removing.
- **Dirty working tree:** repos often carry unrelated uncommitted changes. Review with `git diff <specific files>` scoped to your task; commit only your files. To isolate EXACTLY your lines from pre-existing noise, diff the tool's pre-edit backup against the current file — `edit_file` auto-saves a `.bak` before each edit, so `diff <pre-edit backup> <current file>` shows only your delta (useful when a running service rewrites a config file mid-session, e.g. a server's `yaml.dump` persistence stripping comments and re-sorting keys). Flag the pre-existing noise explicitly so the committer decides what to include.
- **Windows shell:** pipes like `| tail` are auto-rejected by shell_cmd — run the base command; output spillover handles truncation. Run pytest from the HOST shell, not code_interpreter (see [[pytest-docker-subprocess-hang]]).
- **Escaping user-controlled values interpolated into `innerHTML` templates is mandatory**, especially file paths and config names newly exposed to the UI. Vanilla-JS `innerHTML` card templates are XSS sinks; wrap in the project's existing `esc()` helper. If a card needs branching, compute the value in a local (and `esc()` it once at the interpolation) rather than adding a second raw `${}` site.
- **Exceptions raised inside a background task need a re-raise clause.** If a worker function raises `HTTPException` (e.g. a precondition check) and its caller has a broad `except Exception: log` , the status code is downgraded to a 500. Add `except HTTPException: raise` *before* the broad handler.
- **Don't let the coder self-review be the only review.** Independently re-read the diff, confirm helpers exist, and run the suite yourself before marking done; delegate an independent reviewer for a final PASS.
- Keep the change additive: optional params, stable return shapes, no breaking existing JSONL schema.
