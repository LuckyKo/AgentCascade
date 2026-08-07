"""Test with bInheritHandles=TRUE."""

import os, sys, time, ctypes
from ctypes import wintypes
import _winapi
import threading

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
HANDLE_FLAG_INHERIT = 0x00000001

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

kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION),
]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

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
    print("=== ConPTY Test with bInheritHandles=TRUE ===")

    # Create pipes — make them inheritable
    pty_in_read, pty_in_write = create_pipe()
    pty_out_read, pty_out_write = create_pipe()
    
    kernel32.SetHandleInformation(pty_in_read, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
    kernel32.SetHandleInformation(pty_out_write, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

    # Create ConPTY
    size = COORD(120, 50)
    pty_handle = ctypes.c_void_p()
    result = kernel32.CreatePseudoConsole(
        size, wintypes.HANDLE(pty_in_read), wintypes.HANDLE(pty_out_write),
        0, ctypes.byref(pty_handle)
    )
    if result != 0:
        raise RuntimeError(f"CreatePseudoConsole failed: 0x{result:X}")

    # Close our copies
    close_handle(pty_in_read)
    close_handle(pty_out_write)

    # STARTUPINFOEXW with PSEUDOCONSOLE
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)

    size_attr = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_attr))
    attr_buf = ctypes.create_string_buffer(size_attr.value)
    kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size_attr))

    pty_val = ctypes.c_void_p(pty_handle.value)
    if not kernel32.UpdateProcThreadAttribute(
        attr_buf, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.byref(pty_val), ctypes.sizeof(ctypes.c_void_p), None, None
    ):
        raise RuntimeError(f"UpdateAttr failed: {ctypes.WinError()}")

    si.lpAttributeList = ctypes.addressof(attr_buf)

    # Try with bInheritHandles=TRUE
    cmdline = "cmd.exe /c echo Hello && exit"
    pi = PROCESS_INFORMATION()

    print(f"Spawning with INHERIT_HANDLES=TRUE: {cmdline}")
    success = kernel32.CreateProcessW(
        None, ctypes.c_wchar_p(cmdline),
        None, None,
        True,  # bInheritHandles = TRUE !!
        EXTENDED_STARTUPINFO_PRESENT | 0x00000200,
        None, None,
        ctypes.byref(si), ctypes.byref(pi),
    )

    if not success:
        err = ctypes.get_last_error()
        print(f"CreateProcessW FAILED: {err} - {ctypes.WinError(err)}")
        return False

    print(f"Process started: PID={pi.dwProcessId}")
    
    kernel32.DeleteProcThreadAttributeList(attr_buf)
    close_handle(pi.hThread)

    # Wait for process
    result = _winapi.WaitForSingleObject(pi.hProcess, 5000)
    code = _winapi.GetExitCodeProcess(pi.hProcess)
    print(f"Process exited: code={code}")
    close_handle(pi.hProcess)

    # Close stdin and try reading
    close_handle(pty_in_write)
    time.sleep(0.5)

    all_data = bytearray()
    
    def do_read():
        buf = ctypes.create_string_buffer(65536)
        bytes_read = wintypes.DWORD()
        ok = kernel32.ReadFile(
            wintypes.HANDLE(pty_out_read), buf, len(buf),
            ctypes.byref(bytes_read), None
        )
        print(f"ReadFile: ok={ok}, bytes={bytes_read.value}, err={ctypes.get_last_error()}")
        if ok and bytes_read.value > 0:
            all_data.extend(buf.raw[:bytes_read.value])

    reader = threading.Thread(target=do_read)
    reader.daemon = True
    reader.start()
    reader.join(timeout=3)
    
    if reader.is_alive():
        print("Blocking, closing ConPTY...")
        kernel32.ClosePseudoConsole(pty_handle)
        reader.join(timeout=2)
    else:
        close_handle(pty_out_read)
        kernel32.ClosePseudoConsole(pty_handle)

    print(f"Data: {len(all_data)} bytes")
    if all_data:
        text = all_data.decode('utf-16-le', errors='replace')
        print(repr(text))

    return len(all_data) > 0 and "Hello" in all_data.decode('utf-16-le', errors='replace')

if __name__ == "__main__":
    success = main()
    print("✓ PASSED" if success else "✗ FAILED")
    sys.exit(0 if success else 1)