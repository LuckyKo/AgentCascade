---
name: retrieval-gate-calibration
description: Replace a magic similarity/relevance threshold in a TF-IDF/IR/ranking system with a self-calibrating gate, validated by replaying real logged queries through the actual matcher.
source: auto-generated
version: "1.0.0"
triggers:
  - "magic threshold"
  - "similarity threshold"
  - "tf-idf"
  - "cosine similarity"
  - "retrieval gate"
  - "self-calibrating"
  - "hint never fires"
  - "score threshold"
  - "false positive rate"
generated_by: researcher
generated_from_task: "Make a TF-IDF+cosine memory-hint system self-calibrating instead of a magic 0.35 similarity threshold that never fires."
---

## Goal
Replace a hand-picked similarity/relevance threshold (which often never fires, or spams) with a gate derived from the system's OWN score distribution — validated by replaying real queries through the actual matcher, not by reading docs or guessing.

## Procedure

### Step 1 — Replay real queries through the ACTUAL matcher
Do not analyze the algorithm on paper. Mine real query strings from production logs (e.g. session `.jsonl` assistant text / reasoning, first N chars — mirror the production query-extraction exactly) and run them through the real matcher against the real index. Keep per-query rows: `top1`, `top2`, full score vector. This is ground truth for the score scale.

### Step 2 — Measure the score-ceiling percentiles (the core diagnostic)
Compute `top1` percentiles: p50 / p75 / p90 / p99 / max. **The threshold must be compared against the REAL ceiling, not the "expected" score.** A fixed threshold above p99 → structurally dead. Typical root cause for low ceilings: a long multi-topic query + `tf=count/len` (L2) dilutes the query vector across topics → top-1 stays low and `top1≈top2` (ambiguous). Corpus size alone is rarely the cause.

### Step 3 — Test candidate adaptive gates empirically; check for DEGENERACY
For each candidate, compute its fire rate over ALL real queries. **A gate that fires ~100% is useless.** In a large right-skewed corpus, top-1 is *always* an extreme outlier (top1/mean ≈ 10×), so `top1 > mean + k·σ` and `top1/mean > r` are degenerate — reject them even though they look "standard IR." The **margin/gap gate** (`top1 ≥ F·top2`) is the reliable self-calibrating signal: it separates "one clear winner" (specific query) from "many near-equal scores" (generic/diffuse query). Also test a **data-derived floor** (e.g. p90 of top1) and/or an **EWMA of per-turn top1** as the "is there any signal" check.

### Step 4 — Check if log-based precision calibration is even feasible
Count the actual positive events (e.g. "agent later read the hinted doc") in the logs. If only a handful exist across hundreds of queries, **log-based threshold calibration is infeasible** — say so, and fall back to a small manual-labeled sample (n≈48) for precision. Do not overstate precision you cannot measure.

### Step 5 — Optional query-side levers (test, don't assume)
Sublinear TF, drop-low-IDF, top-N-IDF, BM25: test them — they often give NO ceiling/precision gain on diffuse queries. Using only the **first sentence / first ~150 chars** of the turn as the query is the one lever that usually gives a mild real lift.

### Step 6 — Deliver a spec with DATA-derived defaults
Exact formulas + plug-in point (which check in the code is replaced). Defaults come from the measured percentiles (e.g. floor = p90, margin = point where clear-winner rate becomes meaningful), NOT invented. Preserve existing safety gates (noise/`>N strong`, dedup, cooldown). Provide a repeatable replay harness + regression tests (specific query→1 winner fires; generic query→ambiguous skips; no-signal skips).

## Tips
- The #1 trap: trusting the "expected" similarity scale. Always measure the real top-1 ceiling first.
- "Standard IR" heuristics (mean+kσ, ratio) look right but are degenerate for single-top-1 retrieval over large corpora — always compute their fire rate.
- Margin/gap is self-calibrating per-query (no persistence) and degrades gracefully: diffuse queries have margin≈1.0 and also trip a `>N strong` noise gate.
- Keep the fixed threshold as an optional override/kill-switch; retire it as the primary gate.
- Empirical fire-rate + a small manual precision sample beats a literature dump. Delegate the final report to an independent reviewer to catch over-claims.
- Persist adaptive state minimally (one float, e.g. EWMA floor) if it must survive restarts.
