"""
ConPTY Diagnostic v9 — test using Python's subprocess._subprocess module directly.

This bypasses subprocess.Popen's wrapper and calls CreateProcessW via the internal
_subprocess module, which is how Python actually does it internally.
"""

import ctypes
from ctypes import wintypes
import threading
import time
import sys
import _subprocess

kernel32 = ctypes.windll.kernel32
WinError = ctypes.WinError

PSEUDOCONSOLE_INHERIT_CURSOR = 0x1
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
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

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def close_handle(h):
    if h and h != 0xFFFFFFFFFFFFFFFF:
        try:
            kernel32.CloseHandle(h)
        except Exception:
            pass


def main():
    print("=" * 60, flush=True)
    print("ConPTY Diagnostic v9 — subprocess._subprocess.CreateProcess", flush=True)
    print("=" * 60, flush=True)

    # ── Create pipes ──────────────────────────────────────────────────
    hPipePTYIn = wintypes.HANDLE()
    hPipeOut = wintypes.HANDLE()
    if not kernel32.CreatePipe(ctypes.byref(hPipePTYIn), ctypes.byref(hPipeOut), None, 0):
        print(f"[FAIL] CreatePipe(in) failed: {WinError()}")
        return 1

    hPipeIn = wintypes.HANDLE()
    hPipePTYOut = wintypes.HANDLE()
    if not kernel32.CreatePipe(ctypes.byref(hPipeIn), ctypes.byref(hPipePTYOut), None, 0):
        print(f"[FAIL] CreatePipe(out) failed: {WinError()}")
        return 1

    # ── Create pseudoconsole ──────────────────────────────────────────
    size = COORD(120, 50)
    hPC = ctypes.c_void_p()

    result = kernel32.CreatePseudoConsole(size, hPipePTYIn, hPipePTYOut, 0, ctypes.byref(hPC))
    close_handle(hPipePTYIn.value)
    close_handle(hPipePTYOut.value)

    if result != 0:
        print(f"[FAIL] CreatePseudoConsole failed (0x{result:X}): {WinError(result)}")
        return 1
    print(f"ConPTY handle={hPC.value}", flush=True)

    # ── Start reader thread ───────────────────────────────────────────
    captured_data = []
    stop_event = threading.Event()

    def reader():
        buf = ctypes.create_string_buffer(65536)
        br = wintypes.DWORD()
        print(f"[READER] Starting... handle={hPipeIn.value}", flush=True)
        while not stop_event.is_set():
            success = kernel32.ReadFile(hPipeIn.value, buf, 65536, ctypes.byref(br), None)
            if not success:
                print(f"[READER] ReadFile failed: {WinError()}")
                break
            if br.value == 0:
                print("[READER] EOF")
                break
            chunk = buf.raw[:br.value]
            captured_data.append(chunk)
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines(keepends=False):
                if line.strip():
                    print(f"[CAPTURED] {line.strip()}", flush=True)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    time.sleep(0.1)

    # ── Setup STARTUPINFOEXW ──────────────────────────────────────────
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES

    size_attr = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_attr))
    attr_buf = ctypes.create_string_buffer(size_attr.value)
    kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size_attr))
    si.lpAttributeList = ctypes.addressof(attr_buf)

    pty_val = ctypes.c_void_p(hPC.value)
    if not kernel32.UpdateProcThreadAttribute(
        attr_buf, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.byref(pty_val), ctypes.sizeof(pty_val), None, None
    ):
        print(f"[FAIL] UpdateProcThreadAttribute failed: {WinError()}")
        return 1

    # ── Spawn using _subprocess.CreateProcessW directly ───────────────
    command = 'powershell.exe -NoProfile -Command "Write-Host HelloConPTY"'

    # Use _subprocess's CreateProcessW which handles STARTUPINFOEXW properly
    creation_flags = 0x00080000  # EXTENDED_STARTUPINFO_PRESENT

    print(f"\nUsing _subprocess.CreateProcessW...", flush=True)

    try:
        h_process, h_thread, pid, tid = _subprocess.CreateProcessW(
            None, command, None, None, False, creation_flags,
            None, None, ctypes.byref(si),
        )
    except Exception as e:
        print(f"[FAIL] _subprocess.CreateProcessW failed: {e}")
        return 1

    kernel32.DeleteProcThreadAttributeList(attr_buf)
    close_handle(h_thread)

    print(f"Process spawned: PID={pid}", flush=True)

    # ── Wait for process ──────────────────────────────────────────────
    wait_result = kernel32.WaitForSingleObject(h_process, 10000)
    if wait_result == 0:
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
        print(f"Process completed: exit code={exit_code.value}", flush=True)

    time.sleep(0.5)

    # ── Cleanup ───────────────────────────────────────────────────────
    close_handle(h_process)
    stop_event.set()
    reader_thread.join(timeout=2.0)
    kernel32.ClosePseudoConsole(hPC)
    close_handle(hPipeIn.value)
    close_handle(hPipeOut.value)

    # ── Results ───────────────────────────────────────────────────────
    print("\n" + "=" * 60, flush=True)
    all_text = b"".join(captured_data).decode("utf-8", errors="replace")
    print(f"Total bytes captured: {len(all_text)}", flush=True)

    if "HelloConPTY" in all_text:
        print("[SUCCESS] ConPTY output captured!", flush=True)
        return 0
    else:
        print("[FAIL] No ConPTY output captured!", flush=True)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[EXCEPTION] {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(2)