# Implementation Plan: shell_cmd Console Window Toggle + Focus Guard

**Todo:** line 85 — "need an UI toggle for shell_cmd window popup, also they should not steal focus if they do show up"

**Target:** The async shell_cmd Windows console window only (the viewer process spawned via `CREATE_NEW_CONSOLE`). This is NOT about the approval bar. Sync shell_cmd never pops a window.

**Default:** ON (`True`, preserve current behavior). User can disable to suppress the console window entirely.

---

## Part 1: Console Window Toggle — Full-Stack Setting

Follows Pattern A (approval-timeout toggle): HTML → saveSettings/getGenerateCfg → config_handlers → POOL_SETTINGS_KEYS → persistence → broadcast.

**Key naming convention:** Backend key is `enable_async_shell_console_window` (matches `enable_approval_timeout`). localStorage key is `async-shell-console-window` (kebab-case).

### 1.1 HTML: Add toggle in settings panel

**File:** `web_ui/index.html`
**Location:** Lines ~408-415, inside "Approvals & Timeouts" section body.

Add after the approval-timeout checkbox (~line 411):

```html
<label class="setting-field toggle-field">
  <span>Show Async Shell Console Window</span>
  <input type="checkbox" id="settingAsyncShellConsoleWindow" checked />
</label>
```

### 1.2 Frontend: Wire the toggle into settings flow

**File:** `web_ui/app.js`

#### Element reference (line ~637 area)

Add near `approvalTimeoutEnabled`:

```js
const settingAsyncShellConsoleWindow = $('#settingAsyncShellConsoleWindow');
```

#### saveSettings() — line ~975

After approval-timeout settings (~line 975):

```js
if (settingAsyncShellConsoleWindow) s['async-shell-console-window'] = settingAsyncShellConsoleWindow.checked;
```

#### loadSettings() — line ~1074 area

No special handling needed — generic localStorage restore via `POOL_SETTINGS_MAP` covers this.

#### POOL_SETTINGS_MAP — line ~178

Add entry so server state syncs to UI:

```js
{ id: '#settingAsyncShellConsoleWindow', prop: 'checked', key: 'enable_async_shell_console_window', localKey: 'async-shell-console-window' },
```

#### getGenerateCfg() — line ~4700

After approval-timeout settings (~line 4700):

```js
if (settingAsyncShellConsoleWindow) cfg.enable_async_shell_console_window = settingAsyncShellConsoleWindow.checked;
```

### 1.3 Backend: Register the setting

**File:** `agent_cascade/config_handlers.py`

#### POOL_SETTINGS_KEYS — line ~64

Add key to frozenset:

```python
'enable_async_shell_console_window',
```

#### Handler function — after `_handle_enable_approval_timeout` (~line 235)

Follow exact pattern of `_handle_enable_approval_timeout`:

```python
@register_config_handler('enable_async_shell_console_window')
def _handle_enable_async_shell_console_window(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Toggle async shell_cmd console window popup."""
    from agent_cascade.log import logger as _logger
    if agent_pool is not None:
        try:
            agent_pool._enable_async_shell_console_window = bool(ui_cfg['enable_async_shell_console_window'])
        except Exception as e:
            _logger.warning(f"Failed to set async shell console window toggle: {e}")
```

### 1.4 Backend: Store/load on AgentPool

**File:** `agent_cascade/agent_pool.py`

#### __init__ — line ~318 area

After `_async_shell_tracker` init, add default:

```python
self._enable_async_shell_console_window = True  # Default ON (current behavior)
```

#### _save_pool_settings() — line ~401 area

Add to the data dict before `json.dump`, with hasattr guard matching approval-timeout pattern:

```python
if hasattr(self, '_enable_async_shell_console_window'):
    data['enable_async_shell_console_window'] = bool(self._enable_async_shell_console_window)
```

#### _load_pool_settings() — line ~460 area

After loading approval timeout settings (~line 467), add:

```python
if 'enable_async_shell_console_window' in data:
    self._enable_async_shell_console_window = bool(data['enable_async_shell_console_window'])
```

### 1.5 Backend: Broadcast setting to UI

**File:** `agent_cascade/api_integration.py`

#### build_state_from_pool() — line ~851 area

After approval timeout settings (~line 851):

```python
pool_settings['enable_async_shell_console_window'] = getattr(pool, '_enable_async_shell_console_window', True)
```

### 1.6 Backend: Thread the flag into async shell launch

**File:** `agent_cascade/tools/custom/shell_cmd.py`

#### _launch_async() → tracker.launch() call — line ~254-260

Read the setting from agent_pool and pass it through:

```python
console_window = True
if self.agent_pool and hasattr(self.agent_pool, '_enable_async_shell_console_window'):
    console_window = bool(self.agent_pool._enable_async_shell_console_window)

tool_id, pid, early_output, completed_early, return_code = tracker.launch(
    agent_name=agent_name,
    command=command,
    heartbeat_interval=heartbeat_interval,
    timeout=effective_timeout,
    cwd=resolved_cwd,
    console_window=console_window,  # NEW: respect user toggle
)
```

