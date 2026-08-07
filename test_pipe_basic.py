"""Basic pipe test to verify ReadFile works correctly with our ctypes setup."""

import ctypes
from ctypes import wintypes
import _winapi
import time

kernel32 = ctypes.windll.kernel32

kernel32.CreatePipe.restype = wintypes.BOOL
kernel32.CreatePipe.argtypes = [
    ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
    ctypes.c_void_p, wintypes.DWORD,
]

kernel32.WriteFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

def main():
    print("=== Basic Pipe ReadFile Test ===")

    read_h = ctypes.c_void_p()
    write_h = ctypes.c_void_p()
    
    if not kernel32.CreatePipe(ctypes.byref(read_h), ctypes.byref(write_h), None, 0):
        raise RuntimeError(f"CreatePipe failed: {ctypes.WinError()}")
    
    print(f"Pipes created: read={read_h.value}, write={write_h.value}")

    # Write some data
    data = b"Hello from pipe!\x00\x00"
    buf = ctypes.create_string_buffer(data)
    written = wintypes.DWORD()
    
    ok = kernel32.WriteFile(
        wintypes.HANDLE(write_h.value), buf, len(data),
        ctypes.byref(written), None
    )
    print(f"WriteFile: ok={ok}, written={written.value}")

    # Close write end to signal EOF
    kernel32.CloseHandle(write_h)

    # Read the data
    read_buf = ctypes.create_string_buffer(1024)
    bytes_read = wintypes.DWORD()
    
    ok = kernel32.ReadFile(
        wintypes.HANDLE(read_h.value), read_buf, len(read_buf),
        ctypes.byref(bytes_read), None
    )
    print(f"ReadFile: ok={ok}, bytes={bytes_read.value}, err={ctypes.get_last_error()}")
    
    if ok and bytes_read.value > 0:
        chunk = read_buf.raw[:bytes_read.value]
        print(f"Data: {chunk}")

    kernel32.CloseHandle(read_h)

if __name__ == "__main__":
    main()