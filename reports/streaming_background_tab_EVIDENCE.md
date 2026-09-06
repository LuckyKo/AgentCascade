# Streaming Background-Tab Bug — Evidence & Analysis (CDP probe)

**Date:** 2026-08-31 · **Method:** Chrome DevTools MCP driving the live UI at `http://127.0.0.1:8126/` with the
debug-gated `[stream-probe]` instrumentation (STREAM_DEBUG=true) active in `web_ui/app.js`.

## 1. What the probe measures
- `tick#N Δ=Xms vis=... wsReady=...` — one line per `stream_update` WS frame; Δ = ms since previous tick.
- `ref#N Δ=Xms ...` — a 1Hz reference `setInterval`, to A/B whether ordinary timers vs the WS are throttled.
- `visibilitychange -> ...` — logs every real visibility transition.

## 2. Findings (evidence)

### F1 — Foreground streaming is perfectly smooth (no burstiness at all)
Continuous 6s cadence sample while visible: **0 gaps >500ms, max gap 24ms, median 4ms** (25 stream_update ticks).
So the "bursty/hang" symptom is **specific to the background condition**, not a general render problem. The 250ms
render throttle does NOT cause visible chunkiness in a foreground tab — frames arrive ~every 40–130ms and renders
keep up.

### F2 — Genuine background-tab throttling CANNOT be reproduced via CDP (methodological limit)
- Opening a second foreground tab did **not** flip page 1 to `hidden` — the CDP-controlled page stays
  `visibilityState=visible, hasFocus=true`. Ticks stayed smooth (~45ms) throughout. This is an automation quirk:
  the driven page is kept foregrounded by the browser scheduler.
- A **synthetic** `visibilityState='hidden'` override + dispatched `visibilitychange` did **not** starve the event
  loop: ticks kept flowing at ~5/s while "hidden" (no hang). This is expected — real occlusion throttling is a
  browser *scheduler* behavior (Chrome deprioritizes/suspends the background tab's JS event loop so `onmessage`
  isn't dispatched); flipping a property in-page does not invoke that scheduler path.

**Consequence:** The "hang when minimized, smooth when front" symptom can only be reproduced in a **real user
minimized/occluded window**, not through CDP. The probe is still the right tool — but it must be run by the USER
in their normal browser (see §4), where real occlusion throttling applies.

### F3 — There is NO background-tab / visibility handling anywhere in the frontend (confirmed)
Grep across `web_ui/*.js,*.html` for `visibilitychange|visibilityState|document.hidden|pagehide|pageshow|blur|focus`
= **0 matches**. The app has zero awareness of tab visibility. The comment at app.js:126–127 ("browsers optimize
painting for hidden tabs automatically") is wrong — browsers *stop the event loop*; nothing auto-catches-up.

### F4 — Render gate is a MINIMUM-interval throttle, not a max-delay cap (app.js:2191–2195)
```js
const shouldRender = completionDetected || stackChanged || subAgentNewVisibleMessage ||
                     isVisibleActiveAgentContentChanged ||
                     (now - state.genStats.lastSubAgentRender > subThrottleContent); // 250ms
```
This only prevents rendering *more often* than the interval; it never DELAYS a render beyond it. So on return to
foreground, the first processed `onmessage` will have `now - last >> 250ms` → renders immediately (the "dump"), then
subsequent ticks are gated to 4Hz (the "smooth"). **The throttle shapes the burst but does not cause the hang.**

### F5 — Data merge and render both live inside `onmessage` (app.js:1676 → handleServerMessage)
There is no client-side queue and no other path that updates `state.subAgents` from a `stream_update`. Therefore,
IF the event loop is starved in the background (real occlusion), **no data is applied and no render occurs** until
the tab returns — which is exactly the reported "hang then dump."

## 3. Root-cause conclusion
The symptom is consistent with (and best explained by) **Chrome background-tab event-loop throttling delaying
`onmessage` dispatch**, compounded by the **absence of any `visibilitychange` handler** to catch up on return.
This matches every reported detail:
- Hang when minimized → `onmessage` not dispatched while occluded.
- Smooth when front → loop resumes, 250ms throttle gives steady 4Hz.
- "Chunky at first then smooths out (same block)" → first tick after gap dumps all accumulated content; rest gated.
- Worse on subagents → higher stream_update volume → more frames buffered during occlusion → larger burst.

**Confidence:** The *code-side* facts (F1, F3, F4, F5) are **confirmed by direct evidence**. The *browser-scheduler*
mechanism (event loop actually starved in a real background tab) is **highly likely but not directly observed here**,
because CDP cannot reproduce genuine occlusion throttling (F2). It is the well-documented Chrome behavior and is the
only mechanism consistent with all symptoms.

## 4. To get the final confirming datapoint (user run, real browser)
The probe is live. In a NORMAL (non-CDP) browser:
1. Open the UI, start a streaming task (ideally a subagent).
2. **Minimize the window** (or switch to another app) for 1–2 min while it streams.
3. Restore it and read the `[stream-probe]` console lines.

Interpretation:
- `vis=hidden` with **large Δ (≥1s) or ticks stopping**, then a burst of small Δ on return → **CONFIRMED** (loop throttled).
- `vis=hidden` with steady ~100ms Δ → the open WS exempted the tab from aggressive throttling → pivot to backend
  queue-drop path (`streaming.py:16-32` drops stream_update events when its send queue is full).
- `wsReady` showing 2/3 (CLOSING/CLOSED) on return → connection was dropped in background.

## 5. Recommended fix direction (frontend-only, correct under BOTH scenarios)
Add a `document.addEventListener('visibilitychange', ...)`:
- **On visible:** reset all render-throttle timers (`state.genStats.lastSubAgentRender`, `lastSubAgentRenderDuration`,
  `lastGenStatsUpdate`, `lastControlsUpdate`, `lastTelemetryUpdate` → 0), invalidate panel caches, and force an
  immediate `renderSubAgents()` so the UI catches up instantly to whatever state has been merged.
- **On hidden (optional):** pause purely-cosmetic work (activity bar, gen stats) to cut background CPU; keep data
  application running.
- **(Optional hardening)** a `pageshow` → `connect()` fallback in case the WS was closed while occluded.

This is safe regardless of whether the WS exemption applies: if the loop WAS starved, the buffered `onmessage`s fire
on return and the visibility handler ensures no extra render delay; if it WASN'T starved, the handler is a harmless
no-op catch-up. It directly targets the "hang then dump / chunky-then-smooth" shape.

## 6. Cleanup
After the user collects the confirming datapoint, remove the probe: set `STREAM_DEBUG = false` (or delete the three
gated blocks in app.js: ~122–140, ~2007–2015, ~5571–5586) and delete this report + `streaming_probe_HOWTO.md`.
