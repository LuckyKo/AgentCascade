---
name: xdist-shared-tree-test-isolation
description: Fix pytest/xdist flakes caused by a shared on-disk tree (e.g. agents/global/pending-skills) that parallel workers blanket-delete mid-operation — make production paths overridable via instance attributes and point them at per-test tmp_path, then prove zero flakes by running N times under -n auto.
source: auto-generated
version: "1.0.0"
triggers:
  - "xdist flake"
  - "shared temp directory race"
  - "-n auto fails serially passes"
  - "pending-skills No such file or directory"
  - "test isolation tmp_path"
  - "blanket delete shared tree parallel workers"
generated_by: coder
generated_from_task: "Fix 5 xdist flakes in test_skill_generation.py (shared agents/global/pending-skills race) + 2 MagicMock spec auto-mock bypasses."
---

## Goal
Eliminate pytest/xdist (`-n auto`) flakes whose root cause is multiple parallel worker processes sharing one real on-disk tree that an autouse fixture blanket-deletes while a sibling worker is mid-operation — by making the production write path overridable and isolating every test to its own `tmp_path` tree.

## When you know this applies
- Test passes serially (`-n 0`) but fails under `-n auto`, and the **victim test varies run-to-run** while the mechanism is constant.
- Error signatures point at a shared CWD-relative path: `[Errno 2] No such file or directory` / `[Errno 13] Permission denied` on e.g. `agents\global\pending-skills\<uuid>\SKILL.md`, plus downstream cascades (e.g. `'NoneType' object is not subscriptable`) from a missing registry entry.
- An autouse fixture calls a cleanup helper that **blanket-deletes** a shared tree at both setup and teardown of every test.

## Procedure

### Step 1 — Confirm the shared-tree race before touching code
Read the autouse fixture + its cleanup helper. Confirm it deletes a CWD-relative tree (not `tmp_path`). Then find the production write path: grep the manager/service for the hardcoded CWD-relative path (e.g. `Path(f"agents/global/pending-skills/{skill_id}")`). If that path is **hardcoded and not overridable**, that's your race surface — a sibling worker's cleanup lands between `write_text` and a later read → `[Errno 2]`.

### Step 2 — Make the production path overridable (backward-compatible)
Mirror any existing override pattern in the same module. If the code already honors `_candidates_dir` / `_production_skills_dir` via `getattr(self, '_x', None) or DEFAULT`, add the missing one the same way:
```python
# BEFORE
pending_dir = Path(f"agents/global/pending-skills/{skill_id}")
# AFTER — default path unchanged when attribute unset → byte-for-byte backward-compatible
pending_root = getattr(self, '_pending_dir', None) or Path('agents/global/pending-skills')
pending_dir = pending_root / skill_id
```
This is the ONLY production touch. Everything else is test-only.

### Step 3 — Isolate ALL roots per-test in the autouse fixture
`tmp_path` is **function-scoped and unique even across xdist workers** → no two tests share a tree, so the race surface disappears entirely:
```python
manager = SkillManager()
base = tmp_path / 'agents' / 'global'
manager._pending_dir = base / 'pending-skills'
manager._candidates_dir = base / 'candidates'
manager._production_skills_dir = base / 'skills'
yield manager
```

### Step 4 — Re-point every CWD-relative assertion at the isolated root
Grep the test file for the shared tree path. Any assertion that reads a **CWD-relative** path now inspects the real (empty) tree and silently becomes a no-op or fails:
- `target = Path('agents/global/skills') / name / 'SKILL.md'` → `fresh_manager._production_skills_dir / name / 'SKILL.md'`
- `pending_root = Path('agents/global/pending-skills')` → `fresh_manager._pending_dir`

### Step 5 — Preserve the intentional exceptions (do NOT "fix" these)
- Calls that pass an **explicit path** to a scanner, e.g. `discover([Path('agents/global/skills')])`, intentionally read the real tree (to find real production skills like `skill-creator`). Leave them — they don't consult the overridable attribute.
- The legacy blanket cleanup helper may be left as a harmless no-op sweep for one release to clear pre-existing leftovers; it now has no test-owned target in the shared tree.

## Verification (the flake-proof loop)
A single green run does NOT prove a race is gone. Run the affected file **N times under xdist** and require zero failures across all runs:
```bash
# Windows cmd (NOT bash `for i in`):
cd <repo> && for /L %i in (1,1,5) do @echo === RUN %i === & python -m pytest tests/test_skill_generation.py -n auto -q
# Then the full suite to catch cross-file effects on any remaining shared tree:
python -m pytest tests/ -n auto -q
```
Also keep a serial baseline (`-n 0`) for a deterministic signal. Note: `pytest.ini` may hard-code `-n auto` in `addopts` — see [[pytest-ini-addopts-xdist-serial-run]] for forcing a true serial run if needed (a plain `-n 0` on the CLI does override it).

## Tips
- **The victim varies, the mechanism is constant.** Don't chase whichever test failed this run; fix the shared tree and the whole class disappears.
- `tmp_path` uniqueness across xdist workers is what makes this work — a module-level or session-scoped temp dir would still be shared.
- After isolating, re-grep for any OTHER CWD-relative reference to the same tree in the file; missing one leaves a silent no-op assertion (guarded by `if path.exists()`, so it won't crash — it just stops testing what it claims).
- If other test files share the same latent race (they call the same production write under xdist), apply the same attribute isolation to their fixtures — but scope that separately if it's out of the current failure set.
- Related: [[testing-best-practices]] for general determinism; [[magicmock-spec-hides-new-methods]] for the companion "spec auto-mock bypasses a refactored-out helper" class of deterministic test failures.
