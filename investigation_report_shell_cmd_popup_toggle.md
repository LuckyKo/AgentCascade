# Investigation Report — shell_cmd Approval Popup & Window Behavior

**Task:** todo.md line 85 — "Add UI toggle for shell_cmd window popup, ensure popups don't steal focus"
**Date:** 2026-08-06
**Investigator:** researcher (investigator_shell_ui)
**Status:** Architecture mapping complete — ready for implementation planning

---

## Executive Summary

There are **two distinct "popups"** related to shell_cmd, and they must not be conflated:

1. **The Approval Bar** (in-page HTML element) — the interactive Approve/Reject UI. Rendered inline at the top of the page (NOT a modal/overlay). It does not use `window.open`, does not trap focus, and does not call `window.focus()`. It only calls `scrollIntoView()` on show, which can scroll the page but does not steal keyboard focus.

2. **The Windows Console Window** (OS-level) — the **async shell_cmd** feature pops a real OS console window via `CREATE_NEW_CONSOLE` for "user inspection" (marked `TODO #21` in `async_shell.py`). This is the "shell_cmd window popup" that todo #85 almost certainly refers to. It is **hardcoded to `True`** with **no existing UI toggle or config** — this is what needs a toggle.

The focus-steal concern applies to:
- The console window appearing on the desktop (steals OS-level focus when a new window pops up — inherent to `CREATE_NEW_CONSOLE`).
- The approval bar's `scrollIntoView` (minor, page-scroll only, no keyboard focus steal).
- The reject-reason input auto-focus (`showRejectInput` at `app.js:3237`) — only happens when the user clicks Reject, so not a "steal".

---

## 1. Where the shell_cmd approval popup is triggered and rendered

### Backend — approval creation (blocking call)
| Path | Line(s) | Role |
|---|---|---|
| `agent_cascade/operation_manager/shell.py` | `:378-418` (`execute_shell_command`) | **Sync shell_cmd**: after safe-command auto-approval check (`:396`), builds description and calls `self.request_user_approval(...)` at `:409-414` with `tool_name='shell_cmd'` |
| `agent_cascade/tools/custom/shell_cmd.py` | `:223-243` (`_launch_async`) | **Async shell_cmd**: same approval gate at `:235-240` (`tool_name='shell_cmd'`, adds `'async_mode': True` to tool_args) |
| `agent_cascade/operation_manager/approval.py` | `:84-152` (`request_user_approval`) | Creates `PendingApproval`, stores in `self.pending[request_id]`, **blocks the calling thread** on `threading.Event` polling at 0.1s (`:132`) |
| `agent_cascade/operation_manager/approval.py` | `:154-178` (`user_approve` / `user_reject`) | Called by WebUI to resolve; pops entry and sets event |
| `agent_cascade/operation_manager/approval.py` | `:180-194` (`list_pending_approvals`) | Serializes pending list for the UI poll |

**Other approval producers using the same subsystem** (for comparison in Q5): `file_operations.py` (`write_file` :496, `edit_file` :1019, `re_indent` :1334, `delete_file` :1441, `copy_file` :1519, `move_file` :1599), `compression/handler.py:883` (`compress_context`), `tools/custom/propose_skill.py:149` (`propose_skill`), plus the SecurityAdvisor auto-approve path (`security_handler.py:568-598`).

