"""Ultra-minimal CreateProcessW test — just verify it works with STARTUPINFOEXW."""

import ctypes
from ctypes import wintypes
import sys
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

def main():
    print("=== Simple CreateProcessW Test ===")

    # Prepare STARTUPINFOEXW with empty attribute list
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)

    size_attr = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_attr))
    attr_buf = ctypes.create_string_buffer(size_attr.value)
    if not kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size_attr)):
        raise RuntimeError(f"Init failed: {ctypes.WinError()}")

    si.lpAttributeList = ctypes.addressof(attr_buf)

    # CreateProcessW for a simple command
    cmdline = "cmd.exe /c echo test && exit"
    pi = PROCESS_INFORMATION()

    print(f"CreateProcessW: {cmdline}")
    success = kernel32.CreateProcessW(
        None, ctypes.c_wchar_p(cmdline),
        None, None, False,
        EXTENDED_STARTUPINFO_PRESENT | 0x00000200,
        None, None,
        ctypes.byref(si), ctypes.byref(pi),
    )

    if not success:
        err = ctypes.get_last_error()
        print(f"CreateProcessW FAILED: error={err}, {ctypes.WinError(err)}")
        kernel32.DeleteProcThreadAttributeList(attr_buf)
        return False

    print(f"SUCCESS: PID={pi.dwProcessId}")
    
    kernel32.DeleteProcThreadAttributeList(attr_buf)
    kernel32.CloseHandle(pi.hThread)

    # Wait for process
    result = _winapi.WaitForSingleObject(pi.hProcess, 5000)
    code = _winapi.GetExitCodeProcess(pi.hProcess)
    print(f"Wait result={result}, exit code={code}")

    kernel32.CloseHandle(pi.hProcess)
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)