# Investigation: Settings edited in UI revert mid-stream (todo.md line 139)

**Date:** 2026-08-26
**Investigator:** settings-investigation (researcher)
**Scope:** Frontend `web_ui/app.js` + `web_ui/index.html`; backend `agent_cascade/ws_handlers.py`, `config_handlers.py`, `pool/config_persist.py`, `api_integration_pkg/state_builder.py`, `constants.py`, tool read-sites.
**Mode:** Investigation only — no source changes made.
**Anchor setting:** `grep_char_limit`; cross-checked against `shell/code/list_dir_char_limit`, `tool_result_max_chars`, appearance toggles, compression/retry fields.

---

## 1. Executive Summary

The bug is **frontend-side**. Every WebSocket `state` and `stream_update` message carries the server's
`pool_settings` and is fed through `syncPoolSettings()` (app.js:199), which **unconditionally overwrites the
DOM value of the tool char-limit inputs** — including while the user is editing them during an active stream
(these messages arrive roughly every ~150 ms while an agent is running; see stream_publisher.py:133-138).

The guard that is supposed to prevent this ("only sync if the user has not set a value locally",
app.js:211) never engages for the char-limit family, because of a **localStorage key mismatch**:
the map uses hyphenated `localKey`s (`'grep-char-limit'`), but `saveSettings()` stores these particular
keys **only in underscore form** (`grep_char_limit`) via `getGenerateCfg()` (app.js:5330-5334; explicit
comment at app.js:1168-1170; legacy hyphenated keys are actively deleted at app.js:1448).

Result while streaming: user types a new value → next tick stomps the input with the old server value →
`syncPoolSettings` even calls `saveSettings(false)` (app.js:246) writing the stomped value back to
localStorage, and the debounced auto-save (app.js:1465-1473) can push the stomped value to the server via
`update_config` — reverting **both client and server/persistent state**. The backend handlers themselves are
correct and apply updates live (`pool.llm_cfg` mutated in place; tools read it fresh per call).

**Confidence: High** for the primary mechanism (code-traced end-to-end; mechanism uniquely matches the
reported symptom set). Not reproduced live (static analysis only).

---

## 2. Data Flow (text diagram)

### Write path (UI → server) — grep_char_limit

```
[index.html:666]  <input id="setting-grep-char-limit">
      │  'change' → saveSettings()            [app.js:1471, immediate]
      │  'input'  → debouncedSaveSettings()    [app.js:1472, 300 ms]
      ▼
[app.js:1085 saveSettings()]
      ├─ s = getGenerateCfg()                 [app.js:1087]
      │     └─ cfg.grep_char_limit = parseInt($('#setting-grep-char-limit').value)   [app.js:5330]
      ├─ localStorage['agent-cascade-settings'] = JSON.stringify(s)                  [app.js:1172]
      │     └─ stored key: 'grep_char_limit'  (underscore; NO 'grep-char-limit' written)
      └─ send({type:'update_config', generate_cfg:getGenerateCfg()})                 [app.js:1174-1176]
            ▼
[ws_handlers.py:680 handle_update_config()]
      ├─ ConfigUpdateRouter.apply(ui_cfg)      [ws_handlers.py:694-695]
      │     └─ _handle_grep_char_limit → agent_pool.llm_cfg['grep_char_limit'] = val [config_handlers.py:531-536]
      ├─ _save_pool_settings()  → config/pool_settings.json                          [ws_handlers.py:699-702;
      │                                                                              config_persist.py:63-72]
      └─ await self._broadcast()               [ws_handlers.py:712]
```

### Read path (runtime)

```
[file_ops.py:1090-1094]  grep tool, PER CALL:
    char_limit = agent_pool.llm_cfg.get('grep_char_limit', ...)   ← fresh read of the live dict, no cache
(same pattern: shell_cmd.py:182/334/601, code_interpreter.py:1130)
```
No per-turn snapshot, no invalidation requirement. **Backend latency for this setting ≈ zero.**

