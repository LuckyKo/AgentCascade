---
version: 1.0.3
name: pytest-ini-addopts-xdist-serial-run
description: Run a focused pytest file serially (no xdist) in a repo whose pytest.ini pins -n auto via addopts, without triggering unrecognized-arguments or accidentally re-including excluded suites; also covers the missing-plugin CI root cause, capping -n in containers, and why a marker filter cannot suppress a collection-time ImportError.
triggers:
  - pytest
  - pytest.ini
  - addopts
  - xdist
  - pytest-xdist
  - -n auto
  - unrecognized arguments
  - collection error
  - CI test selection
  - conftest.py
---

# Running focused / CI pytest in a repo with pinned xdist addopts

## Goal

Reliably run a focused pytest file — serially (no xdist) or on CI — in a repo where `pytest.ini`
pins parallelism through an `addopts` line containing `-n auto`, without burning turns on
`unrecognized arguments` errors, without accidentally dragging excluded extended-suite tests into
the run, and without discovering halfway through that a collection-time import error was the real
problem all along.

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

Confirm a final line like `N passed in Xs` with an item count. A quick sanity check:
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

## Tips

- **Distinguish three failure signatures — different fixes.** `unrecognized arguments: -n` =
  missing xdist/timeout plugin (add to manifest); `can't start new thread` or an OpenBLAS
  `pthread_create` abort = thread/worker limit (cap `-n`, set thread env vars); `ModuleNotFoundError:
  <app dep>` = missing runtime dep (install requirements). Don't treat them as one problem.
- **A/B toggle one variable at a time when measuring.** To learn what a plugin actually affects,
  `pip uninstall` it, re-measure, reinstall, re-measure — inside the same environment. A
  before/after delta taken across an environment change (e.g. installing requirements between the
  two runs) is confounded and will mislead you.
- Prefer this over editing pytest.ini; a one-off serial run should not mutate shared config.
- "Works on my dev machine" is NOT evidence of CI correctness. Compare the dev machine's installed
  plugins against the CI manifest — the gap is almost always there.
- On Windows hosts there is no grep or tail; pipe through
  `python -c "import sys; [print(l) for l in sys.stdin if 'PASSED' in l]"` to filter verbose output.
