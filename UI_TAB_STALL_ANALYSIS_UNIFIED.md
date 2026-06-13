# UI Tab Stall Issue Analysis — Unified Branch vs Main Branch (commit 1b544ce)

## Executive Summary

The unified branch at `N:\work\WD\AgentCascade_unified` has **all 5 issues** that were fixed in the main branch. However, the code architecture differs significantly between branches, requiring a mixed approach: some fixes can be applied directly with small modifications, while others need custom approaches due to structural differences.

---

## Issue-by-Issue Analysis

### Fix #1: Streaming Threshold — Hardcoded 3-Message Tail

**Status: ❌ ISSUE EXISTS IN UNIFIED BRANCH**

| Branch | File | Line(s) | Details |
|--------|------|---------|---------|
| **Unified** | `agent_cascade/api_integration.py` | Lines 1008-1014 | Threshold is 30 ✅, but tail size is hardcoded to 3 ❌ |
| Main (fixed) | `api_server.py` | Lines 632-635 | Threshold 30 ✅, proportional tail `max(5, len(msgs)//10)` ✅ |

**Unified branch code (lines 1008-1014):**
```python
if streaming and current_state == AgentState.RUNNING and len(msgs) > 30:
    # During active generation only send the tail (last 3 messages) for large
    # conversations to avoid O(N²) serialisation on every ~150ms tick.
    start_idx = max(0, len(msgs) - 3)
    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs[-3:], start_idx)]
```

**Assessment:** The threshold is already at 30 (same as the fix), but the tail size `3` is hardcoded. This needs the same fix: replace with `max(5, len(msgs) // 10)`.

**Fix complexity:** LOW — Direct replacement, same location, same function (`_serialize_instance`).

---

### Fix #2: Periodic Full State Refresh (Every 100 Iterations)

**Status: ❌ ISSUE EXISTS IN UNIFIED BRANCH**

| Branch | File | Line(s) | Details |
|--------|------|---------|---------|
| **Unified** | `agent_cascade/run_agent_unified.py` | Lines 116-224 | No periodic full state refresh mechanism ❌ |
| Main (fixed) | `api_server.py` | Lines 1176-1178 | `force_full = (tick_num % 100 == 0)` ✅ |

**Unified branch code (key section):**
```python
# run_agent_unified.py, lines 163-224
should_broadcast = (
    now - last_send > 0.15
    or len_changed
    or has_tool_event
)
# ... broadcasts stream_update only when conditions met ...
# No periodic full state refresh exists
```

**Assessment:** The unified branch's streaming architecture is fundamentally different — it uses an asyncio queue (`send_queue`) and event-driven broadcasting rather than the old tick-based loop. There is NO mechanism to force a full state refresh periodically, which means any messages lost due to partial updates or dropped queue events will never be recovered until generation ends.

**Fix complexity:** MEDIUM-HIGH — Requires inserting a periodic full-state broadcast in the streaming loop. Since `tick_num` already exists (line 116), adding `force_full = (tick_num % 100 == 0)` is straightforward, but the broadcast mechanism differs (uses `build_stream_update_from_pool` with `streaming=False` flag vs the old `get_sub_agent_state(streaming=(not force_full))`).

---

### Fix #3: Client-Side Hole-Patching in Merge Logic

**Status: ❌ ISSUE EXISTS IN UNIFIED BRANCH**

| Branch | File | Line(s) | Details |
|--------|------|---------|---------|
| **Unified** | `web_ui/app.js` | Lines 1185-1202 | Hole-patching creates undefined entries ❌ |
| Main (fixed) | `web_ui/app.js` | Lines 914-936 | Proper array replacement when startIdx > existing.length ✅ |

**Unified branch code (lines 1185-1202):**
```javascript
// Normal merge path
const startIdx = hCount - sa.messages.length;
if (startIdx >= 0 && startIdx <= existing.messages.length) {
  existing.messages.length = startIdx;     // Creates undefined entries!
  existing.messages.push(...sa.messages);
} else if (startIdx < 0) {
  console.warn(`... server inconsistency`);  // Just warns, doesn't fix
}
// Missing: replacement when startIdx > existing.messages.length
```

**Main branch fixed code (lines 914-936):**
```javascript
const startIdx = hCount - sa.messages.length;
if (startIdx >= 0) {
  // If server's partial is beyond our array length, replace entirely to avoid holes.
  if (startIdx > existing.messages.length) {
    existing.messages = [...sa.messages];   // REPLACES instead of creating holes
  } else {
    existing.messages.length = startIdx;
    existing.messages.push(...sa.messages);
  }
} else {
  // Server has fewer messages than client (rollback/compression). Replace entirely.
  existing.messages = [...sa.messages];
}
```

