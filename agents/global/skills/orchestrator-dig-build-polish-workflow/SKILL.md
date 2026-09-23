---
name: orchestrator-dig-build-polish-workflow
description: The 3-phase DIG/BUILD/POLISH delegation workflow for non-trivial codebase tasks — researcher plan with anchor verification, coder implementation from a pinned plan, independent reviewer verdict, and mandatory todo.md completion-note format.
source: auto-generated
version: "1.0.0"
triggers:
  - "todo item"
  - "implement fix"
  - "dig build polish"
  - "delegate to researcher"
  - "delegation workflow"
generated_by: orchestrator
generated_from_task: "Fix propose_skill wasted-approval bug (todo.md line 153) via full DIG/BUILD/POLISH cycle"
---

## Goal
Execute a non-trivial codebase task end-to-end as an orchestrator without doing specialist work yourself, with every phase independently verified.

## Procedure

### Step 1 — Orient cheaply before delegating
Read the todo item + the 2-3 core files yourself (grep for the class/function, read_file the key sections). You do NOT need a full research pass to write a good delegation brief — but you DO need enough anchors (file paths, line numbers, current behavior) that the researcher verifies rather than re-discovers. Pass absolute paths in every delegation.

### Step 2 — DIG: delegate the plan
`call_agent(agent_class="researcher", ...)` with: the task text verbatim, your gathered context (what you already verified), explicit research questions to answer, and a DELIVERABLE spec: plan file path + required sections (summary, root cause, exact changes at file+function level, test plan, risk table, verification checklist). Add "Do NOT modify any code — plan only." The researcher should verify every anchor against the live tree; explicitly ask it to check your assumptions — wrong assumptions in a brief propagate into the plan.

### Step 3 — Read the full plan yourself before BUILD
Do not hand a plan to the coder unread. Check: does the exact-changes section name real insertion points? Are the test assertions specific (e.g. `assert_not_called()` on the approval mock, not just "returns REJECTED")? Is there a drift/regression guard test for the load-bearing edge case?

### Step 4 — BUILD: delegate implementation
`call_agent(agent_class="coder", ...)` pointing at the plan file as authoritative ("READ IT FIRST"), plus a condensed restatement of the exact changes and the MUST-NOT-TOUCH list. Require: host-shell test run (never code_interpreter for AgentCascade pytest), full-file green report, and explicit "any deviations from the plan" statement.

### Step 5 — Verify independently
Re-run the key test file yourself via `shell_cmd` on the host (agent test reports are claims, not evidence). Note: shell_cmd auto-rejects `| head`/`| tail` pipes on Windows — run bare pytest; output truncation is handled by spillover.

### Step 6 — POLISH: independent reviewer
`call_agent(agent_class="reviewer", ...)` framed as a FIRST look ("do not trust prior claims"). Give the reviewer the plan path, the changed files with line hints, and named review axes (plan conformance, edge cases, test quality — "read them, don't count", untouched-component audit). Require verdict format: PASS/FAIL + numbered findings with severity BLOCKER/MAJOR/MINOR. Fix cycle until explicit PASS.

### Step 7 — Close the loop in todo.md
Mark the item `[x]` and replace it with a detailed completion note matching the file's house style: root cause, fix summary with key design decisions (what was deliberately left unchanged and why), test counts, plan file path, "Independent review PASS." This note is the project's durable record — future agents read it instead of re-investigating.

## Tips
- The researcher catching a wrong assumption in your brief is a feature, not a failure — make sure the correction lands in the plan AND in the coder's brief (e.g. "the pre-check MUST replicate derivation X, not hardcode Y").
- Keep diffs minimal and name the UNTOUCHED list explicitly; reviewers audit it.
- Save a project memory for load-bearing non-obvious findings (derivation quirks, race windows accepted by design) so future tasks don't re-litigate them.
- One reviewer nit about an intentional defensive choice (e.g. broad except with fall-through to an authoritative check) is not a fix cycle — note it and move on.
