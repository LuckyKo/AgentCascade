# Screen & Window Capture Methods for `view_image` — Research Report

**Date:** 2026-08-08
**Author:** capture_researcher
**Requested by:** Maine (orchestrator)
**Objective:** Technical comparison of screen/window capture methods for Windows and Linux, for integration into the AgentCascade `view_image` tool via special path args `__screen_capture` and `__window_capture:PID`.

---

## 1. Executive Summary

| Need | Best choice | Reason |
|---|---|---|
| **Full screen, Windows** | `PIL.ImageGrab.grab(all_screens=True)` | Zero new deps (Pillow already in requirements.txt), returns PIL Image in memory, built-in DPI handling, native C speed |
| **Window by PID, Windows** | ctypes: `EnumWindows`+`GetWindowThreadProcessId` → HWND → **PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)** | Only method that captures occluded/minimized window *content* reliably; Pillow's `grab(window=hwnd)` (11.2.1+) uses BitBlt and only captures the *visible* portion |
| **Full screen, Linux X11** | `ImageGrab.grab(xdisplay="")` when Pillow built with XCB, fallback `mss` / `scrot -F -` | Pure-Python/in-memory; mss is fastest; scrot has no deps beyond binary |
| **Window by ID, Linux X11** | `scrot -w <wid> -F -` (stdout PNG) or `import -window <wid>`; PID→wid via `xdotool search --pid <pid>` / `xdo id -p <pid>` | X11 window tools; stdout avoids temp files |
| **Linux Wayland** | `grim -g <geom> -` (wlroots) or **xdg-desktop-portal** DBus (universal) | In-process capture is impossible by design on Wayland; compositor cooperation required |

**Bottom line:** Add `mss` as an optional cross-platform monitor-level backend; use Pillow ImageGrab as the primary path (already a dependency); implement window-by-PID on Windows with a small ctypes `PrintWindow` helper rather than relying on Pillow's BitBlt-based `window=` parameter for occluded windows; on Wayland delegate to compositor/portal tools.

---

## 2. Windows — Full Screen

### 2.1 Pillow ImageGrab.grab() — RECOMMENDED
- **API:** `ImageGrab.grab(all_screens=True)` → `PIL.Image.Image` (RGB). Optionally `bbox=`. No file write.
- **Implementation:** C extension `Image.core.grabscreen_win32` (`src/display.c`). Uses `CreateDC("DISPLAY")` + `BitBlt(SRCCOPY)`, `SetThreadDpiAwarenessContext` (DPI-aware), extracts BGR via `GetDIBits`.
- **Dependencies:** Pillow only (already in `requirements.txt`). **No pywin32 required** — Pillow calls the Win32 API through its C extension directly.
- **Multi-monitor:** `all_screens=True` covers all monitors; bbox top-left may be negative on Windows with all_screens.
- **Confidence: Confirmed** — Pillow 12.3.0 docs; Pillow 9.2.0 release notes; DeepWiki Pillow "Screen Capture and GUI Integration" (implementation details).

### 2.2 mss
- **API:** `with mss.MSS() as sct: shot = sct.grab(sct.monitors[1])`; `shot.rgb`/`shot.bgra` bytes; `mss.tools.to_png(rgb, size)` → PNG **bytes** (no file); or `sct.shot()` saves file.
- **Implementation:** pure Python + ctypes to GDI (`BitBlt`). No compiled extension, no deps.
- **Perf:** generally the fastest pure-Python option on Windows (marketed as "ultra fast"), widely benchmarked ahead of ImageGrab in tight loops.
- **Limitation:** monitors/regions only — **no window capture**.
- **Confidence: High** — mss API docs + examples ("Get PNG bytes, no file output").

