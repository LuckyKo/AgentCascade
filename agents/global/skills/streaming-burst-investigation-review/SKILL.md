---
name: streaming-burst-investigation-review
description: Systematic review of reproduction tests, benchmark harnesses, and analytical reports for a performance/streaming investigation — verify the test actually executes the target production code path, parameters match source, mock boundaries are appropriate, and conclusions are strictly supported by measured evidence.
source: auto-generated
version: "1.0.0"
triggers:
  - "test file fidelity"
  - "findings report accuracy"
  - "logical gaps"
  - "reproduction test validation"
  - "benchmark harness review"
---

## Goal

Rigorous audit of reproduction tests, benchmark harnesses, and analytical reports to ensure they actually execute the production code they claim to study, and that conclusions are strictly supported by measured evidence.

## Procedure

1. **Identify target production code.** Read the exact production sections referenced (`read_file` with specific line ranges). Extract loop structures, function calls, parameter values; note conditional branches and initialization sequences. Verify line numbers against the CURRENT codebase, not historical commits.
2. **Compare test execution against production.** Does the test invoke the specific method/class being studied (not a parallel reimplementation)? Are all production parameters set identically? Is manual wiring of infrastructure necessary or does it alter behavior? Are mock boundaries appropriate (mock only the LLM/generator, not infrastructure)?

   **Red flags:** test claims "exact replication" but bypasses orchestration code; production setup steps omitted (termination checks, state init); parameters hardcoded differently from source.
3. **Validate conclusions against evidence.** Origin claims ("burst originates in X") require X actually being executed. Causation claims must match actual code logic. Comparison claims need identical metrics + measurement methodology. Clearly distinguish simulation results from production hypotheses.

   **Self-check:** would removing the mock reveal different behavior? could omitted setup steps create artifacts not present in production? are line numbers/file refs consistent across all documents?
4. **Rate severity + prioritize fixes.** 🔴 Critical — false claims about code execution or false origin attribution. 🟠 Major — significant misrepresentation of fidelity, unsupported conclusions. 🟡 Minor — documentation inconsistencies, non-critical parameter differences. 🔵 Nit — typos/formatting. Fixes must be specific (exact files/lines), actionable (concrete rewrites/test additions), and proportionate to severity.

## Tips

Assume misrepresentation until proven otherwise — tests often overclaim fidelity. Follow the data trail (how each metric is captured: queue, timestamps). Check mock boundaries (only LLM/generator should be mocked in broadcast tests). Demand precision — "simulates" ≠ "replicates exact path." Leverage `code-review` and `systematic-debugging` for related patterns.

## Common pitfalls

1. Accepting "similar logic" without verifying actual code execution.
2. Overlooking manual pool/infrastructure wiring that alters timing/behavior.
3. Drawing production conclusions from simulation data without validation.
4. Ignoring missing setup steps that affect state or flow.
5. Vague language ("backend issue") without pinpointing the exact location.

## Output format

Numbered findings with severity (🔴/🟠/🟡/🔵); specific file references (path + line numbers); concrete fix suggestions for each; final verdict PASS / NEEDS WORK / FAIL.
