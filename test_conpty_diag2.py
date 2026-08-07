"""
ConPTY Diagnostic v2 — minimal test matching Microsoft EchoCon sample exactly.

Key differences from v1:
- No inheritable pipe handles (EchoCon doesn't use them)
- bInheritHandles = FALSE in CreateProcessW
- Simpler synchronous read loop with busy-wait during process lifetime
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
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

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
    """Read from ConPTY output pipe — matches EchoCon's PipeListener exactly."""
    buf_size = 512
    buffer = ctypes.create_string_buffer(buf_size)
    bytes_read = wintypes.DWORD()

    print("[LISTENER] Thread started", flush=True)

    while not stop_event.is_set():
        bytes_read.value = 0
        success = kernel32.ReadFile(
            wintypes.HANDLE(pipe_read_handle),
            buffer,
            buf_size,
            ctypes.byref(bytes_read),
            None,  # synchronous — EchoCon uses this too
        )

        if not success:
            err = ctypes.get_last_error()
            print(f"[LISTENER] ReadFile failed: {WinError(err)}", flush=True)
            break

        if bytes_read.value == 0:
            print("[LISTENER] ReadFile returned 0 bytes (EOF)", flush=True)
            break

        chunk = buffer.raw[:bytes_read.value]
        captured_data.append(chunk)
        try:
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines(keepends=False):
                if line.strip():
                    print(f"[CAPTURED] {line.strip()}", flush=True)
        except Exception as e:
            print(f"[LISTENER] Decode error: {e}, raw bytes: {chunk!r}", flush=True)

    print("[LISTENER] Thread exiting", flush=True)


