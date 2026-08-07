"""
ConPTY Diagnostic v10 — spawn cmd.exe directly (not via shell=True), send input.

Instead of using cmd.exe /c "command" which exits quickly, spawn interactive cmd.exe
and send it commands through the ConPTY input handle. This tests if:
1. The process attaches to ConPTY properly
2. We can read output from ConPTY
3. We can write input to ConPTY
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

kernel32.WriteFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
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


def close_handle(h):
    if h and h != 0xFFFFFFFFFFFFFFFF:
        try:
            kernel32.CloseHandle(h)
        except Exception:
            pass


def main():
    print("=" * 60, flush=True)
    print("ConPTY Diagnostic v10 — Interactive cmd.exe", flush=True)
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
        read_count = 0
        print(f"[READER] Starting... handle={hPipeIn.value}", flush=True)
        while not stop_event.is_set():
            success = kernel32.ReadFile(hPipeIn.value, buf, 65536, ctypes.byref(br), None)
            read_count += 1
            if not success:
                err = ctypes.get_last_error()
                print(f"[READER] ReadFile failed (#{read_count}): {WinError(err)}")
                break
            if br.value == 0:
                print(f"[READER] EOF after {read_count} reads")
                break
            chunk = buf.raw[:br.value]
            captured_data.append(chunk)
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines(keepends=False):
                if line.strip():
                    print(f"[CAPTURED] {line.strip()}", flush=True)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    time.sleep(0.2)

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

    # ── Spawn interactive cmd.exe directly (not via shell=True) ───────
    pi = PROCESS_INFORMATION()

    # Use mutable command line for CreateProcessW
    cmd_line = ctypes.create_unicode_buffer('cmd.exe')

    print(f"\n[SETUP] Spawning interactive cmd.exe...", flush=True)

    success = kernel32.CreateProcessW(
        None, cmd_line, None, None, False, 0x00080000, None, None,
        ctypes.byref(si), ctypes.byref(pi),
    )

    if not success:
        print(f"[FAIL] CreateProcessW failed: {WinError()}")
        return 1

    kernel32.DeleteProcThreadAttributeList(attr_buf)
    close_handle(pi.hThread)

    print(f"cmd.exe spawned: PID={pi.dwProcessId}", flush=True)

    # ── Wait for prompt, then send commands ───────────────────────────
    time.sleep(1.0)  # give cmd time to start and show prompt

    # Send "echo HelloConPTY" followed by Enter
    command = b"echo HelloConPTY\r\n"
    written = wintypes.DWORD()
    print(f"\n[INPUT] Sending: {command!r}", flush=True)
    success = kernel32.WriteFile(hPipeOut.value, command, len(command), ctypes.byref(written), None)
    if success:
        print(f"[INPUT] Wrote {written.value} bytes to ConPTY input", flush=True)
    else:
        print(f"[INPUT] WriteFile failed: {WinError()}", flush=True)

    # Wait for output
    time.sleep(1.0)

    # Send "exit" to close cmd.exe
    exit_cmd = b"exit\r\n"
    written = wintypes.DWORD()
    print(f"\n[INPUT] Sending: {exit_cmd!r}", flush=True)
    success = kernel32.WriteFile(hPipeOut.value, exit_cmd, len(exit_cmd), ctypes.byref(written), None)
    if not success:
        print(f"[INPUT] WriteFile failed: {WinError()}", flush=True)

    # Wait for process to exit
    wait_result = kernel32.WaitForSingleObject(pi.hProcess, 5000)
    if wait_result == 0:
        print("cmd.exe exited", flush=True)
    else:
        print(f"Wait timeout ({wait_result})", flush=True)

    time.sleep(0.5)

    # ── Cleanup ───────────────────────────────────────────────────────
    close_handle(pi.hProcess)
    stop_event.set()
    reader_thread.join(timeout=2.0)
    kernel32.ClosePseudoConsole(hPC)
    close_handle(hPipeIn.value)
    close_handle(hPipeOut.value)

    # ── Results ───────────────────────────────────────────────────────
    print("\n" + "=" * 60, flush=True)
    all_text = b"".join(captured_data).decode("utf-8", errors="replace")
    print(f"Total bytes captured: {len(all_text)}", flush=True)

    if all_text.strip():
        print("\nCaptured text:", flush=True)
        display = all_text.replace("\r\n", "[CR LF]").replace("\r", "[CR]").replace("\n", "[LF]")
        print(display[:3000], flush=True)

    if "HelloConPTY" in all_text:
        print("\n[SUCCESS] ConPTY output captured!", flush=True)
        return 0
    else:
        print("\n[FAIL] No ConPTY output captured!", flush=True)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[EXCEPTION] {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(2)