"""
ConPTY test following Microsoft EchoCon sample EXACTLY.
"""

import ctypes
from ctypes import wintypes
import threading
import time
import sys

kernel32 = ctypes.windll.kernel32
WinError = ctypes.WinError

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

# EXACT argtypes from EchoCon sample: lpStartupInfo is pointer to STARTUPINFOW member
kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
]

kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def close_handle(h):
    if h and h != INVALID_HANDLE_VALUE:
        try:
            kernel32.CloseHandle(h)
        except Exception:
            pass


captured_data = []


def pipe_listener_thread_fn(h_pipe_in):
    """Reads from the output pipe (hPipeIn in EchoCon naming) — where ConPTY writes to."""
    buf_size = 512
    buffer = ctypes.create_string_buffer(buf_size)

    print("[LISTENER] Thread started", flush=True)

    while True:
        bytes_read = wintypes.DWORD()
        success = kernel32.ReadFile(
            wintypes.HANDLE(h_pipe_in), buffer, buf_size,
            ctypes.byref(bytes_read), None,
        )

        if not success:
            err = ctypes.get_last_error()
            print(f"[LISTENER] ReadFile failed: {WinError(err)}", flush=True)
            break

        if bytes_read.value == 0:
            print("[LISTENER] EOF (0 bytes)", flush=True)
            break

        chunk = buffer.raw[:bytes_read.value]
        captured_data.append(chunk)
        try:
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines(keepends=False):
                if line.strip():
                    print(f"[CAPTURED] {line.strip()}", flush=True)
        except Exception as e:
            print(f"[LISTENER] Decode error: {e}, raw={chunk!r}", flush=True)

    print("[LISTENER] Thread exiting", flush=True)


