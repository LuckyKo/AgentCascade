---
name: cache-prefix-reuse-optimization
description: Verify and implement a "cache the stable prefix, re-serialize only the growing tail" optimization for per-frame streaming serialization without breaking dedup/stale-guard invariants or hitting aliasing bugs.
source: auto-generated
version: "1.0.0"
triggers:
  - "cache the committed prefix"
  - "prefix reuse per frame"
  - "re-serialize only the tail"
  - "O(n) per-frame serialization"
  - "streaming delta cache"
generated_by: coder
generated_from_task: "todo.md #137 streaming slowdown — cache committed range per turn so per-frame cost is O(tail) not O(n)"
---

## Goal
Implement a per-turn prefix/committed-range cache for an incremental (delta) serialization path that runs every frame, while provably preserving dedup, absolute-index, and stale-prefix-guard invariants — and avoid the two classic failure modes: caching the wrong slice, and aliasing a live list.

## Procedure

### Step 1 — Prove the cached slice is non-empty in the WORST case
Before writing code, determine what the "prefix" actually contains for the shape that motivated the fix. A "cache `[0:cut_point]`" optimization is a **no-op when `cut_point == 0`** — and for an unbroken tool chain `cut_point` IS 0, so the prefix is empty and the *tail* is the entire history. If your worst case has `cut_point==0`, cache the **committed range `[cut_point:]`** (the whole history in that case), not `[0:cut_point]`.
```python
# WRONG for tool chains (prefix empty when start_idx==0):
cached = serialize(msgs[0:start_idx])      # [] when start_idx==0 -> saves nothing
# RIGHT: cache the range that is actually re-sent every frame:
cached = serialize(msgs[start_idx:])       # whole history when start_idx==0
```
Verify with a one-line check of `_safe_tail_start_index` (or equivalent) for each conversation shape; the fix must reduce cost in the shape that was slow, not just the easy one.

### Step 2 — Cache BOTH the serialized list AND its fingerprints
The per-frame O(n) is usually TWO loops: the serialize loop and a fingerprint/dedup-seed loop. Caching only the list leaves the seed loop O(n). Store the fingerprints computed from the same messages so a HIT seeds `existing_fingerprints` in O(1):
```python
committed_fps = {fp for fp in (_streaming_fingerprint(m, j) for j, m in enumerate(cached_msgs)) if fp != EMPTY}
# On HIT: existing_fingerprints = set(committed_fps)   # NOT a re-walk of serialized_msgs
```
Keep the original walk as the fallback for non-delta / force_full frames (no cache entry).

### Step 3 — Store a COPY, never the live list (aliasing bug)
If the streaming loop later does `serialized_msgs.append(partial)` and you stored `serialized_msgs` itself in the cache, frame N's partial gets **baked into** the cached entry reused by frames N+1..end. Always store `list(serialized_msgs)`, and on a HIT assign a fresh `list(cached['committed_serialized'])`.
```python
# MISS:
_prefix_cache_store(name, key, cut_point, list(serialized_msgs), committed_fps)  # COPY
serialized_msgs.append(partial)   # safe: cache holds a different list object
# HIT:
serialized_msgs = list(cached['committed_serialized'])  # fresh copy per frame
```

### Step 4 — Gate the whole cache behind the delta flag
Wrap all cache logic in `if use_delta:`. force_full / prefix_shrink / non-delta frames must bypass it entirely (full send, no consult, no clobber). Confirm by a test that a force_full frame does not read or overwrite the entry.

### Step 5 — Identity key + invalidation + size cap
Key = `(history_count, fingerprint(last_msg))` plus `cut_point`. Any commit/shrink changes the key → automatic MISS/rebuild. Clear the entry in the cache manager's `evict_instance` and `clear_all`. Add a `_PREFIX_CACHE_MAXSIZE` (FIFO) to bound memory across many instances.

### Step 6 — Regression tests: assert IDENTICAL output to the no-cache path
The strongest test is byte-identical output vs a baseline computed with the cache disabled, for BOTH a MISS frame and a HIT frame, plus a serialize-call-count assertion proving the tail/partial was re-serialized but the committed range was not. Cover: reuse across frames, rebuild on commit, rebuild on shrink, force_full bypass, stale-prefix guard still fires, dedup of a partial matching a committed-range message.
```python
def _no_cache_baseline(inst, pool, **kw):
    saved = sb._prefix_cache_lookup
    sb._prefix_cache_lookup = lambda *a, **k: None
    try: return sb._serialize_instance(inst, pool, **kw)
    finally: sb._prefix_cache_lookup = saved
```

## Tips
- Benchmark the REAL serialization entry point with a synthetic large history of BOTH shapes (tool-chain `start_idx==0` and plain-text tail-cut); a mocked serializer makes before/after numbers meaningless. Expect the win to flatten per-frame cost, not zero it — the wire format may still send the full range (O(n) list-copy + JSON), which is inherent and out of scope unless you also change the protocol.
- The stale-prefix guard scans `serialized_msgs` for the LAST assistant; on a HIT that message must still be in the committed range (it is, since the last committed msg is always sent). Append streaming partials AFTER the guard's scan, and never mutate the cached list.
- Absolute indices: tail/committed messages keep `[start_idx:]`; streaming partials get `original_history_count + j`. A HIT must not renumber anything.
- If a "prefix-matching partial dedup" test fails, check whether that message is actually in the SENT range — the original code only seeds from `[start_idx:]`, so a partial matching an UNSENT prefix message was never deduped; don't invent a new invariant the baseline doesn't have.
