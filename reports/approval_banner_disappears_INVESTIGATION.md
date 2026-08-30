# Approval Banner Intermittent Disappearance — Root Cause Investigation

**Date:** 2026-08-31  
**Investigator:** approval_banner_research  
**Status:** CONFIRMED (primary hypothesis)  

---

## Executive Summary

The approval banner disappears because **full-state and stream-update WebSocket messages carry an `approvals` field that can be stale (empty `[]`) relative to a concurrently-registered pending approval**. The client unconditionally overwrites `state.approvals` on every `state`, `stream_update`, and `approvals` message. When a full-state or stream tick — built *before* the approval was registered — arrives *after* the dedicated `approvals` broadcast (which correctly includes the new approval), it clobbers `state.approvals` back to `[]`, and `renderApprovals()` hides the bar.

The ~30% rate reflects the probability that a full-state or stream tick (triggered by config sync, endpoint CRUD, agent selection, or the next streaming tick) is in-flight during the narrow window between approval registration and the `_approval_loop`'s first broadcast (≤ 0.3 s).

---

## End-to-End Approval Flow

### Server Side

| Step | File | Line(s) | Description |
|------|------|---------|-------------|
| 1 | `agent_cascade/operation_manager/approval.py` | 84–116 | `request_user_approval()` creates a `PendingApproval`, adds it to `self.pending[request_id]` under `self._lock` (line 115–116), then blocks in a 0.1 s poll loop (line 123–134). |
| 2 | `agent_cascade/operation_manager/approval.py` | 180–194 | `list_pending_approvals()` reads `self.pending` under the same lock and returns a list of dicts. |
| 3 | `agent_cascade/api_integration_pkg/state_builder.py` | 934–941 | `_get_approvals(pool)` calls `pool.operation_manager.list_pending_approvals()`; returns `[]` on any exception or if `operation_manager` is absent. |
| 4a | `agent_cascade/api_integration_pkg/state_builder.py` | 233–399 | `build_state_from_pool()` includes `'approvals': pending_approvals` (line 379) in every full-state payload. |
| 4b | `agent_cascade/api_integration_pkg/state_builder.py` | 401–565 | `build_stream_update_from_pool()` includes `'approvals': pending_approvals` (line 562) in every stream-tick payload. |
| 5a | `agent_cascade/api_server.py` | 461–475 | `build_state()` delegates to `build_state_from_pool()`. |
| 5b | `agent_cascade/api_server.py` | 442–451 | `_broadcast_state()` sends `{'type': 'state', **build_state()}` to all WS clients. |
| 5c | `agent_cascade/api_server.py` | 1141–1151 | `ws_chat()` sends initial `{'type': 'state', **build_state()}` on connect. |
| 6 | `agent_cascade/api_integration_pkg/streaming.py` | 34–141 | `broadcast_stream_update()` calls `build_stream_update_from_pool()` and queues `{'type': 'stream_update', **stream_update}` via `asyncio.run_coroutine_threadsafe`. |
| 7 | `agent_cascade/run_agent_unified.py` | 199–207 | Main agent loop calls `broadcast_stream_update()` on every tick; `is_streaming_tick=True` when a tool event occurs (line 206: `is_streaming_tick or has_tool_event`). |

### Client Side

| Step | File | Line(s) | Description |
|------|------|---------|-------------|
| 1 | `web_ui/app.js` | 1676–1683 | `ws.onmessage` → `handleServerMessage(data)`. |
| 2 | `web_ui/app.js` | 1775–1842 | `handleServerMessage` case `'state'`: if `'approvals' in data && Array.isArray(data.approvals)` → `state.approvals = data.approvals` (line 1840) → `renderApprovals()` (line 1842). |
| 3 | `web_ui/app.js` | 2137–2142 | `handleServerMessage` case `'stream_update'`: same logic (line 2140) → `renderApprovals()` (line 2142). |
| 4 | `web_ui/app.js` | 2230–2235 | `handleServerMessage` case `'approvals'`: same logic (line 2233) → `renderApprovals()` (line 2235). |
| 5 | `web_ui/app.js` | 3602–3626 | `renderApprovals()`: if `state.approvals.length === 0` → `bar.style.display = 'none'` (line 3622). |
| 6 | `web_ui/index.html` | 152 | `<div class="approval-bar" id="approvalBar" style="display:none;">` — the DOM element. |

### Dedicated Approval Broadcast

The `_approval_loop` (in `approval.py`, polls every ~0.3 s) broadcasts `{'type': 'approvals', 'approvals': pending}` only when the ID set changes. This is the *correct* mechanism for approval state changes. However, the full-state and stream-update paths **also** carry `approvals`, creating a competing write path.

---

## Root Cause

### Primary: Stale State Clobbering (Timing Race)

**Mechanism:**

