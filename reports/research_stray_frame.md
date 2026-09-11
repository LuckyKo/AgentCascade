# Stray-Frame / Duplicate-Message Desync — Root-Cause Diagnosis

**Date:** 2026-09-06 · **Investigator:** stray-frame-research · **Codebase:** `N:\work\WD\AgentCascade`
**Mode:** investigation only (no production code modified)

---

## 1. Executive Summary

The `[assistant "verifying probe #N"] → [assistant ""] → [assistant "verifying probe #N" duplicate]`
pattern is **not a single bug**. It is the interaction of **two independent defects**, one of which
is the primary root cause:

1. **PRIMARY (backend):** the UI serialization cache in `state_builder.py::serialize_message` is a
   plain `Dict` keyed by `id(msg)` (a memory address). Streaming partials are **short-lived deep
   copies** that get GC'd at each turn boundary; the next turn's deep copies **reuse the freed
   addresses**, so `id()` collides with a still-cached entry and `serialize_message` returns the
   **previous turn's stale content** for the new partial. This is why idx29 "duplicates" idx27 —
   it is probe N+1's partial rendering probe N's cached content.

2. **SECONDARY (frontend):** the delta-mode merge in `app.js` (L2037-2094) is a **pure positional
   splice** that trusts `startIdx = history_count - messages.length` and never reconciles a
   previously-streaming entry by identity. Once a stale/mismatched entry is spliced in, no later
   frame overwrites it (delta mode has no full-replacement frames), so the bad entry **persists**
   and the frontend ends with **more** messages than the backend.

The delta-only asymmetry (delta ON → frontend 100 > backend 99; delta OFF → frontend 61 < backend
66) is explained by the fact that **delta frames are partial (splice-in-place) while non-delta frames
are full-replacement** — full frames self-heal stale content, partial frames do not.

---

## 2. The exact mechanism (line-cited)

### 2.1 Backend: `id()` collision in the UI serialization cache — THE ROOT CAUSE

**Cache definition — `agent_cascade/api_integration_pkg/cache.py`:**
- **L38** `self.ui_serialization: Dict[int, dict] = {}` — keyed by an **int** (a `id()` address).
- **L100-110** `_store_ui_cache(msg_id, data)`: `self.ui_serialization[msg_id] = deepcopy(data)`,
  FIFO-evicted only when `len >= _UI_CACHE_MAXSIZE` (2000). **No per-object invalidation.**
- **L49-55** `clear_all()` clears the cache — only on **session reset**.

**Serialization — `agent_cascade/api_integration_pkg/state_builder.py::serialize_message`:**
- **L769** `msg_id = id(msg)`
- **L770-771** `cached = _cache_mgr.ui_serialization.get(msg_id)`
- **L772-783** on cache **hit**: `res = dict(cached)` and **return it**, overwriting only
  `res['index'] = index` (L782). **`content`, `reasoning_content`, `function_call`, `name` are
  taken verbatim from the STALE entry** — i.e. from whatever object previously occupied that address.
- **L870-871** store condition: `if ... and for_ui and index is not None and index > 0: _store_ui_cache(msg_id, d)`
  → streaming partials (serialized at `abs_index = original_history_count + j`, L991) **are cached when
  `abs_index > 0`** — i.e. in any conversation that already has history. (Edge: a *fresh* conversation
  with `original_history_count = 0` gives `abs_index = 0` for the first partial, which is **not** cached;
  the bug manifests once history exists, which is always true in the failing test.)

**Why the key is unsafe — streaming partials are short-lived:**
- `agent_cascade/engine/core.py:1440-1471` `_update_streaming_responses`: on each ~100ms tick
  (throttled at `llm_call.py:719-721`) it does `instance._streaming_responses = copy.deepcopy(last_output)`
  → **brand-new Message objects every tick**.
- `core.py:1919-1923` `_process_response` (turn commit): **L1919** appends the real `turn_output`
  to `conversation` (via `_append_and_log_batch`), **then L1923** `instance._streaming_responses = []`.
  Also cleared on stop/abort (`llm_call.py:701, 730, 777, 1103, 1110`).
- Consequence: the deep-copied streaming objects are **GC'd** when a turn commits, freeing their
  addresses. Because each turn's partials are Message objects of (near-)identical size, CPython's
  pymalloc allocator **very likely** reuses the same free blocks for the next turn's deep copies, so
  the **next turn's** partials are frequently allocated at **the same addresses** → same `id()` →
  **cache hit on a stale entry**. (Address reuse is a strong probabilistic heuristic here, not a
  language guarantee — but same-size short-lived objects make collision probability high; the stale
  read on hit is the defect regardless of *when* the collision lands.)

**Reproducing the observed pattern, per probe turn (reasoning_len≈19212, tool-calling turn):**

