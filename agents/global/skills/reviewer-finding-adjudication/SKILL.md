---
name: reviewer-finding-adjudication
description: Orchestrator procedure for triaging an independent reviewer's findings BEFORE acting — classify each against the user's authoritative spec and task scope, reject false positives with evidence (including findings whose premise is factually wrong but point at a real different defect), route only accepted items to the coder, and re-review a fresh instance with the adjudication stated.
source: auto-generated
version: 1.0.2
triggers:
  - "reviewer FAIL"
  - "review findings"
  - "adjudicate review"
  - "false positive review"
  - "required changes before approval"
  - "BUILD review"
  - "blocker false premise"
  - "reviewer PASS"
generated_by: orchestrator
generated_from_task: "Security instance reuse v2 plan — reviewer BLOCKER claimed task message 'not built on reuse path'; git proved it IS built (duplicated across two methods). Adjudicated the false premise, fixed the real issue it pointed at (extract shared helper), documented the correction in the plan."
---

## Goal
Prevent an orchestrator from blindly applying an independent reviewer's findings, some of which are false positives, out-of-scope, or rest on a factually wrong premise — especially when a finding contradicts the user's authoritative requirements.

## Why this matters (the failure mode)
Reviewers optimize for "is this code defensible?" not "does this match what the USER asked for" and not always "is my factual claim about the code actually true." A reviewer can confidently return FAIL on a change that is exactly right per spec, OR assert something about the code that is simply wrong (misread a branch, conflate two paths, cite a line that doesn't exist). Acting on either silently breaks the requirement or wastes effort. The orchestrator is the only layer that holds BOTH the user's intent and the reviewer's verdict — so the adjudication MUST happen at this layer, not be delegated to the coder or rubber-stamped into the re-review.

## Procedure
### Step 1 — Read the ACTUAL code, not just the report
Before deciding anything, open the exact file:line the finding cites. Confirm the finding describes what's really there. (A reviewer can misread a branch, conflate two code paths, or cite a line that no longer exists.) For claims about *historical* or *reverted* code, verify against git (`git show <commit>:<file>`), not memory.

### Step 2 — Classify EVERY finding into one of FIVE buckets
- **ACCEPT** — real defect in-scope; fix it.
- **REJECT — contradicts spec** — the "fix" would violate an explicit user requirement. This is the dangerous one. Cite the exact user words that override the reviewer.
- **REJECT — out of scope** — pre-existing behavior or a different task; note it, don't fix it here (flag to the user if worth a follow-up).
- **REJECT — nit / over-engineering** — technically valid but violates "prefer minimal safe changes" for this change's size.
- **REJECT-the-claim-BUT-fix-the-real-issue** — the finding's *stated premise is factually wrong* (verify against source/git), yet it is circling a genuine in-scope defect that happens to be different from what was written. Do NOT fix the literal (false) claim; DO fix the real issue it points at, and document both the correction and the real fix so the next reviewer doesn't re-raise the false version.

### Step 3 — For each REJECT (any bucket), state the conflict explicitly
Name the exact evidence (user words for spec-conflicts; file:line or git ref for factual corrections) and explain why the reviewer's claim is wrong or out of scope. This is what you hand to the re-reviewer so they don't re-raise it.

### Step 4 — Send back ONLY accepted items, with the adjudication attached
The coder's instruction must list: (a) exactly what to change, and (b) an explicit "DO NOT touch" list of the rejected findings with one-line reasons. Without the DO-NOT list, a coder may "helpfully" apply a rejected finding anyway. For a false-premise finding, say explicitly: "the reviewer claimed X which is not true (evidence); do NOT do X; instead do Y."

### Step 5 — Re-review with a FRESH reviewer instance + adjudication stated up front
Do not re-ask the same reviewer who FAILed them (confirmation bias). Instruct the new reviewer on the HARD constraints AND explicitly pre-announce which prior findings were overruled and why, so they don't re-litigate settled points. Require an explicit PASS/FAIL.

## Tips
- The user's spec is authoritative over reviewer taste. When they conflict, the user wins — but VERIFY against the code that you're not the one misreading.
- A "MAJOR"/"BLOCKER" severity label does not make a finding correct; classify by substance, not by the reviewer's severity tag. A BLOCKER with a false premise is still just a wrong claim until verified.
- **A false-premise finding is often a useful pointer anyway.** The reviewer was looking at the right area and saw something off — even if their explanation of *what* was off is wrong, chase it. In the originating session the "task message not built on reuse path" BLOCKER was false, but chasing it revealed the real issue: the construction was *duplicated* across two methods. Fixing only the literal claim would have shipped a divergence bug.
- **A PASS still needs per-finding adjudication — and your executed tests are ground truth.** A confident PASS can carry a factually wrong finding (in one run, a reviewer's PASS said seed-on-miss yields `effective_name != reuse_name → remove`, which was backwards — the acquire returns the fixed name on a seed hit). When you're the coder receiving a PASS, do not accept it wholesale: re-derive each correctness claim from source, and cross-check against your OWN passing tests. A test that already ran green outranks the reviewer's prose; if a finding contradicts a green test, the reviewer misread — do NOT "fix" correct code to match them. Report which findings held up and which were wrong, with evidence, so the record shows why nothing was acted on the false one.
- Keep accepted changes minimal and re-run the test suite after; a rejected finding doesn't need a test, an accepted one often does.
- If MANY findings are rejected, that's a signal the reviewer was given incomplete context — consider whether your original task brief to the coder/reviewer underspecified the requirements.
- Never commit on a reviewer PASS you didn't adjudicate yourself; the whole point is that "PASS" from an un-adjudicated re-review can still be wrong if you accepted a bad finding earlier.
