# Message Duplication Bug — Investigation Report v2

## Summary

Messages duplicate because the **append path in `renderSubAgentPanel`** (line 2467) adds new DOM elements without checking if they already exist, and this can fire when the DOM state is out of sync with the cached `lastRenderedCount`. The core problem: after a cache invalidation resets `lastRenderedCount` to `'0'`, the full re-render path (line 2458) clears the DOM and renders everything. But if a subsequent render fires before `lastRenderedCount` is fully synced — or if multiple WebSocket messages arrive in quick succession — the append path can add elements that were already rendered by the previous cycle.

---

## The Critical Code Paths

### 1. `state/done` Handler (lines 853–991)

```
lines 864-869: state.subAgents[rootName] = { ..., messages: data.messages }
lines 880-884: for each name in data.agent_instances → state.subAgents[name] = sa
lines 954-957: Invalidate ALL panel caches (contentKey='', lastRenderedCount='0')
line   960:   renderSubAgents()
```

### 2. `stream_update` Handler (lines 993–1214)

```
lines 1006-1021: Root agent messages = truncate to historyCount + push responseMsgs
lines 1031-1077: For each name in data.agent_instances → merge/overwrite state.subAgents[name]
lines 1115-1123: Invalidate ROOT panel cache ONLY (when !generating)
line   1183:    renderSubAgents() — conditional on throttle
```

### 3. `renderSubAgentPanel` (lines 2373–2509)

```
line 2458: if (currentCount < lastCount || lastCount === 0) → FULL RE-RENDER (clears DOM, rebuilds all)
line 2467: else → APPEND PATH (appends only new messages from lastCount to currentCount)
line 2497: panel.dataset.lastRenderedCount = currentCount
```

---

## Root Cause: Three Interacting Bugs

### Bug A: Append Path Has No DOM Deduplication (lines 2467–2478)

The append path does:
```javascript
for (let i = lastCount; i < currentCount; i++) {
    newMsgs.push(displayMsgs[i]);
}
scrollContainer.appendChild(renderAgentConversation(name, newMsgs, ...));
```

It **never checks** whether `scrollContainer` already contains DOM elements at indices `lastCount` through `currentCount-1`. If the container was cleared by another code path (tab switch, retry button, etc.) but `lastRenderedCount` wasn't reset in time, the append path adds duplicates.

### Bug B: Root Agent Double-Processing — Messages Set Twice Before Render

In `state/done` (lines 864–884):
1. Line 865 sets `state.subAgents[rootName].messages = data.messages`
2. Lines 880–884 iterate over ALL entries in `data.agent_instances`, including the root agent if it's present there

The comment on line 879 says "Merge agent_instances (sub-agents) — don't overwrite root data" but **the code DOES overwrite root data** if `data.agent_instances` includes a key matching `rootName`. This means:
- `data.messages` is set first → messages array = `[msg1, msg2, ...]`
- Then `data.agent_instances[rootName]` overwrites it → messages array = `[possibly_different_msgs]`

If both contain the same messages (which they typically do), this isn't harmful. But if they differ even slightly in length or content, the next render cycle sees a different message count than expected, causing the append path to fire on stale DOM state.

The **same double-processing happens in `stream_update`** at lines 1006–1076:
1. Lines 1006-1021: truncate root messages + push responseMsgs
2. Lines 1031-1077: for each name in agent_instances, if `is_partial: false` → overwrite entirely

If root agent is in `data.agent_instances`, line 1076 does `state.subAgents[name] = sa` which overwrites the truncation+push work from step 1.

### Bug C: Cache Invalidation Only Targets Root Panel (lines 1115–1123)

