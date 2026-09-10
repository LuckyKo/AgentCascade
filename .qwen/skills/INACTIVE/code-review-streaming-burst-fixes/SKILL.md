---
name: code-review-streaming-burst-fixes
description: Systematic review methodology for minimal backend fixes targeting streaming burst and final frame deduplication issues in distributed agent systems. Focus on message loss, duplicate delivery, timestamp collisions, and edge case validation.
source: auto-generated
version: "1.0.0"
triggers:
  - "streaming burst fix"
  - "final frame dedup"
  - "sub-agent turn-end"
  - "broadcast stream update"
  - "code review critical issue"
generated_by: coder
generated_from_task: "Review a MINIMAL backend fix (FIX A) for a sub-agent turn-end streaming burst in AgentCascade. I need a PASS/FAIL with any issues, focused on correctness and edge cases."
---

## Goal

Enable reviewers to systematically audit minimal backend changes to streaming/finalization logic, ensuring no message loss, duplicate delivery, or timestamp collision regressions are introduced.

## Procedure

### Step 1 — Gather Context and Understand the Bug

Read the production code surrounding the change location (e.g., `_create_and_run_agent` in `core.py`). Identify:
- The original bug (e.g., forced final broadcast with `last_send=0.0` causing same-timestamp duplicate)
- The proposed fix logic (conditional final broadcast based on time and length changes)
- Related helper functions (`broadcast_stream_update`, `push_final_state`)

**Example:** In `core.py` L3241-3283, the old code unconditionally forced a final frame; the new code uses `need_final = ((now_mono - _last_sub_send) > 0.1) or (len(final_resp) != _sub_last_resp_len)`.

### Step 2 — Verify Throttle and Broadcast Logic

Read the referenced helper functions (`streaming.py` ~L445-451). Confirm:
- The broadcast condition is `len_changed or (now_sec - last_send > 0.1)`
- On suppressed ticks, the function returns `(last_send, resp_len)` without updating `last_send`
- `_last_sub_send` and `_sub_last_resp_len` are updated correctly across loop iterations

**Key insight:** A suppressed tick updates `_sub_last_resp_len` but leaves `_last_sub_send` stale. This asymmetry is critical for the fix.

### Step 3 — Test All Scenarios Manually

Create a mental model of the loop's final state:

| Scenario | Last Tick Status | Elapsed Time | Length Changed? | Need Final? |
|----------|------------------|--------------|-----------------|-------------|
| Normal   | Broadcast sent   | <100ms       | No              | False (skip) ✅ |
| Throttled| Suppressed       | <100ms       | No              | **False?** ❌ |
| Throttled| Suppressed       | >100ms       | No              | True (time branch) ✅ |
| Changed  | Any              | Any          | Yes             | True ✅ |

**Critical flaw:** Scenario 2 (throttled + <100ms + no length change) yields `need_final=False`, skipping the final frame and losing committed state.

### Step 4 — Check for Edge Cases

- **Empty final_resp**: Loop ran 0 times → `_last_sub_send=0.0` → time >0.1s → `need_final=True`. Correct ✅
- **push_final_state**: Targets different instance (`caller` vs `<sub>`), so no per-instance collision. Reasoning sound ✅
- **Thread-safety**: All variables local to method, no shared state issues ✅

### Step 5 — Verify Test Coverage

Read the test file (`test_subagent_streaming_burst.py`). Check:
- `test_fix_a_no_same_timestamp_subagent_frames`: Ensures no two sub-agent frames share a timestamp.
- `test_fix_a_final_delivered_when_last_tick_throttled`: Uses sleeps to force >100ms, so it doesn't catch Scenario 2.
- `test_fix_a_push_final_state_present`: Verifies root refresh still fires.

**Gap:** No test for fast finalization after a suppressed tick (<100ms total). The existing throttled test uses `time.sleep(0.005)` over 20 iterations, which likely exceeds 100ms due to overhead.

### Step 6 — Propose Minimal Fix

To guard against message loss, add a flag `_last_tick_suppressed` that records whether the last loop tick was throttled out.

**Patch:**
```python
# Before loop
_last_sub_send = 0.0
_sub_last_resp_len = 0
_last_tick_suppressed = False

# Inside loop after broadcast_stream_update
_prev_send = _last_sub_send
_last_sub_send, _sub_last_resp_len = broadcast_stream_update(...)
_last_tick_suppressed = (_prev_send == _last_sub_send)

# After loop
need_final = (
    _last_tick_suppressed or
    ((now_mono - _last_sub_send) > 0.1) or
    (len(final_resp) != _sub_last_resp_len)
)
```

### Step 7 — Final Verdict

- **PASS**: All scenarios covered, no edge cases missed, tests comprehensive.
- **NEEDS WORK**: Minor issues, but fix is salvageable with suggested patch.
- **FAIL**: Critical regression (e.g., message loss in fast finalization) that must be fixed before approval.

## Tips

- **Always trust the verified facts** about helper functions, but verify they still hold after the change.
- **Don't rely solely on existing tests**—they may have hidden assumptions (e.g., sleeps that inflate timing).
- **Check per-instance vs queue-level collision**: The original burst was a queue-level observation; per-instance dedup logic is correct if targets differ.
- **For streaming fixes, think about the "last tick" state**: Is it broadcast or suppressed? What does the UI show at loop exit?
- **When proposing fixes, keep them surgical**: Add one flag, modify one condition, avoid touching unrelated logic.
- **Use tables to map scenarios**: This clarifies which combinations of conditions lead to correct/incorrect behavior.

## Common Pitfalls

1. **Assuming time branch covers all throttled cases** — it fails when loop exits before 100ms.
2. **Ignoring the asymmetry between `_last_sub_send` and `_sub_last_resp_len` updates**.
3. **Overlooking that `broadcast_stream_update` re-evaluates the same condition** — the final `if need_final` call may still skip even if you pass stale values.
4. **Confusing per-instance and queue-level duplicates** — the burst was at the queue level, but the fix is per-instance.

## Quality Checklist

- [ ] Read production code around the change location
- [ ] Read referenced helper functions (streaming.py, stream_publisher.py)
- [ ] Map all scenarios with a truth table
- [ ] Verify edge cases (empty final_resp, fast finalization)
- [ ] Check if existing tests cover the critical scenario
- [ ] Propose minimal, surgical fix if needed
- [ ] Give clear PASS/FAIL verdict with severity ratings

## Extensions

- **For WebSockets**: Consider heartbeat/force-full mechanisms as fallbacks.
- **For gRPC streams**: Similar dedup logic but with different timeout semantics.
- **For real-time dashboards**: May need to prioritize low latency over strict dedup.