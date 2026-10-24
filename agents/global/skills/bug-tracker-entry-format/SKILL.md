---
name: bug-tracker-entry-format
description: Consistent format + lifecycle for .bug_tracker/BUG_XXXX.md entries (one file per bug, /fixed/ subfolder on resolution).
source: auto-generated
version: 1.0.1
triggers:
  - "log a bug"
  - "file a bug"
  - "bug tracker entry"
  - "BUG_XXXX"
  - "record this bug"
  - ".bug_tracker"
generated_by: coder
generated_from_task: "Closing a fixed-but-left-open BUG_0042 entry and reconciling a duplicate same-number file during an AFK bug-tracker pass"
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

### Closing an entry that is "fixed in code but left OPEN at root"
A common AFK/triage finding: the fix is already committed, but the tracker entry was never moved to `fixed/`. Do NOT just move the file — verify first, then reconcile:
1. **Confirm the fix is actually in-tree.** Grep the cited code location for the removed/changed symbol (e.g. the stray flag, the old message string). A stale "OPEN" label is a claim, not evidence.
2. **Run the regression test** that guards the fix and confirm it passes *now* (not "passed at filing time"). Only then fill Fix + Verification with the concrete commit/test refs and set `Status: FIXED`.
3. **Check for a duplicate same-number entry before moving.** `list_dir .bug_tracker/fixed/ include=BUG_NNNN*` — if two files already claim the same number, read both and keep the **canonical** one (the more complete entry: has the commit hash, empirical evidence, root cause, resolution). Delete the redundant duplicate; never leave two files under one number.
4. **Move/delete with the right tool.** `.bug_tracker/` entries are usually **untracked** (not in git), so `git mv` fails with "fatal: not under version control". Use a plain OS move (`move src dst` on Windows, `mv` elsewhere). Deletions go to backup automatically.
5. **Re-check side-findings.** A canonical entry often notes a related defect "tracked separately" — verify whether that sibling issue is still open or was itself already fixed, so you don't leave a dangling pointer.

## Tips

- **Evidence over narrative.** An entry without verbatim log lines or numbers is an opinion, not a report; "Found in" must let someone reproduce it.
- **Don't guess root cause.** A wrong root cause is worse than none and poisons future readers.
- **Keep README.txt terse** (3–5 lines); the template lives here, not in the README.
- **One atomic bug per file.** 3 distinct bugs from one run = 3 files, not one giant entry.
- **Fixed ≠ deleted.** `fixed/` is the regression history — future agents check it to avoid re-filing known-and-fixed issues.
- **"OPEN" at root with a matching in-tree fix = hygiene gap, not a live bug.** Verify-then-close per the procedure above; don't trust the label and don't move blindly.
- **Leftover verification reports** (`BUG_NNNN_verification.md`, `..._e2e_verification.md`) are dated *reports*, not open bugs. They clutter the root "what's broken" list — but archiving them is an organizational preference, so flag it to the user rather than moving them unilaterally.
