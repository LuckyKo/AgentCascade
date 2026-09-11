# Implementation Plan: Additive / Delta Streaming Refactor

**Project:** AgentCascade (`N:\work\WD\AgentCascade`)
**Prepared:** 2026-09-05 | **Status:** Approved — simplified per ponytail review (v2)
**Basis:** `research_additive_streaming.md` + `ponytail_review_additive_streaming.md`

> **Simplification note (v2):** Per ponytail's review, phase 1 is reduced to the minimum viable delta mode:
> - `_conv_version` counter → replaced by 4-line `prefix_shrank` check (no `AgentInstance` changes)
> - `request_state` protocol → cut (drop frame, let force_full resync)
> - R3 in-place fast path → deferred (measure flicker first)
> - Index cross-check (§2.4) → cut (caught by e2e tests)
> - `TAIL_COMMITTED` env var → hardcoded to 1
> 
> **Phase 1 scope: ~60 lines, touching only `state_builder.py` + 5 lines in `app.js`.**

---

## 0. Summary of the Change

Today every `stream_update` frame re-sends the **entire committed conversation + streaming partials** for the active instance (`start_idx = 0`, `state_builder.py:948`). Payload and `json.dumps` cost grow linearly with conversation length (observed 59KB → 378KB, 63ms → 3.2s).

The frontend **already supports tail-only frames**: the partial-merge path computes `startIdx = history_count - messages.length` and splices (`app.js:2051, 2071-2073`). The backend simply never exercises that branch.

**Core change:** in `_serialize_instance`, when the frame is a *partial* delta (streaming active, not `force_full`), send only a small **tail** (last committed message + growing streaming partials) while keeping `history_count = total`. Force-full frames (every 100 ticks) and connect-time `state` frames stay full.

**Safety model:** consistency is anchored on the two existing full-frame mechanisms — the connect-time `state` push (`api_server.py:1258`) and the periodic `force_full` every 100 ticks (`streaming.py:343`). Deltas in between are best-effort; a desync self-heals at the next full frame (≤ ~10s).

**Rollout:** env-gated feature flag `AGENT_CASCADE_STREAM_DELTA=1`, default OFF, flip after e2e validation.

---

## 1. Backend Changes — `agent_cascade/api_integration_pkg/state_builder.py`

### 1.1 The tail cut in `_serialize_instance` (lines 897–1038)

**Current code (lines 943–949):**
```python
msgs = full_msgs_snapshot
original_history_count = len(msgs)

# Always send all messages — no tail optimization. ...
start_idx = 0
serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]
```

**New logic:** compute `start_idx` from a *safe* boundary, then slice.

```python
msgs = full_msgs_snapshot
original_history_count = len(msgs)

# Delta streaming (AGENT_CASCADE_STREAM_DELTA=1): on partial frames send only a
# safe tail instead of the full history. force_full / connect-time frames stay full.
use_delta = STREAM_DELTA_ENABLED and streaming and original_history_count > 0
start_idx = _safe_tail_start_index(msgs) if use_delta else 0

serialized_msgs = [
    serialize_message(m, i)
    for i, m in enumerate(msgs[start_idx:], start=start_idx)   # NOTE: absolute indices!
]
```

**Critical detail — absolute `index` fields:** the frontend's merge is purely positional via `history_count - messages.length`, and message objects carry an `index` field. We must keep **absolute** indices (`enumerate(msgs[start_idx:], start=start_idx)`), so a tail frame's first message has `index == start_idx`. This makes the client able to cross-check (see §2.4) and keeps any future id-based logic consistent.

### 1.2 Safe boundary rule (risk R6 — tool-call pair integrity)

A tail cut must never split an assistant message carrying `function_call`/`tool_calls` from its following `tool`/`function` response(s).

**Do NOT reuse `_find_user_message_insertion_point`** (`state_builder.py:679-746`) for this. Review found it is semantically wrong here: it returns where a *new user message* could be inserted, and its backward walk stops at the **first user message**, which can sit *inside* the last tool-call chain. Example — conversation ending `[..., user, assistant(tool_calls), tool]` → it returns the index of that `user`; clamping to `len-1` then yields a tail of just `[tool]`, splitting the pair. We need a dedicated scan that stops at the **start of the last tool-call chain**.

