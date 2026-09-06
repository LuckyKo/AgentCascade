# Streaming Frontend Background-Tab Throttle — Root-Cause Investigation

**Date:** 2026-08-31  
**Investigator:** stream_fe_research  
**Scope:** `web_ui/app.js`, `web_ui/index.html` (read-only)  
**Verdict:** **LIKELY client-side, pending empirical confirmation.** The code structure is confirmed: no client-side queue, no visibility handling, and ALL data application + rendering is gated behind `onmessage` (app.js:1676). The most likely mechanism is Chrome delaying `onmessage` dispatch in background tabs, BUT Chrome exempts pages with an *active* WebSocket from aggressive throttling (see §2.1 caveat) — so the primary hypothesis must be verified empirically (§4.5) before being treated as confirmed. The 250 ms render throttle is a secondary contributor that shapes (but does not cause) the burst.

---

## 1. Executive Summary

When the Chrome tab is minimized or backgrounded, the browser **deprioritizes or suspends the tab's JavaScript event loop**. WebSocket frames continue to arrive at the network layer and are buffered, but `onmessage` callbacks are **not fired** until the tab returns to the foreground. Since ALL data application to `state.subAgents` happens inside `onmessage` → `handleServerMessage` (app.js:1676–1683 → 1775+), **no state is updated and no render occurs** while the tab is in the background.

When the tab becomes active again, all buffered `onmessage` callbacks fire in rapid succession. Each one merges its data into `state.subAgents`. The first callback to pass the render time-gate triggers a full render of all accumulated content in one burst — the "hang then dump" symptom. Subsequent callbacks are gated by the 250 ms render throttle, so the display "smooths out."

---

## 2. End-to-End Streaming/Render Path (with file:line refs)

### 2.1 WebSocket Connection & Message Receive

| Step | Location | Detail |
|---|---|---|
| WS created | `app.js:1645` | `ws = new WebSocket(url)` — standard browser WebSocket |
| `onmessage` | `app.js:1676–1683` | **Immediate, synchronous.** `JSON.parse(event.data)` → `handleServerMessage(data)`. No queue, no timer, no batch. |
| Reconnect | `app.js:1686–1692` | `setTimeout(..., 2000)` on `onclose`. In a background tab this timer is throttled to ~1 Hz minimum, but it still fires. |

**Key fact:** There is **no client-side message queue**. `onmessage` processes each frame immediately. If the event loop is starved (background tab), the callback simply doesn't run — the frame waits in the browser's internal WS receive buffer.

