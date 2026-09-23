---
name: regression-commit-attribution-asof
description: Pinpoint which commit actually changed the load-bearing line in a "user suspects commit X broke it" bug hunt — using git as-of dumps and timeline-vs-break-timestamp mapping — when the named suspect may be wrong or a later commit in the same feature set made the real change.
source: auto-generated
version: "1.0.0"
triggers:
  - "which commit broke"
  - "regression hunt"
  - "suspects commit"
  - "git as-of"
  - "commit attribution"
generated_by: researcher
generated_from_task: "DIG root cause of a memory-hint regression; user suspected f3afa46d, but the load-bearing line was actually flipped to its breaking value by a later commit e7205dcd in the same feature set."
---

## Goal
Correctly attribute a regression to the commit that ACTUALLY changed the load-bearing code — rather than trusting the user's/orchestrator's named suspect — because feature sets span multiple commits and the triage summary itself can contain factual errors.

## Procedure
### Step 1 — Treat the named suspect AND the orientation as claims, not facts
The person who triaged ("commit X broke it at time T") usually worked from a diff glance. Both the suspect commit and any prior summary are CLAIMS to verify. (In the originating task, the orchestrator's summary said `_rebuild_index` "now passes include_active_only=True" — true of the suspect commit, false of the current tree.)

### Step 2 — Dump the load-bearing line AS OF each candidate commit
For every commit in the feature set (not just the suspect), extract the actual source at that revision and compare the VALUE of the load-bearing line:
```
git show <commit>:<path/to/file> | findstr /N "the_symbol"     # Windows shell
git show <commit>:<path/to/file> | grep -n "the_symbol"        # Unix shell
```
A later commit in the same feature set frequently made the real change (here: suspect f3afa46d set the flag to `True`; a later e7205dcd flipped it to `False`).

### Step 3 — Map the git timeline against the observed break timestamp
```
git log --since="YYYY-MM-DD 00:00" --until="YYYY-MM-DD+1 00:00" --pretty=format:"%h | %ci | %s"
```
Establish which commits were actually on disk when the break was observed. If the real behavior-changing commit is AFTER the observed break, it CANNOT be the cause of that break — re-scope to what was in effect at that time.

### Step 4 — Reproduce with REAL objects before concluding
Build a repro that drives the exact production path with real classes (not mocks) and a bounded timeout to detect hangs. If you CANNOT reproduce the reported symptom, say so explicitly and name the most likely production-state-specific trigger — do NOT force a hypothesis to fit the log.

### Step 5 — State attribution + confidence in the report
Report which commit changed the load-bearing line (with as-of evidence), whether that commit predates or postdates the observed break, which hypotheses are confirmed/refuted with file:line, and what you could not reproduce. Distinguish "confirmed by code+repro" from "inference" from "unknown."

## Tips
- `git show <commit>:<file>` (as-of dump) is the decisive tool — a plain `git show <commit>` diff only shows that commit's own delta, so it hides what a LATER commit changed on the same line.
- On Windows shells `grep` is absent — use `findstr /N "literal"` and pipe from `git show`.
- The orientation/report being wrong is COMMON and load-bearing: when a summary contradicts the code, the code wins — record the contradiction explicitly so it isn't re-inherited downstream.
- Correlation in time ("broke 2 min after commit X") ≠ causation. Always verify via Step 3 that the change was present at the break time before blaming it.
- Pair with [[plan-anchor-verification]] (verify anchors/factual claims against the live tree) and [[systematic-debugging]] (repro-before-conclude, single-hypothesis isolation).
