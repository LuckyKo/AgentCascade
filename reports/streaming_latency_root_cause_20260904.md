# Streaming Latency Root-Cause Investigation — AgentCascade

**Date:** 2026-09-04 · **Mode:** Investigative (investigation only, no code modified)
**Symptom:** e2e mock-emit → WS-arrival latency grows ~linearly with turn count
(0.078s @ turn 1 → 14.38s @ turn 11). Confirmed reproducible in
`tests/test_streaming_fullstack_e2e.py` (real server + real WS + mock LLM,
~28 chunks/sec, 120 reasoning chunks/turn).

---

## Executive Summary

The O(N)-per-tick cost is **confirmed**. Every streaming tick re-processes the
**entire conversation** of the active instance. There are **four** O(N) passes
per tick, all keyed on the active instance whose conversation length `N` grows
with turn count. Two are the direct root causes; two are amplifiers:

1. **[ROOT] Full re-serialization of the active instance's whole conversation
   every tick** — `state_builder.py:858` (serializes all messages), forced by
   `state_builder.py:211` (active instance never skipped), with no tail
   optimization (`state_builder.py:855-857`) and **no UI cache for Message
   objects** (`state_builder.py:788`), so every message runs a full Pydantic
   `model_dump()` per tick.
2. **[ROOT] Token-stats recomputed every tick** — both the stream-level
   (`streaming.py:404`) and instance-level (`state_builder.py:923`) token-stats
   caches are keyed on the **growing streaming content length**
   (`state_builder.py:466`, `state_builder.py:909`), so the cache misses every
   tick and `get_history_stats()` runs over the full conversation twice per tick.
3. **[AMPLIFIER] Fingerprint dedup set** built over all serialized messages —
   `state_builder.py:869-877`.
4. **[AMPLIFIER] `slice_history_for_llm()`** scanned per token-stats pass —
   `conversation.py:126-197` (called at `streaming.py:400`, `state_builder.py:920`).

**Why latency grows with turn count:** per-tick cost `C ∝ N` (total conversation
content bytes), and `N` grows with each turn. A fixed generator cadence
(0.035 s/chunk) + a per-tick cost that eventually exceeds that cadence causes a
**compounding backlog** — the trailing delay accumulates over the turn and grows
with `N`, hence with turn count.

**Most likely single root cause:** #1 (full re-serialization) is the dominant
per-tick CPU cost; #2 (token-stats) is the second and is exactly what the
rolled-back fix `de0c3b2` tried to touch.

---

## 1. The per-tick broadcast path (call chain)

```
advisor_runner.py:178   broadcast_stream_update(pool, instance_name, turn_output,
                                       is_streaming_tick, ...)        # per engine yield
  └─ streaming.py:261   broadcast_stream_update(...)
       ├─ streaming.py:319-323  should_broadcast = is_streaming_tick OR len_changed OR (now-last_send>0.1)
       ├─ streaming.py:343      force_full = (tick_num % 100 == 0)
       └─ streaming.py:345-350  build_stream_update_from_pool(pool, instance_name, responses, force_full)
            │  (state_builder.py:415)
            ├─ state_builder.py:449-451   conv_snapshot / stream_resp_snapshot (under _compression_lock)
            ├─ state_builder.py:457-467   current_version incl. stream_content_len (L466)  ← changes every tick
            ├─ state_builder.py:474       cache-hit check (current_version == last_version)  ← MISSES every tick
            ├─ state_builder.py:480-483   _calc_stream_token_stats(...)   [recompute, O(N)]
            │      └─ streaming.py:385-416
            │            ├─ streaming.py:400  pool.slice_history_for_llm(combined)  [O(N)]
            │            └─ streaming.py:404  get_history_stats(active_h)           [O(N)]
            ├─ state_builder.py:492-494  _serialize_instances_incremental(pool, instance_name, force_full)
            │      └─ state_builder.py:163-231
            │            ├─ state_builder.py:190-200  version incl. stream_content_len (L199)
            │            ├─ state_builder.py:211  if name==instance_name OR version!=prev OR force_full  ← active ALWAYS re-serialized
            │            └─ state_builder.py:212-218  _serialize_instance(..., streaming=True, streaming_responses=...)
            │                 └─ state_builder.py:806-949
            │                      ├─ state_builder.py:847  full_msgs_snapshot = list(inst.conversation)
            │                      ├─ state_builder.py:858  serialized_msgs = [serialize_message(m,i) for i,m in enumerate(msgs)]  ← O(N) full serialize
            │                      ├─ state_builder.py:869-877  fingerprint set over ALL serialized_msgs  ← O(N)
            │                      ├─ state_builder.py:905-909  cache_key incl. per_agent_stream_content_len (L909)  ← changes every tick
            │                      └─ state_builder.py:918-933  token-stats (L919 miss → L920 slice + L923 get_history_stats)  ← O(N), MISSES every tick
            └─ state_builder.py:572-589  build return dict
```

