"""
ConPTY test using OVERLAPPED I/O for reading.
Some ConPTY implementations require async reads.
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
STARTF_USESTDHANDLES = 0x00000100
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258

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

kernel32.SetHandleInformation.restype = wintypes.BOOL
kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED),
]

kernel32.WriteFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]

kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
    ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
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


def create_inheritable_pipe():
    read_h = ctypes.c_void_p()
    write_h = ctypes.c_void_p()
    if not kernel32.CreatePipe(ctypes.byref(read_h), ctypes.byref(write_h), None, 0):
        raise RuntimeError(f"CreatePipe failed: {WinError()}")
    return read_h.value, write_h.value


def conpty_reader_thread_fn(conpty_out_handle, stop_event, captured_data):
    buf_size = 65536
    buffer = ctypes.create_string_buffer(buf_size)

    # Create event for overlapped I/O
    event = kernel32.CreateEventW(None, True, False, None)

    print("[READER] Thread started with OVERLAPPED", flush=True)
    read_count = 0

    while not stop_event.is_set():
        bytes_read = wintypes.DWORD()
        ov = OVERLAPPED()
        ov.hEvent = event

        success = kernel32.ReadFile(
            wintypes.HANDLE(conpty_out_handle), buffer, buf_size,
            ctypes.byref(bytes_read), ctypes.byref(ov),
        )

        read_count += 1
        err = ctypes.get_last_error()

        if success:
            # Sync read completed immediately
            chunk = buffer.raw[:bytes_read.value]
            captured_data.append(chunk)
            try:
                text = chunk.decode("utf-8", errors="replace")
                for line in text.splitlines(keepends=False):
                    if line.strip():
                        print(f"[CAPTURED] {line.strip()}", flush=True)
            except Exception as e:
                print(f"[READER] Decode error: {e}, raw={chunk!r}", flush=True)

        elif err == 997:  # ERROR_IO_PENDING
            # Wait for completion
            wait = kernel32.WaitForSingleObject(event, 5000)
            if wait == WAIT_OBJECT_0:
                final_bytes = wintypes.DWORD()
                ok = kernel32.GetOverlappedResult(
                    wintypes.HANDLE(conpty_out_handle), ctypes.byref(ov),
                    ctypes.byref(final_bytes), False,
                )
                if ok and final_bytes.value > 0:
                    chunk = buffer.raw[:final_bytes.value]
                    captured_data.append(chunk)
                    try:
                        text = chunk.decode("utf-8", errors="replace")
                        for line in text.splitlines(keepends=False):
                            if line.strip():
                                print(f"[CAPTURED] {line.strip()}", flush=True)
                    except Exception as e:
                        print(f"[READER] Decode error: {e}, raw={chunk!r}", flush=True)
                else:
                    print(f"[READER] GetOverlappedResult failed: {WinError(ctypes.get_last_error())}", flush=True)
                    break
            elif wait == WAIT_TIMEOUT:
                print(f"[READER] Wait timeout on read", flush=True)
                # Reset event and continue
                kernel32.SetEvent(event)  # Signal to unblock pending operation
                time.sleep(0.1)
                continue
            else:
                print(f"[READER] WaitForSingleObject returned {wait}", flush=True)
                break

        else:
            print(f"[READER] ReadFile failed (#{read_count}): {WinError(err)}", flush=True)
            break

    close_handle(event)
    print("[READER] Thread exiting", flush=True)


def main():
    print("=" * 60, flush=True)
    print("ConPTY Test with OVERLAPPED I/O", flush=True)
    print("=" * 60, flush=True)

    captured_data = []
    stop_event = threading.Event()

    # ── Create pipes and ConPTY ───────────────────────────────────────
    pty_in_read, pty_in_write = create_inheritable_pipe()
    pty_out_read, pty_out_write = create_inheritable_pipe()

    size = COORD(120, 50)
    pty_handle = ctypes.c_void_p()

    result = kernel32.CreatePseudoConsole(
        size, wintypes.HANDLE(pty_in_read), wintypes.HANDLE(pty_out_write),
        PSEUDOCONSOLE_INHERIT_CURSOR, ctypes.byref(pty_handle),
    )

    close_handle(pty_in_read)
    close_handle(pty_out_write)

    if result != 0:
        print(f"[FAIL] CreatePseudoConsole failed (0x{result:X}): {WinError(result)}", flush=True)
        return 1
    print(f"[SETUP] Pseudoconsole created: handle={pty_handle.value}", flush=True)

    # ── Start reader thread ───────────────────────────────────────────
    reader_thread = threading.Thread(
        target=conpty_reader_thread_fn, args=(pty_out_read, stop_event, captured_data),
        name="conpty-reader", daemon=True,
    )
    reader_thread.start()
    time.sleep(0.1)

    # ── Setup STARTUPINFOEXW with ConPTY attribute ────────────────────
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES

    size_attr = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_attr))
    attr_buf = ctypes.create_string_buffer(size_attr.value)
    kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size_attr))
    si.lpAttributeList = ctypes.addressof(attr_buf)

    pty_val = ctypes.c_void_p(pty_handle.value)
    if not kernel32.UpdateProcThreadAttribute(
        attr_buf, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.byref(pty_val), ctypes.sizeof(pty_val), None, None
    ):
        print(f"[FAIL] UpdateProcThreadAttribute failed: {WinError()}", flush=True)
        return 1

    # ── Spawn interactive cmd.exe ─────────────────────────────────────
    command = ctypes.c_wchar_p('cmd.exe')
    pi = PROCESS_INFORMATION()

    creation_flags = 0x00000200 | 0x00080000  # CREATE_NEW_PROCESS_GROUP | EXTENDED_STARTUPINFO_PRESENT

    print(f"\n[SETUP] Spawning interactive cmd.exe...", flush=True)

    success = kernel32.CreateProcessW(
        None, command, None, None, False, creation_flags,
        None, None, ctypes.byref(si), ctypes.byref(pi),
    )

    if not success:
        print(f"[FAIL] CreateProcessW failed: {WinError()}", flush=True)
        kernel32.DeleteProcThreadAttributeList(attr_buf)
        return 1

    pid = pi.dwProcessId
    print(f"[SETUP] Process spawned: PID={pid}", flush=True)

    close_handle(pi.hThread)
    kernel32.DeleteProcThreadAttributeList(attr_buf)

    time.sleep(0.5)

    # ── Send commands via ConPTY input ────────────────────────────────
    print("\n[INPUT] Sending commands...", flush=True)

    for cmd in [b"echo Hello from ConPTY\r\n", b"echo Second line\r\n", b"exit\r\n"]:
        bytes_written = wintypes.DWORD()
        buf = ctypes.create_string_buffer(cmd)
        success = kernel32.WriteFile(
            wintypes.HANDLE(pty_in_write), buf, len(cmd),
            ctypes.byref(bytes_written), None,
        )
        if success:
            print(f"[INPUT] Wrote {bytes_written.value} bytes", flush=True)
        else:
            print(f"[INPUT] WriteFile failed: {WinError()}", flush=True)
        time.sleep(0.5)

    # ── Wait for process ──────────────────────────────────────────────
    print("\n[WAITING] Waiting for process...", flush=True)
    wait_result = kernel32.WaitForSingleObject(pi.hProcess, 15000)

    if wait_result == WAIT_OBJECT_0:
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
        print(f"[PROCESS] Completed with exit code {exit_code.value}", flush=True)
    else:
        print(f"[TIMEOUT] WaitForSingleObject returned {wait_result}", flush=True)

    time.sleep(0.5)

    # ── Cleanup ───────────────────────────────────────────────────────
    print("\n[CLEANUP] Shutting down...", flush=True)
    close_handle(pi.hProcess)
    stop_event.set()
    reader_thread.join(timeout=3.0)
    kernel32.ClosePseudoConsole(pty_handle)
    close_handle(pty_out_read)
    close_handle(pty_in_write)

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

    expected_keywords = {"Hello from ConPTY", "Second line"}
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