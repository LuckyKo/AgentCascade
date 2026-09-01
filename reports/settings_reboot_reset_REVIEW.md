# Code Review — Settings Reboot Reset Fix (todo.md #145)

**Project:** N:\work\WD\AgentCascade · **Reviewer:** settings-reboot-review (independent, did not write the code)
**Implementation report:** `reports/settings_reboot_reset_IMPL.md` · **Investigation:** `reports/settings_reboot_reset_investigation.md`

---

## Round 1 — Verdict: APPROVE-WITH-FIXES

### 🔴 CRITICAL (must fix)

**1. Zero/NaN coercion trap in FIX 3 (`web_ui/app.js` getGenerateCfg, max_auto_rollbacks)**
- `parseInt($('#setting-max-rollbacks').value) || 3` treats `0` as falsy → a user who sets `max_auto_rollbacks=0` (valid: "no rollbacks") gets it silently coerced to `3` after save/reload.
- Required fix: explicit NaN check (`Number.isNaN(val) ? 3 : val`) so both `0` and `-1` are preserved.

### 🟠 MAJOR (should fix)

**2. Incomplete `_isFiniteRestore` coverage in `loadSettings()`**
- Round-1 fix guarded only the 7 LLM-gen sliders + font-size + max_tokens + max-context.
- Many other numeric restores still used bare `s[key] !== undefined` (max-turns, loop-* thresholds, tool-result-max-chars, idle-timeout/system-idle-timeout, max-parallel, retry fields, cache-pool fields, compression fields, grep/shell/code/list_dir limits) → a null-poisoned localStorage blob would still re-hydrate them.
- Required: audit the WHOLE function; guard every numeric restore site (and add a null/undefined skip for non-numeric restores).

**3. Test quality — no RED verification, missing zero-case test**
- No test covered `max_auto_rollbacks=0` preservation (the exact bug in finding 1).
- Required: add the zero-case test; prove tests FAIL when the fix is reverted (RED) and pass with it (GREEN).

### 🟡 MINOR / verified OK

- **FIX 5 omitted-keys diff logic** (`config_handlers.py` `_handle_llm_config`): diffing `new_llm_cfg` against `{k: current.get(k) for k in new_llm_cfg}` is sound when keys are omitted — update still triggers when any present key differs. ✅
- **import_settings cleanup** (app.js ~L2314): iterates `POOL_SETTINGS_MAP` localKeys, so the corrected `max_auto_rollbacks` localKey is now correctly cleared on import. No other code assumed that key was absent from localStorage. ✅
- **AgentPool init order**: `_load_pool_settings()` runs in `AgentPool.__init__` BEFORE the startup_cfg block → file values load first; skipping the save when nothing was overridden is correct. First-ever boot (no file) is fine — defaults live in pool.settings and get persisted on the first UI update_config. ✅
- **Scope discipline**: no changes beyond the 5 fixes. ✅

### Round-1 checklist summary

| Item | Result |
|---|---|
| FIX 1 override-detection (CLI/env/both/neither) + init ordering + first boot | ✅ verified |
| FIX 2 round-trip (raw string storage, guard data, import cleanup) | ⚠️ broken for value `0` (finding 1) |
| FIX 4 restore-guard coverage | ⚠️ incomplete (finding 2) |
| FIX 5 omitted gen params vs server diff logic | ✅ sound |
| Test RED sensitivity + zero-case coverage | ⚠️ missing (finding 3) |

---

## Round 2 — Verdict: ✅ APPROVE

All four round-1 findings re-verified against the actual current code.

### R2-1: Zero-coercion bug fixed ✅
`web_ui/app.js` getGenerateCfg now reads:
```js
if ($('#setting-max-rollbacks')) { const _mar = parseInt($('#setting-max-rollbacks').value); cfg.max_auto_rollbacks = Number.isNaN(_mar) ? 3 : _mar; }
```
- `0` → preserved (Number.isNaN(0) === false) ✅
- `-1` → preserved ✅
- empty → NaN → 3 (HTML default) ✅

### R2-2: Zero-case regression test added and meaningful ✅
`tests/test_settings_reboot_fixes.js` Test 7b: input `"0"` → asserts `cfg.max_auto_rollbacks === 0`. Confirmed it FAILS against the old `|| 3` expression (RED evidence in IMPL report §"Review fixes (round 2)"): `0 (no rollbacks) must be preserved — got 3 (zero-coercion bug)`; 11 pass / 1 fail.

### R2-3: `_isFiniteRestore` / `_present` coverage complete ✅
Full audit of every restore site in `loadSettings()` (L1209–1459):
- **Numeric restores** all use `_isFiniteRestore(v)`: max-turns, max_auto_rollbacks, max_parallel_agents, loop_min_chars/loop_max_chars/loop_char_run_limit/loop_suspicion_threshold/loop_confirm_required/loop_cooldown_feeds, tool-result-max-chars, idle-timeout, system-idle-timeout, retry_* fields, cache_pool_* fields, compression_* fields, grep/shell/code/list_dir char limits, approval-timeout-seconds, max_images_for_llm, and the 7 LLM-gen sliders.
- **Non-numeric restores** (checkboxes/toggles, api_base/api_key/model strings, colors, work folders, default workspace, auto-security, async shell console window) all use `_present(v)` (null/undefined skip).
- No bare `!== undefined` remains on any numeric restore site.

### R2-4: RED/GREEN verification provided and concrete ✅
IMPL report includes actual failing output for both reverts:

| Fix reverted to | RED result |
|---|---|
| FIX 3 → `parseInt(...) \|\| 3` | Test 7b FAILS: `got 3 (zero-coercion bug)`; 11 pass / 1 fail |
| FIX 1 → unconditional startup_cfg apply | `test_no_override_presists_persisted_values` FAILS: `idle_timeout_seconds clobbered: 1600.0 != 900.0`; 2 failed / 1 passed |

### Independent test run (round 2)
- `node --check web_ui/app.js` → OK
- `node --test tests/test_settings_reboot_fixes.js` → **12 pass, 0 fail**
- `node --test tests/test_settings_live_edit.js` → **3 pass, 0 fail**
- `python -m pytest tests/test_settings_reboot_fix.py -v` → **3 pass, 0 fail**

### Final finding table

| Round-1 finding | Status | Evidence |
|---|---|---|
| 🔴 Zero-coercion bug (FIX 3) | ✅ Resolved | Explicit NaN check + Test 7b |
| 🟠 Missing zero-case test | ✅ Resolved | Test 7b, RED-verified |
| 🟠 Incomplete restore guards | ✅ Resolved | Whole-function audit, `_isFiniteRestore`/`_present` everywhere |
| 🟠 No RED verification | ✅ Resolved | Concrete failing output for both fixes |

**Final verdict: APPROVE — production-ready.**
