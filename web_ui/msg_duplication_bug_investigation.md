# Message Duplication Bug Investigation — `web_ui/app.js`

## Executive Summary

The message duplication bug occurs in the **incremental rendering logic** of `renderSubAgentPanel()` (line 2373) in the unified branch. The root cause is a **timing/state-synchronization issue** where new messages can be appended to the DOM via the append path while old DOM elements from a previous render cycle are still present.

---

## Architecture Background: Unified Branch vs Main Branch

### Main Branch
- Root agent messages stored in `state.messages` (flat array)
- Sub-agent messages stored in `state.subAgents[name].messages`
- Separate rendering functions: `renderMessages()` for root, `renderSubAgentPanel()` for sub-agents
- Global `lastRenderedCount` variable — set to **`Infinity`** on full state updates to guarantee a full re-render

### Unified Branch (BUGGY)
- Root agent messages now stored in `state.subAgents[rootName].messages` (same structure as sub-agents)
- Single rendering path: `renderSubAgents()` → `renderSubAgentPanel()` for ALL agents
- Per-panel cache via `panel.dataset.contentKey` and `panel.dataset.lastRenderedCount`
- On full state updates, `lastRenderedCount` is reset to **`'0'`** (string)

---

## Root Cause Analysis

### The Two Rendering Paths in `renderSubAgentPanel()` (lines 2458–2496)

```javascript
// Lines 2458-2463: FULL RE-RENDER PATH
if (currentCount < lastCount || lastCount === 0) {
    scrollContainer.innerHTML = '';                                    // ← Clears ALL DOM
    scrollContainer.appendChild(renderAgentConversation(...));         // ← Adds ALL messages
}

// Lines 2467-2478: APPEND PATH
else {
    const newMsgs = [];
    for (let i = lastCount; i < currentCount; i++) {
        newMsgs.push(displayMsgs[i]);
    }
    scrollContainer.appendChild(renderAgentConversation(...newMsgs));  // ← Adds ONLY new messages
}
```

### The Bug: Append Path Fires When DOM Still Has Old Elements

The append path appends NEW message elements starting from `lastRenderedCount` index. It does **NOT** check whether the DOM container already has those same elements rendered.

**Duplication scenario:**

1. **Render A** — Full re-render (e.g., after `state/done`):
   - `scrollContainer.innerHTML = ''` → DOM cleared
   - All messages rendered → `lastRenderedCount = N`
   - DOM has N message elements ✓

2. **Cache invalidation fires** (e.g., `stream_update` with `!generating` at lines 1115-1123):
   - `contentKey = ''`, `lastRenderedCount = '0'` ← Reset to 0

3. **Render B — DOM is already cleared, but...**:
   - If Render A's append operation was still pending or if another render happened between steps:
   - `scrollContainer.innerHTML = ''` fires → DOM cleared again
   - All messages re-rendered → `lastRenderedCount = N`

4. **But here's the critical race:**
   - If a **third render** arrives before `lastRenderedCount` is synced:
   - `currentCount > lastCount` (e.g., 3 > 2)
   - Append path fires → adds messages[2]
   - But message[2] was already added by the previous full re-render!
   - **RESULT: Duplicate DOM elements**

### Why This Happens in Unified But Not Main

In the main branch, `lastRenderedCount = Infinity` on state updates. Since `currentCount < Infinity` is always true for any finite message count, the next render ALWAYS takes the full re-render path — it never appends after a cache clear.

In the unified branch, `lastRenderedCount = '0'`. After the full re-render sets it to `N`, if a subsequent render fires with `currentCount > N`, the append path is taken. But if the DOM state is out of sync (e.g., container was cleared by another code path), duplicates result.

---

## Specific Bug Locations

### 1. Cache Invalidation Only Targets Root Panel (Lines 1115–1123)

```javascript
// stream_update handler — only when generation JUST started
if (!state.generating) {
    // Invalidate root panel cache to force re-render on fresh generation
    const rootPanel = document.getElementById(getRootPanelId());
    if (rootPanel) {
        const msgsEl = rootPanel?.querySelector('.messages');
        if (msgsEl) {
            msgsEl.dataset.contentKey = '';
            msgsEl.dataset.lastRenderedCount = '0';
        }
    }
}
```

**Problem:** Only the root panel's cache is invalidated. Sub-agent panels keep their stale `lastRenderedCount` values. If a sub-agent tab was visible during the state change, its panel could have a mismatch between DOM content and cached count.

### 2. `renderSubAgentPanel` Append Path Has No Deduplication (Lines 2467–2478)

```javascript
} else {
    // Append new messages using unified rendering with indexMap to preserve indices
    const newMsgs = [];
    for (let i = lastCount; i < currentCount; i++) {
        newMsgs.push(displayMsgs[i]);
    }
    scrollContainer.appendChild(renderAgentConversation(name, newMsgs, 1, newIndexMap, subConfig));
}
```

**Problem:** No check whether `scrollContainer` already contains elements at indices `lastCount` through `currentCount-1`. If the DOM was cleared by another code path (e.g., tab switch, retry), these elements would be duplicates.

### 3. Multiple Render Triggers Can Fire in Quick Succession

The following code paths all call `renderSubAgents()`:
- **Line 960**: `state/done` handler → `renderSubAgents()`
- **Lines 1182–1183**: `stream_update` handler → conditional `renderSubAgents()`
- **Line 2311**: Close tab handler → `renderSubAgents()`
- **Line 2541**: `switchMainTab()` → `renderSubAgents()`