The active instance is the one with the growing conversation; inactive instances
are correctly skipped by the version cache (`state_builder.py:211` only forces
the active one). So all O(N) work concentrates on the growing instance.

---

## 2. Ranked O(N)-per-tick operations (with citations)

### #1 — Full re-serialization of the active instance's entire conversation  **[ROOT — dominant cost]**

- `state_builder.py:211` — the active instance (`name == instance_name`) is
  **unconditionally** re-serialized on every tick, bypassing the incremental
  cache that protects all other instances.
- `state_builder.py:855-857` — explicit design decision:
  *"Always send all messages — no tail optimization."*
- `state_builder.py:858` —
  `serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]`
  where `msgs = list(inst.conversation)` (`state_builder.py:847`). **Every
  message in the conversation is serialized each tick.**
- `state_builder.py:706-707` — for a `Message` object, `serialize_message` runs
  `d = msg.model_dump()` (full Pydantic dump, cost ∝ message content size).
- `state_builder.py:788-789` — the UI serialization cache is only populated for
  `isinstance(msg, dict) and index > 0`. `Message` is a **Pydantic BaseModel,
  not a dict** (`llm/schema.py:37`, `llm/schema.py:146`), so
  `isinstance(msg, dict)` is **False** → the cache is **never stored** for
  conversation messages → the `id(msg)` lookup at `state_builder.py:691-704`
  always misses → **full `model_dump()` every tick, every message.**

Cost per tick ≈ `sum(model_dump cost over N messages)` ∝ total conversation
content bytes. This is the single largest per-tick cost and scales directly with
conversation length/turn count.

### #2 — Token stats recomputed every tick (two independent full passes)  **[ROOT]**

The prior fix `de0c3b2` (rolled back) targeted exactly this. The current code
**invalidates the cache every tick** because the cache key/version includes the
**growing streaming content length**:

- Stream-level — `state_builder.py:462-467`:
  `current_version = (len(conv), id(conv[-1]), len(stream_resp), stream_content_len)`
  where `stream_content_len` (`state_builder.py:457-460`) grows every tick.
  → `state_builder.py:474` (`current_version == last_version`) is **False every
  tick** → recompute at `state_builder.py:480-483` →
  `streaming.py:404` `get_history_stats(active_h)` over the **full**
  conversation (`streaming.py:400` slices all of it).
- Instance-level — `state_builder.py:905-909`:
  `cache_key = (history_count, id(msgs[-1]), stream_resp_len, per_agent_stream_content_len)`
  where `per_agent_stream_content_len` grows every tick.
  → `state_builder.py:919` (`cache_key not in token_stats`) is **True every
  tick** → recompute at `state_builder.py:920-923`
  (`slice_history_for_llm` + `get_history_stats`) over conversation + streaming.

Both `get_history_stats` calls loop over every message
(`utils/utils.py:1381-1400`) calling `get_message_stats`
(`utils/utils.py:1214`). For `Message` objects the `_tokens` fast path
(`utils/utils.py:1236-1238`) is unavailable (that path is dict-only), so each
call rebuilds an MD5-based key over the full content
(`utils/utils.py:1295-1335`) — O(content) per message even on an LRU hit
(`utils/utils.py:1338-1340`).

