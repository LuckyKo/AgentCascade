# Investigation: UI settings reset to "weird" non-default values after PC reboot

**todo.md #145** · **Mode: Investigative (root-cause)** · **Confidence: HIGH on mechanism, MEDIUM on Chrome-specificity**

---

## 1. Executive Summary

The "weird, non-default values after reboot" bug is **not a single bug** — it is the compound effect of **three independent defects**, each of which survives a hard refresh (because each is anchored in a persistent store: `pool_settings.json` or `localStorage`):

| # | Defect | Where | Produces "weird non-default value"? | Hard-refresh immune? |
|---|--------|-------|-------------------------------------|----------------------|
| **R1** | **Server boot clobber** — on every `create_app`, CLI/env defaults are applied to `pool.settings` and **persisted to `pool_settings.json`** before any client connects | `api_server.py:1394-1408` | **YES** — `idle_timeout_seconds=1600`, `system_agent_idle_timeout_seconds=60`, `max_turns=250` | **YES** — lives in the server file; hard refresh re-fetches from server |
| **R2** | **NaN/null localStorage poisoning** — `getGenerateCfg()` writes `NaN` (→ JSON `null`) for several numeric fields when their input is empty, with **no `|| default` fallback** | `app.js:5310-5316, 5340` | **YES** — on reload, `null` is restored into the input → displays as `null`/empty, neither user value nor default | **YES** — lives in `localStorage` |
| **R3** | **Dead `localKey` guard stomp** — `#setting-max-rollbacks` is in `POOL_SETTINGS_MAP` but its `localKey='max-rollbacks'` is **never written** by `saveSettings()`/`getGenerateCfg()` | `app.js:143` (map) vs `app.js:1105-1204` (save) | **YES** — server value (3, or 5 from `settings.py`) stomps the DOM on **every WS tick**; a user who set 2 sees it revert | **YES** — server value re-broadcast on each reconnect |

**The on-disk `config/pool_settings.json` is the smoking gun.** It currently contains:

```
idle_timeout_seconds = 1600.0          ← create_app default (api_server.py:1363), NOT UI default 900
system_agent_idle_timeout_seconds = 60.0  ← create_app default (api_server.py:1364), NOT UI default 900
max_turns = 250                        ← DEFAULT_MAX_TURNS (settings.py:27), NOT UI default 50
max_auto_rollbacks = 3                 ← a hand-set value (neither HTML default 3 nor AGENT_MAX_AUTO_ROLLBACKS=5)
```

`1600`/`60` are **exactly** the `create_app` fallbacks, not the HTML defaults (`900`) nor the `shared_init` defaults (`900`). This proves R1 is real and active. The user's `900` idle-timeout preference was **never persisted** because idle-timeout is **not** in `POOL_SETTINGS_MAP` and the `update_config` handler for it is never triggered from the UI (see §5).

**Why the user sees "weird, not the default"**: after reboot, the UI displays a **mix** of (a) localStorage-poisoned `null`s, (b) server-broadcast `1600`/`250` values that differ from the HTML defaults, and (c) a stomped `max-rollbacks`. The combination reads as "not what I set, and not the default."

**Chrome angle**: no Service Worker, no bfcache-sensitive code, and `beforeunload` (app.js:6301-6318) is guarded by `settingsLoaded`. The Chrome-specificity is **most plausibly** explained by **Chrome's bfcache + localStorage persistence interaction** — but see §8, this is the one claim I cannot fully confirm from code alone and needs a live repro.

---

## 2. Evidence Chain

### R1 — Server boot clobber (`api_server.py:1394-1408`)

On **every** `create_app` (i.e., every server start / every reboot):

```python
1394  startup_cfg = {
1395      'idle_timeout_seconds': idle_timeout,          # = 1600.0 (L1363)
1396      'system_agent_idle_timeout_seconds': system_idle_timeout,  # = 60.0 (L1364)
1397      'idle_check_interval': idle_check_interval,    # = 60.0 (L1365)
1398  }
1399  for key, value in startup_cfg.items():
1400      handler = CONFIG_HANDLERS.get(key)
1401      if handler:
1403          handler(startup_cfg, agent_pool, [])       # unconditional — no "if user-set" check
1407  if hasattr(agent_pool, '_save_pool_settings'):
1408      agent_pool._save_pool_settings()               # ← WRITES the clobbered values to disk
```