### Echo path (server → UI) — the problem

```
During generation:
  stream_publisher.push_periodic_update() every ~150 ms          [stream_publisher.py:129-138]
  api_integration_pkg/streaming.py:117-128 / runner.py:280-281
      └─ build_stream_update_from_pool()
           └─ pool_settings = {...,'grep_char_limit': pool.llm_cfg['grep_char_limit'], ...}
                                                           [state_builder.py:486-563; llm_cfg keys at 526-530]
      └─ ws → {'type':'stream_update', ..., 'pool_settings': ...}

Frontend handleServerMessage():
  case 'state':         syncPoolSettings(data.pool_settings)   [app.js:1894]
  case 'stream_update': syncPoolSettings(data.pool_settings)   [app.js:2090]   ← EVERY TICK DURING STREAMING
      └─ for each POOL_SETTINGS_MAP entry:                        [app.js:140-191, 199-247]
             if (localKey && saved[localKey] !== undefined) continue;   [app.js:211] ← GUARD
             else el[prop] = ps[key];  changed=true                    [app.js:212-213]
      └─ if changed → saveSettings(false)  (writes stomped DOM into localStorage) [app.js:246]
```

---

## 3. Suspect List (file:line)

| # | Location | What it is | Verdict |
|---|----------|------------|---------|
| S1 | app.js:140-191 + 207-215 | `POOL_SETTINGS_MAP` entries for char limits carry **hyphenated** `localKey`s (`'grep-char-limit'`, `'shell-char-limit'`, …) | **PRIMARY** — makes the respect-local-preference guard dead code for exactly this family |
| S2 | app.js:5330-5334 + 1087 + 1172 (+ comment 1168-1170, cleanup 1448) | These settings exist in localStorage **only as underscore keys**; hyphenated variants are deleted on load | **PRIMARY** (co-conspirator of S1) — `saved['grep-char-limit']` is always `undefined` |
| S3 | app.js:2090 (and 1894) | `syncPoolSettings()` invoked on **every** stream_update/state message (~150 ms cadence during streaming) | **AMPLIFIER** — turns a latent sync bug into a mid-edit stomp loop |
| S4 | app.js:246 + 1465-1473 | Sync itself persists the stomped value (`saveSettings(false)`), and the panel-wide debounced auto-save then transmits the stomped DOM value to the server (`update_config`) | **AMPLIFIER** — makes the revert sticky on client *and* server/pool_settings.json |
| S5 | app.js:199-215 (general) | No "user is editing / element focused / dirty" suppression before writing to the DOM; stale in-flight ticks can clobber a just-committed value ahead of the server echo | SECONDARY — race window even for correctly-mapped keys |
| S6 | ws_handlers.py:706-710 | `_apply_ui_config` is applied to running instances **only when `'disabled_tools'` is present** in the update | ADJACENT (different symptom) — LLM-param edits (temperature/top_p/…) legitimately don't reach *already-running* instances because their `_generate_cfg_override` (state_builder.py:1047-1087) is only refreshed under that gate; not the cause of the grep-limit revert (char limits are stripped from overrides via NON_LLM_KEYS, constants.py:198-202, and tools read `pool.llm_cfg` directly) |
| S7 | Backend handlers/persistence (config_handlers.py:531-536; config_persist.py:14-74, 154-195; pool/core.py:102) | Live mutation of `pool.llm_cfg`, save-on-update, load-once-at-startup | **CLEARED** — no stale cache, no restore-on-stop logic anywhere on the backend |

Backend "restore previous values" search: `_load_pool_settings()` is called **once** in `AgentPool.__init__`
(pool/core.py:102). There is no code path that reloads or restores settings at turn end/stop/error. The
perceived "resets at stop" is the final full `state` message (app.js:1894) applying the server's (old) truth
one last time after the storm of per-tick stomps.

---

## 4. Root-Cause Hypotheses (ranked)

