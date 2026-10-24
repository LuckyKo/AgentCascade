---
name: plan-anchor-verification
description: Verifies all file/line references in design plans against actual source code before treating them as ground truth.
source: auto-generated
version: 1.2.1
triggers:
  - "plan review"
  - "anchor verification"
  - "line number validation"
generated_by: ui_correctness
generated_from_task: "Strata bridge-card WebUI feature plan review"
---

## Goal

Ensure that every file and line number cited in a design plan actually corresponds to the claimed code location, preventing stale anchors from invalidating the entire implementation.

## Procedure

### Step 1 — Extract all file:line references from the plan

Scan the plan for patterns like `file.js:123`, `server.py:456`, `index.html:789`. Collect them into a list with their claimed purpose (e.g., "whitelist in refreshAll at app.js:573-596").

### Step 2 — Read the actual file and inspect the cited range

For each reference, read the file from the workspace and display the exact lines. Verify:
- Does the function/variable/structure at that line match the plan's description?
- Is the line count consistent with the plan's claim (e.g., is app.js actually 751 lines)?
- Are there multiple locations that could be confused?

### Step 3 — Cross-check structural assumptions

Plans often describe "the true gate" or "the only model list fetch." Verify these claims by:
- Grep for the referenced function names across all file types.
- Search for competing implementations (e.g., is there another `list_models` endpoint?).
- Confirm that no other code paths reach the same outcome.

### Step 4 — Report refuted anchors in a dedicated section

If any cited location is wrong, missing, or misdescribed, create a table:

| Plan's anchor | Reality | How checked |
|---------------|---------|-------------|
| `app.js:573-596` (whitelist) | Line 573 contains `maxTokens: 32768,` | Read file at start_line=570, limit=30 |

Do not bury these findings—they invalidate any downstream edits based on the anchor.

### Step 5 — Recompute the plan if anchors shift

If line numbers have moved since a prior UI map or brief, do not assume the old plan is still valid. Either:
- Update the line numbers in the plan, OR
- Reject the plan as stale and require a fresh ground-truth pass.

## Tips

- **Never trust line numbers from memory.** Even if a plan was "verified" last month, code shifts. Always re-read the actual file.
- **Use `grep` to confirm uniqueness.** Before accepting "the only place," grep the repo for the function name and confirm no other candidates exist.
- **Check file size consistency.** A claim of "751 lines" should match `wc -l` or a quick line count script. Discrepancies often indicate you're looking at the wrong version.
- **Be explicit about confidence.** Mark each anchor as "Confirmed by code / High / Moderate / Low."

## Gotchas

- A plan may cite an old branch of the repository. Ensure you're reading the current HEAD or the exact commit under review.
- File renames or directory moves can make a correct line number point to the wrong file. Always verify the file path, not just the line.
- The "ground truth" in a brief is often stale. Treat every `file:line` as a hypothesis until verified against the actual source.
