---
name: composite-sort-key-for-class-ordering
description: When a scalar score conflates multiple factors (e.g., popularity x quality), it does NOT preserve your intended categorical precedence across all input ranges; guarantee eviction/sort order with a lexicographic (class_ordinal, score) key where the ordinal dominates.
source: auto-generated
version: "1.0.0"
triggers:
  - "score then classify then sort or evict"
  - "class precedence ordering guarantee"
  - "composite rank key class_ordinal"
  - "weighted score conflates popularity and quality"
  - "eviction order wrong item first"
generated_by: researcher
generated_from_task: "Design a popularity/usefulness scoring function + useful/bad/useless classification threshold for AgentCascade skill-invalidation. Score = saturating usage (popularity) x shrinkage quality x load-waste penalty x recency. A reviewer found this multiplicative scalar conflates popularity and quality, so it ordered a high-count BAD item above a low-count USELESS one; the fix is to evict by a lexicographic composite rank key (class_ordinal, score) where the ordinal dominates, and to exclude PROTECTED grace-window items from the candidate set."
---

## Goal
Guarantee that a score → classify → sort/evict system removes or ranks items in your intended CLASS precedence (e.g., harmful before neutral before useful) for ALL inputs, not just typical ones.

## The trap
A single scalar built by multiplying/adding several factors (popularity × quality × recency × ...) conflates those factors. Because at least one factor usually GROWS with volume (a saturating usage term `u(n)`), a high-volume item in a "bad" class can out-score a low-volume item in a "neutral/useless" class. Sorting/evicting by the raw score alone then removes the WRONG item first — and it happens for a whole family of inputs, not one.

Worked shape (any concrete numbers):
- **BAD**: high count → large `u`, low quality → e.g. `S = 0.32`
- **USELESS**: low count → small `u`, neutral quality → e.g. `S = 0.11`
- `S(BAD) > S(USELESS)` ⇒ "evict lowest score first" evicts USELESS before the clearly-harmful BAD. Wrong order.

## Procedure
### Step 1 — Detect it before implementing
For your intended class precedence (e.g., BAD < USELESS < UNPROVEN < USEFUL), construct a counterexample: pick a HIGH-volume item in a lower-priority-intent class and a LOW-volume item in a higher-priority-intent class; compute both scores. If ordering by score contradicts the intended precedence, the scalar alone is insufficient.

### Step 2 — Use a composite rank key
Sort/evict by a lexicographic tuple `(class_ordinal, score)` where:
- `class_ordinal` is an integer per class with LOWER = evicted first (e.g., BAD=0, USELESS=1, UNPROVEN=2, USEFUL=3).
- The ordinal dominates the comparison; `score` only breaks ties WITHIN a class.
This guarantees the intended precedence for all inputs because the tuple's first element decides before the score is consulted.

### Step 3 — Define every class's membership in the candidate set
A class that must be immune to eviction (e.g., a PROTECTED / new-item grace window) has NO ordinal and is EXCLUDED from the candidate set entirely — not evicted by any gate until its condition clears — then joins ordering afterward. State this explicitly: an undefined bucket for a class is where implementations crash or silently mis-sort.

### Step 4 — Verify
With concrete numbers, assert that for the counterexample pair the composite key orders them as intended, and argue it holds for all inputs (ordinal dominance). Put this in the design doc and as a test.

## Tips
- The bug is invisible in "typical" data: at low volumes `u(n)` is small so classes happen to line up; it only breaks once a bad item accumulates enough volume. Always test the high-volume-bad vs low-volume-neutral pair explicitly.
- Do NOT "fix" it by re-weighting the score (e.g., raising the quality multiplier). That just moves the crossing point to another input region — the conflation is structural. The composite key is the robust fix.
- Keep class ASSIGNMENT separate from class ORDERING: assignment uses thresholds on the score/quality; ordering uses the ordinal. Conflating them re-introduces the bug.
- When a budget/cap must remove K items, remove the K lowest `(class_ordinal, score)` so harmful ones go first.
