---
name: reviewer-finding-adjudication
description: Orchestrator procedure for triaging an independent reviewer's findings BEFORE acting — classify each against the user's authoritative spec and task scope, reject false positives with evidence, route only accepted items to the coder, and re-review a fresh instance with the adjudication stated.
source: auto-generated
version: "1.0.0"
triggers:
  - "reviewer FAIL"
  - "review findings"
  - "adjudicate review"
  - "false positive review"
  - "required changes before approval"
  - "BUILD review"
generated_by: orchestrator
generated_from_task: "Session Metadata System line (todo.md line 159) — BUILD reviewer FAILed a correct change on a finding that contradicted the user's explicit spec."
---

## Goal
Prevent an orchestrator from blindly applying an independent reviewer's findings, some of which are false positives or out-of-scope — especially when a finding contradicts the user's authoritative requirements.

## Why this matters (the failure mode)
Reviewers optimize for "is this code defensible?" not "does this match what the USER asked for." A reviewer can confidently return FAIL on a change that is exactly right per spec, because it optimizes a different objective (e.g., maximal constancy, maximal DRY). Acting on such a finding silently breaks the requirement. The orchestrator is the only layer that holds BOTH the user's intent and the reviewer's verdict — so the adjudication MUST happen at this layer, not be delegated to the coder or rubber-stamped into the re-review.

## Procedure
### Step 1 — Read the ACTUAL code, not just the report
Before deciding anything, open the exact file:line the finding cites. Confirm the finding describes what's really there. (A reviewer can misread a branch, conflate two code paths, or cite a line that no longer exists.)

### Step 2 — Classify EVERY finding into one of four buckets
- **ACCEPT** — real defect in-scope; fix it.
- **REJECT — contradicts spec** — the "fix" would violate an explicit user requirement. This is the dangerous one. Cite the exact user words that override the reviewer.
- **REJECT — out of scope** — pre-existing behavior or a different task; note it, don't fix it here (flag to the user if worth a follow-up).
- **REJECT — nit / over-engineering** — technically valid but violates "prefer minimal safe changes" for this change's size.

### Step 3 — For each REJECT that contradicts spec, state the conflict explicitly
Name the user instruction verbatim and explain why the reviewer's objective (e.g., "constant forever") is a stricter reading than what was asked (e.g., "stable per run, refresh on reload"). This is what you will hand to the re-reviewer so they don't re-raise it.

### Step 4 — Send back ONLY accepted items, with the adjudication attached
The coder's instruction must list: (a) exactly what to change, and (b) an explicit "DO NOT touch" list of the rejected findings with one-line reasons. Without the DO-NOT list, a coder may "helpfully" apply a rejected finding anyway.

### Step 5 — Re-review with a FRESH reviewer instance + adjudication stated up front
Do not re-ask the same reviewer who FAILed them (confirmation bias). Instruct the new reviewer on the HARD constraints AND explicitly pre-announce which prior findings were overruled and why, so they don't re-litigate settled points. Require an explicit PASS/FAIL.

## Tips
- The user's spec is authoritative over reviewer taste. When they conflict, the user wins — but VERIFY against the code that you're not the one misreading.
- A "MAJOR" severity label does not make a finding correct; classify by substance, not by the reviewer's severity tag.
- Keep accepted changes minimal and re-run the test suite after; a rejected finding doesn't need a test, an accepted one often does.
- If MANY findings are rejected, that's a signal the reviewer was given incomplete context — consider whether your original task brief to the coder/reviewer underspecified the requirements.
- Never commit on a reviewer PASS you didn't adjudicate yourself; the whole point is that "PASS" from an un-adjudicated re-review can still be wrong if you accepted a bad finding earlier.
