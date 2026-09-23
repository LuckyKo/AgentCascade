---
name: constructor-loads-state-before-redirect-leak
description: Diagnose and fix hermetic tests that fail because a class __init__ reads real production state from disk into an instance field BEFORE the test can redirect the path, so clearing the main store does not reset the leaked field.
source: auto-generated
version: "1.0.0"
triggers:
  - "test fails with unexpected global counter value"
  - "hermetic test leaks production state"
  - "metrics_file redirect too late"
  - "constructor loads real file before tmp_path"
  - "assert X == 0 but got large number in test"
  - "activity clock not resetting between tests"
generated_by: orchestrator
generated_from_task: "Fixed 11 rebalance + activity-clock test failures where SkillManager.__init__ leaked the production global_activity_turns=120 into hermetic managers because _metrics_file was redirected after construction."
---

## Goal
Catch and fix the class of test-isolation bug where a fixture builds an object whose `__init__` reads real on-disk state, then redirects the path — leaving a leaked field at its production value instead of a clean default.

## The signature (recognize it fast)
A hermetic test fails with a state value that is **too large / non-zero** and looks like it *accumulated* or came from somewhere unexpected:
- `assert m._counter == 7` → `assert 125 == 7`
- `assert len(evicted) == 5` → `assert 0 == 5` (a leaked high clock kept everything PROTECTED)
- A counter that increments by the test's own delta on top of a big baseline (120, 125, 128, 130 across the suite).

The tell: the value is NOT random and NOT zero — it matches a real persisted production value.

## Procedure

### Step 1 — Confirm the constructor reads state before the redirect
Open the class `__init__`. Look for a load call that runs during construction:
```python
def __init__(self):
    self._counter = 0
    self._load_metrics()   # ← reads self._metrics_file (still the REAL path here)
```
and in `_load_metrics`:
```python
self._counter = int(data.get('global_activity_turns', 0) or 0)  # leaks production value
```

### Step 2 — Show the fixture redirects TOO LATE
The test helper does:
```python
m = SkillManager()                          # __init__ already loaded the REAL file → _counter = 120
m._metrics_file = tmp_path / 'skills-metrics.json'   # too late
with m._metrics_lock:
    m._metrics = {}                          # clears the STORE but NOT the separate counter field
```
`m._metrics = {}` only clears one leaked field; the counter is a *separate* instance attribute that survives.

### Step 3 — Prove it's the production value, not test accumulation
Read the real file and confirm it carries the suspicious value:
```bash
grep '"global_activity_turns"' agents/global/skills-metrics.json   # e.g. 120
```
If the number matches (or is baseline + the test's own bumps), root cause confirmed.

### Step 4 — Fix by resetting EVERY leaked field after construction
Add a reset for each field `__init__` populated from disk that the test assumes starts clean, placed right next to the existing store-clear so both are grouped:
```python
m = SkillManager()
m._metrics_file = tmp_path / 'skills-metrics.json'
with m._metrics_lock:
    m._metrics = {}
m._global_activity_turns = 0   # __init__ leaked the production counter; tests need a frozen clock
```

### Step 5 — Audit for completeness (the usual miss)
Grep the whole test file for EVERY `ClassName()` construction and every helper that depends on a clean default. Fixtures often have TWO shapes: a shared helper AND one or two tests that construct the object directly. The direct-construction ones are the ones you'll miss. Reset in each.

## Tips
- **The "correct" long-term fix** is usually a production API change (let the constructor accept an initial path so it never reads production). That's often out of scope for a test-only patch — resetting the leaked fields is the acceptable minimal mitigation, but note the real gap.
- **Do NOT change the assertions.** In this class of bug the assertions encode correct intended behavior; the FIXTURE is what's wrong. Changing the assertion to match the leaked value papers over the isolation defect.
- A helper docstring often states the precondition you violated (e.g. "only works when the clock is frozen at 0") — read the helpers' docstrings before assuming the fixture is fine.
- After fixing, run the FULL file (not just the failing tests) to confirm you didn't break a test that legitimately relied on the old (leaked) behavior.
- Related: [[skill-manager-hermetic-test-fixture]] (the redirect pattern this skill documents as insufficient), [[xdist-shared-tree-test-isolation]] (shared on-disk tree flakes — a different mechanism), [[baseline-differential-failure-verification]] (prove which failures are pre-existing/environmental before fixing).