> **CRITICAL CAVEAT — Chrome's active-WebSocket exception (unverified for this app):**
> Chrome's "aggressive throttling" of background tabs (timers → 1 Hz, rAF → paused,
> event loop deprioritized) is **automatically disabled for pages with an active WebSocket
> connection** (and pages playing audio). This means the event loop for a tab holding an open
> WS may keep running at normal frequency even while backgrounded — in which case `onmessage`
> WOULD keep firing and the symptom would NOT occur.
>
> This is a **key uncertainty in the diagnosis**. The reported symptom ("hang when minimized,
> smooth when front") is *consistent* with event-loop starvation, but it is **not yet
> empirically confirmed** for this specific app, because whether Chrome treats an *idle-but-open*
> WebSocket as "active" (thus exempting the tab from aggressive throttling) is implementation-
> specific and version-dependent. See Section 4.5 (Empirical Verification) for how to confirm.
>
> The `visibilitychange` fix recommendation (Section 9) remains valid and is in fact *more*
> important if the WS keeps the loop alive: it guarantees a catch-up render on return
> regardless of which throttling regime applied.

### 2.2 `handleServerMessage` — `stream_update` case

`app.js:1987–2228`

The handler does two things on every `stream_update`:

**(A) Data merge — ALWAYS, unthrottled (lines 2004–2084):**
- Iterates `data.agent_instances`, merges each agent's messages into `state.subAgents[name]` (lines 2005–2065).
- For partial updates: splice logic at lines 2032–2044 (`startIdx = hCount - sa.messages.length`).
- Detects `subAgentContentChanged` / `subAgentNewVisibleMessage` flags (lines 2067–2083).

**(B) Render gate — THROTTLED (lines 2135–2227):**

| What | Gate | Interval | Lines |
|---|---|---|---|
| Sub-agent render (`renderSubAgents`) | Time-gate on `performance.now()` | **250 ms** (adaptive: 250–500 ms for root) | 2151–2167 |
| Controls (`updateControls`) | Time-gate | **1000 ms** (1 Hz) | 2146 |
| Gen stats (`updateGenStats`) | Time-gate | **500 ms** (2 Hz) | 2222 |
| Telemetry (`updateTelemetryPanel`) | Time-gate | **2000 ms** | 2129 |
| Activity bar (`pushImmediate`) | Time-gate + dedup | **30 ms** | 653 |
| Activity bar full render | Time-gate | **200 ms** | 686 |

**Throttle constant definitions** (`app.js:44–56`, all nested under `const THROTTLE = Object.freeze({...})`):
```
RENDER_SUBAGENT_MS: 250
RENDER_ROOT_BASE_MS: 250
RENDER_ROOT_MAX_MS: 500
ACTIVITY_BAR_RENDER_MS: 200
GEN_STATS_MS: 500
CONTROLS_MS: 1000
TELEMETRY_MS: 2000
PUSH_IMMEDIATE_MS: 30
AUTO_SECURITY_SYNC_DEBOUNCE: 100
```

**Render gate logic** (lines 2156–2167):
```js
const shouldRender = completionDetected ||
                     stackChanged ||
                     subAgentNewVisibleMessage ||
                     isVisibleActiveAgentContentChanged ||
                     (now - state.genStats.lastSubAgentRender > subThrottleContent);
```
This is a **minimum-interval gate**, not a maximum-delay cap. It prevents rendering *more often* than the interval, but does NOT delay rendering beyond the interval. If `now - lastSubAgentRender > 250 ms` (which is always true after a long background gap), the render fires immediately on the next processed message.

### 2.3 Render Path

`renderSubAgents()` (lines 3874–4071) → `renderSubAgentPanel()` (lines 4073–4280) → `updateBubbleContent()` (lines 3022–3139)

- Incremental append for plain text: `appendStreamingDelta()` (lines 2998–3020) — O(1) text insertion.
- Full re-render (markdown): `renderMarkdown()` (line 3141+) — called every 8th tick for plain text (line 3066, `forceInterval = 8`), every 24th tick for image-heavy bubbles (line 3069).
- **No `requestAnimationFrame` in the streaming render path.** The two `rAF` calls in the file (lines 3520, 3690) are in the message-edit and approval-bar paths, not in the streaming path.

### 2.4 Full-State (`state`/`done`) Path

`app.js:1790–1985` — unthrottled, always calls `renderSubAgents()` (line 1952) and resets the throttle timer (line 1954). This path fires on `done` events (agent completion) and periodic full-state refreshes (every ~100 ticks ≈ 10 s per backend at `streaming.py:111`).

---

## 3. Background-Tab / Visibility Handling

**Finding: NONE EXISTS.**

- `document.visibilityState` — **not used** (grep: 0 matches)
- `visibilitychange` — **not used**
- `pagehide` / `pageshow` — **not used**
- `document.hidden` — **not used**
- `index.html` — no timers, no rAF, no visibility handlers

**Comment at app.js:126–127:**
```js
// Modern browsers optimize painting for hidden tabs automatically — no explicit check needed.
// The early-return check in stream_update was also removed to avoid render delays when switching back.
```
This comment is **incorrect**. Browsers do NOT "optimize painting" — they **stop the event loop**. There is no automatic mechanism to keep the UI in sync. The app has zero background-tab awareness.

---

## 4. Where the Data Gets Stuck — Answer to the Key Question

**Answer: (b) received but not processed — the `onmessage` callback is delayed by browser event-loop throttling.** *(Primary hypothesis; see the active-WebSocket caveat in §2.1 and the verification steps in §4.5.)*

| Layer | What happens in background | Evidence |
|---|---|---|
| Network / WS | Frames continue to arrive, buffered in Chrome's WS receive buffer | Standard browser behavior; WS is not closed on background |
| Event loop | `onmessage` callbacks are **not dispatched** — the tab's JS event loop is starved | Chrome throttles background tabs: timers → 1 Hz, rAF → paused, event loop deprioritized |
| State (`state.subAgents`) | **NOT updated** — the merge code (lines 2005–2065) only runs inside `onmessage` | `app.js:1676` |
| Render | **NOT triggered** — the render gate (line 2163) only runs inside `onmessage` | `app.js:2163` |

When the tab returns to the foreground:
1. All buffered `onmessage` events fire in rapid succession (Chrome delivers them in order).
2. Each callback merges its data into `state.subAgents`.
3. The **first** callback to arrive has `now - lastSubAgentRender >> 250 ms` → `shouldRender = true` → `renderSubAgents()` fires → **all accumulated content renders in one burst** (the "dump").
4. `lastSubAgentRender` is reset to the post-render time (line 2190).
5. **Subsequent** callbacks: `now - lastSubAgentRender < 250 ms` → render is skipped. They only update state.
6. After 250 ms of real time, the next message triggers another render → "smooths out."

**This fully explains all reported symptoms:**

| Symptom | Explanation |
|---|---|
| "Hang when minimized" | `onmessage` not firing → no state updates, no renders |
| "Smooth when brought to front" | Event loop resumes → messages processed in real-time → renders at 4 Hz (250 ms throttle) |
| "Chunky at first then smooths out (same block)" | First message after gap → large burst render; subsequent messages → 250 ms throttle |
| "Worse on subagents" | Subagents stream at higher frequency (more ticks per second) → more messages accumulate in the WS buffer → larger burst on resume. Main agent delegates more / streams less → smaller burst. |
| "Hangs can last 1+ minute" | Duration of tab being in background. Chrome throttles/suspends background tabs for as long as they are occluded (typically up to ~5 min before full suspension, or sooner under memory pressure). |

### 4.5 Empirical Verification (required to confirm/refute the primary hypothesis)

The diagnosis above rests on code analysis (confirmed) plus a browser-behavior assumption (NOT
confirmed for this app). The single most important open question is: **does Chrome apply
aggressive throttling to this tab, or does the open WebSocket keep the event loop alive?**

Concrete verification steps (read-only, no code changes):

1. **Log tick cadence across a visibility transition.** In `handleServerMessage` → `stream_update`
   (app.js:1987), temporarily record `performance.now()` and `document.visibilityState` for each
   tick (e.g. to `console` or a ring buffer). Minimize the tab for 1–2 min during streaming, then
   restore it and inspect the inter-tick deltas:
   - **Large gaps (≥ 1 s) while `hidden`** → event loop WAS throttled → hypothesis **#1 CONFIRMED**.
   - **Steady ~100 ms cadence while `hidden`** → the active-WS exception kept the loop alive →
     data *was* being applied in the background; the "hang" is cosmetic (render still runs, or
     state is fine but a different mechanism causes the perceived freeze) → pivot to **§5 #6**
     and re-examine the backend queue drop path.
2. **Check WS connection state on return.** Log `ws.readyState` (app.js:1641) on `visibilitychange`.
   If it is `CLOSED`/`CLOSING` on return, the connection was dropped in the background and the
   2 s reconnect (app.js:1688) explains the delay.
3. **A/B with a non-WS timer.** Add a `setInterval` (1 Hz) that increments a counter while the tab
   is hidden. If it ticks at 1 Hz (throttled) but the WS ticks faster, the WS is exempted; if
   both are starved, the whole loop is throttled.

These three probes distinguish all plausible mechanisms with no production code changes.

---

## 5. Root Cause Ranking

### #1 (Primary): Browser event-loop throttling delays `onmessage` dispatch
**Confidence: MODERATE** (code structure is confirmed; the throttling mechanism itself is a well-known browser behavior but not empirically verified for this app — see §4.5 and the active-WebSocket caveat in §2.1)

- **Mechanism:** Chrome deprioritizes background tabs. The JS event loop is starved, so `onmessage` callbacks (app.js:1676) are not dispatched. All data application and rendering is gated behind this callback.
- **Evidence (code, confirmed):** No visibility handling exists (grep = 0). The only path to update `state.subAgents` from a `stream_update` is through `onmessage` → `handleServerMessage` → case `stream_update` (app.js:1987). No other path exists, so if the callback is delayed, the UI is necessarily frozen.
- **Evidence (browser behavior, NOT verified here):** The claim that `onmessage` is actually delayed while backgrounded depends on whether Chrome's *aggressive-throttling* regime applies to this tab — and Chrome **exempts pages with an active WebSocket connection** from aggressive throttling (see §2.1 caveat). An idle-but-open WS may or may not count as "active" (implementation/version specific).
- **Why it produces the symptom:** If throttling applies, 100% of the "hang then dump" behavior is explained by the event loop being starved and then resuming. If it does NOT apply (WS keeps the loop alive), the hang must come from another source (e.g., the server's own back-pressure / queue drop, see §5 #6) and the symptom would instead be *smooth-but-dropped* rather than *hang-then-dump*. The reported "hang then dump" shape favors the throttling hypothesis.

### #2 (Secondary/Contributing): Render throttle (250 ms) shapes the burst
**Confidence: HIGH**

- **Mechanism:** Even if messages were processed in real-time, the render gate (app.js:2163, `RENDER_SUBAGENT_MS: 250`) limits renders to ~4 Hz. After a long background gap, the first render dumps all accumulated content; subsequent renders are 250 ms apart.
- **Why it matters:** It determines the *shape* of the burst (one big dump, then 4 Hz updates) but does NOT cause the hang itself. Without the event-loop starvation, the render throttle would produce smooth 4 Hz streaming.

### #3 (Tertiary): No `visibilitychange` handler to flush accumulated state
**Confidence: HIGH**

- **Mechanism:** The app has zero awareness of tab visibility (app.js:126–127 comment is wrong). There is no handler that, on `visibilitychange → visible`, forces a full re-render or resets throttle timers.
- **Why it matters:** Without a visibility handler, the only way to trigger a render after returning to the foreground is to wait for the next `onmessage` to arrive. If the WS connection was closed during the background period (possible under memory pressure), the reconnect timer (2 s, app.js:1691) adds additional delay.

### #4 (Refuted): rAF-driven render loop
**Confidence: CONFIRMED NOT A FACTOR**

- The streaming render path does **not** use `requestAnimationFrame`. The two `rAF` calls in the file (app.js:3520 in message-edit, app.js:3690 in approval bar) are unrelated to streaming.
- The render gate is purely time-based (`performance.now()` comparison), not rAF-based.

### #5 (Refuted): Client-side message queue
**Confidence: CONFIRMED NOT A FACTOR**

- There is no client-side queue. `onmessage` (app.js:1676) calls `handleServerMessage` synchronously. No array accumulates ticks. No `setInterval` exists anywhere in `app.js` (grep: 0 matches).

### #6 (Refuted): Network-level WS throttling
**Confidence: LOW (unlikely primary cause)**

- The network layer is not the bottleneck in the common case. Frames are received and buffered by the network stack independently of the tab's event loop, so the hang is not a network-delivery problem *per se*.
- **Nuance (per reviewer):** Even in background tabs, Chrome may buffer WS frames more aggressively and deliver them in bursts on return — but this only shapes the *delivery pattern*, not the root cause. The WS connection is not closed on backgrounding in the common case (verify via §4.5 step 2); if it is, the 2 s reconnect (app.js:1688) plus event-loop throttling could compound the delay.

---

## 6. Subagent vs. Main Difference

**Finding: No per-agent rendering difference in the code path.**

- All agents (root and sub) flow through the same `agent_instances` loop (app.js:2004–2084). There is no per-agent throttle or per-agent queue.
- The only difference: `RENDER_SUBAGENT_MS` (250 ms) is used when `isSubAgentActive` (app.js:2153–2157), vs. adaptive 250–500 ms for root. Both are similar.
- **Why subagents appear worse:** Subagents stream more tokens per second (they do the actual work; the main agent delegates). More stream_update ticks per second → more messages accumulate in the WS buffer during background → larger burst on resume. This is a volume effect, not a code difference.

---

## 7. Merge/Splice Cost (app.js:2032–2044)

```js
const startIdx = hCount - sa.messages.length;
if (startIdx >= 0) {
  if (startIdx > existing.messages.length) {
    existing.messages = [...sa.messages];   // full replace
  } else {
    existing.messages.length = startIdx;
    existing.messages.push(...sa.messages); // splice
  }
}
```

**Cost analysis:** The splice is O(K) where K = number of messages in the tail (`sa.messages.length`). For streaming, K is typically 1–3 (the current partial message + a few completed ones). This does NOT scale with the number of accumulated background messages — each `onmessage` callback carries its own `agent_instances` payload, and the merge is per-callback. There is no "drain a queue of N messages" operation. The cost is not a factor.

---

## 8. Existing Tests

**Frontend streaming path: NO automated tests exist.**

- `web_ui/` contains only utility scripts (`check_braces.py`, `check_js.py`, `parse_js.py`, etc.) and `test_send_message.js` (a manual WS send test, not a streaming render test).
- No JS test framework (Jest, Mocha, etc.) is configured.
- No Playwright/Cypress E2E tests for the web UI.
- Backend tests cover the streaming broadcast path (`tests/test_streaming_buffering_fixes.py`, `tests/test_agent_pool.py`) but do not test the frontend render loop.

**Relevant lesson:** `.agent_lessons/streaming-broadcast-test-harness.md` documents the backend `broadcast_stream_update` test harness and the reverted "chunky-streaming fix" (todo 142) — a backend-side attempt that was reverted because it misdiagnosed the issue.

---

## 9. Recommended Fix Direction (NOT to be implemented)

### Primary fix: `visibilitychange` handler (addresses root cause #1 + #3)

Add a `document.addEventListener('visibilitychange', ...)` handler:

1. **On `document.visibilityState === 'visible'`:**
   - Reset ALL render throttle timers (`state.genStats.lastSubAgentRender = 0`, `lastSubAgentRenderDuration = 0`, `lastGenStatsUpdate = 0`, `lastControlsUpdate = 0`).
   - Invalidate all panel caches (`invalidateAllPanelCaches()`).
   - Force an immediate `renderSubAgents()`.
   - This ensures the UI catches up to whatever state has been merged by the time the tab becomes visible.

2. **On `document.visibilityState === 'hidden'`:**
   - (Optional) Optionally pause cosmetic work (ActivityBar, gen stats) to reduce background-tab CPU. Data application in `onmessage` should continue if the event loop is still running.

**Why this is sufficient:** The `onmessage` callbacks that were delayed by the event loop will fire when the tab becomes visible. The visibility handler ensures the render throttle doesn't add additional delay on top of the already-accumulated data.

### Secondary fix: Decouple data application from render throttling

The current code structure (data merge + render gate in the same `onmessage` handler) is correct in principle — data is always applied, render is throttled. The only gap is the visibility handler. No architectural change needed.

### Alternative (if the issue persists after the visibility fix):

If Chrome fully suspends the tab (closes the WS connection), add a `pageshow` handler that calls `connect()` to re-establish the WebSocket. The existing `onclose` → `scheduleReconnect()` path (app.js:1660–1692) should handle this, but the 2 s reconnect timer may be throttled in a background tab. A `pageshow` handler that immediately calls `connect()` would close the gap.

---

## 10. Confidence Assessment

| Finding | Confidence |
|---|---|
| No visibility handling exists | **Confirmed** (grep: 0 matches) |
| No client-side message queue | **Confirmed** (grep: 0 `setInterval`, synchronous `onmessage`) |
| No rAF in streaming path | **Confirmed** (only 2 rAF calls, both unrelated) |
| Event-loop throttling delays `onmessage` | **Moderate** (well-documented Chrome behavior; consistent with all symptoms — BUT unverified for this app due to the active-WebSocket exception; see §2.1 caveat + §4.5) |
| Render throttle shapes the burst | **High** (code at app.js:2163, 250 ms gate) |
| Subagents worse due to volume, not code | **High** (no per-agent code difference found) |
| 1+ minute hang duration | **Moderate** (consistent with Chrome background-tab throttling/suspension duration; not directly measured) |

---

## 11. Open Questions

1. **Does Chrome apply aggressive throttling to a tab with an *idle-but-open* WebSocket?** (HIGHEST PRIORITY — determines whether the primary hypothesis holds.) Chrome exempts pages with an *active* WebSocket from aggressive throttling; whether an idle WS counts as "active" is implementation/version-specific. Verify via §4.5 (tick-cadence logging across a visibility transition). If the loop stays alive, the "hang" is cosmetic or server-side, and the diagnosis pivots to §5 #6.
2. **Does Chrome close the WS connection when the tab is backgrounded?** In the common case (short background period), no. Under memory pressure or extended suspension, possibly. The reconnect path (app.js:1686–1692) should handle it, but the 2 s timer is also throttled. Verify via §4.5 step 2 (`ws.readyState` on return).
3. **Does the backend's 100 ms broadcast throttle + bounded send queue (`streaming.py:94`, `streaming.py:16–32` drops stale events when the queue is full) interact with the frontend?** The backend sends at most 10 Hz and **drops stream_update events when its send queue is full** (`_put_stream_update`, QueueFull → drop). If the client is slow to drain (backgrounded), the server may *drop* ticks rather than buffer them — meaning the "dump" on return could be a *gap* (missing intermediate content) rather than a true accumulation. This is a backend-side contribution worth checking alongside the frontend.
4. **Is the `performance.now()` clock monotonic and unaffected by tab suspension?** Yes — `performance.now()` uses a high-resolution monotonic clock that is not affected by tab visibility. The render time-gate will correctly detect the gap on resume.