### Backend — serving to the frontend
| Path | Line(s) | Role |
|---|---|---|
| `agent_cascade/api_server.py` | `:359-362` (`get_approvals()`) | Reads `operation_manager.list_pending_approvals()` |
| `agent_cascade/api_server.py` | `:670-688` (`_approval_loop`) | **Async poll loop**: every 0.3s checks for new/resolved approval IDs, broadcasts `{'type': 'approvals', 'approvals': pending}` via send_queue |
| `agent_cascade/api_server.py` | `:839-844` (`POST /api/approve/{request_id}`) | REST approve endpoint |
| `agent_cascade/api_server.py` | `:846-851` (`POST /api/reject/{request_id}`) | REST reject endpoint |
| `agent_cascade/ws_handlers.py` | `:958-966` (`handle_approve`), `:968-977` (`handle_reject`) | WebSocket approve/reject (frontend's primary path); broadcasts full state after |
| `agent_cascade/api_integration.py` | `:802-803`, `:875`, `:1051` | `'approvals'` key included in `build_state_from_pool` / streaming state (always present — see fix in `.agent_lessons/approval-modal-reopens-fix.md`) |
| `agent_cascade/security_handler.py` | `:568-569`, `:596-597` | Security advisor broadcasts updated approval lists after auto-apply/reject |

### Frontend — rendering
| Path | Line(s) | Role |
|---|---|---|
| `web_ui/index.html` | `:151-152` | `<div class="approval-bar" id="approvalBar" style="display:none;">` — the popup container (inline area, not a modal) |
| `web_ui/app.js` | `:348` | `const approvalBar = $('#approvalBar');` |
| `web_ui/app.js` | `:3044-3205` (`renderApprovals()`) | Builds `.approval-card` per pending approval; hides bar when empty (`:3061-3068`); auto-security/AFK early-return paths (`:3084-3117`); snapshot-based render skip (`:3120-3126`); **`bar.scrollIntoView({behavior:'instant', block:'start'})` at `:3131-3134`**; card markup with inline onclick handlers (`:3185-3201`) |
| `web_ui/app.js` | `:3207-3214` (`approveRequest`) | Optimistic local removal + `send({type:'approve',...})` |
| `web_ui/app.js` | `:3216-3223` (`askSecurity`) | Sends `{type:'ask_security', auto_apply:false}` |
| `web_ui/app.js` | `:3225-3238` (`showRejectInput`) | **`area.querySelector('input').focus()` at `:3237`** — focuses reject-reason input |
| `web_ui/app.js` | `:3240-3248` (`rejectRequest`) | Optimistic local removal + `send({type:'reject',...})` |
| `web_ui/app.js` | `:1626-1629`, `:1926-1929`, `:2017-2023` | `'approvals'` array handling in state / stream_update / approvals message + `renderApprovals()` |
| `web_ui/styles.css` | `:1500-1529` (`.approval-bar`) | Inline flex column, `position:relative`, `z-index:90`, `max-height:45vh`, slide-down animation |
| `web_ui/styles.css` | `:1531-1625` (`.approval-card`, `.approval-actions`, `.reject-input-area`, etc.) | Card styling |

### The console window (the actual "shell_cmd window popup")
| Path | Line(s) | Role |
|---|---|---|
| `agent_cascade/tools/custom/shell_cmd.py` | `:133-144` (`_launch_async` branch in `call()`) | Async mode routes to `_launch_async` |
| `agent_cascade/tools/custom/shell_cmd.py` | `:254-260` | `tracker.launch(...)` — **no `console_window` argument passed**, so the tracker default is used |
| `agent_cascade/async_shell.py` | `:155` | `console_window: bool = True  # Pop console window (TODO #21)` — **hardcoded default True** |
| `agent_cascade/async_shell.py` | `:266-267`, `:279`, `:315` | `launch(..., console_window=True)` → stored on `AsyncShellTask` |
| `agent_cascade/async_shell.py` | `:470-472`, `:487` | `configure_windows_utf8(command, create_new_console=task.console_window)` |
| `agent_cascade/async_shell.py` | `:514-534` | **Viewer process**: spawns secondary `cmd.exe /c` with `CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP` and no piped stdio so output is visible in its own window |
| `agent_cascade/async_shell.py` | `:813-...` (`_kill_viewer_process`) | Cleanup of the viewer window on kill/timeout |
| `agent_cascade/shell_utils.py` | `:77-90` (`configure_windows_utf8`) | `if create_new_console: flags |= subprocess.CREATE_NEW_CONSOLE` |
| `agent_cascade/operation_manager/shell.py` | `:295-301` | **Sync shell** passes `create_new_console=False` — sync shell_cmd does NOT pop a console window |

---

## 2. How approvals are currently displayed (modal/dialog mechanism)

**There is no modal/overlay.** The approval UI is an **inline expanding bar** rendered at the top of the page layout, directly below the main tab bar (`index.html:151-152`), styled as a flex column of "cards".

- Show/hide: `bar.style.display = 'block'` / `'none'` (`app.js:3064`, `:3128`).
- Each approval = `.approval-card` with: tool icon + tool name + agent label (`:3186-3190`), justification block (`:3191`), full tool-args JSON in a `<pre>` (`:3192`), optional security-expert response box (`:3193`), and Approve / Ask Security / Reject buttons (`:3194-3198`).
- Reject expands an inline input row inside the card (`:3225-3238`).
- Positioning: `position: relative; z-index: 90; max-height: 45vh; overflow-y: auto` (`styles.css:1500-1519`) — it pushes content down / scrolls, it does not overlay or trap focus.
- The bar has an entrance animation `slide-down` (`styles.css:1521-1524`) guarded by `animation-fill-mode: both` to prevent replay flicker (see `.agent_lessons/auto_ask_flash_bug_investigation.md` for the historical flash bug).

---

## 3. Existing UI settings/toggles usable as a pattern

The **approval-timeout toggle** is the canonical full-stack pattern. The **Auto Agent Tab Focus** toggle is the best pattern for a *frontend-behavior* toggle (it changes UI behavior without backend config).

### Pattern A — Backend-persisted toggle (approval timeout): full chain
1. **HTML control** — `web_ui/index.html:408-411`:
   ```html
   <label class="setting-field toggle-field">
     <span>Enable Approval Timeout</span>
     <input type="checkbox" id="settingApprovalTimeoutEnabled" checked />
   </label>
   ```
2. **Element reference** — `web_ui/app.js:637-638`.
3. **Local save (localStorage)** — `web_ui/app.js:973-975` in `saveSettings()`.
4. **Local load** — `web_ui/app.js:1174-1180` in `loadSettings()`.
5. **Server send** — `web_ui/app.js:4699-4700` in `getGenerateCfg()` → `send({type:'update_config', generate_cfg})` (`app.js:1018-1019`).
6. **Server→UI sync map** — `web_ui/app.js:176-177` (`POOL_SETTINGS_MAP`, consumed by `syncPoolSettings()` at `:192-240`; respects localStorage precedence at `:204`).
7. **Backend handler** — `agent_cascade/config_handlers.py:211-221` (`_handle_approval_timeout`) and `:224-234` (`_handle_enable_approval_timeout`), both `@register_config_handler(...)` decorated.
8. **Persistence key set** — `agent_cascade/config_handlers.py:49-50` (`POOL_SETTINGS_KEYS` includes `'approval_timeout_seconds', 'enable_approval_timeout'`). `ws_handlers.py:726-731` triggers `agent_pool._save_pool_settings()` when any such key is present in `update_config`.
9. **Pool load/persist** — `agent_cascade/agent_pool.py:357-407` (`_save_pool_settings`), `:408-515` (`_load_pool_settings`), `:545-552` (`_apply_pending_config` restores approval timeout on the operation manager).
10. **State broadcast to UI** — `agent_cascade/api_integration.py:847-851` reads `om.approval_timeout_seconds` / `om.enable_timeout` into `pool_settings`.

### Pattern B — Frontend-only toggle (Auto Agent Tab Focus)
- HTML: `index.html:373-376` (`#setting-auto-tab-focus`).
- JS ref: `app.js:616`; save `app.js:943-944`; load `app.js:1074-1077`; use `app.js:1960, 1965, 1988` (guards whether tabs auto-switch). No backend involvement.

### Pattern C — Dedicated WS message toggle (Auto-Security) — closest to "change behavior + persist"
- HTML: `index.html:222-227` (`#autoSecurityToggle`).
- JS handler: `app.js:1391-1408` — on change: guard against server sync (`THROTTLE.AUTO_SECURITY_TOGGLE_GUARD`), `saveSettings()`, `send({type:'set_auto_security', enabled})`, then `renderApprovals()`.
- Backend: `ws_handlers.py:996-1008` (`handle_set_auto_security`) stores on `app.current_auto_security`, syncs `agent_pool._loaded_auto_security`, calls `_save_pool_settings()`.
- Persistence: `config_handlers.py:71` (`EXTRA_PERSIST_KEYS` includes `'auto_security'`), loaded at `agent_pool.py:433, 456-458`, applied at `api_server.py:224` (`app.current_auto_security`), broadcast to UI at `api_server.py:414, 457`.

**For the shell console-window toggle, Pattern A (or C) fits:** the value affects backend process-spawning behavior, so it should be persisted (either as a pool setting or an EXTRA_PERSIST_KEY) and threaded down to `async_shell`'s `console_window` parameter.

---

## 4. Focus handling of the popup

### Approval bar — focus behavior
- **No `window.focus()`, no `showModal()`, no focus trap.** The bar is a plain in-page element.
- The only focus-affecting calls in the approval path:
  - `app.js:3133` `bar.scrollIntoView({behavior:'instant', block:'start'})` — scrolls page to top. **Does not** move keyboard focus, but can visibly yank the user's viewport away from where they were (relevant to "shouldn't steal focus" perception).
  - `app.js:3237` `area.querySelector('input').focus()` — only when the user clicks Reject; intentional, not a steal.
- Global focus-preservation guard already exists: `app.js:4488-4501` (commit `b18e986`) — clicking anywhere in `.input-area` restores focus to `#chatInput` via `setTimeout(...,0)` unless clicking a checkbox. This prevents the chat input losing focus during UI interactions.
- `renderSubAgents()` preserves `#chatInput` focus/cursor across DOM rebuilds (`app.js:3255-3264`, `:3409-3416`).
- **Gap:** `renderApprovals()` rebuilds `bar.innerHTML` and calls `scrollIntoView` unconditionally on every show — if the user is typing/reading lower in a long conversation, the page jumps to top. There is no check for "is the user actively typing" before scrolling (contrast with the focus guards used elsewhere).

### Console window — focus behavior
- **Inherent OS-level focus steal:** spawning `CREATE_NEW_CONSOLE` (`shell_utils.py:87-89`) and the viewer `cmd.exe /c` (`async_shell.py:528-534`) opens a new native window which takes OS focus on Windows. This is the real "popup steals focus" complaint for async shell_cmd.
- There is **no suppression path** currently — `console_window` is hardcoded `True` (`async_shell.py:155`) and `_launch_async` never passes it (`shell_cmd.py:254-260`).

---

## 5. Existing differentiation between approval types in display

**Yes — partial differentiation exists, purely cosmetic:**

1. **Tool icon map** — `app.js:3176-3183`: `toolIconMap` gives per-tool icons (`'shell_cmd': '⚙️'`, `'write_file': '📝'`, etc.), fallback `'🛠️'`.
2. **Tool name + agent label** — `app.js:3188-3189` displays `ap.tool_name` and `ap.agent_name` on every card.
3. **Description text** — the backend builds different `description` strings per tool (shell_cmd gets the SECURITY WARNING preamble, e.g. `shell.py:403-407`), and the frontend shows `justification` prominently (`app.js:3160-3173`).
4. **Security advisor integration** — *any* approval card can invoke "Ask Security" (`app.js:3196`), not shell-specific.

**There is NO behavioral/conditional differentiation** in rendering by tool type — every pending approval is rendered identically by `renderApprovals()`. There is no code that treats `tool_name === 'shell_cmd'` specially in the frontend. The backend's `ALL_USER_APPROVAL_TOOLS` set (`constants.py:18-24`) is used for **agent tool policy** (which tools require approval / are disabled for new agents), not for UI display branching.

---

## Key Findings for the Implementation Plan

1. **The "shell_cmd window popup" = the Windows console window spawned by async shell_cmd**, hardcoded on in `async_shell.py:155` (`console_window: bool = True`, TODO #21). Sync shell_cmd (`operation_manager/shell.py:301`) never pops a window.
2. **No toggle exists anywhere** for console_window — no config key, no POOL_SETTINGS_MAP entry, no HTML control.
3. **The approval bar is not a modal and does not steal keyboard focus**; its only page-jump behavior is `scrollIntoView` at `app.js:3133`. If todo #85 also covers this, a simple guard (skip scroll when user is typing / when activeElement is an input/textarea) suffices.
4. **Threading the toggle**: `_launch_async` (shell_cmd.py:133) → `tracker.launch` (async_shell.py:266, param at :279) → `AsyncShellTask.console_window` (:155) → `configure_windows_utf8` (shell_utils.py:77-90) and viewer spawn (:528). A single flag needs to flow through these three layers.
5. **Persistence wiring** should mirror `enable_approval_timeout` (Pattern A): HTML toggle → saveSettings/getGenerateCfg → config_handlers handler → POOL_SETTINGS_KEYS → `_save_pool_settings`/`_load_pool_settings` → pool_settings broadcast → POOL_SETTINGS_MAP sync. Or mirror `auto_security` (Pattern C) with a dedicated WS message if instant app-wide effect + persistence is desired.
6. **Prior art to respect**: `.agent_lessons/approval-modal-reopens-fix.md`, `approval-modal-reopens-root-cause.md`, `auto_ask_flash_bug_investigation.md`, `architecture_report_shell_cmd.md` — all document fragile approval UI state sync; any change to `renderApprovals()` must keep the snapshot-skip and `Array.isArray` guards intact.

---

## Confidence Level
**High** — all findings verified against current source (line numbers from working tree, 2026-08-06).

## Open Questions / Unknowns
- Whether the UI toggle should be per-session only (frontend) or persisted in `pool_settings.json` (recommended: persisted, Pattern A/C).
- Whether `console_window=False` should also suppress the viewer process on non-Windows (it's Windows-only code path anyway; the field is on all platforms but only used under `ON_WINDOWS`).
- Whether the todo intends to also suppress the approval-bar `scrollIntoView` jump (separate, small change in `renderApprovals`).

## Suggested Next Actions
1. Confirm with Maine whether "shell_cmd window popup" = async console window (recommended interpretation) or the approval bar.
2. Add `console_window` setting via Pattern A: HTML toggle in "Approvals & Timeouts" section (`index.html:404-415`), JS wiring (4 touch points), `config_handlers` handler, POOL_SETTINGS_KEYS, `api_integration.py` broadcast, POOL_SETTINGS_MAP entry.
3. Thread flag: `_launch_async` reads from `agent_pool.operation_manager` (or pool settings) and passes `console_window` to `tracker.launch(...)`.
4. Optional focus fix: guard `bar.scrollIntoView` in `renderApprovals()` when `document.activeElement` is an input/textarea.
5. Test matrix: sync shell (no window regardless), async shell on/off, Windows viewer process kill on timeout.

## Deliverables
- This report: `investigation_report_shell_cmd_popup_toggle.md`
- Memory saved: `.agent_lessons/shell_cmd_window_popup_architecture.md`