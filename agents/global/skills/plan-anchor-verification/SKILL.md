---
name: plan-anchor-verification
description: Procedure for writing implementation plans from a research report — re-verify every file:line anchor against current HEAD before trusting or citing it, and handle baseline drift.
source: auto-generated
version: "1.0.0"
triggers:
  - "implementation plan"
  - "line numbers"
  - "file:line anchors"
  - "research report"
  - "plan verification"
generated_by: coder
generated_from_task: "Writing corrected implementation plan for auto-skill in-loop trigger; research report line numbers were off and one factual claim was wrong"
---

## Goal

Produce an implementation plan whose every file:line anchor is verified against the current working tree, so a coder can implement from it without re-reading the source report.

## Procedure

### Step 1 — Check baseline drift first
```bash
git log --oneline <report-baseline>..HEAD
git diff --stat <report-baseline> HEAD -- <code dirs> tests/
```
If the delta is cosmetic (version bumps, docs), anchors are probably valid but MUST still be re-verified. If code files in scope changed, treat the report as stale and re-trace those paths from scratch.

### Step 2 — Verify every anchor by reading, not grepping
For each cited location, `read_file` a window around it (±10 lines) and confirm: the symbol is at that line, the surrounding logic matches the report's description, and any claimed control flow (e.g. "this loop subtracts X") actually exists. Grep finds names; only reading confirms semantics.

### Step 3 — Cross-check factual claims, not just locations
Reports drift in two ways: line numbers shift AND descriptions go stale. For each behavioral claim (off-by-one arithmetic, lock scope, fallback logic), re-derive it from the code you just read and note discrepancies explicitly in the plan (e.g. "report said L570 subtracts _current_turn; it doesn't — turns_available = max_turns fresh per run").

### Step 4 — State the verified baseline in the plan header
Record the actual HEAD commit, what changed since the report baseline, and a note that all anchors were re-verified. This is what lets the implementer trust the plan.

## Tips
- Off-by-one claims are the most common source of reviewer FAILs on plans — trace the loop by hand (write out the iteration sequence with concrete numbers) before specifying any budget/counter reset.
- When a report's claim contradicts the code, the code wins; document the contradiction so future readers don't re-inherit the error.
- Keep a "verified anchors" table in the plan mapping each cited location to what was actually found — it doubles as the implementer's spot-check list.
