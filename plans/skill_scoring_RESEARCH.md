# Skill Popularity/Usefulness Scoring — Research & Design (RESEARCH)

**Status:** RESEARCH / DESIGN ONLY — no implementation. This document proposes the scoring
function + classification threshold that would underpin an absolute "used vs useless" gate for
skill-invalidation. A separate implementation step consumes it.

**Author:** researcher (skill_scoring_research) · **Date:** 2026-09-20
**Verified baseline:** `HEAD = bb5b6e72` (v0.2.7). Parent skill work: Phase 1 `a0d1a43f`, Phase 2
(count-cap rebalance) committed + reviewer-PASS; see `.agent_lessons/skill-invalidation-phase2-implementation.md`.

**Companion docs:** `plans/skill_invalidation_BRAINSTORM.md`, `plans/skill_invalidation_PLAN.md`,
`.agent_lessons/skill-system-architecture-map.md`.

---

## 1. Executive Summary

- **Recommended function (one line):** a single bounded score in **[0, 1]** built from four named
  factors — shrinkage quality × saturating usage × load-waste penalty × bounded recency:

  ```
  score = u(n) · [ q̂(n,avg) / 10 ] · w(L,n) · r(A)
    q̂ = (n·avg + K_q·5.0) / (n + K_q)        # shrinkage quality estimate in [0,10], baseline 5.0
    u = 1 − exp(−n / n_half)                  # saturating usage/popularity in [0,1]
    w = exp(−max(0, L−n) / g_half)            # load-waste penalty in [0,1]
    r = r_floor + (1−r_floor)·exp(−A / τ_turns) # activity-recency in [r_floor, 1] (tiebreaker; A=activity-age)
  ```

  with defaults **`q0=5.0, K_q=5, n_half=8, g_half=20, r_floor=0.5, τ_turns=200`**.

- **Recommended thresholds** (the classification is *quality-gated*, not a single raw-score cut —
  see §7 for why a lone scalar cannot separate all three classes). Precedence: **BAD > PROTECTED >
  {USEFUL, USELESS} > UNPROVEN**:
  - **BAD** (evict/inactivate): `q̂ ≤ 4.5 AND n ≥ 5` → clearly harmful + confident; evicted even if young.
  - **PROTECTED** (no action): `activity_age < fair_window_turns = 50` → fewer than N user-turns of *actual
  system activity* since its last chance (§15); an idle break contributes zero aging. Shields the unproven from
  retirement; does NOT shield proven-harmful BAD skills.
  - **USEFUL** (keep, rank by score): `q̂ ≥ 5.5 AND n ≥ 5` → quality above baseline + meaningfully used.
  - **USELESS** (retire, low-risk): `|q̂ − 5.0| ≤ 0.5` and NOT protected → neutral quality, not earning its keep.
  - Everything else → **UNPROVEN / monitor** (default keep; exploration).

- **Ordering guarantee (Refinement 3, §17):** eviction is ordered by an explicit **class-priority rank key**
  `(class_ordinal, score)` with `BAD(0) < USELESS(1) < UNPROVEN(2) < USEFUL(3)`, so a BAD skill always evicts before
  any USELESS one — guaranteed for all `n`. The raw scalar alone does NOT guarantee this (§17 proves the counterexample).

- **Soft eviction (Refinement 2, §16):** eviction is SOFT — an evicted skill is not deleted and must stay loadable via
  `load_skill` and discoverable via `scan_skills:all`. Today this is broken for evicted skills (registry-only
  resolution); §16 specifies the small prerequisite fix.

- **All constants are named settings (Refinement 4, §18):** every formula/gate constant becomes a user-tweakable setting
  mirroring `SKILL_ACTIVE_TARGET_K/MIN_CAP/MAX_CAP` — no magic numbers.

- **Composition with the existing count-cap (the load-bearing decision, §9):** **OR-composition.**
  A skill is evicted if it is (a) selected by the relative count-cap budget **OR** (b) classified
  BAD or USELESS-past-window. So **yes — a BAD/USELESS skill is removed even when the active count
  is below `evict_threshold`.** This directly fixes the stated gap (K=1.0 → nothing evicted → but
  useless/bad skills are still removed by the absolute gate).

- **ELO / Bradley-Terry verdict: MISMATCH.** ELO/BT require pairwise head-to-head outcomes, which we
  do not have (only absolute 0–10 ratings of one skill at a time). The closest valid construction —
  rating each skill against a *fixed 5.0 baseline reference* — degenerates into the shrinkage/
  empirical-Bayes mean above (§4.4), so ELO adds no value and is the wrong framing for this data; the
  shrinkage estimator is the correct object.

