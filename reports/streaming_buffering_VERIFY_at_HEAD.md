# Verification: Streaming-Buffering Root-Cause Report vs. Current HEAD

**Date:** 2026-08-31 | **Verifier:** stream_verify2 | **Mode:** Read-only verification (no files modified)
**Repo:** N:\work\WD\AgentCascade @ HEAD = `ce9570a` (2026-08-31)
**Prior report:** `reports/streaming-buffering-root-cause.md` @ `7bf12c2` (2026-08-23)

---

## Executive Summary

The prior root-cause report is **still accurate at current HEAD**. All three cited mechanisms
(H1-Factor1 full-conversation serialization, H1-Factor2 per-chunk throttle bypass, H2
self-invalidating token-stats cache keys) are present verbatim in current code — line numbers
shifted slightly (package grew), but no logic was changed. **No partial fix landed** since
7bf12c2: no tail optimization, no cache-key quantization, no min-interval guard. The only
streaming-path changes in `7bf12c2..HEAD` are (a) removal of the `approvals` field from
stream updates (ce9570a, neutral-to-slightly-reducing on payload size) and (b) pause-decoupling
frontend wiring (09c318d), neither of which touches serialization sizing, throttling, or the
splice merge. The prior 3-item fix proposal remains valid and needs no adjustment, with one
new caveat: the frontend now has a stale-update guard (`_lastHistoryCount`, app.js:2024) that a
restored tail must coexist with — it does (strict `<` comparison, constant `history_count`
across partials passes).

---

## 1. Per-Factor Verification Table

| Factor | Verdict at HEAD | Current evidence (file:line) |
|---|---|---|
| **H1-Factor1** — every stream_update serializes full conversation, no tail optimization | **STILL TRUE** | `state_builder.py:841-844` — comment "Always send all messages — no tail optimization", `start_idx = 0`, `serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]` over the full snapshot. Docstring at `:799-804` reaffirms "All messages are always sent — no tail optimization applied". No threshold/tail logic exists anywhere in `_serialize_instance` (verified by grep for `start_idx`, `tail` in state_builder.py). |
| — active instance force-reserialized every broadcast | **STILL TRUE** | `state_builder.py:197` — `if name == instance_name or current_version != prev_version or force_full:` (unchanged line number). |
| — all instances' cached message arrays embedded in every frame | **STILL TRUE** | `state_builder.py:478-480` (`build_stream_update_from_pool` calls `_serialize_instances_incremental`), frame dict embeds `'instances': all_instances` / `'agent_instances': all_instances` at `:559-560` (was 544-546; shifted by +15). |
| **H1-Factor2** — per-chunk broadcast bypasses 100ms throttle via OR short-circuit | **STILL TRUE** | `streaming.py:91-95` — `should_broadcast = (is_streaming_tick or len_changed or (now_sec - last_send > 0.1))`; `is_streaming_tick=True` short-circuits. Caller `run_agent_unified.py:202-211` passes `is_streaming_tick=is_streaming_tick or has_tool_event` (line numbers unchanged). Engine yields `(response+turn_output+partial_msgs, True)` per tick at `core.py:695` (was 645-668; shifted); per-chunk `yield None` at `llm_call.py:743` (was 548; shifted — 548 is now a retry-branch yield; the consume loop `for output in gen` is at `:564`). |
| **H2** — token-stats cache/version keys keyed on raw `stream_content_len` ⇒ miss every chunk | **STILL TRUE** | `state_builder.py:443-453` — version tuple `(len(conv), id(last), len(stream_resp), stream_content_len)` with raw `stream_content_len = sum(len(content)+len(reasoning) …)` at `:443-446` (was 441-446). `state_builder.py:891-895` — `cache_key = (original_history_count, id(msgs[-1]), stream_resp_len, per_agent_stream_content_len)` with raw per-agent content length at `:891-894` (was 873-878). No quantization/bucketing anywhere. Miss ⇒ `_calc_stream_token_stats` (`streaming.py:143-173`, full-conversation `get_history_stats` at `:157-163`) runs per chunk. |

Line-shift note: state_builder.py citations shifted ~+15-17 lines (file now 1160 lines);
streaming.py, run_agent_unified.py, core.py, llm_call.py shifts vary (engine loop code moved
with loop-detect/retry features). **No semantic change in any cited mechanism.**

---

## 2. What Changed in the Streaming Path Since 7bf12c2

`git log 7bf12c2..HEAD` = 96 commits. Of these, only 2 touch the streaming data path in a way
relevant to this bug; the rest touch prompts, compression, shell, router, or token-estimator
accuracy:

