"""Test writing to ConPTY input and reading output."""

import ctypes, threading, time, sys
from ctypes import wintypes
import _winapi

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
kernel32.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]

kernel32.ClosePseudoConsole.restype = None
kernel32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]

kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_long, ctypes.POINTER(ctypes.c_size_t)]

kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_ulonglong, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]

kernel32.DeleteProcThreadAttributeList.restype = None
kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

kernel32.CreatePipe.restype = wintypes.BOOL
kernel32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD]

kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

kernel32.WriteFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

def create_pipe():
    r, w = ctypes.c_void_p(), ctypes.c_void_p()
    if not kernel32.CreatePipe(ctypes.byref(r), ctypes.byref(w), None, 0):
        raise RuntimeError(f"CreatePipe failed: {ctypes.WinError()}")
    return r.value, w.value

def close_handle(h):
    if h and h != -1:
        kernel32.CloseHandle(h)

def main():
    print("Start", flush=True)
    
    pty_in_read, pty_in_write = create_pipe()
    pty_out_read, pty_out_write = create_pipe()
    print("Pipes OK", flush=True)

    size = COORD(120, 50)
    pty_handle = ctypes.c_void_p()
    result = kernel32.CreatePseudoConsole(size, wintypes.HANDLE(pty_in_read), wintypes.HANDLE(pty_out_write), 0, ctypes.byref(pty_handle))
    print(f"ConPTY result={result}", flush=True)
    
    close_handle(pty_in_read)
    close_handle(pty_out_write)

    si = STARTUPINFOEXW()
    ctypes.memset(ctypes.byref(si), 0, ctypes.sizeof(si))
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)

    size_attr = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_attr))
    attr_buf = ctypes.create_string_buffer(size_attr.value)
    kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size_attr))
    si.lpAttributeList = ctypes.addressof(attr_buf)

    pty_val = ctypes.c_void_p(pty_handle.value)
    kernel32.UpdateProcThreadAttribute(attr_buf, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, ctypes.byref(pty_val), ctypes.sizeof(ctypes.c_void_p), None, None)

    pi = PROCESS_INFORMATION()
    success = kernel32.CreateProcessW(None, ctypes.c_wchar_p("cmd.exe"), None, None, False, EXTENDED_STARTUPINFO_PRESENT, None, None, ctypes.byref(si.StartupInfo), ctypes.byref(pi))
    
    if not success:
        print(f"CreateProcessW failed: {ctypes.get_last_error()}", flush=True)
        return False

    print(f"PID={pi.dwProcessId}", flush=True)
    kernel32.DeleteProcThreadAttributeList(attr_buf)
    close_handle(pi.hThread)

    # Start reader thread immediately
    all_data = bytearray()
    
    def reader():
        buf = ctypes.create_string_buffer(65536)
        while True:
            br = wintypes.DWORD()
            ok = kernel32.ReadFile(wintypes.HANDLE(pty_out_read), buf, len(buf), ctypes.byref(br), None)
            if not ok or br.value == 0:
                print(f"[R] stop ok={ok} bytes={br.value} err={ctypes.get_last_error()}", flush=True)
                break
            all_data.extend(buf.raw[:br.value])
            print(f"[R] +{br.value} total={len(all_data)}", flush=True)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    
    time.sleep(1)  # let cmd start
    
    # Send Enter key to trigger prompt
    data = "\r\n".encode('utf-16-le')
    buf = ctypes.create_string_buffer(data)
    written = wintypes.DWORD()
    ok = kernel32.WriteFile(wintypes.HANDLE(pty_in_write), buf, len(data), ctypes.byref(written), None)
    print(f"Write \\r\\n: ok={ok} written={written.value}", flush=True)

    time.sleep(1)
    
    # Send "echo test\r\n"
    data2 = "echo test\r\n".encode('utf-16-le')
    buf2 = ctypes.create_string_buffer(data2)
    written2 = wintypes.DWORD()
    ok2 = kernel32.WriteFile(wintypes.HANDLE(pty_in_write), buf2, len(data2), ctypes.byref(written2), None)
    print(f"Write echo: ok={ok2} written={written2.value}", flush=True)

    time.sleep(1)
    
    # Send "exit\r\n"
    data3 = "exit\r\n".encode('utf-16-le')
    buf3 = ctypes.create_string_buffer(data3)
    written3 = wintypes.DWORD()
    kernel32.WriteFile(wintypes.HANDLE(pty_in_write), buf3, len(data3), ctypes.byref(written3), None)

    t.join(timeout=5)
    
    close_handle(pty_in_write)
    close_handle(pi.hProcess)
    time.sleep(0.5)
    kernel32.ClosePseudoConsole(pty_handle)

    print(f"Total: {len(all_data)} bytes", flush=True)
    if all_data:
        text = all_data.decode('utf-16-le', errors='replace')
        print(f"Text: {repr(text[:500])}", flush=True)

if __name__ == "__main__":
    main()