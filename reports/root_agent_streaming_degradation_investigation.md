# Streaming Degradation on Root Agent — Investigation Report

**Date:** 2026-08-23
**Investigator:** streaming-investigation (researcher)
**Task:** Root cause of non-incremental (full-refresh-only) UI streaming for the root agent
**Mode:** Investigation only — no files modified

---

## Executive Summary

The backend LLM streaming pipeline is **fully intact and identical for root and sub-agents**
(`stream=True, delta_stream=False` at every call site; per-chunk yields verified end-to-end).
The degradation is a **frontend JavaScript crash inside the WebSocket message handler**, caused
by a Python-style `logger.debug(...)` call left in browser code where no `logger` exists.

Every `stream_update` (incremental) message that arrives while an approval is pending in
Auto-Ask mode throws `ReferenceError: logger is not defined` at the `renderApprovals()` call,
aborting processing of that message — so incremental chunks never render. The periodic full
`state` broadcast (~every 15s / every 100 ticks) survives because its message-array merge
completes before the same crashing call, so the UI eventually shows the full response —
exactly the reported symptom.

**Confidence: High** — direct browser-console evidence (`[WS] Failed to process server message:
logger is not defined` ×7) matches the exact failing line found by static analysis.

---

## Root Cause

### The bug (frontend)

`web_ui/app.js:3623` (duplicated verbatim in `web_ui/_app_stripped.js:3575`):

```js
logger.debug(`[AUTO-ASK] Triggering security check for ${ap.request_id} ...`);
```

`logger` is not defined anywhere in the frontend bundle (grep across web_ui: only these two
hits, both this statement). Any execution of this line throws
`ReferenceError: logger is not defined`, caught by the outer handler at app.js:1654-1661:

```js
ws.onmessage = (event) => {
    try { const data = JSON.parse(event.data); handleServerMessage(data); }
    catch (err) { console.error('[WS] Failed to process server message:', err.message); }
};
```

### Why it breaks streaming but not full refresh

Both WS handlers call `renderApprovals()`, which executes the buggy line whenever:
- Auto-Security (Auto-Ask) toggle is ON, **and**
- at least one approval is pending (`pending.length > 0`, app.js:3613-3624).

The difference is statement order:

| Handler | Merge messages | renderApprovals | Render |
|---|---|---|---|
| `stream_update` (app.js:1962+) | ✅ lines 1979-2062 | 💥 line 2113 | ❌ never reached (lines 2139-2156) |
| `state`/`done` (app.js:1768+) | ✅ lines 1784-1798 | 💥 line 1820 | ✅ already done at line 1927 |

For `stream_update`: state mutation completes, then the exception aborts the case block before
`renderSubAgents()` → incremental content never paints.
For `state`: merge happens first, then exception aborts before the explicit re-render — but the
message data was already merged into `state.subAgents`; the next successful render path (e.g.,
the next full refresh or any non-crashing tick's render) shows everything. Net effect: user sees
only full-refresh updates.

### Trigger condition (explains intermittency + root-only symptom)

The crash requires a pending approval during generation. In this system approvals arise from
the **root agent's tool calls** (shell_cmd etc.), so sub-agent-only stretches stream fine.
It also explains why a restart "fixes" it: fresh session has no pending approval until the root
agent makes its next approval-gated tool call. Backend dedup (security_handler.py:293-299)
prevents duplicate Security runs per request_id, but the frontend keeps throwing on every tick
while the approval sits pending.

### Not the cause (verified)

Recent commits 8f4103c..824c4e3 are exonerated:
- `oai.py` Change E diff = breaker consult added to `_detect_context_window` only (no stream changes).
- `router.py:call_with_fallback` preserves generator semantics (first-chunk pull + `yield from`
  wrapper, router.py:996-1017). D1 pre-loop wait only delays start; doesn't buffer output.
- All engine call sites use `stream=True, delta_stream=False` (engine/llm_call.py:1222-1228, 1270-1276).
- Compression (201→62 msgs, Compressor_1/2) does not touch the streaming publisher;
  `_update_streaming_responses` (engine/core.py:1336-1367) and `_serialize_instances_incremental`
  behave identically for root and sub-agents.

Pre-existing: introduced 2026-06-18 in commit bb74023 ("fix: resolve security check lock deadlock
from concurrent Auto-Ask checks") — ~963 commits ago. Surfaced now because the user recently ran
with Auto-Ask ON while the root agent made approval-gated tool calls mid-generation.

## Minimal Fix

Delete or guard `web_ui/app.js:3623` (and `_app_stripped.js:3575`):

```js
// Before
logger.debug(`[AUTO-ASK] Triggering security check for ${ap.request_id} ...`);
// After
console.debug(`[AUTO-ASK] Triggering security check for ${ap.request_id} ...`);
```

Optionally add a lint rule banning bare `logger.` in web_ui/*.js to prevent recurrence.

## Open Questions / Residual Risks

1. `_app_stripped.js` appears unreferenced by index.html (which loads app.js) but contains the
   same bug — confirm whether anything serves it; fix both regardless.
2. Secondary robustness gap (not this bug): a single throw in one WS message type can starve
   others sharing the handler. Consider per-case try/catch in handleServerMessage.
3. WS queue-full drops (maxsize=128, silent drop in _put_stream_update) remain a known source
   of occasional gaps under heavy load, but are unrelated to this deterministic failure mode.