```python
def _safe_tail_start_index(msgs: list) -> int:
    """Index of the first message to include in a delta tail (R6-safe).

    Walks backwards from the end over tool/function responses and assistant
    messages carrying tool_calls/function_call, stopping at the start of the
    last tool-call chain (`boundary`) or just after a plain user/assistant
    message. The desired cut `c = len(msgs) - TAIL_COMMITTED` is safe only if it
    does not fall strictly INSIDE the last tool chain, i.e. only when
    `c <= boundary`. Otherwise we must widen the tail to include the whole
    chain (`start_idx = boundary`) so no call/response pair is split.

    Returns 0 (full send) when the entire conversation is one unbroken chain,
    or when there is nothing to cut (len <= TAIL_COMMITTED).
    """
    if TAIL_COMMITTED <= 0:
    return 0                                      # misconfig: always send full
    if not msgs or len(msgs) <= TAIL_COMMITTED:
        return 0                                      # guard: never a negative start_idx
    i = len(msgs) - 1
    while i >= 0:
        msg = msgs[i]
        role = (msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '') or '').lower()
        if role in ('tool', 'function'):
            i -= 1                                   # tool response: walk to its call
            continue
        if role == 'assistant':
            fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
            tc = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
            if (fc is not None) or (isinstance(tc, list) and len(tc) > 0):
                i -= 1                               # assistant with calls: part of the chain
                continue
        break                                         # user / plain assistant / unknown: safe stop
    boundary = i + 1                                  # first index of last tool chain (or after a safe msg)
    c = len(msgs) - TAIL_COMMITTED                    # desired cut (tail of exactly T committed msgs)
    return c if c <= boundary else boundary           # never cut inside the chain
```

Properties (rule **simulated and verified** against all shapes below — no pair ever split):

| Conversation ending | len | start_idx | tail | Splits pair? |
|---|---|---|---|---|
| `[..., user, assistant]` (plain) | 2 | 1 | 1 | No |
| `[..., user, assistant(tool_calls), tool]` | 3 | 1 | 2 (`[assistant, tool]`) | No |
| `[..., user, asst(tc), tool, asst(tc), tool]` (chain of 4) | 5 | 1 | 4 (whole chain) | No |
| `[..., asst(plain), user, asst(tc), tool]` | 5 | 3 | 2 (`[assistant, tool]`) | No |
| one unbroken 20-msg chain from index 0 | 20 | 0 | 20 (full send) | No |
| single message | 1 | 0 | 1 | No |
| legacy `function_call` + `function` response | 3 | 1 | 2 | No |

- **Tail size** = `len(msgs) - start_idx` committed + N streaming partials → normally **1 committed + 1 growing partial** (2 messages); widens to the last tool chain when the cut would fall inside it.
- Worst case (one giant unbroken chain): tail = whole conversation → same payload as today, no regression, no split.
- Cost: O(last-chain length) backwards scan per tick — negligible vs the json.dumps we're removing.

> ⚠️ **Implementation note:** this two-line rule (`c if c <= boundary else boundary`) is the trickiest part of the plan. The unit tests in §4 (case 4) encode exactly the table above — write them FIRST and let them pin the rule.

**Why "last committed + streaming partials" is enough:** every delta frame is followed by either another delta (same tail anchor) or a full frame. The client already holds the prefix from the previous full frame; deltas only ever touch the last committed message and the in-flight partial(s). A newly committed message becomes the *new* last committed message and is included in the very next frame.

### 1.3 `history_count` — unchanged (line 1032)

```python
result.update({
    'messages': serialized_msgs,                      # now possibly a tail
    'history_count': original_history_count + num_streaming,   # TOTAL — unchanged
    ...
})
```

`original_history_count = len(full_msgs_snapshot)` is computed **before** the slice (line 944) and is unaffected by the cut. The client's `startIdx = history_count - messages.length` therefore lands exactly on `start_idx`. **No other backend field changes.**

