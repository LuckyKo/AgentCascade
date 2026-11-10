---
version: 1.2.0
name: pytest-ini-addopts-xdist-serial-run
description: "Run a focused pytest file serially (no xdist) in a repo whose pytest.ini pins -n auto via addopts, without triggering unrecognized-arguments or accidentally re-including excluded suites; covers the missing-plugin CI root cause, capping -n in containers, why a marker filter cannot suppress a collection-time ImportError, the two distinct causes of an xdist worker death (RAM pressure vs. a worker-killing test), and when to use serial (failure triage) vs. parallel (full/basic runs)."
triggers:
  - "pytest"
  - "pytest.ini"
  - "addopts"
  - "xdist"
  - "pytest-xdist"
  - "-n auto"
  - "unrecognized arguments"
  - "collection error"
  - "CI test selection"
  - "conftest.py"
  - "no tests ran"
  - "node down not properly terminated"
---

# Running focused / CI pytest in a repo with pinned xdist addopts

## Goal

Reliably run a focused pytest file — serially (no xdist) or on CI — in a repo where `pytest.ini`
pins parallelism through an `addopts` line containing `-n auto`, without burning turns on
`unrecognized arguments` errors, without accidentally dragging excluded extended-suite tests into
the run, and without discovering halfway through that a collection-time import error was the real
problem all along.

## Serial vs. parallel — pick the right mode (read this first)

- **Basic / full regression run → parallel.** Use the repo default (`-n auto`), i.e. just
  `python -m pytest -q`. This is the fast path. A full *serial* suite is 20-40× slower and a
  massive waste of time for a normal pass/fail sweep — **never run the whole suite serially.**
- **Serial (no xdist) → ONLY to re-check a specific failure.** xdist workers can hide or crash a
  failing test's traceback (the `crash-item` / `node down` signatures below). To see the real
  traceback, re-run JUST that one test/file serially (Step 2). Serial is a *triage* tool, not a
  full-run tool.
- **If `-n auto` OOMs on a host with a co-resident model → cap `-n`, don't go serial.**
  `-n auto` spawns one worker per core and each re-imports the heavy `conftest.py`; with a
  resident local model that OOMs. The in-session fix is a moderate cap (`-n 4` / `-n 8`) — still
  far faster than serial — or free the co-resident RAM (`execution_mode="safe"`) if you can.
  Drop to serial only for a single failing test.

## Why it bites

Many repos set in `pytest.ini` an addopts line like:
`-n auto --timeout=60 --durations=10 -m "not live_api and not skip_if_no_local and not stress"`.
The `-n` flag comes from xdist. Two common serial-run attempts both fail: (1) adding
`-p no:xdist` disables the plugin, so the `-n` token left in addopts is no longer recognized and
pytest errors with `unrecognized arguments`; (2) appending `-n 0` on the CLI gives the same error
because you are now passing `-n` to a disabled parser. **Root cause: the `-n` flag lives in
addopts, not your command line.** Disabling xdist does not remove the token; it just makes pytest
reject it.

**Second trap — re-adding the marker filter wrongly.** If you clear addopts with `-o addopts=""` to
escape the pinned options, you also wipe the `-m "not ..."` exclusion filter, and tests marked for
excluded suites get collected and run. A "1 failed" on a known extended-suite test after an addopts
override is a **mis-invocation artifact, not a regression**. Verify with `--collect-only` before
triaging it as real.

**Third trap — a marker cannot suppress a collection-time ImportError.** `-m` filters apply at
*deselection*, which happens *after* collection. A module that fails to import aborts the whole run
no matter what marker it carries. Verified: with `-m "not extra_examples"` still active, a module
importing `gradio` aborted collection with `Interrupted: 1 error during collection`. The fix is
`collect_ignore_glob` in that directory's `conftest.py`, not a marker. When triaging "why did my
whole run abort", run `--collect-only -q` first and read the `ERROR collecting <file>` lines.

**Fourth trap — CI/sandbox root cause.** If the environment only installs the app's runtime
`requirements.txt` (which usually lists NO test deps), pytest errors immediately with
`unrecognized arguments: -n --timeout=60`. The dev machine works only because those plugins happen
to be installed globally. This is a **packaging gap**, not a code bug.

**Fifth trap — a worker dies from memory pressure, and it looks like two different bugs.** When the
host is already holding a resident local model, `-n auto` spawns one worker per core and each worker
re-imports the shared `conftest.py`. The largest allocation on that import path dies first (observed:
a tokenizer singleton built at module scope — `agent_cascade/utils/tokenization_qwen.py:241` →
`MemoryError` at its decoder dict), and xdist then reports whichever symptom it happens to catch:

