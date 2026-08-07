"""Ultra-minimal ConPTY test — just verify basics work."""

import ctypes
from ctypes import wintypes
import _winapi
import time
import sys

kernel32 = ctypes.windll.kernel32

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

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000

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

kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

def create_pipe():
    read_h = ctypes.c_void_p()
    write_h = ctypes.c_void_p()
    if not kernel32.CreatePipe(ctypes.byref(read_h), ctypes.byref(write_h), None, 0):
        raise RuntimeError(f"CreatePipe failed: {ctypes.WinError()}")
    return read_h.value, write_h.value

def close_handle(h):
    if h and h != -1:
        kernel32.CloseHandle(h)

def main():
    print("=== Ultra-minimal ConPTY Test ===", flush=True)

    pty_in_read, pty_in_write = create_pipe()
    pty_out_read, pty_out_write = create_pipe()
    print(f"Pipes created", flush=True)

    # Create ConPTY
    size = COORD(120, 50)
    pty_handle = ctypes.c_void_p()
    result = kernel32.CreatePseudoConsole(
        size, wintypes.HANDLE(pty_in_read), wintypes.HANDLE(pty_out_write),
        0, ctypes.byref(pty_handle)
    )
    print(f"CreatePseudoConsole result: {result}", flush=True)
    if result != 0:
        raise RuntimeError(f"CreatePseudoConsole failed: 0x{result:X}")

    close_handle(pty_in_read)
    close_handle(pty_out_write)
    print(f"ConPTY created, handles closed", flush=True)

    # STARTUPINFOEXW
    si = STARTUPINFOEXW()
    ctypes.memset(ctypes.byref(si), 0, ctypes.sizeof(si))
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)

    size_attr = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_attr))
    attr_buf = ctypes.create_string_buffer(size_attr.value)
    kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size_attr))
    si.lpAttributeList = ctypes.addressof(attr_buf)

    pty_val = ctypes.c_void_p(pty_handle.value)
    if not kernel32.UpdateProcThreadAttribute(
        attr_buf, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.byref(pty_val), ctypes.sizeof(ctypes.c_void_p), None, None
    ):
        raise RuntimeError(f"UpdateAttr failed: {ctypes.WinError()}")

    # Spawn cmd.exe
    cmdline = "cmd.exe"
    pi = PROCESS_INFORMATION()

    print(f"CreateProcessW: {cmdline}", flush=True)
    success = kernel32.CreateProcessW(
        None, ctypes.c_wchar_p(cmdline),
        None, None, False, EXTENDED_STARTUPINFO_PRESENT,
        None, None,
        ctypes.byref(si.StartupInfo),
        ctypes.byref(pi),
    )

    if not success:
        err = ctypes.get_last_error()
        print(f"CreateProcessW FAILED: {err} - {ctypes.WinError(err)}", flush=True)
        return False

    print(f"Process started: PID={pi.dwProcessId}", flush=True)
    
    kernel32.DeleteProcThreadAttributeList(attr_buf)
    close_handle(pi.hThread)

    # Wait for process to finish or timeout
    result = _winapi.WaitForSingleObject(pi.hProcess, 3000)
    code = _winapi.GetExitCodeProcess(pi.hProcess)
    print(f"Wait result={result}, exit_code={code}", flush=True)

    close_handle(pi.hProcess)
    close_handle(pty_in_write)
    time.sleep(0.5)
    
    # Try reading with a short timeout thread
    import threading
    read_result = [None]
    
    def try_read():
        buf = ctypes.create_string_buffer(65536)
        bytes_read = wintypes.DWORD()
        ok = kernel32.ReadFile(wintypes.HANDLE(pty_out_read), buf, len(buf), ctypes.byref(bytes_read), None)
        read_result[0] = (ok, bytes_read.value, ctypes.get_last_error())
    
    t = threading.Thread(target=try_read)
    t.daemon = True
    t.start()
    t.join(timeout=2)
    
    print(f"ReadFile result: {read_result[0]}", flush=True)
    
    close_handle(pty_out_read)
    kernel32.ClosePseudoConsole(pty_handle)

    return True

if __name__ == "__main__":
    main()