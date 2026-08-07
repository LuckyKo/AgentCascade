"""
ConPTY Diagnostic v3 — uses OVERLAPPED I/O for reading to avoid blocking.

This version creates an OVERLAPPED structure with a manual event and uses
WaitForSingleObject on the event handle with a short timeout, so we can
poll for data without blocking forever.
"""

import ctypes
from ctypes import wintypes
import threading
import time
import sys

kernel32 = ctypes.windll.kernel32
WinError = ctypes.WinError

PSEUDOCONSOLE_INHERIT_CURSOR = 0x1
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x0102

class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_ulonglong),
        ("InternalHigh", ctypes.c_ulonglong),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]

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

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]

# Function prototypes
kernel32.CreatePseudoConsole.restype = wintypes.LONG
kernel32.CreatePseudoConsole.argtypes = [
    COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
]

kernel32.ClosePseudoConsole.restype = None
kernel32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]

kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
kernel32.InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p, ctypes.c_long, ctypes.c_long, ctypes.POINTER(ctypes.c_size_t),
]

kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
kernel32.UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_ulonglong,
    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
]

kernel32.DeleteProcThreadAttributeList.restype = None
kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

kernel32.CreatePipe.restype = wintypes.BOOL
kernel32.CreatePipe.argtypes = [
    ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
    ctypes.c_void_p, wintypes.DWORD,
]

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED),
]

kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
]

kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]

kernel32.ResetEvent.restype = wintypes.BOOL
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION),
]

kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def close_handle(h):
    if h and h != INVALID_HANDLE_VALUE:
        try:
            kernel32.CloseHandle(h)
        except Exception:
            pass


def pipe_listener_thread_fn(pipe_read_handle, stop_event, captured_data):
    """Read from ConPTY output using OVERLAPPED I/O with timeout."""
    buf_size = 65536
    buffer = ctypes.create_string_buffer(buf_size)

    # Create event for overlapped I/O
    event_handle = kernel32.CreateEventW(None, True, False, None)
    if not event_handle:
        print(f"[LISTENER] Failed to create event: {WinError()}", flush=True)
        return

    overlapped = OVERLAPPED()
    overlapped.hEvent = event_handle

    read_count = 0
    print("[LISTENER] Thread started (OVERLAPPED mode)", flush=True)

    while not stop_event.is_set():
        kernel32.ResetEvent(event_handle)

        bytes_read = wintypes.DWORD()
        success = kernel32.ReadFile(
            wintypes.HANDLE(pipe_read_handle),
            buffer,
            buf_size,
            ctypes.byref(bytes_read),
            ctypes.byref(overlapped),
        )

        read_count += 1

        if success:
            # Completed immediately
            if bytes_read.value == 0:
                print(f"[LISTENER] EOF after {read_count} reads", flush=True)
                break
            process_chunk(buffer, bytes_read.value, read_count, captured_data)
            continue

        err = ctypes.get_last_error()

        if err == ERROR_IO_PENDING:
            # I/O pending — wait with timeout
            wait_result = kernel32.WaitForSingleObject(event_handle, 50)  # 50ms timeout
            if wait_result == WAIT_OBJECT_0:
                final_bytes = wintypes.DWORD()
                ok = kernel32.GetOverlappedResult(
                    wintypes.HANDLE(pipe_read_handle),
                    ctypes.byref(overlapped),
                    ctypes.byref(final_bytes),
                    False,  # don't wait again
                )
                if ok and final_bytes.value > 0:
                    process_chunk(buffer, final_bytes.value, read_count, captured_data)
                elif final_bytes.value == 0:
                    print(f"[LISTENER] EOF after {read_count} reads", flush=True)
                    break
            # else: timeout — no data yet, loop again
        else:
            print(f"[LISTENER] ReadFile failed (#{read_count}): {WinError(err)}", flush=True)
            break

    kernel32.CloseHandle(event_handle)
    print("[LISTENER] Thread exiting", flush=True)


def process_chunk(buffer, num_bytes, read_count, captured_data):
    chunk = buffer.raw[:num_bytes]
    captured_data.append(chunk)
    try:
        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines(keepends=False):
            if line.strip():
                print(f"[CAPTURED] {line.strip()}", flush=True)
    except Exception as e:
        print(f"[LISTENER] Decode error #{read_count}: {e}, raw={chunk!r}", flush=True)