| idx | backend object | what `serialize_message` returns | why |
|-----|----------------|----------------------------------|-----|
| 27  | committed probe N assistant ("verifying probe #N") | correct content | stable object in `conversation`, id stable → cache correct |
| 28  | probe N+1 streaming partial, early (content `""`, reasoning `0`) | `""` | id not yet collided (fresh address) → re-serialized correctly |
| 29  | probe N+1 streaming partial, later tick | **"verifying probe #N" (stale)** | `id()` collided with probe N's GC'd deep-copy cache entry → L772-783 returns stale content |

So idx29 is **not** a real duplicate message — it is probe N+1's partial **rendering probe N's stale
cached content**, which is exactly why it looks like a duplicate of idx27.

### 2.2 Why the empty-content frame (idx28) appears

- It is the **next turn's (N+1) streaming partial** at the moment it has generated no content yet
  (content `""`, `reasoning_len=0`). It is a legitimate in-flight message, not a "reset."
- The backend dedup (`state_builder.py:1000`) **excludes** a fully-empty partial
  (`fingerprint != ('', '', 'None', None)`) from the frame, but a partial that has a `function_call`
  or any reasoning is **appended** (L1001). Across consecutive frames the same slot flips between
  "excluded" and "appended-but-stale," which is why the slot shows `""` in one frame and stale
  content in the next.
- It is **not** a "clear of the current partial before the committed version arrives": the commit
  path clears `_streaming_responses` **after** the committed message is appended (`core.py:1919`
  then `:1923`), and that clear is atomic w.r.t. broadcasts (no UI tick fires mid-function). So the
  empty frame is a genuine new-turn partial, not a reset artifact.

### 2.3 Frontend: the positional splice that lets the bad entry persist — `web_ui/app.js`

The partial-merge path (`app.js:2037-2094`), entered only when `sa.is_partial` (set by
`state_builder.py:966` when streaming responses exist):

- **L2039** `const hCount = sa.history_count || 0;`
- **L2050** stale gate: `if (hCount < (existing._lastHistoryCount || 0))` → metadata-only (L2051-2055).
- **L2059** `const startIdx = hCount - sa.messages.length;` — **the entire merge is anchored on this
  arithmetic**, with no per-message identity.
- **L2062-2066** `if (startIdx > existing.messages.length)` → `existing._needsResync = true; continue`
  (frame **dropped**; the old list — including a leftover partial — is **kept**).
- **L2069-2075** index verification: if `sa.messages[0].index !== startIdx` → `_needsResync; continue`.
- **L2076** `existing.messages.length = startIdx;` (truncate to the computed point).
- **L2077** `existing.messages.push(...sa.messages);`

There is **no path that replaces a specific previously-streaming entry by identity**. The only way an
old partial is removed is the positional truncate at L2076. If a prior frame left the list length off
by one from what `startIdx` assumes (which the stale-content / dedup-exclusion interplay in §2.1-2.2
produces at the transition), L2076 truncates to the wrong point, the leftover partial **survives**,
and L2077 appends the committed version **on top of it** → the extra message (frontend 100 vs backend
99).

### 2.4 Why it is delta-specific (the 100>99 vs 61<66 asymmetry)

- **Delta ON:** `state_builder.py:966` sets `is_partial=True` whenever a streaming response exists,
  and `TAIL_COMMITTED=1` keeps the tail tiny. Every frame is therefore a **partial** → the frontend
  takes the splice path (L2037+) and **keeps its existing list**, only updating the tail. A
  stale-content entry (from §2.1) or a mis-anchored splice (from §2.3) is **never overwritten** —
  there are no full frames during streaming — so the bad entry **persists** → frontend ends with
  **more** messages (100 vs 99).
- **Delta OFF:** frames are **full-replacement** (`is_partial=False`, L2109+ branch replaces the whole
  object). Each frame rebuilds the list from scratch, so stale content from §2.1 **self-corrects** on
  the next frame, and the only net effect is trailing in-flight partials not yet committed → frontend
  ends with **fewer** (61 vs 66), never more.

This is why the same backend defect is **benign in non-delta mode** but **visible in delta mode**.

---

## 3. Why the pydantic fix (commit `10f04aa`) kept reintroducing it

- **Before the fix:** the cache only stored `isinstance(msg, dict)` results. Committed conversation
  entries are pydantic `Message` objects, and the streaming deep-copies are `Message` objects too —
  neither was cached, so `serialize_message` **recomputed fresh each tick** → no stale content (just
  slower). This is why pre-fix builds were correct-but-slow.
