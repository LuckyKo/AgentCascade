"""ConPTY read test — use thread for reading to avoid blocking."""

import ctypes
from ctypes import wintypes
import sys
import _winapi
import time
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

def reader_thread_fn(handle, data_lock, data_buffer, stop_event):
    """Read from ConPTY output handle in a background thread."""
    buf = ctypes.create_string_buffer(65536)
    while not stop_event.is_set():
        bytes_read = wintypes.DWORD()
        ok = kernel32.ReadFile(
            wintypes.HANDLE(handle), buf, len(buf),
            ctypes.byref(bytes_read), None
        )
        if not ok or bytes_read.value == 0:
            print(f"[reader] ReadFile returned: ok={ok}, bytes={bytes_read.value}, err={ctypes.get_last_error()}")
            break
        chunk = buf.raw[:bytes_read.value]
        with data_lock:
            data_buffer.extend(chunk)
        print(f"[reader] Got {bytes_read.value} bytes, total={len(data_buffer)}")

def main():
    print("=== ConPTY Read Test (threaded) ===")

    pty_in_read, pty_in_write = create_pipe()
    pty_out_read, pty_out_write = create_pipe()

    # Create ConPTY
    size = COORD(120, 50)
    pty_handle = ctypes.c_void_p()
    result = kernel32.CreatePseudoConsole(
        size, wintypes.HANDLE(pty_in_read), wintypes.HANDLE(pty_out_write),
        0, ctypes.byref(pty_handle)
    )
    if result != 0:
        raise RuntimeError(f"CreatePseudoConsole failed: 0x{result:X}")

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

    # Spawn process
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

    # Start reader thread
    data_lock = threading.Lock()
    data_buffer = bytearray()
    stop_event = threading.Event()
    reader = threading.Thread(target=reader_thread_fn, args=(pty_out_read, data_lock, data_buffer, stop_event))
    reader.daemon = True
    reader.start()

    # Wait for process to finish (max 5 seconds)
    result = _winapi.WaitForSingleObject(pi.hProcess, 5000)
    code = _winapi.GetExitCodeProcess(pi.hProcess)
    print(f"Process exited: result={result}, code={code}")

    # Give reader thread time to finish
    time.sleep(1)
    stop_event.set()
    reader.join(timeout=2)

    # Get collected data
    with data_lock:
        all_data = bytes(data_buffer)

    print(f"\nTotal data collected: {len(all_data)} bytes")
    
    if all_data:
        print(f"First 200 bytes hex: {all_data[:200].hex()}")
        # Try UTF-16 decode (ConPTY uses UTF-16)
        text = all_data.decode('utf-16', errors='replace')
        print(f"\nDecoded as UTF-16 ({len(text)} chars):")
        print(repr(text))

    # Cleanup
    close_handle(pi.hProcess)
    close_handle(pty_out_read)  # This should unblock the reader if still waiting
    close_handle(pty_in_write)
    kernel32.ClosePseudoConsole(pty_handle)

    with data_lock:
        final_text = all_data.decode('utf-16', errors='replace') if all_data else ""
    
    if "Hello from ConPTY" in final_text:
        print("\n✓ TEST PASSED")
        return True
    else:
        print("\n✗ TEST FAILED")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)