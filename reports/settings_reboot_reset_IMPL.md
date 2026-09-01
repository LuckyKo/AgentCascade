# Implementation Report — "UI settings reset to weird non-default values after PC reboot" (todo.md #145)

**Date:** 2026-09-01 · **Author:** settings-reboot-fix (coder agent)
**Investigation:** `reports/settings_reboot_reset_investigation.md`
**Scope:** Minimal, surgical fix of three confirmed defects + two hardening items. No refactors, no renames, no unrelated comment cleanup.

## Summary of changes

| # | File | Change | Type |
|---|------|--------|------|
| FIX 1 | `agent_cascade/api_server.py` L1390–1418 | Boot-time clobber guard: apply + persist idle-timeout overrides **only** when explicitly given (CLI arg not None or env var present) | server |
| FIX 2a | `web_ui/app.js` L143 | `POOL_SETTINGS_MAP` entry for `#setting-max-rollbacks`: `localKey` `'max-rollbacks'` → `'max_auto_rollbacks'` | frontend |
| FIX 2b | `web_ui/app.js` L1137–1138 | `saveSettings()`: store raw `#setting-max-rollbacks` value under `s['max_auto_rollbacks']` so the respect-local guard has data | frontend |
| FIX 3 | `web_ui/app.js` L5353 | `getGenerateCfg()`: `parseInt(...) \|\| 3` for `max_auto_rollbacks` (empty → 3, not NaN; `-1` preserved) | frontend |
| FIX 4 | `web_ui/app.js` L1215–1242 | `loadSettings()`: `_isFiniteRestore()` guard on the 7 LLM-gen sliders + `setting-font-size` / `max_tokens` / `setting-max-context` direct restores (skip null-poisoned values) | frontend hardening |
| FIX 5 | `web_ui/app.js` L5320–5328 | `getGenerateCfg()`: assign the 7 LLM-gen params only when `Number.isFinite(parsed)` (prevents NaN→null) | frontend hardening |

**Tests added:**
- `tests/test_settings_reboot_fixes.js` — 11 Node tests (new file, follows `test_settings_live_edit.js` regex+vm pattern).
- `tests/test_settings_reboot_fix.py` — 3 pytest tests for FIX 1 (real-boot-path via `runpy`).

---

## FIX 1 — server-side boot clobber (`agent_cascade/api_server.py`)

**Root cause (R1):** On every `create_app`/`__main__` start, the startup block resolved idle-timeout values from CLI > env > hardcoded defaults and applied them to `pool.settings` **unconditionally**, then called `_save_pool_settings()`. This overwrote the user's persisted `pool_settings.json` with the defaults (1600 / 60 / 60) on every boot.

**Change (L1390–1418):** Replaced the unconditional apply with explicit-override tracking:

```python
startup_cfg = {}
if args.idle_timeout is not None or os.getenv('QWEN_AGENT_IDLE_TIMEOUT') is not None:
    startup_cfg['idle_timeout_seconds'] = idle_timeout
if (args.system_agent_idle_timeout is not None
        or os.getenv('QWEN_AGENT_SYSTEM_AGENT_IDLE_TIMEOUT') is not None):
    startup_cfg['system_agent_idle_timeout_seconds'] = system_idle_timeout
if (args.idle_check_interval is not None
        or os.getenv('QWEN_AGENT_IDLE_CHECK_INTERVAL') is not None):
    startup_cfg['idle_check_interval'] = idle_check_interval

if startup_cfg:
    for key in startup_cfg:
        handler = CONFIG_HANDLERS.get(key)
        if handler:
            try:
                handler(startup_cfg, agent_pool, [])
            except Exception as e:
                logger.warning(f"[INIT] Config update failed for '{key}': {e}")
    if hasattr(agent_pool, '_save_pool_settings'):
        agent_pool._save_pool_settings()  # Persist the explicitly-overridden values
```

