# ConPTY Diagnostic Report

**Date**: 2026-08-07  
**Investigator**: cd_async_shell_conpty_diag  
**Issue**: Async shell spawns processes with ConPTY but no output is captured or displayed (blank console window, heartbeats show "No new output").

## Executive Summary

After extensive testing with 10 diagnostic scripts, the root cause has been identified:

**The `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` attribute is NOT being applied when spawning processes.** This means child processes are NOT attached to the pseudoconsole — their output goes elsewhere (subprocess.PIPE or parent console), and the ConPTY output handle receives zero data. The reader thread blocks forever waiting for data that never arrives.

## Key Findings

### Finding 1: Reader Thread Blocks Indefinitely
- All tests show the reader thread's first `ReadFile` call blocks until cleanup, then returns ERROR_BROKEN_PIPE (109)
- This means NO data ever reaches the ConPTY output handle during process execution
- Basic anonymous pipe ReadFile works correctly in this environment (verified separately)

### Finding 2: PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE Not Applied with subprocess.Popen
- Test v5 proved that when using `subprocess.Popen` with stdout=PIPE, output appears in the PIPE even WITH ConPTY attribute set
- Same behavior without PIPE — output doesn't go through ConPTY
- **Root cause**: `async_shell.py` creates STARTUPINFOEXW with ConPTY attribute (lines 489-521) but does NOT pass it to subprocess.Popen via the `startupinfo=` parameter

### Finding 3: Even Raw CreateProcessW Fails in This Environment
- Tests v7-v10 used raw ctypes calls to CreateProcessW with properly constructed STARTUPINFOEXW
- All failed with identical behavior — zero bytes captured, reader blocks until pipe closes
- Parameters verified correct: cb=112 (sizeof STARTUPINFOEXW), EXTENDED_STARTUPINFO_PRESENT flag set, attribute list properly initialized

### Finding 4: Anonymous Pipe + ConPTY Issue
- Anonymous pipes created via CreatePipe() do NOT support overlapped I/O
- Synchronous ReadFile on anonymous pipe blocks until data available OR write end closes
- When process isn't attached to ConPTY, no data is written to the ConPTY output handle, so ReadFile blocks forever

## Root Cause in async_shell.py

**Location**: `agent_cascade/async_shell.py`, lines 528-538

```python
# si_user (STARTUPINFOEXW) created above with PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE

try:
    proc = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        shell=True,
        stdin=None,
        stdout=subprocess.PIPE,  # Not used for output — ConPTY pipe is the source of truth
        stderr=subprocess.PIPE,
        creationflags=creation_flags_user,  # Includes EXTENDED_STARTUPINFO_PRESENT
        env=_WIN_ENV,
    )
```

**The bug**: `startupinfo=` parameter is missing! The STARTUPINFOEXW with the ConPTY attribute list is created but never passed to CreateProcess. Python's subprocess module creates its own default STARTUPINFO instead.

## Recommended Fix

### Option A: Pass STARTUPINFOEXW to subprocess.Popen
```python
# Convert ctypes STARTUPINFOEXW to something Popen can use
import struct

startupinfo = subprocess.STARTUPINFO()
startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startupinfo.wShowWindow = subprocess.SW_HIDE

# Build attribute list as bytes for EXTENDED_STARTUPINFO_PRESENT
# This is complex because Python's subprocess doesn't directly support STARTUPINFOEXW
```

### Option B: Use ctypes CreateProcessW Directly (Recommended)
Replace the subprocess.Popen call with a direct ctypes call to CreateProcessW, passing the STARTUPINFOEXW properly. This matches how the Microsoft EchoCon sample works.

Note: Even raw CreateProcessW failed in our tests (v7-v10), suggesting there may be an additional issue specific to this environment that needs investigation.

### Option C: Use NamedPipes Instead of Anonymous Pipes
Anonymous pipes don't support overlapped I/O, which causes the reader thread to block. NamedPipes created with FILE_FLAG_OVERLAPPED would allow non-blocking reads.

## Test Scripts Created

All test scripts are in `N:\work\WD\AgentCascade\`:
- `test_conpty_diag.py` — v1: basic ConPTY test
- `test_conpty_diag2.py` — v2: EchoCon-style (no inheritable handles)
- `test_conpty_diag3.py` — v3: overlapped I/O attempt
- `test_conpty_diag4.py` — v4: subprocess.Popen with ConPTY setup
- `test_conpty_diag5.py` — v5: proved PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE not applied
- `test_conpty_diag6.py` — v6: subprocess.Popen without PIPE
- `test_conpty_diag7_final.py` — v7: raw CreateProcessW with STARTUPINFOEXW
- `test_conpty_diag8.py` — v8: exhaustive debug logging
- `test_conpty_diag9.py` — v9: attempted _subprocess.CreateProcessW
- `test_conpty_diag10.py` — v10: interactive cmd.exe with input

## Conclusion

The async shell's ConPTY implementation has a fundamental bug where the ConPTY attachment attribute is never passed to CreateProcess. Additionally, there appears to be an environment-specific issue preventing even raw CreateProcessW from properly attaching processes to ConPTY. Further investigation is needed to determine if this is a Windows version issue, Python ctypes issue, or something else.

**Immediate action**: Fix async_shell.py to pass STARTUPINFOEXW to subprocess.Popen or switch to raw CreateProcessW.