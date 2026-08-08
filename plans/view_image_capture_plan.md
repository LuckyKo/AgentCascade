# Implementation Plan: view_image Screen Capture Enhancement

## Objective
Extend the `view_image` tool to support special path arguments that trigger screen/window capture instead of reading an image file.

### Supported Directives
- `__screen_capture` — capture the entire screen (all monitors)
- `__window_capture:PID` — capture a specific window identified by its process ID

## Current State

The `ViewImage` tool (`agent_cascade/tools/custom/file_ops.py`) currently:
1. Accepts a `path` parameter
2. Resolves it via `_resolve_path()` 
3. Validates it's an image file
4. Converts SVG to PNG if needed
5. Returns a ContentItem with the file URI for the LLM to view

## Design Decisions

### 1. Architecture: Capture Module Separation

Create a new module `agent_cascade/tools/custom/screen_capture.py` containing capture logic. This keeps concerns separated from file_ops and makes testing easier.

**Rationale:** Screen capture involves platform-specific native APIs (ctypes, win32gui) that are fundamentally different from file operations. Separating allows:
- Independent testing of capture logic
- Easier future enhancements (region capture, etc.)
- Cleaner error handling
- Avoids polluting file_ops.py with platform-specific code

**Module design constraints:**
- Minimal dependencies (only mss + optional pywin32)
- Handles platform detection internally — callers don't need to branch on `sys.platform`
- All functions return PNG bytes or raise descriptive exceptions
- No circular imports with file_ops.py

### 2. Library Selection

**Full Screen Capture — Primary: Pillow ImageGrab, Fallback: mss**
- `PIL.ImageGrab.grab(all_screens=True)` is the primary method:
  - Already a project dependency (Pillow in requirements.txt) — zero new deps
  - Native C implementation on Windows (`grabscreen_win32`): CreateDC("DISPLAY") + BitBlt with DPI awareness
  - Linux X11 support via XCB since Pillow 7.1.0 (check `features.check_feature("xcb")`)
  - Returns PIL Image directly, easy to convert to PNG bytes
- `mss` as fallback for platforms where ImageGrab fails:
  - Pure Python using ctypes — zero external dependencies
  - Cross-platform: Windows (BitBlt), Linux/X11 (XGetImage), macOS (CoreGraphics)
  - Ultra-fast (~60fps on Windows)

**Windows PID-based Capture:** `pywin32` with PrintWindow API
- Required to enumerate windows by PID and capture individual window contents
- Uses native Win32 API: EnumWindows → GetWindowThreadProcessId → **PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)**
- PrintWindow (flag 2) sends WM_PRINT to the application — captures content even if minimized/obscured
- BitBlt from GetWindowDC alone only captures on-screen pixels (fails for occluded/minimized windows)
- Caveat: Returns black for GPU-accelerated/DX12 windows (games, OBS, some Electron apps) — documented limitation

**Linux PID-based Capture:** `mss` region capture + `xdotool` geometry
1. Use `xdotool search --pid` to find window ID
2. Use `xdotool getwindowgeometry` to get window bounds
3. Use `mss.grab()` on the region for pixel capture
4. Return clear error if xdotool not available

**Rationale:** Avoids deprecated ImageMagick `import` command (removed in ImageMagick 7). Uses same mss library as fallback, reducing dependencies and maintaining consistency.

### 3. New Dependencies

| Package | Purpose | Platform | Requirement Format |
|---------|---------|----------|-------------------|
| `mss>=9.0.0` | Full-screen + Linux window capture | All | `mss>=9.0.0` |
| `pywin32>=306` | Windows PID capture | Windows only | `pywin32>=306; sys_platform == 'win32'` |

**Important:** The pywin32 dependency MUST use the platform marker syntax in requirements.txt. Without it, `pip install -r requirements.txt` fails on Linux/macOS because pywin32 has no wheels for those platforms.

Both are well-established, actively maintained packages with minimal footprint.

### 4. Capture Flow

```
view_image(path="__screen_capture") or view_image(path="__window_capture:1234")
    │
    ├─ Detect special directive prefix "__" in path (BEFORE _resolve_path)
    │
    ├─ Validate format:
    │   - "__screen_capture" → valid
    │   - "__window_capture:PID" → validate PID is positive integer
    │   - Anything else starting with "__" → fall through to existing file reading logic
    │
    ├─ Call appropriate capture function:
    │   - screen_capture.capture_screen() → PNG bytes
    │   - screen_capture.capture_window_by_pid(pid) → PNG bytes
    │
    ├─ Write PNG bytes to temp file (same pattern as SVG→PNG conversion)
    │
    └─ Return ContentItem(image=uri) + text confirmation
```

