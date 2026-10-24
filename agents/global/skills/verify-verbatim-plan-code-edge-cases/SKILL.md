---
name: verify-verbatim-plan-code-edge-cases
description: Verify a concrete implementation plan's proposed code blocks by re-implementing them verbatim in a sandbox and running every claimed edge case, break-test input, and oracle grid against the new logic before build.
source: auto-generated
version: 1.0.1
triggers:
  - "plan review"
  - "implementation plan"
  - "pre-build review"
  - "verify plan code"
  - "edge case verification"
generated_by: ponytail
generated_from_task: "Pre-build critical review of a concrete implementation plan for the tolerant skill frontmatter parser — verifying proposed _lenient_parse/_normalize_frontmatter logic, 5 break-test re-pointing inputs, and oracle grid claims against live source."
---

## Goal
Catch correctness gaps in a concrete implementation plan BEFORE it reaches a coder, by running the plan's proposed code (not just reading it) against every claimed edge case.

## Procedure

### Step 1 — Extract the plan's proposed code blocks verbatim
For each new/rewritten function the plan specifies (e.g. `_lenient_parse`, `_normalize_frontmatter`), copy the EXACT code from the plan into a sandbox script. Do NOT paraphrase or "improve" it — you're testing what the coder will write, not what you'd write.

### Step 2 — Run every claimed edge case
For each input→expected pair in the plan's test strategy table:
- Feed the input through the proposed code path (strict → lenient fallback → normalize).
- Compare actual output to the plan's claimed expected value.
- Any mismatch is a plan defect, not a sandbox bug.

### Step 3 — Verify break-test re-pointing inputs empirically
For each test the plan says "breaks and must be re-pointed":
- Run the OLD input through the NEW logic → confirm it genuinely changes behavior (the old assertion would fail).
- Run the NEW re-pointed input through the NEW logic → confirm it produces exactly what the new assertions expect.
- For tests asserting on error message strings (e.g. PyYAML wording), run the actual library and capture the exact string — do NOT trust the plan's quoted wording.

### Step 4 — Run an oracle grid (if the plan claims strict/lenient agreement)
Build a small cartesian product of well-formed inputs (vary each field across its forms: block-list, inline-list, bare-string). For each:
- `strict = yaml.safe_load(block)` (or equivalent strict parser)
- `lenient = _lenient_parse(block_lines)`
- Assert `_normalize(strict)[k] == _normalize(lenient)[k]` for every consumed key.
- Any divergence is a plan defect in the normalization layer.

### Step 5 — Regression-diff against real data
If the repo has real data files (e.g. SKILL.md files, config files), run BOTH old and new logic over all of them. Count differences. Zero differences = safe; any difference must be explained by the plan as intended behavior change.

```python
# Pattern: old vs new regression diff
for path in real_data_dir.rglob('*.md'):
    content = path.read_text()
    old_result = old_logic(content)
    new_result = new_logic(content)
    if old_result != new_result:
        print(f"CHANGED: {path}: {old_result} -> {new_result}")
```

### Step 6 — Check the plan's "stays green" list for completeness
Read every test in the affected test files. For each test NOT in the plan's break-list, verify it would still pass under the new logic. The plan may have missed tests that feed raw shapes directly into functions (bypassing the parser) — those are the ones most likely to be overlooked.

## Tips
- The riskiest plan claims are error-message string assertions (library-specific wording like PyYAML's "cannot start any token"). ALWAYS run the actual library to confirm — never trust a quoted string in a plan.
- When the plan says "replace with a one-line assert" or "a plain guarded join" without specifying the exact code, flag it as a must-fix: the coder will invent something different. Pin the exact body in the plan.
- A plan's "5 tests break" claim is only as good as the test list being COMPLETE. Read every `def test_` in the affected files and simulate each one — don't trust that the researcher found them all.
- The oracle grid catches normalization-layer bugs that individual edge cases miss (e.g. a field that's correct for block-list but wrong for inline-list). Keep the grid small (2×2×3) but cover every form variant.
- Regression-diff against real data is the strongest evidence of safety: if 152 real files parse identically, the change is low-risk regardless of how many edge cases you tested.