| Commit | Effect on streaming path | Impact on bug |
|---|---|---|
| **ce9570a** (HEAD, 2026-08-31) "stop stale stream ticks from clobbering approval banner" | Removed `approvals` field from stream updates: dropped `pending_approvals = _get_approvals(pool)` call and the dict key (state_builder.py diff, 9 lines changed). Approvals now delivered only via dedicated `{'type':'approvals'}` WS message. | **No serialization-sizing change.** Slightly *reduces* per-frame payload by one field; does not touch message arrays, throttle, or cache keys. Confirmed by diff review. |
| **09c318d** (2026-08-30) "decouple global pause from per-instance halt" | Frontend (app.js +43 lines) wires a global `paused` field and invalidates the stream cache on pause/resume/stop/reset transitions; backend adds stream-cache invalidation on `_paused`-change paths. | **No effect** on payload size, throttle, or cache-key invalidation cadence during normal streaming. |
| 109d20d, 6398032 (token estimator fixes) | Token *estimator* now counts reasoning_content + tool_calls wire format. | Changes token *count accuracy*, not cache-key behavior; H2 (miss every chunk) unaffected. |
| 1ae1af6 / 40ae59e (JSONL destruction fix) | Touches compression cache rebuild, not stream_update build path. | None. |
| All other ~90 commits | Prompts, compression, shell, router, loop-detect, Skill Advisor, tests. | None on the stream_update build/serialize/broadcast path (verified via `git log -- <streaming files>`; none implement tail, quantization, or min-interval). |

**Partial-fix check result: NO partial fix landed.** Specifically absent at HEAD:
- no tail/threshold logic in `_serialize_instance` (grep: only the "no tail optimization" comment),
- no quantized/bucketed `stream_content_len` in either version tuple or cache key,
- no min-interval guard in `broadcast_stream_update` (still pure OR at streaming.py:91-95).

---

## 3. Frontend Splice Compatibility (Check 5)

**STILL TRUE — the restored tail would merge correctly.** Current merge logic at `web_ui/app.js:2017-2044`
(report cited ~2007-2019; shifted +10):

```js
if (sa.is_partial) {
  const hCount = sa.history_count || 0;
  if (hCount < (existing._lastHistoryCount || 0)) { ...stale guard... }
  else {
    const startIdx = hCount - sa.messages.length;
    if (startIdx >= 0) {
      if (startIdx > existing.messages.length) existing.messages = [...sa.messages];
      else { existing.messages.length = startIdx; existing.messages.push(...sa.messages); }
    } else { existing.messages = [...sa.messages]; }  // rollback: full replace
  }
}
```

`startIdx = hCount - sa.messages.length` is exactly the invariant the tail fix relies on
(`history_count` = total length incl. appended streaming responses; `messages` = tail + stream
partials). **New since the report:** a stale-update guard (`_lastHistoryCount`, app.js:2024-2030)
skips message-array replacement when `hCount <` the last seen value, but uses strict `<` and syncs
metadata only. A restored tail keeps `history_count` constant across partials of the same
conversation (it is total length, not tail length), so updates pass the guard and splice correctly.
No compatibility blocker identified.

---

## 4. Conclusion

| Item | Status |
|---|---|
| Prior report still accurate at HEAD? | **YES** — all three mechanisms verbatim present; only line numbers shifted. |
| Partial fix since 7bf12c2? | **NO.** ce9570a (approvals removal) is the only stream-update payload change; it is sizing-neutral (slightly smaller). 09c318d affects pause-state wiring only. |
| Prior fix proposal (tail + quantized keys + min-interval) still right? | **YES — no adjustment needed**, with two implementation notes: |
| Note 1 | The frontend's new `_lastHistoryCount` stale guard (app.js:2024) is compatible: strict `<` + constant `history_count` across partials. Keep the backend contract that `history_count` = total conversation+partials length (already enforced at state_builder.py:924-929). |
| Note 2 | ce9570a removed `approvals` from stream updates — when implementing the tail, do not re-add per-frame approval computation; approvals are now exclusive to the `_approval_loop` dedicated message. |

Ranked fix recommendation unchanged from the prior report:
1. **Restore proportional streaming tail** in `_serialize_instance` (state_builder.py:841-844) for `streaming=True` when `len(msgs) > threshold` (e.g. >50 → ~10% tail, min 5), using absolute indices — biggest win, smallest change, frontend already compatible (app.js:2032-2040).
2. **Quantize `stream_content_len`** (e.g. `// 512`) in both version tuples (state_builder.py:443-453) and the per-instance cache key (state_builder.py:891-895) to stop per-chunk full-history token estimation.
3. **Min-interval floor** for streaming-tick broadcasts (streaming.py:91-95), e.g. only bypass the 100ms throttle when `now - last_send > 0.05`.

## Confidence & Open Items

- **Confidence: High** — every check verified against current source; commit range fully enumerated (96 commits) and streaming-file diffs inspected.
- Verified: all 5 task checks.
- Not verified (out of scope, unchanged from prior report): production timing profile (no instrumentation run); llama.cpp-side batching contribution (H4, low likelihood, untested).
