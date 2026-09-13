---
name: code-review
description: Lightweight independent code review for non-reviewer agents — read the real code, verify behavior, report severity-rated findings, and end with a machine-readable PASS / NEEDS WORK / FAIL verdict.
source: auto-generated
version: "1.0.0"
triggers:
  - "code review"
  - "review code"
  - "review the changes"
  - "review this diff"
  - "independent review"
  - "critique the implementation"
  - "review verdict PASS NEEDS WORK FAIL"
generated_by: orchestrator
generated_from_task: "Add a general-use, lightweight code-review skill for non-reviewer agents (a complement to the dedicated reviewer agent)."
---

## Goal

Give any non-specialist agent (coder, ponytail, generalist, researcher) a fast, consistent way to independently review a change and end in a machine-readable verdict. This is a complement, not a replacement — for a deep or high-stakes review, delegate to the dedicated `reviewer` agent instead; use this for a quick independent check on a change you didn't write yourself (don't self-approve your own work).

## Procedure

### Step 1 — Read the real code
- Pull the actual diff/commit (see `version-control`) and read **every** file the change touches, including callers and shared state — never review blind from a description.
- Use `read_file`, `list_dir`, and `grep` to verify claims against source; don't trust what the change is *said* to do.

### Step 2 — Verify behavior
- Run suspect code with `code_interpreter` where possible: hit the edge case, reproduce the reported bug.
- If regression tests exist, run them and confirm they actually exercise the changed path (a test that only re-asserts current behavior adds no value).

### Step 3 — Hunt for real issues
Check the following dimensions: **logic/correctness**, **edge cases** (empty/nil, zero/negative, concurrency, missing error handling), **security** (injection, secrets, authz), **performance/bloat**, and **root cause vs. symptom** (reject patches that hide a bug or over-engineer around it).

### Step 4 — Rate severity
- 🔴 **Critical** — breaks correctness / security hole; must fix before merge.
- 🟠 **Major** — significant bug or design flaw; should fix now.
- 🟡 **Minor** — small defect, readability, missed edge case.
- 🔵 **Nit** — style/preference only.

### Step 5 — Report and verdict
Output a numbered list of findings (each with severity + specific file/line + a concrete fix), then a "Required changes" section listing every 🔴/🟠, then exactly one final line:

```
VERDICT: PASS          # no 🔴 or 🟠; you genuinely verified it
VERDICT: NEEDS WORK    # ≥1 🟠 (or a risky cluster of 🟡), no 🔴
VERDICT: FAIL          # ≥1 🔴, or fundamentally wrong / unverifiable
```

## Tips

- Cite the exact file/line for every finding; prefer evidence (a failing run, a grep hit) over assertion.
- Every issue you raise must carry a concrete fix — no criticism without a path forward.
- Stay scoped to the change under review; use `code-refinement-audit` for whole-repo bloat passes and pair with domain skills (`systematic-debugging`, `safe-optimization-pass`) when the change's nature calls for them.
- If you can't verify something (missing deps, no runnable env), say so explicitly and downgrade confidence — but still issue a verdict. Never say "looks good" without having inspected it.