**Why the cache was made to miss every tick:** `state_builder.py:453-456` and
`state_builder.py:901-903` — deliberate "BUG31 Fix #4" / "Streaming UI Content
Update Fix" to keep `total_tokens` live as the partial grows. This is the
intentional trade that reintroduced the O(N) per-tick recompute.

### #3 — Fingerprint dedup set over all serialized messages  **[AMPLIFIER]**

- `state_builder.py:869-877` — builds an `existing_fingerprints` set by looping
  over **all** `serialized_msgs` (content + reasoning + function_call + name per
  message). O(N) per tick, on top of #1.

### #4 — `slice_history_for_llm` scan  **[AMPLIFIER]**

- `conversation.py:126-197` — scans the whole history for compression markers
  (O(N)) and, in the unculled branch, does further O(N) work. Called twice per
  tick (`streaming.py:400`, `state_builder.py:920`).

---

## 3. Caching & invalidation analysis

Three caches sit in this path (`api_integration_pkg/cache.py:73-86`):
`stream_token_stats`, `token_stats`, and `ui_serialization`.

| Cache | Key / version | Invalidation behavior | Verdict |
|---|---|---|---|
| `stream_token_stats` (`streaming.py:414`) | version tuple incl. `stream_content_len` (`state_builder.py:466`) | Misses **every tick** (content len grows) | **Too aggressive** |
| `token_stats` (`state_builder.py:931`) | `cache_key` incl. `per_agent_stream_content_len` (`state_builder.py:909`) | Misses **every tick** | **Too aggressive** |
| `ui_serialization` (`state_builder.py:692`) | `id(msg)` | Never populated for `Message` objects (`state_builder.py:788` requires dict) | **Effectively disabled** for the hot path |

So **all three caches are bypassed on the hot path every tick**, which is
precisely why the prior memory `activity_feed_investigation.md` flagged
"`_calc_stream_token_stats() — called every tick due to version mismatch bug`"
and "`stream_content_len` ... grows on every tick → cached token stats are NEVER
reused during active streaming."

This confirms the task hypothesis: the cache is keyed on a raw value
(`stream_content_len`) that changes every tick → **full recompute every tick**.

---

## 4. Throttle analysis (`MIN_STREAM_BROADCAST_INTERVAL`)

There is **no** `MIN_STREAM_BROADCAST_INTERVAL` constant in the codebase. The
only throttle is a hardcoded 100 ms floor:
`streaming.py:322` `or (now_sec - last_send > 0.1)`.

- `streaming.py:319-323`: `should_broadcast = is_streaming_tick or len_changed or (now-last_send > 0.1)`.
- During active streaming `is_streaming_tick=True` **short-circuits** the
  100 ms floor, so a broadcast fires **every chunk** (~every 0.035 s in the
  test) rather than being rate-limited.
- The floor is a **fixed** lower bound; it cannot accumulate or cause growing
  latency. `should_broadcast` never skips ticks in a compounding way — it's a
  one-shot floor OR an event.

