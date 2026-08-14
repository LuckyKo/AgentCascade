# UI Config Audit — Verification & Completion Report

**Date:** 2026-08-12 (09:15, updated 09:22)
**Agent:** ui_config_audit_child1 (investigation/evidence)
**Scope:** Verify known issues + answer 4 specific questions about UI settings round-trip
**Method:** Direct code reading + programmatic AST set-analysis + live config file inspection + git diff.

**⚠️ IMPORTANT — Code changed DURING the audit.** The audited source files (`agent_pool.py`, `config_handlers.py`) were modified in the working tree at 09:13-09:14 (uncommitted) while this audit was in progress:
- `agent_pool.py` +1 line: `'max_images_for_llm'` added to the save tuple (fixes F2)
- `config_handlers.py` +1/-2: compression thresholds moved EXTRA → POOL_SETTINGS_KEYS (fixes F7)

This report reflects the **CURRENT working tree** (post-fix). A reviewer agent independently verified the same post-fix state. The earlier snapshot in `audit_reports/ui_settings_export_import_audit_20260812.md` (09:08) reflects the PRE-fix state. **Findings F2 and F7 are now RESOLVED in the working tree but NOT COMMITTED and not yet effective on a running server** (pool_settings.json last written 08:12, before the fix).

---

## Answers to the 4 specific questions

### Q1 — Is `sleeping_timeout` still needed? (Drop or keep for backward compat?)

**Verdict: DROP — it is dead code, and keeping it actively breaks round-trip consistency. STILL UNRESOLVED.**

Evidence:
- **Not consumed anywhere**: `sleeping_timeout` appears in exactly 3 places — the dataclass field (agent_instance.py:689, marked `# DEPRECATED (2026-08): sleeping_timeout is no longer used`), the handler (config_handlers.py:701-706, marked DEPRECATED), and the settings constant (settings.py:131-134, marked DEPRECATED + "now unused"). The execution engine no longer reads it.
- **Present but not importable**: NOT in POOL_SETTINGS_KEYS or EXTRA_PERSIST_KEYS (set-analysis: pool=False extra=False), yet `PoolSettings.to_dict()` still serializes it → live `pool_settings.json:17` and export sample both contain `"sleeping_timeout": 300`.
- **Consequence**: Export emits it → import filters it out (`known_keys = POOL_SETTINGS_KEYS | EXTRA_PERSIST_KEYS`, ws_handlers.py:842-843) → persisted/exported but never reapplies. Round-trip inconsistency; the handler also writes a field nothing reads.
- Backward-compat argument is weak: import already ignores unknown keys, so old export files import fine either way.

**Recommendation:** Remove the dataclass field (agent_instance.py:689) + handler (config_handlers.py:701-706) + constant (settings.py:131-134). Alternative (not recommended): add to EXTRA_PERSIST_KEYS so import doesn't discard it — but that keeps dead weight.

---

### Q2 — Any POOL_SETTINGS_KEYS with NO registered handler?

**Verdict: NONE. All 58 POOL_SETTINGS_KEYS have handlers. Confirmed by AST set-analysis (current tree).**

- POOL_SETTINGS_KEYS = **58** keys (was 56 pre-fix; +2 compression thresholds moved from EXTRA). CONFIG_HANDLERS = 62 registrations.
- `POOL_SETTINGS_KEYS − CONFIG_HANDLERS = ∅` (empty).
- EXTRA_PERSIST_KEYS (3 keys now, was 5) minus handlers = `{'auto_security'}` — the single key without a handler, a **known intentional special-case** (see Q6: handled via `set_auto_security` WS message, inline import code at ws_handlers.py:861-867, `_loaded_auto_security` load/save).
- Orphaned handlers (registered, not in either persist set): `mcpServers` (runtime MCP, no persistence by design) + `sleeping_timeout` (deprecated, see Q1) + 13 LLM_CONFIG_KEYS (routed to `api_router.default_llm_cfg`, not disk-persisted — known limitation).

