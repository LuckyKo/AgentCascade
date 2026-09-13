---
name: bug-tracker-entry-format
description: Consistent format + lifecycle for .bug_tracker/BUG_XXXX.md entries (one file per bug, /fixed/ subfolder on resolution)
source: auto-generated
version: "1.0.0"
triggers:
  - "log a bug"
  - "file a bug"
  - "bug tracker entry"
  - "BUG_XXXX"
  - "record this bug"
  - ".bug_tracker"
  - "create a bug report file"
  - "how do I log a bug in the tracker"
generated_by: coder
generated_from_task: "Make a skill for nice bug-tracking formatting for .bug_tracker/README.txt (terse README, one BUG_XXXX_short_description.md per issue, /fixed subfolder)"
---

## Goal

Give every bug report in `.bug_tracker/` a uniform, skimmable structure so any agent can file, find, and close bugs without re-deriving the convention each time.

## Directory layout (canonical)

```
.bug_tracker/
  README.txt            # this convention (keep it short; point to the skill for details)
  BUG_0001_title1.md           # open bug — numbered, zero-padded 4 digits
  BUG_0002_title2.md
  fixed/
    BUG_0003_title3.md         # resolved bug — MOVED here, never deleted
```

- **One file per bug.** Name `BUG_NNNN_title.md` where `NNNN` is the next free number (scan existing files in both root and `fixed/` to find it). Never reuse a number.
- **Open bugs live at the root; fixed bugs are MOVED into `fixed/`.** Moving preserves history and keeps the root a live "what's broken" list.

## File template

```markdown
# BUG_NNNN — <one-line title>

**Status:** OPEN | FIXED
**Filed:** YYYY-MM-DD
**Found in:** <task / test / log that surfaced it, e.g. `tests/test_streaming_fullstack_e2e.py` N=50 run>
**Severity:** critical | high | medium | low
**Component:** <file(s)/module(s), e.g. `agent_cascade/compression/agent_invoker.py`, `engine/core.py:435-437`>

## Symptom
What was observed (the user-visible or log-visible behavior). Quote the exact error/warning line verbatim where possible.

## Evidence
Concrete, reproducible proof — not speculation:
- Log lines / stack traces (verbatim, with file:line)
- Measured numbers (e.g. "11 of 50 turns segmented", "max_input_tokens=65536 for Compressor")
- Repro command + minimal input

## Root cause
The actual mechanism (why it happens), with code refs. If not yet diagnosed, say so explicitly and mark the entry `Status: OPEN (root cause unknown)`.

## Fix
What changed to resolve it (file + line + diff summary). For open bugs: the proposed fix direction.

## Verification
How the fix was confirmed (test that now passes, before/after numbers). Required before moving to `fixed/`.

## Related
[[other-bug-or-memory]] links — connect to `.agent_lessons/` memories and sibling BUG files.
```

## Procedure

1. **Find the next number**: list `.bug_tracker/*.md` AND `.bug_tracker/fixed/*.md`, take max `NNNN` + 1.
2. **Create** `.bug_tracker/BUG_NNNN_title.md` from the template. Fill Symptom + Evidence first (always verifiable); Root cause only if actually diagnosed — never invent it.
3. **Severity heuristic**: `critical` = data loss / crash / blocks all runs; `high` = wrong results or a broken feature path; `medium` = degraded behavior with workaround; `low` = cosmetic / log noise.
4. **On resolution**: fill Fix + Verification, set `Status: FIXED`, then **move** the file to `.bug_tracker/fixed/`. Do not edit it in place and leave it at the root.
5. **Cross-link**: add a `[[memory-name]]` link to any related `.agent_lessons/` memory (and vice-versa) so the bug and its root-cause knowledge are discoverable from both sides.

## Tips

- **Evidence over narrative.** A bug entry without verbatim log lines or numbers is an opinion, not a report. The "Found in" field must let someone reproduce it.
- **Don't guess root cause.** If you only have the symptom, say `root cause unknown` — a wrong root cause is worse than none and poisons future readers.
- **Keep README.txt terse** (3–5 lines); the template lives here in the skill, not in the README. The README should just say "one BUG_NNNN_title.md per issue, move to /fixed/ when done" and point to this skill.
- **One atomic bug per file.** If a run surfaces 3 distinct bugs (e.g. a loop-detector false positive + a Compressor state bug + a config-propagation gap), that's 3 files, not one giant entry.
- **Fixed ≠ deleted.** The `fixed/` folder is the regression history — future agents check it to avoid re-filing known-and-fixed issues.