- **Top risks / assumptions that could invalidate the design (§12):**
  1. **Rating sparsity / self-selection** — `ratings.count` only counts skills the agent *chose* to
     rate ("actually applied"). A skill relevant to a rare domain may be genuinely useful yet rarely
     rated → misclassified USELESS. Mitigated by the fair-window + UNPROVEN bucket, but not eliminated.
  2. **Single-rater bias** — all ratings come from one model's honest-but-noisy judgment on a coarse
     0.5-step scale; the 5.0 baseline is a convention, not a measured ground truth. δ_q and K_q are
     the knobs that absorb this noise.
   3. **Activity-clock availability (§15)** — the PROTECTED gate and recency now key off user-turns of
      activity, not wall-clock, so idle breaks no longer age skills out (resolving the old "wall-clock vs
      opportunity" concern). Requires a persisted cumulative turn counter; falls back to wall-clock if
      unavailable (§12 Risk #3).

---

## 2. Verified Data & Current System (grounding)

Every input below was verified against the live tree at `HEAD=bb5b6e72` (anchors in §13).

### 2.1 Per-skill metrics record (schema 1.3, `_default_metrics_entry`, manager.py:70-74 + writers)

| Field | Type | Source / writer | Meaning |
|---|---|---|---|
| `ratings.count` (`n`) | int | `_record_rating` manager.py:724-765 (`count += 1`) | **Number of times the skill was rated = deliberate-use count.** The most important signal. |
| `ratings.sum / n` → `avg` | float [0,10] | same; validated at manager.py:773 (`0 ≤ rating ≤ 10`) | Average rating. **5.0 = no-signal baseline** (new skills auto-init here). |
| `total_loads` (`L`) | int | `_increment_load_count` manager.py:695-722 | Discovery/injection count. Less important than `n`. |
| `last_used` | ISO-8601 UTC / absent | set in `_increment_load_count` (manager.py:706) | Time of last load. Minor tiebreaker. |
| `status` | 'active'\|'inactive' | rebalance / toggles | Durable active flag (derived from metrics, not registry). |
| `ratings_by_version` | dict | `_record_rating` (schema 1.2) | Per-version rating history (not used by this design). |

**Rating semantics (verified, `prompts/dna.py:188-197`):** the reflection prompt instructs the agent
to rate **only skills it "ACTUALLY applied"**; "loaded but barely needed / ignored → no rating /
skip"; "actively misleading or harmful → very low (0–4)." Therefore:
- `n = ratings.count` counts **deliberate, applied uses** — not loads.
- The gap **`g = max(0, L − n)` = loads where the skill was injected but never meaningfully rated =
  wasted tokens.** This is a genuine **negative signal** (confirmed by prompt design).
- **5.0 baseline:** `SKILL_RATING_INITIAL = 5.0` (settings.py:540-541), auto-recorded at registration
  (manager.py:1443). So a brand-new skill is exactly `(n=1, avg=5.0, L≥1)` — the canonical "no real
  signal yet" state. Above 5 = useful; below 5 = hindering.

### 2.2 Verified constants (settings.py)

| Constant | Default | Role |
|---|---|---|
| `SKILL_ACTIVE_TARGET_K` | **1.0** | count-cap multiplier (`raw = round(K·N_qualified)`) |
| `SKILL_ACTIVE_MIN_CAP` | **20** | eviction floor (lower bound on eviction, not force-enable) |
| `SKILL_ACTIVE_MAX_CAP` | **200** | eviction ceiling |
| `SKILL_RATING_INITIAL` | **5.0** | new-skill baseline rating |
| `CANDIDATE_MIN_RATINGS` | **5** | min ratings before a candidate's average is trusted (candidate gate) |

### 2.3 Current count-cap math (`rebalance_active_skills`, manager.py:544-646) — verified

```
n_qualified = #{ skills with total_loads ≥ 1 OR ratings.count ≥ 1 }   # over FULL corpus
raw           = round(K · n_qualified)
evict_threshold = max(min_cap, min(max_cap, raw))
to_evict      = lowest-ranked active skills down to evict_threshold   # ranked by _rank_key
```

`_rank_key` (manager.py:527-542) = `(rating_avg_or_-1.0, total_loads, last_used_iso, name.lower())`,
ascending = worst-first. **This is a pure RELATIVE count cap — it has no absolute usefulness notion.**

**The gap (why this work exists):** with `K=1.0` and most skills touched ≥1 time, `n_qualified ≈
total skills`; if that is ≤ `max_cap=200`, then `evict_threshold = n_qualified` → **nothing is ever
evicted.** Useless-but-once-used skills survive forever. Confirmed against the code path above.

### 2.4 The preview path the new score must slot into (`compute_rebalance_preview`, manager.py:648-693)

A read-only preview already runs the *same* count-cap math (deepcopy snapshot under `_metrics_lock`,
**skips** the one-time migration, applies nothing) and returns
`{ok, n_qualified, raw, evict_threshold, reenable_target, n_servable, active_count, k, min_cap, max_cap}`.
**Design requirement:** the new per-skill score + classification must be computable in this same
read-only path so the UI's "what would happen" preview stays consistent with a real pass (see
skill `read-only-threshold-preview-endpoint`). The score is a pure function of the snapshot fields —
no side effects, no migration — so it drops into this path cleanly.

---

## 3. Design Goals & Classification Intent

Encode the user's three classes + a recency protection:

| Class | Definition (user intent) | Operational action |
|---|---|---|
| **USEFUL** | high `n` AND good rating (>5) | keep; rank high |
| **BAD** | med-high `n` AND rating <5 (used a lot, rated poorly) | evict / inactivate |
| **USELESS** | low `n`, score ≈ 5 (baseline); worst = `(n=1, r=5)` = brand-new state | retire (low-risk) |
| *(protection)* | freshly-proposed skill must NOT be instantly classified useless | PROTECTED until fair chance elapsed |

Two structural observations that shape the math:
1. **`n` gates everything.** Low `n` → "unproven/useless-or-new" regardless of quality; high `n` →
   quality decides useful vs bad. So usage is a first-class dimension, not just a confidence weight.
2. **"Bad" and "useless" are different failure modes that both score low.** A single scalar alone
   cannot separate them (a heavily-used-but-harmful skill and a barely-used-neutral skill can have
   similar low scores). The classification therefore uses the **quality sign** (relative to 5.0) +
   **usage level**, with the scalar as the ranking/primary-useful signal. This is stated explicitly in
   §7 — it is the single most important "don't be hand-wavy" point.

---

## 4. Candidate Scoring Functions Evaluated

### 4.1 (A) Weighted multiplicative / additive score

`score = popularity(n) × rating_multiplier(avg) × (1 − waste_penalty) × recency_decay`, with each
factor bounded to [0,1] or a multiplier band.

- **Pros:** intuitive; every factor has an obvious meaning; trivially implementable in pure Python.
- **Cons:** the "rating multiplier" mapping around the 5.0 baseline is **arbitrary** (you must invent
  a curve from [0,10]→multiplier); the small-count case is *fudged* by the popularity sigmoid rather
  than handled statistically; no principled reason for the weights. It is essentially "shrinkage with
  extra steps and no justification."

**Verdict:** rejected as the primary form, but its *factor structure* (usage × quality × waste ×
recency) is retained in the recommendation — only the quality factor is replaced by a principled
estimator (§4.2).

### 4.2 (B) Bayesian shrinkage — posterior mean  ★ RECOMMENDED ★

Treat `avg` as a noisy observation of a latent quality, with an **informative prior centered at the
5.0 baseline** and strength `K_q` (a pseudo-count). The posterior-mean / empirical-Bayes estimator is:

```
q̂ = (n·avg + K_q·q0) / (n + K_q),   q0 = 5.0
```

This is a weighted average of the observed mean and the prior mean, where the data's weight grows
with `n`. It is exactly the conjugate-posterior-mean behavior (the continuous-scale analog of the
Beta posterior mean) and has three properties the naive approaches lack:

1. **Small-count handling is principled, not fudged.** At `n=0`, `q̂ = q0` (pure prior). As `n` grows,
   `q̂ → avg`. At `n = K_q`, data and prior contribute equally. So a new skill is pulled **toward the
   neutral baseline** — neither over-penalized nor over-rewarded — which is precisely the user's
   requirement.
2. **`K_q` has an interpretable meaning** ("how many ratings before we trust the data over the prior")
   and can be anchored to an existing constant: **`K_q = 5 = CANDIDATE_MIN_RATINGS`** — the system
   already treats "5 ratings" as the bar for trusting an average in the candidate gate. Reusing it
   keeps the whole skill subsystem consistent.
3. **No binarization cliff.** §8 shows that a *binarized* Beta at threshold 5.0 swings wildly for a
   0.2-point rating change (4.9↔5.1), while continuous shrinkage is stable. Continuous uses the full
   [0,10] signal with no information loss.

**Verdict: RECOMMENDED.** It subsumes what a separate popularity sigmoid would fudge, is statistically
defensible, and reuses an existing system constant.

### 4.3 (B′) Wilson lower bound / Beta-Bernoulli posterior (conservative variant)

Two related Bayesian options worth stating honestly:

- **Beta-Bernoulli posterior mean** on a *binarized* "useful?" outcome (`rating ≥ 5` → success):
  `posterior_mean = (s + α)/(n + α + β)` with symmetric prior at 0.5. For low `n` this pulls toward
  0.5 (neutral) — good. **But** it requires choosing a binarization threshold, and that threshold is a
  **cliff**: §8 quantifies the worst case (assuming *every* rating lands on one side of 5.0 — the most
  extreme illustration). At `n=40`, an average of 4.9 vs 5.1 flips every rating's label and swings the
  estimate from ≈0.06 to ≈0.94, whereas continuous shrinkage moves only 0.491↔0.509 over the same
  swing. Real mixed distributions are less extreme, but the cliff is structural: a 0.2-point rating
  change straddles the binarization boundary. Continuous shrinkage avoids it entirely (uses the full
  [0,10] signal). So **continuous (B) > binarized Beta** for the *score*.
- **Wilson lower bound** (the classic "rank-by-quality-with-confidence" interval): it is deliberately
  **pessimistic** — designed to avoid *over-rewarding* unproven items in a leaderboard. That means it
  pulls low-count items **toward 0**, which would *penalize brand-new skills* — violating the user's
  "not over-penalized" requirement if used for the score. **However**, pessimism is exactly what you
  want for an *eviction* decision ("only call something BAD when confident"). So: **use posterior mean
  (B) for the score/ranking, and optionally a credible/Wilson lower bound as the conservative gate for
  the BAD class** (evict only when the lower bound is already below baseline). Recommended as an
  enhancement, not the base.

### 4.4 (C) ELO / Bradley-Terry — MISMATCH

ELO/BT model **pairwise outcomes** ("skill A beat skill B in the same context") and estimate a latent
strength from a graph of head-to-head results. We have **no head-to-head data** — only absolute 0–10
ratings of one skill at a time, from a single rater. There is no natural comparison graph to feed an
ELO/BT update; standard ELO (K·(outcome − expected-win)) is not even defined without an opponent's
current rating and a match outcome.

The closest valid construction — treating "the 5.0 baseline" as a *fixed reference* and updating a skill
toward its observed ratings with a prior strength `K_q` — produces the shrinkage/empirical-Bayes mean
`(n·avg + K_q·q0)/(n + K_q)`. That is an **analogy**, not an exact ELO reduction (real ELO is
nonlinear in the rating gap and needs a moving opponent); the point is that *any* legitimate way of
using ELO on this data degenerates into "compare against a fixed baseline," which is exactly what the
shrinkage estimator already does, more simply. **Verdict: mismatch — ELO/BT demand a comparison
structure we do not have; do not use them here.** The shrinkage estimator *is* the correct object.

### 4.5 (D) Thompson sampling / Beta-Bernoulli posterior — complementary, not a substitute

Thompson sampling is an **exploration-vs-exploitation decision rule for which skill to LOAD next**
(sample each skill's posterior, inject the best). It answers "what to serve," not "what to retire."
Our shrinkage score + threshold answers "what to prune." They are **complementary**: if a later phase
wants adaptive *loading* (reward side), Thompson on the Beta posterior (binarized useful) is the
natural choice. Do not conflate the two; this design is for the **pruning** side only.

---

## 5. The Recommended Function — Exact Formula & Constants

All inputs are the verified schema fields (§2.1). `n = ratings.count`, `avg = ratings.sum/n` (or `q0`
if `n=0`), `L = total_loads`. **All time-based factors now key off an *activity clock*, not wall-clock** (§15):
- `A` (drives recency `r` and the PROTECTED gate) = **activity-age in user-turns** since the skill last had a
  chance to be used = `activity_clock − skill.last_activity_turn`. An idle break adds zero turns → zero aging.
- The legacy wall-clock `last_used` is retained for display/debug only; it no longer drives eviction or recency.

```
score = u(n) · [ q̂(n,avg) / 10 ] · w(L,n) · r(A)          ∈ [0, 1]

  q̂   = (n·avg + K_q·q0) / (n + K_q)           # shrinkage quality, [0,10]; q̂=q0 when n=0
  u   = 1 − exp(−n / n_half)                   # saturating usage/popularity, [0,1]
  w   = exp(−max(0, L−n) / g_half)             # load-waste penalty, [0,1] (w=1 when L≤n)
  r   = r_floor + (1−r_floor)·exp(−A / τ_turns) # bounded activity-recency, [r_floor,1]; A = activity-age in user-turns (§15)
```

**Bounded range [0,1]; the neutral point is 0.5** — a fully-used (`u→1`), no-waste (`w=1`), fresh
(`r=1`) skill at baseline quality (`q̂=5`) scores exactly `1·0.5·1·1 = 0.5`. This **mirrors the rating
scale's own structure** (5.0 is the neutral midpoint of [0,10] ↔ 0.5 is the neutral midpoint of [0,1]).
If a [0,10] display is preferred, multiply by 10.

**Decomposition (for clarity and implementation):** the *time-stable core* usefulness score is
`S = u·(q̂/10)·w ∈ [0,1]` (neutral at 0.5); the full `score = S·r` applies bounded recency on top as a
**minor tiebreaker** (`r ∈ [0.5, 1]`, so recency can reduce — never increase — the score by at most 50%).
The classification rule (§7) keys off `q̂` and `n` (the interpretable drivers), not off the raw `score`;
`score`/`S` are used for **ranking** (eviction order under the count-cap) and UI display. §8 reports both
`S` and the recency-adjusted `score` so each number is reproducible from the formula.

### Constants — named and justified

| Constant | Default | Justification |
|---|---|---|
| `q0` | **5.0** | The no-signal baseline; new skills auto-init here (`SKILL_RATING_INITIAL`). Neutral midpoint of [0,10]. Not a free parameter — fixed by the system. |
| `K_q` | **5** | Prior strength / pseudo-count = "ratings needed before data outweighs prior." Anchored to existing `CANDIDATE_MIN_RATINGS=5` for subsystem consistency. At `n=K_q`, data and prior are equal-weighted. |
| `n_half` | **8** | Usage half-saturation: `u` reaches ≈0.63 at `n=8`. A skill reached-for ~8 times has clearly "proven" it's something the system uses; below that it's still being sampled. Drives ranking, not class (§11). |
| `g_half` | **20** | Load-waste half-scale: `w` halves when `L−n = 20`. A skill loaded 20× more than it was meaningfully used has clearly wasted budget. Loose by design — waste is a secondary signal. |
| `r_floor` | **0.5** | Recency multiplier floor → recency can reduce the score by **at most 50%**, encoding the user's "minor tiebreaker" requirement directly (a year-old skill retains ≥0.5). |
| `τ_turns` | **200 turns** | Recency decay constant in *activity* (user-turns), replacing the old 90-day wall-clock τ. With `r_floor=0.5`: `A=200→r≈0.68`, `A=400→r≈0.57`. An idle break adds 0 turns, so it never decays a skill (§15). Slow enough to stay a minor tiebreaker. |
| `δ_q` | **0.5** | Quality margin: "clearly above/below baseline" requires `q̂ ≥ 5.5` / `q̂ ≤ 4.5`. Absorbs rating-scale noise (0.5-step scale + shrinkage uncertainty at low `n`). Sets the width of the neutral/USELESS band. |
| `n_min` | **5** | "Meaningly used" bar for the USEFUL and BAD classes (= `CANDIDATE_MIN_RATINGS`). Below this, a skill is UNPROVEN (or PROTECTED if new), not confidently good/bad. |
| `fair_window_turns` | **50 turns** | Protection window in *activity*: a skill with <50 user-turns of opportunity since its last chance is PROTECTED from retirement (§15). Replaces the old 14-day wall-clock window; an idle break adds 0 turns so it never "expires" while the system is idle. |
| `T_useful` | **0.6** | Optional single-scalar "clearly useful" line on the score for UI/ranking (above the 0.5 neutral baseline by a margin). The *class* decision uses quality gates, not this cut (§7). |

---

## 6. How the Three Classes Map to the Score

Because `score` folds usage and quality together multiplicatively, the three classes occupy
**different regions for different reasons** — this is the crux of "don't be hand-wavy":

- **USEFUL** → high `u` (high `n`) **and** `q̂/10 > 0.5` (above baseline) → score pushed **above 0.5**,
  up toward ~1. (Both factors reinforce.)
- **BAD** → high `u` (med-high `n`) **but** `q̂/10 < 0.5` (below baseline) → score pulled **below 0.5**.
  It is *low*, but what makes it BAD (vs USELESS) is the **high-usage + below-baseline-quality** combo.
- **USELESS** → low `u` (low `n`) with neutral quality → score **well below 0.5** regardless of quality.

**Therefore a single raw-score threshold cannot by itself separate BAD from USELESS** (both are
"< 0.5"). The scalar is the correct signal for *ranking* and for the *USEFUL* cut; the BAD/USELESS
split requires the **quality sign** (`q̂` vs `q0`) and **usage level** (`n`). §7 gives the exact,
implementable decision rule that uses all of this.

---

## 7. The Classification Decision Rule (what actually drives eviction)

**Precedence: BAD > PROTECTED > {USEFUL, USELESS} > UNPROVEN.** The order matters and is a deliberate
design decision (see note 1 below): a *demonstrably harmful* skill is evicted even if young; the
fair-window protection shields only the *unproven* from retirement.

```
if   q̂ ≤ q0 − δ_q  and n ≥ n_min:         → BAD         (evict / inactivate — clear harm, any age)
elif A < fair_window_turns:                 → PROTECTED   (no action; <N user-turns since last chance)
elif q̂ ≥ q0 + δ_q  and n ≥ n_min:         → USEFUL      (keep; rank by score)
elif |q̂ − q0| ≤ δ_q:                       → USELESS     (retire — low-risk; neutral quality)
else:                                      → UNPROVEN    (monitor / keep; exploration)
```

(`A` = activity-age in user-turns since the skill last had a chance to be used — the same activity clock
that drives the recency factor `r`; an idle break adds zero turns, so PROTECTED never "expires" while the
system is idle. See §15 for the clock definition.)

Notes:
1. **Why BAD is checked before PROTECTED (design decision).** The fair-window's purpose is to not punish
   a skill that *hasn't had a fair chance to show value* — i.e., the USELESS/UNPROVEN "no signal yet"
   cases (the user's exact requirement: "new skills aren't instantly retired"). A skill that has already
   accumulated `n ≥ n_min = 5` ratings and is clearly below baseline (`q̂ ≤ 4.5`) **has shown its colors** —
   it is not unproven, it is *proven-harmful*, and continuing to serve it wastes tokens. So BAD eviction
   is exempt from the fair window. Note this can only trigger for skills with `n ≥ 5`; a truly brand-new
   skill (`n = 1`) can never be BAD (it fails `n ≥ n_min`), so the protection still fully covers the
   "freshly-proposed" case. **Conservative alternative (Option 1):** check PROTECTED first so *nothing*
    is evicted/retired while `A < fair_window_turns`, period. Simpler and safer, but it shields a clearly-harmful
    young skill for up to `fair_window_turns` turns (wasting tokens). The difference only matters for the narrow
   "young + n≥5 + clearly-bad" case; recommend Option 2 (default), fall back to Option 1 if false-eviction
   risk is unacceptable.
2. **USELESS covers both low-count-neutral AND high-count-neutral.** A skill used 40× but rated ~5.1
   every time is *confidently* neutral-not-good → retiring it is **low-risk** (we are sure it isn't
   hiding real value). This is the decisive answer to the "high-count near-baseline" edge case (§8).
3. **UNPROVEN** catches "clearly good/bad but `n < n_min`" and "used-a-bit, mildly off baseline."
   Shrinkage keeps its score modest, so it is neither over-ranked nor over-evicted; it simply gets more
   chances. This is the built-in exploration behavior.
4. **Optional conservative BAD gate (B′):** evict as BAD only when a credible/Wilson *lower bound* on
   quality is also ≤ `q0 − δ_q`. Recommended enhancement for high-stakes corpora; off by default.

---

## 8. Edge-Case Analysis — Worked Numbers (all computed, defaults from §5)

All values computed from the §5 formula with defaults `q0=5, K_q=5, n_half=8, g_half=20, r_floor=0.5,
τ_turns=200, δ_q=0.5, n_min=5, fair_window_turns=50`. `S = u·(q̂/10)·w` (time-stable core). **Class is
determined by `q̂`, `n`, and activity-age `A` — NOT by `r`.** The `r`/`score` columns are illustrative ranking
values only (recency is a minor tiebreaker, §5), shown assuming last-used ≈ an activity proxy; they do not
change any class below. "created / last-used" given per case for reference.

| # | Case — (n, avg, L; created, last-used) | q̂ | S | r | score=S·r | **Class** | Why |
|---|---|---|---|---|---|---|---|
| 1 | **Brand-new** (1, 5.0, 1; 0h, never) | 5.000 | 0.0588 | 1.000 | 0.0588 | **PROTECTED** | A≈0 < 50 turns → shielded from retirement ✓ (the crux requirement) |
| 2 | **Perfect but rare** (2, 10, 2; 30d, 20d) | 6.429 | 0.1422 | 0.900 | 0.1280 | **UNPROVEN/keep** | q̂ pulled down 10→6.43 (low confidence); n<5 → not yet USEFUL; kept, gets more chances ✓ |
| 3 | **Popular but bad** (30, 3.0, 30; 7d, 5d) | 3.286 | 0.3208 | 0.973 | 0.3122 | **BAD → evict** | q̂≤4.5 & n≥5 → evicted **even though created only 7d ago** (Option 2: proven-harmful not shielded) ✓ |
| 4 | **Loaded-never-rated** (0, —, 50; 30d, never) | 5.000 | 0.0000 | 0.858 | 0.0000 | **USELESS → retire** | u=0 (no deliberate use), neutral quality, not new; worst case (wasted tokens) ✓ |
| 5 | **High-count near-baseline** (40, 5.1, 40; 30d, 20d) | 5.089 | 0.5055 | 0.900 | 0.4551 | **USELESS → retire** | \|q̂−5\|=0.09≤0.5 → neutral; high count ⇒ *confidently* neutral = low-risk retirement (decisive, not a cop-out) ✓ |
| 6 | **Good but rare** (5, 8.0, 5; 30d, 20d) | 6.500 | 0.3021 | 0.900 | 0.2720 | **USEFUL (kept)** | q̂≥5.5 & n≥5; S modest (low usage) → kept & ranked mid, not evicted ✓ |
| 7 | **Clearly-bad but low-n** (3, 2.0, 3; 30d, 20d) | 3.875 | 0.1212 | 0.900 | 0.1091 | **UNPROVEN/monitor** | q̂≤4.5 but n<5 → not confidently BAD yet; monitor (exploration), not evicted ✓ |

Note case 3 vs case 7: both are below-baseline, but case 3 has `n≥5` (→ BAD, evicted) while case 7 has
`n<5` (→ UNPROVEN, kept). This is exactly the user's "med-high count" requirement for the BAD class.

**Recency tiebreaker** (identical `n=1, avg=5, L=1`, differ only in activity-age `A`; S=0.0588 for both):
- recently active (`A≈10` turns): `r = 0.5+0.5·e^(−10/200) ≈ 0.976` → score **≈0.0574**
- long-idle (`A≈400` turns): `r = 0.5+0.5·e^(−400/200) ≈ 0.568` → score **≈0.0334**

→ The recently-active one ranks higher (tiebreaker works) but the difference is *minor* (recency floored at
0.5), matching "activity is a minor tiebreaker." Both remain low-scored USELESS-candidates once past the fair
window — recency does not rescue a genuinely-unused skill, it only orders them. **Crucially, an idle break adds
zero turns, so `A` (and `r`) are frozen during idle time** — see §15 for the 3-week-break worked example.

**Fair-window protection for the canonical useless baseline `(n=1, r=5)` (activity clock):**

| activity-age A | class |
|---|---|
| ~0 / 20 turns | **PROTECTED** (new) |
| 50 turns + ε | **USELESS** (fair chance elapsed) |
| 500 turns | **USELESS** |

→ The *same* `(n=1, q̂=5)` state is **protected when fresh** and **useless once past the window.** This
is precisely the behavior the user required: a freshly-proposed skill (which is literally this state)
is not instantly retired.

---

## 9. Composition with the Existing Count-Cap (the load-bearing decision)

**Question (raised by supervisor):** *does an evict-as-BAD skill get removed even when the active count
is below `evict_threshold`?* **Answer: YES — recommended OR-composition.**

- The **count-cap** governs the **size/budget** of the active set (how many may be active) — a relative
  ceiling. Preserve it as-is.
- The **score/threshold** governs an **absolute quality gate** per skill, independent of cap headroom.
- **Eviction rule:** evict a skill if **(a)** the count-cap budget selects it (existing behavior; when the cap
  must remove skills they are ordered by the **class-priority rank key** `(class_ordinal, score)` — §17 — so BAD
   evicts before USELESS before UNPROVEN) **OR (b)** it is classified **BAD** or **USELESS-past-window**.
- **PROTECTED exemption:** a skill with `A < fair_window_turns` that is not BAD is PROTECTED and is exempt from both
  (a) and (b) — the fair window shields it from *all* eviction until it expires (§17).

Consequences:
- Fixes the stated gap: with `K=1.0`, the cap evicts nothing, but BAD/USELESS skills are still removed
  by the absolute gate → "useless-but-once-used" no longer survives forever.
- The cap can still evict a *neutral* skill purely to enforce budget (existing behavior preserved).
- **Alternative (not recommended):** score-only (cap as pure ceiling, never an eviction trigger).
  Simpler, but then a corpus could hold many neutral skills up to `max_cap` with no quality pressure —
  weaker than OR. OR-composition strictly dominates: it keeps the cap's budget safety net *and* adds
  the absolute gate.

**Preview consistency:** the per-skill score + class is a pure function of the snapshot, so it slots into
`compute_rebalance_preview` (manager.py:648-693) verbatim — the UI "what would happen" preview will show
exactly what a real pass evicts (cap ∪ absolute gate), satisfying `read-only-threshold-preview-endpoint`.

---

## 10. The "Fair Chance" Window — Proposed Constant & Justification

**Proposed: `fair_window_turns = 50` user-turns of activity** before a skill can be classified USELESS/evicted.
A skill is PROTECTED while its activity-age `A < fair_window_turns` (§15). This replaces the earlier wall-clock
"14 days" proposal — see §15 for why an activity clock is strictly better (idle breaks no longer age a skill out).

Justification:
- A skill is discovered per-session by relevance matching and rated at task-end *only if actually applied*.
  The right unit of "fair opportunity" is therefore **how many chances the system has had to use it** — i.e.,
  user-turns of activity — not elapsed calendar time.
- `50` turns ≈ a few days of normal interactive use (a handful of sessions). Short enough that a genuinely-useless
  skill does not linger, yet long enough to absorb project-domain skew: a relevant skill in a rarely-touched domain
  still gets its chance once the domain is active again — because idle time adds zero turns (§15).
- Because the gate is activity-based it **directly subsumes** the earlier "opportunity, not wall-clock" concern:
  a skill old-by-calendar but in a rarely-fired domain has a *small* `A` and stays PROTECTED until it has actually
  been given ~50 chances. No separate `total_loads < L_probe` proxy is needed (it was the fallback for the
  wall-clock design; now redundant).

Sensitivity / alternatives:
- **25 turns** — fast-moving deployments; risks retiring a useful-but-slow-to-surface skill.
- **100 turns** — slow/seasonal projects; lets useless skills linger longer (more wasted tokens).
- The activity clock makes the window *self-calibrating* to deployment intensity: busy systems age skills out
  faster (in wall-clock terms), quiet systems slower — exactly matching "opportunity." Recommend confirming the
  typical turns-to-first-rating from `skills-metrics.json` telemetry before locking the default.

---

## 11. Parameter Sensitivity (±20% on the key constants)

Re-classified five non-new probe skills under ±20% perturbations of each constant:

| Config | new(n=1,r=5) | bad(30,r=3) | nearbase(40,r=5.1) | goodrare(5,r=8) | clearly-bad-low-n(3,r=2) |
|---|---|---|---|---|---|
| **baseline** (K_q=5, δ_q=0.5) | USELESS* | BAD | USELESS | USEFUL | UNPROVEN |
| K_q +20% (6.0) | USELESS* | BAD | USELESS | USEFUL | UNPROVEN |
| K_q −20% (4.0) | USELESS* | BAD | USELESS | USEFUL | UNPROVEN |
| δ_q +20% (0.6) | USELESS* | BAD | USELESS | USEFUL | UNPROVEN |
| δ_q −20% (0.4) | USELESS* | BAD | USELESS | USEFUL | UNPROVEN |

\* shown without the protection gate (30d-old probe); a truly-new skill is PROTECTED regardless.

**Finding: the classification is ROBUST — no probe flips under ±20% on `K_q` or `δ_q`.** This is a
strength, not a weakness: the three-way split is driven by the *quality sign relative to baseline* and
the *count bands*, which are stable; the constants have slack. What each constant actually controls:
- **`δ_q`** — width of the neutral/USELESS band (how close to 5.0 counts as "neutral" vs "good/bad").
  The most behavior-relevant knob; 0.5 is a sensible default given the 0.5-step rating scale.
- **`K_q`** — how fast `q̂` trusts data over prior (confidence ramp). Anchored to `CANDIDATE_MIN_RATINGS`.
- **`n_half`** — shape of the usage factor → affects **score/ranking**, not class membership.
- **`τ_turns`, `r_floor`** — activity-recency tiebreaker strength; floored so it stays minor by construction.
- **`fair_window_turns`** — protection duration in activity (the main operational lever; see §10, §15).

---

## 12. Risks / Assumptions That Could Invalidate the Design

1. **Rating sparsity & self-selection (HIGH).** `ratings.count` counts only skills the agent *chose* to
   rate ("actually applied"). A skill relevant to a rare domain may be genuinely useful yet rarely
   rated → its `n` stays low and it drifts toward USELESS. The fair-window + UNPROVEN bucket delay but do
   not eliminate this. *Mitigation:* the load-count-based opportunity gate (§10); monitor real
   accumulation rates; consider a floor that never retires a skill whose `total_loads` is high but
   `n` is low for domain reasons (that's actually the "loaded-never-rated" signal — see #2).
2. **Single-rater, coarse-scale noise (MEDIUM).** All ratings come from one model's judgment on a 0.5-step
   scale; the 5.0 baseline is a *convention*, not measured ground truth. Mis-rating near 5.0 is common.
   *Mitigation:* `δ_q=0.5` margin + shrinkage (`K_q`) absorb this; optionally the conservative Wilson-LB
   BAD gate (§7) before evicting.
3. **Activity-clock availability & drift (MEDIUM).** The activity clock (§15) needs a *persisted* cumulative
   turn counter; telemetry's `total_user_turns` is per-session (`_session_stats`, telemetry.py:72/330), so it must
   be accumulated across sessions and bumped on every user turn. If updates are missed, `A` under-counts → skills
   stay PROTECTED longer (conservative, low risk); a *frozen* counter would freeze aging entirely. *Mitigation:*
   persist the counter durably; fall back to wall-clock if unavailable. This replaces the old "wall-clock vs
   opportunity" risk, which the activity clock resolves by construction (§10).
4. **`total_loads − n` gap conflation (LOW).** The waste penalty treats all un-rated loads as "wasted,"
   but a skill can be loaded and genuinely helpful yet go un-rated if the agent skips the reflection
   step. *Mitigation:* `g_half=20` keeps the penalty loose/secondary; it discounts, never zeroes, score.

**Assumptions (stated explicitly):** ratings are honest-ish and roughly exchangeable per skill; the 5.0
baseline is a reasonable neutral prior; activity-turns (§15) is a valid proxy for "opportunity";
the corpus is large enough that an absolute gate won't starve it (the count-cap's `min_cap` floor still
applies as a safety net).

---

## 13. Verified Anchors Table (spot-check list for the implementer)

All re-verified against the live tree at `HEAD=bb5b6e72` this session (read, not just grep):

| Claim in this doc | Location | Verified? |
|---|---|---|
| `_default_metrics_entry` schema 1.3 shape `{total_loads, by_version, status}` | manager.py:70-74 | ✓ |
| `ratings = {count,sum,latest,last_version}`, `count += 1`, `sum += rating` | manager.py:724-765 | ✓ |
| rating validated `0 ≤ r ≤ 10` | manager.py:773 | ✓ |
| `total_loads += 1` and `last_used = now` on load | manager.py:695-722 (esp. 702, 706) | ✓ |
| `_rank_key = (rating_avg_or_-1.0, total_loads, last_used_iso, name)` ascending=worst-first | manager.py:527-542 | ✓ |
| count-cap math `n_qualified`/`raw`/`evict_threshold`/`to_evict` | manager.py:544-646 (esp. 586-611) | ✓ |
| read-only preview returns `{ok,n_qualified,raw,evict_threshold,reenable_target,n_servable,active_count,...}`, skips migration | manager.py:648-693 | ✓ |
| new skills auto-rated 5.0 at registration | manager.py:1443 + settings.py:540-541 | ✓ |
| "New skills start at rating 5.0 automatically" (prompt) | prompts/dna.py:201-202 | ✓ |
| rate-only-if-applied; loaded-but-ignored → no rating; harmful → 0–4 | prompts/dna.py:188-197 | ✓ |
| `SKILL_ACTIVE_TARGET_K=1.0`, `MIN_CAP=20`, `MAX_CAP=200` | settings.py:545-547 | ✓ |
| `CANDIDATE_MIN_RATINGS=5` | settings.py:555-557 | ✓ |
| `total_user_turns` per-session counter (init 0; +=1 per user turn) — seed for the activity clock (§15) | telemetry.py:72, 330, 825 | ✓ |
| `_servable_skill_names()` = disk-servable names WITHOUT the `_disabled` filter (source for soft-eviction fallback + `scan_skills:all`) | manager.py:329-346 | ✓ |
| `load_full_instructions` is registry-only today: returns None if name not in registry (no disk/servable fallback) — the gap §16 closes | manager.py:1085-1117 | ✓ |
| `get_all_metadata()` = Tier-1 metadata backing the `scan_skills` tool | manager.py:1031-1032 | ✓ |
| `scan_skills` registered tool (where the `scan_skills:all` listing is produced) | tools/custom/scan_skills.py:17-47 | ✓ |

**Deviations/notes:** none found — all line refs from the task brief were accurate against the live
tree. One clarification recorded for the implementer: the *new* absolute gate must be composed with the
existing relative cap via **OR** (§9); this interaction is currently unspecified in the codebase and is
where a naive implementation would silently break (a BAD skill surviving under cap headroom).

---

## 14. Recommendation & Alternatives

**RECOMMENDED (implement next):** continuous Bayesian **shrinkage score** (§5) + **quality-gated 3-class
rule** (§7) + **OR-composition** with the count-cap (§9), evicted skills ordered by the **class-priority rank
key** (§17). Defaults `q0=5, K_q=5, n_half=8, g_half=20, r_floor=0.5, τ_turns=200, δ_q=0.5, n_min=5,
fair_window_turns=50` — all exposed as named settings (§18). Pure Python, no heavy deps (only `math.exp`).
Slots into `compute_rebalance_preview` for a consistent UI preview. **Prerequisite:** the small soft-eviction
fix (§16) so evicted skills stay loadable/discoverable.

**Alternatives with tradeoffs:**
- **A′ — Naive weighted multiplicative (§4.1):** simplest to code, but the rating-multiplier curve is
  arbitrary and small-count handling is fudged. Choose only if you want minimalism over defensibility.
- **B′ — Shrinkage score + conservative Wilson-LB BAD gate (§4.3/§7):** same base, plus "evict as BAD
  only when the credible lower bound is below baseline." More cautious (fewer false evictions), slightly
  more math. Recommended for high-stakes corpora; the extra conservatism costs a little pruning power.

**Not recommended:** ELO/BT (mismatch — §4.4); binarized Beta for the *score* (cliff problem — §8, §4.3);
Thompson sampling as a substitute (it's the loading/reward side, complementary not equivalent — §4.5).

---

## 15. Refinement 1 — Activity-Based Clock (why, what, where)

**Why wall-clock fails.** A skill's "fair opportunity" is how many *chances* the system has had to use it —
measured in user-turns of activity — not elapsed calendar days. Under wall-clock aging, a 3-week idle break
ages every skill out by ~21 days while it does nothing, so a relevant-but-domain-skewed skill can cross the
fair window purely because the *user* was away. Activity-based aging fixes this: idle time adds **zero** turns.

**Definition.** Let `global_activity_turns` be a persisted cumulative count of user-turns. Each skill stores
`last_activity_turn` (the value of `global_activity_turns` the last time it was rated/used). Its activity-age is
`A = global_activity_turns − skill.last_activity_turn`. On use/rating, set `skill.last_activity_turn =
global_activity_turns` (resets that skill's clock). Because `global_activity_turns` only advances on real user
turns, **an idle break leaves every `A` unchanged.**

**Where it comes from (verified).** Telemetry already counts user-turns: `total_user_turns` is initialized to 0
(`telemetry.py:72`), incremented once per user turn (`telemetry.py:330`), and surfaced in the session summary
(`telemetry.py:825`). **Caveat:** it is *per-session* (`_session_stats` resets each session), so the activity
clock needs a **persisted cumulative** counter (a new durable field, e.g. persisted `global_activity_turns`, or a
`last_activity_timestamp` + derived turns) that survives sessions and is bumped on every user turn. *Fallback:*
if the counter is unavailable/stale, degrade to wall-clock aging (graceful; never breaks the pass).

**What it drives.** (a) The **PROTECTED gate** (§7): `A < fair_window_turns` → PROTECTED. (b) The **recency
factor** `r` (§5): bounded decay in `A`. Both are now activity-relative and share one clock.

**Worked example — the 3-week break.** A skill used regularly has `last_activity_turn = T`; at that moment
`global_activity_turns = T`, so `A = 0` (fresh). The system is then idle for 3 weeks: **no user turns occur, so
`global_activity_turns` stays `T` and `A` stays 0.** When work resumes, the skill is still `A = 0` → still
PROTECTED/fresh. Under wall-clock it would have aged ~21 days and could have been retired for "doing nothing."

---

## 16. Refinement 2 — Soft Eviction / Loadability Prerequisite

**Design intent.** Eviction is **inactivation (soft), not deletion.** An evicted skill must remain (a) loadable
via `load_skill` and (b) discoverable via `scan_skills:all` (marked inactive), so it can be re-enabled if later
useful. "Evict" removes it from the *active/served* set, not from existence.

**Current gap (verified).** `load_full_instructions` (`manager.py:1085-1117`) resolves **only** via
`_skills_registry`: exact match, then case-insensitive; if the name is not in the registry it logs and
**returns None** (`manager.py:1114-1117`). So an evicted (registry-removed) skill **cannot be loaded today** —
soft eviction is silently broken.

**Required fix (small, scoped):**
1. `load_full_instructions`: when the name is absent from the registry, fall back to the disk-servable corpus —
   enumerate `_servable_skill_names()` (`manager.py:329-346`, which returns on-disk names *without* the
   `_disabled` filter) and load the body from disk if present. This makes evicted-but-on-disk skills loadable again.
2. `scan_skills` (`tools/custom/scan_skills.py:17`, backed by `get_all_metadata`, `manager.py:1031`): the `all`
   listing must include evicted/inactive skills by enumerating disk-servable names and marking them `(inactive)`,
   so they stay discoverable and re-enable-able.

**Why it is a prerequisite (not optional):** without it, "evict" silently becomes "delete from usability," which
contradicts the soft-eviction intent and makes re-enable impossible. Re-enable *semantics* are a follow-on
(Open Questions #3); this section only guarantees evicted skills stay reachable.

---

## 17. Refinement 3 — Ordering Guarantee (BAD ranks below USELESS)

**The problem.** The multiplicative scalar does **not** order BAD below USELESS for all `n`, because `u(n)` grows
with count: a high-count *bad* skill can out-score a low-count *neutral* one. Worked counterexample (defaults):
- **BAD** `(n=30, avg=3.0, L=30)`: `u=1−e^(−30/8)=0.9765`, `q̂=(90+25)/35=3.286`, `w=1` → **S≈0.3208**.
- **USELESS** `(n=2, avg=5.0, L=2)`: `u=1−e^(−2/8)=0.2212`, `q̂=(10+25)/7=5.0`, `w=1` → **S≈0.1106**.

So `S(BAD)=0.3208 > S(USELESS)=0.1106`. A naive "evict the lowest score first" would retire the *neutral* skill
**before** the clearly-harmful one — the wrong order, and it happens for a whole family of `(high-n bad, low-n
neutral)` pairs, not just this one.

**The fix — an explicit class-priority rank key.** Assign each class an ordinal and sort evictions by the tuple
`(class_ordinal, score)`:
- `class_ordinal`: **BAD=0, USELESS=1, UNPROVEN=2, USEFUL=3** (lower = evicted first).
- Eviction order: all BAD first (worst quality among them by score), then USELESS, then UNPROVEN; USEFUL is never
  evicted by the absolute gate (only by cap budget, §9).

**PROTECTED skills are excluded from the eviction candidate set.** A skill with `A < fair_window_turns` that is not
BAD is PROTECTED and is **immune to eviction entirely** — both the absolute gate (§7) *and* the count-cap (§9). It
drops out of protection only when `A ≥ fair_window_turns`, at which point it is classified normally (USEFUL/USELESS/
UNPROVEN) and joins the rank-key ordering. This is why PROTECTED has no ordinal: it is not a member of the set being
sorted. (BAD takes precedence over PROTECTED — §7 Option 2 — so a young-but-proven-harmful skill is still evicted.)

**Proof it fixes the counterexample.** `class_ordinal(BAD)=0 < class_ordinal(USELESS)=1`, so in the tuple
comparison `(0, ·) < (1, ·)` **regardless of the score component** — the BAD skill always sorts first and is
evicted first. This holds for *all* `n` because the ordinal dominates the lexicographic comparison; the score only
breaks ties *within* a class.

**Interaction with the count-cap (§9).** When the cap must remove K active skills, it removes the K lowest
`(class_ordinal, score)` — harmful ones go first, exactly matching intent. Note §1's "precedence" is therefore an
**eviction-ordering** guarantee (not merely a classification priority); the class *assignment* itself is unchanged (§7).

---

## 18. Refinement 4 — Named Settings (no magic numbers)

Every formula/gate constant becomes a user-tweakable setting mirroring the existing `SKILL_ACTIVE_TARGET_K /
MIN_CAP / MAX_CAP` pattern (`settings.py:545-547`). Each has a default, meaning, clamp (applied identically in the
settings handler — see `read-only-threshold-preview-endpoint`), and a one-line effect of increasing it.

| Setting | Default | Meaning / drives | Valid range (clamp) | Effect of increasing |
|---|---|---|---|---|
| `SKILL_SCORE_Q0` (`q0`) | 5.0 | neutral quality baseline / prior mean; fixed by system | [0,10] | shifts the whole quality scale; keep = `SKILL_RATING_INITIAL` (5.0) |
| `SKILL_SCORE_KQ` (`K_q`) | 5 | prior strength / pseudo-count for shrinkage `q̂` | [0,20] int | higher → `q̂` stays near 5.0 longer (slower to trust data) |
| `SKILL_SCORE_NHALF` (`n_half`) | 8 | usage half-saturation of `u(n)` | [1,50] | higher → popularity saturates slower (more skills look "used") |
| `SKILL_SCORE_GHALF` (`g_half`) | 20 | load-waste half-scale of `w` | [1,100] | higher → waste penalty looser (less discounting) |
| `SKILL_SCORE_RFLOOR` (`r_floor`) | 0.5 | recency multiplier floor | [0,1) | lower → recency can penalize more (stronger tiebreaker); must stay <1 |
| `SKILL_SCORE_TAU_TURNS` (`τ_turns`) | 200 | activity-recency decay constant (turns) | [10,5000] | higher → recency decays slower (more minor) |
| `SKILL_SCORE_DQ` (`δ_q`) | 0.5 | quality margin: USEFUL/BAD vs neutral band width | [0.1,3.0] | higher → wider neutral/USELESS band (fewer USEFUL/BAD) |
| `SKILL_SCORE_NMIN` (`n_min`) | 5 | "meaningfully used" bar (= `CANDIDATE_MIN_RATINGS`) | [1,20] int | higher → more ratings needed before USEFUL/BAD (more UNPROVEN) |
| `SKILL_FAIR_WINDOW_TURNS` (`fair_window_turns`) | 50 | PROTECTED window in user-turns | [0,1000] | higher → longer protection (fewer early retirements) |
| `SKILL_ACTIVITY_CLOCK` | `activity` | **internal** clock source for graceful degradation (not a primary user knob) | enum {activity, wallclock} | auto-falls back to `wallclock` when the persisted turn counter is unavailable; manual override only as a last resort |

---

## 19. Open Questions / Suggested Next Actions

1. **Calibrate `fair_window_turns` and confirm the activity counter.** Measure real accumulation rates from
   `skills-metrics.json` (distribution of `n`, `L`, turns-to-first-rating) to set `fair_window_turns`/`n_half`
   (§10), and verify a persisted cumulative turn counter is available for the activity clock (§15, Risk #3).
2. **Decide the conservative BAD gate** (on/off) based on how much false-eviction risk is acceptable.
3. **Define the re-enable interplay:** when a USELESS-retired skill later accrues good ratings, does it
   auto-reactivate? (Currently re-enable is cap-driven; an absolute "promoted to USEFUL" trigger could
   feed it.) Out of scope here but natural follow-on.
4. **Implement** in a separate step: add `score`/`classify` as pure functions over the snapshot, wire into
   `compute_rebalance_preview` (read-only) and `rebalance_active_skills` (OR-composition), plus settings
   for the new constants + a UI preview row. Then delegate to an independent **reviewer** to verify the
   formula/edge-case claims in this doc against the implementation.
