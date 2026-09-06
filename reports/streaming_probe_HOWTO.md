# Streaming Background-Throttle Probe — HOWTO (TEMP, diagnosis only)

**Date:** 2026-08-31
**Author:** stream_probe_coder
**Status:** TEMPORARY instrumentation — **DO NOT COMMIT.** Evidence-gathering only. Trivially removable behind one flag (`STREAM_DEBUG`).

This probe empirically confirms or refutes the primary root-cause hypothesis for the streaming
burstiness bug: *when the Chrome tab is backgrounded, the browser throttles the JS event loop so
`ws.onmessage` callbacks are not dispatched; WS frames buffer and fire in a burst on return.*
It also resolves the **open question** from the investigation report (§2.1 caveat / §4.5): does
Chrome's "active-WebSocket exemption" keep the loop alive instead?

Full context: `reports/streaming_frontend_background_throttle_INVESTIGATION.md` (esp. §4.5).

---

## 1. What was added (all in `web_ui/app.js`)

Every line is gated on a single flag and only **reads state + logs**. Nothing modifies `state`,
calls render functions, changes any throttle timer, or touches the WebSocket. When `STREAM_DEBUG`
is `false`, no listeners are added, no interval is started, and nothing is logged — the app behaves
byte-for-byte as before.

| # | What | Location (current line) |
|---|------|--------------------------|
| 1 | **Master flag** `const STREAM_DEBUG = true;` + module-scope probe state vars (`_dbgLastTick`, `_dbgTickCount`, `_dbgRefTimerId`, `_dbgRefLastFire`, `_dbgRefTickCount`) | `app.js:122–140` (flag at `:133`) |
| 2 | **Tick-cadence + visibility logging** — at the very top of the `stream_update` case in `handleServerMessage`. One line per tick: inter-tick Δms, `document.visibilityState`, `ws.readyState`, running tick counter. | `app.js:2007–2015` (log at `:2014`) |
| 3 | **1Hz reference timer (A/B probe)** + **`visibilitychange` logger** — started ONCE at the init point, just before `connect()`. The interval increments a counter and logs its own actual elapsed Δms + visibilityState. Runs for the page lifetime while the flag is on. | `app.js:5571–5586` (interval log `:5581`, visibility log `:5584`) |

> Note: line numbers reflect the state of the file *after* this probe was added. The original
> investigation report references pre-probe line numbers (e.g. WS at `app.js:1645`, `stream_update`
> at `app.js:1987`).

### Console output format (all tagged `[stream-probe]`)

```
[stream-probe] tick#<n> Δ=<ms>ms vis=<visible|hidden> wsReady=<0|1|2|3>
[stream-probe] ref#<n>  Δ=<ms>ms vis=<visible|hidden> wsReady=<0|1|2|3>
[stream-probe] visibilitychange -> <visible|hidden> @<ms>ms wsReady=<0|1|2|3>
```

- `tick#` = a `stream_update` message was dispatched (i.e. `onmessage` actually ran).
- `ref#`  = the ordinary 1Hz `setInterval` fired (a baseline for "is the event loop alive at all?").
- `Δ`     = real elapsed time since the previous fire of that same probe (`performance.now()`).
- `vis`   = `document.visibilityState` at the moment it fired.
- `wsReady` = `WebSocket.readyState`: **0** CONNECTING, **1** OPEN, **2** CLOSING, **3** CLOSED.

---

## 2. How to enable / disable

The probe is **already on** in the working tree (`STREAM_DEBUG = true` at `app.js:133`).

- To run the experiment: just load the page (hard-refresh so the updated `app.js` is fetched).
- To turn it off without deleting code: set `const STREAM_DEBUG = false;` at `app.js:133`.
- To remove entirely: delete the TEMP block (`app.js:122–140`) and the two gated snippets
  (`app.js:2007–2015` and `app.js:5571–5586`). Nothing else references these symbols.

> The probe is intentionally left **enabled** in the working tree so it can be exercised immediately.
> Do **not** commit it — remove or flag-off before any commit.

---

## 3. Step-by-step reproduction

