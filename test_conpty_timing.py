"""
ConPTY test with detailed timing to see if process exits immediately.
"""

import ctypes
from ctypes import wintypes
import sys, time

k32 = ctypes.windll.kernel32

class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]

class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

k32.CreatePseudoConsole.restype = wintypes.LONG
k32.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE,
                                     wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]

k32.ClosePseudoConsole.restype = None
k32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]

k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, ctypes.c_long,
                                                   ctypes.c_long, ctypes.POINTER(ctypes.c_size_t)]

k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_ulonglong,
                                           ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]

k32.DeleteProcThreadAttributeList.restype = None
k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

k32.CreatePipe.restype = wintypes.BOOL
k32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                            ctypes.c_void_p, wintypes.DWORD]

k32.ReadFile.restype = wintypes.BOOL
k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

k32.WriteFile.restype = wintypes.BOOL
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

k32.CloseHandle.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]

k32.CreateProcessW.restype = wintypes.BOOL
k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.BOOL, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPCWSTR,
                                ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]

k32.WaitForSingleObject.restype = wintypes.DWORD
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

k32.GetExitCodeProcess.restype = wintypes.BOOL
k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

def main():
    print("=== ConPTY Timing Test ===", flush=True)
    
    # Create pipes
    read_end_in = ctypes.c_void_p()
    write_end_in = ctypes.c_void_p()
    k32.CreatePipe(ctypes.byref(read_end_in), ctypes.byref(write_end_in), None, 0)
    
    read_end_out = ctypes.c_void_p()
    write_end_out = ctypes.c_void_p()
    k32.CreatePipe(ctypes.byref(read_end_out), ctypes.byref(write_end_out), None, 0)
    
    # Create ConPTY
    size = COORD(80, 24)
    hpc = ctypes.c_void_p()
    hr = k32.CreatePseudoConsole(size, read_end_in.value, write_end_out.value, 0, ctypes.byref(hpc))
    k32.CloseHandle(read_end_in)
    k32.CloseHandle(write_end_out)
    
    if hr != 0:
        print(f"CreatePseudoConsole failed: {ctypes.WinError(hr)}", flush=True)
        return 1
    
    print(f"ConPTY created: handle={hpc.value}", flush=True)
    
    # Set up attribute list
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    
    attr_size = ctypes.c_size_t()
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
    attr_buf = ctypes.create_string_buffer(attr_size.value)
    si.lpAttributeList = ctypes.addressof(attr_buf)
    k32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(attr_size))
    
    pty_handle_val = ctypes.c_void_p(hpc.value)
    if not k32.UpdateProcThreadAttribute(attr_buf, 0, 0x00020016,
                                          ctypes.byref(pty_handle_val), ctypes.sizeof(ctypes.c_void_p), None, None):
        print(f"UpdateProcThreadAttribute failed: {ctypes.WinError()}", flush=True)
        return 1
    
    # Spawn cmd.exe with a long-running command so it doesn't exit immediately
    cmdline = ctypes.c_wchar_p('cmd.exe /k echo READY')
    pi = PROCESS_INFORMATION()
    
    print("Spawning cmd.exe /k echo READY...", flush=True)
    t0 = time.time()
    
    if not k32.CreateProcessW(None, cmdline, None, None, False, 0x00080000, None, None,
                               ctypes.byref(si.StartupInfo), ctypes.byref(pi)):
        print(f"CreateProcessW failed: {ctypes.WinError()}", flush=True)
        return 1
    
    pid = pi.dwProcessId
    t1 = time.time()
    print(f"cmd.exe spawned: PID={pid} (took {t1-t0:.3f}s)", flush=True)
    
    k32.CloseHandle(pi.hThread)
    k32.DeleteProcThreadAttributeList(attr_buf)
    
    # Check immediately if process is alive
    exit_code = wintypes.DWORD()
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
    print(f"Initial exit code: {exit_code.value}", flush=True)
    
    # Try reading from output immediately after spawn (before any sleep)
    t2 = time.time()
    buf = ctypes.create_string_buffer(4096)
    bytes_read = wintypes.DWORD()
    success = k32.ReadFile(read_end_out.value, buf, 4096, ctypes.byref(bytes_read), None)
    err = ctypes.get_last_error()
    t3 = time.time()
    
    print(f"ReadFile #1 (immediate): success={success}, bytes={bytes_read.value}, error={err} (took {t3-t2:.3f}s)", flush=True)
    
    if success and bytes_read.value > 0:
        text = buf.raw[:bytes_read.value].decode("utf-8", errors="replace")
        print(f"[DATA] {text!r}", flush=True)
    
    # Wait a bit more and check again
    time.sleep(1.0)
    
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
    print(f"Exit code after 1s: {exit_code.value}", flush=True)
    
    # Try reading again with short timeout
    buf2 = ctypes.create_string_buffer(4096)
    bytes_read2 = wintypes.DWORD()
    success2 = k32.ReadFile(read_end_out.value, buf2, 4096, ctypes.byref(bytes_read2), None)
    err2 = ctypes.get_last_error()
    
    print(f"ReadFile #2 (after sleep): success={success2}, bytes={bytes_read2.value}, error={err2}", flush=True)
    
    if success2 and bytes_read2.value > 0:
        text2 = buf2.raw[:bytes_read2.value].decode("utf-8", errors="replace")
        print(f"[DATA #2] {text2!r}", flush=True)
    
    # Try writing to input
    cmd_bytes = b"echo TEST123\r\n"
    buf_cmd = ctypes.create_string_buffer(cmd_bytes)
    bytes_written = wintypes.DWORD()
    write_ok = k32.WriteFile(write_end_in.value, buf_cmd, len(cmd_bytes), ctypes.byref(bytes_written), None)
    write_err = ctypes.get_last_error()
    
    print(f"WriteFile to input: success={write_ok}, bytes_written={bytes_written.value}, error={write_err}", flush=True)
    
    # Cleanup
    k32.CloseHandle(pi.hProcess)
    k32.ClosePseudoConsole(hpc)
    k32.CloseHandle(read_end_out)
    k32.CloseHandle(write_end_in)
    
    print("Done.", flush=True)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"EXCEPTION: {e}", flush=True)
        import traceback; traceback.print_exc()
        sys.exit(2)