### H1 — CONFIRMED (High confidence): `syncPoolSettings` stomp-loop caused by `localKey` ↔ storage-key mismatch, fired on every stream tick

Chain of evidence:

1. `POOL_SETTINGS_MAP` (app.js:175-179) declares `{key:'grep_char_limit', localKey:'grep-char-limit'}` etc.
2. `saveSettings()` merges `getGenerateCfg()` into the saved object first (app.js:1087), which writes
   **underscore** keys only for this family (app.js:5330-5334). The in-code comment says so explicitly:
   *"Grep settings saved via getGenerateCfg() as grep_char_limit / grep_spillover — no duplicate hyphenated
   keys needed here"* (app.js:1168-1170).
3. `loadSettings()` cleans up legacy hyphenated keys from localStorage (app.js:1448), so
   `saved['grep-char-limit']` is `undefined` forever.
4. Therefore the guard `if (localKey && saved[localKey] !== undefined) continue;` (app.js:211) **never
   skips** for these five controls, and step 5 executes on **every** message.
5. `case 'stream_update'` calls `syncPoolSettings(data.pool_settings)` on every tick (app.js:2090); ticks
   arrive ~every 150 ms while an agent streams (stream_publisher.py:136). Each tick assigns
   `el.value = ps[key]` (old server value) — visually "instantly resetting" the field mid-typing.
6. `changed=true` ⇒ `saveSettings(false)` (app.js:246) writes the stomped value into localStorage; the
   panel-level debounced save (app.js:1470-1473) subsequently sends `update_config` built from the stomped
   DOM (app.js:1174-1176), which can revert the **server-side** value and its persistence too.

Why the symptom set matches exactly:

- **"settings LIKE grep char limit"** — contrast with `tool_result_max_chars`, retry, compression,
  approval-timeout, console-window entries: for those, `saveSettings()` *also* writes the hyphenated
  `localKey` (app.js:1104, 1122, 1131-1135, 1153-1156, 1159-1163), so their guards work and they are
  protected. The **unprotected set is precisely** `{grep_char_limit, grep_spillover, shell_char_limit,
  code_char_limit, list_dir_char_limit}` — the tool-output char-limit family named in the bug.
- **"while the agent is streaming"** — the stomp frequency is proportional to stream_update traffic; idle
  pools only send occasional `state` messages, so edits when nothing is running mostly survive (single
  opportunistic stomp window instead of a continuous one).
- **"resets them to whatever value they had at stop"** — when the turn ends, a final full `state`
  (app.js:1894) re-applies server truth; combined with step 6 the old value is what the server kept.

### H2 — Secondary (Moderate confidence): missing edit/focus suppression allows stale-tick races for ALL mapped settings

Even with correct keys, an in-flight `stream_update` serialized *before* the server applied the user's
`update_config` can arrive *after* the DOM was updated, clobbering the fresh value before the authoritative
echo lands. For guarded settings the guard hides this after the first save; for the char-limit family it is
wide open. There is no `document.activeElement` / dirty-flag check anywhere in `syncPoolSettings`
(app.js:199-247). This widens H1's blast radius and would resurface if H1 were fixed naively (e.g., by only
fixing the key strings) whenever the user edits faster than one round-trip.

### H3 — Investigated and CLEARED: backend staleness/caching

Verified negative:
- Handlers mutate the live shared dicts (`config_handlers.py:523-567`); no queueing, no deferral.
- Runtime readers consume `pool.llm_cfg` fresh per tool call (`file_ops.py:1092-1094`,
  `shell_cmd.py:182/334/601`, `code_interpreter.py:1130`). No per-turn copy exists.
- `api_router.default_llm_cfg` (updated only for `LLM_CONFIG_KEYS`, config_handlers.py:759-762) never holds
  char limits and is not consulted by the tool read sites.
