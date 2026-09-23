---
name: durable-counter-batched-flush-store
description: Add a persisted cumulative counter (e.g. global_activity_turns) plus per-entity stamps to an existing batched-flush JSON metrics store, with idempotent migration seeding and a conservative-by-construction safety helper that protects rather than mass-evicts on a broken clock.
source: auto-generated
version: "1.0.0"
triggers:
  - "persisted cumulative counter"
  - "global_activity_turns"
  - "activity clock skill scoring"
  - "last_activity_turn"
  - "batched flush metrics store new field"
  - "idempotent migration seed rollout"
  - "conservative safety helper never mass-evict"
  - "durable counter survives restart session"
generated_by: coder
generated_from_task: "Phase A skill-scoring: durable global_activity_turns + per-skill last_activity_turn in skills-metrics.json, bumped once per user turn via engine hook, conservative _activity_age helper, D-SEED idempotent migration."
---

## Goal

Add a durable counter (and optional per-entity stamps) to an EXISTING batched-flush JSON metrics store — surviving restart/session with NO new I/O hot path and NO mass-eviction on a broken clock — done correctly and verifiably.

## When to use
You must persist a cumulative value that (a) outlives process restart + session boundaries, (b) is bumped frequently (per-turn/per-event), and (c) feeds a safety gate where a stuck/missing value must be the CONSERVATIVE outcome (protect, never cull). Distinct from [[fullstack-data-feature-plumbing]] (which surfaces a value to the UI/API — here there is no reader yet; pure additive recording).

## Procedure

### Step 1 — Reuse the existing store + flush path (do NOT add I/O)
- Put the counter as a NEW **top-level** field in the same JSON file as the per-entity records (not a separate file, not a new schema version unless the plan demands it). Per-entity stamps are NEW **optional** keys on each record.
- Read both via `.get(key, default)` at load time so old files load cleanly — no migration required to READ.
- Snapshot the counter under the SAME lock as the existing records when building the flush payload (e.g. inside the existing `with self._metrics_lock:` deepcopy block).

### Step 2 — Bump method reuses the batched cadence
Mirror the existing writer exactly: increment in-memory under the lock, bump the shared `_pending_flush_count`, and only set `flush_needed=True` when the threshold OR interval fires. Flush OUTSIDE the lock. Never add a per-event disk write.

### Step 3 — Bump hook at the call site is best-effort
- Put the bump INSIDE the existing once-per-unit guard (e.g. `if not getattr(instance,'_turn_consumed',False):`) so it fires exactly once per unit, beside any sibling recorder (telemetry).
- Wrap in `try/except: pass` — a store failure must never break the caller loop. **The loop-safety guarantee lives at THIS hook, not in the store** (see gotcha below).

### Step 4 — Idempotent migration seed (safe rollout)
In the existing one-time migration (the place that already fills missing `status`/`last_used` etc.), fill ONLY entries whose new key is `None`/missing, to the current counter value. Never overwrite a real stamp. This gives every pre-existing entity a fresh fair window so nothing mass-evicts on first upgrade. Re-running is a no-op. Also give freshly-created records the seeded value for consistency.

### Step 5 — Conservative-by-construction safety helper
The age/gate helper must degrade toward the SAFE outcome when inputs are missing:
```python
def _age(self, entry, global_val, now):
    stamp = entry.get('last_stamp') if isinstance(entry, dict) else None
    if global_val > 0 and isinstance(stamp, int):
        return max(0, global_val - stamp)          # primary: counter clock
    lu = entry.get('last_used') if isinstance(entry, dict) else None   # fallback
    if lu:
        try:
            return int(max(0.0, now - _iso_to_epoch(lu)) / SECONDS_PER_UNIT)
        except Exception:
            return 0
    return 0                                        # nothing usable => PROTECTED (safe)
```
A frozen/missed clock must EXTEND protection, never mass-cull. Keep the fallback rate a named, env-overridable constant so it's tunable without code change.

### Step 6 — Tests (one per guarantee)
1. Bump increments counter AND survives reload into a fresh instance (restart persistence).
2. The stamping event sets the entity's stamp to the current counter.
3. A NON-stamping event does NOT reset it (assert still None/unchanged).
4. Idle invariance: no bump → age frozen at 0; +N bumps → age == N.
5. Migration seed fills missing stamps to counter → age 0; and re-run does NOT clobber an existing stamp.
6. Fallback when counter==0 + past last_used → bounded >0.
7. Safest case: counter==0 + no last_used → 0 (PROTECTED).
8. No per-event I/O: N-1 sub-threshold bumps write nothing; the Nth triggers exactly one flush.

## Tips / pitfalls
- **The existing flush already swallows its own I/O errors** (try/except inside `_flush_metrics_to_disk`). So it never actually raises — do NOT write a test expecting your bump method to catch a flush exception. The "never breaks the loop" guarantee is at the CALLER hook's try/except. Verify the no-hot-path contract instead (file not written below threshold).
- **Fixtures often don't pre-populate per-entity records.** If your test does `m._metrics[name]['key'] = ...` and gets KeyError, the fixture built an empty store — seed `m._metrics[name] = {...}` explicitly first.
- **No schema bump by default** for a new top-level + optional per-entity key: both read via `.get(default)`. Only bump the version string if the plan explicitly requires it; an unrequested bump is a deviation reviewers will flag.
- **Pure additive = no reader yet.** Grep for the new field name across the package to PROVE nothing reads it at runtime until the later phase wires it in — that's your "no behavior change" evidence.
- Add the inverse of any existing ISO-timestamp helper (e.g. `_iso_to_epoch`) if you need wall-clock fallback; treat naive timestamps as UTC to match the writer.