### 1.4 Streaming-partial append + fingerprint dedup (lines 956–985) — works unchanged, one note

The dedup loop builds `existing_fingerprints` from `serialized_msgs` (now the tail only). A streaming partial whose fingerprint matches a message in the *prefix* (not in the tail) would no longer be deduped and could be appended as a duplicate. In practice this cannot happen: `_streaming_responses` holds only the **current turn's** in-flight messages, and the current turn's assistant message is by definition at the end of the conversation — inside the tail. So dedup behavior is preserved. Add a code comment documenting this invariant so nobody "optimizes" the tail to exclude the last committed message later.

### 1.5 `serialize_message` cache (lines 748–885) — no change

The `id(msg)` UI cache is untouched: we simply call it for fewer messages. Note the cache only stores entries for `index > 0` (line 879); with absolute indices, tail messages keep their real index, so caching behavior is identical to today. **Do not** pass relative indices — that would silently change what gets cached and break cross-frame consistency.

### 1.6 Non-active instances — no change

`_serialize_instances_incremental` (lines 243–311) reuses `cached_instances[name]` for unchanged non-active instances. Those cached dicts were built by the same `_serialize_instance`; if they were built during a delta frame they contain a tail, and that's fine — a non-streaming instance's conversation isn't changing, and its next full re-serialization happens on `force_full`. No change needed here.

### 1.7 Feature flag

```python
# state_builder.py, module level (next to _STREAM_TIMING_ENABLED, ~line 31)
import os as _os_delta
STREAM_DELTA_ENABLED = _os_delta.environ.get("AGENT_CASCADE_STREAM_DELTA") == "1"
TAIL_COMMITTED = 1  # hardcoded for phase 1; add env var if tuning needed later
```

Read at import time (consistent with `_STREAM_TIMING_ENABLED`). No runtime toggling needed for phase 1.

### 1.8 Prefix-shrink detection (replaces `_conv_version` — simplified per ponytail review)

- `force_full = tick_num % 100 == 0` (`streaming.py:343`) already gates full frames; `build_stream_update_from_pool(force_full=...)` → `_serialize_instances_incremental(streaming=(not force_full))` → delta cut only applies when `streaming=True`.
- **Compression/rollback detection (4 lines, no `AgentInstance` changes):** In `_serialize_instances_incremental`, detect when the conversation *shrank* (compression or rollback) and force that frame to be full:

```python
# In _serialize_instances_incremental, after computing current_version:
prefix_shrank = (
    prev_version is not None
    and current_version[0] < prev_version[0]  # message count decreased
)
if name == instance_name or current_version != prev_version or force_full:
    full_this_frame = force_full or prefix_shrank
    all_instances[name] = _serialize_instance(
        inst, pool, include_messages=True,
        streaming=(not full_this_frame),          # False => no tail cut + is_partial from responses
        streaming_responses=inst_streaming_responses,
    )
```