**Behavior:**
- **No override** → `startup_cfg` empty → handlers NOT called, `_save_pool_settings()` NOT called. The values loaded from `pool_settings.json` in `AgentPool.__init__` survive boot. ✅
- **CLI or env override present** → that key is applied via its `CONFIG_HANDLERS` handler and persisted. Behavior identical to before for the overridden keys. ✅

The value-resolution lines (L1362–1365) are unchanged — they still resolve CLI > env > default, but the resolved value is now only *used* when an explicit override exists.

**Not touched:** `settings.py` defaults, `state_builder.py`, the guard logic in `syncPoolSettings`. Data-only change on the server side.

---

## FIX 2 — dead localKey guard stomp on `#setting-max-rollbacks` (`web_ui/app.js`)

**Root cause (R3):** The `POOL_SETTINGS_MAP` entry had `localKey: 'max-rollbacks'`, but nothing ever writes that localStorage key. So the respect-local guard in `syncPoolSettings` (`if (localKey && saved[localKey] !== undefined) continue`) was dead → the server value stomped the user's input on every WS tick.

**FIX 2a (L143):**
```js
{ id: '#setting-max-rollbacks', prop: 'value', key: 'max_auto_rollbacks', localKey: 'max_auto_rollbacks' },
```
`localKey` now matches the key that `getGenerateCfg`/`saveSettings` actually write.

**FIX 2b (L1137–1138):** Added a line in `saveSettings()` mirroring the existing `#setting-max-turns` save, storing the **raw string** under the underscore key so the guard has data:
```js
// Mirror #setting-max-turns: store the raw value under the underscore key so the respect-local
// (POOL_SETTINGS_MAP localKey='max_auto_rollbacks') has data to protect against server stomps.
if ($('#setting-max-rollbacks')) s['max_auto_rollbacks'] = $('#setting-max-rollbacks').value;
```

**Consistency note (Advisor item 4):** `loadSettings` L1305 reads `s['max_auto_rollbacks']` and assigns it to the input. Storing the raw string is consistent with how `'max-turns'` is stored at L1135, and `input.value = <string>` is the natural DOM assignment. The guard test passes and restore works (verified by `test_settings_reboot_fixes.js`).

---

## FIX 3 — NaN poisoning of `max_auto_rollbacks` (`web_ui/app.js`)

**Root cause (R2):** Empty `#setting-max-rollbacks` input → `parseInt('')` = NaN → JSON `null` in localStorage AND pushed to the server on connect, where `int(null)` raises.

**Change (L5353):**
```js
if ($('#setting-max-rollbacks')) cfg.max_auto_rollbacks = parseInt($('#setting-max-rollbacks').value) || 3;
```
- Empty input → `NaN || 3` → **3** (matches the HTML default at `index.html` L662 and the `state_builder` broadcast default). ✅
- `-1` (unlimited) → `parseInt('-1') || 3` → **-1** preserved. ✅

---

## FIX 4 — null-poisoned slider restore (`web_ui/app.js`, hardening)

**Root cause:** The 7 LLM-gen sliders are `<input type="range">` with HTML defaults (never empty), but legacy/corrupted localStorage blobs may hold `null` for their element-id keys. `s[id] !== undefined` is **true** for `null` → `input.value = null`.

**Change (L1215–1242):** Added a `_isFiniteRestore` helper and applied it to the ranges loop and the three direct restores:
```js
const _isFiniteRestore = (v) =>
  (typeof v === 'number' && Number.isFinite(v)) ||
  (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)));

ranges.forEach(r => {
  if (r.input && _isFiniteRestore(s[r.input.id])) {
    r.input.value = s[r.input.id];
    r.input.dispatchEvent(new Event('input'));
  }
});

if (settingFontSize && _isFiniteRestore(s['setting-font-size'])) { ... }
if (settingMaxTokens && _isFiniteRestore(s['max_tokens'])) { ... }
if (settingMaxContext && _isFiniteRestore(s['setting-max-context'])) { ... }
```
`null`, `undefined`, empty strings, and non-numeric garbage are all skipped; finite numbers and numeric strings still restore. ✅

