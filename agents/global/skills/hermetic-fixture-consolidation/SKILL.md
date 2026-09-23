---
name: hermetic-fixture-consolidation
description: Consolidate divergent per-file test fixtures that each redirect the same production paths into one shared conftest factory so no test destroys production state and isolation cannot drift.
source: auto-generated
version: "1.0.0"
triggers:
  - "no test destroys production state"
  - "hermetic tests pass in isolation"
  - "order-dependent test failures"
  - "redirect metrics_file to tmp_path"
  - "consolidate isolated fixture helpers"
generated_by: researcher
generated_from_task: "Comprehensive test-isolation audit + fix plan for AgentCascade: ensure no test destroys/mutates production state and all tests are hermetic"
---

## Goal
When several test files each build their own isolated fixture that redirects the same production paths (metrics store, pending/candidate/production dirs), consolidate them into ONE shared factory so no test touches production and isolation cannot drift between files.

## Procedure

### Step 1 — Inventory every production-state touch point
Grep `tests/` for the constructor (e.g. `SkillManager()`) and direct writes: `_metrics_file=`, `.record_rating(`, `.rebalance_active_skills(`, `register_skill_from_content(`, `os.environ[` set without restore, `shutil.rmtree`/`unlink` on non-`tmp_path`. Build a per-file verdict table: HERMETIC / LEAKS-METRICS / LEAKS-PENDING-TREE / MUTATES-CONFIG / ORDER-DEPENDENT / write-safe-but-latent.

### Step 2 — Confirm the leak class empirically
Read the constructor: does it read real on-disk state into a field BEFORE the test redirects the path? Fields populated at construction survive a later `_x_dir = tmp` re-point (the store dict AND separate counter/status fields). Check whether any flush runs with `_metrics_file` still on production.

### Step 3 — Write one shared factory in conftest.py
```python
@pytest.fixture
def hermetic_skill_manager(tmp_path):
    m = SkillManager()                       # __init__ already loaded the REAL file
    base = tmp_path / 'agents' / 'global'
    m._metrics_file      = base / 'skills-metrics.json'
    m._pending_dir       = base / 'pending-skills'
    m._candidates_dir    = base / 'candidates'
    m._production_skills_dir = base / 'skills'
    with m._metrics_lock:
        m._metrics = {}                      # don't leak production ratings into tests
    m._disabled_names = set(SKILLS_DISABLED)  # drop production inactive names; discover sees all real skills
    m._global_activity_turns = 0             # frozen clock (tests assume a clean default)
    yield m
```
Redirect ALL four write roots and reset EVERY `__init__`-loaded field tests assume starts clean.

### Step 4 — Funnel existing fixtures through it
Replace fixture bodies with `self.manager = hermetic_skill_manager(tmp_path)`; keep any local tmp seeding. Remove duplicated redirect/reset lines. Do NOT change test ASSERTIONS to match leaked values — the fixture is wrong, not the assertion.

## Tips
- Resetting `_disabled_names`/`_metrics` can change behavior for fixtures that relied on a production-inactive name; search those before merging.
- Verify in an ISOLATED COPY of the repo: non-test processes may rewrite the production file concurrently, so live before/after sha256 is unreliable.
- Prefer dropping a shared-tree teardown sweep that deletes a CWD-relative path under xdist; per-test `tmp_path` is auto-cleaned.
- Related: [[skill-manager-hermetic-test-fixture]], [[xdist-shared-tree-test-isolation]], [[constructor-loads-state-before-redirect-leak]].
