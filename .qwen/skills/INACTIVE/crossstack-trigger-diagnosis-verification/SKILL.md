---
name: crossstack-trigger-diagnosis-verification
description: Diagnose exact root triggers in cross-stack (frontend+backend) systems via end-to-end protocol tracing (e.g. DOM rebuild bursts), then delegate independent verification to a reviewer agent using ABSOLUTE file paths and a numbered load-bearing-claims list.
source: auto-generated
version: "1.0.0"
triggers:
  - "diagnose why"
  - "root cause"
  - "DOM rebuild"
  - "reviewer verify diagnosis"
  - "frontend backend protocol"
  - "exact trigger"
  - "no code changes"
generated_by: researcher
generated_from_task: "Diagnose (NO code changes) why a sub-agent panel does a FULL DOM rebuild at turn end in AgentCascade web_ui/app.js"
---

## Goal

Produce a precise, evidence-cited diagnosis of a cross-stack bug (no fix) and get it independently verified without false refutations from path-resolution failures.

## Procedure

### Step 1 — Trace end-to-end across the protocol boundary
For a "which branch fires / what state exists at moment X" question, walk BOTH sides:
- Backend: find the exact broadcast/emission site (e.g. grep for the frame `type` or helper function), confirm what data the final frame carries (guards, flags like `is_partial`, full vs tail payload).
- Frontend: find the frame handler, the state merge, the render dispatch, and the branch conditions.
Cite `file:line` for every hop. A diagnosis that stops at one stack is a theory, not a trigger.

### Step 2 — Answer each hypothesized mechanism with evidence, not silence
For every candidate trigger listed in the task (splice shrink, resync flag, placeholder desync, DOM wrapping, ...), verify and explicitly RULE IT OUT with the line that proves it (e.g. "length non-decreasing: L2102-2103; gates L2086-2100 only skip"). Distinguish "cost-only explanations" (same code path, different perf) from "different code path" and label which one you're claiming.

### Step 3 — Save memory BEFORE report delivery
Write the root-cause chain as a `.agent_lessons/` memory (load `project-memory-writing` first), including state values at the failure moment and ruled-out alternatives with evidence.

### Step 4 — Write the report with a numbered "load-bearing claims" appendix
The report must include: exact branch + state values at the moment, full causal chain, secondary triggers, ruled-out list, confidence per claim (confirmed vs inference), open questions, suggested next actions (no fix, if out of scope).

### Step 5 — Delegate verification with ABSOLUTE paths only
Critical lesson: sub-agents (reviewers) resolve relative paths against THEIR OWN workspace root, which often differs from the project root. A reviewer resolving `web_ui/app.js` against the wrong root reports "file not found → REFUTED" for a correct diagnosis.
When calling `call_agent` for verification:
1. Give EXPLICIT ABSOLUTE paths for every file the reviewer must open (never relative).
2. Provide a numbered list of load-bearing claims, each with file + line range + expected content.
3. Ask for verdict format: CONFIRMED / CONFIRMED_WITH_NITS / REFUTED with file:line counter-evidence.
4. If the reviewer returns "REFUTED — files not found", check whether it grepped the wrong root before doubting your own work; re-run with absolute paths and a note about the path mismatch.

## Tips

- Grep for the wire-protocol type strings (`'type': 'done'`, `case 'done'`) on both sides to find the boundary fast.
- Sentinel values (e.g. `lastRenderedCount='999999999'`) are prime suspects for "impossible" branch conditions — grep for the literal.
- When a hidden/early-return path skips bookkeeping, the desync defers until later (e.g. tab switch) — note this in the report.
- Keep `max_turns` modest for verification agents; the task is bounded spot-checking, not re-investigation.
