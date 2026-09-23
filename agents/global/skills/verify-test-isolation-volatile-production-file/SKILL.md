---
name: verify-test-isolation-volatile-production-file
description: Verify a test-isolation fix when the production state file is volatile (rewritten by concurrent non-test processes) and/or gitignored — isolated git worktree + seeded state + before/after sha256 no-mutation proof, with the CWD gotcha that pytest may not read your seed.
source: auto-generated
version: "1.0.0"
triggers:
  - "verify tests don't destroy production file"
  - "test isolation verification volatile metrics file"
  - "before/after sha256 no mutation test proof"
  - "git worktree seed gitignored state for test verification"
  - "pytest CWD relative path not reading seeded file"
  - "prove order-independence of discovery tests"
generated_by: orchestrator
generated_from_task: "Verified a hermetic SkillManager test-isolation fix in an isolated git worktree with a seeded production metrics file, proving no production mutation and order-independence."
---

## Goal
Prove a test-isolation change is real (no production state destroyed, tests order-independent) when the production state file is VOLATILE (a concurrent non-test writer mutates it during a session) and/or GITIGNORED (absent from a fresh clone/worktree).

## Why you can't just run the suite in the live tree
A live before/after `sha256` of the production file is UNRELIABLE: a concurrent non-test process
(skill regeneration, telemetry, an activity clock) rewrites it between your two reads. You'll see the
hash change and wrongly conclude "tests clobbered it" — or see it stable and wrongly conclude "isolated."
You MUST verify in an isolated copy where YOU control the only writer (the test run).

## Procedure

### Step 1 — Create an isolated git worktree inside an allowed folder
```bash
git worktree add <allowed-rw-path>/tmp_verify HEAD
cd <allowed-rw-path>/tmp_verify
```
Copy your UNCOMMITTED fix into it (worktrees check out HEAD, not your working tree):
```bash
copy /Y <repo>\tests\conftest.py <worktree>\tests\conftest.py   # each changed test file
```

### Step 2 — Discover the production state file is ABSENT (gitignored) and seed it
Worktrees/clones don't carry gitignored files. Check: `python -c "import pathlib;print(pathlib.Path('agents/global/skills-metrics.json').exists())"` → often False.
Seed it with a KNOWN state that exercises the defect you're fixing. For order-dependence, mark a real skill inactive:
```python
# _seed_metrics.py (run in worktree)
import json, pathlib
p = pathlib.Path('agents/global/skills-metrics.json')
d = {'schema_version':'1.3','global_activity_turns':0,'skills':{
     'version-control':{'status':'inactive','total_loads':0,'by_version':{}},
     'systematic-debugging':{'status':'active','total_loads':1,'by_version':{}}}}
p.write_text(json.dumps(d,indent=2))
```
**An empty/active-only file MASKS the leak** — you must seed the exact condition that breaks the unfixed code.

### Step 3 — Capture baseline hash, run the full suite, compare (the no-mutation proof)
```bash
python -c "import hashlib;print('B',hashlib.sha256(open('agents/global/skills-metrics.json','rb').read()).hexdigest()[:16])"
python -m pytest tests/<all-affected> -n 0 -q
python -c "import hashlib;print('A',hashlib.sha256(open('agents/global/skills-metrics.json','rb').read()).hexdigest()[:16])"
# B must EQUAL A → no test mutated production state. THIS is the primary proof.
```

### Step 4 — Prove order-independence (both orders, seeded file in place)
```bash
python -m pytest tests/test_A.py "tests/test_B.py::TestClass" -n 0 -q   # order 1
python -m pytest "tests/test_B.py::TestClass" tests/test_A.py -n 0 -q   # order 2
# both must be green with the seeded state.
```

### Step 5 — Regression-revert-proof (optional but strong)
Temporarily revert the fix in the WORKTREE copy only, re-run; confirm it now fails / mutates. Restore.

## The CWD gotcha (I hit this — read before concluding anything)
A direct probe (`SkillManager()` + `discover()`) may show the suppression/leak working, yet the pytest test still passes. Cause: many classes default their state path to a **CWD-relative** path (`Path('agents/global/skills-metrics.json')`), and pytest's CWD may differ from your shell CWD — so the test manager reads a DIFFERENT (often empty) file than the one you seeded.
- Before trusting a repro, print/confirm the path the test's object actually resolves: add a debug in the fixture or check `os.getcwd()` under pytest vs your shell.
- If they differ, cd into the worktree root for the pytest run, or make the seed path match what the code resolves at runtime.
- A passing test does NOT prove the leak is absent; a failing probe does NOT prove the test is broken — reconcile WHICH file each reads first.

## Tips
- Always `git worktree remove <path> --force` when done (probe scripts leave untracked files). Remove any leftover worktrees too (`git worktree list`).
- `-n 0` for a deterministic signal; re-run the xdist-prone file N× under `-n auto` to confirm race fixes.
- The no-mutation sha256 proof is the highest-value check — it directly answers "do tests destroy production state."
- Clean up temp seed/probe scripts before removing the worktree.
- Related: [[hermetic-fixture-consolidation]] (writing the shared factory), [[constructor-loads-state-before-redirect-leak]] (the leak this verifies against), [[xdist-shared-tree-test-isolation]] (shared on-disk tree flakes), [[regression-test-revert-proof]] (revert-to-fail discipline).
