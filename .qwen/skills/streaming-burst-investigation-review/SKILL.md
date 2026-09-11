---
name: streaming-burst-investigation-review
description: Systematic review methodology for validating reproduction tests, benchmark harnesses, and analytical reports in streaming performance investigations; ensures test simulations accurately execute target production code paths and that conclusions are strictly supported by measured evidence
source: auto-generated
version: "1.0.0"
triggers:
  - "test file fidelity"
  - "findings report accuracy"
  - "logical gaps"
  - "streaming burst"
  - "reproduction test validation"
generated_by: burst_review1
generated_from_task: "Review sub-agent streaming burst investigation deliverables for correctness and completeness. Check test file fidelity, findings report accuracy, and logical gaps."
---

## Goal

Enable rigorous auditing of reproduction tests, benchmark harnesses, and analytical reports to ensure they accurately execute the production code they claim to study and that their conclusions are strictly supported by measured evidence.

## Procedure

### Step 1 — Identify Target Production Code

Locate and read the exact production sections referenced:
- Use `read_file` with specific line ranges (e.g., L3152-3253)
- Extract loop structures, function calls, parameter values
- Note conditional branches and initialization sequences

**Tip:** Always verify line numbers against current codebase, not historical commits.

### Step 2 — Compare Test Execution Against Production

Determine whether test actually runs target code:
- **Critical**: Does test invoke the specific method/class being studied? (e.g., `core.py._create_and_run_agent` vs `ExecutionEngine.run`)
- **Check**: Are all production parameters set identically? (e.g., `last_send=0.0` in both)
- **Check**: Is manual pool wiring (`_ws_send_queue`, `_ws_loop`) necessary or does it alter behavior?
- **Check**: Are mock boundaries appropriate? (Only LLM calls, not infrastructure)

**Red flags:**
- Test claims "exact replication" but bypasses orchestration code
- Production setup steps omitted (termination checks, state initialization)
- Parameters hardcoded differently from source

### Step 3 — Validate Conclusions Against Evidence

Ensure every claim is backed by actual measured data:
- **Origin claims**: "Burst originates in X" requires X actually being executed
- **Causation claims**: Mechanism explanations must match actual code logic
- **Comparison claims**: Profiles compared on same metrics with identical measurement methodology
- **Production extrapolations**: Clearly distinguish simulation results from production hypotheses

**Self-check questions:**
- Would removing the mock reveal different behavior?
- Could omitted setup steps create artifacts not present in production?
- Are line numbers and file references consistent across all documents?

### Step 4 — Rate Severity and Prioritize Fixes

Use severity labels:
- 🔴 **Critical**: False claims about code execution or false origin attribution
- 🟠 **Major**: Significant misrepresentation of fidelity, unsupported conclusions
- 🟡 **Minor**: Documentation inconsistencies, non-critical parameter differences
- 🔵 **Nit**: Typos, formatting issues, minor clarifications

**Fix recommendations must be:**
1. **Specific**: Exact files and line numbers
2. **Actionable**: Concrete rewrites or test additions
3. **Proportionate**: Effort matches severity level

## Tips

- **Assume misrepresentation until proven otherwise**: Tests often overclaim fidelity
- **Follow the data trail**: Trace how each metric is captured (queue, timestamps)
- **Check mock boundaries**: Only LLM/generator should be mocked in broadcast tests
- **Demand precision**: "Simulates" ≠ "Replicates exact path"
- **Use existing skills**: Leverage `code_review` and `debugging_workflow` for related patterns

## Common Pitfalls to Avoid

1. **Accepting "similar logic"** without verifying actual code execution
2. **Overlooking manual pool wiring** that could alter timing/behavior
3. **Drawing production conclusions from simulation data** without validation
4. **Ignoring missing setup steps** that affect state or flow
5. **Using vague language** like "backend issue" without pinpointing exact location

## Example Review Checklist

- [ ] Test claims match actual code execution (not just function calls)
- [ ] All production parameters verified against source
- [ ] Conclusions strictly limited to what evidence supports
- [ ] Line numbers are current and accurate
- [ ] Mock boundaries are appropriate and documented
- [ ] Severity ratings reflect actual impact on findings validity

## Output Format

Every review should include:
1. **Numbered findings** with severity rating (🔴/🟠/🟡/🔵)
2. **Specific file references** (absolute path + line numbers)
3. **Concrete fix suggestions** for each issue
4. **Final verdict**: PASS / NEEDS WORK / FAIL

---

*This skill is essential for QA, debugging, and research integrity when evaluating reproduction tests, benchmark harnesses, or any analysis that claims to study production behavior.*