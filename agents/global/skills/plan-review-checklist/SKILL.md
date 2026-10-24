---
name: plan-review-checklist
description: Systematic methodology for independently reviewing implementation plans before code is written. Covers anchor verification, stated-count re-derivation, cross-section contract contradictions, pseudocode execution, and test-citation checking.
source: auto-generated
version: "1.0.0"
triggers:
  - "plan review"
  - "implementation plan"
  - "code review plan"
  - "verify before implement"
  - "review the plan, no implementation"
generated_by: coder
generated_from_task: "Review a DIG-phase plan against the live tree at a pinned HEAD; verdict on whether a coder could execute it without guessing"
---

## Goal

Provide a repeatable, adversarial review process that catches broken anchors, false "verified" claims,
self-contradictions between sections, and pseudocode that would not behave as the plan claims —
**before any code is written**.

## Procedure

### Step 0 — Pin the tree first
Confirm the HEAD the plan claims (`git rev-parse HEAD`) and that the cited paths are clean
(`git status --porcelain -- <cited paths>` → empty). Every line number is only valid against that SHA.
Read the whole plan before touching the tree; never review blind.

### Step 1 — Batch-verify anchors, don't grep one at a time
A plan can carry 40+ `file:line` citations. Do them in **one scripted pass** (a Python snippet that,
per file, prints the numbered lines or greps a list of patterns) instead of dozens of tool calls:
- for each cited range, print the lines and check the claimed symbol/behaviour is actually there;
- resolve bare filenames to real paths first (`os.walk`) — repos hold near-duplicate files
  (`path_security.py` vs `operation_manager/path_security.py`, `config.py` in two packages);
- a ±1–2 line offset in a prose citation is a nit; a cited line showing a *different symbol* is a defect.

### Step 2 — Re-derive every number the plan asserts
Counts are the most trusted and least checked thing in a plan. Recompute them from the tree:
- "N callers of X" → grep the call sites repo-wide, then subtract the one being modified ("N other" is
  usually off by one).
- "expected baseline: K passed" → count `def test_` **plus parametrize expansion** per file
  (`defs - 1 + len(params)`), then sum across the files the command lists.
- "exactly one hit" audits → re-run the grep with the plan's own pattern.
Flag any count that does not reproduce, and check that a test recipe's *source* matches its count
(e.g. "copy the fixed strings the 11 callers pass, from `bot.py:198-403`" yields only 9 if two callers
live elsewhere).

### Step 3 — Hunt cross-section contradictions
Diff the *behavioural* statements between sections, not just the anchors. The classic shape: §A says the
parser removes matched spans, §B says the sender removes only successfully-sent spans — both cannot hold.
Also check a declared-pure function isn't assigned I/O-dependent knowledge (a no-I/O parser cannot know
'missing'/'oversized'), and that span offsets stay well-defined after any removal.

### Step 4 — Execute the plan's own pseudocode against its own edge-case table
Copy the plan's regex/normalizer/branch into a sandbox and run every case the plan lists (plus the
traps that class always hides: scheme prefixes, drive letters, query/fragment, malformed syntax, empty
input, over-long fields). Empirical execution beats mental tracing for regexes.

### Step 5 — Audit test citations by symbol, not by line
If the plan says "tests exercise `_send_one` directly at :787, :1855", grep the named symbol at those
lines. A cited test that calls the *wrapper* is a wrong citation, and the real direct-call tests often
carry the invariants the refactor must preserve (attempt bounds, no-retry-on-X) — add those to the
plan's "tests that constrain the implementation" list if missing.

### Step 6 — Treat absolute negatives as working-tree checks
"No `X` anywhere in the repo", "no other call sites", "nothing else sends text to the phone": check
the **filesystem**, not only git-tracked files (untracked configs/artifacts exist and may be
functionally referenced by code), and grep the sink symbol (e.g. `bot.send_message(`) repo-wide to
prove the claimed single funnel really is one.

### Step 7 — Verify external-library claims against the installed copy
Version, type aliases, docstring limits ("10 MB", "0-1024"): introspect the **installed** package
(`python -c "import pkg; print(pkg.__version__)"`, `inspect.signature`, docstring greps). Note where
the symbol actually lives (`telegram.Bot`, not `telegram.ext.Bot`). Prefer the docstring's own
constant name over quoting the website.

### Step 8 — Check the change list against the requirements
Every MUST behaviour needs a named place in the file-by-file change list: a config gate that exists only
in the risk section but not in the change list is unimplementable; a "draft regex in §2" that §2 never
drafts forces the coder to invent the core matcher. List missing edge cases (length limits, double-decoding,
feature-flag location) as concrete additions.

## Report format
Numbered findings, each with `file:line` evidence and severity (🔴 Critical, 🟠 Major, 🟡 Minor, 🔵 Nit),
a concrete fix per finding, a short "what checks out" section (proves you actually looked), then
**required changes** followed by a verdict: PASS / NEEDS WORK / FAIL. Answer the question actually asked
(e.g. "could a coder execute this without guessing?") — do not silently substitute "is it correct?".

## Tips
- Be adversarial; never write "looks good" about anything you did not open.
- The highest-yield defects are rarely wrong line numbers: they are contradicted contracts, unmet MUST
  requirements with no drafted code, and numbers that don't reproduce.
- A plan's own honesty section ("not verified / moderate confidence") is a feature — do not re-flag
  what it already labels as unverified; flag only what it claims is confirmed but isn't.
- Do not edit the plan when only a review was asked for.

## Common Pitfalls
- Verifying anchors but never the counts, contracts, or pseudocode the anchors support.
- Trusting a memory/skill's line numbers instead of re-deriving from the symbol.
- Grepping git-tracked files only, then asserting a file "does not exist".
- Citing a test as exercising symbol X because it is near the code for X, without grepping X at that line.
- Reviewing from the plan's summary instead of reading the full document first.