**Caveat on auto_security**: router's `apply()` (config_handlers.py:807-814) silently no-ops for keys without handlers → sending `auto_security` inside `update_config`/generate_cfg is a silent no-op. Only working live paths: dedicated `set_auto_security` message and import.

---

### Q3 — Does `enable_async_shell_console_window` load properly on startup?

**Verdict: YES — verified correct (UNCHANGED by mid-audit edits).**

- Default `True` set at agent_pool.py:265 — **before** `_load_pool_settings()` at line 275. Ordering correct (prior bug `[[async_shell_console_toggle_off_restart_bug]]` — default overwriting persisted `false` — is fixed; no unconditional re-assignment remains).
- Load: `_load_pool_settings()` pops at agent_pool.py:455, applies at 490-491: `self._enable_async_shell_console_window = bool(raw)`.
- Only **two assignments** in agent_pool.py (grep: lines 265 + 491) — no later reset.
- Live proof: `config/pool_settings.json:120` = `"enable_async_shell_console_window": false` — persisted OFF survives restart.
- Save: agent_pool.py:409. Broadcast: api_integration.py:761. Consumer: shell_cmd.py:254-255. In POOL_SETTINGS_KEYS (config_handlers.py:55); handler config_handlers.py:246-252.

**Full round-trip: OK.**

---

### Q4 — Is `max_images_for_llm` persisted anywhere?

**Verdict: FIXED in working tree at 09:13 (uncommitted) — was a confirmed bug at audit start.**

- **PRE-fix state** (committed HEAD, captured at 09:10): `_save_pool_settings()` llm_cfg loop had only 6 keys (`tool_result_max_chars, grep_char_limit, grep_spillover, shell_char_limit, code_char_limit, list_dir_char_limit`) — `max_images_for_llm` omitted → never saved to disk, lost on restart, never exported. Live pool_settings.json (written 08:12) confirms absence.
- **CURRENT state** (working tree): agent_pool.py:417-419 tuple now has **7 keys including `'max_images_for_llm'`** (verified by direct read + regex extraction). Fix takes effect on the next `_save_pool_settings()` call.
- Load path (unchanged, correct): agent_pool.py:542-546 restores into `llm_cfg`. Handler (unchanged): config_handlers.py:541-550.
- **Caveat**: fix is UNCOMMITTED; a running server started before 09:13 still has old code. `pool_settings.json` will only contain `max_images_for_llm` after the next save with the new code.

---

## Verification of the known issues from the task brief

1. **`compression_proactive_threshold` / `compression_context_reserve_tokens` in EXTRA_PERSIST_KEYS (redundant but harmless)** — **NOW FIXED** at 09:14 (uncommitted): moved from EXTRA_PERSIST_KEYS (3 keys remain: disabled_tools, auto_security, compression_fraction) into POOL_SETTINGS_KEYS (58 keys). Functionally equivalent (to_dict persisted them either way; both sets are in the import allow-list), but taxonomy is now consistent with the "PoolSettings fields" comment.

2. **`auto_security` stored in `_loaded_auto_security`, saved 397-398, loaded 477-478 — OK** — CONFIRMED (line numbers unchanged by mid-audit edits).
   - Save: agent_pool.py:397-398. Load: agent_pool.py:452 pop + 477-478. Export: ws_handlers.py:809 (`app.current_auto_security`). Import: ws_handlers.py:862-867 inline.
   - In EXTRA_PERSIST_KEYS (config_handlers.py:80); **no config handler** (see Q2/Q6). Live file: pool_settings.json:117 `"auto_security": true`.

---

## Export Settings path — full inclusion check

Export handler `handle_export_settings` (ws_handlers.py:740-817):
1. Disk contents of pool_settings.json (756-763)
2. Merged with `agent_pool.settings.to_dict()` (766-767) — all live dataclass fields
3. Effective `disabled_tools` per template (772-799)
4. Work folders + default workspace (802-806)
5. `auto_security` from app state (809)