- `idle_timeout` / `system_idle_timeout` / `idle_check_interval` are resolved at `api_server.py:1363-1365` from CLI > env > **hardcoded defaults 1600 / 60 / 60**.
- `_handle_idle_timeout` (`config_handlers.py:194-205`) does `agent_pool.settings.idle_timeout_seconds = max(0.0, val)` **unconditionally** — it overwrites whatever was in `pool_settings.json` (loaded earlier in `__init__` via `_load_pool_settings`).
- `_save_pool_settings()` (`config_persist.py:14-74`) then serialises `self.settings.to_dict()` + runtime fields to `pool_settings.json`, **overwriting** the user's persisted idle-timeout.

**Net effect after each reboot:** the file's `idle_timeout_seconds`/`system_agent_idle_timeout_seconds` are reset to `1600`/`60` regardless of what the user last saved. The UI's `#setting-idle-timeout` (HTML default `900`, `index.html:434`) is **not** in `POOL_SETTINGS_MAP`, so `syncPoolSettings` never overwrites it — but the **server actually runs with 1600/60**, and the file says so. Any export/inspect of `pool_settings.json` shows the "weird" 1600/60.

`max_turns` is **not** in `startup_cfg`, so it is **not** clobbered by R1; the `250` in the file is `DEFAULT_MAX_TURNS` (`settings.py:27`) baked into `PoolSettings` defaults at construction and persisted when the onopen `update_config` fires with an empty/stale `max_turns` (see R2/R3 interplay).

### R2 — NaN/null localStorage poisoning (`app.js:5310-5316, 5340`)

`getGenerateCfg()` reads the **live DOM** for these LLM-gen fields with **no fallback**:

```js
5310  if ($('#setting-temperature')) cfg.temperature = parseFloat($('#setting-temperature').value);   // '' → NaN
5311  if ($('#setting-top-p'))      cfg.top_p      = parseFloat($('#setting-top-p').value);           // '' → NaN
5312  if ($('#setting-top-k'))      cfg.top_k      = parseInt($('#setting-top-k').value);             // '' → NaN
5313  if ($('#setting-min-p'))      cfg.min_p      = parseFloat($('#setting-min-p').value);           // '' → NaN
5314  if ($('#setting-repeat-penalty')) cfg.repeat_penalty = parseFloat(...);                          // '' → NaN
5316  if ($('#setting-frequency-penalty')) cfg.frequency_penalty = parseFloat(...);                     // '' → NaN
5340  if ($('#setting-max-rollbacks')) cfg.max_auto_rollbacks = parseInt($('#setting-max-rollbacks').value); // '' → NaN
```

Compare with the **safe** ones that have `|| default`: `max_tokens` (5317), `max_input_tokens` (5318), `max_turns` (5320 `|| 50`), `max_parallel_agents` (5322 `|| 3`), etc.

When the input value is empty string, `parseFloat('')` / `parseInt('')` → `NaN`. `JSON.stringify(NaN)` → **`null`**. So `saveSettings()` (app.js:1107 `const s = getGenerateCfg()`) and the **onopen push** (app.js:1657) store `null` for these keys.