---

## FIX 5 — skip NaN for gen params (`web_ui/app.js`, hardening)

**Root cause:** Defense-in-depth — even though the sliders can't be empty, guard against NaN→null being stored/pushed in edge cases.

**Change (L5320–5328):** Each of the 7 LLM-gen params is assigned only when finite:
```js
const _t = parseFloat($('#setting-temperature') ? $('#setting-temperature').value : ''); if (Number.isFinite(_t)) cfg.temperature = _t;
const _tp = parseFloat($('#setting-top-p') ? $('#setting-top-p').value : ''); if (Number.isFinite(_tp)) cfg.top_p = _tp;
const _tk = parseInt($('#setting-top-k') ? $('#setting-top-k').value : '', 10); if (Number.isFinite(_tk)) cfg.top_k = _tk;
const _mp = parseFloat($('#setting-min-p') ? $('#setting-min-p').value : ''); if (Number.isFinite(_mp)) cfg.min_p = _mp;
const _rp = parseFloat($('#setting-repeat-penalty') ? $('#setting-repeat-penalty').value : ''); if (Number.isFinite(_rp)) cfg.repeat_penalty = _rp;
const _pp = parseFloat($('#setting-presence-penalty') ? $('#setting-presence-penalty').value : ''); if (Number.isFinite(_pp)) cfg.presence_penalty = _pp;
const _fp = parseFloat($('#setting-frequency-penalty') ? $('#setting-frequency-penalty').value : ''); if (Number.isFinite(_fp)) cfg.frequency_penalty = _fp;
```
Empty/invalid → key **omitted** from `cfg` (not NaN/null). ✅

---

## Testing

### New Node tests — `tests/test_settings_reboot_fixes.js` (11 tests, all pass)

Follows the existing `test_settings_live_edit.js` pattern: extracts real `POOL_SETTINGS_MAP` / `syncPoolSettings` / `getGenerateCfg` / `saveSettings` / `loadSettings` from `web_ui/app.js` via regex + runs them in a `vm` sandbox with a stubbed DOM/localStorage.

| Test | Covers |
|------|--------|
| audit: every POOL_SETTINGS_MAP localKey is actually written | **Whole class of dead-guard bugs** — for EVERY entry, asserts its `localKey` is written by `saveSettings` OR `getGenerateCfg` (key writes extracted via regex). The max-rollbacks entry now passes. |
| FIX 2a: #setting-max-rollbacks localKey matches the key getGenerateCfg/saveSettings write | localKey consistency |
| FIX 2b/3: server value does NOT stomp #setting-max-rollbacks when localStorage has max_auto_rollbacks | respect-local guard active |
| FIX 2b/3: server value applies to #setting-max-rollbacks when no local pref exists | server value still applies when no local pref |
| FIX 2b: saveSettings() writes s.max_auto_rollbacks from #setting-max-rollbacks | raw-string storage |
| FIX 3: getGenerateCfg empty #setting-max-rollbacks → max_auto_rollbacks === 3 (not NaN) | default fallback |
| FIX 3: getGenerateCfg #setting-max-rollbacks "-1" → -1 preserved | unlimited mode |
| FIX 5: getGenerateCfg omits the 7 LLM-gen params when their inputs are empty | no NaN/null in cfg |
| FIX 5: getGenerateCfg assigns finite LLM-gen param values | valid values still assigned |
| FIX 4: loadSettings does not assign null into range sliders / max_tokens / max-context | null-poison skip |
| FIX 4: loadSettings still restores finite numeric values for sliders / max_tokens / max-context | no over-correction |

**Result:** `node --test tests/test_settings_reboot_fixes.js` → **11 pass, 0 fail**.

### Existing Node test — `tests/test_settings_live_edit.js` (3 tests, all pass)

Re-run to confirm no regression from the app.js changes.
**Result:** `node --test tests/test_settings_live_edit.js` → **3 pass, 0 fail**.

