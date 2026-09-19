# Plan — Todo 148: Wire `AUTO_SKILL_MIN_TURNS` to the UI settings screen

**Goal:** Make the auto-skill reflection turn-threshold (`AUTO_SKILL_MIN_TURNS`, default 20) configurable from
the UI, with **live update** (no restart). Scope: ONLY `auto_skill_min_turns`. Do NOT wire
`AUTO_SKILL_EXTRA_TURNS` (flagged separately, out of scope for this todo).

**Template to copy:** the fully-wired siblings `auto_skill_enabled` / `auto_skill_mode` (bool/select) and the
numeric `loop_min_chars`. Follow their exact shape. Research report with all verified anchors:
`reports/todo148_auto_skill_min_turns_ui_wiring.md`.

---

## The 10 edits (ordered, self-contained)

### Frontend — `web_ui/index.html`
**Edit 1.** In the Skills settings group, immediately after the `#setting-auto-skill-gen` checkbox block
(anchor ~:605), before the group's closing `</div>` (~:606), add a numeric field mirroring the
`loop_min_chars` input style:
```html
<label class="setting-field"><span title="Minimum turns an agent must complete before a natural finish triggers auto-skill reflection.">Auto-Skill Min Turns</span><input type="number" id="setting-auto-skill-min-turns" min="1" max="500" value="20" /></label>
```

### Frontend — `web_ui/app.js` (4 seams)
**Edit 2.** `POOL_SETTINGS_MAP` (~:182, next to the auto_skill entries):
```js
{ id: '#setting-auto-skill-min-turns', prop: 'value', key: 'auto_skill_min_turns', localKey: 'auto-skill-min-turns' },
```
**Edit 3.** `saveSettings()` (~:1177, next to the auto-skill save lines):
```js
if ($('#setting-auto-skill-min-turns')) s['auto-skill-min-turns'] = $('#setting-auto-skill-min-turns').value;
```
**Edit 4.** `loadSettings()` restore block (~:1342, next to the auto-skill restore lines):
```js
if (_present(s['auto-skill-min-turns'])) $('#setting-auto-skill-min-turns').value = s['auto-skill-min-turns'];
```
**Edit 5.** `getGenerateCfg()` (~:5601, next to the auto_skill cfg lines):
```js
if ($('#setting-auto-skill-min-turns')) cfg.auto_skill_min_turns = parseInt($('#setting-auto-skill-min-turns').value) || 20;
```

### Backend — `agent_cascade/agent_instance.py`
**Edit 6a.** Add `AUTO_SKILL_MIN_TURNS` to the existing `from agent_cascade.settings import (...)` block (~:21-28).
(It is NOT currently imported here.)
**Edit 6b.** In the `PoolSettings` dataclass, next to `auto_skill_enabled` (~:852), add:
```python
auto_skill_min_turns: int = AUTO_SKILL_MIN_TURNS
```

### Backend — `agent_cascade/config_handlers.py` (or wherever POOL_SETTINGS_KEYS + handlers live)
**Edit 7.** Add `'auto_skill_min_turns',` to the `POOL_SETTINGS_KEYS` allow-list (~:56, Skills group).
**Edit 8.** Add a new handler after the `_handle_auto_skill_enabled` / `_handle_auto_skill_mode` handlers (~:398),
mirroring the numeric clamp style of `loop_min_chars`:
```python
@register_config_handler('auto_skill_min_turns')
def _handle_auto_skill_min_turns(agent_pool, ui_cfg):
    if not hasattr(agent_pool, 'settings'):
        return
    val = int(ui_cfg.get('auto_skill_min_turns', 20))
    agent_pool.settings.auto_skill_min_turns = max(1, min(500, val))
```
(Match the exact handler signature/decorator style used by the neighboring handlers — read them first.)

### Backend — `agent_cascade/state_builder.py` (TWO blocks)
**Edit 9.** Add `'auto_skill_min_turns': getattr(ps, 'auto_skill_min_turns', 20),` to **BOTH** pool-settings
serialization dicts: the ~:425-428 block AND the ~:649-652 block. Missing one leaves a code path that never
pushes the value to the UI.

### Runtime — `agent_cascade/engine/core.py` (THE load-bearing change)
**Edit 10.** At ~:283, inside `_try_auto_skill_extension`, change the import-time constant read to a live read
(matching the existing live-read pattern at :279):
```python
# before:
if instance._current_turn <= AUTO_SKILL_MIN_TURNS:
    return False
# after:
if instance._current_turn <= getattr(settings, 'auto_skill_min_turns', AUTO_SKILL_MIN_TURNS):
    return False
```
`settings` is already `getattr(self.pool, 'settings', None)` at :278, so the `getattr(..., default)` form safely
falls back to the constant when pool-settings is `None`. **Without this edit the UI change is a no-op until restart.**

---

## Persistence — NO CHANGE NEEDED
`config_persist.py` uses `PoolSettings.to_dict()` (= asdict) on save and `from_dict()` (hasattr-filtered) on load.
A standard dataclass field auto-persists/restores to `pool_settings.json`. Do NOT add it to the explicit
`data.pop(...)` block (that's only for non-dataclass EXTRA keys).

## Validation range
Clamp `[1, 500]` in the handler. Min ≥ 1 preserves "long-enough run" intent (0 would mean reflect on every
natural completion — not desired here; that's a separate open design question).

## Tests to add (`tests/`)
Mirror existing config-handler tests:
- Handler happy path: valid value (e.g. 30) → `pool.settings.auto_skill_min_turns == 30`.
- Clamp low: input 0 → clamped to 1. Input -5 → 1.
- Clamp high: input 9999 → 500.
- Live-gate test: with `pool.settings.auto_skill_min_turns` set low/high, `_try_auto_skill_extension` gate at
  core.py:283 respects the LIVE value (not the constant). This is the key behavioral test proving live update.

## Verification after implementation
1. Run the new tests + existing config-handler / auto-skill suites serially:
   `python -m pytest tests/test_skill_generation.py <new test file> -o addopts="" -n 0 -p no:cacheprovider`
2. Then a broader regression run to confirm no breakage in the settings/pool/state_builder paths.
3. Independent reviewer re-reads the diff (all 10 sites present, both state_builder blocks, live-read at core.py).

## Consistency / gotchas checklist
- [ ] All 10 edits present (5 frontend JS/HTML + agent_instance field+import + config_handlers key+handler + 2×state_builder + core.py live read).
- [ ] Three defaults stay in sync: `settings.py:531` (20), `agent_instance.py` field default, `state_builder` getattr default.
- [ ] HTML `value="20"` matches runtime default.
- [ ] Both state_builder broadcast blocks updated.
- [ ] core.py:283 uses the live-read form with constant fallback.
- [ ] Did NOT touch AUTO_SKILL_EXTRA_TURNS (out of scope).