**Problem:** When a user sends a message and the server responds with both `state/done` AND `stream_update`, these can trigger multiple render cycles in rapid succession. The DOM operations from one cycle may not complete before the next starts, causing append-to-duplicate.

### 4. Root Agent Message Assignment Race in `state/done` Handler (Lines 863–877)

```javascript
// Step 1: Route server's data.messages into subAgents under the root agent name
if (data.messages) {
    state.subAgents[rootName] = Object.assign({}, state.subAgents[rootName], {
        messages: data.messages,
        is_partial: false,
    });
}

// Step 2: Merge agent_instances — don't overwrite root data
if (data.agent_instances) {
    for (const [name, sa] of Object.entries(data.agent_instances)) {
        state.subAgents[name] = sa;
    }
}
```

**Problem:** If `data.messages` and `data.agent_instances[rootName]` both contain root agent data with different message arrays, the second assignment overwrites the first. This can cause:
- Messages lost (if `agent_instances` has fewer)
- Messages duplicated (if both paths are processed by different render cycles)

---

### 5. Root Agent Double-Processing in `stream_update` Handler (Lines 1006–1076)

```javascript
// Lines 1006-1021: Direct root agent message merge (truncate + push)
const rootData = state.subAgents[rootName];
if (rootData && rootData.messages) {
    if (historyCount > rootData.messages.length) {
        // keep existing
    } else {
        rootData.messages.length = historyCount;  // truncate
    }
    rootData.messages.push(...responseMsgs);    // push new messages
}

// Lines 1075-1076: OVERWRITE if root is in data.agent_instances
} else {
    state.subAgents[name] = sa;  // ← If name == rootName, this overwrites everything above!
}
```

**Problem:** The root agent's messages are first modified via truncation + push (lines 1006-1021), but then if `rootName` appears in `data.agent_instances` with non-partial data (`is_partial: false`), line 1076 OVERWRITES the entire root agent object — discarding the truncation+push work.

This creates a **silent state loss**: messages that were carefully merged via the stream path are replaced wholesale by the agent_instances merge. If the DOM was already updated based on the stream-merged messages, and then the overwrite introduces different messages, the next render cycle may:
1. Take the append path (thinking `currentCount > lastCount`)
2. Add "new" messages that were actually already rendered via the full re-render path
3. **Result: duplicate DOM elements**

### 5. Content Key Check Doesn't Guard Against DOM Staleness (Line 2447)

```javascript
if (panel.dataset.contentKey === contentKey && state.editingIndex === null 
    && parseInt(panel.dataset.lastRenderedCount || '0') === displayMsgs.length) {
    return; // Nothing changed — skip
}
```

**Problem:** The check compares `contentKey` and message count, but doesn't verify the actual DOM content. If the container was cleared externally (e.g., by tab switch), the function would skip re-rendering because the key matches, leaving an empty container.

---

## Triggers That Expose the Bug

1. **User sends a message** → Server responds with `state/done` + `stream_update`
2. **Tab switch during generation** → `switchMainTab()` invalidates cache but DOM may have pending operations
3. **Rapid interaction** → Multiple WebSocket messages arrive before renders complete
4. **Retry/Reset** → Clears DOM and cache, then immediately re-renders

---

## Comparison: Main Branch (No Bug) vs Unified Branch (Buggy)

| Aspect | Main Branch | Unified Branch |
|--------|-------------|----------------|
| Root msg storage | `state.messages` | `state.subAgents[rootName].messages` |
| Render function | Separate: `renderMessages()` + `renderSubAgentPanel()` | Unified: `renderSubAgentPanel()` for all |
| Cache mechanism | Global `lastRenderedCount = Infinity` on state update | Per-panel `dataset.lastRenderedCount = '0'` |
| Append safety | Full re-render always fires after state update (Infinity > any count) | Append can fire if DOM out of sync with cache |
| Sub-agent cache invalidation | Global variable, always reset | Only root panel reset in stream_update |

---

## Files & Line Numbers

| File | Lines | Description |
|------|-------|-------------|
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 2373–2509 | `renderSubAgentPanel()` — contains the buggy incremental rendering logic |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 848–991 | `handleServerMessage()` → `state/done` handler — sets messages and invalidates cache |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 993–1213 | `stream_update` handler — partial merge and conditional render |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 1006–1021 | Root agent message truncation + push in stream_update |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 1075–1076 | Agent instances overwrite — can discard root's stream-merged messages |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 1115–1123 | Cache invalidation — only targets root panel, misses sub-agents |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 863–877 | Root agent message assignment — potential race with agent_instances merge |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 2513–2542 | `switchMainTab()` — calls `renderSubAgents()`, adds another render trigger |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 2458–2463 | Full re-render path — clears DOM, rebuilds all messages |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 2467–2478 | Append path — appends new messages without deduplication check |

---

## Recommended Fix Direction (NOT Implemented)

1. **Change cache reset from `'0'` to a sentinel value** that forces full re-render on next cycle
2. **Add DOM element count verification** before taking the append path
3. **Invalidate ALL panel caches** in `stream_update`, not just root
4. **De-duplicate message objects** in state assignment (lines 863–877)
5. **Serialize render calls** to prevent concurrent DOM operations

---

*Investigation completed. No code changes made.*