### 1.7 AsyncShellTracker — already wired

**File:** `agent_cascade/async_shell.py`

- Line ~266: `launch(...)` already accepts `console_window: bool = True` parameter.
- Line ~315: Already stores it on `AsyncShellTask.console_window`.
- Lines ~472, ~521: Already uses `task.console_window` to decide whether to spawn viewer process and set CREATE_NEW_CONSOLE.

No changes needed here — the flag flows correctly once passed from shell_cmd.py.

---

## Part 2: Focus Handling for the Console Window

**Goal:** If the console window popup is shown (toggle ON), it should not steal focus from the browser/UI.

### Current Behavior

When `console_window=True`, the viewer process (`cmd.exe /c ...`) is spawned with `CREATE_NEW_CONSOLE` (`async_shell.py:531`). On Windows, a new console window inherently takes OS-level foreground focus — this is the focus-steal issue mentioned in todo #85.

### Options Analysis

| Approach | Pros | Cons |
|----------|------|------|
| Leave as-is (CREATE_NEW_CONSOLE) | Simple, visible immediately | Steals focus (the problem we're solving) |
| Spawn minimized (`SW_SHOWMINNOACTIVE`) | Doesn't steal focus | User may not notice it's running; harder to find |
| Don't spawn viewer at all when toggle OFF | No focus steal, clean | Already covered by the toggle itself |
| Use `SetForegroundWindow` workaround | Theoretical no-focus-spawn | Unreliable on Windows 10/11, race conditions |

### Decision: Accept Limitation + Document It

**There is no reliable way to spawn a visible Windows console window via CREATE_NEW_CONSOLE without it briefly stealing focus.** This is OS behavior, not our bug.

For this task:
- The **toggle** lets users disable the popup entirely if focus-steal is unacceptable.
- When toggle is ON and the window appears, we accept that Windows will bring it to front — this is documented as a known limitation.
- No code changes needed for focus handling beyond the toggle itself.

If we later want true non-intrusive console access, options include:
- Spawn minimized with a tray/notification indicator (requires more infrastructure).
- Remove the external console entirely and rely on heartbeat output in-browser (already available).

---

## Change Summary

| File | Lines | Change |
|------|-------|--------|
| `web_ui/index.html` | ~412 | Add checkbox `<input id="settingAsyncShellConsoleWindow">` |
| `web_ui/app.js` | ~637 | Element reference |
| `web_ui/app.js` | ~975 | saveSettings entry |
| `web_ui/app.js` | ~178 | POOL_SETTINGS_MAP entry |
| `web_ui/app.js` | ~4700 | getGenerateCfg entry |
| `agent_cascade/config_handlers.py` | ~64 | Add key to POOL_SETTINGS_KEYS |
| `agent_cascade/config_handlers.py` | ~235+ | Register handler function (correct signature) |
| `agent_cascade/agent_pool.py` | ~318 | Default value in __init__ |
| `agent_cascade/agent_pool.py` | ~401 | Save to pool_settings.json |
| `agent_cascade/agent_pool.py` | ~460 | Load from pool_settings.json |
| `agent_cascade/api_integration.py` | ~851 | Broadcast to UI state |
| `agent_cascade/tools/custom/shell_cmd.py` | ~254 | Read setting and pass console_window to tracker.launch() |

**No changes needed:**
- `async_shell.py` — already wired for the flag end-to-end.
- `shell_utils.py` — already respects `create_new_console` param.

**Total:** 12 file touch points, ~30 lines of code.

---

## Risks / Edge Cases

1. **Race condition on toggle change:** If user flips the toggle while an async shell is running, it only affects new launches (correct behavior). Existing tasks keep their original `console_window` value.

2. **Persistence format mismatch:** The frontend uses kebab-case keys in localStorage (`async-shell-console-window`) but snake_case in config sent to server (`enable_async_shell_console_window`). This matches the existing pattern used by all other toggles (e.g., `approval-timeout-enabled` / `enable_approval_timeout`).

3. **Server restart:** Setting is persisted via `_save_pool_settings()` triggered automatically when any POOL_SETTINGS_KEY is modified in update_config (`ws_handlers.py:726-731`). Loaded back on startup via `_load_pool_settings()`.

4. **Non-Windows platforms:** The `console_window` flag only affects Windows behavior (`ON_WINDOWS` guard at `async_shell.py:521`). On Linux/macOS it's a no-op — safe to expose as a global setting.

---

## Testing Checklist

- [ ] Toggle ON (default): async shell_cmd spawns visible console window
- [ ] Toggle OFF: async shell_cmd runs silently, no console window spawned
- [ ] Sync shell_cmd: never shows console window regardless of toggle
- [ ] Toggle change persists across page reload and server restart
- [ ] Setting value broadcasts correctly to UI via state/stream_update
- [ ] Toggle during running async shell → only affects new launches, existing tasks unchanged
- [ ] Fresh install (no pool_settings.json): default is True (console window shows)