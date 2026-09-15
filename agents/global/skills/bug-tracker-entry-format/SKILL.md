---
name: bug-tracker-entry-format
description: Consistent format + lifecycle for .bug_tracker/BUG_XXXX.md entries (one file per bug, /fixed/ subfolder on resolution).
source: auto-generated
version: "1.0.0"
triggers:
  - "log a bug"
  - "file a bug"
  - "bug tracker entry"
  - "BUG_XXXX"
  - "record this bug"
  - ".bug_tracker"
---

## Goal

Give every bug report in `.bug_tracker/` a uniform, skimmable structure so any agent can file, find, and close bugs without re-deriving the convention.

## Directory layout

```
.bug_tracker/
  README.txt            # terse; points to this skill
  BUG_0001_title1.md    # open bug — zero-padded 4 digits
  fixed/
    BUG_0003_title3.md  # resolved — MOVED here, never deleted
```

- **One file per bug.** Name `BUG_NNNN_title.md`; `NNNN` is the next free number (scan root AND `fixed/`). Never reuse a number.
- **Open bugs at the root; fixed bugs MOVED into `fixed/`.** Moving preserves history and keeps the root a live "what's broken" list.

## File template

```markdown
# BUG_NNNN — <one-line title>

**Status:** OPEN | FIXED
**Filed:** YYYY-MM-DD
**Found in:** <task/test/log that surfaced it, e.g. `tests/test_x.py` N=50 run>
**Severity:** critical | high | medium | low
**Component:** <file(s)/module(s), e.g. `<path/to/module.py>:<line-range>`>

## Symptom
Observed behavior (user- or log-visible). Quote the exact error/warning line verbatim where possible.

## Evidence
Concrete, reproducible proof — not speculation: verbatim log lines/stack traces (with file:line), measured numbers, repro command + minimal input.

## Root cause
The actual mechanism (why it happens), with code refs. If not yet diagnosed, say so and mark `Status: OPEN (root cause unknown)`.

## Fix
What changed to resolve it (file + line + diff summary). For open bugs: the proposed fix direction.

## Verification
How the fix was confirmed (test that now passes, before/after numbers). Required before moving to `fixed/`.

## Related
[[other-bug-or-memory]] links — connect to `.agent_lessons/` memories and sibling BUG files.
```

## Procedure

1. **Find the next number**: list `.bug_tracker/*.md` AND `.bug_tracker/fixed/*.md`, take max `NNNN` + 1.
2. **Create** from the template. Fill Symptom + Evidence first (always verifiable); Root cause only if actually diagnosed — never invent it.
3. **Severity heuristic**: `critical` = data loss/crash/blocks all runs; `high` = wrong results or a broken feature path; `medium` = degraded behavior with workaround; `low` = cosmetic/log noise.
4. **On resolution**: fill Fix + Verification, set `Status: FIXED`, then **move** to `.bug_tracker/fixed/`. Don't edit in place and leave it at the root.
5. **Cross-link**: add a `[[memory-name]]` link to any related `.agent_lessons/` memory (and vice-versa) so bug + root-cause knowledge are discoverable from both sides.

## Tips

- **Evidence over narrative.** An entry without verbatim log lines or numbers is an opinion, not a report; "Found in" must let someone reproduce it.
- **Don't guess root cause.** A wrong root cause is worse than none and poisons future readers.
- **Keep README.txt terse** (3–5 lines); the template lives here, not in the README.
- **One atomic bug per file.** 3 distinct bugs from one run = 3 files, not one giant entry.
- **Fixed ≠ deleted.** `fixed/` is the regression history — future agents check it to avoid re-filing known-and-fixed issues.
