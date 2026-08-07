"""
ConPTY Diagnostic v5 — test if subprocess.Popen actually attaches to ConPTY.

Key test: spawn a process with ConPTY and also capture stdout via subprocess.PIPE.
If the process IS attached to ConPTY, its stdout should NOT go to the PIPE (it goes through ConPTY).
If it's NOT attached, output will appear in proc.stdout.
This tells us whether the PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE is actually working.
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


def main():
    print("=" * 60, flush=True)
    print("ConPTY Diagnostic v5 — Is ConPTY actually attached?", flush=True)
    print("=" * 60, flush=True)

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

    # ── Test A: spawn with ConPTY attribute + stdout=PIPE ─────────────
    # If ConPTY is attached, output goes to ConPTY (not PIPE).
    # If NOT attached, output appears in proc.stdout.
    command = 'cmd.exe /c "echo TEST_OUTPUT_12345"'

    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00080000  # type: ignore[attr-defined]

    print(f"\n[TEST A] Spawning with ConPTY attribute + PIPE...", flush=True)
    print(f"         Command: {command}", flush=True)

    proc = subprocess.Popen(
        command, shell=True,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
        env=os.environ.copy(),
    )

    kernel32.DeleteProcThreadAttributeList(attr_buf)

    try:
        stdout_data, stderr_data = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout_data, stderr_data = proc.stdout.read(), proc.stderr.read() if proc.stderr else b""

    print(f"[TEST A] Process exit code: {proc.returncode}", flush=True)
    print(f"[TEST A] PIPE stdout ({len(stdout_data)} bytes): {stdout_data!r}", flush=True)
    print(f"[TEST A] PIPE stderr ({len(stderr_data)} bytes): {stderr_data!r}", flush=True)

    # ── Test B: spawn WITHOUT ConPTY attribute for comparison ─────────
    print(f"\n[TEST B] Spawning WITHOUT ConPTY attribute + PIPE...", flush=True)

    proc2 = subprocess.Popen(
        command, shell=True,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=0,  # no special flags
        env=os.environ.copy(),
    )

    try:
        stdout_data2, stderr_data2 = proc2.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc2.kill()
        stdout_data2, stderr_data2 = proc2.stdout.read(), proc2.stderr.read() if proc2.stderr else b""

    print(f"[TEST B] Process exit code: {proc2.returncode}", flush=True)
    print(f"[TEST B] PIPE stdout ({len(stdout_data2)} bytes): {stdout_data2!r}", flush=True)
    print(f"[TEST B] PIPE stderr ({len(stderr_data2)} bytes): {stderr_data2!r}", flush=True)

    # ── Cleanup ConPTY ────────────────────────────────────────────────
    kernel32.ClosePseudoConsole(pty_handle)
    close_handle(pty_out_read)
    close_handle(pty_in_write)

    # ── Analysis ──────────────────────────────────────────────────────
    print("\n" + "=" * 60, flush=True)
    print("ANALYSIS", flush=True)
    print("=" * 60, flush=True)

    test_a_has_output = b"TEST_OUTPUT_12345" in stdout_data
    test_b_has_output = b"TEST_OUTPUT_12345" in stdout_data2

    print(f"Test A (with ConPTY): output in PIPE? {test_a_has_output}", flush=True)
    print(f"Test B (no ConPTY):  output in PIPE? {test_b_has_output}", flush=True)

    if test_a_has_output:
        print("\n[FINDING] Process output went to PIPE even WITH ConPTY attribute.", flush=True)
        print("This means PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE is NOT taking effect.")
        print("The process is NOT attached to the pseudoconsole!")
    elif not test_b_has_output:
        print("\n[FINDING] Neither test produced output in PIPE — unexpected.")
    else:
        print("\n[FINDING] Process output did NOT go to PIPE with ConPTY attribute.", flush=True)
        print("This means the process IS attached to the pseudoconsole.")
        print("Output should be coming through the ConPTY output handle.")
        print("The issue is in reading from the ConPTY output handle, not attachment.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[EXCEPTION] {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(2)