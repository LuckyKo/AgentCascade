---
name: verify-reported-bug-still-reproduces
description: Determine whether a reported crash/kill/termination bug actually still reproduces in the current tree before assuming it is live — path enumeration, empirical repro, git-timeline archaeology, misattribution check.
source: auto-generated
version: "1.0.0"
triggers:
  - "does this bug still reproduce"
  - "kills the server"
  - "crashes the process"
  - "terminates instead of erroring"
  - "bug report investigation"
  - "stale todo"
  - "already fixed"
  - "root cause kill"
generated_by: researcher
generated_from_task: "Investigate AgentCascade bug: shell_cmd control command with a non-existent tool_id reportedly terminates the whole AC server instead of erroring out."
---

## Goal
Before spending effort fixing or deep-diving a reported crash/kill/termination bug, establish whether it **still reproduces in the current tree** and whether the **reported trigger is accurate**. Many such reports are stale (fixed since) or misattributed (wrong command/path blamed). This skill gets you to a defensible verdict fast.

## Procedure

### Step 1 — Read the report verbatim, then refuse to anchor on its label
Read the exact reported line/issue. Note the claimed trigger (e.g., "`__kill` on a non-existent ID"). Treat it as a **hypothesis**, not a fact. Explicitly list every sibling candidate that could produce the same symptom (all control commands, all dispatch branches, sync vs async paths). Do NOT start by hunting inside the blamed function — that is confirmation bias.

### Step 2 — Enumerate ALL code paths that can reach the physical effect
The symptom "process terminates" has a small set of physical causes: unhandled exception in the main thread, an OS signal broadcast (e.g., Windows `GenerateConsoleCtrlEvent(…, 0)` console broadcast), `os._exit`/`sys.exit`, or a native crash. Grep for the **physical primitives** (`taskkill`, `killpg`, `os.kill`, `GenerateConsoleCtrlEvent`, `ExitProcess`) across the whole repo and note, for each call site, what value is passed (a concrete PID? zero? a group?). The bug lives at one of these sites — work backward from the effect.

### Step 3 — Empirically reproduce against a REAL instance (ground truth beats reading)
Static reading can miss guard coverage. Instantiate the real component and trigger the exact reported condition; observe whether it terminates or returns gracefully. Example pattern:
```python
from agent_cascade.async_shell_pkg.tracker import AsyncShellTracker
tr = AsyncShellTracker(pool=object())   # dummy pool; None-guard paths need no queue
for name, fn in [("kill", tr.kill_task), ("ctrl_c", tr.send_ctrl_c), ...]:
    print(name, "->", fn("agentA", 999))   # 999 = non-existent id
print("alive:", os.getpid())              # if this prints, nothing terminated
```
If it does NOT reproduce in the current tree, that is a first-class finding — do not keep forcing a live-bug narrative.

### Step 4 — Git-timeline archaeology: did a fix already land?
Use read-only git to establish whether defenses pre-exist and when they landed:
```bash
git status --short                       # are the implicated files clean vs HEAD? (live tree == HEAD?)
git log --oneline -S "<symbol>" -- <file>   # which commits introduced a guard/symbol?
git show <commit>                        # did that commit ADD the guard or only reword it?
git log --oneline --grep="kill|ctrl|broadcast" -- <file>
```
Key discriminator: if a fix commit's diff shows it only **changed the return string** of an `if x is None:` guard (the guard line itself unchanged), the guard **pre-existed** — so that path was never the bug. This single check often converts "still broken" into "already fixed."

### Step 5 — Cross-check the physical mechanism vs the reported label (misattribution)
Compare what physically kills the process (Step 2) against what the report blames. If they don't match (report says command A; only command B can produce the effect), flag it as a **likely misattribution** and state the real mechanism + its fix commit. Distinguish clearly: *verified* (repro + code), *high confidence* (docstring/commit message), *speculative* (e.g., "user observed on an older build" — say so).

### Step 6 — Verdict + residual hardening
Deliver: (a) does it reproduce now? (b) which path/command is actually implicated, with file:line; (c) the historical root cause + the commit that fixed it; (d) any remaining hole and a minimal defense-in-depth fix (put the guard at the exact blast-radius point); (e) confidence per claim. Then delegate anchor re-verification to an independent reviewer.

## Tips
- "The bug is already fixed / misattributed" is a legitimate, valuable conclusion — do not manufacture a live bug to justify the task.
- A reported trigger naming a *specific* command is often wrong; the physical mechanism (signal broadcast, zero-PID kill) usually points at a different command.
- `git show <fix-commit>` is the fastest way to prove a guard pre-existed vs was added by that commit — it resolves "stale todo" questions in seconds.
- Keep the empirical repro OS-safe: on Linux you can exercise None-guard / not-found branches (they return before any Windows-only OS call), but Windows-only broadcast paths must be reasoned about from code, not executed.
- Overlaps with [[systematic-debugging]] (reproduce + data-flow trace) and [[plan-anchor-verification]] (read-not-grep, cross-check factual claims); this skill adds the *still-live?* framing, physical-primitive grep, git-timeline archaeology, and misattribution detection.