### New Python tests — `tests/test_settings_reboot_fix.py` (3 tests, all pass)

Drives the **real** `api_server.py` startup code path (`if __name__ == "__main__"` block) via `runpy.run_path`, with:
- a pre-seeded isolated config dir (`AGENT_CASCADE_TEST_CONFIG_DIR`) holding `pool_settings.json` with user values (900 / 45 / 30) that differ from the hardcoded defaults, and
- `AgentPool` / `OperationManager` patched to lightweight fakes and `uvicorn.run` no-op'd so no real model init / network / blocking occurs.

| Test | Asserts |
|------|---------|
| `test_no_override_presists_persisted_values` | Boot with NO CLI/env overrides → `pool.settings` keeps the file's values (900/45/30, **not** 1600/60/60); `_save_pool_settings()` NOT called; file on disk not re-stamped. |
| `test_cli_override_wins_and_persists` | Boot WITH explicit CLI overrides → the override wins (1234/77/15) and is persisted (`_save_pool_settings` called ≥ 1). |
| `test_env_override_wins_and_persists` | Boot with an explicit env-var override (no CLI) → env value wins for that key (2000); non-overridden keys keep file values; persisted. |

**Result:** `python -m pytest tests/test_settings_reboot_fix.py -v` → **3 pass, 0 fail**.

### Regression check — existing settings-related pytest suites

Ran the three suites that exercise `create_app` / pool settings:
- `tests/test_startup_integration.py`
- `tests/test_unified_system.py`
- `tests/test_api_endpoints.py`

**Result:** **71 passed, 0 failed.** No regressions from the `api_server.py` change.

### Syntax checks

- `node --check web_ui/app.js` → OK
- `python -m py_compile agent_cascade/api_server.py` → OK

---

## Deviations from spec

None of substance. Two minor notes:

1. **FIX 4 also guards `setting-font-size`.** The spec named the 7 LLM-gen sliders + `max_tokens` + `setting-max-context`. I applied the same `_isFiniteRestore` guard to `setting-font-size` (L1229) because it uses the identical direct-restore pattern and is equally vulnerable to a null-poisoned blob. This is a one-line extension of the same fix, not a refactor.

2. **Python test approach.** The spec said "prefer testing the real code path" and noted `create_app` is heavy. I drove the real `__main__` startup block via `runpy` (the actual boot path that contains the FIX 1 logic) rather than calling `create_app` directly, patching only `AgentPool`/`OperationManager`/`uvicorn.run`. This exercises the exact lines changed (L1390–1418) end-to-end.

## Constraints honored

- ✅ Minimal diff; no refactors, renames, or unrelated comment cleanup.
- ✅ Did not change server broadcast logic (`state_builder.py`) or the `syncPoolSettings` guard logic — only the data that feeds it.
- ✅ Did not touch `settings.py` defaults or the guard logic.
- ✅ Syntax checks passed; existing tests re-run with no regression.

---

## Review fixes (round 2)

Review came back **APPROVE-WITH-FIXES**. All findings addressed below.

### R2-1 🔴 CRITICAL — zero-coercion bug in FIX 3 (`web_ui/app.js` L5357)
`cfg.max_auto_rollbacks = parseInt(...) || 3;` clobbered a legitimate `0` (no rollbacks) because `0 || 3 === 3`.
**Fix:** replaced with an explicit NaN check:
```js
if ($('#setting-max-rollbacks')) { const _mar = parseInt($('#setting-max-rollbacks').value); cfg.max_auto_rollbacks = Number.isNaN(_mar) ? 3 : _mar; }
```
`0` and `-1` are now both preserved; only a genuine `NaN` (empty/invalid input) falls back to `3`.

### R2-2 — regression test for the zero case (`tests/test_settings_reboot_fixes.js`)
Added **Test 7b**: `#setting-max-rollbacks` input `"0"` → `getGenerateCfg()` returns `cfg.max_auto_rollbacks === 0` (not 3). This is the RED-verified guard against the zero-coercion regression.

