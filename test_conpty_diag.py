"""
Standalone ConPTY diagnostic — tests whether our ctypes-based ConPTY setup can capture output.

Runs a simple PowerShell loop that outputs one line per second for 5 seconds.
Reads from the ConPTY output handle in a background thread and prints captured lines.
NO relay window, NO subprocess module for spawning — pure ctypes to isolate the issue.
"""

import ctypes
from ctypes import wintypes
import threading
import time
import sys

# ─── ctypes setup (mirrors async_shell.py) ──────────────────────────

kernel32 = ctypes.windll.kernel32
WinError = ctypes.WinError

PSEUDOCONSOLE_INHERIT_CURSOR = 0x1
PIPE_ACCESS_INBOUND = 0x00000001
PIPE_ACCESS_OUTBOUND = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
HANDLE_FLAG_INHERIT = 0x00000001

class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]

class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]

# Function prototypes
kernel32.CreatePseudoConsole.restype = wintypes.LONG
kernel32.CreatePseudoConsole.argtypes = [
    COORD,
    wintypes.HANDLE,
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
]

kernel32.ClosePseudoConsole.restype = None
kernel32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]

kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
kernel32.InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p,
    ctypes.c_long,
    ctypes.c_long,
    ctypes.POINTER(ctypes.c_size_t),
]

kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
kernel32.UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_ulonglong,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_void_p,
]

kernel32.DeleteProcThreadAttributeList.restype = None
kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

kernel32.CreatePipe.restype = wintypes.BOOL
kernel32.CreatePipe.argtypes = [
    ctypes.POINTER(wintypes.HANDLE),
    ctypes.POINTER(wintypes.HANDLE),
    ctypes.c_void_p,
    wintypes.DWORD,
]

kernel32.SetHandleInformation.restype = wintypes.BOOL
kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,  # lpOverlapped
]

# OVERLAPPED structure for async I/O
class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_ulonglong),
        ("InternalHigh", ctypes.c_ulonglong),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]

kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(OVERLAPPED),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.BOOL,   # bWait
]

kernel32.ResetEvent.restype = wintypes.BOOL
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]

kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]

FILE_FLAG_OVERLAPPED = 0x40000000

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

# Define PROCESS_INFORMATION before using it in CreateProcessW argtypes
class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]

kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,  # lpApplicationName
    wintypes.LPWSTR,   # lpCommandLine
    ctypes.c_void_p,   # lpProcessAttributes
    ctypes.c_void_p,   # lpThreadAttributes
    wintypes.BOOL,     # bInheritHandles
    wintypes.DWORD,    # dwCreationFlags
    wintypes.LPWSTR,   # lpEnvironment
    wintypes.LPCWSTR,  # lpCurrentDirectory
    ctypes.POINTER(STARTUPINFOEXW),  # lpStartupInfo (EXTENDED_STARTUPINFO_PRESENT requires STARTUPINFOEXW)
    ctypes.POINTER(PROCESS_INFORMATION),  # lpProcessInformation
]


def close_handle(h):
    if h and h != INVALID_HANDLE_VALUE:
        try:
            kernel32.CloseHandle(h)
        except Exception:
            pass


