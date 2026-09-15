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
---

## Goal

Give any non-specialist agent a fast, consistent way to independently review a change and end in a machine-readable verdict. Complement, not replacement — for a deep/high-stakes review delegate to the dedicated `reviewer` agent. Use this for a quick independent check on a change you didn't write (don't self-approve your own work).

## Procedure

1. **Read the real code.** Pull the actual diff/commit (see `version-control`) and read every file the change touches, including callers and shared state — never review blind from a description. Use `read_file`/`list_dir`/`grep` to verify claims against source; don't trust what it's *said* to do.
2. **Verify behavior.** Run suspect code with `code_interpreter` where possible (hit the edge case, reproduce the reported bug). If regression tests exist, run them and confirm they actually exercise the changed path (a test that only re-asserts current behavior adds no value).
3. **Hunt real issues.** Dimensions: logic/correctness; edge cases (empty/nil, zero/negative, concurrency, missing error handling); security (injection, secrets, authz); performance/bloat; root-cause vs symptom (reject patches that hide a bug or over-engineer around it).
4. **Rate severity.** 🔴 Critical — breaks correctness/security hole, must fix before merge. 🟠 Major — significant bug or design flaw, should fix now. 🟡 Minor — small defect, readability, missed edge case. 🔵 Nit — style/preference only.
5. **Report + verdict.** Numbered findings (each with severity + specific file/line + a concrete fix), then a "Required changes" section listing every 🔴/🟠, then exactly one final line:
   ```
   VERDICT: PASS          # no 🔴 or 🟠; you genuinely verified it
   VERDICT: NEEDS WORK    # ≥1 🟠 (or a risky cluster of 🟡), no 🔴
   VERDICT: FAIL          # ≥1 🔴, or fundamentally wrong / unverifiable
   ```

## Tips

- Cite exact file/line for every finding; prefer evidence (a failing run, a grep hit) over assertion.
- Every issue must carry a concrete fix — no criticism without a path forward.
- Stay scoped to the change; use `code-refinement-audit` for whole-repo bloat passes and pair with domain skills (`systematic-debugging`, `safe-optimization-pass`) when the change's nature calls for them.
- If you can't verify something (missing deps, no runnable env), say so explicitly and downgrade confidence — but still issue a verdict. Never say "looks good" without having inspected it.
