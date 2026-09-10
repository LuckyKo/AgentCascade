---
name: streaming-turn-end-frame-dedup-fix
description: Implement a safe conditional final-frame dedup for throttled broadcast pipelines — removes a spurious same-timestamp duplicate at turn end while guaranteeing the final committed state is still delivered (no message loss).
source: auto-generated
version: "1.0.0"
triggers:
  - "turn-end streaming burst"
  - "duplicate frame"
  - "broadcast throttle bypass"
  - "last_send=0.0"
  - "final frame dedup"
  - "spurious duplicate stream_update"
  - "message loss final broadcast"
generated_by: coder
generated_from_task: "FIX A backend turn-end frame dedup for sub-agent streaming burst in AgentCascade core.py _create_and_run_agent"
---

## Goal

Replace an unconditional forced final broadcast (e.g. `last_send=0.0` throttle bypass) with a **safe conditional** that emits exactly one authoritative final frame when needed — eliminating the same-timestamp duplicate without ever dropping the final committed state.

## The trap (why the naive fix is wrong)

A throttled broadcast helper typically broadcasts when `len_changed OR (now - last_send > 0.1)` and, on a **suppressed** tick, returns `(last_send, resp_len)` — i.e. the *length* state updates to latest but the *send-time* stays stale. Consequences:
- **"Skip if len unchanged" is UNSAFE**: if the last tick was throttled out AND the final committed length equals what that tick reported, you DROP the final frame (message loss).
- **Time-check alone is ALSO unsafe**: on a fast turn that ends <100ms after the previous actual send with len unchanged, both time and len checks are False → final frame dropped.

## Procedure

### Step 1 — Track whether each tick was actually suppressed
A suppressed tick returns its `last_send` unchanged. Capture that per tick:
```python
_prev = last_send
last_send, last_len = broadcast_stream_update(..., last_send=last_send, last_resp_len=last_len)
suppressed = (last_send == _prev)   # True => this tick's state never reached the UI
```

### Step 2 — Compute one safe decision after the loop
```python
now = time.monotonic()
need_final = (
    suppressed                                   # last tick throttled out → MUST deliver
    or ((now - last_send) > 0.1)                 # throttle window opened
    or (len(final_resp) != last_len)             # new committed message
)
if need_final:
    broadcast_stream_update(..., last_send=last_send, last_resp_len=last_len)  # REAL values, NOT 0.0
```
Passing the **real** tracked values (not `0.0`) means: if the last tick just sent (<100ms) and nothing changed and it wasn't suppressed → `need_final=False` → skip (no duplicate). If it was suppressed or throttled out → send (guaranteed delivery). The helper re-evaluates the same condition internally, so no double-send.

### Step 3 — Do NOT stagger a *different-instance* final push
If a companion call targets a DIFFERENT instance (e.g. root/caller full snapshot vs the sub-agent frame), it is **not** a per-instance duplicate — leave it unchanged. Staggering only makes sense for two frames of the SAME instance. Document the reasoning in a comment.

### Step 4 — Write regression tests (all four)
1. No two same-instance frames share a timestamp (the core dedup).
2. Final state delivered when last tick throttled out AND >100ms elapsed (time branch).
3. **Fast-turn edge**: last tick suppressed AND loop ends <100ms after previous send, len unchanged → final frame STILL delivered (the flag branch). This is the case that catches a naive fix.
4. The companion/different-instance push still fires.

## Tips

- **Mirror production exactly in the test harness** — replicate the real `need_final` formula and real-value passing; don't approximate, or you'll pass tests that fail in prod.
- **Engine yield cadence**: many engines only emit a UI tick on their own floor (e.g. ≥100ms OR ≥N chunks OR ≥M chars). Mocks that yield too little content produce too few loop ticks — your "2+ ticks" precondition will fail. Give mocks enough delta volume.
- **Inner-loop detector pitfall**: repeated/identical mock content trips generation-loop detection, which can crash on a null `api_router` in tests (`AttributeError: advance_instance_endpoint`). Use unique, non-repeating content in streaming mocks.
- **max_turns notice**: a turn-limit notice may be appended to the final committed message; assert membership (`in`), not `endswith`.
- Keep the diff minimal and local to the execution thread; don't touch throttle/force-full interval logic or the broadcast helper itself.