**Why this is sufficient for phase 1:** Compression always shrinks the conversation (that's its purpose — free context window). Rollback also shrinks. Both are caught by `current_version[0] < prev_version[0]`. The one theoretical gap (same-length rewrite) is practically impossible in our compression implementation, and even if it occurred, the 100-tick force_full self-heals within ~10s.

**If same-length compression ever becomes a problem:** add `id(msgs[0])` to the version tuple (one line) — a same-length rewrite almost certainly changes the first message's identity. No counter needed.

**Clarification:** `streaming=False` here only disables the tail cut. `is_partial` is set from `len(stream_responses) > 0` (`state_builder.py:954`) *independently* of the `streaming` param — so a forced-full mid-stream frame is sent with the **full list AND `is_partial=True`**. The client's partial merge then sees `startIdx = 0`, same length → in-place update (line 2056 branch). Correct and flicker-free.

---

## 2. Frontend Changes — `web_ui/app.js`

### 2.1 R2 fix: initialize `_lastHistoryCount` in the `state`/`done` handler (line ~1827)

**Current:**
```js
for (const [name, sa] of Object.entries(data.agent_instances)) {
  state.subAgents[name] = sa;          // _lastHistoryCount never set here
}
```

**New:**
```js
for (const [name, sa] of Object.entries(data.agent_instances)) {
  sa._lastHistoryCount = sa.history_count || 0;   // R2: anchor staleness gate on full frames
  state.subAgents[name] = sa;
}
```

Today this "works by accident" via the `|| 0` fallback (`app.js:2043`); with deltas a wrong anchor would mis-route splice/replace decisions, so set it explicitly. (The non-partial stream_update path already sets it at lines 2116/2118/2121 — consistent.)

### 2.2 R3 fast path — DEFERRED to phase 1.5

With a tail of 1 committed + 1 streaming partial, `startIdx = hCount - 2` and the common case is "same history_count, last message grew" — today's full-send code hits the in-place `Object.assign` branch (line 2056), but with a tail it falls into the splice branch (2071) which **replaces** the tail message objects → new references → DOM re-render/flicker per token.

**Decision (ponytail review):** This is a *visual* issue, not a correctness issue. The splice path produces correct content. Ship without R3 in phase 1. Measure flicker in Phase B manual testing. If noticeable, add the 5-line in-place `Object.assign` fast path as a follow-up commit.

**Review note:** do NOT special-case hardcoded tail lengths (`sa.messages.length === 1/2`) — multiple streaming partials make the tail size variable, and a condition like `startIdx === existing.messages.length - 1` compares an *absolute* index against the client's *total* message count, which is wrong. The correct general condition: **the frame's tail exactly covers the end of what the client already has** → update those slots in place instead of truncate+push:

```js
if (startIdx >= 0) {
  if (startIdx > existing.messages.length) {
    // Server ahead — full replace (existing safety net, unchanged).
    existing.messages = [...sa.messages];
    } else if (startIdx > 0 && startIdx + sa.messages.length === existing.messages.length) {
    // R3 fast path (TAIL FRAMES ONLY): tail covers exactly the last N slots we already hold
    // (growing streaming partial, or commit+partial with constant history_count).
    // In-place Object.assign keeps object references stable -> no DOM teardown.
    // Restricted to startIdx > 0 so full frames (startIdx=0) use their existing per-case logic.
    for (let i = 0; i < sa.messages.length; i++) {
      const tgt = existing.messages[startIdx + i];
      if (tgt) Object.assign(tgt, sa.messages[i]);
    }
  } else if (startIdx === 0 && sa.messages.length === existing.messages.length) {
    // ... existing full-send in-place branch (line 2056) — unchanged ...
  } else if (startIdx === 0 && sa.messages.length === existing.messages.length + 1) {
    // ... existing commit-append branch (line 2063) — unchanged ...
  } else {
    existing.messages.length = startIdx;
    existing.messages.push(...sa.messages);
  }
}
```

Placement: the new `startIdx + sa.messages.length === existing.messages.length` branch goes **immediately after** the `startIdx > existing.messages.length` guard and **before** the two `startIdx === 0` branches. It is mutually exclusive with them in practice (when `startIdx === 0` it degenerates to "same length" — same outcome as line 2056, so ordering doesn't change behavior for full frames), but putting it first keeps the delta path explicit.

Edge behavior:
- **New message committed** (`history_count` grows by 1, tail = [new committed, partial]): `startIdx + len < existing.length`? No — client total is now one short of the frame's end → falls to the generic splice (truncate to startIdx, push tail) → correct, and this happens once per turn boundary, not per token.
- **Dropped/desynced frame**: `startIdx > existing.messages.length` or mismatched lengths → existing replace/splice paths handle it.

### 2.3 R1 guard: partial frame without a prefix → resync (lines 2091–2094)

**Current fallback:**
```js
} else {
  // Fallback: if we don't have existing state, we can't merge partials
  state.subAgents[name] = sa;
}
```

A delta frame (`startIdx > 0`) adopted here would become the *entire* message list → lost prefix. The initial `state` frame is sent first in `ws_chat` (`api_server.py:1258`) so this shouldn't happen, but guard anyway:

```js
} else {
  const startIdx = (sa.history_count || 0) - (sa.messages?.length || 0);
  if (startIdx > 0) {
    // Delta frame but we have no prefix — dropping it; will self-heal at next full frame.
    console.warn(`[WS] ${name}: delta without prefix (startIdx=${startIdx}), dropping`);
  } else {
    state.subAgents[name] = sa;
    sa._lastHistoryCount = sa.history_count || 0;
  }
}
```

**No `request_state` protocol message.** The next force_full (≤10s) or any user action triggering `_broadcast()` will resync. No new backend handler needed.

### 2.4 Index cross-check — CUT for phase 1

The `index` field drift scenario is a developer error caught by e2e tests (test case 6: absolute index check). Not worth adding runtime protocol complexity. If indices are wrong, the splice produces garbage → visible in e2e tests → caught before deployment.

### 2.5 Verify existing branches with smaller payloads

- **`startIdx > existing.messages.length` (line 2054):** still correct — full replace when server is ahead. With deltas this now also fires on a dropped-prefix situation → self-heals.
- **`startIdx < 0` (line 2075–2077):** rollback rescue, unchanged; backend §1.8 makes rollbacks emit full frames anyway.
- **Non-partial merge (lines 2095–2123):** untouched — force_full and completion frames still carry the full list (`is_partial=False` → `streaming=False` → no tail cut).
- **R9:** a connect-time `state` frame captured mid-token can have `is_partial=True` with a *full* list. Client treats `case 'state'` as unconditional replace (line 1827) — still correct, no change.

---

## 3. Optional Phase 2: Per-instance `seq` field

**Purpose:** explicit stale-drop and resync-on-gap (R7/R8), replacing the fragile `history_count < _lastHistoryCount` gate which misbehaves on rollback (shrunken count looks "stale").

### Backend
- Add a monotonically increasing counter per instance, e.g. in `CacheManager`: `_cache_mgr.instance_seq: Dict[str, int]`, incremented under `_cache_mgr._lock` every time `_serialize_instances_incremental` re-serializes an instance (both the fresh and cache-miss paths).
- Emit it in `_serialize_instance`'s result: `result['seq'] = <n>` (include on full frames too — same counter).

### Frontend
- Track `existing._lastSeq`. In the partial merge, replace the stale gate:
```js
if (typeof sa.seq === 'number') {
  if (sa.seq <= (existing._lastSeq || 0)) { /* stale — metadata only */ }
  else if (sa.seq > (existing._lastSeq || 0) + 1) { send({type:'request_state'}); /* gap → resync */ }
  existing._lastSeq = sa.seq;
}
```
- Keep the `history_count` gate as fallback for old servers (flag OFF).

**Decision:** defer to phase 2. The 100-tick force_full already bounds desync recovery to ~10s, and §1.8 makes rewrites emit full frames. Implement only if we observe stale-drop or gap issues in production.

---

## 4. Edge Cases & Tests

### Edge cases (design-time answers)

| Case | Behavior with this plan |
|---|---|
| **Compression mid-stream (R5)** | Conversation shrinks/rewrites under `_compression_lock` → version tuple changes (§1.8 detects `len < prev` or same-len different last-id) → that frame is emitted **full** (`streaming=False`, but `is_partial` stays true if a partial is in flight — see §1.8 clarification). Client merges via the partial path with `startIdx = 0`; no prefix assumption violated. |
| **Rollback (R8)** | `del self.conversation[new_len:]` (`agent_instance.py:489`) shrinks history → same §1.8 detection → full frame. Client's `startIdx < 0` replace (line 2075) remains as belt-and-braces. |
| **Tool-call pair at tail boundary (R6)** | `_safe_tail_start_index` (§1.2) never cuts strictly inside the last tool-call chain — verified against 7 conversation shapes in the §1.2 table; tail = last committed + partials, widened to the whole last chain when needed. |
| **QueueFull drop of a delta (R7)** | Client is missing ≤ 1 frame; next force_full (≤ ~10s) or next commit fixes it. Bounded and pre-existing (same as today). `seq` (§3) would shorten this if ever needed. |
| **Empty conversation / first message** | `original_history_count == 0` → `use_delta=False` → full (empty) send. First user message frame: tail = that message, fine. |
| **Multiple streaming partials** (`num_streaming > 1`) | All appended after the tail; `history_count - messages.length` still equals `start_idx`. Client splice handles N≥1 identically. |
| **Non-active instance cached from a delta frame** | Its conversation is static; stale-tail dict is only reused until version change or force_full. Harmless — verify with test below. |
| **`index` field on tail messages** | Absolute indices preserved (§1.1) → frontend cross-check (§2.4) passes; `serialize_message` caching (line 879, `index > 0`) unaffected. |

### Unit tests — new file `tests/test_state_builder_tail_cut.py`

Use lightweight fakes: a stub instance with `_compression_lock`/`_state_lock` (RLock), `conversation`, `_streaming_responses`, and the minimal pool attrs `_serialize_instance` touches (`is_instance_halted`, `has_messages`, `get_queue_messages`). Cases:

1. **Tail cut on:** flag ON, 50 committed msgs + 1 streaming partial → `len(result['messages']) == 2` (last committed + partial), `history_count == 51`, first message `index == 49`.
2. **Flag OFF** → same setup → `len(messages) == 51`, `start_idx` behavior identical to today (regression guard).
3. **force_full / streaming=False** with flag ON → full list sent (`streaming` param False → no cut), but `is_partial` still reflects live `_streaming_responses`. This is the connect-time path.
4. **Tool-pair integrity — table-driven:** encode the §1.2 verification table as a parametrized test over conversation shapes (plain end, pair at end, 4-msg chain, plain-then-chain, giant unbroken chain, single msg, legacy `function_call`). For each: assert (a) no call/response pair is split across the tail boundary, (b) `start_idx == expected` from the table, (c) `0 <= start_idx <= len - TAIL_COMMITTED`.
5. **history_count invariant:** across all of the above, `history_count == len(committed) + num_streaming` regardless of tail size.
6. **Absolute index check:** for every sent message, `msg['index'] == position in full conversation` (catches a relative-index regression that would also break `serialize_message` caching at line 879).
7. **Dedup invariant:** a streaming partial whose content equals the last committed message's (Phase-4 commit race) is not double-appended — and note this relies on the last committed message being *inside* the tail (documented invariant, §1.4).
8. **Rollback during streaming:** shrink conversation by 3 messages mid-stream (simulating `del conversation[new_len:]`). Assert: length decreased → `prefix_shrank` triggers full frame (`streaming=False`). Client's `startIdx < 0` rescue remains as belt-and-braces.
9. **Compression during streaming (normal shrink):** simulate compression that removes 5 messages and inserts 1 marker (net -4). Assert: length decreased → `prefix_shrank` triggers full frame. Verify the client receives the complete compressed list.

> Note: same-length rewrite (remove N, insert N) is NOT caught by `prefix_shrank` — this is an accepted gap (practically impossible; force_full self-heals ≤10s). Not tested in phase 1.

### E2E — existing `tests/test_streaming_fullstack_e2e.py`

**Why it passes unchanged with delta frames ON:**
- `_live_assistant_msg` (lines 810–820) scans the instance's `messages` for the **last assistant message** — the growing partial is always in the tail → latency measurement (§`_measure_latency`, lines 873–938) unaffected.
- Turn segmentation uses `is_partial` transitions (lines 836, 961) — unchanged semantics.
- The Node harness runs **real app.js** over the live socket (line 1160) → exercises the new tail-splice + R3 fast path end-to-end; `ok`/`ws_closed` assertions catch merge regressions.

**Additions to make it actually *verify* delta mode:**
1. Run the full test **twice** — once with `AGENT_CASCADE_STREAM_DELTA=0`, once with `=1` (parametrize or a second fixture env). Both must pass identically.
2. In delta mode, add explicit assertions on captured frames (the existing `_measure_*` helpers only *report* — nothing asserts):
   ```python
   DELTA_MODE = os.environ.get("AGENT_CASCADE_STREAM_DELTA") == "1"
   TAIL_COMMITTED = int(os.environ.get("AGENT_CASCADE_STREAM_TAIL", "1"))
   MAX_STREAMING_PARTIALS = 2          # observed max of len(_streaming_responses) in practice; raise if it changes
   if DELTA_MODE:
       # (a) tail stays bounded while history_count grows
       for _a, ev, _b in updates:
           inst = (ev.get("agent_instances") or {}).get(INSTANCE_NAME)
           if isinstance(inst, dict) and inst.get("is_partial"):
               assert inst["history_count"] - len(inst["messages"]) >= 0
               assert len(inst["messages"]) <= TAIL_COMMITTED + MAX_STREAMING_PARTIALS + 1
       # (b) payload flatness across turns (acceptance criterion for the refactor)
       if len(payload) >= 2:
       assert payload[-1]["median_bytes"] < 1.5 * payload[0]["median_bytes"], \
       f"Payload grew with conversation: turn1={payload[0]['median_bytes']}B -> turnN={payload[-1]['median_bytes']}B (limit 1.5x)"
   ```
3. Note: `dump_payload_breakdowns` referenced at line 1077 does **not exist yet** in `state_builder.py` (test guards with `getattr(..., lambda: {})`). If we want the per-frame byte breakdown during validation, add it as a small `_STREAM_TIMING_ENABLED`-gated accumulator in `build_stream_update_from_pool` (total / instances / active-instance messages / other). Optional — raw frame bytes from `_capture_ws` are sufficient for the flatness check.

---

## 5. Rollout Strategy

1. **Flag:** `AGENT_CASCADE_STREAM_DELTA=1` (default OFF). `TAIL_COMMITTED = 1` hardcoded. Read at import in `state_builder.py`. Frontend hardening (§2) ships in the same change and is flag-agnostic — it only makes existing branches more correct.
2. **Phase A (flag OFF, default):** merge backend+frontend; run full test suite + e2e → must be byte-identical behavior to today (regression gate).
3. **Phase B (flag ON in dev/staging):** run `test_streaming_fullstack_e2e.py` with flag ON (parametrized run). Verify:
   - All existing assertions pass;
   - Payload flatness (§4.3): per-turn median bytes stay within ~1.5× of turn 1 across N turns;
   - No console warnings from the R1 guard in the Node harness logs;
   - Manual long-session soak (50+ turn conversation) watching payload size and UI flicker (R3 deferred — note if flicker is noticeable).
4. **Phase C:** flip default to ON (`STREAM_DELTA_ENABLED = env != "0"`), keep `=0` as kill switch. Monitor one release cycle; revert = set env var, no code rollback needed.
5. **Phase 2 (separate change):** per-instance `seq` (§3) if R7/R8 symptoms appear; meta/delta frame split (R10) only if the ~40-field `pool_settings` floor proves significant after deltas land.

**Verification checklist for each phase:** e2e latency median unchanged vs baseline (deltas should *improve* it), payload flatness, no lost-prefix warnings, compression + rollback soak both emit full frames (grep server log / capture frames).

---

## 6. Files to Modify (with line ranges)

| File | Change | Lines (approx.) |
|---|---|---|
| `agent_cascade/api_integration_pkg/state_builder.py` | Flag constant (`STREAM_DELTA_ENABLED`) + `TAIL_COMMITTED = 1` | ~31 (next to `_STREAM_TIMING_ENABLED`) |
| `agent_cascade/api_integration_pkg/state_builder.py` | New helper `_safe_tail_start_index(msgs)` | new, near `_find_user_message_insertion_point` (~679) |
| `agent_cascade/api_integration_pkg/state_builder.py` | Tail cut in `_serialize_instance`: replace `start_idx = 0` + full enumerate with delta-aware slice (absolute indices) | 943–949 |
| `agent_cascade/api_integration_pkg/state_builder.py` | Comment documenting dedup invariant on tail-only fingerprint set | 956–968 |
| `agent_cascade/api_integration_pkg/state_builder.py` | `_serialize_instances_incremental`: `prefix_shrank` check → force full frame | 275–309 |
| `web_ui/app.js` | R2: set `_lastHistoryCount` in `state`/`done` adoption (1 line) | ~1826–1828 |
| `web_ui/app.js` | R1: guard delta-without-prefix → drop frame with console.warn (3 lines) | ~2091–2094 |
| `tests/test_state_builder_tail_cut.py` | **New** — unit tests for tail cut, tool-pair safety, history_count/index invariants (5 cases, §4) | new file |
| `tests/test_streaming_fullstack_e2e.py` | Parametrize delta flag ON/OFF; assert bounded tail length + payload flatness in delta mode | ~993–1030 (test body), ~941–985 (`_measure_payload_sizes`) |

**Not modified (phase 1):** `agent_instance.py` (no `_conv_version`); `api_server.py` (no `request_state` handler); `streaming.py` (force_full cadence unchanged); `web_ui/streaming_demo.html` (check only, per Q1).

**Deferred to phase 1.5/2:** R3 in-place fast path; index cross-check; `seq` field; `TAIL_COMMITTED` env var; meta/delta frame split.

---

## 6A. Resync Event Matrix (NOTICE for implementer)

The following events mutate conversation structure and require a full-state resync. Verify each works correctly with delta mode:

| Event | Code path | Resync mechanism | Delta-mode concern |
|-------|-----------|-----------------|-------------------|
| **User edit message** | `ws_handlers.py:1004` → `inst.rebuild_conversation()` → `self._broadcast()` | Immediate full `state` frame (bypasses streaming tick loop) | None — `_broadcast()` is a separate code path, always full. Verify it still works when delta flag is ON. |
| **User delete messages** | `ws_handlers.py:1109` → `inst.rebuild_conversation()` → `self._broadcast()` | Immediate full `state` frame | Same as above. |
| **Compression mid-stream** | `compression_exec.py` → rewrites `inst.conversation` under `_compression_lock` | `prefix_shrank` (len decreased) → next broadcast tick (≤100ms) emits full frame | The 100ms gap: any delta frames emitted between the rewrite and the next tick will have a stale prefix. The client's stale gate (`_lastHistoryCount`) rejects shrunken `history_count`. Verify no visual glitch in this window. |
| **Rollback** | `agent_instance.py` → `del self.conversation[new_len:]` | `prefix_shrank` (len decreased) → next tick full frame | Same 100ms gap as compression. Client's `startIdx < 0` full-replace is the belt-and-braces fallback. |
| **Session load / retry** | `ws_handlers.py` → `self._broadcast()` | Immediate full `state` frame | None. |
| **Instance dismiss** | `ws_handlers.py:538` → `self._broadcast()` | Immediate full `state` frame | None. |
| **WS (re)connect** | `api_server.py:1258` → pushes full `state` on connect | Server-driven full state push | None — unchanged. |
| **`force_full` periodic** | `streaming.py:343` (`tick_num % 100 == 0`) | Full frame every ~10s | This is the safety-net anchor. Unchanged. |

**Key distinction:** `_broadcast()` (user-initiated events) and `broadcast_stream_update()` (LLM streaming ticks) are **separate code paths**. Delta logic only applies to the latter. User edits/deletes always produce full `state` frames regardless of the delta flag. If you discover a path where a conversation mutation does NOT trigger either a length decrease (caught by `prefix_shrank`) OR an immediate `_broadcast()`, that's a bug — add the missing resync.

---

## 7. Open Questions (verify during implementation)

- **Q1:** `web_ui/streaming_demo.html:217-221` builds mock frames with `history_count`/`is_partial` — confirm it's a standalone demo (not loaded by app.js) so tail frames can't break it.
- **Q2:** Tail size tuning — `TAIL_COMMITTED=1` hardcoded for phase 1; if R3 flicker is noticeable in Phase B, consider adding the env var + R3 fast path together in phase 1.5.
- **Q3:** Whether to add `seq` in the same PR — recommendation: no (§3 phase 2). Only if stale-drop or gap issues are observed in production.
