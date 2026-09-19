---
name: tfidf-gate-test-fixtures
description: How to build deterministic test fixtures for TF-IDF/cosine similarity gates — measure real scores first, isolate each gate stage, handle exact ties
source: auto-generated
version: "1.0.0"
triggers:
  - "tf-idf"
  - "similarity gate"
  - "matcher test fixture"
  - "cosine threshold test"
  - "memory-hint"
generated_by: coder
generated_from_task: "Self-calibrating specificity gate for memory-hint (floor/gap/noise stages)"
---

## Goal
Write deterministic, self-validating tests for TF-IDF/cosine similarity gates without burning iterations on fixture score surprises.

## Procedure

### Step 1 — Measure real scores BEFORE writing assertions
Never assume a query "should" score high/low. Build the exact fixture docs in a scratch run and print actual `matcher.match(query)` results:
```python
# scratch: build fixture vault, then
for q in ['candidate query 1', 'candidate query 2']:
    print(repr(q), [(p, round(s,4)) for p,s in m.match(q)])
```
Key trap: **small-vault scores are HIGH, not low.** A "weak" overlap can score 0.3–0.7 when only 2–5 docs exist (IDF is corpus-relative; the low ceilings measured on a 945-doc production corpus do NOT transfer to tiny test vaults). If you need a sub-floor score, probe many candidate queries until one lands in the required band (e.g. [FLOOR_MIN, FLOOR_SEED)).

### Step 2 — Isolate each gate stage with pre-assertions
For a multi-stage gate (floor → gap → noise → fire), a test of stage N must PROVE stages 1..N−1 pass first, or the skip could come from an earlier stage and the test proves nothing:
```python
# Noise-gate test: assert the EARLIER gates do NOT trip
assert top1 >= floor, f'top1 {top1:.3f} below floor — bad fixture'
assert top1 - top2 >= GAP, f'gap gate would skip first: {matches}'
assert n_strong > MAX_HINTS_PER_TURN  # the condition under test
mgr._process_job(job)
assert inst._tool_warnings == []
```
If an independent reviewer is available, this is exactly what they will catch.

### Step 3 — Handle exact-score ties explicitly
Docs with identical matching content score EXACTLY equal; sort order then falls back to the tiebreak (usually path name). Two consequences:
- Do NOT assert a guessed relative order among tied docs — derive expectations from the actual ranking: `ranked = [p for p,_ in matches]; assert ranked[3] not in hint`.
- To make one doc a clear winner while keeping all docs above the floor, give the winner distinctive extra tokens AND keep a shared topic phrase across all docs (measured pattern: 0.91 / 0.37×4 → gap 0.53, all ≥ floor).

### Step 4 — Keep fixtures self-validating
Every gate test should assert its own fixture invariants (gap ≥ GAP, top1 in band, n_strong > N) with clear failure messages including the actual scores. If the matcher changes later, the test fails on the invariant with diagnostics instead of silently testing the wrong stage.

## Tips
- "Diffuse tie" fixtures: two docs sharing the same distinctive words → near-equal scores (gap ≈ 0). Verify top1 ≥ floor so the gap gate (not the floor gate) is what skips.
- For EWMA/adaptive-state tests, call the update function directly in a loop (e.g. 200 low scores) — faster and deterministic vs driving full jobs.
- Record measured fixture scores in a code comment (`# Scores verified: 0.78 / 0.26×4`) so future edits know what to expect.
