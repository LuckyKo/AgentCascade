# UI Settings Export/Import Chain Audit

**Date:** 2026-08-12
**Scope:** UI settings panel toggle round-trip through export → save → load → import
**Files reviewed:**
- `config/agent-cascade-settings-2026-08-12T04-50-32.json` (export sample, 61 keys)
- `agent_cascade/config_handlers.py` (POOL_SETTINGS_KEYS, EXTRA_PERSIST_KEYS, CONFIG_HANDLERS registry)
- `agent_cascade/ws_handlers.py` (handle_update_config / handle_export_settings / handle_import_settings / handle_set_auto_security)
- `agent_cascade/agent_pool.py` (_save_pool_settings / _load_pool_settings / _apply_pending_config)
- `agent_cascade/agent_instance.py` (PoolSettings dataclass + to_dict/from_dict)
- `agent_cascade/api_integration.py` (UI key mapping), `agent_cascade/constants.py`, `agent_cascade/api_router.py`

> **NOTE (concurrent fixes):** During this audit, parallel workstream `config_cleanup_fixes` (coder) independently applied two of the findings below. Verify against current code before acting:
> - **F2 FIXED** — `max_images_for_llm` added to `_save_pool_settings()` tuple (agent_pool.py:417-419).
> - **F7 FIXED** — `compression_proactive_threshold` + `compression_context_reserve_tokens` moved from EXTRA_PERSIST_KEYS → POOL_SETTINGS_KEYS (config_handlers.py:65).
> F1, F3, F4 remain open as of this report.

---

## Executive Summary

The export/import chain is **mostly sound**: 59 of 61 exported keys are covered by the import allow-list (`POOL_SETTINGS_KEYS | EXTRA_PERSIST_KEYS`), every POOL_SETTINGS_KEYS key has a handler, and the per-agent-class `disabled_tools` dict format, `compression_fraction`, work folders, and `default_workspace` all round-trip correctly.

**Two real round-trip bugs found:**
1. `max_workers` is exported but **not importable** (UI/backend key-name mismatch with `max_parallel_agents`) → parallel-agent setting lost on import.
2. `max_images_for_llm` is handler-wired and load-wired but was **never written to disk** by `_save_pool_settings` → lost on restart, never exported. *(FIXED concurrently — see note above.)*

Plus two cleanup items: deprecated `sleeping_timeout` is persisted/exported but dropped on import (inconsistent), and `auto_security` has no config handler (relies on special-casing).

---

## Key Findings

### F1 — ROUND-TRIP BREAK: `max_workers` vs `max_parallel_agents` (High, still open)
- UI key is `max_parallel_agents` (api_integration.py:842 maps `settings.max_workers` → `max_parallel_agents`); handler `_handle_max_parallel_agents` (config_handlers.py:257) writes `agent_pool.settings.max_workers`.
- Persistence serializes the **dataclass field name** `max_workers` via `PoolSettings.to_dict()` → saved to pool_settings.json → included in export (sample JSON line 15: `"max_workers": 3`).
- Import allow-list = `POOL_SETTINGS_KEYS | EXTRA_PERSIST_KEYS` (ws_handlers.py:842); **`max_workers` is in neither set** (only `max_parallel_agents` is, config_handlers.py:31). No handler is registered for `max_workers` either.
- **Result:** exported `max_workers` is silently filtered out on import → the setting does not round-trip. Export emits one name; import accepts another.
- **Fix options:** (a) add `max_workers` to POOL_SETTINGS_KEYS + register an alias handler (same pattern as `work_access_folders_rw` alias); or (b) normalize export to emit `max_parallel_agents`.

### F2 — `max_images_for_llm` NOT SAVED TO DISK — **FIXED** (was High)
- Was in POOL_SETTINGS_KEYS (config_handlers.py:51), handler exists (config_handlers.py:541-552), startup load reads it (agent_pool.py:542-546).
- But `_save_pool_settings` wrote only 6 llm_cfg keys (agent_pool.py:417-418) — `max_images_for_llm` was omitted.
- **Now fixed** by concurrent `config_cleanup_fixes`: added to save tuple (agent_pool.py:417-419). Recommend verifying with a round-trip test.

### F3 — DEPRECATED `sleeping_timeout`: persisted + exported but not importable (Medium, still open)
- Handler marked DEPRECATED (config_handlers.py:701-706); dataclass field marked DEPRECATED (agent_instance.py:688-689).
- Not in either persist-key set → import silently drops it (sample JSON line 17: `"sleeping_timeout": 300` never reapplies).
- However `to_dict()` still serializes it (only 5 Phase-3 loop fields are popped, agent_instance.py:760-763) → saved to disk + exported every time.
- **Result:** inconsistent — present in export file but silently discarded on import.
- **Fix:** remove the dataclass field + handler entirely (feature is dead per sleeping_state_unified_queue.md), or add to persist keys if true backward-compat round-trip is wanted (not recommended).

