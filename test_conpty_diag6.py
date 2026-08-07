"""
ConPTY Diagnostic v6 — test subprocess.Popen WITHOUT stdout=PIPE.

When async_shell.py spawns with ConPTY, it uses stdout=subprocess.PIPE but
the comment says "Not used for output — ConPTY pipe is the source of truth".
Maybe specifying PIPE overrides our STARTUPINFOEXW's handle settings?

This test spawns without any PIPE to see if ConPTY attachment works.
"""

import ctypes
from ctypes import wintypes
import subprocess
import threading
import time
import sys
import os

kernel32 = ctypes.windll.kernel32
WinError = ctypes.WinError

PSEUDOCONSOLE_INHERIT_CURSOR = 0x1
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
HANDLE_FLAG_INHERIT = 0x00000001
STARTF_USESTDHANDLES = 0x00000100

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
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

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
    kernel32.SetHandleInformation(read_h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
    return read_h.value, write_h.value


def conpty_reader_thread_fn(conpty_out_handle, stop_event, captured_data):
    buf_size = 65536
    buffer = ctypes.create_string_buffer(buf_size)

    print("[READER] Thread started", flush=True)
    read_count = 0

    while not stop_event.is_set():
        bytes_read = wintypes.DWORD()
        success = kernel32.ReadFile(
            wintypes.HANDLE(conpty_out_handle), buffer, buf_size,
            ctypes.byref(bytes_read), None,
        )

        read_count += 1

        if not success:
            err = ctypes.get_last_error()
            print(f"[READER] ReadFile failed (#{read_count}): {WinError(err)}", flush=True)
            break

        if bytes_read.value == 0:
            print(f"[READER] EOF after {read_count} reads", flush=True)
            break

        chunk = buffer.raw[:bytes_read.value]
        captured_data.append(chunk)
        try:
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines(keepends=False):
                if line.strip():
                    print(f"[CAPTURED] {line.strip()}", flush=True)
        except Exception as e:
            print(f"[READER] Decode error: {e}, raw={chunk!r}", flush=True)

    print("[READER] Thread exiting", flush=True)


def main():
    print("=" * 60, flush=True)
    print("ConPTY Diagnostic v6 — subprocess.Popen WITHOUT PIPE", flush=True)
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

    # ── Spawn WITHOUT stdout=PIPE — let ConPTY be the only output path ─
    command = 'cmd.exe /c "echo TEST_OUTPUT_12345 & ping -n 2 127.0.0.1 >nul & echo Done"'

    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00080000  # type: ignore[attr-defined]

    print(f"\n[SETUP] Spawning WITHOUT PIPE, with ConPTY attribute...", flush=True)
    print(f"         Command: {command}", flush=True)

    # Build STARTUPINFO for subprocess.Popen
    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    # We need to pass our STARTUPINFOEXW somehow...
    # subprocess.Popen accepts a STARTUPINFO, not STARTUPINFOEXW directly.
    # But with EXTENDED_STARTUPINFO_PRESENT, it should handle it via ctypes.

    # Actually, let's use the low-level approach: convert si to something Popen can use
    # The issue is that Python's subprocess module on Windows uses its own internal
    # _subprocess.CreateProcess and may not properly handle STARTUPINFOEXW from outside.

    # Let's try passing our STARTUPINFOEXW via the startupinfo parameter
    # by copying fields manually...

    startupinfo = ctypes.cast(ctypes.byref(si), ctypes.POINTER(subprocess.STARTUPINFO))  # type: ignore[attr-defined]

    try:
        proc = subprocess.Popen(
            command, shell=True,
            stdin=None,
            stdout=None,   # NO PIPE — ConPTY only
            stderr=None,   # NO PIPE — ConPTY only
            creationflags=creation_flags,
            startupinfo=startupinfo,
            env=os.environ.copy(),
        )
    except Exception as e:
        print(f"[FAIL] subprocess.Popen failed: {e}", flush=True)
        return 1

    kernel32.DeleteProcThreadAttributeList(attr_buf)

    print(f"[SETUP] Process spawned: PID={proc.pid}", flush=True)

    # ── Wait for process ──────────────────────────────────────────────
    print("\n[WAITING] Waiting for process...", flush=True)
    try:
        proc.wait(timeout=10)
        print(f"[PROCESS] Completed with exit code {proc.returncode}", flush=True)
    except subprocess.TimeoutExpired:
        print("[TIMEOUT] Process did not complete in 10 seconds", flush=True)
        proc.kill()

    time.sleep(0.5)

    # ── Cleanup ───────────────────────────────────────────────────────
    print("\n[CLEANUP] Shutting down...", flush=True)
    stop_event.set()
    reader_thread.join(timeout=2.0)
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
        display = all_text.replace("\r\n", "[CR LF]").replace("\r", "[CR]").replace("\n", "[LF]")
        print(display[:2000], flush=True)

    expected_keywords = {"TEST_OUTPUT_12345", "Done"}
    found_keywords = {kw for kw in expected_keywords if kw.lower() in all_text.lower()}

    if found_keywords == expected_keywords:
        print("\n[SUCCESS] ConPTY output capture works without PIPE!", flush=True)
        return 0
    elif captured_data:
        print(f"\n[PARTIAL] Got data but missing keywords", flush=True)
        return 1
    else:
        print("\n[FAIL] No output captured even without PIPE!", flush=True)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[EXCEPTION] {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(2)