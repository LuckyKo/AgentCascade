# Todo 148 — Wire `AUTO_SKILL_MIN_TURNS` to the UI settings screen

**Mode:** Investigative / Due-diligence (research only, no code changes)
**Baseline:** all file:line anchors verified by direct `read_file` of each cited window (not grep-only).
**Date:** 2026-09-19

---

## Executive Summary

`AUTO_SKILL_MIN_TURNS` is a **module-level constant** in `settings.py` (default **20**) that gates the
in-loop auto-skill reflection trigger. It is **not** exposed in the UI. The UI is a **plain HTML/JS app**
(`web_ui/index.html` + `web_ui/app.js`) talking to the backend over a **WebSocket** (`update_config` →
`generate_cfg`). There is **no single Python "settings registry"** — the wiring is distributed across 6 files.

The sibling `auto_skill_enabled` / `auto_skill_mode` ARE wired to the UI and form the exact template to copy.
`AUTO_SKILL_EXTRA_TURNS` (the other sibling) has the **identical gap** (also unwired, also a module constant).

**Critical gotcha:** wiring it through the standard 6-seam pipeline is *necessary but not sufficient*. The
consumer at `engine/core.py:283` reads the **import-time constant**, not the runtime pool-settings object.
A UI change would persist and broadcast but **never take effect** until core.py:283 is changed to read from
`self.pool.settings`. That one line is what makes it a **live update** instead of restart-required.

---

## 1. The setting itself

### Definition — `agent_cascade/settings.py:531-532`
```python
AUTO_SKILL_MIN_TURNS: int = int(os.getenv('AGENT_CASCADE_AUTO_SKILL_MIN_TURNS',
                                          20))  # Fire reflection when turns effectuated > N (strictly greater)
```
- Type `int`, default **20**, env-overridable via `AGENT_CASCADE_AUTO_SKILL_MIN_TURNS`.
- Bound **once at module import** (no getter, no per-instance copy).

### Sibling settings (same block, `settings.py:525-532`)
| Constant | Line | Default | Wired to UI? |
|---|---|---|---|
| `AUTO_SKILL_ENABLED` | :526 | `False` | **Yes** (as PoolSettings field `auto_skill_enabled`) |
| `AUTO_SKILL_EXTRA_TURNS` | :527-529 | `25` | **No** — same gap as MIN_TURNS |
| `AUTO_SKILL_MIN_TOOL_CALLS` | :530 | `5` | No (legacy, superseded) |
| `AUTO_SKILL_MIN_TURNS` | :531-532 | `20` | **No** ← this todo |

### Where it is READ — the only runtime consumer: `engine/core.py:283`
Inside `_try_auto_skill_extension(...)` (the in-loop reflection gate), `core.py:273-284`:
```python
settings = getattr(self.pool, 'settings', None)                       # :278
if not getattr(settings, 'auto_skill_enabled', AUTO_SKILL_ENABLED):   # :279  ← reads LIVE from pool.settings
    return False
if getattr(settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE) == LOAD_SKILL_NONE:  # :281
    return False
if instance._current_turn <= AUTO_SKILL_MIN_TURNS:                    # :283  ← reads IMPORT-TIME CONSTANT
    return False
```
**What it controls:** the minimum number of turns an agent must have effectuated before a natural completion
triggers an auto-skill reflection. Strictly-greater gate (`> N`). Note the asymmetry at :279 vs :283 —
`auto_skill_enabled` is already live (pool.settings), but `AUTO_SKILL_MIN_TURNS` is not.

Import binding: `core.py:36-37` imports `AUTO_SKILL_MIN_TURNS` from `agent_cascade.settings` at module load.

---

## 2. The UI tech stack

- **Frontend:** plain static HTML/JS — `web_ui/index.html` (markup) + `web_ui/app.js` (logic, ~6885 lines).
  No framework, no build step. (This **corrects** the skill-advisor assumption of a Gradio-only UI.)
- **Transport:** WebSocket. Settings are pushed via `send({ type: 'update_config', generate_cfg: getGenerateCfg() })`
  (`app.js:1241`, `:1737`) and on connect (`app.js:5793/5825/5838/5885`).
- **Backend:** the WS handler `handle_update_config` (`ws_handlers.py:735-762`) → `ConfigUpdateRouter.apply(ui_cfg)`
  dispatches to per-key handlers, then persists if any key ∈ `POOL_SETTINGS_KEYS | EXTRA_PERSIST_KEYS`.

### Is there a central "UI-exposable settings" registry? **No single one.**
The wiring is distributed. A numeric setting like this touches these seams:

