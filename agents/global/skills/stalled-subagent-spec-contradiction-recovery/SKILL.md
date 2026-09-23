---
name: stalled-subagent-spec-contradiction-recovery
description: When a delegated sub-agent (researcher) stalls, gets compression-killed, or reports an unresolvable contradiction, recover its full reasoning from the JSONL log and determine whether YOUR spec is self-inconsistent before re-delegating with tightened scope.
source: auto-generated
version: "1.0.0"
triggers:
  - "sub-agent stalled"
  - "researcher stuck"
  - "compression killed agent"
  - "no plan file produced"
  - "agent hit contradiction"
  - "re-delegate after failure"
generated_by: orchestrator
generated_from_task: "A delegated researcher sub-agent stalled and was compression-killed with no plan file produced, appearing to hit a contradiction. Recovered its full reasoning from the JSONL log, determined my own spec was self-inconsistent (a spec-contradiction, not an execution failure), surfaced it to the user as bounded options, got a design decision, then re-delegated after failure with tightened scope and pre-verified findings."
---

## Goal
Turn a "my sub-agent failed/stalled" situation into a precise, de-risked re-delegation — by first determining whether the failure is in the agent's execution or in YOUR spec, instead of blindly re-running the same task.

## Why this matters
The most expensive mistake is re-delegating an unchanged task after a stall: you get the same wall again, with more wasted context. A stalled researcher often isn't incompetent — it correctly detected that the instructions are self-contradictory or under-specified. Blind re-delegation burns a second (larger) context budget on the same dead end.

## Procedure

### Step 1 — Recover the full reasoning from the log, don't trust the summary
A stalled/compression-killed agent's final message is useless (it's mid-thought). Go to the JSONL log (`logs/<agent>_<instance>_<timestamp>.jsonl`) and read the LARGE reasoning blocks with `read_logs` (mode=none, high max_chars_per_message) or a quick `code_interpreter` json parse that dumps the tail of the biggest assistant message. Look for where it stopped making progress — usually a repeated "but wait... so condition X is False... contradiction" loop.

### Step 2 — Classify: agent-execution failure vs spec-contradiction
Ask: is the agent stuck because (a) it misread the code / ran out of context on a solvable task, or (b) it found that two things in MY instructions cannot both hold? The signature of (b) is the agent repeatedly re-deriving the same impossibility and never committing to an answer. If (b), DO NOT re-delegate — the fix is a design decision only the user can make.

### Step 3 — Verify the contradiction yourself before escalating
Don't take the sub-agent's word that your spec is broken. Trace the control flow / logic yourself against the actual code (a few targeted reads/greps). Confirm the impossibility is real. This step is what separates a legitimate "your design has a hole" from an agent rationalizing its own confusion.

### Step 4 — Surface to the user as concrete options, not a wall of analysis
Present: (1) what the contradiction is in plain terms, (2) 2-3 bounded options with your recommendation and the tradeoff/risk of each, (3) a specific question. Do NOT dump the agent's raw reasoning. Get the decision, THEN re-delegate.

### Step 5 — Re-delegate with tightened scope + the already-confirmed findings
The second delegation must be strictly smaller: feed it the code facts you ALREADY verified (so it doesn't re-read everything and blow context again), pin the exact design decision from Step 4, set an explicit tool-call budget ("keep under ~15 calls"), and name the output file. This is why the first stall becomes a feature — its investigation now de-risks the retry.

## Tips
- A compression kill is a SYMPTOM of over-long single reasoning blocks, not a cause. The real signal is what it was reasoning about. Read that.
- If the contradiction is in YOUR spec, own it explicitly ("my option-(b) as written is internally inconsistent") — this builds user trust and prevents them from assuming the tooling is flaky.
- When you re-delegate after a spec fix, state which prior findings are CONFIRMED (don't re-derive) vs which still need verification. This is the single biggest context-saver on the retry.
- If Step 3 shows the contradiction was NOT real (agent error), skip to Step 5 with corrected framing — but note the agent's reasoning gap so you can scope tighter.