### 5. Error Handling Strategy

Error messages follow existing ViewImage pattern: `return f"ERROR: {description}"`

| Error | Response |
|-------|----------|
| Invalid PID format | `"ERROR: Invalid window capture format. Use __window_capture:PID where PID is a positive integer."` |
| Window not found for PID | `"ERROR: No visible window found for PID {pid}. The process may not have a UI or may be hidden."` |
| Multiple windows for PID (Windows) | `"ERROR: Multiple windows found for PID {pid}. __window_capture returns the first visible window."` (logged warning, proceeds with first) |
| Capture failed — no display / headless | `"ERROR: Screen capture requires a graphical display. No display server detected."` |
| Wayland with no portal support | `"ERROR: Screen capture on Wayland requires xdg-desktop-portal or X11 compatibility. Consider using X11 or installing a portal implementation."` |
| mss not installed | `"ERROR: Screen capture requires the 'mss' package. Install with: pip install mss"` |
| pywin32 not installed (Windows) | `"ERROR: Window capture on Windows requires 'pywin32'. Install with: pip install pywin32"` |
| xdotool not found (Linux window capture) | `"ERROR: Linux window capture requires 'xdotool'. Install via your package manager."` |
| Permission denied / access error | Platform-specific message explaining the limitation |

### 6. Path Prefix Convention

Using `__` prefix (double underscore):
- Unlikely to conflict with real file paths (though technically possible on some systems)
- Matches Python convention for "special/internal" identifiers
- Self-documenting that this is a special directive, not a path

**Edge case:** A user could theoretically create a file named `__screen_capture` or `__window_capture:123`. The plan handles this by checking for exact matches only:
- `"__screen_capture"` (exact) → triggers capture
- `"__screen_capture.png"` or `"__screen_capture_backup"` → falls through to normal file resolution

**Alternative considered:** Using a URI scheme like `capture://screen` or `capture://window:1234`. Rejected because it would require changes to the tool's parameter schema and existing path resolution logic. The prefix approach is simpler and fits naturally into the existing `path` string parameter.

### 7. Security Model

Screen capture has higher security implications than file reading:
- Can capture sensitive data from ANY visible window (password managers, banking apps, etc.)
- Operates at system level, not restricted to workspace paths
- May be subject to privacy regulations in some jurisdictions

**Mitigations implemented:**
1. **Explicit capability documentation:** Tool description clearly states screen capture is available and what it can see. Users/operators must be aware of this capability.
2. **No silent background capture:** Capture only occurs when explicitly invoked via the directive syntax.
3. **Configurable disable option:** A configuration flag `SCREEN_CAPTURE_ENABLED` (default: True) allows operators to disable screen capture entirely.
4. **Logging:** Each screen/window capture is logged with timestamp and directive type for audit trail.