def main():
    print("=" * 60, flush=True)
    print("ConPTY Diagnostic v3 (OVERLAPPED I/O)", flush=True)
    print("=" * 60, flush=True)

    captured_data = []
    stop_event = threading.Event()

    # ── Create pipes ──────────────────────────────────────────────────
    hPipePTYIn = wintypes.HANDLE(INVALID_HANDLE_VALUE)
    hPipeOut = wintypes.HANDLE(INVALID_HANDLE_VALUE)

    if not kernel32.CreatePipe(ctypes.byref(hPipePTYIn), ctypes.byref(hPipeOut), None, 0):
        print(f"[FAIL] CreatePipe (input) failed: {WinError()}", flush=True)
        return 1

    hPipeIn = wintypes.HANDLE(INVALID_HANDLE_VALUE)
    hPipePTYOut = wintypes.HANDLE(INVALID_HANDLE_VALUE)

    if not kernel32.CreatePipe(ctypes.byref(hPipeIn), ctypes.byref(hPipePTYOut), None, 0):
        print(f"[FAIL] CreatePipe (output) failed: {WinError()}", flush=True)
        return 1

    # ── Create pseudoconsole ──────────────────────────────────────────
    console_size = COORD(120, 50)
    hPC = ctypes.c_void_p()

    result = kernel32.CreatePseudoConsole(
        console_size, hPipePTYIn, hPipePTYOut, 0, ctypes.byref(hPC),
    )

    close_handle(hPipePTYIn.value)
    close_handle(hPipePTYOut.value)

    if result != 0:
        print(f"[FAIL] CreatePseudoConsole failed (0x{result:X}): {WinError(result)}", flush=True)
        return 1
    print(f"[SETUP] Pseudoconsole created: handle={hPC.value}", flush=True)

    # ── Start listener thread ─────────────────────────────────────────
    listener_thread = threading.Thread(
        target=pipe_listener_thread_fn,
        args=(hPipeIn.value, stop_event, captured_data),
        name="pipe-listener",
        daemon=True,
    )
    listener_thread.start()
    time.sleep(0.1)

    # ── Initialize STARTUPINFOEXW ─────────────────────────────────────
    startupInfo = STARTUPINFOEXW()
    startupInfo.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)

    attrListSize = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attrListSize))
    lpAttributeList = ctypes.cast(ctypes.create_string_buffer(attrListSize.value), ctypes.c_void_p)

    if not kernel32.InitializeProcThreadAttributeList(lpAttributeList, 1, 0, ctypes.byref(attrListSize)):
        print(f"[FAIL] InitializeProcThreadAttributeList failed: {WinError()}", flush=True)
        return 1

    startupInfo.lpAttributeList = lpAttributeList

    pty_val = ctypes.c_void_p(hPC.value)
    if not kernel32.UpdateProcThreadAttribute(
        lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.byref(pty_val), ctypes.sizeof(pty_val), None, None
    ):
        print(f"[FAIL] UpdateProcThreadAttribute failed: {WinError()}", flush=True)
        return 1

    # ── Spawn process ─────────────────────────────────────────────────
    command = ctypes.c_wchar_p('cmd.exe /c "echo Hello from ConPTY test & timeout /t 2 >nul & echo Done"')
    pi = PROCESS_INFORMATION()

    print("[SETUP] Spawning process...", flush=True)

    success = kernel32.CreateProcessW(
        None, command, None, None, False, 0x00080000, None, None,
        ctypes.byref(startupInfo), ctypes.byref(pi),
    )

    if not success:
        print(f"[FAIL] CreateProcessW failed: {WinError()}", flush=True)
        return 1

    pid = pi.dwProcessId
    print(f"[SETUP] Process spawned: PID={pid}", flush=True)
    close_handle(pi.hThread)

    # ── Wait for process ──────────────────────────────────────────────
    print("\n[WAITING] Waiting for process...", flush=True)
    wait_result = kernel32.WaitForSingleObject(pi.hProcess, 10000)

    if wait_result == WAIT_OBJECT_0:
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
        print(f"[PROCESS] Completed with exit code {exit_code.value}", flush=True)
    else:
        print(f"[TIMEOUT] WaitForSingleObject returned {wait_result}", flush=True)

    time.sleep(0.5)  # let listener catch up

    # ── Cleanup ───────────────────────────────────────────────────────
    print("\n[CLEANUP] Shutting down...", flush=True)
    close_handle(pi.hProcess)
    kernel32.DeleteProcThreadAttributeList(lpAttributeList)
    stop_event.set()
    listener_thread.join(timeout=2.0)
    kernel32.ClosePseudoConsole(hPC)
    close_handle(hPipeIn.value)
    close_handle(hPipeOut.value)

    # ── Report results ────────────────────────────────────────────────
    print("\n" + "=" * 60, flush=True)
    print("RESULTS", flush=True)
    print("=" * 60, flush=True)

    all_text = b"".join(captured_data).decode("utf-8", errors="replace")
    print(f"Total raw bytes captured: {len(all_text)}", flush=True)

    if all_text.strip():
        print("\nAll captured text:", flush=True)
        print("-" * 40, flush=True)
        display = all_text.replace("\r\n", "[CR LF]").replace("\r", "[CR]").replace("\n", "[LF]")
        print(display[:2000], flush=True)
        print("-" * 40, flush=True)

    expected_keywords = {"Hello from ConPTY test", "Done"}
    found_keywords = {kw for kw in expected_keywords if kw.lower() in all_text.lower()}

    if found_keywords == expected_keywords:
        print("\n[SUCCESS] ConPTY output capture works!", flush=True)
        return 0
    elif captured_data:
        print(f"\n[PARTIAL] Got data but missing keywords", flush=True)
        print(f"  Expected: {expected_keywords}, Found: {found_keywords}")
        return 1
    else:
        print("\n[FAIL] No output captured!", flush=True)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[EXCEPTION] {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(2)