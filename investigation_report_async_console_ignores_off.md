# Investigation Report — Async Shell Console Window Appears Despite "Show Async Shell Console Window" Toggle = OFF

**Task:** User reports console windows still pop up for async `shell_cmd` even with the setting OFF.
**Investigator:** researcher (shell_console_investigator)
**Date:** 2026-08-07
**Status:** Root cause identified (High Confidence)

---

## Executive Summary

The "Show Async Shell Console Window" toggle **is** correctly defined, read, wired, persisted, and applied — but a **persistence initialization-ordering bug** causes the setting to be **reset to `True` on every server restart**.

The persisted config file `config/pool_settings.json` contains `"enable_async_shell_console_window": false` (line 117) — the toggle is correctly saved as OFF. However, during `AgentPool.__init__`, the code **loads** that `false` and then **unconditionally overwrites it** with `True` a few lines later. Result: once the server restarts, `_enable_async_shell_console_window` is `True` again, so every async shell still opens a console window.

---

## The Bug (exact location)

File: `agent_cascade/agent_pool.py`

**Ordering in `AgentPool.__init__`:**

1. **Line 274:** `self._load_pool_settings()`  ← loads persisted values, **including** the OFF flag
   - Inside `_load_pool_settings()` (`agent_pool.py:481-483`):
     ```python
     # Apply async shell console window toggle from disk if present
     if enable_async_shell_console_window_raw is not None:
         self._enable_async_shell_console_window = bool(enable_async_shell_console_window_raw)
     ```
     → sets `self._enable_async_shell_console_window = False`

2. **Line 319:** (runs **AFTER** the load)
   ```python
   # ── Async Shell Infrastructure (background shell_cmd support) ────────
   from agent_cascade.async_shell import AsyncShellTracker
   self._async_shell_tracker = AsyncShellTracker(pool=self)
   self._enable_async_shell_console_window = True  # Default ON (current behavior)
   ```
   → **unconditionally overwrites** the loaded `False` back to `True`

Because line 319 executes after line 274's load, the user's OFF preference is discarded on startup. The default `True` is the effective value after every restart.

### Fix

Move the attribute initialization **before** the `_load_pool_settings()` call, OR guard line 319 so it only sets the default when the attribute hasn't already been loaded. Recommended minimal fix: initialize `self._enable_async_shell_console_window = True` at the class/init area **before** line 274, and delete/replace the unconditional `= True` at line 319 with a "set default only if not already loaded" guard.

```python
# Before self._load_pool_settings() (i.e. near line 273)
self._enable_async_shell_console_window = True  # default, overridden by persisted value below

# Line 319: remove the unconditional "= True" (leave the comment), OR guard it:
# self._enable_async_shell_console_window = True  # DELETE — clobbers persisted value
```

---

## Evidence Chain (how the setting is supposed to work)

### 1. Setting definition & frontend control
- `web_ui/index.html:409-412` — checkbox `#settingAsyncShellConsoleWindow` labeled **"Show Async Shell Console Window"**
- `web_ui/app.js:179` — `POOL_SETTINGS_MAP` entry maps it to backend key `enable_async_shell_console_window` (localStorage `'async-shell-console-window'`)
- `web_ui/app.js:642-643` — element ref `settingAsyncShellConsoleWindow`
- `web_ui/app.js:982-983` — saved to localStorage in `saveSettings()`
- `web_ui/app.js:4710-4712` — included in `getGenerateCfg()` → sent as `update_config`

### 2. Backend handler (config → pool)
- `agent_cascade/config_handlers.py:239-247` — handler `_handle_enable_async_shell_console_window` sets `agent_pool._enable_async_shell_console_window = bool(...)`
- `agent_cascade/config_handlers.py:51-52` — key added to `POOL_SETTINGS_KEYS` (triggers `_save_pool_settings()`)
- Dispatched via `ConfigUpdateRouter.apply()` (`config_handlers.py:749-762`), called by `ws_handlers.py:723-731` (`handle_update_config`)

### 3. Persistence
- Save: `agent_pool.py:400-401` writes `enable_async_shell_console_window` to `pool_settings.json`
- **Confirmed in file:** `config/pool_settings.json:117` → `"enable_async_shell_console_window": false` ✓ (correctly OFF)
- UI→state broadcast: `api_integration.py:748` (`_add_pool_runtime_settings`)

### 4. Consumption (async launch path)
- `agent_cascade/tools/custom/shell_cmd.py:252-255` — reads `self.agent_pool._enable_async_shell_console_window` → `console_window` variable
- `shell_cmd.py:259-266` — passes `console_window=console_window` to `tracker.launch(...)`
- `agent_cascade/async_shell.py:266, 315` — `launch(console_window=...)` stored on `AsyncShellTask.console_window`
- `async_shell.py:472, 487` — `configure_windows_utf8(command, create_new_console=task.console_window)`
- `shell_utils.py:77-90` — if `create_new_console` → `flags |= subprocess.CREATE_NEW_CONSOLE`
- `async_shell.py:521-534` — **viewer process** spawn with `CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP` (the visible window), guarded by `if ON_WINDOWS and task.console_window`

**All downstream checks gate on `console_window`**, so the code path is correct *if* the pool flag is actually `False`. The only defect is that the flag is reset to `True` at startup.

---

## Confidence Level: **Confirmed (High)**

- Persisted file value is `false` (`pool_settings.json:117`).
- The load path (`agent_pool.py:481-483`) reads it correctly.
- The default overwrite (`agent_pool.py:319`) runs after the load and unambiguously sets it to `True`.
- Timeline consistent: user turns it OFF → it saves `false` → a restart resets to `True` → async shells pop windows again.

---

## Why regression tests pass

The toggle tests in `tests/` and the earlier regression suite for `shell_cmd` verify the **runtime handler wiring** (setting the flag, passing `console_window` down to `Popen`), not the `AgentPool.__init__` restart-persistence path. The bug is purely in the object initialization load-vs-default ordering, which existing tests do not cover.

---

## Recommended Fix

Move the default assignment before the load, or guard it:
```python
# In __init__ BEFORE self._load_pool_settings() (line 274)
self._enable_async_shell_console_window = True

# At line 319 -> remove the unconditional assignment
# (keep attribute creation only if the load didn't set it)
```
Add a regression test asserting that after `_load_pool_settings()` a persisted `false` in the file is NOT overwritten by `__init__` defaults.

---

## Confidence
- **Root cause:** Confirmed
- **Fix verifies window suppression:** Pending implementation + restart test