On reload, `loadSettings()` restores via the `ranges` loop (app.js:1212-1216):
```js
ranges.forEach(r => { if (r.input && s[r.input.id] !== undefined) { r.input.value = s[r.input.id]; ... } });
```
`s['setting-top-k']` is `null`, and `null !== undefined` is **true**, so `r.input.value = null` → the `<input>` renders `null` / empty → the user sees a "weird value that isn't the default." This is **hard-refresh-immune** (it's in `localStorage`) and is only "fixed" if the user manually re-types the field.

These keys are **stored under the element id** (`setting-temperature`, `setting-top-k`, …) via `ranges.forEach` (app.js:1131-1133), and are **not** in `POOL_SETTINGS_MAP`, so the server never stomps them — the only source is the poisoned localStorage. That's why hard refresh re-shows the poison.

### R3 — Dead `localKey` guard stomp (`#setting-max-rollbacks`)

`POOL_SETTINGS_MAP` (app.js:143):
```js
{ id: '#setting-max-rollbacks', prop: 'value', key: 'max_auto_rollbacks', localKey: 'max-rollbacks' }
```

The respect-local guard (app.js:215):
```js
if (localKey && saved[localKey] !== undefined) continue;   // saved['max-rollbacks']
```

But **nothing ever writes the key `max-rollbacks`**:
- `saveSettings()` does **not** have a line for `#setting-max-rollbacks` (see full audit §4).
- `getGenerateCfg()` writes `cfg.max_auto_rollbacks` (underscore) at 5340 — **not** `max-rollbacks`.
- `loadSettings()` restores `#setting-max-rollbacks` from `s['max_auto_rollbacks']` (app.js:1295) — **not** `max-rollbacks`.

Therefore `saved['max-rollbacks']` is **always `undefined`** → the guard is **dead** → on **every** WS `state`/`stream_update` message, `syncPoolSettings` overwrites `#setting-max-rollbacks` with the server's `max_auto_rollbacks` (currently `3` in the file; `AGENT_MAX_AUTO_ROLLBACKS=5` default at `settings.py:151-152`). A user who set it to `2` sees it silently revert to `3`/`5` on every tick. This is the **exact same class** as the fixed todo #137 (`.agent_lessons/settings-live-edit-stream-stomp.md`) — the code comment at app.js:176-178 even documents this failure mode for the `grep_*` family, but `max-rollbacks` was missed. **Hard-refresh-immune** (server re-broadcasts on each reconnect).

---

## 3. POOL_SETTINGS_MAP audit (dead-guard table)

`localKey` is "alive" only if `saveSettings()` **or** `getGenerateCfg()` writes that exact key into the persisted `s` object.

| id (UI element) | key (server) | localKey | Written by saveSettings/getGenerateCfg? | Guard alive? | Verdict |
|---|---|---|---|---|---|
| `#setting-max-turns` | `max_turns` | `max-turns` | **YES** — saveSettings L1135 `s['max-turns']` | ✅ alive | OK (but server runs 250 — see R1 note) |
| `#setting-max-rollbacks` | `max_auto_rollbacks` | `max-rollbacks` | **NO** — only `max_auto_rollbacks` (5340); `loadSettings` reads `max_auto_rollbacks` (1295) | ❌ **DEAD** | **STOMP** (R3) |
| `#setting-auto-rollback` | `auto_rollback_on_loop` | `auto_rollback_on_loop` | **YES** — getGenerateCfg 5324 | ✅ alive | OK |
| `#setting-agent-budgeting` | `enable_agent_budgeting` | `enable_agent_budgeting` | **YES** — getGenerateCfg 5321 + save L1142 | ✅ alive | OK |
| `#setting-inner-loop-detect` | `inner_loop_detect_enabled` | `inner-loop-detect` | **YES** — save L1140 | ✅ alive | OK |
| `#setting-loop-min-chars` | `loop_min_chars` | `loop-min-chars` | **YES** — save L1159 | ✅ alive | OK |
| `#setting-loop-max-chars` | `loop_max_chars` | `loop-max-chars` | **YES** — save L1160 | ✅ alive | OK |
| `#setting-loop-max-chars-enabled` | `loop_max_chars_enabled` | `loop-max-chars-enabled` | **YES** — save L1163 | ✅ alive | OK |
| `#setting-loop-char-run` | `loop_char_run_enabled` | `loop-char-run-enabled` | **YES** — save L1161 | ✅ alive | OK |
| `#setting-loop-char-run-limit` | `loop_char_run_limit` | `loop-char-run-limit` | **YES** — save L1162 | ✅ alive | OK |
| `#setting-loop-two-phase` | `loop_two_phase_enabled` | `loop-two-phase-enabled` | **YES** — save L1164 | ✅ alive | OK |
| `#setting-loop-suspicion-threshold` | `loop_suspicion_threshold` | `loop-suspicion-threshold` | **YES** — save L1165 | ✅ alive | OK |
| `#setting-loop-confirm-required` | `loop_confirm_required` | `loop-confirm-required` | **YES** — save L1166 | ✅ alive | OK |
| `#setting-loop-cooldown-feeds` | `loop_cooldown_feeds` | `loop-cooldown-feeds` | **YES** — save L1167 | ✅ alive | OK |
| `#setting-enable-skills` | `default_load_skill_mode` | `enable-skills` | **YES** — save L1137 | ✅ alive | OK |
| `#setting-auto-skill-mode` | `auto_skill_mode` | `auto-skill-mode` | **YES** — save L1138 | ✅ alive | OK |
| `#setting-auto-skill-gen` | `auto_skill_enabled` | `auto-skill-gen` | **YES** — save L1139 | ✅ alive | OK |
| `#setting-retry-max-attempts` | `retry_max_attempts` | `retry-max-attempts` | **YES** — save L1174 | ✅ alive | OK |
| `#setting-endpoint-max-retries` | `endpoint_max_retries` | `endpoint-max-retries` | **YES** — save L1175 | ✅ alive | OK |
| `#setting-retry-base-delay` | `retry_base_delay` | `retry-base-delay` | **YES** — save L1176 | ✅ alive | OK |
| `#setting-retry-max-delay` | `retry_max_delay` | `retry-max-delay` | **YES** — save L1177 | ✅ alive | OK |
| `#setting-cache-pool-enabled` | `cache_pool_enabled` | `cache-pool-enabled` | **YES** — save L1169 | ✅ alive | OK |
| `#setting-cache-pool-size` | `cache_pool_size` | `cache-pool-size` | **YES** — save L1170 | ✅ alive | OK |
| `#setting-cache-threshold-chars` | `cache_threshold_chars` | `cache-threshold-chars` | **YES** — save L1171 | ✅ alive | OK |
| `#setting-tool-result-max-chars` | `tool_result_max_chars` | `tool-result-max-chars` | **YES** — save L1143 | ✅ alive | OK |
| `#setting-grep-char-limit` | `grep_char_limit` | `grep_char_limit` | **YES** — getGenerateCfg 5352 | ✅ alive | OK (underscore, correct) |
| `#setting-grep-spillover` | `grep_spillover` | `grep_spillover` | **YES** — getGenerateCfg 5353 | ✅ alive | OK |
| `#setting-shell-char-limit` | `shell_char_limit` | `shell_char_limit` | **YES** — getGenerateCfg 5354 | ✅ alive | OK |
| `#setting-code-char-limit` | `code_char_limit` | `code_char_limit` | **YES** — getGenerateCfg 5355 | ✅ alive | OK |
| `#setting-list-dir-char-limit` | `list_dir_char_limit` | `list_dir_char_limit` | **YES** — getGenerateCfg 5356 | ✅ alive | OK |
| `#settingApprovalTimeoutEnabled` | `enable_approval_timeout` | `approval-timeout-enabled` | **YES** — save L1152 | ✅ alive | OK |
| `#settingApprovalTimeoutSeconds` | `approval_timeout_seconds` | `approval-timeout-seconds` | **YES** — save L1153 | ✅ alive | OK |
| `#settingAsyncShellConsoleWindow` | `enable_async_shell_console_window` | `async-shell-console-window` | **YES** — save L1156 | ✅ alive | OK |
| `#setting-compression-warning-threshold` | `compression_warning_threshold` | `compression-warning-threshold` | **YES** — save L1180 | ✅ alive | OK |
| `#setting-compression-force-threshold` | `compression_force_threshold` | `compression-force-threshold` | **YES** — save L1181 | ✅ alive | OK |
| `#setting-compression-proactive-threshold` | `compression_proactive_threshold` | `compression-proactive-threshold` | **YES** — save L1182 | ✅ alive | OK |
| `#setting-compression-context-reserve-tokens` | `compression_context_reserve_tokens` | `compression-context-reserve-tokens` | **YES** — save L1183 | ✅ alive | OK |
| `#setting-compression-fraction` | `compression_fraction` | `compression-fraction` | **YES** — save L1184 | ✅ alive | OK |

**Only one dead guard found: `#setting-max-rollbacks` (R3).** Everything else has a matching writer. Note the code comment at app.js:176-178 shows the team was *aware* of this failure mode and fixed the `grep_*`/`shell_*` families, but `max-rollbacks` was overlooked.

**Settings NOT in POOL_SETTINGS_MAP at all** (rely solely on localStorage via `loadSettings`): `#setting-idle-timeout` (`idle-timeout`, save L1144), `#setting-system-idle-timeout` (`system-idle-timeout`, L1145), `#setting-max-parallel` (`max_parallel_agents` via getGenerateCfg 5322), the LLM-gen sliders (`temperature`/`top_p`/`top_k`/`min_p`/`repeat_penalty`/`presence_penalty`/`frequency_penalty` via `ranges`, L1131-1133), `max_tokens`, `max_input_tokens`. These are **never stomped by the server** (not in the map) — their only "weird value" source is **R2 NaN/null poisoning**.

---

## 4. getGenerateCfg() NaN/empty-input poisoning audit

| Field (line) | Expression | Empty input → | `|| default`? | Risk |
|---|---|---|---|---|
| `temperature` (5310) | `parseFloat(v)` | `NaN` → `null` | ❌ none | **HIGH** |
| `top_p` (5311) | `parseFloat(v)` | `NaN` → `null` | ❌ none | **HIGH** |
| `top_k` (5312) | `parseInt(v)` | `NaN` → `null` | ❌ none | **HIGH** |
| `min_p` (5313) | `parseFloat(v)` | `NaN` → `null` | ❌ none | **HIGH** |
| `repeat_penalty` (5314) | `parseFloat(v)` | `NaN` → `null` | ❌ none | **HIGH** |
| `presence_penalty` (5315) | `parseFloat(v)` | `NaN` → `null` | ❌ none | **HIGH** |
| `frequency_penalty` (5316) | `parseFloat(v)` | `NaN` → `null` | ❌ none | **HIGH** |
| `max_tokens` (5317) | `parseInt(v) \|\| 8192` | `8192` | ✅ safe | OK |
| `max_input_tokens` (5318) | `parseInt(v) \|\| 32768` | `32768` | ✅ safe | OK |
| `max_turns` (5320) | `parseInt(v) \|\| 50` | `50` | ✅ safe | OK |
| `max_parallel_agents` (5322) | `parseInt(v) \|\| 3` | `3` | ✅ safe | OK |
| `max_auto_rollbacks` (5340) | `parseInt(v)` | `NaN` → `null` | ❌ none | **HIGH** (also feeds R3) |
| `idle_timeout_seconds` (5341) | `parseFloat(v) \|\| 900` | `900` | ✅ safe | OK |
| `system_agent_idle_timeout_seconds` (5342) | `parseFloat(v) \|\| 900` | `900` | ✅ safe | OK |
| `tool_result_max_chars` (5343) | `parseInt(v) \|\| 10000` | `10000` | ✅ safe | OK |
| compression fields (5346-5350) | clamped `Math.min/max(… \|\| default)` | default | ✅ safe | OK |
| grep/shell/code/list_dir (5352-5356) | `parseInt(v) \|\| -1` | `-1` | ✅ safe | OK |
| `max_images_for_llm` (5358) | `parseInt(v) \|\| 2` | `2` | ✅ safe | OK |
| retry/cache (5369-5376) | all `\|\| default` | default | ✅ safe | OK |

**8 fields are poisonable** (temperature, top_p, top_k, min_p, repeat/presence/frequency_penalty, max_auto_rollbacks). All others are safe. The LLM-gen sliders are stored under element-id keys and restored by the `ranges` loop (1212-1216), so a stored `null` re-hydrates the input to `null` on every load — exactly the "weird non-default value" symptom, and **hard-refresh-immune**.

---

## 5. loadSettings() restore audit (dead-restore / legacy keys)

- `agent-cascade-settings` with legacy fallback `qwen-settings` (app.js:1208). **Legacy key read is a real path** — if a user has an old `qwen-settings` blob and no `agent-cascade-settings`, the *old* values load. Worth checking in DevTools.
- `#setting-max-rollbacks` restored from `s['max_auto_rollbacks']` (1295) — **key asymmetry** with the map's `localKey='max-rollbacks'` (root of R3).
- LLM-gen sliders restored from element-id keys via `ranges` (1212-1216) — inherits R2 poison.
- `grep_*`/`shell_*`/etc. restored with `??` legacy-hyphen fallback (1324-1341) — safe.
- **No dead-restore keys found** for MAP settings (every `loadSettings` read has a matching writer), **except** the `max-rollbacks`/`max_auto_rollbacks` naming split.

---

## 6. Server boot sequence — what writes non-user values

`pool/core.py __init__` → `_load_pool_settings()` (`config_persist.py:76`) loads the file into `self.settings`. **Then** `api_server.py:1394-1408` overwrites `idle_timeout_seconds`/`system_agent_idle_timeout_seconds`/`idle_check_interval` with CLI/env defaults and calls `_save_pool_settings()` (1408), which **rewrites the file**. The user's persisted idle-timeout is destroyed on every boot. `max_turns` is **not** in `startup_cfg`, so it survives from the file — but the file's `250` came from an earlier onopen `update_config` push of a stale/default `max_turns` (see §7, git).

---

## 7. Git history

The boot-clobber + `_save_pool_settings()` was introduced in **one commit**:
- **`8af50cf` "feat: persist all non-cosmetic UI settings + export/import functionality"** — this is the **regression source** for R1. Before this commit, the server did not persist `idle_timeout` on boot, so the file kept the user's value. After it, every reboot re-stamps `1600`/`60`.

This matches the user's "still getting reset" — it predates the recent streaming/todo fixes and has persisted because it is structurally unconditional.

---

## 8. Chrome-specific angle (assessment)

- **No Service Worker** registration found in `app.js`/`index.html` (grep for `serviceWorker` → none).
- **No explicit bfcache handling** (`pagehide`/`pageshow`/`freeze`/`resume` → none found).
- **`beforeunload`** (6301-6318) is guarded by `settingsLoaded` (set at 1478 after `loadSettings` succeeds) and flushes the debounce timer (6305-6308). On a clean reboot Chrome *does* fire `beforeunload`, so the last edit is saved. This path is **safe**.

**Most plausible Chrome-specific mechanism (needs live confirmation):**
1. **bfcache + stale DOM on restore.** Chrome aggressively caches the page in bfcache. If the tab is restored from bfcache after a reboot and the WebSocket has silently dropped, the reconnected `onopen` (app.js:1647-1658) fires `send({type:'update_config', generate_cfg: getGenerateCfg()})` reading **stale DOM** values (which may be `null`/empty if the panel was collapsed/unrendered) → pushes `null`/poisoned values to the server → `_save_pool_settings` persists them. Other browsers (Firefox/Edge) have different bfcache heuristics, so they "seem fine."
2. **localStorage persistence + partial eviction.** If Chrome's storage quota evicted part of the `agent-cascade-settings` blob, `getGenerateCfg()` reads the missing keys as empty → NaN → null (R2), while the keys that survived keep their (possibly stale) values → a **mixed** "some settings weird" pattern that is exactly what the user reports ("*some* settings").

I **cannot confirm** either from code alone — both require a live repro. I flag them as the leading explanations for the Chrome-only aspect, but the three core defects (R1/R2/R3) are **browser-independent** and fully confirmed.

---

## 9. Discriminating analysis — which candidates fit ALL symptoms

| Symptom | R1 boot clobber | R2 NaN/null | R3 dead-guard stomp | Chrome bfcache/eviction |
|---|---|---|---|---|
| Reboot-specific | ✅ (re-stamped each boot) | ✅ (survives reboot in LS) | ✅ (re-broadcast each reconnect) | ✅ |
| Non-default "weird" value | ✅ (1600/60/250 ≠ HTML defaults) | ✅ (`null`) | ✅ (server value ≠ user value) | ✅ |
| Hard-refresh-immune | ✅ (in server file) | ✅ (in localStorage) | ✅ (server re-sends) | ✅ |
| Chrome-only | ⚠️ browser-independent | ⚠️ browser-independent | ⚠️ browser-independent | ✅ (best fit) |
| "Some settings, not all" | ✅ (only 3 fields) | ✅ (only 8 poisonable fields) | ✅ (only 1 field) | ✅ |

**R1, R2, R3 each independently reproduce "some settings, weird, non-default, hard-refresh-immune."** The Chrome-only aspect is best explained by the bfcache/eviction layer **on top of** R2 (stale DOM → null push). No single candidate explains the Chrome-only bit alone — it is a **stacking** of a confirmed core defect with a Chrome behavior.

---

## 10. Recommended fix plan (minimal, safe)

**Fix R1 (highest impact, server-side, one change):**
- `api_server.py:1394-1408`: only apply `startup_cfg` and call `_save_pool_settings()` **if the corresponding CLI/env override was explicitly provided** (`args.idle_timeout is not None`, etc.). Never clobber or persist when the user didn't override. Alternatively, make `_handle_idle_timeout` a no-op when `ui_cfg` value equals the current in-memory value *and* no CLI override was given.

**Fix R3 (one-line, frontend):**
- `app.js:143`: change `localKey: 'max-rollbacks'` → `localKey: 'max_auto_rollbacks'` (match what `getGenerateCfg` 5340 and `loadSettings` 1295 actually use). This revives the respect-local guard and stops the stomp.

**Fix R2 (add `|| default` fallbacks, frontend):**
- `app.js:5310-5316, 5340`: add `|| <default>` to `temperature`, `top_p`, `top_k`, `min_p`, `repeat_penalty`, `presence_penalty`, `frequency_penalty`, `max_auto_rollbacks` (e.g. `parseFloat(v) || 1.0`, `parseInt(v) || 0`, `parseInt(v) || -1`). Prevents `null` from ever being stored/pushed.

**Verification (live, in Chrome DevTools after next reboot):**
1. `localStorage.getItem('agent-cascade-settings')` → check for `"setting-top-k":null` / `"setting-temperature":null` etc. (R2). Check for presence of `qwen-settings` legacy key.
2. `pool_settings.json` on the server → confirm `idle_timeout_seconds`/`system_agent_idle_timeout_seconds` reset to `1600`/`60` after each boot (R1).
3. In the UI, set `#setting-max-rollbacks` to `2`, wait ~5s (one stream tick) → it reverts to `3` (R3). After the fix it should stay `2`.
4. For the Chrome angle: open the tab, **close the PC lid / reboot without closing the tab**, restore, and check whether `onopen`'s `update_config` pushed `null`s (watch WS messages in DevTools → Networking → WS frames).

---

## 11. Open questions / remaining unknowns

- Whether the user's Chrome has an old `qwen-settings` blob (legacy) still shadowing `agent-cascade-settings`.
- Confirmation of the bfcache stale-DOM push (needs a live WS frame capture during a reboot restore).
- Whether `max_turns=250` in the file is from an onopen push of a stale `max_turns` or from the `DEFAULT_MAX_TURNS` default — both point to the same fix class (R2/R1).

## 12. Confidence

- **R1 (boot clobber): Confirmed** (on-disk file + code).
- **R3 (max-rollbacks stomp): Confirmed** (map vs save/load key split, direct read).
- **R2 (NaN/null poisoning): Confirmed** (code; `JSON.stringify(NaN)===null` is well-defined).
- **Chrome-only mechanism: Moderate** (code rules out SW/bfcache handlers; bfcache/eviction is the leading hypothesis but unconfirmed live).