```javascript
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

This only invalidates the **root panel**. Sub-agent panels keep their stale `lastRenderedCount` values. If a sub-agent tab was visible during a state change, its DOM could be out of sync with its cached count.

---

## The Duplication Scenario (Step by Step)

1. **User sends message** → server responds with `state/done`
2. **`state/done` handler** (line 954-960): invalidates all caches, calls `renderSubAgents()`
3. **`renderSubAgentPanel`** for root: `lastCount = 0` → full re-render path (line 2458), DOM cleared, all messages rendered, `lastRenderedCount = N`
4. **Server sends `stream_update`** with new messages while generating
5. **`stream_update` handler**: root messages truncated + new ones pushed (lines 1006-1021)
6. **`renderSubAgentPanel`** again: `currentCount = N+1`, `lastCount = N` → append path fires (line 2467)
7. **But**: if between steps 3 and 6, a tab switch or other render cleared the DOM without resetting `lastRenderedCount`, then step 6 appends to a container that already has messages from step 3's full re-render → **DUPLICATE**

---

## Why Main Branch Doesn't Have This Bug

In main branch:
- Root agent uses separate `state.messages` (not in `subAgents`)
- Separate render function: `renderMessages()` for root, `renderSubAgentPanel()` for sub-agents
- Global `lastRenderedCount = Infinity` on state/done handler (line 811) — since `currentCount < Infinity` is always true, the next render **always** takes the full re-render path

In unified branch:
- Root agent stored in `subAgents[rootName]` — same structure as sub-agents
- Single render function: `renderSubAgentPanel()` for ALL agents
- Per-panel `lastRenderedCount = '0'` on state/done (line 956) — after full re-render sets it to `N`, a subsequent render with `currentCount > N` takes the append path, which has no deduplication

---

## Proposed Fixes

### Fix 1: Add DOM Element Count Verification Before Append Path (CRITICAL — lines 2467–2478)

**Before appending new messages, verify the container actually contains `lastCount` child elements.** If not, take the full re-render path instead.

```javascript
// At line ~2467, change from:
} else {
    // Append new messages...
    
// To:
} else {
    // Verify DOM matches expected state before appending
    const actualChildCount = scrollContainer.children.length;
    if (actualChildCount !== lastCount) {
        // DOM is out of sync — do a full re-render instead of append
        scrollContainer.innerHTML = '';
        const subConfig = getAgentConfig(name);
        subConfig.isGenerating = isActive;
        scrollContainer.appendChild(renderAgentConversation(name, displayMsgs, 1, null, subConfig));
    } else {
        // Safe to append — DOM matches expected state
        const newMsgs = [];
        const newIndexMap = [];
        for (let i = lastCount; i < currentCount; i++) {
            newMsgs.push(displayMsgs[i]);
            newIndexMap.push(i);
        }
        const subConfig = getAgentConfig(name);
        subConfig.isGenerating = isActive;
        scrollContainer.appendChild(renderAgentConversation(name, newMsgs, 1, newIndexMap, subConfig));
    }
```

### Fix 2: Skip Root Agent in agent_instances Loop (lines 880–884 and 1031–1077)

In `state/done` handler, add a guard to prevent overwriting root data from `data.messages`:

```javascript
// At lines 880-884, change from:
if (data.agent_instances) {
    for (const [name, sa] of Object.entries(data.agent_instances)) {
        state.subAgents[name] = sa;
    }
}

// To:
if (data.agent_instances) {
    for (const [name, sa] of Object.entries(data.agent_instances)) {
        if (!isRootAgentName(name)) {  // ← Skip root agent
            state.subAgents[name] = sa;
        }
    }
}
```

Same change needed in `stream_update` handler at lines 1031–1077.

### Fix 3: Invalidate ALL Panel Caches in stream_update (lines 1115–1123)

Change from invalidating only the root panel to invalidating all panels when generation starts:

```javascript
// At lines 1115-1123, change from:
if (!state.generating) {
    const rootPanel = document.getElementById(getRootPanelId());
    if (rootPanel) {
        const msgsEl = rootPanel?.querySelector('.messages');
        if (msgsEl) {
            msgsEl.dataset.contentKey = '';
            msgsEl.dataset.lastRenderedCount = '0';
        }
    }
}

// To:
if (!state.generating) {
    mainTabPanels.querySelectorAll('.messages').forEach(p => {
        p.dataset.contentKey = '';
        p.dataset.lastRenderedCount = '0';
    });
}
```

### Fix 4 (Optional but Recommended): Use Infinity Sentinel Like Main Branch (lines 956 and 1121)

Change the cache reset value from `'0'` to `'Infinity'` to guarantee full re-render after any state change:

```javascript
// At line 956, change from:
p.dataset.lastRenderedCount = '0';

// To:
p.dataset.lastRenderedCount = 'Infinity';
```

This ensures `currentCount < lastCount` is always true on the first render after a cache clear, forcing a full re-render. The main branch uses this pattern (line 811).

---

## File References

| File | Lines | Description |
|------|-------|-------------|
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 2467–2478 | **Fix 1**: Append path — add DOM verification |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 880–884 | **Fix 2a**: Skip root in agent_instances loop (state/done) |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 1031–1077 | **Fix 2b**: Skip root in agent_instances loop (stream_update) |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 1115–1123 | **Fix 3**: Invalidate ALL panel caches, not just root |
| `N:\work\WD\AgentCascade_unified\web_ui\app.js` | 956, 1121 | **Fix 4**: Use Infinity sentinel instead of '0' |

---

*Investigation completed. No code changes made.*