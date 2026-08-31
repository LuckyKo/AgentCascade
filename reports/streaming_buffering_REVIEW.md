# Streaming Buffering Fix — Independent Review

**Reviewer:** streaming_fix_review  
**Date:** 2026-08-31  
**Task:** Verify chunky-streaming fix (proportional tail + quantized cache keys + broadcast floor) for AgentCascade.  
**Verdict:** **APPROVE** — All critical invariants hold, edge cases are correctly handled.

---

## Executive Summary

The three fixes have been implemented and tested. Independent verification confirms:

1. **Index-alignment math is correct** despite the implementation deviating from the plan's literal `history_count == original_history_count` assumption. The actual invariant (`history_count = original_history_count + num_streaming`) ensures the frontend splice computes the right `startIdx`.

2. **Edge cases are properly handled:**
   - Fresh load uses full state, so no hole exists before tailing begins.
   - Changing `num_streaming` between ticks maintains alignment because the cancellation is robust.
   - Full-replace branch triggers correctly when client falls behind.
   - Splice truncation works as intended.

3. **All tests pass** (51/51). The fix does not introduce collateral changes.

4. **No regression risk** to other consumers of `stream_update` — the payload structure is unchanged except for the tail optimization, which the frontend already supports.

---

## Detailed Findings

### 1. Critical: history_count / index-alignment verification (🟢 PASS)

**Server-side code (`state_builder.py`, lines 842–948):**

```python
msgs = full_msgs_snapshot
original_history_count = len(msgs)

if streaming and original_history_count > TAIL_THRESHOLD:
    k = max(5, original_history_count // 10)
    start_idx = max(0, original_history_count - k)
else:
    start_idx = 0
serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs[start_idx:], start_idx)]

# ... later, after appending streaming responses
num_streaming = 0
if stream_responses and len(stream_responses) > 0:
    # ... append with abs_index = original_history_count + j
    num_streaming += 1

result.update({
    'messages': serialized_msgs,
    'history_count': original_history_count + num_streaming,
    # ...
})
```

**Frontend splice (`web_ui/app.js`, lines 2032–2040):**

```javascript
const hCount = sa.history_count || 0;
const startIdx = hCount - sa.messages.length;
if (startIdx >= 0) {
  if (startIdx > existing.messages.length) {
    existing.messages = [...sa.messages];
  } else {
    existing.messages.length = startIdx;
    existing.messages.push(...sa.messages);
  }
}
```

**Mathematical verification:**

- With tail active (K messages) and `num_streaming` appended:
  - `messages.length = K + num_streaming`
  - `history_count = original_history_count + num_streaming`
  - `startIdx = history_count - messages.length = (original_history_count + num_streaming) - (K + num_streaming) = original_history_count - K = start_idx`

✅ **The `num_streaming` term cancels exactly.** The frontend splice lands on the first tail message (`start_idx`). This is **proven** by test `test_tail_active_for_long_streaming_history` which asserts:
```python
assert result['history_count'] == 201, f"history_count should be 201 (200+1), got {result['history_count']}"
assert result['history_count'] - len(msgs) == msgs[0]['index'], "frontend splice misaligned"
```

### 2. Edge Cases (All verified)

#### a) Fresh load → first streaming tick (🟢 PASS)

- Initial state is built via `build_state_from_pool()` which calls `_serialize_all_instances()` with `streaming=False` (see `state_builder.py:280`).
- This sends **full** conversation (`start_idx=0`, all messages). Client has complete history.
- First streaming tick then sends a tail. No hole exists. ✅

#### b) `num_streaming` changes between ticks (🟢 PASS)

- Case 1: Streaming responses clear → `num_streaming = 0`.
  - Server sends: `messages.length = K`, `history_count = original_history_count`.
  - Frontend: `startIdx = N - K = start_idx`. ✅
- Case 2: New streaming response arrives → `num_streaming` increases by 1.
  - `messages.length` grows by 1, `history_count` grows by 1.
  - StartIdx remains unchanged because the added message is at the end (index `original_history_count`). ✅

#### c) Splice truncates stale messages correctly (🟢 PASS)

- Code: `existing.messages.length = startIdx; existing.messages.push(...sa.messages)`
- This **replaces** any client messages from `startIdx` onward with the server's tail.
- If client has fewer messages than `startIdx`, the full-replace branch (`if (startIdx > existing.messages.length)`) executes, loading the entire new array. ✅

#### d) Full-replace branch trigger (🟢 PASS)

- Condition: `startIdx > existing.messages.length`.
- With a tail, this occurs when the client is missing early messages (e.g., after a reconnect or long gap).
- The branch replaces the entire array — **correct** because partial merge would be impossible.

