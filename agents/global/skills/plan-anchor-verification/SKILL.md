---
name: plan-anchor-verification
description: Verify every file:line anchor AND factual/on-disk claim in a design or report against the live working tree before writing an implementation plan, so a coder can implement without re-reading the source.
source: auto-generated
version: 1.0.1
triggers:
  - "implementation plan"
  - "anchor"
  - "file:line"
  - "re-trace"
  - "baseline drift"
  - "verify plan"
---

## Goal
Produce an implementation plan whose every file:line anchor AND factual claim is verified against the current working tree, so a coder can implement from it without re-reading the source report.

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

### Step 4 — Verify existence / on-disk claims by LISTING the tree, not trusting the doc
Design/brainstorm docs frequently assert facts about on-disk or runtime state that are simply WRONG (not merely stale): a directory they claim "does not exist" actually does; file artifacts they reference were never written. Reading/grepping the CODE will NOT catch these — you must inspect the real tree:
- `list_dir` the actual locations (the data store, skills dir, config dirs) and diff what is there against what the doc claims.
- For "no X location exists" / "X is aspirational only" claims, list the parent directory and look for X. A pre-existing manual convention the author forgot is a common source of this.
- For "seed from artifact Y mtime" / "read Y" claims, confirm Y actually exists on disk: grep for what writes it; if nothing does, the claim is false and the design must fall back to an available substitute.
Record each contradiction in a "Deviations/notes" section with the corrected reality — these are load-bearing (a missed pre-existing convention can change migration logic, eligibility sets, or re-enable semantics).

### Step 5 — State the verified baseline + deviations in the plan header
Record the actual HEAD/tree state, what changed since the report baseline, that all anchors were re-verified, AND a table of factual corrections found. This is what lets the implementer trust the plan.

## Tips
- Off-by-one claims are the most common source of reviewer FAILs on plans — trace the loop by hand (write out the iteration sequence with concrete numbers) before specifying any budget/counter reset.
- When a report's claim contradicts the code, the code wins; document the contradiction so future readers don't re-inherit the error.
- Keep a "verified anchors" table in the plan mapping each cited location to what was actually found — it doubles as the implementer's spot-check list.
- A design doc's factual errors about on-disk state are MORE dangerous than line drift: they silently corrupt the design (wrong migration, wrong eligibility sets). Always `list_dir` the real tree for any claim about files/dirs that exist or artifacts that get written.
