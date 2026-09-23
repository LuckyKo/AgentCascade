---
name: reported-regression-self-consistency-check
description: Reject or validate an incoming "regression" report BEFORE touching code by (1) confirming tree state vs the named commit, (2) running the exact command and comparing pass/fail counts against the report's arithmetic, and (3) checking whether the report's numbers are even self-consistent. Catches phantom regressions from dirty/polluted runs.
source: auto-generated
version: "1.0.0"
triggers:
  - "regression introduced by commit"
  - "N failures after change"
  - "tests broke at commit X"
  - "reproduce the failure first"
  - "phantom regression"
  - "verify before fixing"
generated_by: coder
generated_from_task: "Fix a Phase 2 skill-invalidation regression (report claimed 23 failures / 197 passed at e54c8709 vs 209/209 at dd5fe950) — turned out to be a non-reproducing phantom."
---

## Goal
Before "fixing" a reported regression, establish whether the report is even self-consistent and reproducible on a clean tree — so you don't waste effort (or ship a wrong fix) chasing a phantom caused by a dirty/polluted run. This complements `systematic-debugging` (reproduce-before-fix) with the *report-validation* layer: sometimes the "bug" is in the report, not the code.

## Procedure

### Step 1 — Confirm the working tree state against the NAMED commit
A report names a commit (e.g. "broke at e54c8709"). Verify what's actually on disk:
```bash
git log --oneline -5                      # is HEAD really that commit?
git diff --numstat <named_commit> -- <relevant files>   # empty = tree matches the commit
# or, for a single file, byte-compare against the committed blob:
git show <named_commit>:path/to/file.py > /tmp/committed.txt
fc /n /tmp/committed.txt path/to/file.py  # "no differences" = unmodified
```
If the tree is byte-identical to the named commit AND tests pass, the regression is not present in that code — stop and re-examine the report. (Common trap: a dirty tree with unrelated modified/deleted files + an untracked full-checkout snapshot dir can pollute a run.)

### Step 2 — Run the EXACT command and count results against the report's arithmetic
Run the reporter's command verbatim (serial `-n0` AND default xdist) and record pass/failed. Then do the arithmetic:
- Count tests at the BASE commit vs the REGRESSION commit (`grep -c "def test_"` on a `git show <commit>:tests/...py` extraction, or just trust pytest's collected count).
- **The tell:** if the report says "X passed / Y failed" at the regression commit but **X < (total tests at the base commit)**, the numbers are impossible. A regression can only *remove* passes; it cannot shrink the passing count below the smaller prior suite's total, nor change how many tests exist. An internally-inconsistent report came from a polluted/dirty run — not a clean checkout of that commit.

### Step 3 — Trace each hypothesized mechanism to confirm it is actually active
For every mechanism the report blames, verify it can even fire in the failing context:
- Does the code path execute in these tests? (e.g. a startup background thread only fires in `AgentPool.__init__` — unit tests that never construct the pool don't trigger it.)
- Do the specific named entities actually match the mechanism's filter? (e.g. "discover() returns 0" via `_disabled_names` requires the *asserted* skill names to be in that set — check the real on-disk store for those exact entries, not just "there are some inactive entries".)
- Do test fixtures already isolate the shared state the mechanism would bleed? (e.g. `manager._metrics_file = tmp_path/...` re-pointing means no cross-test mutation.)

### Step 4 — Check whether the proposed "fix" would break committed tests
Before editing production, grep the test suite for assertions that pin the current behavior. If the report's suggested fix (e.g. "make target corpus-bounded") contradicts a committed test that asserts the *current* behavior on the exact edge case, the "fix" is a spec change, not a bug fix — do not apply it silently. Escalate instead.

### Step 5 — Report non-reproduction with evidence; request a clean re-run
If no repro: report the tree-state proof (empty diff), the actual counts, and the arithmetic inconsistency. Ask the reporter to re-run on a **clean `git checkout <commit>`** (or `git stash` the dirty tree) and share raw output + failing test names. Save a project memory documenting the phantom so it isn't re-investigated.

## Tips
- **A passing count lower than the prior commit's total is the single strongest red flag.** It means the "failure" run had fewer tests than the baseline suite — impossible for a clean regression, diagnostic of a polluted environment.
- Don't anchor on the report's root-cause diagnosis. A confident, detailed mechanism explanation creates false confidence (see `systematic-debugging` anti-pattern "hypothesis anchoring"). Validate the mechanism is *active* in the failing context before trusting it.
- Distinguish "mechanism is real in principle" from "mechanism fires here." A shared store holding 11 inactive entries is real; but if none of the asserted skills are among them, it can't produce "discover() returns 0".
- Keep zero code changes when there's nothing to fix. Making an edit to "address" a phantom regression introduces a real change for no reason and risks breaking committed tests.
- Related: `systematic-debugging` (reproduce-before-fix), `subagent-deliverable-verification` (treat reports as claims to check), `regression-test-revert-proof` (prove a NEW test fails pre-fix).