**Note on user confirmation:** Adding an interactive confirmation prompt would break the agent workflow (agents don't have a UI). The trust model relies on:
- The human operator knowing what tools their agents have access to
- Agents only using capabilities they've been instructed to use
- Same fundamental trust as existing `shell_cmd` tool

### 8. Edge Case Handling

| Scenario | Handling |
|----------|----------|
| Headless server (no X display) | Check for DISPLAY env var on Linux; mss/ImageGrab will raise exception → caught and returns descriptive error. Windows session 0 (services) also fails — documented limitation. |
| Window minimized | PrintWindow with PW_RENDERFULLCONTENT captures content even when minimized. If PrintWindow fails, BitBlt fallback may show blank/last frame. |
| GPU-accelerated windows (DX12/games/OBS/Electron) | Documented limitation: PrintWindow returns black for hardware-accelerated content. This is a Windows API restriction, not a bug. |
| Multiple windows per PID | Returns first visible window found by EnumWindows/xdotool. Logs warning if more than one found. |
| Permissions denied (Linux Wayland) | mss/X11 error caught → returns specific error message with instructions |
| `mss` import fails | Lazy import with try/except → clear install instructions returned |
| Window closes during capture | Try/except around capture logic → returns `"ERROR: Window for PID {pid} closed or became inaccessible."` |
| Temp file write fails | Caught in ViewImage.call() finally block → error propagated, cleanup still attempted |

## Implementation Steps

### Step 1: Create screen_capture module
- `agent_cascade/tools/custom/screen_capture.py`
- Functions:
  - `capture_screen() -> bytes` — full screen capture as PNG bytes (cross-platform via mss)
  - `capture_window_by_pid(pid: int) -> bytes` — window capture by PID (platform-specific dispatch)
  - `_capture_window_windows(pid: int) -> bytes` — Windows implementation via pywin32
  - `_capture_window_linux(pid: int) -> bytes` — Linux implementation via mss + xdotool
- Platform detection at module level using `sys.platform`

### Step 2: Modify ViewImage.call()
- At the very start of `call()` (BEFORE `_resolve_path()`), check if path starts with `__`
- Branch on exact directive match:
  - `"__screen_capture"` → call `screen_capture.capture_screen()`
  - `"__window_capture:PID"` → parse PID, validate, call `screen_capture.capture_window_by_pid(pid)`
  - Other `__` prefixed paths → fall through to existing file resolution logic
- Reuse existing temp file + ContentItem pattern exactly as SVG→PNG does:
  - Use `tempfile.mkstemp(suffix='.png', prefix='capture_view_')`
  - Track temp_png in same variable as SVG conversion
  - Cleanup in finally block (already exists)

### Step 3: Update tool description in dna.py
- Extend the view_image description to document the new directives and security implications

### Step 4: Add dependencies to requirements.txt
- `mss>=9.0.0`
- `pywin32>=306; sys_platform == 'win32'`

### Step 5: Write unit tests
- Test directive parsing and routing in ViewImage.call()
- Test error cases (invalid PID, missing window, headless detection)
- Mock the actual capture functions (they require a display)
- Add tests for screen_capture module with mocked mss/pywin32

## Platform-Specific Implementation Details

### Full Screen Capture (Pillow ImageGrab primary, mss fallback)
```python
import io
from PIL import Image


def capture_screen() -> bytes:
    """Capture the entire screen (all monitors combined) as PNG bytes.
    
    Uses Pillow ImageGrab as primary (already a dependency), falls back to mss.
    """
    # Try Pillow ImageGrab first — zero new dependencies
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except (ImportError, OSError) as e:
        # ImageGrab may fail on Linux without XCB or in headless environments
        # Fall back to mss
        pass

    # Fallback: use mss
    try:
        import mss
    except ImportError:
        raise ImportError(
            "Screen capture requires either Pillow with ImageGrab support or 'mss'. "
            "Install mss with: pip install mss"
        )

    with mss.mss() as sct:
        # Monitor 0 is the combined virtual screen (all monitors). 
        # NOTE: monitors[-1] is the LAST physical monitor only — common mistake.
        monitor = sct.monitors[0]
        screenshot = sct.grab(monitor)
        img = Image.frombytes('RGB', screenshot.size, screenshot.bgra, 'raw', 'BGRX')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
```

### Windows PID Capture (pywin32 with PrintWindow — captures minimized/obscured windows)
```python
import sys
import ctypes
import win32gui
import win32ui
import win32con
import win32process
from PIL import Image
import io


def _find_window_by_pid(pid: int):
    """Find the first visible window owned by the given PID."""
    results = []

    def enum_callback(hwnd, _):
        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        if found_pid == pid and win32gui.IsWindowVisible(hwnd):
            results.append(hwnd)

    win32gui.EnumWindows(enum_callback, None)
    return results[0] if results else None


def _capture_window_windows(pid: int) -> bytes:
    """Capture a specific window by PID on Windows using PrintWindow API.
    
    Uses PrintWindow with PW_RENDERFULLCONTENT (flag 2) to capture the actual
    window content even if minimized or obscured. Falls back to BitBlt if
    PrintWindow fails.
    
    Limitation: Returns black for GPU-accelerated/DX12 windows (games, OBS, 
    some Electron apps). This is a Windows API limitation.
    """
    try:
        import win32gui
        import win32ui
        import win32con
        import win32process
    except ImportError:
        raise ImportError(
            "Window capture on Windows requires 'pywin32'. Install with: pip install pywin32"
        )

    hwnd = _find_window_by_pid(pid)
    if not hwnd:
        raise ValueError(f"No visible window found for PID {pid}")

    # Get window dimensions
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = right - left
    height = bottom - top

    if width <= 0 or height <= 0:
        raise ValueError(f"Window for PID {pid} has invalid dimensions ({width}x{height})")

    # Create a memory DC and bitmap to receive the window content
    hdc_window = win32gui.GetDC(hwnd)
    try:
        hdc_mem = win32ui.CreateDCFromHandle(hdc_window)
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(hdc_mem, width, height)
        hdc_mem.SelectObject(bmp)

        # Try PrintWindow first — captures actual window content even when minimized/obscured
        PW_RENDERFULLCONTENT = 2  # Windows 8.1+ flag for full content rendering
        print_window_result = ctypes.windll.user32.PrintWindow(
            hwnd, hdc_mem.GetSafeHdc(), PW_RENDERFULLCONTENT
        )

        if not print_window_result:
            # PrintWindow failed — fall back to BitBlt (only works if window is visible/on-screen)
            hdc_window_dc = win32gui.GetWindowDC(hwnd)
            try:
                hdc_mem.BitBlt((0, 0), (width, height), hdc_window_dc, (left, top), win32con.SRCCOPY)
            finally:
                win32gui.ReleaseDC(hwnd, hdc_window_dc)

        # Extract bitmap bits and convert to PNG
        bmp_bits = bmp.GetBitmapBits(True)
        img = Image.frombytes('RGB', (width, height), bmp_bits, 'raw', 'BGRX')

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    finally:
        # Clean up GDI objects to prevent resource leaks
        win32gui.ReleaseDC(hwnd, hdc_window)
        hdc_mem.DeleteDC()
        bmp.DeleteObject()
```

### Linux PID Capture (mss region + xdotool geometry — replaces deprecated ImageMagick import)
```python
import sys
import subprocess
import re
from PIL import Image
import io


def _capture_window_linux(pid: int) -> bytes:
    """Capture a specific window by PID on Linux using mss + xdotool."""
    try:
        import mss
    except ImportError:
        raise ImportError(
            "Screen capture requires the 'mss' package. Install with: pip install mss"
        )

    # Check for display server
    if not (sys.environ.get('DISPLAY') or sys.environ.get('WAYLAND_DISPLAY')):
        raise RuntimeError(
            "Screen capture requires a graphical display. No display server detected "
            "(DISPLAY and WAYLAND_DISPLAY are unset)."
        )

    # Get window ID from PID using xdotool
    try:
        result = subprocess.run(
            ['xdotool', 'search', '--pid', str(pid)],
            capture_output=True, text=True, timeout=5
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Linux window capture requires 'xdotool'. Install via your package manager "
            "(e.g., sudo apt install xdotool)."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Timeout while searching for window with PID {pid}.")

    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(f"No window found for PID {pid}")

    window_id = result.stdout.strip().split('\n')[0]

    # Get window geometry using xdotool
    try:
        geom_result = subprocess.run(
            ['xdotool', 'getwindowgeometry', '--window', window_id],
            capture_output=True, text=True, timeout=5
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Timeout while getting geometry for window {window_id}.")

    if geom_result.returncode != 0:
        raise RuntimeError(
            f"Failed to get window geometry for PID {pid}: {geom_result.stderr.strip()}"
        )

    # Parse geometry output (e.g., "Geometry of window 0x123456: x=100, y=200, w=800, h=600")
    match = re.search(r'x=(\d+),\s*y=(\d+),\s*w=(\d+),\s*h=(\d+)', geom_result.stdout)
    if not match:
        raise RuntimeError(
            f"Could not parse window geometry from xdotool output: {geom_result.stdout.strip()}"
        )

    x, y, width, height = map(int, match.groups())

    if width <= 0 or height <= 0:
        raise ValueError(f"Window for PID {pid} has invalid dimensions ({width}x{height})")

    # Capture the region using mss
    with mss.mss() as sct:
        monitor = {'left': x, 'top': y, 'width': width, 'height': height}
        try:
            screenshot = sct.grab(monitor)
        except Exception as e:
            raise RuntimeError(
                f"Failed to capture window region for PID {pid}. "
                f"This may be due to Wayland restrictions or missing permissions. Error: {e}"
            )

        img = Image.frombytes('RGB', screenshot.size, screenshot.bgra, 'raw', 'BGRX')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
```

### Platform Dispatch (in screen_capture.py)
```python
import sys


def capture_window_by_pid(pid: int) -> bytes:
    """Capture a specific window by PID. Dispatches to platform-specific implementation."""
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError(f"PID must be a positive integer, got {pid!r}")

    if sys.platform == 'win32':
        return _capture_window_windows(pid)
    elif sys.platform == 'linux':
        return _capture_window_linux(pid)
    else:
        raise RuntimeError(
            f"Window capture is not supported on {sys.platform}. "
            f"Supported platforms: Windows, Linux."
        )
```

## Testing Strategy

### Unit Tests for ViewImage.call() (mocked, no display required)
- `test_screen_capture_directive_parsed` — verifies `"__screen_capture"` routes to capture function
- `test_window_capture_directive_parsed` — verifies `"__window_capture:1234"` extracts PID correctly
- `test_invalid_pid_rejected` — non-numeric and negative PIDs rejected with clear error
- `test_unknown_directive_falls_through` — `"__something_else"` treated as normal file path
- `test_capture_before_resolve_path` — verifies directive check happens BEFORE `_resolve_path()`
- `test_temp_file_cleanup_on_failure` — temp files cleaned up even when capture raises

### Unit Tests for screen_capture module (mocked dependencies)
- `test_capture_screen_uses_mss` — mocks mss, verifies correct monitor selection and PNG output
- `test_capture_window_windows_calls_pywin32` — mocks win32gui/win32ui, verifies GDI flow
- `test_capture_window_linux_calls_xdotool_and_mss` — mocks subprocess + mss, verifies geometry parsing
- `test_headless_detection` — verifies RuntimeError when DISPLAY is unset on Linux
- `test_missing_dependency_messages` — verify ImportError messages include install instructions

### Integration Tests (display required, marked skipif in CI)
- `test_screen_capture_returns_valid_png` — verify output passes PNG header check
- `test_window_capture_known_pid` — capture a known running window (e.g., current terminal via os.getpid())
- `test_multi_monitor_capture` — verify combined monitor capture works

### Testing Notes for CI/CD
- All unit tests run without a display (fully mocked)
- Integration tests use `@pytest.mark.skipif(not has_display(), ...)` or similar
- No test should crash the CI runner on headless environments

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| mss/ImageGrab fails on some Linux/Wayland setups | Medium | Clear error message, document limitation. xdg-desktop-portal integration is future work. |
| pywin32 install issues on non-Windows | Low | Fixed via conditional dependency: `pywin32>=306; sys_platform == 'win32'` |
| Security: agent captures sensitive screen content | Medium | Documented in tool description. Configurable disable option available. Audit logging. |
| Race condition: window closes during capture | Low | Try/except around capture, returns descriptive error message |
| GPU-accelerated windows show black (Windows) | Low | Documented limitation of PrintWindow API. BitBlt fallback captures on-screen pixels if visible. |
| Large screenshots consume context tokens | Medium | Future consideration: add optional resize/compression parameter |

## Success Criteria

1. `view_image(path="__screen_capture")` returns a valid screenshot the LLM can see
2. `view_image(path="__window_capture:PID")` captures the specified window on Windows and Linux
3. Invalid inputs produce clear error messages without crashing
4. Existing file-based view_image behavior is unchanged (backward compatible)
5. No installation failures on Linux/macOS due to pywin32 dependency
6. All new code passes review and testing
7. Headless environments fail gracefully with descriptive errors

## Reviewer Findings Addressed

This revised plan incorporates all findings from the code review:

1. ✅ **Windows capture code rewritten** — Uses correct GDI pattern with proper DC/bitmap selection and cleanup
2. ✅ **Linux capture uses mss + xdotool** — Replaced deprecated ImageMagick `import` with region capture
3. ✅ **Conditional pywin32 dependency** — Uses platform marker: `pywin32>=306; sys_platform == 'win32'`
4. ✅ **Security model expanded** — Added documentation, logging, configurable disable option
5. ✅ **Edge cases handled** — Headless detection, minimized windows, multiple PIDs, permissions documented
6. ✅ **Testing strategy complete** — Unit tests with mocks for screen_capture module added
7. ✅ **Code structure follows patterns** — Directive check before `_resolve_path()`, temp file pattern matches SVG conversion
8. ✅ **Path prefix edge case documented** — Exact match only, partial matches fall through
9. ✅ **Error messages match ViewImage pattern** — All use `"ERROR: {message}"` format
10. ✅ **Import statements included** — All code snippets are self-contained with full imports