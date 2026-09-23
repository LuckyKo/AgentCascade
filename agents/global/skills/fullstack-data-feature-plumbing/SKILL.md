---
name: fullstack-data-feature-plumbing
description: Add or rename a data field / metric / UI setting end-to-end in a fullstack app — thread it through backend collector → settings pipeline → API → web UI, with the seams that break silently if missed and verification pitfalls.
source: auto-generated
version: 1.0.1
triggers:
  - "add a setting"
  - "rename a config key"
  - "settings pipeline"
  - "new metric to UI"
  - "plumb through backend and frontend"
generated_by: coder
generated_from_task: "Make hint cooldown a UI setting + rename memory_hint_max_chars→max_entries across full settings pipeline"
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

## Procedure

1. **Locate seams with grep, not guessing.** Grep the concept name across `.py`, `.js`, `.html` separately (see grep gotcha). Read the API route handler fully — note every return branch you must extend.
2. **Backend: accumulate under the lock, expose a SNAPSHOT getter.** Mutate shared state ONLY inside the module's coarse RLock. Getter returns a fresh list/dict snapshot. Thread new per-entity dimensions through as optional params (default empty) so old call sites/tests don't break. Keep return TYPE stable.
3. **API: extend every return branch.** Missing the fallback/empty branch is a common source of frontend `undefined`.
4. **Frontend: markup + renderer + wiring.** Unique `tbody` id, sensible empty-state row (`colspan` = column count), a renderer that guards `if (!rows || rows.length === 0)` and null-safes optional numerics. Wire into the EXISTING fetch handler — do not add a second fetch.
5. **Settings pipeline: edit all six seams with IDENTICAL names.** After editing, grep each extension for the NEW key (should appear in every seam) AND the OLD key on renames (should be zero).
6. **Verify helpers exist before using them.** Grep the frontend for existing formatters/class helpers; reuse, don't reinvent.
7. **Tests + full suite.** One test per new getter (happy + empty/null edge). For a "top-N by score" cap: assert exactly N of N+1 survive with the last excluded. Run the targeted file first, then the full suite.

## Tips / pitfalls

- **Combined multi-extension grep glob is unreliable here.** `grep ... include="*.py,*.js,*.html"` can return "No matches found" even when matches exist (it silently treats the comma list as one bad pattern). Grep each extension SEPARATELY (`include="*.py"`, then `"*.js"`, then `"*.html"`) — or verify a known-present key first to confirm the glob is working before trusting a "no matches" result.
- **Rename = breaking change for persisted state.** An old persisted key isn't auto-migrated; it falls back to the default on next load. That's usually acceptable, but flag it in your summary so the reviewer/orchestrator can decide on a one-time migration.
- **Orphaned constants:** changing a grouping key can orphan a settings constant or helper. Grep the whole repo; if zero references remain, confirm via git history that THIS change orphaned it before removing.
- **Dirty working tree:** repos often carry unrelated uncommitted changes. Review with `git diff <specific files>` scoped to your task; commit only your files.
- **Windows shell:** pipes like `| tail` are auto-rejected by shell_cmd — run the base command; output spillover handles truncation. Run pytest from the HOST shell, not code_interpreter (see [[pytest-docker-subprocess-hang]]).
- **Don't let the coder self-review be the only review.** Independently re-read the diff, confirm helpers exist, and run the suite yourself before marking done; delegate an independent reviewer for a final PASS.
- Keep the change additive: optional params, stable return shapes, no breaking existing JSONL schema.
