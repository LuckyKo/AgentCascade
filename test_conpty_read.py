"""Test ConPTY output reading directly."""

import ctypes
from ctypes import wintypes
import sys
import _winapi
import time

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

PSEUDOCONSOLE_INHERIT_CURSOR = 0x1
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
    print("=== ConPTY Read Test ===")

    # Create pipes
    pty_in_read, pty_in_write = create_pipe()
    pty_out_read, pty_out_write = create_pipe()
    print(f"Pipes: in({pty_in_read},{pty_in_write}) out({pty_out_read},{pty_out_write})")

    # Create ConPTY
    size = COORD(120, 50)
    pty_handle = ctypes.c_void_p()
    result = kernel32.CreatePseudoConsole(
        size, wintypes.HANDLE(pty_in_read), wintypes.HANDLE(pty_out_write),
        0, ctypes.byref(pty_handle)
    )
    if result != 0:
        raise RuntimeError(f"CreatePseudoConsole failed: 0x{result:X}")
    print(f"ConPTY created")

    close_handle(pty_in_read)
    close_handle(pty_out_write)

    # Prepare STARTUPINFOEXW
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

    # CreateProcessW
    cmdline = "cmd.exe /c echo Hello from ConPTY && exit"
    pi = PROCESS_INFORMATION()

    print(f"Spawning: {cmdline}")
    success = kernel32.CreateProcessW(
        None, ctypes.c_wchar_p(cmdline),
        None, None, False,
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

    # Read from ConPTY output in a loop
    buffer = ctypes.create_string_buffer(65536)
    all_data = bytearray()
    read_attempts = 0
    max_attempts = 50
    
    print("Reading from ConPTY output...")
    
    while read_attempts < max_attempts:
        # Check if process is done
        code = _winapi.GetExitCodeProcess(pi.hProcess)
        if code != 0x102:  # not STILL_ACTIVE
            print(f"Process exited with code {code}")
            break
        
        # Try non-blocking read via WaitForSingleObject on the handle
        result = _winapi.WaitForSingleObject(pty_out_read, 100)
        if result == 0:  # signaled - data available or EOF
            bytes_read = wintypes.DWORD()
            ok = kernel32.ReadFile(
                wintypes.HANDLE(pty_out_read), buffer, len(buffer),
                ctypes.byref(bytes_read), None
            )
            if ok and bytes_read.value > 0:
                chunk = buffer.raw[:bytes_read.value]
                all_data.extend(chunk)
                print(f"Read {bytes_read.value} bytes (total: {len(all_data)})")
                print(f"Chunk hex preview: {chunk[:80].hex()}")
            elif not ok:
                err = ctypes.get_last_error()
                print(f"ReadFile failed: {err} - {ctypes.WinError(err)}")
                break
            else:
                print("ReadFile returned 0 bytes (EOF)")
                break
        read_attempts += 1

    # Final read after process exit
    time.sleep(0.5)
    bytes_read = wintypes.DWORD()
    ok = kernel32.ReadFile(
        wintypes.HANDLE(pty_out_read), buffer, len(buffer),
        ctypes.byref(bytes_read), None
    )
    if ok and bytes_read.value > 0:
        all_data.extend(buffer.raw[:bytes_read.value])
        print(f"Final read: {bytes_read.value} bytes (total: {len(all_data)})")

    # Decode as UTF-16 (ConPTY output format)
    text = all_data.decode('utf-16', errors='replace')
    print(f"\nDecoded text ({len(text)} chars):")
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