```
Timeline (race window):

Agent thread:        [tool call starts] → request_user_approval() → pending[rid] = ap
                                                             ↓ (immediate, line 115-116)
Approval loop:         …polls… → detects new ID → broadcasts {type:'approvals', approvals:[ap]}
                                                             ↓ (≤ 0.3 s)
Stream/State tick:   [built at T₀ before pending[rid] exists] → approvals: []
                                                             ↓ (queued, delivered after approval broadcast)

Client receives:     1. {type:'approvals', approvals:[ap]}  → state.approvals=[ap] → banner SHOWS
                    2. {type:'state'|'stream_update', approvals:[]} → state.approvals=[] → banner HIDES
```

**Why ~30%:** The race window is the time between (a) the approval being registered in `self.pending` and (b) the `_approval_loop`'s next poll detecting it and broadcasting. This is ≤ 0.3 s. If a full-state or stream tick is triggered *during* that window (by any of the many triggers listed below) AND its `build_state_from_pool()` / `build_stream_update_from_pool()` call reads `pending` *before* step (a), the resulting payload has `approvals: []`. WebSocket per-connection ordering then delivers the stale payload *after* the approval broadcast, clobbering it.

The probability depends on how frequently full-state / stream ticks are in-flight. During active generation (stream ticks every ~100 ms) the probability is high; during idle it's lower. The 30% figure is consistent with typical generation activity.

**Triggers that can produce a stale full-state or stream tick:**

| Trigger | File | Line(s) |
|---------|------|---------|
| Stream tick (every ~100 ms during generation) | `run_agent_unified.py` | 199–207 |
| Config sync (`update_config` from client on connect) | `ws_handlers.py` | via `handle_update_config` → `_broadcast()` |
| Endpoint CRUD (add/update/remove) | `api_server.py` | 1078, 1091, 1103, 1119, 1132 |
| Agent dismissal | `api_server.py` | 738 |
| Reset | `api_server.py` | 937 |
| `set_auto_security` toggle | `ws_handlers.py` | 982 |
| Initial state on WS connect | `api_server.py` | 1146–1151 |

### Secondary: `done` Message Path

