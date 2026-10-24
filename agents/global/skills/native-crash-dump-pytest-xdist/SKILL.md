---
name: native-crash-dump-pytest-xdist
description: Diagnose intermittent hard process deaths in parallel pytest-xdist workers where faulthandler writes empty dumps — victim-vs-cause framing, cheap A/B deselect test, and reading procdump per-worker logs to extract the real exception code when no dump is written.
source: auto-generated
version: 1.1.0
triggers:
  - "xdist worker crash"
  - "assert not crashitem"
  - "faulthandler empty dump"
  - "procdump no dump"
  - "0xC0000005"
  - "0x406D1388"
  - "thread-naming exception"
  - "pytest exit code 3"
generated_by: orchestrator
generated_from_task: "Root-cause intermittent xdist worker hard-death flake (victim TestCLIMode, faulthandler empty dumps, procdump -e/-b both fail to capture)"
---

## Goal
Root-cause an intermittent, hard process death in a parallel pytest-xdist suite where Python's `faulthandler` is useless (empty dumps) and the "crashed" test is a **victim**, not the cause. Includes how to extract the real exception code from procdump logs even when no memory dump is written.

## Procedure
### Step 1 — Recognize the signature (confirm it's native, not Python)
Symptom set = native hard crash: `INTERNALERROR: assert not crashitem` naming a random in-flight test + exit code 3 + the named test **passes in isolation** + `faulthandler` (even interpreter-level `-X faulthandler` with an eagerly-opened file) writes **0-byte** dumps. A Python exception would produce a traceback; empty dumps mean the OS killed the process before the handler flushed → investigate C-level causes, not Python logic.

### Step 2 — Separate victim from cause
The test xdist names is whatever was **in-flight** when the faulting thread fired. Do NOT debug that test's own code. Ask instead: (a) is that test late in its worker's sequence (late-collected file, last-in-class) → higher prior of being in-flight at death? (b) what **background/armed resource** from an EARLIER test is still alive and could tick against torn-down state? Confirm the test's resources ARE cleaned up (e.g. does the conftest teardown fixture actually capture/stop what the test creates — verify import chains resolve to the SAME class object).

### Step 3 — Cheap A/B first (do this before dumping)
Deselect the suspected arming test and re-run the suite several times (match the base rate, e.g. ×5–10 for a ~1-in-3 flake):
```
python -m pytest -q -k "not <suspect_test_name>"
```
If the flake **vanishes** and reappears when included → causal confirmation without any debugger. This is the highest-value, lowest-cost step. (Note: an A/B that does NOT change the crash rate DISPROVES that suspect — record it as ruled out.)

### Step 4 — Attach procdump SAFELY to short-lived workers (NOT a live interpreter)
**CRITICAL SAFETY:** NEVER attach procdump (or any debugger) to a long-lived python.exe you're not sure is a throwaway test worker. Attaching a debug handle to a live interpreter (e.g. the AgentCascade runtime / your own orchestrator process) can **freeze or crash it**. Only ever attach to PIDs you've positively identified as children of the pytest run.

**Identifying xdist workers by PID-diff, not cmdline:** xdist workers are spawned via execnet/multiprocessing and their cmdline is just `python -u -c "import sys;exec(eval(sys.stdi..."` — it does **NOT** contain 'pytest'. So: snapshot all python.exe PIDs *before* launching pytest, then attach only to NEW PIDs that (a) appeared after launch AND (b) match a worker signature (`'import sys;exec(eval(sys.stdi' in cmd` or `'pytest' in cmd`). Belt-and-suspenders — never rely on one filter alone.

A driver loop is the reliable pattern: loop `pytest -n N`, and on each attempt attach procdump to every newly-appearing worker PID, until a crash reproduces (these flakes are ~1-in-3 to 1-in-4).

### Step 5 — READ THE PROCDUMP LOGS EVEN WHEN NO DUMP IS WRITTEN (the key move)
**`procdump -ma -e` AND `-ma -e -b` (first-chance) BOTH often fail to write a `.dmp`** for this crash class — the death is hard enough to bypass all debugger exception hooks. Do NOT conclude "no data". Instead, redirect each worker's procdump stdout to its own log file and read them:
```
procdump64.exe -accepteula -ma -e -b -n 3 -f "OUTDIR\att%d_w<pid>_%p.dmp" <pid>   # stdout -> per-worker .log
```
Then, after the crash:
- **Find the dying worker** = the one whose log shows a **non-zero exit code** (`Process Exit: PID NNNN, Exit Code 0x00000001`); the other workers show `0x00000000`. That PID is your `gwN`.
- **Extract the exception code** from that worker's log lines like `[HH:MM:SS]Exception: 406D1388`. This is the actual fault signature — far more informative than "no dump".

### Step 6 — Decode the exception code (this often redirects the whole investigation)
- `0xC0000005` = access violation → classic dangling-pointer / use-after-free.
- **`0x406D1388` = Microsoft thread-naming exception** — NOT an AV. Native libraries (ZMQ/zeromq, SDL2, etc.) raise it to label their threads for the debugger. Known issue (zeromq/libzmq#4128): it's raised when spawning I/O threads **with a non-Visual-Studio debugger attached**, and the debugger mishandles it → can kill the process. If you see `0x406D1388`, the faulting native code is very likely a library that names its threads (grep the codebase for `zmq`/`zeromq`/`CreateThread`/thread-naming). This also explains why faulthandler AND procdump both fail: the death is debugger/fail-fast-mediated, not a plain AV.
- Other codes: look up via `!errrec` in cdb or Microsoft docs.

### Step 7 — Hunt the armed/leaked resource (mechanism-specific to the code)
Native hard deaths in CPython come from: a **ctypes callback GC'd while still OS-registered** (drop the last Python ref to a `WINFUNCTYPE`/`PyCFuncPtr` → dangling C pointer the OS still holds), a daemon thread touching a **GC'd C-backed object**, a native lib's thread (ZMQ I/O/reaper) outliving its context/socket, or `ctypes` calling a freed pointer. Grep for `ctypes`/`WINFUNCTYPE`/`SetConsoleCtrlHandler`/`zmq`/`PyGILState_` in shared init paths AND TESTS (tests can arm a process-wide OS callback that outlives the test). Check whether anything ever **unregisters** it.

### Step 8 — If you truly need the native stack: WinDbg/cdb, not procdump
procdump cannot capture a `0x406D1388`/fail-fast death. To get the faulting thread's native stack you need **WinDbg or cdb** (which handle the thread-naming exception correctly), typically via the Windows `AeDebug` post-mortem registry key so the OS itself launches the debugger on the next crash. This requires installing a debugger — coordinate with the user.

## Tips
- "Thread count alone is not the trigger" is a real signal: if raising stack size / capping `-n` doesn't help, the fault is a specific poisoned resource or a native-lib interaction, not accumulation.
- Distinguish **confirmed** (code-verified arming) from **inferred** (the trigger event). State both; confirm the inferred part with Step 5/6.
- A test that installs a real OS-level callback (console handler, signal handler via ctypes) and then nulls the reference is a prime suspect — make such tests use a **fake** kernel32/dependency so nothing real is registered in a worker.
- The victim being *consistently* one specific test class (e.g. `TestCLIMode`) is a strong clue: that test builds **real** pools/tools that initialize native libs (ZMQ, etc.) — the crash lives in that path, not in the test's assertions.
- Save the finding to `.agent_lessons/` before context compression; it has value beyond the final answer.
