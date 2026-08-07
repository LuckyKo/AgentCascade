"""Quick smoke test for ConPTY async shell fix.

Verifies that a simple command spawned via the ConPTY path produces captured output.
Only runs on Windows (where ConPTY is available).
"""

import os
import sys
import time
import threading
import subprocess
from dataclasses import dataclass, field

ON_WINDOWS = os.name == 'nt'

if not ON_WINDOWS:
    print("SKIP: ConPTY test only runs on Windows")
    sys.exit(0)

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_cascade.async_shell import (
    _spawn_conpty_with_relay,
    _close_handle,
    ON_WINDOWS as ASYNC_ON_WINDOWS,
)


@dataclass
class FakeAsyncShellTask:
    """Minimal fake task object for testing _spawn_conpty_with_relay."""
    stdout_lines: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)


def test_conpty_output():
    """Spawn a simple command via ConPTY and verify output is captured."""
    print("=== ConPTY Quick Test ===")
    print(f"Platform: {sys.platform}")

    if not ASYNC_ON_WINDOWS:
        print("SKIP: Not on Windows")
        return False

    task = FakeAsyncShellTask()
    command = 'echo Hello from ConPTY && echo Second line'
    tool_id = 999

    print(f"Spawning: {command}")

    try:
        proc, pty_handle, reader_thread, relay_proc, relay_write, stdin_write = (
            _spawn_conpty_with_relay(command, None, task, tool_id)
        )
        print(f"Process started: PID={proc.pid}")
        print(f"Relay process: PID={relay_proc.pid}")

        # Close stdin_write to signal EOF to ConPTY (helps flush output)
        _close_handle(stdin_write)

        # Wait briefly for output to be captured by reader thread
        time.sleep(2.0)

        # Check what we captured
        print(f"\nCaptured {len(task.stdout_lines)} line(s):")
        for i, line in enumerate(task.stdout_lines):
            print(f"  [{i}] {line}")

        # Verify expected output appeared
        found_hello = any("Hello from ConPTY" in line for line in task.stdout_lines)
        found_second = any("Second line" in line for line in task.stdout_lines)

        print(f"\nVerification:")
        print(f"  Found 'Hello from ConPTY': {found_hello}")
        print(f"  Found 'Second line': {found_second}")

        # Clean up: close handles, wait for process
        try:
            proc.wait(timeout=5)
            print(f"Process exit code: {proc.returncode}")
        except subprocess.TimeoutExpired:
            print("Process did not exit in time, killing...")
            proc.kill()
            proc.wait()

        # Close ConPTY and handles
        import ctypes
        from agent_cascade.async_shell import _kernel32
        _kernel32.ClosePseudoConsole(ctypes.c_void_p(pty_handle))
        _close_handle(relay_write)

        try:
            relay_proc.wait(timeout=3)
            print(f"Relay exit code: {relay_proc.returncode}")
        except subprocess.TimeoutExpired:
            relay_proc.kill()
            relay_proc.wait()

        # Result
        if found_hello and found_second:
            print("\n✓ TEST PASSED: ConPTY output captured correctly")
            return True
        else:
            print("\n✗ TEST FAILED: Expected output not captured")
            return False

    except Exception as e:
        print(f"\n✗ TEST ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_conpty_output()
    sys.exit(0 if success else 1)