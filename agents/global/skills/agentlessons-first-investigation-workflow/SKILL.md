---
name: agentlessons-first-investigation-workflow
description: For AgentCascade bug/investigation tasks, mine .agent_lessons/ and reports/ for prior verified findings before re-deriving anything from source or logs — most non-trivial bugs already have a root-caused, confidence-verified lesson file.
source: auto-generated
version: "1.0.0"
triggers:
  - "agentcascade bug"
  - "todo.md line"
  - "investigate agent cascade"
  - "check out todo"
  - "async shell"
  - "agent_lessons"
generated_by: orchestrator
generated_from_task: "Investigate AgentCascade todo.md line 158 (__kill killing the AC server)"
---

## Goal
Spend investigation time on what is NOT yet known, not re-deriving root causes that a prior session already pinned down and verified in `.agent_lessons/`.

## Procedure
### Step 1 — Grep the memory dir by symptom + component keywords FIRST
Before opening source or logs, search `N:\work\WD\AgentCascade\.agent_lessons\` (and `reports/`) for the bug's keywords and subsystem. Example: for a shell-kill/server-death report, grep for `kill`, `ctrl_c`, `console`, `async-shell`, `server`. Lesson files use YAML frontmatter with `tags:` and `aliases:` plus Obsidian-style `[[related]]` links — follow those links to pull the full cluster (e.g. `[[async-shell-wait-queue-blind-polling]] → [[async-shell-wait-queue-fix-impl]]`).

### Step 2 — Trust the `confidence: verified` flag, but re-anchor line numbers
A lesson marked `confidence: verified` has already gone through an independent review PASS. Treat its ROOT CAUSE as established and do NOT re-run the same experiments. BUT line numbers in lessons drift as code changes — re-confirm each `file.py:NNN` against the live tree before acting (see plan-anchor-verification).

### Step 3 — Cross-check the "fixed" claim against git history
If a lesson says a fix was implemented/committed, verify with `git log --oneline -S'<symbol>'` or `--grep=` on the affected files. Confirms the fix is actually in-tree and gives you the commit hash to cite.

### Step 4 — Surface the real finding vs the reported symptom
Reported symptoms are often mislabeled (e.g. user said `__kill` kills the server; actual culprit was `__ctrl_c` broadcasting via `GenerateConsoleCtrlEvent(0,0)`). The lesson file usually already contains this correction. Lead with the corrected root cause and name the specific code site.

## Tips
- Lesson files are the single best source of truth in this repo for "what already happened here" — they encode gotchas (e.g. test-harness import traps, flaky-test notes, exact substrings tests assert) that are NOT recoverable from reading code alone.
- The `related:` links form a graph; a bug's full context is usually 3-5 linked files, not one.
- Do NOT burn turns re-running reproduction experiments for a bug whose lesson already says "verified" — instead spend the budget on the fix or on confirming the fix landed.
- If a lesson exists but no fix is committed yet (status: "pending implementation"), that is your starting point for the BUILD phase, not a reason to re-investigate from scratch.
