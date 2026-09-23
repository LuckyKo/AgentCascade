---
name: verify-native-leak-arming-path
description: Verify or refute whether a hypothesized native-resource (ZMQ thread/socket, ctypes callback, event-loop primitive) leak is actually armed in a test suite before proposing a fix.
source: auto-generated
version: "1.0.0"
triggers:
  - "native leak"
  - "thread leak"
  - "arming path"
  - "zmq"
  - "verify hypothesis"
  - "victim test"
  - "resource leak"
  - "refute root cause"
---

## Goal
Before proposing a fix for a hypothesized native-resource leak, confirm the arming path actually exists in the code — or refute it. Cheap code-tracing that can redirect (or kill) the whole investigation.

## Procedure
### Step 1 — Find the concrete instantiation point (the "arming")
Locate the SINGLE place where the real native resource is actually created (not a mock).
- ZMQ / Jupyter: the `BlockingKernelClient(...).start_channels()` call (the only thing that spawns native I/O threads).
- ctypes console handlers: the `kernel32.SetConsoleCtrlHandler(WINFUNCTYPE(...)(handler), True)` registration.
- Event-loop primitives: the `asyncio.Queue(...)` / `asyncio.Lock(...)` construction.
Everything upstream (constructors, config) is inert; only this point arms the OS-level resource.

### Step 2 — Trace the victim test's construction path to the arming point
Follow the victim test's object construction end-to-end. Watch for dependency guards that short-circuit the arming:
- `if operation_manager is not None:` → skip (tool never built)
- `os.name != 'nt'` → no-op (native call never runs)
- `try: ... except Exception: logger.warning(...)` → silently skipped
If a guard leaves the arming unreachable in the victim test, the victim does NOT arm the resource — it is a pure VICTIM.

### Step 3 — Distinguish real vs mocked usage across ALL tests
Grep for the concrete instantiation call AND for mock markers (`MagicMock`, `mock`, `patch(`, `patch.object`). A leak hypothesis is only as strong as the strongest REAL (unmocked) arming. If every test mocks the arming point, the "leak" cannot occur in the suite — refute the hypothesis.

### Step 4 — Check teardown for the unregister/stop
Even if armed, the leak requires the resource to OUTLIVE teardown. Confirm the explicit cleanup (`close()` / `stop()` / `SetConsoleCtrlHandler(handler, False)`) is actually reachable from the test's teardown. Also note module-level resources (import-time daemon threads) that NO per-test teardown can reach — these are real (if minor) leaks and deserve their own finding.

### Step 5 — State confirmed vs inferred; pivot if refuted
- Confirmed: the code-traced arming path (or its absence).
- Inferred: which specific event triggers the fault at runtime.
If the arming path is ABSENT, REFUTE the hypothesis and pivot to other candidate native sources (ctypes callbacks, other C extensions in the dependency tree). Do NOT force the original narrative to fit the symptoms.

## Tips
- "The victim test builds real objects" ≠ "the victim test arms the native resource." Real pools often guard the dangerous construction behind a dependency tests leave `None`.
- An import-time module-level daemon thread is a real leak even when no test calls into it — record it separately; don't conflate it with the native-resource hypothesis.
- A procdump log line like `Exception: 406D1388` (thread-naming) is often a benign observation, not the fatal fault. Verify the fatal code with WinDbg/cdb before committing to a narrative.
- Cross-check `.agent_lessons/` for a COMPETING hypothesis with a different (code-verified) mechanism before concluding; exception codes must match (0xC0000005 AV ≠ 0x406D1388 thread-naming).
- After refuting a hypothesis, save a memory naming the refutation with file:line evidence so the next agent doesn't re-litigate it.
