# Frontend Index Preservation Pattern

## Problem
When wiring `fullRender()` to use the unified `renderAgentConversation()` function, filtering messages (e.g., hiding system messages) before rendering changes the array indices. Since `data-index` attributes on DOM elements are used by edit/delete handlers to look up messages in `state.messages`, shifted indices cause operations to target the wrong messages.

## Concrete Failure Scenario
```
state.messages = [user(0), assistant(1), system(2), user(3), assistant(4)]

Simple filter → visibleMsgs = [user, assistant, user, assistant]
renderAgentConversation assigns data-index: [0, 1, 2, 3]

User clicks "edit" on message at data-index=2 (originally user@3):
  startEdit(2) → msgs[2] = system ← WRONG!
```

## Solution: Index Map Pattern
Build both a filtered array AND an index map in the same pass, then pass the index map to `renderAgentConversation`:

```javascript
const visibleMsgs = [];
const indexMap = []; // maps filtered-index → original-index
for (let i = 0; i < msgs.length; i++) {
  if (msgs[i].role !== 'system') {
    visibleMsgs.push(msgs[i]);
    indexMap.push(i);
  }
}
renderAgentConversation('root', visibleMsgs, 0, indexMap);
```

In `renderAgentConversation`, use `origIndex = indexMap ? indexMap[i] : i` to get the correct original index for `data-index`.

## Key Takeaway
When filtering a message array before rendering, always preserve the original indices so that DOM-level interactions (edit/delete) can correctly look up messages in the source array. The optional `indexMap` parameter on `renderAgentConversation` makes this pattern reusable without breaking callers that don't need filtering.

---

## Step K: renderMessages() — Why It Was Skipped

The incremental append loop in `renderMessages()` (lines 1183-1210) could NOT be replaced with `renderAgentConversation` because of lazy rendering logic:
- Tool calls / function results always render immediately
- Assistant/user messages only render if their PREVIOUS message is done streaming (has content), OR they're the last message

`renderAgentConversation` renders ALL messages unconditionally — no skip/lazy logic exists. Adding that would require a significant redesign beyond Step K's scope.

**Resolution:** Added a TODO comment at line 1212-1218 with two future paths: (1) add 'lazy' mode to renderAgentConversation, or (2) pre-filter into ready/pending sets before calling it. The full-re-render path via `fullRender()` is already unified (Step J), only the incremental append path remains on the old direct-`createMessageEl` path.

---

## Step L: renderSubAgentPanel() — Wiring + Bug Fix

### Changes Made
1. **Fixed `createMessageEl` to pass `config.instanceName`** to `startEdit()` and `deleteMessage()` (3 call sites). Previously these calls omitted the instance name, which means sub-agent edit/delete would look at `state.messages` instead of `state.subAgents[name].messages`. Root rendering unaffected because `instName = null` for root config.

2. **Replaced both rendering loops** in `renderSubAgentPanel`: full re-render uses `renderAgentConversation(name, msgs, 1)`; incremental append builds a subset with indexMap and calls `renderAgentConversation(name, newMsgs, 1, newIndexMap)`.

3. **Deprecated `createSubMsgEl`** — no longer called after wiring. Added deprecation comment; removal deferred to future cleanup.

### Known Issues (documented in TODOs)
- **Double-render on streaming first tick:** `renderAgentConversation` renders the new message, then `updateSubBubbleContent` renders it again because `prevSubContent` is undefined. Functionally correct but wasteful.
- **isGenerating detection is root-only:** `createMessageEl` checks `state.generating && index === state.messages.length - 1` which only works for root chat. Sub-agents need `agentData.active` check instead. Fix requires passing active state through config.