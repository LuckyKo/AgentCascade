# Streaming Visibility Catch-Up Fix — Implementation Notes

**Date:** 2026-08-31 · **Author:** stream_fix_coder
**Scope:** FRONTEND ONLY — `N:\work\WD\AgentCascade\web_ui\app.js`
**Bug:** "Streaming hangs when tab is minimized, dumps a burst on return" (Chrome starves the background/occluded tab's JS event loop → `ws.onmessage` stops firing; no state merged, no render while hidden).

---

## 1. The change (exact location)

**File:** `N:\work\WD\AgentCascade\web_ui\app.js`
**Location:** lines **5587–5615** (inserted in the Init section, *between* the TEMP `[stream-probe]` init block at 5572–5586 and the existing `connect();` call — which moved from line 5587 to line 5617).

The change is **one** new top-level `document.addEventListener('visibilitychange', ...)` registered once at init. Nothing else was modified. The TEMP probe (`STREAM_DEBUG = true`, its three gated blocks) is left byte-for-byte intact.

```js
// ── Visibility catch-up ──────────────────────────────────────────────────────
// Chrome starves a background/occluded tab's JS event loop, so ws.onmessage stops
// firing while hidden and no state is merged / rendered. On return to the foreground
// all buffered frames fire at once and the first one dumps the whole backlog. This
// handler makes the UI catch up INSTANTLY on visibilitychange → visible: it resets
// every render-throttle timestamp (so the next render isn't gated behind a stale
// "last rendered" time), invalidates panel caches, and forces an immediate
// renderSubAgents(). It is a safe no-op when the tab was never backgrounded, and is
// correct regardless of whether Chrome's open-WebSocket exemption kept the loop alive.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return; // only catch up on return to foreground
  try {
    // Only act when the WS is live and there's something to render.
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (!state.subAgents || Object.keys(state.subAgents).length === 0) return;
    // Reset all render-throttle timers so the next render fires immediately, not after a stale interval.
    state.genStats.lastSubAgentRender = 0;
    state.genStats.lastSubAgentRenderDuration = 0;
    state.genStats.lastGenStatsUpdate = 0;
    state.genStats.lastControlsUpdate = 0;
    state.genStats.lastTelemetryUpdate = 0;
    // Force panels to re-render (bypass lastRenderedCount/contentKey cache) and paint now.
    invalidateAllPanelCaches();
    renderSubAgents();
  } catch (err) {
    console.error('[visibility] catch-up failed:', err.message);
  }
});
```

### What each line does
| Line | Purpose |
|---|---|
| `if (document.visibilityState !== 'visible') return;` | Fire only on the **hidden→visible** transition. This makes the handler a true no-op when visibility never changes (zero foreground perf impact) and skips all work while hidden. |
| `if (!ws \|\| ws.readyState !== WebSocket.OPEN) return;` | Guard: only catch up if the WS is actually connected. `WebSocket.OPEN === 1`. If the connection was dropped while occluded, we don't force a render on stale/absent state — the existing `onclose → scheduleReconnect()` path owns reconnection (see §5). |
| `if (!state.subAgents \|\| Object.keys(state.subAgents).length === 0) return;` | Guard: skip if there's nothing to render (e.g., before the first full-state frame arrives). Also mirrors `renderSubAgents()`'s own early-return at app.js:3939. |
| `state.genStats.lastSubAgentRender = 0;` | Reset sub-agent render throttle timestamp → next `stream_update`'s gate (`now - last > 250ms`, app.js:2195) passes immediately. |
| `state.genStats.lastSubAgentRenderDuration = 0;` | Reset adaptive-throttle duration accumulator (used at app.js:2183/2219) so root/sub adaptive interval isn't inflated by a stale value. |
| `state.genStats.lastGenStatsUpdate = 0;` | Reset gen-stats throttle → next `updateGenStats` fires immediately (app.js:2249–2253). |
| `state.genStats.lastControlsUpdate = 0;` | Reset controls throttle → next `updateControls` fires immediately (app.js:2174–2176). |
| `state.genStats.lastTelemetryUpdate = 0;` | Reset telemetry throttle → next `updateTelemetryPanel` fires immediately (app.js:2157–2159). |
| `invalidateAllPanelCaches();` | Bypass per-panel render cache: sets each `.main-tab-panel`'s `dataset.contentKey=''` and `lastRenderedCount='999999999'` (app.js:511–517) so the forced render is a full repaint, not skipped by the "same count" fast-path at app.js:4172. |
| `renderSubAgents();` | **Force an immediate re-render** of whatever state has been merged by now (app.js:3902), painting it right away instead of waiting for the next throttled tick. No args; early-returns if no agents. |
| `try/catch` + `console.error('[visibility] catch-up failed:', ...)` | A failure here must never break the app — any exception is swallowed and logged, matching the existing `onmessage` error-handling style (app.js:1700–1702). |

---

## 2. Timer names reset (and where confirmed)

All five live under `const state = { ... genStats: { ... } }`. Confirmed at **two** sites — the initial definition and `resetGenStats()` — with identical field names:

- Initial `state.genStats` definition: **app.js:91–105**
- `resetGenStats()` re-init: **app.js:4735–4752**

| Timer | Defined at | Used (throttle gate) at |
|---|---|---|
| `state.genStats.lastSubAgentRender` | app.js:99, 4747 | app.js:2180, 2195, 2218, 2224 |
| `state.genStats.lastSubAgentRenderDuration` | app.js:100, 4748 | app.js:2183, 2219, 2225 |
| `state.genStats.lastGenStatsUpdate` | app.js:98, 4746 | app.js:2249–2253 |
| `state.genStats.lastControlsUpdate` | app.js:103, 4751 | app.js:2174–2176 |
| `state.genStats.lastTelemetryUpdate` | app.js:104, 4752 | app.js:2157–2159 |

All five names from the task spec matched exactly. (Note: `genStats` also has `lastContextBarUpdate` and `lastUiUpdate` at app.js:101–102, but those were **not** in scope per the task and are left untouched.)

## 3. `invalidateAllPanelCaches()` — EXISTS

Confirmed at **app.js:511–517**. It iterates `mainTabPanels.querySelectorAll('.main-tab-panel')` and sets `dataset.contentKey = ''` + `dataset.lastRenderedCount = '999999999'`. It is already called in other state-changing paths (app.js:1068, 1969, 2133, 5252, 5279), so calling it here follows an established pattern. It is guarded internally by `if (!mainTabPanels) return;`.

## 4. `renderSubAgents()` — confirmed

Defined at **app.js:3902**, no parameters, early-returns when `namesArr.length === 0` (app.js:3939). Safe to call unconditionally after the guards above.

---

## 5. Edge cases considered

1. **Visibility flips rapidly (visible↔hidden churn).** The handler only acts on `=== 'visible'`. A rapid toggle just re-runs the catch-up a few times; each run is idempotent (resetting already-0 timers to 0, invalidating caches, one extra render). No state corruption. Worst case = a couple of redundant full renders, which the existing 250ms throttle on subsequent `onmessage` ticks would have produced anyway.

2. **WS not yet connected / closed while occluded.** Guarded by `ws && ws.readyState === WebSocket.OPEN`. If the WS is CLOSED/CLOSING on return, we skip the catch-up (nothing reliable to render) and leave reconnection to the existing path. See §6 for why a separate `pageshow` handler was NOT added.

3. **hidden→visible before the first tick / empty state.** Guarded by the `state.subAgents` emptiness check; also `renderSubAgents()` self-early-returns. No-op.

4. **Correctness under BOTH throttling scenarios (the key requirement).**
   - *Loop WAS starved:* buffered `onmessage`s fire on return and merge state; our handler resets the throttle timestamps and forces a render so the UI catches up with no extra gate delay.
   - *Loop was NOT starved (open-WS exemption kept it alive):* state is already current; the handler's reset+render is a harmless, correct catch-up (idempotent). No double-render bug because we don't schedule anything — we just paint once now.

5. **No-op when visibility never changes.** The listener only does work on a real `visibilitychange → visible` event. In a foreground tab that never backgrounded, the handler body is never entered → zero perf impact.

6. **Handler registration timing / duplicate registration.** Registered exactly once at init (top-level, after the TEMP probe block). Not inside any per-message handler, so it can't be re-registered per frame. It coexists with the TEMP probe's own `visibilitychange` logger (app.js:5583) — two independent listeners, both fire; the probe only logs.

7. **Exception safety.** Entire body wrapped in `try/catch`; a failure is logged and never propagates to break the app or block subsequent `onmessage` processing.

---

## 6. Optional hardening (`pageshow → connect()`) — SKIPPED (with rationale)

The task said add it **only if** the existing `onclose → scheduleReconnect()` path has an *obvious gap* for the background case. It does not:

- `scheduleReconnect()` (app.js:1706–1712) uses a 2s `setTimeout`. Even if Chrome throttles background timers to ~1 Hz, the timer **still fires** (throttling raises the minimum interval; it doesn't cancel the callback). So reconnection is not blocked while occluded — at worst it's delayed by the throttle granularity.
- `connect()` (app.js:1660) already guards against duplicate sockets with `if (ws && ws.readyState <= 1) return;` and clears `reconnectTimer` on `onopen` (app.js:1673). So even a redundant `pageshow → connect()` call would be a safe no-op, but it adds a second reconnect trigger for no proven benefit.

**Decision:** skip the `pageshow` handler to keep the change minimal and surgical, per the constraint. If future field data (the TEMP probe's `wsReady` reading on return) shows the WS is frequently CLOSED on resume AND reconnection is visibly lagging, a `pageshow → connect()` fallback can be added then with the existing `connect()` guard making it safe.

---

## 7. On-`hidden` behavior — deliberately minimal (no change)

Per requirement #3, I did **not** add any on-`hidden` work: data application in `onmessage` is left running if the loop is still alive, and no cosmetic-update suppression was added (it would add risk/complexity for marginal benefit). The handler early-returns on anything other than `visible`, so there is effectively no hidden-path code.

---

## 8. Verification

| Check | Command | Result |
|---|---|---|
| **Authoritative syntax check** | `node --check web_ui/app.js` | ✅ **PASS** (`NODE_CHECK_OK`) |
| Project `check_js.py` | `python check_js.py` | ⚠️ reports `Line 249: ')' does not match '{' from line 223` — **pre-existing false positive**, NOT from this change (line 249 is a comment; the naive matcher miscounts). Unrelated to lines 5587–5615. |
| Project `check_braces.py` | `python check_braces.py` | ⚠️ reports `Unclosed '{' from line 4861 / '(' & '[' from line 4864` — **pre-existing false positive** from the regex `/[#*\`_\[\]()]/g` at app.js:4864 (brackets inside a regex literal that the naive matcher miscounts). Unrelated to this change. |

Per the task note, `node --check` is the trusted source of truth; it passes cleanly. The two project checkers flag only pre-existing false positives located far from the edit (lines 249 and 4861/4864), confirming they are not caused by this change.

**No JS test framework exists** for the web UI (confirmed in the investigation report §8 — `web_ui/` has only utility checkers, no Jest/Mocha/Playwright). Manual testing below is the verification path.

---

## 9. How to test manually

1. Start the server and open the UI (`http://127.0.0.1:8126/`) in a **normal** (non-CDP) Chrome window.
2. Start a streaming task — ideally one that spawns a **subagent** (higher stream_update volume → bigger burst to catch up on).
3. Watch it stream smoothly in the foreground.
4. **Minimize the window** (or switch to another app) for ~1–2 minutes while it streams.
5. **Restore the window.** Expected: the UI catches up **instantly** — the latest merged content is painted immediately on return, rather than waiting for the next 250ms tick or showing a stale frame before the first buffered `onmessage` arrives.
6. Read the console `[stream-probe]` lines (still enabled): you should now see your new handler's effect alongside the probe — specifically, after the `visibilitychange -> visible` line, the backlog renders without an extra render-delay gap. (If a catch-up ever fails, a `[visibility] catch-up failed: <msg>` line will appear.)
7. **No-op check:** leave the tab in the foreground and stream normally — behavior should be identical to before (no added lag, no console noise from this handler), confirming zero foreground perf impact.

**What to watch for as a regression:** double-rendering / flicker on return, focus loss on `#chatInput` (`renderSubAgents()` already preserves input focus/caret at app.js:3905–3914), or any console error from the catch-up block. None expected given the guards + try/catch.

---

## 10. Out of scope / not touched
- No Python/backend files modified.
- Render throttle **intervals** (THROTTLE constants, app.js:44–56) unchanged — only the *timestamps* are reset on visibility return.
- Merge/splice logic (app.js:2032–2044) unchanged.
- TEMP `[stream-probe]` instrumentation left exactly as-is (`STREAM_DEBUG = true`, three gated blocks intact).
- No refactors; change is additive and matches existing style (`const state`, `state.genStats.*`, plain functions, `try/catch` + `console.error`).

## 11. Independent review & disposition of findings

An independent reviewer was delegated to check the change. Its verdict was "NEEDS WORK" on two points; both were investigated and **rejected as not applicable**, with evidence:

**Finding A (reviewer: "CRITICAL") — reset `lastContextBarUpdate` and `lastUiUpdate` too.**
→ **Rejected (dead code).** Grep across app.js shows `lastContextBarUpdate` (app.js:101) and `lastUiUpdate` (app.js:102) are *written* only in the `state.genStats` definition and `resetGenStats()` (app.js:4749–4750), but **never read as a throttle gate** — there is no `now - state.genStats.lastContextBarUpdate > ...` comparison anywhere. Resetting them would be a no-op assignment with zero effect. The *actual* activity-bar throttle is `ActivityBar.lastRenderTime` (app.js:577, 706) — an instance field on the ActivityBar object, **not** under `state.genStats`, so it is out of scope for a genStats reset and would require touching the ActivityBar class (excluded by the minimal-change constraint). The context bar (`updateAllContextBars()`, app.js:4712–4725) has **no time-gate at all** — it repaints on every call, so there is no catch-up gap to fix. The task explicitly scoped exactly these 5 timers; staying in scope is correct.

**Finding B (reviewer: "MAJOR") — handler "is not a no-op when the tab was never backgrounded" because it "fires once at init".**
→ **Rejected (incorrect premise).** `visibilitychange` only fires on an actual *change* to `document.visibilityState`; it does **not** fire at page load. A tab that remains visible for its whole life never dispatches the event, so the handler body never runs → genuinely a no-op with zero foreground perf impact. The comment is accurate as written.

**Other reviewer notes (minor/nit) — disposition:**
- *Defensive `if (!mainTabPanels) return;` guard:* not needed — `invalidateAllPanelCaches()` already self-guards (`if (!mainTabPanels) return;`, app.js:512), and the handler is wrapped in try/catch. Adding a redundant check would be noise.
- *Debounce rapid visibility flips:* not needed for correctness — each run is idempotent (resetting 0→0, invalidating caches, one extra render that `renderSubAgents()`'s content-key/count dedup at app.js:4172 already handles). Churn cost is negligible and a debounce would add complexity the task asked to avoid.
- *Comment verbosity:* acceptable; kept for traceability of the non-obvious "why".

**Net:** No code changes required from review. The 5-timer reset, cache invalidation, forced render, guards, and try/catch are all correct and complete for the stated scope.

---

**Status:** Implemented + syntax-verified + independently reviewed (findings investigated & dispositioned). **Not committed** — awaiting Maine's final review.