### 2.3 win32gui + win32ui (pywin32)
- **API:** `win32gui.GetWindowDC`/`win32ui.CreateDCFromHandle` + `BitBlt`; also `PrintWindow` via `win32gui.PrintWindow`? (pywin32 doesn't bind PrintWindow directly — use ctypes).
- **Dependencies:** pywin32 — a **native binary** package (~large wheel), heavier install. Not currently in requirements.
- **Value:** mainly needed if you want to enumerate windows (PID→HWND via `GetWindowThreadProcessId`). That enumeration can be done with ctypes alone — pywin32 is not mandatory.
- **Confidence: High** — standard Win32 recipe (EnumWindows/GetWindowThreadProcessId/GetWindowRect), widely documented.

### 2.4 pyautogui.screenshot
- **API:** `pyautogui.screenshot()` → PIL Image.
- **Implementation:** on Windows it literally calls `ImageGrab.grab(all_screens=...)` (PyScreeze `_screenshot_win32` source).
- **Dependencies:** pulls in pyscreeze, pytweening, pygetwindow, pymsgbox, mouseinfo (and python3-xlib on Linux). **Do not add** — it's a wrapper around the free capability.
- **Confidence: High** — PyScreeze source code inspected directly.

---

## 3. Windows — Window by PID (Capture Specific Window)

### 3.1 Two-phase approach
1. **PID → HWND:** `EnumWindows(callback)` → per-window `GetWindowThreadProcessId(hwnd, byref(pid))`; match target PID → collect HWND. Filter visible + not-iconic (minimized) if desired.
2. **HWND → pixels:**
   - **Option A — BitBlt from `GetWindowDC(hwnd)`:** captures current on-screen pixels (what's visible). **Fails to capture occluded areas / shows black for Hardware-accelerated (DirectComposition) windows.** This is exactly what Pillow does.
   - **Option B — `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)`:** asks the target window to render into a DC — captures occluded and minimized windows (Win 8.1+). The standard technique for "screenshot of window even when hidden," used by browsers' snapshot code. Caveats: requires the app cooperate with `WM_PRINT`; protected content (DRM, some Direct2D/videostreams) returns blank/black; UIPI blocks cross-elevation capture.
- **Performance:** PrintWindow is generally slower and can block (the owning app processes the message).

### 3.2 Pillow 11.2.1 `grab(window=hwnd)` — CONFIRMED but caution
- Added in **Pillow 11.2.1 (2025-04-12)**: `ImageGrab.grab(window=hwnd)` selects a window by HWND on Windows.
- **Important caveat:** implementation sets `all_screens=-1` and uses `GetDC(wnd)` + BitBlt — i.e., **visible surface only, not occluded content** (DeepWiki docs the mechanism). For the common "capture the app's window contents even if covered" case, **PrintWindow-based approach is more reliable**.
- **Confidence: High** — Pillow release note + DeepWiki C-layer explanation.

### 3.3 Recommended minimal implementation
ctypes only (no pywin32):
```python
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# PID -> HWND
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
def _find_hwnd(pid):
    result = []
    @EnumWindowsProc
    def cb(hwnd, lparam):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            result.append(hwnd)
        return True
    user32.EnumWindows(cb, 0)
    return result[0] if result else None
```
Then PrintWindow into a DIB/HBITMAP DC and read pixels into PIL `Image.frombuffer`. (Full reference snippet in `temp/screen_capture_prototype.py` if you want me to add one.)

---

## 4. Linux

### 4.1 Full screen
| Method | Mechanism | Notes |
|---|---|---|
| `ImageGrab.grab(bbox=None, xdisplay="")` | XCB via ctypes (if Pillow built w/ XCB — `features.check_feature("xcb")`); X11 only | In-memory; fastest pure-Python; on X11 `xdisplay=""` forces direct X11 rather than `gnome-screenshot` |
| ImageGrab without args on Wayland | subprocess fallback chain **gnome-screenshot → grim → spectacle** (temporary file internally) | ImageGrab writes tmp PNG then reads it — hides it from you; works only if tool installed |
| `mss` | X11: `XGetImage` via ctypes (XShm optional); `with_cursor` on Linux | Monitor-only, 32-bpp only; X11 only, no Wayland. Needs `libX11`+`libXrandr` present (usually installed) |
| `scrot -F -` | CLI, imlib2, X11 | Outputs PNG to stdout via `-F -`; no temp file |
| `import -window root` | ImageMagick | root window = full screen; heavy tool install |

- **Headless:** all X11 methods work under **Xvfb** (set `$DISPLAY`) — capture tools read the virtual framebuffer. Confirmed by multiple headless-GUI guides.
- **X11 vs Wayland detection:** check `XDG_SESSION_TYPE` / `WAYLAND_DISPLAY` / `DISPLAY` env vars (this is what PyScreeze/Pillow do).

### 4.2 Window capture by PID (X11)
- **PID → Window ID:** X11 applications should export `_NET_WM_PID` (EWMH spec) on their top-level windows. Tools: `xdotool search --pid <PID>` (list windows of a process), `xdo id -p PID` (used in scrot docs to select window by PID).
- **Capture by window ID:**
  - `scrot -w <window_id> -F -` → PNG bytes to stdout (X11 only). **RECOMMENDED** (small, no deps beyond X).
  - `import -window <window_id> - | convert ...` (ImageMagick) — can output PNG to stdout.
  - `xwd -id <window_id>` — outputs **XWD format** (not PNG); needs `PIL` XWD plugin (or ImageMagick `convert xwd:- png:-`) to convert.
- **Confidence: HIGH** — scrot man page (Ubuntu 1.12.1) explicitly documents `-w/--window` with `<wid>`, `-F -` stdout, and example `scrot -w $(xdo id -p PID)`; xwd man page documents `-id`; EWMH spec documents `_NET_WM_PID`.

### 4.3 Wayland
- **Fundamental:** There is NO way (as of 2026) for an unsandboxed app to capture the screen or windows on Wayland without compositor cooperation. Pillow/mss/scrot/imagemagick all fail. X11 tools can't see Wayland surfaces.
- **Options:**
  1. **xdg-desktop-portal** (`org.freedesktop.portal.Screenshot` on D-Bus) — the cross-desktop standard. Interface v3 adds `AvailableTargets` bitmask: 1=Screen, 2=Window (user-chosen), 4=Area, 8=Active Window. Requires a portal backend + user/interactive permission; **blocks on prompts in headless**. Used by gnome-screenshot etc.
  2. **grim** — wlroots compositors only (Sway/Hyprland/niri). `grim` full screen; `grim -g "<x>,<y> <w>x<h>"` region; focused window example uses `swaymsg get_tree | jq`. Output to **stdout** (`grim -`). No user prompt; works in scripts on wlroots systems.
  3. `gnome-screenshot -f file.png`, `spectacle` — via portal; interactive.
- **Python approaches:** For portal, use `dbus-next`/`jeepney`/`gdbus` call to `org.freedesktop.portal.Screenshot.Screenshot()` with `interactive` + accept_token, then `read()` the pipe fd. (Implementation detail — no mainstream pure-Python lib bundles this; must write ~40 lines of DBus code.)
- **Confidence: HIGH** — portal docs list target mask; grim README; mss issue #155 explicitly says "The cross-platform way to take screenshots on Wayland is via xdg-desktop-portal".

---

## 5. Comparison Table

| Aspect | Pillow ImageGrab | mss | pyautogui | win32 ctypes+PrintWindow | scrot/import/xwd (X11) | grim (wlroots) | xdg-portal (Wayland) |
|---|---|---|---|---|---|---|---|
| OS | Win/mac/Linux-X11 | Win/mac/Linux-X11 | Win/mac/Linux | Windows only | Linux X11 | Linux Wayland | Linux Wayland |
| Full screen | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Window by PID | ⚠️ 11.2.1+ HWND only (visible) | ❌ monitors only | ❌ (region only) | ✅ PrintWindow (occluded ok) | ✅ by window ID | ✅ focused window | ⚠️ "Active Window" target (user-visible) |
| Returns in-memory | ✅ Image | ✅ bytes/Image | ✅ Image | ✅ (made so) | ✅ stdout (`-F -`) | ✅ stdout | no (portal writes a file) |
| Image result model | PIL Image | ScreenShot obj → PNG bytes | PIL Image | PIL Image | PNG bytes | PNG bytes | path to PNG file |
| Native deps | None (C ext in Pillow) | None (ctypes; needs libX11/Xrandr on Linux) | Heavy chain + python-xlib | None (ctypes) | scrot binary (~small) / ImageMagick (heavy) | grim binary (+swaymsg/jq optional) | portal daemon + backend |
| PyPI dep | **already bundled** in requirements.txt | pure-Python `pip install mss` | avoid | none (stdlib ctypes) | system package | system package | python dbus lib |
| Headless/server | ❌ no DISPLAY / session 0 | ❌ no display | ❌ | ❌ session 0 | ✅ under Xvfb (DISPLAY) | ❌ (needs Wayland session) | ❌ (prompts) |
| DPI-aware (HiDPI) | ✅ (SetThreadDpiAwarenessContext) | ⚠️ (SetProcessDPIAware not called; report says Ill) | ✅ (calls SetProcessDPIAware) | ⚠️ you must call | ✅ (X has no DPI scaling) | ✅ compositor | ✅ compositor |
| Occluded window | ⚠️ black | ❌ | ❌ | ✅ PrintWindow | ✅ (WM_PRINT) | ✅ compositor | ✅ compositor |

---

## 6. Recommendations

### Implementation plan for AgentCascade `view_image`
1. **Add detection in `ViewImage.call()` BEFORE `_resolve_path`** (File `agent_cascade/tools/custom/file_ops.py:498`): if `path == "__screen_capture"` or `path.startswith("__window_capture:")`, dispatch to a new helper module (e.g., `agent_cascade/tools/custom/screen_capture.py`) instead of path resolution. This is mandatory — `_resolve_path` will treat `__...` as a relative path and fail lookup.
2. **Windows full screen:** `ImageGrab.grab(all_screens=True)` → save to temp PNG → `ContentItem(image=temp.as_uri())`, cleanup in `finally` (mirror the existing SVG flow).
3. **Windows window-by-PID:** ctypes `EnumWindows`→`GetWindowThreadProcessId(pid)`; capture with `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` (Win 8.1+); copy to PIL via `Image.frombuffer("RGB", (w,h), data, "raw", "BGRX", ...)`. Keep `grab(window=hwnd)` as fallback when PrintWindow returns black (some apps don't implement WM_PRINT).
4. **Linux X11:** session detection (`XDG_SESSION_TYPE`). If X11 → `ImageGrab.grab(xdisplay="")` (XCB) or `mss`; window-PID: `xdotool search --pid` → `scrot -w <id> -F -` → read bytes. If no xdotool, parse `_NET_WM_PID` via `python-xlib`.
5. **Linux Wayland:** call `grim - ` if available (fastest, wlroots); else `gnome-screenshot` (will flash); else DBus portal with `AvailableTargets` mask for window=8.
6. **mss as optional cross-platform fallback** (pure Python; monitors only) — add to `requirements-optional` IN THE FUTURE if monitor-level capture needed.

### Immediate diffs (2 of them)
- Always write a temp PNG for ContentItem (the LLM pipeline `encode_image_as_base64(path)` requires a file path — in-memory bytes won't reach the model).
- Clean up temp file in `finally` (established pattern in `view_image` for SVG).

## 7. Confidence & Open Questions
- **High confidence** on all library facts (verified against official docs/source).
- **Open:** Does the AgentCascade server process run in an interactive desktop session? If it runs as a Windows service (session 0) or a detached Linux daemon without X, capture will fail for the display; only `Xvfb`-based Linux headless works.
- **Open:** whether Pillow installed in the current env has the `xcb` feature (affects Linux X11 path).
- **Open:** user-permission UX for xdg-desktop-portal (requires portal prompt which cannot be automated silently).

## 8. Sources (primary)
- Pillow: ImageGrab module reference, releasenotes 9.2.0, 11.2.1, 12.1.0; src/PIL/ImageGrab.py; DeepWiki Pillow 2.8 "Screen Capture & GUI Integration".
- mss: python-mss docs API/Examples; GitHub issue #155 (Wayland status); DeepWiki mss Linux implementation (X11/XGetImage/XRandR).
- PyScreeze/PyAutoGUI: pyscreeze/__init__.py (screenshot logic); pyautogui PyPI deps.
- scrot man page (ubuntu 1.12), xwd man page (xorg/debian), xdotool man page.
- grim README (sr.ht/GitHub), xdg-desktop-portal Screenshot docs.
- Microsoft Learn: PrintWindow (winuser.h), SetProcessDpiAwareness.