### F4 — `auto_security` HAS NO CONFIG HANDLER (Medium, still open)
- In EXTRA_PERSIST_KEYS (config_handlers.py:81); no `@register_config_handler('auto_security')` exists (only pool|extra key without a handler).
- It works through special-casing instead: import sets `app.current_auto_security` directly (ws_handlers.py:861-867); startup load stores `_loaded_auto_security` (agent_pool.py:477-478); save/export read it (agent_pool.py:397-398, ws_handlers.py:808-809); the dedicated `set_auto_security` WS message persists it (ws_handlers.py:993-1003).
- **Gap:** if a client sends `auto_security` inside `update_config` (generate_cfg), the router finds no handler → **silent no-op** (a pointless save is triggered, value unchanged). The only working live path is the dedicated `set_auto_security` message.
- **Fix:** register a handler that sets `agent_pool._loaded_auto_security` (and document the app-sync requirement), or explicitly document that auto_security is only settable via `set_auto_security`/import.

### F5 — Coverage verification (Confirmed)
- All JSON keys **except 2** (`max_workers` F1, `sleeping_timeout` F3) are in `POOL_SETTINGS_KEYS | EXTRA_PERSIST_KEYS`.
- All POOL_SETTINGS_KEYS keys have registered handlers (only `auto_security` in EXTRA lacks one — see F4).
- Orphaned handlers (registered, not persisted) — both intentional/known:
  - `mcpServers` (config_handlers.py:100) — runtime MCP config, no persistence by design.
  - 13 `LLM_CONFIG_KEYS` — routed to `api_router.default_llm_cfg`; general LLM settings are **not persisted to disk** (known limitation, cf. lessons_max_input_tokens_flow.md). They are also absent from the export file, so export/import never covers model/API config.

### F6 — Specific toggles round-trip status (Confirmed)
| Key | Export | Import allow-list | Handler | Save | Load | Round-trip |
|---|---|---|---|---|---|---|
| `disabled_tools` (per-agent dict) | ✅ dict keyed by agent name | ✅ EXTRA | ✅ dict-preserving | ✅ dict | ✅ dict | ✅ OK |
| `compression_fraction` | ✅ (percent) | ✅ EXTRA | ✅ %→fraction | ✅ % (round 1dp) | ✅ fraction→% | ✅ OK |
| `work_access_folders_ro/rw` | ✅ (always) | ✅ POOL | ✅ (rw=alias) | ✅ (only if non-empty) | ✅ | ✅ OK (empty = no-op) |
| `default_workspace` | ✅ | ✅ POOL | ✅ | ✅ | ✅ (deferred) | ✅ OK |
| `auto_security` | ✅ | ✅ EXTRA | ❌ none | ✅ | ✅ | ⚠️ via special-case only (F4) |
| `max_parallel_agents` | ❌ (emits `max_workers`) | ✅ POOL | ✅ | ⚠️ as `max_workers` | ✅ as `max_workers` | ❌ BROKEN (F1) |
| `max_images_for_llm` | ❌→✅ | ✅ POOL | ✅ | ❌→✅ (F2 fixed) | ✅ | ✅ after fix |
| `sleeping_timeout` | ✅ | ❌ | ✅ (deprecated) | ✅ | ✅ | ❌ BROKEN (F3) |

Notes on `disabled_tools`: export merges UI overrides **plus backend defaults** (`resolve_disabled_tools_for_agent`), so re-importing may turn implicit defaults into explicit UI entries — semantically harmless (set of disabled tools identical) but not byte-identical to source. Per-agent-class dict format is preserved through all stages.

### F7 — Minor taxonomy inconsistency — **FIXED**
- `compression_proactive_threshold` and `compression_context_reserve_tokens` were PoolSettings dataclass fields (agent_instance.py:676-677) yet lived in EXTRA_PERSIST_KEYS. Moved to POOL_SETTINGS_KEYS (config_handlers.py:65) by concurrent `config_cleanup_fixes`. Functionally equivalent (to_dict persists them), now consistent with the POOL_SETTINGS_KEYS comment.

---

## Cleanup Opportunities
1. **Remove `sleeping_timeout`** — deprecated field + deprecated handler + broken round-trip (F3). Dead feature per comments.
2. ~~**Add `max_images_for_llm` to save tuple**~~ — **DONE** (F2).
3. **Reconcile `max_workers`/`max_parallel_agents`** — alias handler + persist key, or export normalization (F1).
4. **Register `auto_security` handler or document the special-case** (F4).
5. ~~**Consolidate compression thresholds into POOL_SETTINGS_KEYS**~~ — **DONE** (F7).
6. **Add an export→import round-trip test** asserting: all exported keys re-import, `max_parallel_agents` value preserved, `compression_fraction` percent↔fraction stable, `disabled_tools` dict unchanged.

---

## Confidence Level
**High** — every finding verified by direct code reading (config_handlers.py, ws_handlers.py, agent_pool.py, agent_instance.py, api_integration.py) plus programmatic set analysis of the export JSON vs. the key sets (61 JSON keys, 56 POOL + 5 EXTRA, 62 handlers).

## Open Questions
- Was the `max_workers`/`max_parallel_agents` split intentional? No comment indicates it; looks like an oversight from the UI-key rename (`max_workers` → `max_parallel_agents`).
- Should LLM general settings (`default_llm_cfg`) be included in export/import? Currently not persisted at all (known limitation, out of scope here).

## Recommended Next Actions
1. ~~F2 (one line)~~ — already applied by `config_cleanup_fixes`; verify with test.
2. Fix F1 (alias handler + persist key) — restores parallel-agent round-trip.
3. Resolve F3 (remove or re-enable sleeping_timeout persistence).
4. Address F4 (handler or documentation).
5. Add round-trip regression test.