**Verdict: the throttle is NOT the cause.** It is a fixed floor and, during
streaming, is effectively bypassed. The growing latency comes entirely from the
O(N) work *inside* each broadcast, not from the dispatch cadence.
(`force_full = tick_num % 100 == 0` at `streaming.py:343` is a periodic full
refresh; it's a bounded ~1-in-100 spike and not the linear driver.)

---

## 5. Why latency grows (roughly) linearly with turn count

- `N` (active instance conversation length / total content bytes) grows with each
  turn (each turn commits a user msg, a large reasoning-heavy assistant msg, and
  tool call/result).
- Per-tick cost `C ∝ N` (operations #1–#4 are all linear in N).
- The generator emits at a fixed cadence `I ≈ 0.035 s` (`test:114
  CHUNK_SLEEP`). Because `is_streaming_tick` bypasses the throttle, one tick is
  attempted per chunk.
- When `C > I`, the backend cannot keep up: a **backlog forms** and the
  trailing delay (time from chunk emission to WS arrival) accumulates over the
  turn at rate ≈ `(C − I)`. The **median** token (mid-turn) trailing ≈
  `(C − I) × (ticks-to-median)` ∝ `C` ∝ `N` ∝ turn count.

> **Honest caveat:** the measured medians span 0.078 s → 14.38 s (~184×) while
> conversation size plausibly grows only ~1–2×. That means the *trailing* is
> amplified well beyond a pure linear-in-N per-tick cost — consistent with the
> **compounding backlog** (once per-tick cost exceeds the chunk cadence, delay
> accumulates nonlinearly over the turn, producing the steep t6→t7 jump). A
> runtime profile is still needed to pin the dominant cost and the exact
> scaling exponent (see Open Questions). The O(N)-per-tick operations above are
> the mechanism; the exact constant/exponent is the remaining unknown.

---

## 6. Root cause vs contributing factors

| Rank | Operation | Citation | Classification |
|---|---|---|---|
| 1 | Full re-serialization of active instance's entire conversation | `state_builder.py:858` + `:211` + `:855-857` + `:788` | **ROOT (dominant)** |
| 2 | Token stats recomputed every tick (2 full passes) | `state_builder.py:466`, `:909`, `:480-483`, `:918-933`; `streaming.py:400-404` | **ROOT** |
| 3 | Fingerprint dedup set over all messages | `state_builder.py:869-877` | Amplifier |
| 4 | `slice_history_for_llm` per pass | `conversation.py:126-197` | Amplifier |
| — | 100 ms throttle | `streaming.py:322` | **Not a cause** (fixed floor, bypassed during streaming) |

---

## 7. Minimal & safe fix recommendation (DO NOT implement — recommendation only)

The fix must be surgical (the two prior fixes were rolled back). Two localized
options, in recommended order:

**Fix A — Tail optimization for the active instance (biggest win, addresses #1/#3).**
In `_serialize_instances_incremental` (`state_builder.py:163-231`) and
`_serialize_instance` (`state_builder.py:806-949`), split the active instance's
output into (a) the **stable history prefix** (unchanged during a turn) and
(b) the **streaming tail** (partial responses). Cache the serialized stable
prefix keyed on `(msg_count, id(last_msg))` and only re-serialize the tail each
tick. This reuses the existing `_cache_mgr.cached_instances` machinery
(`state_builder.py:218-229`) and the existing `is_partial`/`history_count`
client-merge contract (`state_builder.py:863`, `:943`), so it is low-risk. It
converts the per-tick cost from O(N) full-serialize to O(tail).
- *Why safe:* the client already merges partials via `history_count`; only the
  already-on-client stable prefix is reused, never dropped.
- *Guard:* keep the periodic `force_full` (`streaming.py:343`) to recover from
  any sync gap, as it already does.

**Fix B — Split the token-stats cache (safe, addresses #2 — the de0c3b2 intent done right).**
Key `h_stats` (history tokens over the **stable** conversation) on the stable
conversation identity `(msg_count, id(last_msg))` only — **remove
`stream_content_len`** from that key (`state_builder.py:466`, `state_builder.py:909`)
so it is **cached across ticks** within a turn. Recompute only `r_stats`
(the small streaming partial) per tick — it's O(tail) and cheap. This keeps
`total_tokens` live (r_stats still updates) while eliminating the two full
`get_history_stats` passes per tick.
- *Why it avoids the de0c3b2 regression:* de0c3b2 quantized the key into
  ~256-char buckets (caused stale/coarse values → "different behavior").
  Splitting h/r stats instead keeps history stats exact and only refreshes the
  genuinely-changing partial — no quantization, no behavior change.

**Optional (complementary, lowest-risk micro-opt):** extend the `ui_serialization`
cache (`state_builder.py:788`) to also store `Message` objects keyed by `id(msg)`
(stable during a turn). This turns the full `model_dump()` in #1 into a cheap
dict copy. Handle id-reuse/eviction carefully; lower priority than A/B.

**Combined A+B** is the minimal, targeted fix: it removes the two O(N) root
causes while preserving the live-token-count behavior that the naive fix
(breaking the cache entirely) would have lost.

---

## 8. Confidence level

- **O(N)-per-tick operations exist and are the mechanism: Confirmed** (direct code
  reading; corroborated by independent prior memory
  `activity_feed_investigation.md`).
- **Throttle is not the cause: High confidence** (fixed floor, bypassed during
  streaming — verified in code).
- **Which cost is dominant (#1 vs #2): Moderate confidence** (static reasoning;
  `model_dump` over large reasoning content is expected to dominate, but a
  runtime profile would confirm).
- **Exact scaling exponent (why ~184×):** Low confidence — likely backlog
  compounding, but not measured.

## 9. Open questions

1. What fraction of per-tick time is serialization (#1) vs token stats (#2) vs
   fingerprint (#3)? → **Action:** run a `cProfile`/`py-spy` on one streaming
   turn at high turn count; compare `serialize_message`, `get_history_stats`,
   `slice_history_for_llm`.
2. Does the test's conversation exceed the `token_stats` LRU (512,
   `cache.py:84`) causing `qwen_count` tokenization to re-run every tick?
   (Seed ~211 msgs + streaming — likely under, but confirm.)
3. Is `instance.conversation` ever mutated in place for committed messages
   (would invalidate an id-based UI cache)? → verify before Fix A's optional part.

## 10. Suggested next actions

1. Profile one high-turn streaming run to confirm the dominant cost and scaling.
2. Implement **Fix B** first (smallest, directly targets the rolled-back
   de0c3b2 area, low risk) and re-run the e2e test.
3. If latency is still dominated by serialization, implement **Fix A**
   (tail optimization) and re-run.
4. Keep `force_full` (`streaming.py:343`) as the safety net for sync recovery.

---

## Key file:line index

- `agent_cascade/api_integration_pkg/streaming.py:261` `broadcast_stream_update`
- `agent_cascade/api_integration_pkg/streaming.py:319-323` throttle (100 ms floor)
- `agent_cascade/api_integration_pkg/streaming.py:343` `force_full = tick_num % 100`
- `agent_cascade/api_integration_pkg/streaming.py:385-416` `_calc_stream_token_stats`
- `agent_cascade/api_integration_pkg/state_builder.py:415` `build_stream_update_from_pool`
- `agent_cascade/api_integration_pkg/state_builder.py:457-467` version incl. `stream_content_len`
- `agent_cascade/api_integration_pkg/state_builder.py:474` cache-hit check
- `agent_cascade/api_integration_pkg/state_builder.py:163-231` `_serialize_instances_incremental`
- `agent_cascade/api_integration_pkg/state_builder.py:211` active instance always re-serialized
- `agent_cascade/api_integration_pkg/state_builder.py:806-949` `_serialize_instance`
- `agent_cascade/api_integration_pkg/state_builder.py:858` full-serialize all messages
- `agent_cascade/api_integration_pkg/state_builder.py:869-877` fingerprint set
- `agent_cascade/api_integration_pkg/state_builder.py:905-933` token-stats cache (misses every tick)
- `agent_cascade/api_integration_pkg/state_builder.py:788-789` UI cache store (dicts only)
- `agent_cascade/utils/utils.py:1214` `get_message_stats`
- `agent_cascade/utils/utils.py:1381-1400` `get_history_stats`
- `agent_cascade/pool/conversation.py:126-197` `slice_history_for_llm`
- `agent_cascade/llm/schema.py:37`, `:146` `BaseModelCompatibleDict`/`Message` (Pydantic, not dict)
- `agent_cascade/advisor_runner.py:178-188` per-tick broadcast call site
- `tests/test_streaming_fullstack_e2e.py:109`, `:114` 120 reasoning chunks, 0.035 s cadence