- Persistence loads once at startup (pool/core.py:102); nothing re-reads or restores at stop/error.
- `instance._generate_cfg_override` excludes char-limit keys by construction (NON_LLM_KEYS filter,
  state_builder.py:1041-1042 + constants.py:198-202).

Residual backend observation (out of scope for THIS bug, flagged for the backlog): because
`_apply_ui_config` for running instances is gated on `disabled_tools` (S6), LLM-parameter changes made
mid-run take effect only for instances created afterwards. If the user perceives "temperature didn't change
either", that is this separate mechanism, not H1.

---

## 5. How settings are read at runtime (question 4 answered explicitly)

| Layer | Mechanism | Cached? | Invalidation |
|---|---|---|---|
| Tool char limits (`pool.llm_cfg` keys) | Direct dict read per tool invocation (file_ops.py:1094) | Shared mutable dict = always current | None needed (mutated in place by handlers) |
| `PoolSettings` fields (thresholds/timeouts) | Attribute access on `pool.settings` dataclass throughout engine | Live object | None needed |
| Per-instance LLM params (`_generate_cfg_override`) | Deep-copied template cfg + sanitized UI cfg at instance creation; merged at LLM call time (state_builder.py:1047-1087; lifecycle_manager.py:691) | **Yes — per instance** | Refreshed only via the `disabled_tools`-gated loop (ws_handlers.py:706-710) or instance recreation |
| Disk (`config/pool_settings.json`) | Written on each persistent-key update; read once at boot (config_persist.py:14-74, pool/core.py:102) | n/a | n/a |

⇒ The divergence between "what the user set" and "what the system uses" is created **entirely in the
frontend sync layer**, not by backend caching.

---

## 6. Recommended Next Steps (for the fix phase — not yet implemented)

1. **Fix the key mismatch (minimal, targeted).** Change `localKey`s in `POOL_SETTINGS_MAP` for the five
   char-limit entries to the underscore storage keys (`'grep_char_limit'`, `'grep_spillover'`,
   `'shell_char_limit'`, `'code_char_limit'`, `'list_dir_char_limit'`) — app.js:175-179. Note the
   `import_settings` cleanup (app.js:2285-2288) iterates `localKey`, so it stays consistent automatically.
2. **Add edit suppression to `syncPoolSettings` (defense-in-depth).** Skip assignment when
   `el === document.activeElement`, and/or set a short dirty timestamp on `input` events per control and
   ignore sync writes within it (addresses H2 for all mapped settings). Also avoid the unconditional
   `saveSettings(false)` write-back (app.js:246) for controls the user is actively editing.
3. **Optional consistency guard:** before assigning in step 5 of the sync loop, skip if
   `String(el[prop]) === String(ps[key])` (cheap no-op filter that prevents `changed=true` churn and the
   associated localStorage write).
4. **Regression tests (frontend harness/Node):**
   - Simulate N `stream_update` ticks carrying `pool_settings` while a test edits `#setting-grep-char-limit`;
     assert the control retains the user value and the emitted `update_config` carries it.
   - Assert invariant: every `POOL_SETTINGS_MAP.localKey` equals the exact key `saveSettings()` writes for
     that control (would have caught this class of bug; derivable by diffing the map against
     `getGenerateCfg()`/`saveSettings()` output keys).
5. **Backlog (separate ticket, do not bundle):** decide whether mid-run `update_config` should refresh
   `_generate_cfg_override` on running instances for LLM params (S6) — currently only `disabled_tools`
   updates do.

### Risks / unknowns
- Static analysis only; the ~150 ms tick cadence and exact interleavings are taken from code comments
  (stream_publisher.py:136) rather than a live trace. A 2-minute manual repro with DevTools breakpoints on
  app.js:212/246 would confirm H1 empirically before the fix lands.
- Multiple simultaneous browser tabs share the server; a stale second tab can push old values via
  `update_config` (incl. the onopen full-cfg push, app.js:1635). Out of scope here, but relevant to any
  "settings changed themselves" reports from multi-tab sessions.

— End of report.