- **After the fix** (`state_builder.py:865-871`, "BUG_0005 fix: cache both dicts AND Pydantic
  objects"): pydantic `Message` objects — **including the short-lived streaming deep-copies** — are
  cached by `id()`. The unsafe keying is now active for exactly the objects that churn → id-collision
  → stale content.
- The repeated rollbacks (`609aacc`, `3fc4b39`, `d00a6c4`, …) and re-applies (`f59d929` "re-apply
  BUG_0004/0005") removed the **tail-only optimization** each time, but **never** the unsafe
  `id()`-keyed cache — so the defect kept returning. `0ae2485` ("fix message stack sync bugs
  dup/missing in delta mode") is a **frontend/workaround** layer (the splice guards at L2062-2075),
  not a fix for the backend cache; it masks symptoms and is itself the fragile §2.3 path.

---

## 4. Recommended fixes

### 4.1 Backend (PRIMARY — fixes the root cause; lowest risk first)

**A. Validate cache hits against the live object (minimal, drop-in).**
In `state_builder.py::serialize_message`, store a lightweight content fingerprint alongside the dict
(e.g. `role + content[:64] + reasoning[:64] + function_call name + len(content)`). On the hit path
(L772) recompute it from `msg` and **only accept the cached dict if the fingerprints match**; on
mismatch fall through to a fresh re-serialize. This kills the stale-content symptom directly, keeps
the committed-message cache hit rate high, and requires no global cache restructure.

**B. Robust: key by object identity, not address.**
Replace `ui_serialization: Dict[int, dict]` (`cache.py:38`) with a `weakref.WeakKeyDictionary`
keyed by the `Message` **object**. GC'd streaming deep-copies **auto-evict** their entries, so a new
object at the same address finds **no** stale entry → re-serialized correctly. (Conversation and
streaming partials are `Message` objects → weak-referenceable; handle raw `dict` inputs by skipping
the cache, as they cannot be weak-referenced.)

**C. Simplest targeted change:** add `use_cache: bool = True` to `serialize_message` and pass
`use_cache=False` for streaming partials at `state_builder.py:1001`. Only **stable committed tail
messages** (L979-986 loop) get cached — their `id()` is stable for the object's lifetime, so no
collision is possible. This alone removes the collision while preserving the committed-path perf win.

### 4.2 Frontend (defense-in-depth — `web_ui/app.js`)

- Give each message a **stable backend-assigned id** (not just a positional `index`) and, on the
  partial→committed transition, **replace the previously-streaming entry by id** rather than relying
  solely on `existing.messages.length = startIdx` (L2076). This makes the merge robust to a one-off
  count drift instead of leaving a duplicate behind.
- Keep the existing guards (L2062-2075) as a resync trigger, but add a **content-reconciliation**
  step: after splicing, if a spliced tail message's content no longer matches the entry it landed on,
  force a `force_full` resync for that agent instead of silently keeping both.

---

## 5. Confidence

- **`id()`-collision in the UI cache is the root cause of the stale "duplicate" content:** **High**
  (direct code path: `cache.py:38/100-110`, `state_builder.py:769-783/870-871`, `core.py:1471/1919-1923`;
  short-lived deep-copy lifecycle verified).
- **Delta-specificity via partial-splice vs full-replacement:** **High** (L966 `is_partial`, app.js
  L2037+ vs L2109+; matches the 100>99 vs 61<66 asymmetry).
- **Empty frame (idx28) = next-turn partial, not a reset:** **High** (commit clears `_streaming_responses`
  after commit, atomic w.r.t. broadcasts; dedup L1000 excludes fully-empty, appends otherwise).
- **Exact trigger of the +1 structural count (frontend 100 vs backend 99):** **Moderate.** The stale
  content is confirmed; the extra *structural* entry is best explained by the fragile positional splice
  (§2.3) combined with the dedup-exclusion of empty partials, but was not reproduced frame-by-frame in
  this pass.

## 6. Open questions
1. Does `force_full` (resync) actually fire during the failing run, or is `_needsResync` stuck (which
   would explain a *persisted* leftover partial)? (Check for stuck `_needsResync` state.)
2. Confirm the observed idx29 carries probe N's **exact** content (not a partial overlap) in a live
   capture — would definitively confirm the id-collision fingerprint (add a temporary log of
   `id(msg)` + returned content on the `serialize_message` hit path).
3. Is the affected agent the visible root or a sub-agent tab? (Affects whether §2.3's splice is the
   dominant count driver.)

## 7. Suggested next actions
1. **Apply fix 4.1-C** (skip cache for streaming partials) as the quick, safe change; verify the e2e
   delta run no longer shows the `[partial, empty, duplicate]` pattern and that frontend==backend.
2. **Apply fix 4.1-A** (fingerprint-validated hits) as the durable fix; add a regression test that
   allocates a Message, serializes it, deletes it, allocates a new Message (same address), and asserts
   the second serializes to its own content.
3. **Add fix 4.2** (id-based replacement on transition) for robustness.
4. **Capture one failing frame pair** (backend `serialize_message` hit-path log + frontend splice
   trace) to close open question #1/#2 before final sign-off.

---

*All file:line references verified against the working tree at investigation time (2026-09-06).
Memory saved: `.agent_lessons/agentcascade_ui_cache_id_collision.md`.*