| # | Seam | File:line | Role |
|---|---|---|---|
| A | HTML control | `web_ui/index.html` | the `<input>` element |
| B | JS sync map | `app.js:158-222` (`POOL_SETTINGS_MAP`) | id↔key↔localKey + server→UI sync |
| C | JS localStorage save | `app.js:1139` (`saveSettings`) | persist to browser |
| D | JS localStorage restore | `app.js:~1340` (`loadSettings`) | restore on load |
| E | JS cfg build | `app.js:5576` (`getGenerateCfg`) | build dict sent to server |
| F | Python runtime field | `agent_instance.py:771` (`PoolSettings`) | the dataclass field + default |
| G | Python persist allow-list | `config_handlers.py:26-120` (`POOL_SETTINGS_KEYS`) | which keys hit disk |
| H | Python apply+validate handler | `config_handlers.py` (`@register_config_handler`) | write to `pool.settings`, clamp |
| I | Python server→UI broadcast | `state_builder.py` (2 blocks) | serialize current value back to UI |
| J | **Runtime consumer** | `engine/core.py:283` | the gate that must read live settings |

---

## 3. The template — how `auto_skill_enabled` / `auto_skill_mode` are wired (copy this)

These two ARE in the UI. Full chain, each with its anchor:

1. **HTML** — `index.html:602-605` (`#setting-auto-skill-gen` checkbox), `:594-601` (`#setting-auto-skill-mode` select).
2. **JS sync map** — `app.js:181-182`:
   ```js
   { id: '#setting-auto-skill-mode', prop: 'value', key: 'auto_skill_mode', localKey: 'auto-skill-mode' },
   { id: '#setting-auto-skill-gen',  prop: 'checked', key: 'auto_skill_enabled', localKey: 'auto-skill-gen' },
   ```
3. **JS save** — `app.js:1176-1177` (`s['auto-skill-mode']`, `s['auto-skill-gen']`).
4. **JS restore** — `app.js:1341-1342`.
5. **JS cfg build** — `app.js:5600-5601` (`cfg.auto_skill_mode`, `cfg.auto_skill_enabled`).
6. **Python field** — `agent_instance.py:852` (`auto_skill_enabled: bool = False`), `:858` (`auto_skill_mode`).
7. **Python persist allow-list** — `config_handlers.py:55-56`.
8. **Python handler** — `config_handlers.py:394-398` (`_handle_auto_skill_enabled` → `agent_pool.settings.auto_skill_enabled = ...`), `:401-416` (`_handle_auto_skill_mode`).
9. **Python broadcast** — `state_builder.py:425-428` and `:649-652` (`'auto_skill_enabled': getattr(ps, ...)`, `'auto_skill_mode': ...`).
10. **Runtime consumer (live)** — `core.py:279` reads `getattr(settings, 'auto_skill_enabled', AUTO_SKILL_ENABLED)`.

> Note the numeric sibling that is fully wired end-to-end and closest in shape: **`loop_min_chars`**
> (`index.html:620-623`, `app.js:167/1204/5604`, `config_handlers.py:419-424` with clamp `max(500, min(20000, val))`,
> `agent_instance.py:810`, `state_builder` via `_serialize_loop_settings`). Use it as the numeric template.

---

## 4. The precise gap — what must be added for `AUTO_SKILL_MIN_TURNS`

Confirmed **missing** from every seam (grep across `.py/.js/.html` found zero references to
`auto_skill_min_turns` in any wiring file). Add **11 edit sites across 6 files**:

### Frontend (`web_ui/`)
| # | File:anchor | Change |
|---|---|---|
| 1 | `index.html` after :605 (Skills group, before `</div>` at :606) | `<label class="setting-field"><span title="Minimum turns an agent must complete before a natural finish triggers auto-skill reflection.">Auto-Skill Min Turns</span><input type="number" id="setting-auto-skill-min-turns" min="1" max="500" value="20" /></label>` |
| 2 | `app.js` `POOL_SETTINGS_MAP` (~:182) | `{ id: '#setting-auto-skill-min-turns', prop: 'value', key: 'auto_skill_min_turns', localKey: 'auto-skill-min-turns' },` |
| 3 | `app.js` `saveSettings()` (~:1177) | `if ($('#setting-auto-skill-min-turns')) s['auto-skill-min-turns'] = $('#setting-auto-skill-min-turns').value;` |
| 4 | `app.js` `loadSettings()` restore (~:1342) | `if (_present(s['auto-skill-min-turns'])) $('#setting-auto-skill-min-turns').value = s['auto-skill-min-turns'];` |
| 5 | `app.js` `getGenerateCfg()` (~:5601) | `if ($('#setting-auto-skill-min-turns')) cfg.auto_skill_min_turns = parseInt($('#setting-auto-skill-min-turns').value) || 20;` |

