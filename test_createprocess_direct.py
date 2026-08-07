"""Minimal direct CreateProcessW test with STARTUPINFOEXW and PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE."""

import ctypes
from ctypes import wintypes
import sys
import time

kernel32 = ctypes.windll.kernel32

# Structures
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

# Constants
PSEUDOCONSOLE_INHERIT_CURSOR = 0x1
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
HANDLE_FLAG_INHERIT = 0x00000001
STARTF_USESTDHANDLES = 0x00000100

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
    kernel32.SetHandleInformation(read_h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
    return read_h.value, write_h.value

def close_handle(h):
    if h and h != -1:
        kernel32.CloseHandle(h)

def main():
    print("=== Direct CreateProcessW + ConPTY Test ===")

    # Step 1: Create pipes for ConPTY I/O
    pty_in_read, pty_in_write = create_pipe()
    pty_out_read, pty_out_write = create_pipe()
    print(f"Pipes created: in_read={pty_in_read}, in_write={pty_in_write}")
    print(f"             out_read={pty_out_read}, out_write={pty_out_write}")

    # Step 2: Create pseudoconsole
    size = COORD(120, 50)
    pty_handle = ctypes.c_void_p()
    result = kernel32.CreatePseudoConsole(
        size, wintypes.HANDLE(pty_in_read), wintypes.HANDLE(pty_out_write),
        PSEUDOCONSOLE_INHERIT_CURSOR, ctypes.byref(pty_handle)
    )
    if result != 0:
        raise RuntimeError(f"CreatePseudoConsole failed: 0x{result:X}")
    print(f"ConPTY created: handle={pty_handle.value}")

    # Close our copies — ConPTY owns them
    close_handle(pty_in_read)
    close_handle(pty_out_write)

    # Step 3: Prepare STARTUPINFOEXW with PSEUDOCONSOLE attribute
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    si.StartupInfo.hStdInput = wintypes.HANDLE(0)
    si.StartupInfo.hStdOutput = wintypes.HANDLE(0)
    si.StartupInfo.hStdError = wintypes.HANDLE(0)

    size_attr = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_attr))
    print(f"Attribute list size: {size_attr.value}")
    attr_buf = ctypes.create_string_buffer(size_attr.value)
    if not kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size_attr)):
        raise RuntimeError(f"InitializeProcThreadAttributeList failed: {ctypes.WinError()}")

    pty_val = ctypes.c_void_p(pty_handle.value)
    if not kernel32.UpdateProcThreadAttribute(
        attr_buf, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.byref(pty_val), ctypes.sizeof(ctypes.c_void_p), None, None
    ):
        raise RuntimeError(f"UpdateProcThreadAttribute failed: {ctypes.WinError()}")

    si.lpAttributeList = ctypes.addressof(attr_buf)
    print("STARTUPINFOEXW prepared with PSEUDOCONSOLE attribute")

    # Step 4: CreateProcessW for cmd.exe
    cmdline = "cmd.exe /c echo Hello from ConPTY && exit"
    pi = PROCESS_INFORMATION()

    print(f"Calling CreateProcessW with cmdline: {cmdline}")
    success = kernel32.CreateProcessW(
        None,
        ctypes.c_wchar_p(cmdline),
        None, None,
        False,  # bInheritHandles
        EXTENDED_STARTUPINFO_PRESENT | 0x00000200,  # CREATE_NEW_PROCESS_GROUP
        None,  # env (inherit)
        None,  # cwd (inherit)
        ctypes.byref(si),
        ctypes.byref(pi),
    )

    if not success:
        err = ctypes.get_last_error()
        raise RuntimeError(f"CreateProcessW failed with error {err}: {ctypes.WinError(err)}")

    print(f"Process started: PID={pi.dwProcessId}")

    kernel32.DeleteProcThreadAttributeList(attr_buf)
    close_handle(pi.hThread)  # We don't need thread handle

    # Step 5: Read output from ConPTY
    buffer = ctypes.create_string_buffer(65536)
    all_output = bytearray()
    
    import _winapi
    for _ in range(20):  # max 2 seconds
        result = _winapi.WaitForSingleObject(pi.hProcess, 100)
        if result == 0:  # process finished
            break
        
        bytes_read = wintypes.DWORD()
        ok = kernel32.ReadFile(
            wintypes.HANDLE(pty_out_read), buffer, len(buffer),
            ctypes.byref(bytes_read), None
        )
        if ok and bytes_read.value > 0:
            all_output.extend(buffer.raw[:bytes_read.value])

    # Wait for process to finish
    _winapi.WaitForSingleObject(pi.hProcess, 5000)
    
    # Read remaining output
    bytes_read = wintypes.DWORD()
    ok = kernel32.ReadFile(
        wintypes.HANDLE(pty_out_read), buffer, len(buffer),
        ctypes.byref(bytes_read), None
    )
    if ok and bytes_read.value > 0:
        all_output.extend(buffer.raw[:bytes_read.value])

    # Decode output (ConPTY uses UTF-16)
    text = all_output.decode('utf-16', errors='replace')
    print(f"\nCaptured output ({len(text)} chars):")
    print(repr(text))

    # Cleanup
    close_handle(pi.hProcess)
    close_handle(pty_out_read)
    close_handle(pty_in_write)
    kernel32.ClosePseudoConsole(pty_handle)

    if "Hello from ConPTY" in text:
        print("\n✓ TEST PASSED")
        return True
    else:
        print("\n✗ TEST FAILED: Expected output not found")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)