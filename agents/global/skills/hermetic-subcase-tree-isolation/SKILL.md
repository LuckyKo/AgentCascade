---
name: hermetic-subcase-tree-isolation
description: When a test has multiple sub-cases that each build an on-disk fixture tree under a shared tmp dir, isolate each sub-case to its own subdir or they leak files into each other's corpus and inflate counts.
source: auto-generated
version: "1.0.0"
triggers:
  - "two sub-cases same test"
  - "fixture leaks between cases"
  - "count inflated in second sub-case"
  - "shared tmp dir"
  - "on-disk fixture cross-contamination"
generated_by: coder
generated_from_task: "rebalance clamp test: floor and ceiling sub-cases shared self.tmp skills dir; floor's 25 files leaked into ceiling corpus, evicted=125 instead of 100."
---

## Goal
Prevent a class of silent test bugs where multiple sub-cases in one test build on-disk fixture trees under a SHARED tmp directory and leak files (and reloaded state) into each other's corpus.

## Why this matters
A fixture helper like `_rebalance_manager(tmp, names)` writes files to `tmp/skills/` AND points the metrics store at `tmp/skills-metrics.json`. If one test method runs two sub-cases (e.g. a "floor" case and a "ceiling" case) against the SAME `self.tmp`, the first sub-case's on-disk files persist when the second manager is constructed:
- The second corpus is inflated (300 + 25 = 325 servable, not 300).
- The metrics file is reloaded from disk, so the first case's now-inactive entries become re-enable candidates in the second case.
- Result: a count that looks "almost right" but is off by exactly the first sub-case's contribution (real case: `evicted` was 125 instead of 100 — the extra 25 were the floor case's leaked files).

This is INVISIBLE in a single sub-case and only surfaces when you add a second one, so it slips past review.

## Procedure

### Step 1 — Detect the shared-tree pattern
Grep your test for a fixture helper that (a) takes a tmp/dir argument and writes files into it, and (b) is called more than once within a single test method with the SAME dir argument. That's your leak surface.

### Step 2 — Give each sub-case its own subdir
```python
floor_tmp   = self.tmp / 'floor'
m   = _rebalance_manager(floor_tmp, floor_names)     # writes floor/skills/ + floor/skills-metrics.json
# ... assert floor case ...

ceiling_tmp = self.tmp / 'ceiling'
m2  = _rebalance_manager(ceiling_tmp, ceiling_names)  # isolated; no leak from floor
# ... assert ceiling case ...
```
Distinct subdirs → distinct skills trees AND distinct metrics files → no cross-contamination. `tmp_path` is already function-scoped and unique across xdist workers, so subdirs stay isolated under parallel runs too.

### Step 3 — Verify empirically
Run the test. If a count in the LATER sub-case is off by exactly the earlier sub-case's file count, that confirms the leak (not a real math bug). After isolation, re-run serial AND xdist.

## Tips
- **The tell-tale signature:** a later sub-case's count is inflated by EXACTLY the number of files the earlier sub-case created. That arithmetic match is the diagnosis — don't chase it as a production bug.
- This also applies to ANY file-backed fixture (metrics stores, caches, registries) that both writes files and reloads from disk on construction.
- Complements [[skill-manager-hermetic-test-fixture]] (per-test isolation of the WHOLE manager) — this skill is about isolating MULTIPLE SUB-CASES WITHIN a single test.
- `tmp_path` uniqueness across xdist workers is what makes subdirs safe; a module/session-scoped temp dir would still be shared.
