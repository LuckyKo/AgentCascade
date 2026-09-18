---
name: quantify-perframe-on-scaling
description: Measure whether a streaming backend's per-frame work scales with history size by driving the REAL serialization entry point with synthetic large histories of both shapes, plus a per-component breakdown.
source: auto-generated
version: "1.0.0"
triggers:
  - "streaming slower the more messages"
  - "scales with history size"
  - "per-frame O(n)"
  - "streaming regression"
  - "LLM not at full capacity"
  - "compression improves streaming"
generated_by: researcher
generated_from_task: "Investigate streaming slowdown that grows with conversation history; compression (fewer messages) improves it."
---

## Goal
Decide EVIDENCE-BASED (not by reading code top-down) whether a streaming backend does per-frame work that scales with history length, find the dominant O(n) term, and attribute it to the right code path — before hypothesizing which commit caused it.

## Procedure

### Step 1 — Measure before hypothesizing
Do NOT start by reading code for the culprit commit. First reproduce/quantify the per-frame cost. Identify the ONE function that runs on every broadcast frame for the active instance (e.g. the incremental state serializer + its full-serialize helper). Confirm the throttle cadence (e.g. 10 fps / 100 ms) by reading the broadcast gate.

### Step 2 — Benchmark the REAL entry point, both shapes
Write a script that drives the **actual production entry point** (not a copied helper) with synthetic large histories of the two relevant SHAPES. The shape matters: in delta/tail-cut designs, a **plain-text** conversation cuts the tail (O(1)) but a **tool-chain** conversation (unbroken call/response pairs) often falls back to a **full send** (O(n)). Build both, e.g.:
```python
def make_toolchain(n_pairs):
    msgs = []
    for i in range(n_pairs):
        msgs.append({'role':'assistant','content':'','function_call':{'name':f't{i%5}','arguments':f'{{"x":{i}}}'}})
        msgs.append({'role':'function','name':f't{i%5}','content':'result '*(8+i%3)})
    return msgs
```
Sweep message counts (1k / 4k / 10k) and measure best-of-N wall time of the entry point. Linear scaling with n → O(n) confirmed.

### Step 3 — Per-component breakdown
Time each sub-call (list copy, tail-index walk, per-message serialize loop, fingerprint loop, token-stats) at the largest n to find the DOMINANT term. **Watch for caches**: token-stats / per-message stats are often cached per turn (keyed on history_count + last-msg fingerprint) — measure the *direct* call only to confirm it is NOT the per-frame term; only the growing streaming partial recomputes per tick.

### Step 4 — Attribute the O(n) to the code path
Find WHY the full send happens (e.g. a tail-cut integrity rule that returns start_idx=0 for unbroken chains). Confirm with the pinned unit test (grep for the test that asserts the full-send behavior). Note the per-message constant (e.g. ~2 µs/msg) vs the O(n) scaling.

### Step 5 — Attribute to commits honestly
`git show` each candidate commit. Distinguish (a) a pre-existing O(n) path that merely *emerged* as histories grew, from (b) a recent commit that ADDED per-frame O(n) work. Do not blame a refactor commit for pre-existing scaling. Check whether the per-message cost was already optimized (e.g. deepcopy→shallow copy, md5→O(1) fingerprint) so you don't re-diagnose a fixed constant.

### Step 6 — Verify the LLM-starvation mechanism (if "not using the LLM at full capacity")
If the frame work runs on the thread that consumes the LLM generator, confirm the generator wrapper does NOT buffer (it just yields with timeout guards) → the O(n) work pauses the LLM read loop → llama.cpp idles. Enable any existing debug probe (e.g. a `STREAM_BACKEND_DEBUG` SSE-cadence vs yield→enqueue probe) for definitive confirmation: expect "streaming fine but broadcast delayed," not "LLM not streaming."

## Tips
- **Shape is the discriminator**: if plain-text is O(1) but tool-chain is O(n), the full-send fallback for tool chains is the root cause — not the frontend.
- **Frontend O(n) is often a red herring**: benchmark the frontend per-frame work (e.g. a content-key over all messages) in isolation; it's frequently negligible (µs) vs the backend serialize.
- **Caches flip the conclusion**: a function that looks O(n) in a direct call may be once-per-turn in production (cached). Read the cache key before claiming per-frame cost.
- **Stub missing 3rd-party deps** in the benchmark (bs4/requests/httpx) via `sys.modules` so the real module imports cleanly in a sandbox without the full venv.
- **Separate concurrent symptoms**: "next turn blocked until current stream finishes" is usually a pre-existing generator-consumption model, *amplified* by the O(n) — verify it independently; don't conflate with the scaling root cause.
- Deliverable: ranked hypotheses (HIGH/MED/LOW) with file:line, measured numbers, commit attribution, fix directions (no implementation), and the test/evidence that would confirm each.