def main():
    print("=" * 60, flush=True)
    print("ConPTY Test — EchoCon Pattern", flush=True)
    print("=" * 60, flush=True)

    # ── Create pipes exactly like EchoCon ─────────────────────────────
    # From EchoCon:
    #   CreatePipe(&hPipePTYIn, phPipeOut, ...)      -> hPipePTYIn for ConPTY input, caller writes to *phPipeOut
    #   CreatePipe(phPipeIn, &hPipePTYOut, ...)      -> caller reads from *phPipeIn, ConPTY writes to hPipePTYOut
    
    h_pipe_pty_in = wintypes.HANDLE()
    h_pipe_out = wintypes.HANDLE()  # Caller writes input commands here
    h_pipe_in = wintypes.HANDLE()   # Caller reads output here (ConPTY writes to other end)
    h_pipe_pty_out = wintypes.HANDLE()

    if not kernel32.CreatePipe(ctypes.byref(h_pipe_pty_in), ctypes.byref(h_pipe_out), None, 0):
        print(f"[FAIL] CreatePipe (input) failed: {WinError()}", flush=True)
        return 1
    
    if not kernel32.CreatePipe(ctypes.byref(h_pipe_in), ctypes.byref(h_pipe_pty_out), None, 0):
        print(f"[FAIL] CreatePipe (output) failed: {WinError()}", flush=True)
        return 1

    # ── Create ConPTY ────────────────────────────────────────────────
    console_size = COORD(80, 24)
    h_pc = ctypes.c_void_p()

    result = kernel32.CreatePseudoConsole(
        console_size, h_pipe_pty_in, h_pipe_pty_out, 0, ctypes.byref(h_pc),
    )

    # Close PTY-end of pipes (ConPTY owns them now)
    close_handle(h_pipe_pty_in)
    close_handle(h_pipe_pty_out)

    if result != 0:
        print(f"[FAIL] CreatePseudoConsole failed (0x{result:X}): {WinError(result)}", flush=True)
        return 1
    
    print(f"[SETUP] Pseudoconsole created: handle={h_pc.value}", flush=True)

    # ── Start pipe listener thread ───────────────────────────────────
    listener_thread = threading.Thread(
        target=pipe_listener_thread_fn, args=(h_pipe_in.value,),
        name="pipe-listener", daemon=True,
    )
    listener_thread.start()

    # ── Initialize STARTUPINFOEXW ────────────────────────────────────
    startup_info = STARTUPINFOEXW()
    startup_info.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)

    attr_list_size = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_list_size))
    
    # Use malloc-style allocation via ctypes
    libc = ctypes.CDLL("msvcrt")
    malloc_func = libc.malloc
    malloc_func.restype = ctypes.c_void_p
    malloc_func.argtypes = [ctypes.c_size_t]
    
    free_func = libc.free
    free_func.restype = None
    free_func.argtypes = [ctypes.c_void_p]

    startup_info.lpAttributeList = malloc_func(attr_list_size.value)
    
    if not startup_info.lpAttributeList:
        print("[FAIL] malloc failed", flush=True)
        return 1

    if not kernel32.InitializeProcThreadAttributeList(
        startup_info.lpAttributeList, 1, 0, ctypes.byref(attr_list_size)
    ):
        print(f"[FAIL] InitializeProcThreadAttributeList failed: {WinError()}", flush=True)
        free_func(startup_info.lpAttributeList)
        return 1

    # Set Pseudo Console attribute — pass HPCON directly (not byref of c_void_p)
    if not kernel32.UpdateProcThreadAttribute(
        startup_info.lpAttributeList,
        0,
        PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.byref(h_pc),  # Pass HPCON handle directly
        ctypes.sizeof(ctypes.c_void_p),
        None,
        None
    ):
        print(f"[FAIL] UpdateProcThreadAttribute failed: {WinError()}", flush=True)
        kernel32.DeleteProcThreadAttributeList(startup_info.lpAttributeList)
        free_func(startup_info.lpAttributeList)
        return 1

    # ── Launch process (like ping localhost in EchoCon) ──────────────
    sz_command = ctypes.c_wchar_p('cmd.exe /c "echo Hello from ConPTY && echo Second line"')
    pi_client = PROCESS_INFORMATION()

    print(f"\n[SETUP] Spawning via CreateProcessW...", flush=True)

    success = kernel32.CreateProcessW(
        None,                          # No module name - use Command Line
        sz_command,                    # Command Line
        None,                          # Process handle not inheritable
        None,                          # Thread handle not inheritable
        False,                         # Inherit handles = FALSE (same as EchoCon)
        0x00080000,                    # EXTENDED_STARTUPINFO_PRESENT
        None,                          # Use parent's environment block
        None,                          # Use parent's starting directory 
        ctypes.byref(startup_info.StartupInfo),  # Pointer to STARTUPINFOW member (EXACTLY like EchoCon)
        ctypes.byref(pi_client),       # Pointer to PROCESS_INFORMATION
    )

    if not success:
        print(f"[FAIL] CreateProcessW failed: {WinError()}", flush=True)
        kernel32.DeleteProcThreadAttributeList(startup_info.lpAttributeList)
        free_func(startup_info.lpAttributeList)
        return 1

    pid = pi_client.dwProcessId
    print(f"[SETUP] Process spawned: PID={pid}", flush=True)

    # ── Wait for process ─────────────────────────────────────────────
    wait_result = kernel32.WaitForSingleObject(pi_client.hThread, 10 * 1000)
    
    if wait_result == 0:
        print(f"[PROCESS] Completed", flush=True)
    else:
        print(f"[TIMEOUT] WaitForSingleObject returned {wait_result}", flush=True)

    # Allow listening thread to catch-up with final output (from EchoCon)
    time.sleep(0.5)

    # ── CLOSEDOWN (same order as EchoCon) ────────────────────────────
    close_handle(pi_client.hThread)
    close_handle(pi_client.hProcess)
    
    kernel32.DeleteProcThreadAttributeList(startup_info.lpAttributeList)
    free_func(startup_info.lpAttributeList)

    kernel32.ClosePseudoConsole(h_pc)

    close_handle(h_pipe_in)
    close_handle(h_pipe_out)

    listener_thread.join(timeout=1.0)

    # ── Report results ───────────────────────────────────────────────
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