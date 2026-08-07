"""
Ultra-minimal ConPTY test — stripped down to absolute basics.
Tests if ConPTY output capture works at all via Python ctypes on this system.
"""

import ctypes
from ctypes import wintypes
import threading, time, sys

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

k32.CloseHandle.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]

k32.CreateProcessW.restype = wintypes.BOOL
k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.BOOL, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPCWSTR,
                                ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]

k32.WaitForSingleObject.restype = wintypes.DWORD
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

def main():
    print("=== Minimal ConPTY Test ===", flush=True)
    
    # Create pipes: input pipe (ConPTY reads from read_end_in), output pipe (ConPTY writes to write_end_out)
    read_end_in = ctypes.c_void_p()
    write_end_in = ctypes.c_void_p()
    k32.CreatePipe(ctypes.byref(read_end_in), ctypes.byref(write_end_in), None, 0)
    
    read_end_out = ctypes.c_void_p()
    write_end_out = ctypes.c_void_p()
    k32.CreatePipe(ctypes.byref(read_end_out), ctypes.byref(write_end_out), None, 0)
    
    # Create ConPTY with NO flags (not even INHERIT_CURSOR)
    size = COORD(80, 24)
    hpc = ctypes.c_void_p()
    hr = k32.CreatePseudoConsole(size, read_end_in.value, write_end_out.value, 0, ctypes.byref(hpc))
    
    # Close our copies — ConPTY owns these now
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
    
    # Spawn cmd.exe directly (no /c wrapper) — interactive mode like EchoCon does with ping
    cmdline = ctypes.c_wchar_p('cmd.exe')
    pi = PROCESS_INFORMATION()
    
    print("Spawning cmd.exe...", flush=True)
    
    if not k32.CreateProcessW(None, cmdline, None, None, False, 0x00080000, None, None,
                               ctypes.byref(si.StartupInfo), ctypes.byref(pi)):
        print(f"CreateProcessW failed: {ctypes.WinError()}", flush=True)
        return 1
    
    pid = pi.dwProcessId
    print(f"cmd.exe spawned: PID={pid}", flush=True)
    
    k32.CloseHandle(pi.hThread)
    k32.DeleteProcThreadAttributeList(attr_buf)
    
    time.sleep(0.5)
    
    # Try reading directly from read_end_out (where ConPTY writes)
    print("Attempting to read from ConPTY output pipe...", flush=True)
    
    buf = ctypes.create_string_buffer(4096)
    bytes_read = wintypes.DWORD()
    
    success = k32.ReadFile(read_end_out.value, buf, 4096, ctypes.byref(bytes_read), None)
    err = ctypes.get_last_error()
    
    print(f"ReadFile result: success={success}, bytes_read={bytes_read.value}, error={err}", flush=True)
    
    if success and bytes_read.value > 0:
        text = buf.raw[:bytes_read.value].decode("utf-8", errors="replace")
        print(f"[GOT DATA] {text!r}", flush=True)
    else:
        print(f"ReadFile failed or returned 0 bytes. Error: {ctypes.WinError(err)}", flush=True)
    
    # Send "exit" command via ConPTY input
    cmd_bytes = b"exit\r\n"
    buf_cmd = ctypes.create_string_buffer(cmd_bytes)
    bytes_written = wintypes.DWORD()
    k32.WriteFile(write_end_in.value, buf_cmd, len(cmd_bytes), ctypes.byref(bytes_written), None)
    print(f"Wrote {bytes_written.value} bytes to ConPTY input", flush=True)
    
    # Wait for cmd.exe to exit
    wait = k32.WaitForSingleObject(pi.hProcess, 5000)
    print(f"WaitForSingleObject returned: {wait}", flush=True)
    
    # Try reading again after exit
    print("Attempting second read from ConPTY output pipe...", flush=True)
    buf2 = ctypes.create_string_buffer(4096)
    bytes_read2 = wintypes.DWORD()
    success2 = k32.ReadFile(read_end_out.value, buf2, 4096, ctypes.byref(bytes_read2), None)
    err2 = ctypes.get_last_error()
    
    print(f"ReadFile #2 result: success={success2}, bytes_read={bytes_read2.value}, error={err2}", flush=True)
    
    if success2 and bytes_read2.value > 0:
        text2 = buf2.raw[:bytes_read2.value].decode("utf-8", errors="replace")
        print(f"[GOT DATA #2] {text2!r}", flush=True)
    else:
        print(f"ReadFile #2 failed or returned 0 bytes. Error: {ctypes.WinError(err2)}", flush=True)
    
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