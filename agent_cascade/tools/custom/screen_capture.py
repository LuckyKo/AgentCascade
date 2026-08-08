"""Screen and window capture utilities for the view_image tool.

Provides platform-specific screen capture via Pillow ImageGrab (primary) and mss
(fallback), plus PID-based window capture using pywin32 (Windows) or xdotool+mss
(Linux).

All functions return PNG bytes or raise descriptive exceptions.
"""

import io
import logging
import os
import re
import subprocess
import sys
from PIL import Image

logger = logging.getLogger(__name__)


def capture_screen(monitor_index: int | None = None) -> bytes:
    """Capture the screen as PNG bytes.

    Args:
        monitor_index: If None, captures all monitors combined (default).
                       If set to a non-negative int, captures only that monitor.

    Returns:
        PNG bytes of the captured screen/monitor.

    Raises:
        ValueError: If monitor_index is out of range.
        ImportError: If neither ImageGrab nor mss is available.
    """
    # When a specific monitor is requested, skip ImageGrab (doesn't support per-monitor) and use mss directly
    if monitor_index is not None:
        return _capture_screen_mss(monitor_index)

    # Try Pillow ImageGrab first — zero new dependencies
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except (ImportError, OSError):
        # ImageGrab may fail on Linux without XCB or in headless environments
        # Fall back to mss
        pass

    # Fallback: use mss with monitor 0 (combined virtual screen)
    return _capture_screen_mss(0)


def _capture_screen_mss(monitor_index: int) -> bytes:
    """Capture the screen using mss.

    Args:
        monitor_index: Index into sct.monitors list. 0 is combined virtual screen,
                       1+ are individual monitors.

    Returns:
        PNG bytes of the captured screen/monitor.

    Raises:
        ValueError: If monitor_index is out of range.
        ImportError: If mss is not installed.
    """
    try:
        import mss
    except ImportError:
        raise ImportError(
            "Screen capture requires the 'mss' package. Install with: pip install mss"
        )

    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor_index < 0 or monitor_index >= len(monitors):
            raise ValueError(
                f"Monitor index {monitor_index} is out of range. "
                f"Available monitors: 0 (combined), {', '.join(str(i) for i in range(1, len(monitors)))}"
            )
        monitor = monitors[monitor_index]
        screenshot = sct.grab(monitor)
        img = Image.frombytes('RGB', screenshot.size, screenshot.bgra, 'raw', 'BGRX')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()


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


def _capture_window_windows(pid: int) -> bytes:
    """Capture a specific window by PID on Windows using PrintWindow API.

    Uses PrintWindow with PW_RENDERFULLCONTENT (flag 2) to capture the actual
    window content even if minimized or obscured. Falls back to BitBlt if
    PrintWindow fails.

    Limitation: Returns black for GPU-accelerated/DX12 windows (games, OBS,
    some Electron apps). This is a Windows API limitation.
    """
    import ctypes

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

    # Capture window content using PrintWindow API (preferred) or BitBlt fallback
    PW_RENDERFULLCONTENT = 2  # Windows 8.1+ flag for full content rendering

    # Create device context and bitmap via ctypes
    hdc_window = win32gui.GetWindowDC(hwnd)
    try:
        hdc_mem = ctypes.windll.gdi32.CreateCompatibleDC(hdc_window)
        bmp = ctypes.windll.gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        ctypes.windll.gdi32.SelectObject(hdc_mem, bmp)

        # Try PrintWindow first — captures actual window content even when minimized/obscured
        print_window_result = ctypes.windll.user32.PrintWindow(
            hwnd, hdc_mem, PW_RENDERFULLCONTENT
        )

        if not print_window_result:
            # PrintWindow failed — fall back to BitBlt (only works if window is visible/on-screen)
            ctypes.windll.gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_window, 0, 0, win32con.SRCCOPY)

        # Create a DIB header to get pixel data
        bmp_info = ctypes.create_string_buffer(40)  # BITMAPINFOHEADER
        ctypes.windll.gdi32.GetObjectA(bmp, 40, bmp_info)
        bits = ctypes.create_string_buffer(width * height * 4)
        ctypes.windll.gdi32.GetDIBits(hdc_mem, bmp, 0, height, bits, bmp_info, win32con.DIB_RGB_COLORS)

        img = Image.frombytes('RGBA', (width, height), bytes(bits))
        # Convert to RGB for PNG
        img = img.convert('RGB')

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    finally:
        win32gui.ReleaseDC(hwnd, hdc_window)
        ctypes.windll.gdi32.DeleteDC(hdc_mem)
        ctypes.windll.gdi32.DeleteObject(bmp)


def _find_window_by_pid(pid: int):
    """Find the first visible window owned by the given PID."""
    import win32gui
    import win32process

    results = []

    def enum_callback(hwnd, _):
        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        if found_pid == pid and win32gui.IsWindowVisible(hwnd):
            results.append(hwnd)

    win32gui.EnumWindows(enum_callback, None)
    if len(results) > 1:
        logger.warning("Multiple windows found for PID %d (%d total). Using first visible window.", pid, len(results))
    return results[0] if results else None


def _capture_window_linux(pid: int) -> bytes:
    """Capture a specific window by PID on Linux using mss + xdotool."""
    try:
        import mss
    except ImportError:
        raise ImportError(
            "Screen capture requires the 'mss' package. Install with: pip install mss"
        )

    # Check for display server
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
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