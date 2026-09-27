---
name: plan-anchor-verification
description: Verify every file:line anchor AND factual claim in an implementation plan against the live tree before editing, including resolving bare filenames to their real paths.
source: auto-generated
version: 1.1.0
triggers:
  - "plan file"
  - "line anchors may have shifted"
  - "verify anchors before editing"
  - "implementation plan"
  - "follow the plan closely"
generated_by: coder
generated_from_task: "Implement Option B root-cause fix per a 131-line plan; task warned line numbers may have shifted"
---

## Goal
Produce an implementation where every file:line anchor AND factual claim in the plan is verified against the current working tree, so you can edit confidently without re-deriving context — and catch when a cited path doesn't exist at all.

## Procedure
### Step 1 — Check baseline drift first
```bash
git log --oneline <report-baseline>..HEAD
git diff --stat <report-baseline> HEAD -- <code dirs> tests/
```
If the delta is cosmetic (version bumps, docs), anchors are probably valid but MUST still be re-verified. If code files in scope changed, treat the plan as stale and re-trace those paths from scratch.

### Step 2 — Verify every anchor by reading, not grepping
For each cited location, `read_file` a window around it (±10 lines) and confirm: the symbol is at that line, the surrounding logic matches the plan's description, and any claimed control flow actually exists. Grep finds names; only reading confirms semantics. Batch independent reads in one parallel tool block to save turns.

### Step 3 — Resolve bare filenames to real paths (the path-not-line drift)
Plans often cite a file by bare name with a line number but NO directory (e.g. "mirror the idiom in `system_info.py:445-463`"). The actual file may live under a nested package dir that the plan omitted — e.g. it was really `agent_cascade/tools/custom/system_info.py`, NOT repo-root `tools/custom/system_info.py`. Reading at the assumed path returns "File not found" and you'd stall guessing. Fix: grep for a UNIQUE SYMBOL from the cited code (a distinctive method name, an unusual identifier, or the exact line text) across the whole tree — that locates the real file in one shot regardless of directory. Then read it. This is distinct from line drift: the lines were right, only the path was wrong/incomplete.

### Step 4 — Cross-check factual claims, not just locations
Plans drift in two ways: line numbers shift AND descriptions go stale. For each behavioral claim (off-by-one arithmetic, lock scope, fallback logic), re-derive it from the code you just read and note discrepancies explicitly. When a plan's claim contradicts the code, the code wins; document the contradiction so future readers don't re-inherit the error.

### Step 5 — Verify existence / on-disk claims by LISTING the tree, not trusting the doc
Design/brainstorm docs frequently assert facts about on-disk or runtime state that are simply WRONG: a directory they claim "does not exist" actually does; file artifacts they reference were never written. Reading/grepping the CODE will NOT catch these — you must inspect the real tree with `list_dir`. Record each contradiction in a "Deviations/notes" section with the corrected reality.

### Step 6 — State the verified baseline + deviations in the plan header / report
Record the actual HEAD/tree state, what changed since the baseline, that all anchors were re-verified, AND a table of factual corrections found (including any bare-filename → real-path resolutions). This is what lets the implementer/reviewer trust the work.

## Tips
- Off-by-one claims are the most common source of reviewer FAILs on plans — trace the loop by hand (write out the iteration sequence with concrete numbers) before specifying any budget/counter reset.
- When a "File not found" hits on a cited path, do NOT guess sibling directories one by one; grep for the unique symbol from the cited code instead. One grep beats five list_dir probes.
- Keep a "verified anchors" table mapping each cited location to what was actually found — it doubles as the implementer's spot-check list and the reviewer's checklist.
- A plan's wrong/incomplete file path is MORE dangerous than line drift: it silently points you at the wrong reference implementation (you may copy an idiom from a non-existent or different file). Always resolve bare filenames to real paths before mirroring any code they cite.