### 3. Fix 1 — Proportional tail logic (🟢 PASS)

**Implementation matches plan:**

- `TAIL_THRESHOLD = 50` only tails when history > 50.
- `k = max(5, original_history_count // 10)` gives ~10% tail, min 5.
- Non-streaming path (`streaming=False`) always uses `start_idx=0`.
- Short histories (≤50) use full send.

**Tests confirm:**
- `test_tail_active_for_long_streaming_history`: 200 msgs → 21 messages (20 tail + 1 streaming), first index 180, history_count 201.
- `test_tail_inactive_below_threshold`: 30 msgs → 31 messages (full send).
- `test_tail_inactive_for_non_streaming`: 200 msgs → 200 messages (full send).
- `test_tail_min_floor_of_five`: 60 msgs → 6 messages, first index 54.

### 4. Fix 2 — Quantized cache/version keys (🟢 PASS)

**Two spots quantized to `//256`:**

1. `build_stream_update_from_pool` version tuple (`state_builder.py:450`):
   ```python
   stream_content_len = raw_stream_len // 256
   current_version = (len(conv_snapshot), id(conv_snapshot[-1]), len(stream_resp_snapshot), stream_content_len)
   ```

2. `_get_or_build_cached_agent_states` cache key (`state_builder.py:909`):
   ```python
   per_agent_stream_content_len = raw_per_agent_len // 256
   cache_key = (original_history_count, id(msgs[-1]), stream_resp_len, per_agent_stream_content_len)
   ```

**Tests confirm:**
- `test_version_bucket_stable_within_256_chars`: 10 chars vs 200 chars → same bucket.
- `test_version_bucket_changes_at_256_chars`: 10 chars vs 300 chars → different buckets.
- `test_version_bucket_stable_across_sub_256_growth`: end-to-end test shows reused cache across <256 growth, recomputed at ≥256.

### 5. Fix 3 — Min-interval broadcast floor (🟢 PASS)

**Implementation (`streaming.py:95–100`):**

```python
MIN_STREAM_BROADCAST_INTERVAL = 0.2
should_broadcast = (
    len_changed
    or (is_streaming_tick and (now_sec - last_send >= MIN_STREAM_BROADCAST_INTERVAL))
    or (not is_streaming_tick and (now_sec - last_send > 0.1))
)
```

- Streaming ticks broadcast at most ~5x/sec.
- `len_changed` bypasses immediately.
- Non-streaming path keeps original 100ms throttle.

**Tests confirm:**
- `test_streaming_tick_floor_suppresses_rapid_ticks`: second tick 50ms later suppressed.
- `test_streaming_tick_broadcasts_after_floor_elapses`: tick 250ms later broadcasts.
- `test_len_changed_bypasses_floor`: length change broadcasts immediately.

### 6. Diff analysis — No collateral changes (🟢 PASS)

**Modified files:**

```
agent_cascade/api_integration_pkg/state_builder.py | 31 ++++++++++++++++------
agent_cascade/api_integration_pkg/streaming.py     | 13 ++++++---
```

Only the three intended fixes were changed. No other logic touched.

### 7. Test suite results (🟢 PASS)

**Command:** `python -m pytest tests/test_streaming_buffering_fixes.py tests/test_state_builder.py tests/test_api_endpoints.py -v`

**Result:**
```
tests/test_state_builder.py            3 passed
tests/test_streaming_buffering_fixes.py 10 passed
tests/test_api_endpoints.py           38 passed
-------------------------------------------
Total: 51 passed, 0 failed
```

All tests are substantive assertions (not vacuous). They directly pin the tail invariant, quantization boundary, and throttle floor.

### 8. Other consumers of `stream_update` (🟢 PASS)

**Search for readers:** The frontend splice logic in `app.js` is designed to handle tails correctly. No other consumers were found that assume full sends. The payload structure (`messages`, `history_count`) remains identical except for the tail size.

**Initial load path:** Uses `build_state_from_pool` (full send), untouched by this fix.

---

## Deviations from Plan (Benign)

The plan stated: `history_count == original_history_count`.  
**Actual code:** `history_count = original_history_count + num_streaming`.

This is **not a bug** — it's a deliberate prior bug fix (comment at lines 939–941) that makes the tail invariant work. The implementation correctly accounts for appended streaming responses so that the frontend splice lands on the first tail message. The plan's literal wording was too strict; the underlying invariant is preserved and verified by tests.

---

## Final Verdict: **APPROVE**

All critical invariants are mathematically sound, edge cases are handled correctly, tests pass, and no regressions exist. The fix can be merged.

---

## Required Changes Before Merge

None. The implementation meets all requirements and exceeds the plan's expectations with robust edge-case handling.
