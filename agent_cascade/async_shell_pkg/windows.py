"""Windows console / Ctrl+C helpers (moved verbatim from async_shell.py).

Phase 3c pure-move refactor. Defines the ``ON_WINDOWS`` / ``_WIN_ENV`` module globals
and the Ctrl+C helper used by the tracker on Windows.
"""

import os
import subprocess

from agent_cascade.log import logger

ON_WINDOWS = os.name == 'nt'

# Pre-cached Windows environment dict with PYTHONIOENCODING set for child Python processes
if ON_WINDOWS:
    _WIN_ENV = os.environ.copy()
    _WIN_ENV['PYTHONIOENCODING'] = 'utf-8'
else:
    _WIN_ENV = None


def _send_windows_ctrl_c(pid: int) -> bool:
    """Send Ctrl+C to a Windows process using GenerateConsoleCtrlEvent via ctypes.

    Works for cmd.exe commands running with CREATE_NEW_CONSOLE where
    proc.send_signal(signal.CTRL_C_EVENT) fails. Runs in a separate Python
    subprocess to safely call FreeConsole/AttachConsole without affecting the parent.

    Args:
        pid: Target process PID

    Returns:
        True if Ctrl+C was sent successfully, False on failure.
    """
    import sys as _sys
    # Launch helper that attaches to target's console and sends CTRL_C_EVENT
    # Uses a proper no-op handler so the helper itself doesn't get killed by Ctrl+C
    helper_code = f"""
import ctypes, sys, time

kernel = ctypes.windll.kernel32
pid = {pid}

# No-op handler that ignores all control events (prevents helper from dying)
def ctrl_handler(dwCtrlType):
    return True

try:
    kernel.FreeConsole()
    kernel.AttachConsole(pid)

    # Install dummy handler so Ctrl+C doesn't kill this process
    handler_func = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)(ctrl_handler)
    if not kernel.SetConsoleCtrlHandler(handler_func, True):
        print(f"SetConsoleCtrlHandler failed: {{ctypes.get_last_error()}}", file=sys.stderr)
        sys.exit(1)

    # Send CTRL_C_EVENT to the target's console process group
    result = kernel.GenerateConsoleCtrlEvent(0, 0)  # 0 = CTRL_C_EVENT, 0 = all processes in console
    if not result:
        print(f"GenerateConsoleCtrlEvent failed: {{ctypes.get_last_error()}}", file=sys.stderr)
        sys.exit(1)

except Exception as e:
    print(f"Error: {{e}}", file=sys.stderr)
    sys.exit(1)

# Wait briefly for target process to respond to Ctrl+C
time.sleep(0.5)
sys.exit(0)
"""
    try:
        result = subprocess.run(
            [_sys.executable, '-c', helper_code],
            capture_output=True, text=True, timeout=3
        )
        return result.returncode == 0
    except Exception as e:
        logger.debug(f"[AsyncShell] Ctrl+C helper failed for PID {pid}: {e}")
        return False