**Still broken in export round-trip:**
| Key | In export? | Importable? | Issue |
|---|---|---|---|
| `max_workers` | ✅ (via to_dict) | ❌ | UI key `max_parallel_agents`; export emits dataclass name `max_workers`; not in import allow-list, no handler → silently dropped (F1 — STILL UNFIXED) |
| `sleeping_timeout` | ✅ | ❌ | Deprecated dead field (Q1) — exported but dropped on import (F3 — STILL UNFIXED) |

**Now fixed (mid-audit):**
| Key | Status |
|---|---|
| `max_images_for_llm` | ✅ now saved to disk (F2 fixed) → will appear in export after next save |
| compression thresholds taxonomy | ✅ moved to POOL_SETTINGS_KEYS (F7 fixed) |

**Still NOT exported (known limitation, out of scope):** LLM general settings (model, api_base, temperature, etc.) — 13 LLM_CONFIG_KEYS → `api_router.default_llm_cfg`, never disk-persisted.

---

## Findings Summary (current working tree)

| # | Finding | Severity | Status |
|---|---|---|---|
| F1 | `max_workers` exported but not importable (`max_parallel_agents` mismatch) | High | **UNFIXED** (no working-tree change) |
| F2 | `max_images_for_llm` never saved to disk | High | **FIXED 09:13** — uncommitted; needs commit + server restart/save to take effect |
| F3 | `sleeping_timeout` deprecated dead field, persisted+exported, dropped on import | Medium | **UNFIXED** — recommend removal |
| F4 | `auto_security` has no config handler (special-case only; silent no-op via update_config) | Medium | **UNFIXED** — by design but undocumented |
| F5 | All 58 POOL_SETTINGS_KEYS have handlers | — | ✅ OK |
| F6 | `enable_async_shell_console_window` full round-trip | — | ✅ OK |
| F7 | Compression thresholds in EXTRA vs POOL taxonomy | Low | **FIXED 09:14** — uncommitted |

**Mid-audit code changes detected & documented**: `git diff` confirms exactly the F2 + F7 fixes; no other modifications. Both files show `M` (uncommitted); file mtimes 09:13 (agent_pool.py) / 09:14 (config_handlers.py). Live pool_settings.json mtime 08:12 (pre-fix write).

## Confidence Level
**High for current state** — every claim verified by direct code reading of the working tree, AST set-analysis (58 POOL / 3 EXTRA / 62 handlers), regex extraction of the save tuple, git diff, and live config inspection. The mid-audit race was resolved by re-reading after the reviewer flagged the discrepancy; both pre-fix and post-fix observations are documented in this report.

## Open Questions
- Was the `max_workers`/`max_parallel_agents` split intentional? No comment suggests so; looks like a UI-key rename oversight.
- Should LLM general settings be included in export/import? Currently not persisted at all (known limitation).
- Who applied the mid-audit fixes? Uncommitted; likely another agent session working in parallel under Maine's orchestration.

## Recommended Next Actions (priority order)
1. **Commit the two working-tree fixes (F2 + F7)** — verified correct but uncommitted; a restart/re-deploy would lose them.
2. **Fix F1** (still open) — add `max_workers` to POOL_SETTINGS_KEYS + alias handler → `settings.max_workers` (mirror `work_access_folders_rw` alias), OR normalize export to emit `max_parallel_agents`.
3. **Resolve F3** — remove deprecated `sleeping_timeout` (field, handler, constant).
4. **Address F4** — register `auto_security` handler or document special-case.
5. **Add round-trip regression test** — assert all exported keys re-import, `max_parallel_agents` preserved, `max_images_for_llm` survives restart, compression thresholds survive restart, `enable_async_shell_console_window=false` survives restart, `disabled_tools` dict unchanged.

## Cross-references
- Prior full audit (pre-fix snapshot): `audit_reports/ui_settings_export_import_audit_20260812.md` (09:08).
- Lessons: `[[ui-settings-export-import-roundtrip-gaps]]` (updated with status), `[[tool_char_limits_not_persisted]]`, `[[async_shell_console_toggle_off_restart_bug]]`, `[[sleeping_state_unified_queue]]`.