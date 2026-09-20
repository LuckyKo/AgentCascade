---
name: differential-equality-verify-refactor
description: Verify a "behavior-preserving" string/logic refactor by running a throwaway differential test comparing old vs new output across edge cases, before trusting the equivalence claim.
source: auto-generated
version: "1.0.0"
triggers:
  - "byte-identical"
  - "output-equivalent"
  - "behavior-preserving refactor"
  - "O(n) to O(k)"
  - "tail slice"
  - "differential test"
generated_by: coder
generated_from_task: "P1 O(300) preview refactor in ActivityBar getActivityPreview — verify old vs new string output identical across message shapes."
---

## Goal
Before shipping a refactor that claims to be *behavior-preserving* (byte-identical output, same result, equivalent logic), prove it empirically with a throwaway differential test rather than trusting the claim or eyeballing the code.

## Procedure
### Step 1 — Extract the exact old and new logic verbatim
Copy BOTH the original expression/function body and the proposed replacement into a standalone script, character-for-character. Also copy any helper they depend on (e.g. `getLastWords`, normalizers) so the comparison tests the real pipeline, not just the changed line.

### Step 2 — Enumerate the input-space partitions
The equivalence usually breaks at *boundary* cases. List the logical branches of the expression and build a case for each, plus boundary values:
- field present / absent / empty-string (these are NOT the same when `|| ''` is involved)
- length exactly at the threshold (`== 300`), just below (`299`), just above
- both inputs populated AND each individually populated
- realistic non-repetitive input (repeated-char cases like `'A'.repeat(n)` can hide word-boundary bugs in `split(' ')`-style helpers)

### Step 3 — Assert equality, exit non-zero on mismatch
```js
let allPass = true;
for (const [label, msg] of cases) {
  const o = oldFn(msg), n = newFn(msg);
  const ok = o === n;
  if (!ok) { allPass = false; console.log('FAIL', label, JSON.stringify(o), JSON.stringify(n)); }
}
console.log(allPass ? 'ALL EQUIVALENT' : 'MISMATCH');
process.exit(allPass ? 0 : 1);
```
Run it. Report the case count and result.

### Step 4 — Delete the throwaway script afterward
Keep the workspace clean; the value is in the run, not the artifact.

## Tips
- **The naive equivalent is often NOT equivalent.** A "simpler" rewrite (e.g. `(c ? c.slice(-300) : r.slice(-300))` vs `((r||'')+(c||'')).slice(-300)`) silently drops data when both fields are present and one is shorter than the window. Always test the *rejected* variant too if you want to confirm why it was rejected — it documents the trap for future readers.
- **`|| ''` vs falsy:** empty string, `undefined`, `null`, and missing-key all collapse to `''` here, but only if both old AND new apply the same coercion. Test empty-string explicitly.
- **Repeated-char inputs hide word-boundary bugs.** If a downstream helper does `.split(' ')`, `'A'.repeat(500)` is one giant "word"; add a realistic multi-word case to catch off-by-one in `slice(-count)`.
- This is cheap insurance: ~10 lines, <1s runtime, and it converts an untrusted claim into evidence. If the test FAILS, you've found a real bug before review — do not "fix" by weakening the assertion; re-examine the logic.