def main():
    print("=" * 60, flush=True)
    print("ConPTY Diagnostic v2 (EchoCon-style)", flush=True)
    print("=" * 60, flush=True)

    captured_data = []
    stop_event = threading.Event()

    # ── Create pipes exactly like EchoCon does ────────────────────────
    # First pipe: ConPTY input — we write to phPipeOut, ConPTY reads from hPipePTYIn
    hPipePTYIn = wintypes.HANDLE(INVALID_HANDLE_VALUE)
    hPipeOut = wintypes.HANDLE(INVALID_HANDLE_VALUE)  # our write end for sending TO ConPTY

    if not kernel32.CreatePipe(ctypes.byref(hPipePTYIn), ctypes.byref(hPipeOut), None, 0):
        print(f"[FAIL] CreatePipe (input) failed: {WinError()}", flush=True)
        return 1
    print(f"[SETUP] Input pipe: PTY_in={hPipePTYIn.value}, our_write={hPipeOut.value}", flush=True)

    # Second pipe: ConPTY output — we read from phPipeIn, ConPTY writes to hPipePTYOut
    hPipeIn = wintypes.HANDLE(INVALID_HANDLE_VALUE)  # our read end for reading FROM ConPTY
    hPipePTYOut = wintypes.HANDLE(INVALID_HANDLE_VALUE)

    if not kernel32.CreatePipe(ctypes.byref(hPipeIn), ctypes.byref(hPipePTYOut), None, 0):
        print(f"[FAIL] CreatePipe (output) failed: {WinError()}", flush=True)
        return 1
    print(f"[SETUP] Output pipe: our_read={hPipeIn.value}, PTY_out={hPipePTYOut.value}", flush=True)

    # ── Create pseudoconsole ──────────────────────────────────────────
    console_size = COORD(120, 50)
    hPC = ctypes.c_void_p()

    result = kernel32.CreatePseudoConsole(
        console_size,
        hPipePTYIn,   # ConPTY reads from here (our input to it)
        hPipePTYOut,  # ConPTY writes to here (its output)
        0,            # no flags (EchoCon uses 0)
        ctypes.byref(hPC),
    )

    # Close PTY-end handles — ConPTY owns them now
    close_handle(hPipePTYIn.value)
    close_handle(hPipePTYOut.value)

    if result != 0:
        print(f"[FAIL] CreatePseudoConsole failed (0x{result:X}): {WinError(result)}", flush=True)
        return 1
    print(f"[SETUP] Pseudoconsole created: handle={hPC.value}", flush=True)

    # ── Start pipe listener thread ────────────────────────────────────
    listener_thread = threading.Thread(
        target=pipe_listener_thread_fn,
        args=(hPipeIn.value, stop_event, captured_data),
        name="pipe-listener",
        daemon=True,
    )
    listener_thread.start()
    time.sleep(0.1)  # let thread initialize

    # ── Initialize STARTUPINFOEXW for ConPTY attachment ───────────────
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

    # ── Spawn process (bInheritHandles=FALSE like EchoCon) ────────────
    command = ctypes.c_wchar_p('cmd.exe /c "echo Hello from ConPTY test & timeout /t 2 >nul & echo Done"')
    pi = PROCESS_INFORMATION()

    print("[SETUP] Spawning process with bInheritHandles=FALSE...", flush=True)

    success = kernel32.CreateProcessW(
        None,                          # lpApplicationName
        command,                       # lpCommandLine (mutable via c_wchar_p)
        None,                          # lpProcessAttributes
        None,                          # lpThreadAttributes
        False,                         # bInheritHandles — FALSE like EchoCon!
        0x00080000,                    # EXTENDED_STARTUPINFO_PRESENT
        None,                          # lpEnvironment
        None,                          # lpCurrentDirectory
        ctypes.byref(startupInfo),     # lpStartupInfo (STARTUPINFOEXW)
        ctypes.byref(pi),              # lpProcessInformation
    )

    if not success:
        print(f"[FAIL] CreateProcessW failed: {WinError()}", flush=True)
        return 1

    pid = pi.dwProcessId
    print(f"[SETUP] Process spawned: PID={pid}", flush=True)

    close_handle(pi.hThread)

    # ── Wait for process, letting listener thread capture output ──────
    print("\n[WAITING] Waiting for process to complete...", flush=True)
    wait_result = kernel32.WaitForSingleObject(pi.hProcess, 10000)

    if wait_result == 0:
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
        print(f"[PROCESS] Completed with exit code {exit_code.value}", flush=True)
    else:
        print(f"[TIMEOUT] WaitForSingleObject returned {wait_result}", flush=True)

    # Give listener thread time to finish reading (like EchoCon's Sleep(500))
    time.sleep(0.5)

    # ── Cleanup ───────────────────────────────────────────────────────
    print("\n[CLEANUP] Shutting down...", flush=True)
    close_handle(pi.hProcess)
    kernel32.DeleteProcThreadAttributeList(lpAttributeList)
    stop_event.set()
    listener_thread.join(timeout=2.0)

    # Close ConPTY (terminates any remaining child processes)
    kernel32.ClosePseudoConsole(hPC)

    # Close our pipe handles
    close_handle(hPipeIn.value)   # our read end of output pipe
    close_handle(hPipeOut.value)  # our write end of input pipe

    # ── Report results ────────────────────────────────────────────────
    print("\n" + "=" * 60, flush=True)
    print("RESULTS", flush=True)
    print("=" * 60, flush=True)

    all_text = b"".join(captured_data).decode("utf-8", errors="replace")
    print(f"Total raw bytes captured: {len(all_text)}", flush=True)

    if all_text.strip():
        print("\nAll captured text:", flush=True)
        print("-" * 40, flush=True)
        # Show with visible line endings for debugging
        display = all_text.replace("\r\n", "[CR LF]").replace("\r", "[CR]").replace("\n", "[LF]")
        print(display[:2000], flush=True)
        print("-" * 40, flush=True)

    expected_keywords = {"Hello from ConPTY test", "Done"}
    found_keywords = {kw for kw in expected_keywords if kw.lower() in all_text.lower()}

    if found_keywords == expected_keywords:
        print("\n[SUCCESS] ConPTY output capture works!", flush=True)
        print("The issue is in the relay/display logic, not ConPTY setup.")
        return 0
    elif captured_data:
        print(f"\n[PARTIAL] Got data but missing keywords", flush=True)
        print(f"  Expected: {expected_keywords}")
        print(f"  Found: {found_keywords}")
        return 1
    else:
        print("\n[FAIL] No output captured!", flush=True)
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