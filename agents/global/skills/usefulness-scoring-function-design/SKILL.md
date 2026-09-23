---
name: usefulness-scoring-function-design
description: Research methodology for designing a popularity/usefulness scoring function + useful/bad/useless classification threshold from per-item (usage count, average rating with a neutral baseline, auxiliary signals like load-waste/recency).
source: auto-generated
version: "1.0.0"
triggers:
  - "scoring function"
  - "usefulness score"
  - "popularity score"
  - "useful useless threshold"
  - "skill invalidation scoring"
  - "wilson score rating"
  - "bayesian shrinkage rating"
generated_by: researcher
generated_from_task: "Design a popularity/usefulness scoring function + useful/useless threshold for AgentCascade skill-invalidation (research/design doc only)."
---

## Goal
Produce a mathematically sound, bounded **usefulness score** and a defensible **multi-class threshold** (useful / bad / useless) for items that have a usage count, an average quality rating on a scale with a known neutral baseline, and optional auxiliary signals — so a rank/prune decision can be made that is robust to small-count noise.

## Procedure
### Step 1 — Ground the data schema before any math
Read (don't grep) the actual per-item record. Determine: what does the **count** measure (deliberate-use vs mere discovery/injection — these differ and change the whole design); the **rating scale + neutral baseline** (e.g. [0,10] with 5.0 = "no signal yet"); which auxiliary fields exist (load-waste gap, last-used). Anchor every constant you introduce to an existing system constant where possible (e.g. prior strength K = an existing "min ratings to trust an average" constant) so the design stays consistent with the rest of the system.

### Step 2 — Evaluate candidate functions honestly (at least these)
- **Naive weighted multiplicative** (popularity × rating-multiplier × penalties): intuitive but the rating curve is arbitrary and small-count handling is fudged. Keep its *factor structure*, replace the quality factor with something principled.
- **Bayesian shrinkage / posterior mean** `q̂ = (n·avg + K·q0)/(n+K)` (q0 = baseline): RECOMMENDED for count+baseline data. Pulls low-count toward the neutral baseline (not over-penalized, not over-rewarded); K is an interpretable pseudo-count.
- **Wilson lower bound / Beta-Bernoulli**: deliberately *pessimistic* — pulls low-count toward 0, so it PENALIZES new items (wrong for a score). Use only as a conservative *eviction* gate ("call it bad only when the lower bound is below baseline"). Binarized Beta has a **cliff** at the threshold (a 0.2-pt rating swing straddles it and swings the estimate hugely) — continuous shrinkage avoids this.
- **ELO / Bradley-Terry**: needs pairwise head-to-head outcomes. For absolute single-rater ratings it's a **mismatch**; "rating vs a fixed baseline" degenerates to the shrinkage mean, so ELO adds nothing.
- **Thompson sampling**: that's the *loading/serving* (reward) side — complementary, not a substitute for the *pruning* score.

### Step 3 — Use a quality-gated multi-class rule, NOT a single raw-score cut
A lone scalar cannot separate "bad" (used a lot, rated poorly) from "useless" (barely used, neutral) — both score low. Key the classes off the **quality sign** (q̂ vs baseline ± margin δ) and **usage level** (count bands); use the scalar for *ranking* + the top "useful" cut. Typical precedence: BAD > PROTECTED > {USEFUL, USELESS} > UNPROVEN. Decide explicitly whether a young-but-demonstrably-harmful item is evicted (BAD-exempt protection) or shielded (absolute protection) — this is where naive designs silently break.

### Step 4 — Bounded minor recency + neutral point
Make recency a **floored** multiplier `r = r_floor + (1-r_floor)·e^(-t/τ)` with r_floor≈0.5 so it can reduce the score by at most ~50% ("minor tiebreaker" by construction). Set the score's neutral point to mirror the rating baseline (e.g. 0.5 for a [0,1] score ↔ 5.0 for [0,10]). Use TWO distinct clocks and name them: creation-age for the "new/protected" gate; last-used for recency.

### Step 5 — Decide composition with any existing relative cap
If a relative count-cap/budget already exists, state how the absolute gate composes with it. **OR-composition** (evict if cap selects it OR class is bad/useless) fixes the "cap has headroom so nothing evicted" gap and strictly dominates cap-only.

### Step 6 — Verify every worked number with code, not by hand
Recompute all edge-case scores in a throwaway script (code_interpreter) from the formula + defaults before writing them into the doc. Hand-computed tables routinely drop a factor (e.g. omitting the recency term) and mislabel classes. Walk each case through the decision rule to confirm the stated class matches.

## Tips
- **Recommend shrinkage, justify K** by anchoring it to an existing "trust threshold" constant — that's what makes it defensible rather than a magic number.
- **The neutral baseline is load-bearing**: above = useful, below = harmful, at = no signal. A "high-count near-baseline" item is *confidently* neutral → low-risk to retire (decide this explicitly; don't leave it as a cop-out).
- **New-item protection must be real**: the canonical useless state (count=1, rating=baseline) IS the brand-new state, so protection (a fair-window on creation-age) is what keeps new items from being instantly retired. Confirm count<min means a new item can't be "bad" anyway.
- **Report robustness**: show that classifications don't flip under ±20% perturbations of the key constants — it proves the design isn't knife-edge and tells you which constant actually drives behavior (usually the quality margin δ).
- **Deliverable shape**: one-line formula + named/justified constants + worked edge-case table (with both the core score S and recency-adjusted score) + threshold rules + a "verified anchors" table mapping every data claim to source.