### Backend (`agent_cascade/`)
| # | File:anchor | Change |
|---|---|---|
| 6 | `agent_instance.py` import block :21-28 + field near :852 | Add `AUTO_SKILL_MIN_TURNS` to the `from agent_cascade.settings import (...)`; add dataclass field `auto_skill_min_turns: int = AUTO_SKILL_MIN_TURNS` (next to `auto_skill_enabled`). **Note:** it is NOT currently imported here. |
| 7 | `config_handlers.py` `POOL_SETTINGS_KEYS` (~:56, Skills group) | Add `'auto_skill_min_turns',` |
| 8 | `config_handlers.py` after :398 | New handler: `@register_config_handler('auto_skill_min_turns')` → `agent_pool.settings.auto_skill_min_turns = max(1, min(500, int(ui_cfg.get('auto_skill_min_turns', 20))))` (guard on `hasattr(agent_pool,'settings')`). |
| 9 | `state_builder.py` :425-428 block **and** :649-652 block | Add `'auto_skill_min_turns': getattr(ps, 'auto_skill_min_turns', 20),` to **both** serialization dicts. |

### Runtime (the load-bearing change)
| # | File:anchor | Change |
|---|---|---|
| 10 | `engine/core.py:283` | `if instance._current_turn <= AUTO_SKILL_MIN_TURNS:` → `if instance._current_turn <= getattr(settings, 'auto_skill_min_turns', AUTO_SKILL_MIN_TURNS):` (matches the live-read pattern already at :279). |

> **Persistence needs NO change in `pool/config_persist.py`.** Save uses `self.settings.to_dict()`
> (`config_persist.py:32`, = `asdict(self)`) and load uses `PoolSettings.from_dict(data)` (`:116`, filters via
> `hasattr`). A standard dataclass field auto-persists to / auto-restores from `pool_settings.json`. The explicit
> `data.pop(...)` block at `config_persist.py:106-113` is **only** for non-dataclass EXTRA keys (disabled_tools,
> work folders, approval timeout, etc.) — `auto_skill_min_turns` is a normal field and must NOT be popped.

---

## 5. Gotchas

1. **Live update vs restart (the big one).** `core.py:283` reads the import-time constant. Without edit #10,
   the UI value persists + broadcasts but the trigger keeps using the stale constant → change is a no-op until
   process restart. Edit #10 makes it live. Confidence: **Confirmed** (read directly).
2. **`settings` may be `None`.** `core.py:278` does `getattr(self.pool, 'settings', None)`. Use the
   `getattr(settings, 'auto_skill_min_turns', AUTO_SKILL_MIN_TURNS)` form so a `None` pool-settings falls back
   to the constant (identical safety to the existing :279 line).
3. **Validation range.** Recommend clamp `[1, 500]` in the handler (mirrors `max_turns` HTML `min="1" max="500"`
   and `loop_min_chars` clamp style). A value of `0` would mean "reflect on every natural completion"; keep min ≥ 1
   to preserve the "long-enough run" intent (see open plan note in §6).
4. **Two broadcast blocks.** `state_builder.py` serializes pool settings in **two** places (:425-428 and :649-652);
   missing one leaves a code path that never pushes the value to the UI.
5. **Rename/migration N/A.** This is an *add*, not a rename — no persisted-state migration concern. Old
   `pool_settings.json` files simply lack the key → `from_dict` fills it with default 20 (graceful).
6. **Sibling gap.** `AUTO_SKILL_EXTRA_TURNS` (`settings.py:527-529`, used at `core.py:1001/1002/1015`) is ALSO unwired
   and ALSO a module constant with the same import-time-binding issue. If the goal is "make auto-skill tuning
   configurable," it should be wired identically (same 11-site pattern, consumer at core.py:1001-1015). Out of scope
   for todo 148 as worded, but flag for planning.

---

## 6. Confidence & open questions

**Confidence:** High on all anchors (each verified by direct read). The "no single registry" and
"config_persist.py needs no change" conclusions are **Confirmed**.

**Open questions / decisions for the planner:**
- **Scope:** wire only `AUTO_SKILL_MIN_TURNS`, or also `AUTO_SKILL_EXTRA_TURNS` (identical pattern, one more consumer)?
- **Min value semantics:** keep min=1, or allow 0/-1 to mean "always reflect"? The open decision in
  `plans/auto_skill_natural_end_trigger_PLAN.md:23-28` ("keep vs drop/lower the MIN_TURNS gate") is directly
  related — lowering it via UI is exactly what this feature enables.
- **Default display:** HTML `value="20"` should match the runtime default; if `AUTO_SKILL_MIN_TURNS` default ever
  changes in settings.py, keep the three defaults (settings.py:531, agent_instance.py field, state_builder getattr)
  in sync.

---

## 7. Suggested next actions
1. Turn §4 into a checklist-driven implementation plan (the 10 numbered edits are ordered and self-contained).
2. Add one unit test per new getter/handler: happy path + clamp edge (e.g., input 0 → clamped to 1; input 9999 → 500),
   mirroring existing config-handler tests.
3. After implementation, independently re-read the diff and run `tests/test_skill_generation.py` serially
   (`-o addopts=""`) — that suite guards the trigger gate (see `.agent_lessons` / plan §117).
