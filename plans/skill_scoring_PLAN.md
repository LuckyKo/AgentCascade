# Skill-Scoring Feature — Implementation Plan (PLAN)

**Status:** IMPLEMENTATION PLAN (DIG step) — no code written; this is the executable action plan for a coder.
**Source of truth:** `plans/skill_scoring_RESEARCH.md` (approved design). This plan realizes that design EXACTLY and re-verifies every anchor against live HEAD.
**Author:** researcher (skill_scoring_plan) · **Date:** 2026-09-21

---

## 0. Verified Baseline & Deviations

- **`HEAD = bb5b6e72`** (v0.2.7). `git log bb5b6e72..HEAD` is **empty → zero drift** since the research baseline; working tree clean for `agent_cascade/ tests/ web_ui/`.
- **All 13 research-doc line anchors re-verified by READING (not grep) against live HEAD — all match exactly.** No line drift, no stale descriptions. See §0.2.
- The research doc is accurate. The items below are **refinements the design deliberately left open** (§19 of the research doc), now pinned to make the plan directly actionable. They are NOT contradictions of the design.

### 0.1 Load-bearing implementation decisions (pinned)

| ID | Decision | Rationale / evidence |
|---|---|---|
| **D-ACT** | `last_activity_turn` resets on **RATING only**, not on load. | Required so §8 case 4 "loaded-never-rated → USELESS" ages out (A must grow unbounded while the skill is only loaded). If loads reset the clock, that worst case stays PROTECTED forever. A helpful-but-unrated skill also ages out — accepted per research §12 Risk #4 (consistent with `n = ratings.count` = "deliberate applied uses", §2.1). |
| **D-COUNT** | Durable cumulative counter = **top-level field `global_activity_turns`** in `skills-metrics.json`; bumped by new `SkillManager.bump_activity_turn()`, hooked in `engine/core.py:_consume_turn` beside `record_user_turn`. | Telemetry's `total_user_turns` (telemetry.py:72/330) is per-session (`_session_stats`) — cannot serve as the cumulative clock. The metrics store is the existing durable state; flush is batched (threshold 5 / interval 30s) so a per-turn bump adds **no new I/O hot path** (in-memory increment under `_metrics_lock`; disk write only on batched flush). |
| **D-SEED** | On first upgrade, seed every existing skill's `last_activity_turn = current global_activity_turns`. | Gives every skill a fresh 50-turn fair window after ship → **nothing mass-evicts immediately** (safe rollout). New skills already get this via the initial 5.0 rating (`_record_rating`, manager.py:1443) which sets `last_activity_turn` under D-ACT. |
| **D-SAFE** | Add a new safety setting **`SKILL_MAX_EVICTIONS_PER_PASS`** (default 25, clamp [0,1000]) capping total evictions in one pass; keep existing `skill_auto_invalidate_enabled` master switch as the rollback gate. | Research §9 lets the absolute gate evict BAD/USELESS even under cap headroom; without a per-pass cap a bad config could nuke the corpus. This is the "bad config can't mass-evict" gate (supervisor note #5). |
| **D-FALLBACK** | Activity clock: primary = turns (`A = global_activity_turns − last_activity_turn`); fallback = wall-clock when `global_activity_turns == 0` (counter never advanced). | Research §15 / Risk #3. Exact wall-clock→turns rate is an **open decision** (§8 below) — the safe default is "counter stuck ⇒ A≈0 ⇒ all PROTECTED" (conservative, no mass eviction), which needs no conversion constant. |

### 0.2 Verified anchors table (implementer spot-check list)

| Claim | Location | Verified against live HEAD? |
|---|---|---|
| `_default_metrics_entry` = `{'total_loads':0,'by_version':{},'status':...}` (single source for per-skill shape) | manager.py:70-74 | ✓ (add `last_activity_turn` here + read-side default) |
| `_load_metrics` / `_flush_metrics_to_disk`; batched flush `_FLUSH_THRESHOLD=5`, `_FLUSH_INTERVAL=30.0` | manager.py:196-221 / 223-263 (L190-191) | ✓ (add top-level `global_activity_turns` read+write) |
| `_servable_skill_names()` returns **lowercase names only** (no paths), no `_disabled` filter | manager.py:329-361 | ✓ (Phase D needs a separate path-resolution walk) |
| `_migrate_metrics_to_v13` lock order `_write_lock -> _metrics_lock`, seeds `last_used` from mtime, flushes once | manager.py:363-431 | ✓ (add `last_activity_turn` seed step here) |
| `_rank_key = (rating_num, loads, last_used, name)` ascending worst-first; unrated → `-1.0` | manager.py:527-542 | ✓ (extend eviction ordering to `(class_ordinal, score, …)`) |
| `rebalance_active_skills`: migrate→snapshot→math→sort→apply; `evict_threshold=max(min_cap,min(max_cap,raw))`, `to_evict=active_ranked[:max(0,active_before-evict_threshold)]` | manager.py:544-646 | ✓ (insert OR-composition + class-ordinal ordering) |
| `compute_rebalance_preview`: read-only, **skips migration**, returns `{ok,n_qualified,raw,evict_threshold,reenable_target,n_servable,active_count,k,min_cap,max_cap}` | manager.py:648-693 | ✓ (add per-skill class/score; keep side-effect-free) |
| `_increment_load_count` / `_record_rating` under `_metrics_lock`, batched flush; `record_rating` validates 0–10 | manager.py:695-722 / 724-779 | ✓ (set `last_activity_turn` in `_record_rating` per D-ACT) |
| new skill auto-rated `SKILL_RATING_INITIAL` at registration (`_record_rating(name, SKILL_RATING_INITIAL)`) | manager.py:1443 + settings.py:540-541 | ✓ (seeds `last_activity_turn` under D-ACT) |
| `load_full_instructions` registry-only; returns None at L1117 when name absent | manager.py:1085-1146 | ✓ (Phase D fallback target) |
| `get_all_metadata` backs the `scan_skills` tool | manager.py:1031 + tools/custom/scan_skills.py:17-143 | ✓ (inactive already marked via `get_inactive_names()` L102/L113) |
| `SKILL_ACTIVE_TARGET_K/MIN_CAP/MAX_CAP`, `SKILL_RATING_INITIAL`, `CANDIDATE_MIN_RATINGS` | settings.py:545-547 / 540 / 555-557 | ✓ (template for new settings) |
| config handlers: persistence key list L84-90; skill handlers L713-753 (clamp `min(max(lo,val),hi)`; max_cap cross-field ≥ min_cap L752-753) | config_handlers.py | ✓ |
| pool rebalance daemon thread gated by `skill_auto_invalidate_enabled`, reads k/min_cap/max_cap from `llm_cfg` | pool/core.py:236-245 | ✓ (extend kwargs with new constants) |
| API: `/api/skills` L1003, `/api/skills/threshold` preview L1011-1063 (clamps identically + query overrides), `initial_llm_cfg` seed L1563-1567 | api_server.py | ✓ |
| `total_user_turns` per-session (init 0; +=1 per user turn) | telemetry.py:72 / 330 (`record_user_turn`) | ✓ (NOT the cumulative clock — see D-COUNT) |
| user-turn recording call site (guarded "must never break the agent loop") | engine/core.py:162-173 (`_consume_turn`) | ✓ (Phase A bump hook) |
| frontend seams: registry L209-213, saveSettings L1197-1200, restore L1409-1414, getGenerateCfg L5721-5722; index.html inputs L764-778 | web_ui/app.js + index.html | ✓ (mirror for 9 new settings) |
| test fixtures `_rebalance_manager`/`_migrate_once`/`_set_metrics`/`_disable_after_migration`; preview-parity test L1856 | tests/test_skills_system.py:1503-1541 / 1856 | ✓ (template for new tests) |

---

## 1. Phase Breakdown & Dependencies

| Phase | Name | One-line summary | Depends on |
|---|---|---|---|
| **A** | Persisted cumulative activity-turn counter | Add a durable `global_activity_turns` + per-skill `last_activity_turn`, bumped each user turn, surviving restart/session, with wall-clock fallback. | — (foundation) |
| **B** | Score + classify as PURE functions | `skill_score()` / `skill_classify()` over a metrics snapshot (+ activity-age A); no I/O, no side effects; reusable by preview and real pass. | — (independent of A; takes A as input) |
| **C** | Wire into preview + rebalance | Extend `compute_rebalance_preview` (read-only per-skill class/score) and `rebalance_active_skills` (OR-composition + class-ordinal eviction ordering). | A + B |
| **D** | Soft-eviction loadability prerequisite | `load_full_instructions` disk/servable fallback so evicted skills stay loadable via `load_skill` + discoverable via `scan_skills:all`. | — (independent) |
| **E** | Settings exposure + UI preview row | All §18 constants (+ D-SAFE cap) through the full 6-seam pattern; a UI preview row showing each skill's class/score. | C |

**Build order:** A, B, D can proceed in parallel (independent). C after A+B. E after C. Each phase is independently shippable + testable (see §7 rollback/safety).

---

## 2. Phase A — Persisted Cumulative Activity-Turn Counter

**Goal:** a durable `global_activity_turns` that survives restart/session and is bumped once per user turn, plus per-skill `last_activity_turn`, so the activity-age `A = global_activity_turns − last_activity_turn` (idle adds zero turns). Wall-clock fallback when unavailable.

### 2.1 Files to touch
- `agent_cascade/skills/manager.py` — primary.
- `agent_cascade/engine/core.py` — bump hook.
- `tests/test_skills_system.py` — new tests.

### 2.2 Changes (concrete)

**(a) `SkillManager.__init__` (manager.py:174-192):** add attribute `self._global_activity_turns: int = 0`. Loaded in `_load_metrics`.

**(b) `_default_metrics_entry` (manager.py:70-74):** add `'last_activity_turn': None` to the default shape (single source). Read-side: treat a missing/`None` value as "no clock yet" → handled by the age helper (§2.4), NOT by assuming 0.

**(c) `_load_metrics` (manager.py:196-221):** read top-level `data.get('global_activity_turns', 0)` into `self._global_activity_turns`. Per-skill `last_activity_turn` rides along in the existing `skills` dict (no schema bump required — it's a new optional per-skill key; old files simply lack it → age helper treats as "seed on first touch").

**(d) `_flush_metrics_to_disk` (manager.py:237-241):** change the payload to include the counter:
```python
data = {'schema_version': '1.3',
        'skills': _copy.deepcopy(self._metrics),
        'global_activity_turns': self._global_activity_turns}
```
(snapshot read of `_global_activity_turns` under `_metrics_lock` alongside the existing deepcopy).

**(e) New method `bump_activity_turn(self) -> None`** (place after `_record_rating`, ~L767): increment the durable counter, reuse the batched flush cadence so a single turn never forces disk I/O:
```python
def bump_activity_turn(self) -> None:
    """Advance the persisted cumulative user-turn clock by one (activity clock, research §15).

    In-memory increment under _metrics_lock; disk write only on the existing batched
    flush cadence (_FLUSH_THRESHOLD / _FLUSH_INTERVAL) — same path as load/rating writes.
    Best-effort; never raises (callers wrap in try/except 'must never break the loop').
    """
    with self._metrics_lock:
        self._global_activity_turns += 1
        self._pending_flush_count += 1
        now = time.monotonic()
        should_flush = (self._pending_flush_count >= self._FLUSH_THRESHOLD or
                        (now - self._last_flush_time) >= self._FLUSH_INTERVAL)
        if should_flush:
            self._pending_flush_count = 0
            self._last_flush_time = now
            flush_needed = True
        else:
            flush_needed = False
    if flush_needed:
        self._flush_metrics_to_disk()
```

**(f) `_record_rating` (manager.py:735-751):** under D-ACT, stamp the skill's clock on every rating (deliberate applied use):
```python
# inside the existing `with self._metrics_lock:` block, after entry['ratings'] = ratings:
entry['last_activity_turn'] = self._global_activity_turns   # resets this skill's activity clock
```
This also covers the initial 5.0 rating at registration (manager.py:1443) → brand-new skills get `A≈0` → PROTECTED (the crux requirement).

**(g) Bump hook in `engine/core.py:_consume_turn` (L162-173):** add a guarded bump right beside `record_user_turn`:
```python
if not getattr(instance, '_turn_consumed', False):
    instance._turn_consumed = True
    if (tel := self._telemetry()) is not None:
        try:
            tel.record_user_turn(instance.instance_name)
        except Exception:
            pass  # telemetry must never break the agent loop
    sm = getattr(self.pool, 'skill_manager', None) if hasattr(self, 'pool') else None
    if sm is not None:
        try:
            sm.bump_activity_turn()
        except Exception:
            pass  # activity clock must never break the agent loop
```

**(h) Rollout seed (D-SEED) in `_migrate_metrics_to_v13` (manager.py:402-428):** inside the existing `with self._write_lock: with self._metrics_lock:` block, for every entry whose `last_activity_turn` is `None`/missing, set it to `self._global_activity_turns`. This gives all pre-existing skills a fresh fair window on first upgrade (safe rollout). Idempotent (only fills missing, like the existing `status`/`last_used` seeding).

**(i) Age helper (used by Phase B/C):** new pure-ish method `_activity_age(self, entry, global_turns, now) -> int`:
```python
def _activity_age(self, entry: dict, global_turns: int, now: float) -> int:
    """Activity-age A in user-turns since the skill last had a chance (research §15).

    Primary: turns clock. Fallback (D-FALLBACK): if the durable counter never advanced
    (global_turns == 0), degrade to wall-clock via last_used; if neither is available,
    return 0 (=> PROTECTED, conservative — never mass-evict on a broken clock).
    """
    lat = entry.get('last_activity_turn')
    if global_turns > 0 and isinstance(lat, int):
        return max(0, global_turns - lat)
    # wall-clock fallback (rate is an open decision — see §8 OD-1)
    lu = entry.get('last_used')
    if lu:
        try:
            age_s = max(0.0, now - _iso_to_epoch(lu))
            return int(age_s / SKILL_WALLCLOCK_SECONDS_PER_TURN)  # default constant; see OD-1
        except Exception:
            return 0
    return 0
```
`SKILL_WALLCLOCK_SECONDS_PER_TURN` is a new internal constant (settings.py) — its value is **Open Decision OD-1**.

### 2.3 What stays unchanged
- `_increment_load_count` (manager.py:695-722) — **do NOT stamp `last_activity_turn` here** (D-ACT). Loads must not reset the clock.
- Lock order (`_write_lock -> _metrics_lock`) — preserved everywhere.
- Batched flush cadence — reused, not changed.

### 2.4 Tests (add to `tests/test_skills_system.py`)
Use `_rebalance_manager`/`_set_metrics` fixtures. New class `TestActivityClock`:
1. **`test_bump_advances_persisted_counter`** — fresh manager; call `bump_activity_turn()` N times; assert `_global_activity_turns == N`; after a forced flush, reload from disk and assert the counter survived (restart persistence).
2. **`test_record_rating_stamps_last_activity_turn`** — set counter to T via bumps; record a rating; assert `entry['last_activity_turn'] == T`.
3. **`test_load_does_not_stamp_clock`** (D-ACT) — bump to T, then `_increment_load_count`; assert `last_activity_turn` unchanged/None.
4. **`test_age_idle_break_invariance`** — skill rated at counter=T (A=0); do NOT bump (idle); assert `_activity_age == 0` (idle adds zero turns → frozen). Then bump +50; assert A==50.
5. **`test_rollout_seed_gives_fresh_window`** (D-SEED) — pre-existing skill with `last_activity_turn=None`; run migration; assert it was seeded to current counter → `_activity_age == 0`.
6. **`test_wallclock_fallback_when_counter_zero`** — counter stays 0, entry has `last_used` in the past; assert A > 0 (wall-clock path) and bounded by the rate constant.
7. **`test_bump_never_raises_and_no_per_turn_io`** — force `_flush_metrics_to_disk` to raise; assert `bump_activity_turn` doesn't propagate; assert a single bump below threshold does not rewrite the file (mtime/size unchanged).

---

## 3. Phase B — Score + Classify as PURE Functions

**Goal:** `skill_score()` and `skill_classify()` as pure functions over a metrics snapshot (+ activity-age A) — no I/O, no side effects — so they're trivially unit-testable and reused by BOTH the preview path and the real pass. This is the heart of the design; implement the formula EXACTLY (research §5/§7).

### 3.1 Files to touch
- `agent_cascade/skills/scoring.py` — **new module** (keeps manager.py lean; pure, no imports beyond `math`). Import into manager.py.
- `tests/test_skills_system.py` — new property tests.

> Rationale for a separate module: the design requires these to be side-effect-free and reusable by two call sites; a dedicated pure module is the cleanest home and avoids bloating manager.py. If preferred, they can instead be module-level functions in manager.py — either works; pick one and keep them free of `self`/I/O.

### 3.2 Exact signatures + bodies (implement verbatim)

```python
# agent_cascade/skills/scoring.py
"""Pure skill-scoring functions (research §5/§7). No I/O, no side effects, no globals read."""
import math

CLASS_BAD = 'BAD'
CLASS_PROTECTED = 'PROTECTED'
CLASS_USEFUL = 'USEFUL'
CLASS_USELESS = 'USELESS'
CLASS_UNPROVEN = 'UNPROVEN'

# Eviction-ordering ordinals (research §17): lower evicts first. PROTECTED has no ordinal
# (excluded from the eviction candidate set entirely).
CLASS_ORDINAL = {CLASS_BAD: 0, CLASS_USELESS: 1, CLASS_UNPROVEN: 2, CLASS_USEFUL: 3}


def _shrinkage_qhat(n: int, avg, q0: float, kq: float) -> float:
    """Posterior-mean / empirical-Bayes quality estimate in [0,10]; q̂=q0 when n=0 (research §4.2)."""
    a = avg if (avg is not None and n > 0) else q0
    return (n * a + kq * q0) / (n + kq)


def skill_score(n: int, avg, L: int, A: int, *,
                q0=5.0, kq=5, n_half=8, g_half=20, r_floor=0.5, tau_turns=200) -> dict:
    """Pure bounded score in [0,1] (research §5). Returns all factors for display/reproducibility."""
    qhat = _shrinkage_qhat(n, avg, q0, kq)
    u = 1.0 - math.exp(-n / n_half)                      # saturating usage, [0,1]
    w = math.exp(-max(0, L - n) / g_half)                # load-waste penalty, [0,1] (w=1 when L<=n)
    r = r_floor + (1.0 - r_floor) * math.exp(-A / tau_turns)  # bounded recency, [r_floor,1]
    S = u * (qhat / 10.0) * w                            # time-stable core, neutral at 0.5
    score = S * r                                        # full score; recency is a minor tiebreaker
    return {'score': score, 'S': S, 'qhat': qhat, 'u': u, 'w': w, 'r': r}


def skill_classify(n: int, avg, A: int, *,
                   q0=5.0, kq=5, dq=0.5, n_min=5, fair_window_turns=50) -> str:
    """Pure quality-gated classification (research §7). Precedence BAD > PROTECTED > {USEFUL,USELESS} > UNPROVEN."""
    qhat = _shrinkage_qhat(n, avg, q0, kq)
    if qhat <= q0 - dq and n >= n_min:
        return CLASS_BAD                                  # clear harm + confident; evicted even if young
    if A < fair_window_turns:
        return CLASS_PROTECTED                            # <N user-turns since last chance (unless BAD)
    if qhat >= q0 + dq and n >= n_min:
        return CLASS_USEFUL                               # above baseline + meaningfully used
    if abs(qhat - q0) <= dq:
        return CLASS_USELESS                              # neutral quality, not earning its keep
    return CLASS_UNPROVEN                                 # monitor / keep (exploration)


def eviction_rank_key(class_name: str, score: float, name: str):
    """(class_ordinal, score, name) — BAD(0)<USELESS(1)<UNPROVEN(2)<USEFUL(3); lower evicts first (research §17)."""
    return (CLASS_ORDINAL[class_name], score, name.lower())
```

**Notes:**
- `avg` may be `None` when `n==0`; both functions handle it via `_shrinkage_qhat`.
- `skill_classify` needs `kq` to compute `q̂` (it is a gate on quality, not just count) — pass the same `kq` as the score so the two never disagree.
- The classification keys off `q̂`, `n`, and `A` — **NOT** off `r`/raw `score` (research §5/§7). `score` is used only for ranking within a class + UI display.
- `eviction_rank_key` is the §17 ordering guarantee: the ordinal dominates the lexicographic comparison, so BAD always sorts before USELESS regardless of score.

### 3.3 What stays unchanged
Nothing — this phase is purely additive (new module). No existing behavior changes.

### 3.4 Tests (add to `tests/test_skills_system.py`) — new class `TestSkillScoringPure`
**Property tests across an n-grid** (the design's worked numbers in §8 must reproduce exactly):
1. **`test_score_reproduces_research_section8`** — assert the 7 §8 cases produce the documented `q̂`, `S`, and class to ~3 decimals:
   - case1 brand-new (n=1,avg=5,L=1,A≈0) → PROTECTED, S≈0.0588
   - case2 perfect-rare (n=2,avg=10,L=2) → UNPROVEN, q̂≈6.429
   - case3 popular-bad (n=30,avg=3,L=30) → BAD, q̂≈3.286
   - case4 loaded-never-rated (n=0,avg=None,L=50,A≥50) → USELESS, S==0.0
   - case5 high-count-near-baseline (n=40,avg=5.1,L=40) → USELESS, q̂≈5.089
   - case6 good-rare (n=5,avg=8,L=5) → USEFUL, q̂==6.5
   - case7 clearly-bad-low-n (n=3,avg=2,L=3) → UNPROVEN
2. **`test_neutral_point_is_half`** — fully used (`u→1` via large n), no waste (`L<=n`), fresh (`A=0`), `q̂=5` ⇒ `S == 0.5` (neutral midpoint).
3. **`test_bad_below_useless_ordering_across_counts`** (§17 guarantee) — for a grid of `(high-n bad)` vs `(low-n neutral)` pairs (e.g., n_bad ∈ {5,10,20,30,40}, fixed avg 3.0; n_neut ∈ {1,2,3} avg 5.0), assert `eviction_rank_key(BAD,...) < eviction_rank_key(USELESS,...)` in EVERY combination (ordinal dominates). Include the exact counterexample from §17: BAD(n=30,avg=3,L=30) S≈0.3208 > USELESS(n=2,avg=5,L=2) S≈0.1106, yet BAD still ranks first.
4. **`test_classify_precedence_bad_over_protected`** — a young-but-proven-harmful skill (n≥5, q̂≤4.5, A<fair_window) → BAD (not PROTECTED). And brand-new (n=1) can never be BAD (fails n≥n_min).
5. **`test_useless_covers_low_and_high_count_neutral`** — (n=1,avg=5,A≥window) and (n=40,avg=5.1,A≥window) both → USELESS.
6. **`test_score_bounded_0_to_1`** — random grid over n∈[0,60], avg∈[0,10], L∈[0,80], A∈[0,1000] ⇒ `0 <= score <= 1`, `r_floor <= r <= 1`.
7. **`test_pure_no_side_effects`** — call both functions repeatedly; assert no module/global state changes (they read only args).

---

## 4. Phase C — Wire into Preview + Rebalance

**Goal:** (C-1) extend `compute_rebalance_preview` to a read-only per-skill class/score preview (side-effect-free, per the `read-only-threshold-preview-endpoint` skill); (C-2) extend `rebalance_active_skills` with OR-composition + class-ordinal eviction ordering.

### 4.1 Files to touch
- `agent_cascade/skills/manager.py` — both methods.
- `agent_cascade/api_server.py` — threshold endpoint query params (optional overrides for the new constants).
- `agent_cascade/pool/core.py` — thread kwargs (pass new constants).
- `tests/test_skills_system.py` — preview-parity + ordering tests.

### 4.2 C-1: `compute_rebalance_preview` (manager.py:648-693) — stay side-effect-free
Add a per-skill breakdown to the return dict. **Do NOT** run `_migrate_metrics_to_v13` (keep skipping it). Read the global counter alongside the snapshot:
```python
# after `with self._metrics_lock: metrics_snap = deepcopy(self._metrics)`, also capture:
with self._metrics_lock:
    global_turns_snap = self._global_activity_turns   # Phase A attribute
# ...existing math unchanged...
# NEW: per-skill class/score over the snapshot (pure functions, research §5/§7):
now = time.time()
rows = []
for nm, m in metrics_snap.items():
    if not isinstance(m, dict) or m.get('status') != 'active':
        continue
    r_ = (m.get('ratings') or {})
    n = r_.get('count', 0); avg = (r_['sum']/n) if n else None; L = m.get('total_loads', 0)
    A = self._activity_age(m, global_turns_snap, now)          # Phase A helper
    sc = skill_score(n, avg, L, A, q0=..., kq=..., n_half=..., g_half=..., r_floor=..., tau_turns=...)
    cls = skill_classify(n, avg, A, q0=..., kq=..., dq=..., n_min=..., fair_window_turns=...)
    rows.append({'name': nm, 'class': cls, 'score': round(sc['score'],4),
                 'S': round(sc['S'],4), 'qhat': round(sc['qhat'],3), 'n': n, 'L': L, 'A': A})
rows.sort(key=lambda x: (eviction_rank_key(x['class'], x['score'], x['name'])[0], x['score']))
result.update(skills=rows,
              would_evict_absolute=[x['name'] for x in rows if x['class'] in ('BAD','USELESS')],
              would_evict_cap=_cap_budget_names(rows, evict_threshold, active_count))  # see C-2 helper
```
The new constants (`q0,kq,n_half,g_half,r_floor,tau_turns,dq,n_min,fair_window_turns`) come from `agent_pool.llm_cfg` with the same defaults as pool/core.py (mirror how the endpoint reads k/min_cap/max_cap). **Clamp any query-param overrides identically to the config handlers** (see §5.4) so a displayed number always matches a real save+pass.

### 4.3 C-2: `rebalance_active_skills` (manager.py:544-646) — OR-composition + class-ordinal
Replace step (c)/(d) ordering. Keep steps (0),(a),(b) unchanged (migration, snapshot, cap math). Insert after the snapshot:
```python
# (c') classify every active skill (pure, over snapshot). Capture global counter in step (a).
now = time.time()
info = {}
for nm in active_names:
    m = metrics_snap[nm]; r_ = (m.get('ratings') or {})
    n = r_.get('count', 0); avg = (r_['sum']/n) if n else None; L = m.get('total_loads', 0)
    A = self._activity_age(m, global_turns_snap, now)
    info[nm] = {'class': skill_classify(n, avg, A, ...), 'score': skill_score(n, avg, L, A, ...)['score']}

# PROTECTED is immune to ALL eviction (cap + absolute) — excluded from the candidate set (§9/§17).
candidates = [nm for nm in active_names if info[nm]['class'] != CLASS_PROTECTED]

# (b') OR-composition:
abs_evict = {nm for nm in candidates if info[nm]['class'] in (CLASS_BAD, CLASS_USELESS)}  # (b) absolute gate
cap_target = max(0, active_before - evict_threshold)                                     # (a) cap budget
ordered_cands = sorted(candidates, key=lambda nm: eviction_rank_key(info[nm]['class'], info[nm]['score'], nm))
cap_evict = set(ordered_cands[:cap_target])                                              # cap draws worst-first from candidates
to_evict_list = sorted(abs_evict | cap_evict, key=lambda nm: eviction_rank_key(info[nm]['class'], info[nm]['score'], nm))

# (b'') safety cap (D-SAFE): never evict more than max_evictions_per_pass in one pass.
max_ev = int((self.llm_cfg or {}).get('skill_max_evictions_per_pass', 25) if hasattr(self,'llm_cfg') else 25)
to_evict_list = to_evict_list[:max(0, max_ev)]
to_evict = set(to_evict_list)
```
Then step (d) applies `to_evict` exactly as today (nested locks + 1 flush + invalidate + rescan), and the re-enable logic (manager.py:605-613) is **unchanged** (it raises inactive servable skills toward `reenable_target`; PROTECTED-active skills already count toward the active total). Log each evicted skill with its class (extend the E2 audit log at L640-643 to include `(class)`).

> **`llm_cfg` access note:** `rebalance_active_skills` is a `SkillManager` method and does not currently hold `llm_cfg`. The pool passes constants via the thread kwargs (pool/core.py:241-243). Extend that call to pass the new scoring constants + `max_evictions_per_pass` as kwargs (defaulted), mirroring the existing `k/min_cap/max_cap` pattern — do NOT reach into `self.llm_cfg` from the manager (it has none).

### 4.4 What stays unchanged
- Steps (0) migration, (a) snapshot, (b) cap math (`n_qualified`/`raw`/`evict_threshold`/`reenable_target`) — untouched.
- Re-enable logic (L605-613) — untouched.
- Lock order + single flush/invalidate/rescan — preserved.

### 4.5 Tests (add to `tests/test_skills_system.py`)
Extend the `_set_metrics` fixture usage; new class `TestSkillScoringRebalance`:
1. **`test_bad_evicted_under_cap_headroom`** (§9 gap fix) — corpus where cap evicts nothing (`K=1.0`, active ≤ max_cap) but one skill is BAD (n≥5, q̂≤4.5, A≥window); assert it IS evicted by the absolute gate.
2. **`test_useless_past_window_evicted_under_headroom`** — same headroom, a USELESS-past-window skill → evicted.
3. **`test_protected_never_evicted_even_over_cap`** (§17) — a PROTECTED skill (A<window) present while over cap; assert it is NOT in `to_evict` even though the cap wants to remove that many (the cap draws from non-PROTECTED candidates only).
4. **`test_bad_before_useless_ordering`** — over-cap corpus with both a high-n BAD and a low-n USELESS; assert the BAD is evicted before/alongside per `(ordinal,score)` (reproduce §17 counterexample: BAD S>USELESS S yet BAD removed first).
5. **`test_max_evictions_per_pass_caps_mass_eviction`** (D-SAFE) — 40 BAD skills, `max_evictions_per_pass=10`; assert exactly 10 evicted in one pass.
6. **`test_rebalance_never_raises_with_scoring`** — force a scoring exception path; assert summary returned, no raise (extends existing `test_rebalance_never_raises`).
7. **Preview parity + side-effect-freedom** (mirror `test_compute_rebalance_preview_matches_and_is_side_effect_free` L1856):
   - **`test_preview_per_skill_matches_real_pass`** — run preview and a real pass on identical fresh managers; assert the preview's `would_evict_absolute ∪ would_evict_cap` equals the real pass's evicted set, and per-skill class/score match.
   - **`test_preview_is_side_effect_free`** — deepcopy-compare `_metrics`, `_disabled_names`, and the metrics file before/after a preview call; assert no change (no migration ran, no flush).

---

## 5. Phase D — Soft-Eviction Loadability Prerequisite

**Goal:** evicted (registry-removed) skills stay loadable via `load_skill` and discoverable via `scan_skills:all`, so "evict" = soft inactivation, not deletion (research §16).

### 5.1 Files to touch
- `agent_cascade/skills/manager.py` — `load_full_instructions` + a path-resolution helper.
- `tests/test_skills_system.py` — fallback tests.

> Note: `scan_skills` already marks inactive skills `(inactive)` via `get_inactive_names()` (scan_skills.py:102,113) when `all=true`, and `_servable_skill_names`/`get_all_metadata` already surface them. So the discoverability half is largely present; the **loadability** half (`load_full_instructions`) is the real gap. Confirm during implementation whether `get_all_metadata` includes inactive skills for the `all=true` listing (it reads the registry; evicted skills are dropped from the registry by `_ensure_discovered`) — if so, extend `get_all_metadata` to also enumerate disk-servable names not in the registry (marked inactive). Flag as **Open Decision OD-2** if the scope is unclear.

### 5.2 Changes (concrete)
**(a) New helper `_find_servable_skill_path(self, name: str) -> Optional[Path]`** (near `_servable_skill_names`, ~L361): walk `self._skill_paths` for `<root>/<name>/SKILL.md` (exact + case-insensitive), return the first match or None. (`_servable_skill_names` returns names only — no paths — so this walk is required.)

**(b) `load_full_instructions` (manager.py:1085-1146):** in the `if reg is None:` branch (L1114-1117), before returning None, fall back to disk:
```python
if reg is None:
    # Soft-eviction fallback (research §16): evicted skills are absent from the registry but
    # still on disk at a servable location — load them so they remain usable + re-enable-able.
    sp = self._find_servable_skill_path(skill_name)
    if sp is not None:
        try:
            parsed = parse_skill_file(sp)
            body = parsed.get('body', '')
            version = parsed.get('version', '1.0.0')
            if count_load:
                self._increment_load_count(skill_name, version)
            return body or None
        except (FileNotFoundError, OSError):
            pass
    logger.debug("[SKILLS] load_full_instructions: skill '%s' not in registry or on disk", skill_name)
    return None
```
Keep the existing registry path (exact → case-insensitive → parsed-data → disk re-read) untouched; the fallback only fires when the registry lookup fails.

### 5.3 What stays unchanged
- All registry-resolution logic, `_resolve_skill_names`, `resolve_load_skill*` — untouched.
- The `count_load` semantics — preserved (fallback counts a real load).

### 5.4 Tests (add to `tests/test_skills_system.py`)
New class `TestSoftEvictionLoadability`:
1. **`test_evicted_skill_still_loadable_from_disk`** — register + discover a skill, disable it (`disable_skill` → removed from registry on next `_ensure_discovered`), then `load_full_instructions(name)` returns the body (not None).
2. **`test_evicted_skill_counts_a_load`** — after loading an evicted skill with `count_load=True`, assert its `total_loads` incremented.
3. **`test_unknown_skill_still_returns_none`** — a name that is neither in registry nor on disk → None (no regression).
4. **`test_scan_all_lists_evicted_as_inactive`** — with `all=true`, an evicted skill appears marked `(inactive)` (verifies the discoverability half; adjust if OD-2 changes scope).

---

## 6. Phase E — Settings Exposure + UI Preview Row

**Goal:** every formula/gate constant becomes a named setting through the **full 6-seam pattern** (mirror `SKILL_ACTIVE_TARGET_K/MIN_CAP/MAX_CAP`), plus a UI preview row showing each skill's class/score. Load `fullstack-data-feature-plumbing` — no seam may be silently missed.

### 6.1 The settings to add (research §18 + D-SAFE)

| Setting | Default | Clamp (config handler + preview endpoint, IDENTICAL) | int? |
|---|---|---|---|
| `skill_score_q0` | 5.0 | [0,10] | no |
| `skill_score_kq` | 5 | [0,20] | yes |
| `skill_score_nhalf` | 8 | [1,50] | no (float ok) |
| `skill_score_ghalf` | 20 | [1,100] | no |
| `skill_score_rflood` | 0.5 | [0,0.99] (must stay <1) | no |
| `skill_score_tau_turns` | 200 | [10,5000] | yes |
| `skill_score_dq` | 0.5 | [0.1,3.0] | no |
| `skill_score_nmin` | 5 | [1,20] | yes |
| `skill_fair_window_turns` | 50 | [0,1000] | yes |
| `skill_max_evictions_per_pass` (D-SAFE) | 25 | [0,1000] | yes |
| `skill_activity_clock` | `activity` | enum {activity, wallclock} — **internal**, see OD-3 | no |

> Naming: use lowercase snake_case keys in `llm_cfg`/UI (matching the existing `skill_active_target_k` convention), and uppercase `SKILL_*` constants in settings.py. The r_floor key is `skill_score_rflood` (avoid the typo-prone "rfloor"/"rfloord").

### 6.2 The six seams (per setting; edit ALL with IDENTICAL names)
1. **`agent_cascade/settings.py`** — add the `SKILL_*` constants (near L545-547), each `float/int(os.getenv('AGENT_CASCADE_<NAME>', '<default>'))`. Also `SKILL_WALLCLOCK_SECONDS_PER_TURN` (internal, Phase A).
2. **`agent_cascade/config_handlers.py`** — (a) add each key to the persistence list at L84-90; (b) add a `@register_config_handler('<key>')` fn per setting with the clamp above + sane default (mirror L720-753; int settings use `int()` in try/except; `skill_score_rflood` clamps to `<1`; `skill_activity_clock` validates against the enum).
3. **`agent_cascade/pool/config_persist.py`** — add each key to the save list AND the restore `data.pop(...)` block (one line each). *(Verify exact file/lines during implementation — this is the pool persistence seam; grep for where `skill_active_target_k` is persisted/restored.)*
4. **`agent_cascade/api_server.py`** — add each key to `initial_llm_cfg` seed (L1563-1567) with defaults.
5. **`web_ui/app.js`** — FOUR sites: (a) settings registry (L209-213 pattern); (b) `saveSettings` (L1197-1200 pattern); (c) restore block (L1409-1414 pattern, `_isFiniteRestore` guard for numerics); (d) `getGenerateCfg` (L5721-5722 pattern with the JS clamp mirroring the handler). All four use the same DOM id + key.
6. **`web_ui/index.html`** — add each `<input>` with a matching `id` (mirror L764-778; number inputs get `min/max/step/value`).

**Post-edit verification:** grep each extension for the NEW key (should appear in every seam) — and note that a combined multi-extension grep glob is unreliable here; grep `.py`, `.js`, `.html` **separately**.

### 6.3 pool/core.py thread kwargs
Extend the rebalance thread (pool/core.py:238-243) to read + pass the new constants from `llm_cfg` (mirror the existing `_k/_min_cap/_max_cap` reads at L238-240), and extend `rebalance_active_skills`/`compute_rebalance_preview` signatures with matching defaulted kwargs.

### 6.4 API threshold endpoint — query-param overrides
Extend `/api/skills/threshold` (api_server.py:1011-1063) to accept optional `?q0=&kq=&nhalf=&ghalf=&rflood=&tau_turns=&dq=&nmin=&fair_window_turns=` overrides, parsed defensively (only if present + parseable) and **clamped identically to the config handlers** (copy each `min(max(lo,v),hi)` verbatim). This keeps the "displayed number always matches a real save+pass" guarantee.

### 6.5 UI preview row
- Add a muted `<div id="skill-score-preview">` in the skill settings section (panel's secondary-text styling). **Display-only** — no input/button.
- In app.js, a `refreshSkillScorePreview()` that does a plain `fetch('/api/skills/threshold?' + qs)` built from the current input values; render each skill's `name — class (score)` on success, muted "n/a" on any failure/503 (never throw). Call once on init + on `input` of each relevant field with a ~300ms debounce (reuse an existing debounce helper if present).

### 6.6 Tests
- One test per new config handler (happy + out-of-range clamp edge), e.g., `test_skill_score_rflood_clamped_below_one`, `test_skill_max_evictions_per_pass_clamp`.
- A 6-seam wiring smoke test: assert each new key appears in the persistence list, has a registered handler, is in `initial_llm_cfg`, and (for JS) the registry/save/restore/getGenerateCfg all reference the same key. *(JS-side can be a manual grep checklist if no JS test harness exists — flag as OD-4.)*

---

## 7. Rollback / Safety (per phase independently safe to ship)

| Phase | Why it's safe to ship alone | Rollback |
|---|---|---|
| **A** | Purely additive durable counter; if never bumped, `_activity_age` returns 0 → all PROTECTED → absolute gate evicts nothing (conservative). No behavior change until C reads it. | Stop calling `bump_activity_turn()` (engine hook) — clock freezes at safe "all-protected" state. |
| **B** | New pure module, zero callers until C. Cannot affect runtime. | Delete module / remove imports. |
| **C** | Gated by existing `skill_auto_invalidate_enabled` master switch (pool/core.py:237) — set it False and the whole rebalance pass (cap + absolute) is a no-op. D-SAFE `skill_max_evictions_per_pass` caps mass-eviction even if enabled. OR-composition only ADDS evictions the cap would otherwise miss; with all scoring constants at defaults and a freshly-seeded clock (D-SEED), first-pass evictions are bounded + PROTECTED-shielded. | Set `skill_auto_invalidate_enabled=False` (full off) or `skill_max_evictions_per_pass=0` (absolute gate off, cap-only = today's behavior). |
| **D** | Only fires when the registry lookup already fails (previously returned None); strictly widens loadability, removes nothing. | Remove the fallback branch → back to registry-only. |
| **E** | Settings default to research values; UI preview is read-only display. A bad value is clamped by handlers before it reaches the pass. | Revert settings to defaults; the clamp bounds any single misconfiguration. |

**Master rollback gate:** `skill_auto_invalidate_enabled` (existing) turns off the entire rebalance pass. **Safety cap:** `skill_max_evictions_per_pass` (new, D-SAFE) bounds per-pass evictions so a bad config can't mass-evict. Together these guarantee no single misconfiguration nukes the corpus.

---

## 8. Risk Register

**Carried from research doc (§12):**
1. **Rating sparsity / self-selection (HIGH).** `n` counts only rated ("applied") uses; a rare-domain skill may be useful yet rarely rated → drifts to USELESS. Mitigated by fair-window + UNPROVEN bucket, not eliminated. *Action:* monitor real accumulation from `skills-metrics.json`; consider a floor for high-`L`/low-`n` (that's the loaded-never-rated signal).
2. **Single-rater coarse-scale noise (MEDIUM).** One model, 0.5-step scale; 5.0 is a convention. Mitigated by `δ_q=0.5` + shrinkage `K_q`; optional conservative Wilson-LB BAD gate (research §7 note 4) — see OD-5.
3. **Activity-clock availability/drift (MEDIUM).** Now addressed by Phase A's durable counter; a frozen/missed bump → A under-counts → skills stay PROTECTED longer (conservative, low risk). *New:* see R-A below.

**NEW implementation risks found by reading the code:**
- **R-A — Turn-bump coverage.** The bump lives in `engine/core.py:_consume_turn`, which fires once per fresh run/budget (`_turn_consumed` guard). If some execution paths start a user turn WITHOUT going through `_consume_turn` (e.g., async wakeups, retries that reuse budget), the counter under-counts → conservative (skills protected longer), never over-counts. *Mitigation:* the safe failure mode is "protected too long", not "evicted too soon"; verify no path can double-bump (the `_turn_consumed` guard prevents per-run double counting).
- **R-B — Per-turn I/O cost.** `bump_activity_turn` increments in-memory under `_metrics_lock` and only flushes on the batched cadence (threshold 5 / interval 30s) — same as existing load/rating writes. User turns are more frequent than loads, so flushes may trigger somewhat more often, but each is an atomic best-effort write already on the hot path. *No new hot-path I/O introduced.*
- **R-C — Lock ordering.** All new mutations stay under `_metrics_lock` (counter + `last_activity_turn`) or the established `_write_lock -> _metrics_lock` nesting (D-SEED in migration). No new lock, no reversed order. The engine bump only takes `_metrics_lock`. *No deadlock surface added.*
- **R-D — `llm_cfg` not on the manager.** `rebalance_active_skills`/`compute_rebalance_preview` are `SkillManager` methods with no `llm_cfg`; constants must be passed as kwargs from the pool thread (pool/core.py:241-243) and the API endpoint. Reaching into `self.llm_cfg` from the manager would be a bug. *Mitigation:* spec'd in §4.3/§6.3 to mirror the existing k/min_cap/max_cap kwarg pattern.
- **R-E — Preview/real-pass divergence.** If the preview's clamp or constant source drifts from the config handlers, the "always matches" guarantee breaks (the #1 silent failure per the preview-endpoint skill). *Mitigation:* copy each clamp verbatim; read defaults from the same `llm_cfg` keys; test with out-of-range query params (`kq=99→20`, `rflood=-3→0`, `fair_window<0→0`).
- **R-F — D-ACT helpful-but-unrated aging.** A genuinely helpful skill the agent loads but never rates will age out on the activity clock (consistent with `n=ratings.count` semantics, research §12 Risk #4). Accepted tradeoff; `g_half=20` keeps the waste penalty loose. Flagged for OD-6.

---

## 9. Open Decisions (need supervisor sign-off before build)

- **OD-1 — Wall-clock fallback rate.** The exact `SKILL_WALLCLOCK_SECONDS_PER_TURN` conversion (e.g., ~300s/turn ≈ 48/day), OR should the fallback simply be "counter unavailable ⇒ A=0 ⇒ all PROTECTED" (safest, no constant needed)? *Recommendation:* default to the safe "all-PROTECTED when counter is 0" and treat wall-clock aging as an optional manual override (`skill_activity_clock=wallclock`) with a documented rate.
- **OD-2 — `scan_skills:all` scope for evicted skills.** Does `get_all_metadata` (registry-backed) already include inactive/evicted skills for the `all=true` listing, or must it be extended to enumerate disk-servable names not in the registry? Confirm during Phase D; if extension is needed, scope it there.
- **OD-3 — Expose `skill_activity_clock` in the UI?** Research §18 marks it an *internal* last-resort override (enum {activity, wallclock}), not a primary user knob. Recommend: wire it through config-handler + API seed only (no index.html input) unless the supervisor wants a visible toggle.
- **OD-4 — JS test coverage.** Is there a JS test harness for app.js? If not, the 6-seam frontend wiring is verified by a manual grep checklist (new key present in all 4 app.js sites + index.html) rather than automated tests.
- **OD-5 — Conservative BAD gate (Wilson lower bound).** Research §7 note 4 / §14 B′: evict as BAD only when a credible/Wilson *lower bound* on quality is also ≤ `q0 − δ_q`. Off by default in the research doc. Decide on/off based on acceptable false-eviction risk.
- **OD-6 — D-ACT reset semantics confirmation.** Confirm "reset `last_activity_turn` on rating only (not load)" (this plan's choice) vs "also on load". The former makes loaded-never-rated age out (§8 case 4) but also ages out helpful-but-unrated skills (R-F). This is the single most behavior-relevant open point.
- **OD-7 — Re-enable interplay for promoted-to-USEFUL skills.** When a USELESS-retired skill later accrues good ratings, does it auto-reactivate? Currently re-enable is cap-driven; an absolute "promoted to USEFUL" trigger could feed it. Out of scope here (research §19 #3) but a natural follow-on — decide whether to include in this feature or defer.

---

## 10. Deliverable Note
This document is the plan only — **no implementation**. A coder can build phases A→E directly from it without further design decisions; anything genuinely ambiguous is listed under §9 (Open Decisions), not guessed. After implementation, delegate to an independent reviewer to verify the formula/edge-case claims (research §8) and the preview-parity guarantee against the code.
