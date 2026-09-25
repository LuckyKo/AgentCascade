---
name: cross-platform-windows-path-bug-testing
description: Diagnose and test Windows-specific path-handling bugs (backslashes, drive letters, UNC, virtual prefixes) from a Linux sandbox/CI where the bug does not reproduce natively.
source: auto-generated
version: "1.0.0"
triggers:
  - "windows path bug on linux ci"
  - "backslash path handling test"
  - "PureWindowsPath simulation"
  - "cross-platform path normalization"
  - "drive letter path resolve"
  - "pathlib platform dependent"
generated_by: orchestrator
generated_from_task: "Fix multi delete_file backslash bug (todo.md:165) — Windows-only defect, had to prove + test it from a Linux sandbox."
---

## Goal
Diagnose and write **revert-proof tests** for a path-handling bug that only manifests on the target OS (usually Windows), when your dev sandbox / CI runs a different one (usually Linux). The trap: you cannot reproduce the bug natively, so naive "run it and watch it fail" verification is impossible — you must simulate the other platform's path semantics AND design the fix so one code path runs on both.

## Procedure

### Step 1 — Do NOT trust a native repro on the wrong OS
On Linux, `\` is an ordinary filename character: `PosixPath('src\\a.py')` → single component `src\a.py`. So backslash inputs "fail" for the *wrong* reason and absolute Windows paths like `N:\x\y` are treated as relative. A native Linux run tells you almost nothing about the real Windows defect. **Stop trying to reproduce it natively** and switch to simulation.

### Step 2 — Simulate the target OS's parsing rules with Pure path types
`pathlib.PureWindowsPath` / `PurePosixPath` replicate each platform's *parsing* (no filesystem access), so you can reason about Windows behavior on any host:
```python
from pathlib import PureWindowsPath as WP, PurePosixPath as PP
import ntpath
for c in [r"N:\work\WD\a.py", "src\\a.py", "/workspace/src/a.py", r"\\server\share\x.py"]:
    print(c, "nt_isabs=", ntpath.isabs(c), "WP_abs=", WP(c).is_absolute(), "WP_parts=", list(WP(c).parts))
```
Key non-obvious facts to verify per case (they are NOT symmetric):
- Windows treats a leading-`/` path with **no drive** as **not absolute** (`WP('/workspace/x').is_absolute() == False`, `root='\\'`) — so any virtual-prefix handling that relies on `Path(...).is_absolute()` silently misfires on Windows and depends entirely on explicit string guards.
- Windows natively parses `\` in relative/absolute paths, so many backslash inputs *already work* there; the real defect is usually narrower (e.g. a backslashed virtual prefix not stripped by forward-slash-only `startswith` guards).
- POSIX treats `\` as literal → every backslash input breaks.

Build an **outcome table** (input × {pre-fix, post-fix} × {Windows, Linux}) before writing any fix. This is your evidence and your test spec.

### Step 3 — Design the fix to normalize to a platform-independent form BEFORE any `Path` call
The highest-leverage move: convert the input to a canonical separator (e.g. `\`→`/`) at the top of the resolver, *before* prefix guards / absolute checks. Then **one code path runs on both OSes**, so a single test proves both — no monkeypatching `os.name` or `pathlib.Path` needed. Verify it is a **no-op** for already-working inputs (forward-slash / relative / absolute) and that any security/containment check still runs on the fully-resolved path.

### Step 4 — Pick a cross-platform revert-proof anchor
Choose an input that **fails pre-fix on BOTH OSes** and passes post-fix on both. A backslashed virtual prefix (e.g. `workspace\src\a.py`) is ideal: on Windows it isn't stripped, on Linux it's a literal filename → NOT FOUND on both. Also add a pure unit test of the normalization helper itself — it fails pre-fix on both via import error and is trivially OS-independent. Add a security guard test (UNC `\\server\share`, `..\` escape) asserting containment still blocks — this should pass pre AND post fix.

### Step 5 — Prove revert-proofness empirically, per OS
- On your sandbox OS: temporarily revert the fix → confirm the anchor tests FAIL → restore → confirm PASS.
- For the *other* OS you can't run: cite the Step-2 simulation table + the "single code path" argument (the fix normalizes before any `Path` call, so both OSes execute identical logic). Document this explicitly in the commit/plan rather than claiming a live run.

## Tips
- **Sandbox path auto-translation:** if your tooling rewrites Windows-style paths (`N:\...`) to container paths, disable it (e.g. `fix_paths=false`) before simulating, or you'll test the wrong string.
- **Absolute-path detection gotcha:** on Windows `Path('/x/y')` is NOT absolute (no drive). Any code that branches on `.is_absolute()` behaves differently per OS — flag every such branch in your outcome table.
- **Don't over-fix:** Windows often already handles plain backslash relative/absolute paths natively. Fix only what the outcome table proves broken; a "fix" for working inputs is scope creep and regression risk.
- **Security invariant:** normalization must run *before* resolution but the containment check (e.g. `commonpath`) must still apply to the *resolved* path — otherwise you may weaken an escape guard. Watch for edge cases that become STRICTER post-fix (safer) and document them so the change is explicit, not accidental.
- **xdist-safe + hermetic:** per-test temp dirs, never real `N:\` paths; the simulation step needs no filesystem at all.