**Two specific problems in unified branch:**
1. **Missing `startIdx > existing.messages.length` check** — When the server sends a partial update that extends beyond the client's array, it creates undefined entries instead of replacing entirely.
2. **Missing rollback handling for `startIdx < 0`** — The unified branch only logs a warning instead of replacing messages entirely (needed when server compresses/rolls back).

**Fix complexity:** LOW-MEDIUM — Same merge logic pattern, just needs the two additional conditions from the main branch fix.

---

### Fix #4: Tab Switch Throttle Timer Not Reset

**Status: ❌ ISSUE EXISTS IN UNIFIED BRANCH**

| Branch | File | Line(s) | Details |
|--------|------|---------|---------|
| **Unified** | `web_ui/app.js` | Lines 2751-2788 | No throttle reset before renderSubAgents() ❌ |
| Main (fixed) | `web_ui/app.js` | Lines 2617-2621 | `state.genStats.lastSubAgentRender = 0` ✅ |

**Unified branch code (`switchMainTab`, lines 2751-2788):**
```javascript
function switchMainTab(tabId) {
  // ... tab button/panel updates ...
  state.activeSubTab = tabId;
  ActivityBar.setActiveTab(tabId);
  
  // Trigger immediate render — but NO throttle reset!
  renderSubAgents();   // ← May be throttled for up to 750ms
  
  // ... active class re-application ...
}
```

**Main branch fixed code (`switchMainTab`, lines 2617-2621):**
```javascript
if (tabId === 'chat') {
  lastRenderedCount = Infinity;
  renderMessages();
} else {
  // Reset sub-agent render throttle timer so the tab renders immediately when switched to.
  state.genStats.lastSubAgentRender = 0;   // ← THE FIX
  renderSubAgents();
}
```

**Assessment:** The unified branch's `switchMainTab` calls `renderSubAgents()` directly without resetting `state.genStats.lastSubAgentRender = 0`. This means if the user switches tabs during a throttle window, the newly visible tab may not render for up to 750ms, causing the "stall" effect.

**Fix complexity:** LOW — Single line addition: `state.genStats.lastSubAgentRender = 0;` before `renderSubAgents()`.

---

### Fix #5: Defensive Null Message Guards

**Status: ⚠️ PARTIALLY ADDRESSED (different architecture)**

| Branch | File | Line(s) | Details |
|--------|------|---------|---------|
| **Unified** | `web_ui/app.js` | Line 1548 | No null check in iteration loop ❌ |
| Main (fixed) | `web_ui/app.js` | Lines 2422-2427 | Null/undefined message guard with placeholder ✅ |

**Unified branch code (`renderAgentConversation`, line 1548):**
```javascript
for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    // No null check — if msg is undefined, createMessageEl will crash
    const el = createMessageEl(msg, origIndex, config);
    fragment.appendChild(el);
}
```

**Main branch fixed code (`createSubMsgEl`, lines 2422-2427):**
```javascript
// FIX F1: Handle null/undefined messages by rendering a placeholder showing how many were missed.
if (!msg) {
  div.className = 'sub-msg sub-msg-unknown missed-msg';
  // ... renders "N messages skipped" placeholder ...
}
```

**Assessment:** The unified branch's `renderAgentConversation` doesn't guard against null/undefined messages in the iteration loop. While this is less likely to occur (the unified branch's merge logic has a defensive fallback at line 1200: `existing.messages = existing.messages || sa.messages || []`), it's still a missing safety net. The main branch handles this at the rendering level with a visual placeholder for missed messages.

**Fix complexity:** LOW — Add `if (!msg) { /* render placeholder or skip */ }` inside the iteration loop in `renderAgentConversation`.

---

## Summary Table

| # | Issue | Exists in Unified? | Location | Fix Complexity |
|---|-------|-------------------|----------|----------------|
| 1 | Hardcoded 3-message tail | ✅ Yes | `api_integration.py:1008-1014` | LOW |
| 2 | No periodic full state refresh | ✅ Yes | `run_agent_unified.py:116-224` | MEDIUM-HIGH |
| 3 | Hole-patching in merge logic | ✅ Yes | `web_ui/app.js:1185-1202` | LOW-MEDIUM |
| 4 | Tab switch throttle not reset | ✅ Yes | `web_ui/app.js:2751-2788` | LOW |
| 5 | Null message guards | ⚠️ Partially | `web_ui/app.js:1548` | LOW |

## Recommendations

1. **Fix #1** can be applied directly — replace `msgs[-3:]` with `msgs[-tail_size:]` where `tail_size = max(5, len(msgs) // 10)`.
2. **Fix #2** requires architecting a periodic full-state broadcast in the asyncio queue flow — likely by checking `tick_num % 100 == 0` and broadcasting a non-streaming `build_state_from_pool()` result periodically.
3. **Fix #3** needs two additional conditions in the merge logic to handle array extension beyond existing length and server-side rollback.
4. **Fix #4** is a single-line addition before `renderSubAgents()`.
5. **Fix #5** can be added as a guard in the message rendering loop.