def create_inheritable_pipe():
    read_h = ctypes.c_void_p()
    write_h = ctypes.c_void_p()
    if not kernel32.CreatePipe(ctypes.byref(read_h), ctypes.byref(write_h), None, 0):
        raise RuntimeError(f"CreatePipe failed: {WinError()}")
    kernel32.SetHandleInformation(read_h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
    return read_h.value, write_h.value


def conpty_reader_thread_fn(pty_out_read, stop_event, captured_lines):
    """Read from ConPTY output handle using overlapped I/O to avoid blocking."""
    buf_size = 65536
    buffer = ctypes.create_string_buffer(buf_size)
    bytes_read = wintypes.DWORD()

    # ERROR_NO_DATA (232) is normal for ConPTY — means nothing available right now
    ERROR_NO_DATA = 232
    ERROR_IO_PENDING = 997
    read_count = 0

    # Create event for overlapped I/O
    event_handle = kernel32.CreateEventW(None, True, False, None)
    if not event_handle:
        print("[READER THREAD] Failed to create event", flush=True)
        return

    overlapped = OVERLAPPED()
    overlapped.hEvent = event_handle

    print("[READER THREAD] Started (using overlapped I/O)", flush=True)

    while not stop_event.is_set():
        bytes_read.value = 0
        kernel32.ResetEvent(event_handle)

        result = kernel32.ReadFile(
            wintypes.HANDLE(pty_out_read),
            buffer,
            buf_size,
            ctypes.byref(bytes_read),
            ctypes.byref(overlapped),  # overlapped I/O!
        )

        read_count += 1

        if result:
            # Read completed immediately
            if bytes_read.value == 0:
                print(f"[READER THREAD] EOF after {read_count} reads (0 bytes)", flush=True)
                break
            process_read_data(buffer, bytes_read.value, read_count, captured_lines)
            continue

        err = ctypes.get_last_error()
        if err == ERROR_IO_PENDING:
            # I/O is pending — wait with timeout
            wait_result = kernel32.WaitForSingleObject(event_handle, 100)  # 100ms timeout
            if wait_result == 0:  # Event signaled — data available
                final_bytes = wintypes.DWORD()
                ok = kernel32.GetOverlappedResult(wintypes.HANDLE(pty_out_read), ctypes.byref(overlapped), ctypes.byref(final_bytes), False)
                if ok and final_bytes.value > 0:
                    process_read_data(buffer, final_bytes.value, read_count, captured_lines)
                elif final_bytes.value == 0:
                    print(f"[READER THREAD] EOF after {read_count} reads", flush=True)
                    break
            # else: timeout — no data yet, loop again
        elif err == ERROR_NO_DATA:
            # No output available right now
            time.sleep(0.05)
        else:
            print(f"[READER THREAD] ReadFile failed (call #{read_count}): {WinError(err)}", flush=True)
            break

    kernel32.CloseHandle(event_handle)


def process_read_data(buffer, num_bytes, read_count, captured_lines):
    """Process bytes read from ConPTY output."""
    raw = buffer.raw[:num_bytes]
    print(f"[READER THREAD] ReadFile success (call #{read_count}): {num_bytes} bytes", flush=True)

    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[READER THREAD] Decode error: {e}", flush=True)
        return

    for line in text.splitlines(keepends=False):
        stripped = line.strip()
        if stripped:
            captured_lines.append(stripped)
            print(f"[CAPTURED] {stripped}", flush=True)


def main():
    print("=" * 60, flush=True)
    print("ConPTY Diagnostic Test", flush=True)
    print("=" * 60, flush=True)

    captured_lines = []
    stop_event = threading.Event()

    # ── Step 1: Create pipes for ConPTY I/O ───────────────────────────
    print("\n[SETUP] Creating pipes...", flush=True)
    pty_in_read, pty_in_write = create_inheritable_pipe()
    pty_out_read, pty_out_write = create_inheritable_pipe()
    stdin_write = pty_in_write
    print(f"[SETUP] Pipes created: in_read={pty_in_read}, out_read={pty_out_read}", flush=True)

    # ── Step 2: Create the pseudoconsole ──────────────────────────────
    print("[SETUP] Creating pseudoconsole...", flush=True)
    size = COORD(120, 50)
    pty_handle = ctypes.c_void_p()

    result = kernel32.CreatePseudoConsole(
        size,
        wintypes.HANDLE(pty_in_read),
        wintypes.HANDLE(pty_out_write),
        PSEUDOCONSOLE_INHERIT_CURSOR,
        ctypes.byref(pty_handle),
    )

    # ConPTY now owns pty_in_read and pty_out_write; close our copies
    close_handle(pty_in_read)
    close_handle(pty_out_write)

    if result != 0:
        close_handle(stdin_write)
        close_handle(pty_out_read)
        print(f"[FAIL] CreatePseudoConsole failed (0x{result:X}): {WinError(result)}", flush=True)
        return 1

    print(f"[SETUP] Pseudoconsole created: handle={pty_handle.value}", flush=True)

    # ── Step 3: Start reader thread ───────────────────────────────────
    print("[SETUP] Starting reader thread...", flush=True)
    reader_thread = threading.Thread(
        target=conpty_reader_thread_fn,
        args=(pty_out_read, stop_event, captured_lines),
        name="conpty-reader",
        daemon=True,
    )
    reader_thread.start()

    # ── Step 4: Spawn PowerShell attached to ConPTY ───────────────────
    print("[SETUP] Spawning PowerShell process...", flush=True)

    # Test with a simple cmd command first, then PowerShell
    command = 'cmd.exe /c "echo Hello from ConPTY test & timeout /t 2 >nul & echo Done"'
    print(f"[SETUP] Command: {command}", flush=True)

    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)  # Must be size of full STARTUPINFOEXW when using EXTENDED_STARTUPINFO_PRESENT
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    si.StartupInfo.hStdInput = wintypes.HANDLE(0)
    si.StartupInfo.hStdOutput = wintypes.HANDLE(0)
    si.StartupInfo.hStdError = wintypes.HANDLE(0)

    # Initialize attribute list for ConPTY attachment
    size_attr = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_attr))
    attr_buf = ctypes.create_string_buffer(size_attr.value)
    if not kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size_attr)):
        print(f"[FAIL] InitializeProcThreadAttributeList (alloc) failed: {WinError()}", flush=True)
        return 1

    si.lpAttributeList = ctypes.addressof(attr_buf)

    # Set the ConPTY attribute
    pty_ptr = ctypes.c_void_p(pty_handle.value)
    if not kernel32.UpdateProcThreadAttribute(
        attr_buf,
        0,
        PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.addressof(pty_ptr),
        ctypes.sizeof(pty_ptr),
        None,
        None,
    ):
        print(f"[FAIL] UpdateProcThreadAttribute failed: {WinError()}", flush=True)
        return 1

    pi = PROCESS_INFORMATION()

    # dwCreationFlags: inherit handles + extended startup info
    creation_flags = 0x00000200 | 0x00080000  # CREATE_NEW_PROCESS_GROUP | EXTENDED_STARTUPINFO_PRESENT

    print("[SETUP] Calling CreateProcessW...", flush=True)
    if not kernel32.CreateProcessW(
        None,                          # lpApplicationName (use command line)
        ctypes.c_wchar_p(command),     # lpCommandLine (mutable)
        None,                          # lpProcessAttributes
        None,                          # lpThreadAttributes
        True,                          # bInheritHandles
        creation_flags,                # dwCreationFlags
        None,                          # lpEnvironment (inherit parent)
        None,                          # lpCurrentDirectory (inherit parent)
        ctypes.byref(si),              # lpStartupInfo (full STARTUPINFOEXW with attribute list)
        ctypes.byref(pi),              # lpProcessInformation
    ):
        print(f"[FAIL] CreateProcessW failed: {WinError()}", flush=True)
        kernel32.DeleteProcThreadAttributeList(attr_buf)
        return 1

    pid = pi.dwProcessId
    print(f"[SETUP] Process spawned: PID={pid}", flush=True)

    # Close thread handle (not needed)
    close_handle(pi.hThread)

    # ── Step 5: Wait for process to finish, collecting output ─────────
    print("\n[WAITING] Waiting up to 10 seconds for process to complete...", flush=True)
    print("[WAITING] Expected: 'Hello from ConPTY test' and 'Done'", flush=True)

    start_time = time.time()
    timeout_ms = 10000

    while True:
        elapsed = time.time() - start_time
        if elapsed > 10:
            print(f"\n[TIMEOUT] Process did not finish within 10 seconds", flush=True)
            # Kill the process
            kernel32.TerminateProcess(pi.hProcess, 1)
            break

        wait_result = kernel32.WaitForSingleObject(pi.hProcess, 500)
        if wait_result == 0:  # WAIT_OBJECT_0 — process finished
            exit_code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
            print(f"\n[PROCESS] Completed with exit code {exit_code.value}", flush=True)
            break

        # Periodic status
        if int(elapsed) % 2 == 0 and elapsed > 0:
            print(f"[WAITING] ... {int(elapsed)}s elapsed, captured {len(captured_lines)} lines", flush=True)

    # ── Step 6: Cleanup ───────────────────────────────────────────────
    print("\n[CLEANUP] Stopping reader thread...", flush=True)
    stop_event.set()
    reader_thread.join(timeout=2.0)

    close_handle(pi.hProcess)
    kernel32.DeleteProcThreadAttributeList(attr_buf)
    close_handle(stdin_write)
    close_handle(pty_out_read)
    kernel32.ClosePseudoConsole(pty_handle)

    # ── Step 7: Report results ────────────────────────────────────────
    print("\n" + "=" * 60, flush=True)
    print("RESULTS", flush=True)
    print("=" * 60, flush=True)
    print(f"Total lines captured: {len(captured_lines)}", flush=True)

    if captured_lines:
        print("\nCaptured lines:", flush=True)
        for i, line in enumerate(captured_lines, 1):
            print(f"  {i}. {line}", flush=True)

    # Check if we got any output at all
    expected_keywords = {"Hello from ConPTY test", "Done"}
    found_keywords = set()
    for line in captured_lines:
        for kw in expected_keywords:
            if kw.lower() in line.lower():
                found_keywords.add(kw)

    if found_keywords == expected_keywords:
        print("\n[SUCCESS] ConPTY output capture is working correctly!", flush=True)
        print("The issue is likely in the relay/display logic, not ConPTY setup.")
        return 0
    elif captured_lines:
        print(f"\n[PARTIAL] Got some output but not expected keywords", flush=True)
        print(f"  Expected keywords: {expected_keywords}")
        print(f"  Found keywords: {found_keywords}")
        return 1
    else:
        print("\n[FAIL] No output captured from ConPTY at all!", flush=True)
        print("ConPTY setup or reading has a fundamental issue.")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[EXCEPTION] {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(2)