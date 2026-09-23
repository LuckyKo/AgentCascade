---
name: xdist-worker-internalerror-capture
description: Capture the WORKER-side pytest INTERNALERROR traceback for an intermittent pytest-xdist worker death (exit 3) when the master only shows "assert not crashitem" — via a repo-root sitecustomize that auto-imports a per-worker capturer (stdout/stderr tee + pytest_internalerror hook + faulthandler).
source: auto-generated
version: "1.0.0"
triggers:
  - "xdist worker INTERNALERROR"
  - "assert not crashitem"
  - "worker exit code 3"
  - "capture worker traceback"
  - "pytest-xdist flake death"
generated_by: researcher
generated_from_task: "Extract the worker-side INTERNALERROR traceback for an intermittent pytest-xdist flake in AgentCascade"
---

## Goal
Get the **worker's own** Python INTERNALERROR traceback (the exception that made an xdist worker call `sys.exit(3)`) for an intermittent worker death, when the master only reports its detection line `INTERNALERROR: assert not crashitem <victim test>` and xdist loses the dead worker's relayed output. Purely diagnostic.

## Key non-obvious facts (hard-won)
- **The victim test is a bystander** — it passes in isolation. Do NOT debug its code; the error happens elsewhere in that worker's run.
- **`-p <plugin>` does NOT propagate into xdist workers** (only the master loads it). So a plugin registered via `-p` will NOT run its hooks in the worker. Do not rely on it.
- **Workers do NOT inherit `PYTHONPATH`**, but their **cwd IS the repo root** → `site` auto-imports a repo-root `sitecustomize.py` in *every* worker. This is the reliable load vector.
- Exit code **3 = pytest INTERNALERROR** (unhandled exception in a *hook/fixture/plugin*, NOT test code). Confirm with a memory dump if unsure it's native vs Python (a clean `exit(3)` stack = Python-level; see [[native-crash-dump-pytest-xdist]]).

## Procedure
### Step 1 — Build the per-worker capturer (non-invasive, no repo edits)
Two TEMP files at the **repo root** (remove both after capture; they are a no-op in the master and only act when `PYTEST_XDIST_WORKER` is set):

`<repo>/sitecustomize.py`:
```python
try:
    import worker_err_capturer  # noqa: F401
except Exception:
    pass
```

`<repo>/worker_err_capturer.py`:
```python
import os, sys, faulthandler
if os.environ.get("PYTEST_XDIST_WORKER"):
    OUT = r"<abs path to a scratch dir>"   # e.g. tmp_ac_iso/worker_err
    w = os.environ["PYTEST_XDIST_WORKER"]; pid = os.getpid()
    import os as _os; _os.makedirs(OUT, exist_ok=True)
    log = open(f"{OUT}/worker_{w}_{pid}.log", "a", buffering=1)
    class _Tee:
        def __init__(s, o): s.o=o; s.f=log
        def write(s, d): s.o.write(d); s.f.write(d); s.f.flush()
        def flush(s): s.o.flush(); s.f.flush()
    sys.stdout, sys.stderr = _Tee(sys.stdout), _Tee(sys.stderr)
    faulthandler.enable(open(f"{OUT}/faulthandler_{w}_{pid}.log","a",buffering=1))
    def pytest_internalerror(excrepr, excinfo):
        with open(f"{OUT}/internalerror_{w}_{pid}.log","a") as f:
            f.write(f"== INTERNALERROR ({w} pid {pid}) ==\n")
            f.write(f"excinfo type: {excinfo.typename}\n")
            f.write(str(excinfo))
            if excrepr is not None:
                f.write("\n== excrepr ==\n"); f.write(str(excrepr))
        return None  # let the default handler print too
    import pytest
    pytest.hookimpl(tryfirst=True)(pytest_internalerror)  # or register via a tiny plugin class
```
> Registration note: a plain function is not auto-collected as a hook. The robust way is a tiny
> plugin class registered with `pytest_configure`, or — simplest — rely on the **stdout/stderr tee**,
> which already captures the `INTERNALERROR>` lines `TerminalReporter` prints. The explicit hook just
> adds the structured `excinfo`. Either the tee or the hook is enough; keep both for redundancy.

### Step 2 — Run and reproduce in a background loop (match the base rate)
Run from the repo root:
```
python -m pytest -q -n 4 -p no:cacheprovider -p worker_err_capturer
```
(`-n auto` = more workers = higher concurrent churn = higher repro rate on a many-core box.)
Flakes are intermittent — loop N full runs (N ≈ 3–4× the historical base rate) in a **background**
shell (each run can exceed the sync timeout), and on any run whose output contains
`assert not crashitem` or `INTERNALERROR`, stop and read the worker logs.

### Step 3 — Read the per-worker error
- Find the dying worker = the one whose `worker_*.log` / faulthandler log shows the death.
- `internalerror_<worker>_<pid>.log` = the structured exception + traceback (the deliverable).
- `worker_<worker>_<pid>.log` = the raw terminal output (the `INTERNALERROR>` lines).

### Step 4 — If the flake is rare (won't reproduce in budget)
- Run the pool/thread-heavy file **serially** with the repo's thread-trace env (e.g. `AC_TRACE_HANDLES=1`, `-n 0`) and read the per-test thread-count log. **Bounded/flat count ⇒ the known leaks are fixed; the crash is a *transient racy interaction* (a leaked thread briefly touching torn-down/GC'd state), not a steadily-growing leak.** A monotonically rising count ⇒ a real accumulating leak to fix.
- Do NOT attach any debugger to the orchestrator/runtime interpreter — only to short-lived worker PIDs.

## Tips
- **Never edit repo tests/source** for diagnosis; use repo-root temp files + env vars, and **remove them after** (a lingering `sitecustomize.py` at the repo root is a landmine).
- A caught WARNING like `…: [Errno 9] Bad file descriptor` on a fresh `read_text()` is a *symptom* of a process-level fd anomaly (subprocess fd inheritance / a C ext closing a shared fd) — note it, but it is not itself the INTERNALERROR.
- **Label confidence honestly**: distinguish *Confirmed* (dump/live-observed) from *INFERRED* (hypothesis). An "X-in-Y" rate estimated from zero observed crashes in N runs is an **estimate**, not a measured fact — say so.
- After the diagnosis, delegate the report to an independent reviewer to catch fabricated evidence / inconsistent counts / unlabeled inferences.
