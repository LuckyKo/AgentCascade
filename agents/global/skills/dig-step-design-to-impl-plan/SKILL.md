---
name: dig-step-design-to-impl-plan
description: Turn an approved research/design doc into a phased, directly-actionable implementation plan (the "DIG step") — verified anchors, per-phase test specs, a risk register that adds new impl risks from reading code, rollback gates, and an open-decisions list. Plan only; no code.
source: auto-generated
version: "1.0.0"
triggers:
  - "implementation plan"
  - "DIG step"
  - "turn design doc into plan"
  - "phased implementation plan"
  - "design to buildable plan"
generated_by: researcher
generated_from_task: "Produce a phased implementation plan for the AgentCascade skill-scoring feature from an approved research/design doc; deliverable is the plan doc only."
---

## Goal
Convert an **approved** design/research doc into a concrete, phased implementation plan that a coder can build from with **no further design decisions** — while keeping the plan itself reviewable (verified anchors, per-phase tests, risks, rollback, open items). This is the DIG step: research + plan only, no implementation.

## Procedure
### Step 1 — Treat the design doc as source of truth; realize it EXACTLY
Read the approved doc fully FIRST. The job is to *realize* the specified design, not re-derive or "improve" it. Where the doc left a constant/behavior unspecified, that becomes an explicit decision (Step 3) — do not silently invent behavior the doc didn't ask for.

### Step 2 — Re-verify baseline + anchors against live HEAD
`git log <doc-baseline>..HEAD` to detect drift (empty = zero drift). Then **read** each cited location (not grep) to confirm the symbol is there AND the surrounding semantics match the doc's description. Produce a plan header: actual HEAD/tree state, what changed since the baseline, that all anchors were re-verified, and a "verified anchors" table mapping each cited location to what was actually found. This step delegates to [[plan-anchor-verification]].

### Step 3 — Pin the open design decisions into named decisions (D-*)
The doc's §Open Questions / refinements are usually left deliberately loose. Pin each one to a concrete, evidence-backed decision and label it `D-<tag>`. Distinguish **refinements** (the doc was silent; you're completing it) from **contradictions** (doc vs code — the code wins; record the deviation). For every load-bearing choice, state the rationale + which worked-example/edge case forces it.

### Step 4 — Phase breakdown with a dependency graph
Split into **independently shippable + testable** phases. Draw explicit dependencies and a build order (what can run in parallel vs must wait). Name each phase after its deliverable. A good split keeps pure logic (no I/O) separate from wiring, and separates prerequisites (e.g., a loadability fix) from the feature that depends on them.

### Step 5 — Per phase: files, functions, line anchors, what stays unchanged, tests
For each phase name: the exact files to touch; the functions to add/change **with current line anchors**; explicitly **what stays unchanged** (bounds the diff + the rollback); and the specific test cases that must pass. Reference the repo's EXISTING test fixtures/helpers by name so the coder extends them rather than reinventing. Be concrete — name the functions, settings keys, lock interactions, return-shape changes.

### Step 6 — Verification strategy per phase
Which existing tests to run + what new unit tests to add. For scoring/ranking logic, prefer **property tests across an input grid** (reproduce the design doc's worked numbers exactly), plus explicit guarantee tests (e.g., an ordering invariant holds for ALL counts; a read-only path is side-effect-free via deepcopy before/after compare).

### Step 7 — Risk register: carry forward + ADD new impl risks
Copy the design doc's risks, then **add NEW implementation risks you found by reading the code** — this is where the real value is (e.g., does bumping a durable counter on every event add I/O hot-path cost? what's the lock ordering vs existing locks? is some state missing from an object that will need it? which execution paths reliably count an event?). For each, give the failure mode and a mitigation. Prefer designing so the failure mode is *conservative* (no-op / over-protect), not catastrophic.

### Step 8 — Rollback / safety
Feature-gate so a bad config can't mass-act: keep any existing master switch AND consider a per-pass safety cap. State, per phase, why it's safe to ship alone and how to roll it back. The goal: no single misconfiguration can nuke the system.

### Step 9 — Open decisions (NOT guesses)
Anything genuinely ambiguous goes in an "Open decisions" section for supervisor sign-off — never silently decided. The rest of the plan must be directly actionable with zero remaining design calls. This discipline is the core of the DIG step.

### Step 10 — Review + memory before delivery
Delegate to an independent reviewer to verify anchors + formula/logic fidelity against the code (PASS/FAIL per axis). Save a project memory in `.agent_lessons/` of the verified anchors + load-bearing decisions, backlinking related lessons, so future agents don't re-derive them.

## Tips
- **"Open decisions, not guesses" is the core discipline.** An unresolvable ambiguity must surface as a sign-off item; silently picking one is how plans inherit wrong assumptions.
- The risk register that only copies the doc's list is low-value — **reading the code for NEW risks** (hot-path I/O, lock ordering, missing object state, event-count coverage) is what makes the plan trustworthy.
- "What stays unchanged" per phase is as important as "what changes": it bounds the diff and makes rollback precise.
- For settings/config features, mirror an EXISTING template block's full seam set — see [[fullstack-data-feature-plumbing]]. For read-only "what would happen" previews, keep them side-effect-free and clamp unsaved inputs identically to the config handlers — see [[read-only-threshold-preview-endpoint]].
- Keep it tight and concrete; this drives real implementation. Anchor every claim in live source; if a line number might be stale, re-read rather than trust the doc.
