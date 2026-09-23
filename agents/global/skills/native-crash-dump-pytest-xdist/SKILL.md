---
name: native-crash-dump-pytest-xdist
description: Diagnose intermittent native C-level crashes (Windows access violation 0xC0000005) that hard-kill a pytest-xdist worker, where faulthandler writes empty dumps. Covers victim-vs-cause framing, the cheap A/B deselect test, and procdump -ma -e dump capture.
source: auto-generated
version: "1.0.0"
triggers:
  - "xdist worker crashed"
  - "assert not crashitem"
  - "faulthandler empty dump"
  - "access violation"
  - "procdump"
  - "0xC0000005"
  - "worker process died mid-run"
  - "internalerror exit code 3"
generated_by: researcher
generated_from_task: "Investigate why TestCLIMode is the recurring victim of an intermittent xdist worker-death flake (Windows access violation) in the AgentCascade pytest suite."
---

## Goal
Root-cause an intermittent, hard process death (native access violation) in a parallel pytest-xdist suite where Python's `faulthandler` is useless (empty dumps), and the "crashed" test is a **victim**, not the cause.

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
If the flake **vanishes** and reappears when included → causal confirmation without any debugger. This is the highest-value, lowest-cost step.

### Step 4 — Capture the native AV stack (only if A/B is inconclusive)
Sysinternals procdump — monitor by **image name** (the suite spawns many short-lived `python.exe` workers, so don't attach to one PID):
```
procdump64.exe -ma -e -f "OUTDIR\python_%y%m%d_%H%M%S_%p.dmp" python.exe
```
- `-ma` = full user+kernel dump (needed to resolve dangling pointers/native stacks); `-e` = trigger on unhandled exception; `%p` = PID.
- Map the dump's PID to the dying worker: xdist prints each worker PID at startup (`gwN pid=NNNNN`); cross-reference.
- Open in WinDbg/cdb: the **faulting thread** is the answer. A pool/daemon thread (name it, e.g. `skill-*`, `async_tool`) vs a **console-control / ctypes callback / raw control thread** tells you the mechanism.

### Step 5 — Hunt the armed/leaked resource
Native AVs in CPython come from: a **ctypes callback GC'd while still OS-registered** (drop the last Python ref to a `WINFUNCTYPE`/`PyCFuncPtr` object → dangling C pointer the OS still holds — a classic pitfall; the code often has a "keep a reference" comment), a daemon thread touching a **GC'd C-backed object** (pydantic-core/numpy), or `ctypes` calling a freed pointer. Grep for `ctypes`/`WINFUNCTYPE`/`SetConsoleCtrlHandler`/`PyGILState_` in shared init paths and TESTS (tests can arm a process-wide OS callback that outlives the test). Check whether anything ever **unregisters** (e.g. `SetConsoleCtrlHandler(h, False)`); if never, the callback is a process-lifetime landmine.

## Tips
- "Thread count alone is not the trigger" is a real signal: if raising stack size / capping `-n` doesn't help, the fault is a specific poisoned resource, not accumulation.
- Distinguish **confirmed** (code-verified arming) from **inferred** (the trigger event). State both; confirm the inferred part with Step 4.
- A test that installs a real OS-level callback (console handler, signal handler via ctypes) and then nulls the reference is a prime suspect — make such tests use a **fake** kernel32/dependency so nothing real is registered in a worker.
- Save the finding to `.agent_lessons/` before context compression; it has value beyond the final answer.
