"""Test CreateProcessW with custom environment block."""

import ctypes
from ctypes import wintypes
import sys
import os
import _winapi

kernel32 = ctypes.windll.kernel32

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

EXTENDED_STARTUPINFO_PRESENT = 0x00080000

kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
kernel32.InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p, ctypes.c_long, ctypes.c_long, ctypes.POINTER(ctypes.c_size_t),
]

kernel32.DeleteProcThreadAttributeList.restype = None
kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION),
]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

def build_env_block(env):
    """Build Windows env block: KEY=VALUE\0KEY=VALUE\0...\0\0 as UTF-16-LE bytes."""
    parts = []
    for k, v in sorted(env.items()):
        parts.append(f"{k}={v}")
    block_str = '\0'.join(parts) + '\0\0'
    return block_str.encode('utf-16-le')

def main():
    print("=== CreateProcessW with Custom Env Block Test ===")

    env = os.environ.copy()
    env['TEST_VAR'] = 'hello_world'

    env_bytes = build_env_block(env)
    print(f"Env block size: {len(env_bytes)} bytes")
    
    # Try different ways to pass the env block pointer
    for method in ['create_string_buffer', 'cast+buffer']:
        print(f"\nTrying method: {method}")
        
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)

        size_attr = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_attr))
        attr_buf = ctypes.create_string_buffer(size_attr.value)
        kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size_attr))
        si.lpAttributeList = ctypes.addressof(attr_buf)

        pi = PROCESS_INFORMATION()

        if method == 'create_string_buffer':
            buf = ctypes.create_string_buffer(env_bytes, len(env_bytes))  # explicit size!
            env_ptr = ctypes.cast(ctypes.addressof(buf), ctypes.c_void_p)
        else:
            buf = ctypes.create_string_buffer(env_bytes, len(env_bytes))
            env_ptr = ctypes.cast(ctypes.addressof(buf), ctypes.c_void_p)

        cmdline = "cmd.exe /c echo %TEST_VAR% && exit"
        
        success = kernel32.CreateProcessW(
            None, ctypes.c_wchar_p(cmdline),
            None, None, False,
            EXTENDED_STARTUPINFO_PRESENT | 0x00000200,
            env_ptr, None,
            ctypes.byref(si), ctypes.byref(pi),
        )

        kernel32.DeleteProcThreadAttributeList(attr_buf)

        if not success:
            err = ctypes.get_last_error()
            print(f"  FAILED: error={err}, {ctypes.WinError(err)}")
        else:
            print(f"  SUCCESS: PID={pi.dwProcessId}")
            _winapi.WaitForSingleObject(pi.hProcess, 5000)
            code = _winapi.GetExitCodeProcess(pi.hProcess)
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(pi.hProcess)
            print(f"  Exit code: {code}")

    return True

if __name__ == "__main__":
    main()