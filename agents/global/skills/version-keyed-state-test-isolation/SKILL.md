---
name: version-keyed-state-test-isolation
description: Diagnose and fix test failures in systems that key state (metrics/history) by a version string, where defaults collide across entities and corrupt baselines.
source: auto-generated
version: "1.0.0"
triggers:
  - "ratings_by_version"
  - "per-version history collision"
  - "version key corrupts baseline"
  - "candidate vs incumbent version"
  - "KeyError seeding metrics in test"
  - "cross-drive rename WinError 17"
generated_by: coder
generated_from_task: "Fix round on candidate-flow: 10 test failures from version-keyed rating history collision + missing metrics entry + cross-drive promotion rename"
---

## Goal
Fix test-isolation failures in systems that key per-entity state (e.g. `ratings_by_version`, load counts) by a **version string**, where a default value shared across entities silently corrupts baselines — and where registration paths leave state entries absent until first use.

## When this applies
- State is keyed by an identifier that often defaults to the same value (`1.0.0`) for different logical entities (incumbent vs candidate, v1 vs v2 of a skill).
- Tests seed or assert on `state[entity][key]` and hit `KeyError` or wrong aggregate counts (e.g. count 5 instead of 2).
- A "wipe/refresh" hypothesis in the brief does NOT explain the failures — the real bug is upstream data collision, not mechanism.

## Procedure
### Step 1 — Categorize failures by exact error before hypothesizing
Group tests by their *actual* assertion/error, not by the brief's narrative. Distinct root causes hide behind one symptom label. (Here: `KeyError` seeding, wrong aggregate count, and a `WinError 17` rename were three separate bugs.)

### Step 2 — Trace where the shared key gets polluted
When two entities share a version key, ratings/writes recorded while entity B serves land on entity A's key. Find the write site (usually resolves the "serving winner" version from a registry) and confirm both entities resolve to the same string. **The collision is the bug; the mechanism is fine.**

### Step 3 — Fix at the data boundary, not by rewriting product logic
- **Separate the keys in tests**: give each entity an explicit distinct version (`content.replace('---\n', '---\nversion: 2.0.0\n', 1)`) or a non-default incumbent version. This is a *setup* concern.
- **Do NOT** add product-code "bump if versions collide" — it usually breaks the legitimate shared-key paths and doesn't fix collisions between two same-default entities. Revert such attempts; verify they're gone.

### Step 4 — Seed state defensively when an entry is created lazily
If a registration path only creates `state[name]` on first write (e.g. via `setdefault` inside `_record_rating`), tests that seed it directly before any write hit `KeyError`. Seed with the same shape the product code uses:
```python
entry = m._metrics.setdefault(name, {'total_loads': 0, 'by_version': {}})
entry.setdefault('ratings_by_version', {})[ver] = {'count': n, 'sum': s, 'latest': l}
```
Do NOT make the product path eagerly record a synthetic initial value to satisfy tests — it inflates aggregates and changes gate/decision timing.

### Step 5 — Cross-drive file moves (Windows)
If a promotion/move uses `Path.rename(src, dst)` and `dst` can be on another drive in tests (pytest tmp dir), it raises `WinError 17`. Replace with an atomic same-dir tmp+replace copy:
```python
tmp_out = target.with_suffix('.tmp')
tmp_out.write_text(src.read_text(encoding='utf-8'), encoding='utf-8')
os.replace(str(tmp_out), str(target))
# a copy (not move) leaves src behind — best-effort cleanup after
```

### Step 6 — Verify the full suite, not just the failing class
Run the whole affected test set; a fix that clears the target class can regress sibling tests that relied on the old shared-key behavior.

## Tips
- A brief's root-cause narrative is a hypothesis, not truth — verify against the actual error text and data flow before editing product code.
- Prefer minimal test-side setup changes over product-code rewrites when the "bug" is really a shared-default in test fixtures.
- Every time you try a product-code fix that regresses other tests, revert it fully and confirm absence (grep) before moving on — half-reverts cause confusion.
- Save a project memory with the collision mechanism + the defensive-seeding pattern so the next agent doesn't re-derive it.
