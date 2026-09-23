---
name: skill-manager-hermetic-test-fixture
description: Write fast, isolated unit tests for AgentCascade's SkillManager (or any discovery-cached, file-backed subsystem) without touching production files or sleeping. Includes Phase 2 rebalance and Phase D soft-eviction fixture patterns.
source: auto-generated
version: 1.0.2
triggers:
  - "SkillManager test"
  - "hermetic skill fixture"
  - "evicted skill test"
  - "disable_skill registry"
generated_by: coder
generated_from_task: "Phase D soft-eviction loadability — building a genuinely evicted (registry-removed) skill fixture for load_full_instructions fallback tests"
---

## Goal
Write fast, isolated unit tests for AgentCascade's `SkillManager` (or any discovery-cached, file-backed subsystem) without touching production files or sleeping. Includes patterns for the Phase 2 `rebalance_active_skills()` pass and the Phase D soft-eviction loadability fallback.

## Procedure

### Step 1 — Point the metrics store at tmp_path BEFORE anything else
`SkillManager.__init__` already loaded the REAL `agents/global/skills-metrics.json` into memory. Re-pointing `_metrics_file` alone is not enough:
```python
m = SkillManager()
m._metrics_file = tmp_path / 'skills-metrics.json'
with m._metrics_lock:
    m._metrics = {}          # OR call m._load_metrics() to load a SEEDED legacy file
```
If you seeded a 1.2 store first, call `m._load_metrics()` explicitly — `__init__` ran against whatever existed at construction time.

### Step 2 — Build a hermetic skills tree and use discover() as the canonical setup
Never assign `m._skill_paths = [root]` directly: it is set INSIDE `discover()`, and sibling methods (`prune_stale_metrics`, `_servable_skill_names`) read it. Do this instead:
```python
root = tmp_path / 'skills'
(root / 'alpha').mkdir(parents=True)
(root / 'alpha' / 'SKILL.md').write_text('---\nname: alpha\n...\n---\n# Body\n')
m._cache_ttl = 0.0          # force the TTL branch to age out (no sleeping)
m.discover([root])          # sets _skill_paths AND primes the cache signature
```

### Step 3 — Respect validate_skill() in fixture content
`register_skill_from_content` runs `validate_skill`, which enforces `MIN_SKILL_BODY_LENGTH` (~100 chars) and required frontmatter. Fixture bodies must pass it or the test fails BEFORE the behavior under test is reached. Keep bodies comfortably long with a comment explaining why.

### Step 4 — Cache-invariant regressions: assert the invariant, not just the outcome
For "toggle must invalidate the cache" tests, assert BOTH that `_cache_signature is None` AND that the next `discover()` re-scans and the registry changed as expected. The field assertion is what makes it a genuine regression guard.

### Step 5 — Isolate per-test mutable roots for the candidate flow
Point every overridable root at tmp so xdist workers never share trees:
```python
base = tmp_path / 'agents' / 'global'
m._pending_dir = base / 'pending-skills'
m._candidates_dir = base / 'candidates'
m._production_skills_dir = base / 'skills'
```
Use unique names (`f'skill-{os.getpid()}'`) when the real registry is involved.

### Step 6 — REBALANCE-SPECIFIC (Phase 2): the internal migration clobbers injected state
`rebalance_active_skills()` calls `_migrate_metrics_to_v13()` FIRST, which:
- **Prunes orphans**: every metrics entry you inject needs a REAL on-disk SKILL.md file, or it's deleted → `n_qualified` collapses to 0.
- **Re-derives status from disk** for standard-location skills (D-A → active): an injected `status='inactive'` gets clobbered back to active before the snapshot.

Canonical rebalance fixture pattern:
```python
m = _rebalance_manager(tmp, names)   # creates real files + discover()
_migrate_once(m)                     # no-op rebalance (min_cap==max_cap==active count) → schema 1.3
_set_metrics(m, entries)             # inject ranking fields (loads/ratings/last_used/status='active')
# For a durable INACTIVE status that must survive the pass:
m.disable_skill('name')              # public API post-migration → sets _disabled_names + metrics status
s = m.rebalance_active_skills(k=..., min_cap=..., max_cap=...)
```
Alternative for inactive: place the file under `INACTIVE/` (non-servable → migration keeps it inactive).

### Step 7 — REBALANCE target math: force eviction with fractional k
`target = clamp(round(k × N_qualified), min_cap, max_cap)`. The min_cap floor only applies when `k×N < min_cap`. If you want eviction to trigger, ensure `target < active_count`. With k=1.0 and all skills qualified+active, target=N → nothing evicts. Use fractional k (e.g. k=0.5, N=40 → target=20) to force eviction while keeping all skills active.

### Step 8 — SOFT-EVICTION fixture (Phase D): disable_skill alone does NOT evict from a built registry
To test the `load_full_instructions` servable-disk fallback you need a skill that is **on disk at a servable location but absent from `_skills_registry`**. The trap: `_apply_status_flip` (`disable_skill`) only adds to `_disabled_names` + sets metrics status + calls `invalidate_cache()` — it does NOT immediately clear an already-built registry. So after `discover()` has populated the registry, the disabled skill is STILL in `_skills_registry`. You must re-run discovery to actually evict it:
```python
m = _rebalance_manager(tmp, ['bravo'])   # discover → bravo IS in registry
ok, _ = m.disable_skill('bravo')         # sets _disabled_names + status=inactive (still in registry!)
assert ok
m._cache_ttl = 0.0
m.discover([m._skill_paths[0]])          # re-scan → filters on _disabled_names → bravo dropped
with m._write_lock:
    assert 'bravo' not in m._skills_registry   # NOW genuinely evicted
body = m.load_full_instructions('bravo')       # servable-disk fallback must return the body
```
Assert the eviction precondition (`not in registry`) before asserting the fallback behavior — otherwise a test can pass even though it never exercised the fallback. For the "non-servable still returns None" case, move the skill deeper (e.g. `root/INACTIVE/<name>/`) so `_servable_skill_names()` excludes it; the one-level walk must NOT find it.

## Tips
- **Lock order:** mirror the established `_write_lock → _metrics_lock` order in any new method; reviewers check this first.
- **Idempotency tests:** run the operation twice with a FRESH manager over the same on-disk store, then deep-compare `_metrics` — catches "second run overwrites" bugs single-run tests miss.
- **Orphan/prune interactions:** when placing artifacts in non-standard locations (e.g. `INACTIVE/<name>/`), remember `prune_stale_metrics`' live set contains BOTH the frontmatter name and the dir name (rglob walk) — a mismatched frontmatter name keeps the entry alive as an "orphan of its own".
- **Don't sleep** to age caches out; `_cache_ttl = 0.0`.
- **Run pytest from the HOST shell, not code_interpreter** (see [[pytest-docker-subprocess-hang]]).
- **"Active" is metrics status, NOT the live registry:** disable/enable flips call `invalidate_cache()` which removes disabled skills from `_skills_registry`, so a registry-based active count under-counts after any flip. Always derive "active" from `metrics_snap[nm].get('status') == 'active'`.
- **`SKILLS_DISABLED` (env) is never auto-re-enabled:** filter it out of re-enable candidates. Test by monkeypatching `manager.SKILLS_DISABLED` (module-level list in settings.py).
- Project context: see `.agent_lessons/skill-invalidation-phase2-implementation.md` for Phase 2 and `.agent_lessons/skill-soft-eviction-loadability-phaseD.md` for Phase D specifics.