1. Start the server and open the web UI in Chrome. Open DevTools → **Console**.
   - Make sure Console is set to show `Info`/`Verbose` (the probe uses `console.log`, so it shows by default).
2. Trigger an **active streaming** run — ideally a subagent doing real work, since subagents stream
   the highest tick rate (largest burst on resume; see report §6). Keep the tab in the foreground
   and confirm you see steady `[stream-probe] tick#…` lines with small `Δ` (~tens of ms) while
   `vis=visible`.
3. **Minimize the tab** (or switch to another window so it becomes backgrounded/occluded). Keep the
   generation running for **1–2 minutes**.
4. **Restore** the tab to the foreground and watch the Console.
5. Collect the Console output around the minimize → restore transition. That is the evidence.

---

## 4. How to interpret each output pattern

Focus on the `tick#` lines (the WS dispatch cadence) during the `vis=hidden` window, cross-referenced
with the `ref#` lines (the ordinary timer baseline).

### Pattern A — Event loop WAS throttled → **hypothesis CONFIRMED**
- During `vis=hidden`: `tick#` lines **stop appearing** (or appear with very large gaps), and when
  they resume on return the `Δ` values are **large (≥ 1 s)**.
- The `ref#` timer also shows large `Δ` (throttled to ~1 Hz or starved).
- **Meaning:** Chrome suspended/deprioritized the tab's event loop, so `onmessage` was not dispatched.
  All buffered WS frames fire in a burst on return → the "hang then dump" symptom is explained.
- **Next step:** implement the recommended fix (report §9) — a `visibilitychange` handler that resets
  throttle timers + forces a catch-up render on return to visible.

### Pattern B — WS exemption kept the loop alive → **PIVOT to backend**
- During `vis=hidden`: `tick#` lines keep flowing at a **steady ~100 ms cadence** (small `Δ`), i.e.
  data *was* being applied in the background.
- The `ref#` timer may still be throttled to ~1 Hz (timers are throttled even when WS keeps the loop
  alive) — this is the A/B discriminator: **WS ticks fast + ref slow ⇒ WS is exempted.**
- **Meaning:** the event loop was NOT starved; state was updated in the background. The perceived
  "hang" is therefore cosmetic or comes from elsewhere → pivot to the **backend queue-drop path**
  (report §5 #6) and re-examine server-side back-pressure / buffer drops.

### Pattern C — Connection dropped in the background
- On return, `wsReady` shows **2 (CLOSING)** or **3 (CLOSED)**, or you see a reconnect cycle.
- **Meaning:** the WS was dropped while backgrounded (e.g. under memory pressure / full tab
  suspension). The 2 s reconnect timer (`scheduleReconnect`, throttled in background) adds delay on
  top of any event-loop throttling. This compounds Pattern A and points to also adding a `pageshow`
  handler that calls `connect()` immediately (report §9, alternative fix).

### Quick decision table

| During `hidden` | `tick#` Δ | `ref#` Δ | `wsReady` on return | Verdict |
|---|---|---|---|---|
| starved / large gaps | ≥ 1 s (or absent) | ≥ 1 s | 1 (OPEN) | **A — CONFIRMED** (loop throttled) |
| steady small cadence | ~100 ms | ~1000 ms (throttled) | 1 (OPEN) | **B — PIVOT** (WS exempted; check backend) |
| any | any | any | 2 / 3 | **C — connection dropped** (add `pageshow`) |

> Both A and C can co-occur (loop throttled *and* WS dropped). Read `wsReady` first to rule out C,
> then use the tick/ref cadence to separate A from B.

---

## 5. Cleanup checklist (after evidence is collected)

1. Set `STREAM_DEBUG = false` at `app.js:133` (or delete the TEMP block + two gated snippets).
2. Confirm no remaining references: grep for `STREAM_DEBUG`, `_dbgLastTick`, `_dbgTickCount`,
   `_dbgRefTimerId`, `_dbgRefLastFire`, `_dbgRefTickCount`, and `[stream-probe]`.
3. Re-run `node --check web_ui/app.js` to confirm the file still parses.
4. **Do not commit** the probe in any form.