When an agent turn completes, `_broadcast_state('done')` is called (`api_server.py:937`). If this full state was built just before the approval was registered (e.g., the agent's turn "completes" in the sense of yielding to the approval wait, and a state snapshot is taken at that moment), it could carry `approvals: []`. However, this is less likely because `request_user_approval()` registers the approval *before* the agent yields.

### Ruled Out

- **Auto-security toggle:** `state.autoSecurity` is `false` by default on the client (line 109) and `renderApprovals()` only hides the bar if `autoSecurity === true` AND there are approvals. The toggle sync uses a 100 ms debounce (line 1861) and does not clear `state.approvals`. Not the cause.
- **Client-side timeout / auto-dismiss:** No timer-based auto-dismiss of the approval bar exists. The bar stays until `state.approvals` is cleared or the user approves/rejects.
- **Instance switch / agent selection:** Switching agents re-renders panels but does not clear `state.approvals` (which is global, not per-instance).
- **CSS visibility:** The bar uses `display:none` only when `renderApprovals()` explicitly sets it (line 3622). No CSS animation or transition auto-hides it.

---

## Evidence: The Clobbering Code

### Client: Unconditional Overwrite

```javascript
// app.js:1790-1791 — 'state' and 'done' fall through to the SAME handler
case 'state':
case 'done':
    // ...
    // app.js:1839-1842
    if ('approvals' in data && Array.isArray(data.approvals)) {
        state.approvals = data.approvals;  // ← overwrites with [] if stale
    }
    renderApprovals();

// app.js:2139-2142 (stream_update message)
if ('approvals' in data && Array.isArray(data.approvals)) {
    state.approvals = data.approvals;  // ← overwrites with [] if stale
}
renderApprovals();
```

The comment at line 2138 explicitly states the design intent: *"FIX: Use Array.isArray check to update approvals (including empty array to clear all)"*. This was added to ensure approvals are cleared when the server confirms they're resolved. But it also means *any* stale empty array clobbers live approvals.

**Note:** `done` messages (sent on agent turn completion via `_broadcast_state('done')`, `api_server.py:937`) use the *same* code path as `state` (line 1790-1791 fall-through), so they are an **additional clobbering vector** — a `done` broadcast built before approval registration will also hide the banner.

### Server: Approvals in Every Payload

```python
# state_builder.py:379 (full state)
'approvals': pending_approvals,

# state_builder.py:562 (stream update)
'approvals': pending_approvals,
```

Both `build_state_from_pool()` and `build_stream_update_from_pool()` include `approvals` in every payload. The value is read at build time via `_get_approvals(pool)` (line 934–941), which reads the *current* `pending` dict. The issue is not that the read is wrong — it's that a build initiated *before* the approval is registered will correctly return `[]`, but be *delivered* after the approval broadcast.

### Server: Registration Before Blocking

```python
# approval.py:115-116
with self._lock:
    self.pending[request_id] = approval
# Then blocks (line 123-134)
```

The approval IS registered before the tool call blocks. So any state build that happens *after* line 116 will include it. The race is with state builds that were *initiated* (and had `_get_approvals()` called) *before* line 116 but are *delivered* after the approval broadcast.

---

## Why Manual Refresh Fixes It

`ws.onopen` (line 1647–1658) does not re-fetch state via HTTP. However, the server sends initial state on connect (line 1146–1151). A page refresh closes the old WS, opens a new one, and the server sends a fresh `build_state()` that reads the *current* `pending` (which still contains the approval, since the agent is still blocked waiting). This correctly populates `state.approvals` and the banner reappears.

---

## Recommended Fix Directions (NOT implemented)

### Option A: Remove `approvals` from Stream Updates (Server-Side, Minimal)

**Change:** In `build_stream_update_from_pool()` (`state_builder.py:562`), omit the `approvals` field entirely. Keep it in `build_state_from_pool()` (for initial load and explicit refreshes).

**Effect:** Stream ticks can no longer clobber approval state. The dedicated `approvals` message type (from `_approval_loop`) becomes the sole source of approval state changes during active generation.

**Risk:** If the client relies on stream updates to *clear* approvals (e.g., when all are resolved), it would need the `approvals` message type to handle clearing. Verify that `_approval_loop` broadcasts an empty array when all approvals are resolved (it should, since it broadcasts on ID set changes).

**Effort:** ~1 line change (remove the key from the dict).

### Option B: Client-Side Monotonic Guard (Client-Side)

**Change:** In `handleServerMessage`, when processing `state` or `stream_update`, only accept `data.approvals` if it's *not* a regression (i.e., if the incoming array is empty but `state.approvals` was non-empty within the last N ms, defer the clear by one message or use a sequence number).

**Effect:** Prevents a single stale empty array from clobbering a live approval.

**Risk:** More complex; introduces a new timing constant. Could delay legitimate clears (e.g., user approved all pending items and the server sends `approvals: []`).

**Effort:** ~15–20 lines of client logic.

### Option C: Sequence Number / Generation ID (Server + Client)

**Change:** Add a monotonically increasing `approval_seq` to every payload that includes `approvals`. Client only applies `data.approvals` if `data.approval_seq >= state.approval_seq`. The `_approval_loop` increments the seq on each broadcast; full-state / stream builds read the current seq.

**Effect:** Stale payloads (lower seq) are ignored.

**Risk:** Most robust but highest effort. Requires coordination between `_approval_loop` and the state builders.

**Effort:** ~30–50 lines across 3–4 files.

### Recommendation

**Option A** is the minimal, lowest-risk fix. It addresses the root cause (stale stream ticks clobbering approval state) with a 1-line change, and the dedicated `approvals` message type already exists and handles all approval state transitions. Verify that `_approval_loop` broadcasts an empty list when the pending set becomes empty (it should, per the ID-set-change logic).

---

## Relevant Test Files

| File | Coverage |
|------|----------|
| `agent_cascade/tests/test_approval.py` | Unit tests for `request_user_approval`, `user_approve`, `user_reject`, `list_pending_approvals`. |
| `agent_cascade/tests/test_state_builder.py` | Tests for `build_state_from_pool` and `build_stream_update_from_pool` output structure (including `approvals` field). |
| `agent_cascade/tests/test_api_server.py` | Integration tests for `build_state()`, `_broadcast_state()`, WS connect initial state. |
| `agent_cascade/tests/test_ws_handlers.py` | Tests for `handle_set_auto_security`, `handle_update_config` broadcast paths. |

*Note: No existing test covers the specific race condition (stale state clobbering a fresh approval broadcast). A regression test would need to simulate the interleaving of a stale full-state and a fresh `approvals` broadcast on the same WS connection.*

---

## Open Questions

1. **~~Does `_approval_loop` broadcast an empty `approvals` list when all pending approvals are resolved?~~** ✅ **CONFIRMED — `api_server.py:758-762`:** When `resolved_ids` is non-empty (approvals removed), the loop broadcasts `{'type': 'approvals', 'approvals': pending}` where `pending` is the *current* list (possibly `[]`). This means **Option A is safe**: after removing `approvals` from stream updates, the client will still receive clearing signals via the dedicated `approvals` message type.

2. **Are there other code paths that trigger a full-state broadcast during active approval waits?** The list in the "Triggers" table covers all known `_broadcast_state()` and `self._broadcast()` call sites found via grep. `security_handler.py:878-883` and `905-913` also broadcast `approvals` directly (for auto-apply and security-check resolution) — these use the correct `list_pending_approvals()` snapshot and are not clobbering vectors.

3. **~~Is the `done` message type handled separately from `state` in the client?~~** ✅ **CONFIRMED — `app.js:1790-1791`:** `case 'state': case 'done':` fall through to the same handler. A `done` broadcast (sent on turn completion, `api_server.py:937`) that was built before approval registration will also clobber `state.approvals`. This is an additional clobbering vector alongside `stream_update`.

---

## Confidence Level

- **Primary hypothesis (stale state clobbering): HIGH** — confirmed by direct code inspection of both the server (approvals in every payload) and client (unconditional overwrite on empty array).
- **~30% rate explanation: MODERATE** — consistent with the timing math (0.3 s window × stream tick frequency) but not measured.
- **Ruled-out causes: HIGH** — auto-security, CSS, timeouts, and instance-switch paths all verified as non-causal.
