# Streaming Buffering — Fix Plan (Option: proportional tail + quantized cache keys)

**Bug:** todo line 142 — "weird streaming hangups; buffers for minutes then drops the whole chunk quickly."
**Root cause:** CONFIRMED still valid at HEAD (ce9570a). See `reports/streaming-buffering-root-cause.md` + `reports/streaming_buffering_VERIFY_at_HEAD.md`.
Every `stream_update` frame serializes the ENTIRE conversation of the active instance (+ all other instances' cached arrays), and per-chunk ticks bypass the 100ms throttle, so this O(N) build+serialize runs once per SSE chunk. Cost grows linearly with history ⇒ engine falls behind ⇒ UI receives ever-larger batches.

## Scope (do ALL three; they are independent and low-risk)

### Fix 1 — Proportional streaming tail in `_serialize_instance` (PRIMARY, biggest win)
File: `agent_cascade/api_integration_pkg/state_builder.py`, function `_serialize_instance`, the block at ~line 838-844.

CURRENT:
```python
msgs = full_msgs_snapshot
original_history_count = len(msgs)

# Always send all messages — no tail optimization. ...
start_idx = 0
serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]
```

REPLACE with a proportional tail that ONLY applies when `streaming=True` and history is long:
```python
msgs = full_msgs_snapshot
original_history_count = len(msgs)

# Tail optimization (streaming only): during active streaming the client already
# holds the confirmed history, so send only the last K messages. The frontend
# splice (web_ui/app.js: startIdx = history_count - sa.messages.length) merges a
# partial array correctly and keeps indices aligned with `history_count`.
# Full sends are preserved for non-streaming updates and short histories.
TAIL_THRESHOLD = 50          # only tail when history is long
if streaming and original_history_count > TAIL_THRESHOLD:
    k = max(5, original_history_count // 10)   # ~10% tail, min 5
    start_idx = max(0, original_history_count - k)
else:
    start_idx = 0
serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs[start_idx:], start_idx)]
```

CRITICAL CONSTRAINTS (do not violate):
- `start_idx` MUST be based on `original_history_count` (the full conversation length), NOT on the post-slice list. The frontend computes `startIdx = history_count - sa.messages.length`; with a tail of K, that equals `original_history_count - K = start_idx`. Indices must stay aligned.
- DO NOT change how streaming content is appended below (~line 851-880). `stream_responses` are still appended at `abs_index = original_history_count + j` — that logic stays identical and correct regardless of the tail.
- Keep `result['is_partial']`, `history_count` (wherever it's set from `original_history_count`), and everything else unchanged. Verify `history_count` in the returned payload is still the FULL count (`original_history_count`), not the sliced length — grep for where `history_count` is assigned in this function and confirm it uses the full count.
- Non-streaming path (`streaming=False`) MUST keep `start_idx = 0` (full send) — initial load / refresh depends on it.

### Fix 2 — Quantize token-stats cache/version keys (kills per-chunk recompute)
File: same file.
(a) In `build_agent_states`, the version tuple (~line 443-453): change the `stream_content_len` element from raw sum to a bucketed value: `raw_stream_len // 256`. Keep the raw sum in a local var for clarity; put the bucket in the tuple.
(b) In `_get_or_build_cached_agent_states`, the `cache_key` (~line 891-895): change `per_agent_stream_content_len` element to `raw_per_agent_len // 256`.
Rationale: token stats then refresh at most every ~256 chars of streaming growth instead of every chunk. Add a one-line comment explaining the bucketing.

### Fix 3 — Min-interval floor for streaming-tick broadcasts (insurance)
File: `agent_cascade/api_integration_pkg/streaming.py`, `broadcast_stream_update`, the `should_broadcast` OR (~line 91-95).
Add a minimum interval so streaming ticks broadcast at most ~5x/sec instead of per-chunk, while still letting `len_changed` (committed messages) and metadata updates through immediately. Suggested:
```python
MIN_STREAM_BROADCAST_INTERVAL = 0.2
should_broadcast = (
    len_changed
    or (is_streaming_tick and (now_sec - last_send >= MIN_STREAM_BROADCAST_INTERVAL))
    or (not is_streaming_tick and (now_sec - last_send > 0.1))
)
```
Preserve existing behavior for the non-streaming periodic path. Read the current exact expression first and adapt minimally.

## Out of scope / do NOT touch
- `build_state_from_pool` full-state payload (initial load must stay full).
- The `approvals` field handling (recently fixed in ce9570a — unrelated).
- Engine loop, llm_call.py, core.py — no changes needed.
- Frontend app.js — the splice path already handles tails; do NOT modify it unless a test proves otherwise.

## Tests (add regression coverage)
1. **Tail behavior** (`tests/test_state_builder.py` or new `tests/test_streaming_tail.py`):
   - streaming=True, 200 msgs → returned `messages` length == max(5, 200//10)=20, and `history_count` in payload == 200 (full), first serialized message index == 180.
   - streaming=True, 30 msgs (below threshold) → full send (len==30, start_idx 0).
   - streaming=False, 200 msgs → full send (len==200).
   - Verify appended stream_responses still present with correct abs_index when tail active.
2. **Cache key quantization**: two snapshots differing only by <256 chars of streaming content produce the SAME cache_key/version bucket; ≥256-char difference produces a different one.
3. **Throttle floor** (`tests/test_streaming.py` if it exists): rapid consecutive is_streaming_tick calls within 200ms → only first broadcasts; after interval elapses → next broadcasts. len_changed still bypasses.

Run existing suites: `tests/test_state_builder.py`, `tests/test_streaming.py` (if present), `tests/test_api_server.py`. Report exact pass/fail counts.

## Deliverable
Write impl report to `reports/streaming_buffering_IMPL.md`: diff summary per fix, the history_count-alignment verification, test results, any deviations. Do NOT commit — a Reviewer verifies first.