### R2-3 🟠 — extend `_isFiniteRestore` / null-skip guards to ALL loadSettings restore sites (`web_ui/app.js`)
Audited the **entire** `loadSettings()` function and guarded every restore site:
- Added a `_present(v)` helper (L1223): `v !== null && v !== undefined` for non-numeric restores.
- **Numeric restores** now use `_isFiniteRestore(...)`: `max-turns`, `max_auto_rollbacks`, `max_parallel_agents`, `loop-min-chars`, `loop-max-chars`, `loop-char-run-limit`, `loop-suspicion-threshold`, `loop-confirm-required`, `loop-cooldown-feeds`, `tool-result-max-chars`, `idle-timeout`, `system-idle-timeout`, `grep_char_limit`/`grep-char-limit`, `shell_char_limit`/`shell-char-limit`, `code_char_limit`/`code-char-limit`, `list_dir_char_limit`/`list-dir-char-limit`, `setting-max-image-size`, `max_images_for_llm`, `approval-timeout-seconds`, `cache-pool-size`, `cache-threshold-chars`, `retry-max-attempts`, `endpoint-max-retries`, `retry-base-delay`, `retry-max-delay`, `compression-warning/force/proactive-threshold`, `compression-context-reserve-tokens`, `compression-fraction`.
- **Non-numeric restores** (toggles, colors, strings) now use `_present(...)`: lines-enabled, msg-meta, sound-intervention/completed/notification, truncate-tools, auto-tab-focus, user/assistant/raw-edit color, api_base/api_key/model, vision-enabled, auto-continue, enable-skills, auto-skill-mode, auto-skill-gen, inner-loop-detect, enable_agent_budgeting, log-api-post (incl. the legacy-key fallback), auto_rollback_on_loop, loop-char-run-enabled, loop-max-chars-enabled, loop-two-phase-enabled, grep_spillover (bool), setting-image-detail, mcp-enabled, mcp-servers, approval-timeout-enabled, cache-pool-enabled, work-access-folders-rw/ro, default-workspace, afk-enabled, afk-message, auto-security, async-shell-console-window.
- **No keys were changed** — only the assignment guards. `setting-image-detail` (a select) uses `_present` since it's a string value, not a number.

### R2-4 — RED/GREEN verification
Both fixes proven to catch regressions by temporarily reverting and re-running:

| Fix | Reverted to | Result (RED) | Restored (GREEN) |
|-----|-------------|--------------|------------------|
| FIX 3 zero-coercion | `parseInt(...) \|\| 3` | Test 7b **FAILS**: `0 (no rollbacks) must be preserved — got 3 (zero-coercion bug)`; 11 pass / 1 fail | All 12 reboot-fix tests + 3 live-edit = **15 pass** |
| FIX 1 boot clobber | unconditional `startup_cfg` apply (`if True`) | `test_no_override_presists_persisted_values` **FAILS**: `idle_timeout_seconds clobbered: 1600.0 != 900.0`; `test_env_override_wins_and_persists` **FAILS**: `non-overridden system idle was clobbered`; 2 failed / 1 passed | All 3 reboot-fix Python tests **pass** |

### R2 test results (final, all fixes in place)
- `node --check web_ui/app.js` → OK
- `python -m py_compile agent_cascade/api_server.py` → OK
- `node --test tests/test_settings_reboot_fixes.js` → **12 pass, 0 fail** (incl. new zero-case test)
- `node --test tests/test_settings_live_edit.js` → **3 pass, 0 fail** (no regression)
- `python -m pytest tests/test_settings_reboot_fix.py` → **3 pass, 0 fail**

### R2 deviations
None. The only judgment call in R2-3 was classifying `setting-image-detail` as a string restore (`_present`) rather than numeric — it is an `<input type="select">` whose value is a string label, so `_isFiniteRestore` would wrongly reject valid values like `"low"`/`"high"`.