- `node down: Not properly terminated` + `ConftestImportFailure: MemoryError` + `OSError: [WinError 8]
  Not enough memory resources…` + **`no tests ran`**, or
- `INTERNALERROR> … assert not crashitem, (crashitem, node)` with **exit code 3**, after some tests
  already passed, naming an innocent test as the crash item.

**Same cause, two faces — and neither is a code defect.** The discriminating test is one parameter:
re-run the identical command with the co-resident RAM freed (e.g. `shell_cmd` `execution_mode="safe"`,
which unloads local models for the command's duration). If it completes, RAM was the variable; nothing
about the tests changed. If you *cannot* free the co-resident model (it is serving your own live
session), the in-session fix is to **cap `-n` to a moderate count** (`-n 4`/`-n 8`) — not to fall back
to a full serial run (see "Serial vs. parallel" above). Do not open a new bug for the second
signature, and do not blame a previously "fixed" terminal-writer or colorama issue until you have
re-run with memory available.

**Sixth trap — a worker-killing test, which is NOT memory pressure (distinct from the Fifth).** A
specific test can crash its own xdist worker. Observed: a threaded local socket/HTTP-server test on
Windows (`socketserver` / `ThreadingHTTPServer` — a test that spawns a server thread and tears it
down, leaving the worker in a state that aborts it). xdist reports `[gwN] node down: Not properly
terminated` at the progress point where that test runs, and the *bystander* tests that were in flight
in that worker get marked `F` (so a whole file of "failures" can be crash collateral, not real
regressions).

This is a **different cause** from the Fifth trap. Discriminate with two negative tests:
- **Capping `-n` does NOT fix it** — the crash reproduces at the same progress point with `-n 4`
  (a RAM cause often survives a moderate cap).
- **Freeing RAM does NOT fix it** — the host had plenty of free RAM; the crash is structural, not an
  allocation failure.

The fix is to run the crashing test **serially** (single process — there is no worker to crash) or
exclude it from the parallel run; it is a *test-isolation* defect, not a harness/memory defect. Do
NOT file a RAM bug and do NOT keep capping `-n` — neither addresses a worker-killing test. Re-run the
suspect test alone serially to confirm it is the culprit (it passes or hangs in single-process mode).

## Procedure

### Step 1 — Read the addopts line and copy the marker filter verbatim

Read `pytest.ini`. If addopts contains `-n`, you must override addopts, not disable the plugin.
**Copy the exact `-m "..."` string out of that line.** Do not reconstruct it from memory and do not
assume it lists every marker you remember from an older version of the repo — the filter and the
`[markers]` documentation section drift apart and contradict each other. The `addopts` line is
authoritative for what runs; `[markers]` prose is only a description of each marker's meaning.

### Step 2 — Override addopts and re-add the COMPLETE marker filter

```
python -m pytest tests/test_x.py -q --no-header -p no:cacheprovider \
  -o addopts="" \
  -m "<copy the entire not-A and not-B string from pytest.ini, verbatim>"
```

Re-add only what you want, **including the full default marker exclusion**. Serial runs of a small
file finish in one to five seconds. **Check the deselected count**: unexpected failures of a
single-process/extended-suite test (or an xdist guard firing, "requires single-process execution")
almost certainly mean you dropped part of the filter.

### Step 3 — Verify it is actually serial AND that exclusions held

Confirm a final line like `N passed in Xs` with a **non-zero** item count. A quick sanity check:
`python -m pytest <file> --collect-only -q` should show `no tests collected (N deselected)` for a
file entirely excluded by markers.

### Step 4 — On CI or in a sandbox, fix the manifest (root cause) and cap workers

1. **Add the missing plugins to the manifest the environment installs.** `addopts` with `-n` needs
   **pytest-xdist**; `--timeout` needs **pytest-timeout**. Also check for plugins implied by marks
   rather than flags — e.g. `asyncio_mode` settings or `@pytest.mark.asyncio` need
   **pytest-asyncio**, and that one shows up as *failing tests*, not as an `unrecognized arguments`
   error, so it is easy to miss. Diff the plugins installed on the dev machine against the manifest;
   the dev machine's global site-packages is the hidden source of truth.
2. **Install from the manifest first.** A fresh container has NONE of your local pip installs. Run
   `pip install -r requirements.txt` before testing, or you'll chase phantom `ModuleNotFoundError`s
   for app deps that are unrelated to the plugin fix.
3. **Cap `-n` on constrained runners only.** `-n auto` spawns one worker per core; in a constrained
   container this can hit the OS thread limit → `RuntimeError: can't start new thread`, or kill the
   interpreter outright at startup via native libs:
   `OpenBLAS blas_thread_init: pthread_create failed for thread 15 of 16`.
   Setting `OPENBLAS_NUM_THREADS=1` (and OMP/MKL/NUMEXPR equivalents) prevents the startup crash.
   **On a normal CI runner do NOT cap** — `-n 2` on a 4-core runner roughly halves parallelism for
   no benefit. Capping fixes thread *exhaustion*, not thread *leakage*; if a suite leaks background
   threads per worker, capping `-n` will not help and no marker will either.
4. **Free the RAM before theorising.** On a workstation that already hosts a local model, run the
   suite in a mode that unloads it for the command's duration. If the identical command succeeds with
   the memory freed, the diagnosis is done — record it as an operational requirement, not a code bug.

## Tips

- **Distinguish failure signatures — different fixes.** `unrecognized arguments: -n` = missing
  xdist/timeout plugin (add to manifest); `can't start new thread` / OpenBLAS `pthread_create` =
  thread or worker limit (cap `-n`, set thread env vars); `ModuleNotFoundError: <app dep>` = missing
  runtime dep (install requirements); `MemoryError` / `WinError 8` / `no tests ran` = memory
  pressure (free the co-resident allocation, or cap `-n`, and re-run the same command).
- **`node down` has TWO causes — RAM and a worker-killing test.** RAM (Fifth trap): capping `-n` /
  freeing the co-resident model fixes it. Worker-killing test (Sixth trap): capping and freeing RAM
  do NOT fix it; the crash pins to ONE test (re-run it serially to confirm) — fix the test's
  isolation, not the harness, and do not file a RAM bug.
- **A zero-test run is a failure, not a result.** Before using any run as a baseline, read the passed
  count. `no tests ran`, `Interrupted: 1 error during collection`, and `1 passed` when you expected 84
  are all the same class of error: the harness told you nothing about the code.
- **A crash-item is a bystander.** `assert not crashitem, (crashitem, node)` names whatever test was
  in flight when the worker died. Re-run that test alone; if it passes, it is not the cause — and the
  next question is *why the worker died*, not *what the test does*.
- **Check for an existing operational entry before filing a new bug.** The same host-memory failure can
  present as two different signatures weeks apart; a second filing with an invented root cause is worse
  than no filing.
- **A/B toggle one variable at a time when measuring.** To learn what a plugin actually affects,
  `pip uninstall` it, re-measure, reinstall, re-measure — inside the same environment. A before/after
  delta taken across an environment change (e.g. installing requirements, or freeing model RAM, between
  the two runs) is confounded and will mislead you.
- Prefer this over editing pytest.ini; a one-off serial run should not mutate shared config.
- "Works on my dev machine" is NOT evidence of CI correctness. Compare the dev machine's installed
  plugins against the CI manifest — the gap is almost always there.
- On Windows hosts there is no grep or tail; pipe through
  `python -c "import sys; [print(l) for l in sys.stdin if 'PASSED' in l]"` to filter verbose output.

## Anti-patterns

| Pattern | Why it fails |
|---|---|
| Running the FULL suite serially to "avoid OOM" | 20-40× slower for no benefit; cap `-n` (or free RAM) for a full run, reserve serial for re-checking ONE failing test |
| `-p no:xdist` or `-n 0` to get a serial run in a repo pinning `-n auto` | The token lives in addopts; you get `unrecognized arguments` |
| `-o addopts=""` with no marker filter | Wipes the exclusion filter; excluded suites run and a known-excluded failure reads as a regression |
| Trusting a run that printed `no tests ran` as a green baseline | A worker died during setup; nothing about the code was observed |
| Filing a new bug per xdist crash signature | RAM pressure presents as both `no tests ran` and a crashitem INTERNALERROR; the cause and the workaround are the same |
| Capping `-n` or freeing RAM to fix a worker-killing test | Neither addresses a test that crashes its own worker; the crash pins to one test — run it serially / fix its isolation |
| Blaming a previously fixed terminal/colorama issue without re-running with memory available | The discriminating variable (free RAM) was never tested |
| Chasing a marker to hide a broken import | Markers deselect after collection; the import already aborted the run |
| Capping `-n` on a healthy CI runner | Halves parallelism for a problem that is not thread exhaustion |
