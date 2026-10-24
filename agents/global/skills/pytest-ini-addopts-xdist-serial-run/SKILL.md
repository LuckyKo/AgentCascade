---
name: pytest-ini-addopts-xdist-serial-run
description: Run pytest serially in a repo whose pytest.ini hard-codes xdist in addopts. The no-xdist and -n 0 attempts fail with unrecognized arguments; override with -o addopts empty string instead.
source: auto-generated
version: "1.0.0"
triggers:
  - "unrecognized arguments -n"
  - "pytest.ini addopts"
  - "run pytest serially"
  - "disable xdist pytest"
  - "cmd.exe line continuation pytest"
  - "pytest collects whole suite"
generated_by: coder
---

## Goal
Reliably run a focused pytest file (or specific node IDs) serially (no xdist) in a repo where pytest.ini pins parallelism via an addopts line containing -n auto, without burning turns on the unrecognized-arguments error or accidentally collecting the entire suite.

## Why it bites
Many repos set in pytest.ini an addopts line like: `-n auto --timeout=60 --durations=10 -m "not live_api and not skip_if_no_local"`. The -n flag comes from xdist. Two common serial-run attempts both fail: (1) adding `-p no:xdist` disables the plugin, so the -n token left in addopts is no longer recognized and pytest errors with unrecognized arguments; (2) appending `-n 0` on the CLI gives the same error because you are now passing -n to a disabled parser. Root cause: the -n flag lives in addopts, not your command line. Disabling xdist does not remove the token; it just makes pytest reject it.

## Procedure
### Step 1 — Confirm xdist is pinned in addopts
Read pytest.ini (or pyproject.toml tool.pytest.ini_options). If addopts contains -n, you must override addopts, not disable the plugin.

### Step 2 — Override addopts, KEEP the marker filter
Use `-o addopts=""` to clear the pinned options, then re-add only what you want — critically the default marker exclusion so live/e2e/stress tests stay skipped. Example: `python -m pytest tests/test_x.py -q --no-header -p no:cacheprovider -o addopts="" -m "not live_api and not skip_if_no_local"`.
If you target files that carry NONE of the excluded markers (pure unit files), dropping the `-m` re-add is harmless. But if you are unsure, re-add it: omitting it silently drags in the extended-suite tests the marker filter normally excludes (fullstack_e2e, stress, etc.), turning a targeted run into a long one.

### Step 3 — Keep node-ID lists on ONE line (Windows gotcha)
On a Windows host (cmd.exe shell) a multi-argument pytest command wrapped with `^` line continuations is silently mis-parsed: the continuation breaks the argument list and pytest falls back to collecting the ENTIRE suite (you will see it collecting thousands of items). When passing several explicit node IDs, put them ALL on a single command line — never split with `^`. Same applies to PowerShell backticks. Quote each node ID.

### Step 4 — Verify it is actually serial, targeted, and real
Confirm a final line like `N passed in Xs` with an item count (not a bare "passed"), and that the collected count equals the node IDs / files you named. A serial run of a small file finishes in one to five seconds. If instead you see many progress dots then silence then a timeout, that is the in-container subprocess hang — switch to host shell_cmd rather than adding more flags.

## Tips
- `-o addopts=""` wipes ALL pinned opts (timeout, durations, markers), so re-add any you need; do not assume --timeout=60 or the `-m` exclusion survived. Re-add `--timeout=300` explicitly for runs that touch slow I/O.
- Prefer this over editing pytest.ini; a one-off serial run should not mutate shared config.
- If you only need to reduce parallelism rather than kill it, use `-o addopts="-n 2 --timeout=60"` and restate every opt you keep.
- On Windows hosts there is no grep or tail; pipe through `python -c "import sys; [print(l) for l in sys.stdin if 'PASSED' in l]"` to filter verbose output, or redirect to a file and read it.
- A full-suite collection when you meant a targeted run is almost always one of two causes: dropped the `-m` re-add, or cmd.exe `^` broke the node-ID list. Check both before assuming the suite is genuinely slow.
