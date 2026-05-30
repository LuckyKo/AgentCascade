# Web UI Frontend — Architecture Redesign Plan

> **Goal**: Replace the spaghetti unified rendering with a clean, simple architecture where chat = perfect mirror of agent working pool, streaming only touches the last bubble, and there are no unnecessary re-renders.
> 
> **Core Rule**: Root agent and sub-agents share EXACTLY the same rendering code. No isRoot checks in rendering. No separate paths. The only difference is the tab title.

---

## Cleanup Sprint Summary (Phase A–C, May 2026)

This document tracked the original refactoring plan. Below is a status summary of what was cleaned up during the Phase A–C cleanup sprint vs. what remains deferred or intentional.

### Implemented ✅ (7 commits, May 2026)

| Cleanup Item | Git Hash |
|---|---|
| Qwen fallback removal (`qwen-session-name`) | `93d8901` |
| Legacy migration script removal (`work-access-folders`) | `93d8901` |
| Chat sentinel (`'chat'`) removal | `93d8901` |
| `lastChatRender` / `lastChatContentKey` genStats fields removed | `93d8901` |
| Dead code comments removed | `93d8901` |
| Dead CSS selector (`_root`) removal | `93d8901` |
| Event listener leak fix (event delegation) | `51de3e9` |
| Parameterize `DEFAULT_SESSION_NAME` | `24c68c5` |
| Inline `setInnerHtmlWithState` into `updateBubbleContent` | `24c68c5` |
| Fix sub-agent tab pulsing (removed global `state.generating` fallback) | `7e47c93` |
| Rename `configOverride` → `renderOpts` | `7e47c93` |

### Deferred ⏸ (Future Refactor)

| Item | Reason |
|---|---|
| Full innerHTML audit across the codebase | Low risk, high effort — not urgent |
| Browser dialog replacement (`prompt`/`confirm`) | UI polish, no functional impact |
| Inline style extraction to CSS | Cosmetic cleanup |
| `$()` helper rename (jQuery conflict) | No actual jQuery usage found; safe to leave |

### Not Legacy — Intentional Design ❌

| Item | Reason |
|---|---|
| `isRoot` checks in tab rendering | Used only for tab title icon differentiation (💬 vs 🤖) and message routing — intentional design, not legacy baggage |

---

## Table of Contents

1. [Current Problems](#current-problems)
2. [Design Principles](#design-principles)
3. [What to DELETE](#what-to-delete)
4. [What to KEEP](#what-to-keep)
5. [What to WRITE (New)](#what-to-write-new)
6. [Data Flow Architecture](#data-flow-architecture)
7. [Rendering Pipeline](#rendering-pipeline)
8. [Tab Structure](#tab-structure)
9. [Activity Bar — Decoupled Status Tab](#activity-bar--decoupled-status-tab)
10. [System Message Handling](#system-message-handling)
11. [Implementation Order](#implementation-order)

---

## Current Problems

| Problem | Root Cause |
|---------|-----------|
| Spaghetti code from unification attempts | Tried to merge root agent and sub-agent rendering into one path, but kept divergent logic (system message filtering for root, active state derivation differs) making it fragile |
| Message duplication during streaming | `stream_update` handler merges `response_messages` using `historyCount`, but `state`/`done` events also push messages — overlap causes duplicates |
| Overly complex incremental rendering | `contentKey`, `lastRenderedCount`, `prevContent`, `prevReasoning` dataset tracking, `appendStreamingDelta` — all this complexity to avoid re-rendering the last bubble. But it breaks when reasoning changes or content doesn't start with prevContent |
| System messages filtered inconsistently | Root agent filters system messages (`displayMsgs = isRoot ? msgs.filter(m => m.role !== 'system') : msgs`) but sub-agents don't — inconsistent UX |
| Activity bar coupled to each agent tab | Each panel has its own activity bar with pause/terminate buttons — UI clutter and duplicated controls |
| Frequent unnecessary re-renders | `renderSubAgents()` called on every `stream_update` tick (throttled but still), even when nothing meaningful changed. Throttle tiers (150ms/300ms/750ms) add complexity |

---

## Design Principles

### 1. Single Source of Truth
- `state.subAgents[instanceName].messages` is the ONLY source of truth for what to render
- The UI always mirrors this exactly — no filtering, no hiding (except during streaming where we only update the last bubble)

### 2. Full Re-render on State Change, Incremental Only During Streaming
- **State events** (`state`, `done`): clear + re-render ALL messages in the panel. These are rare and represent actual state changes (compression, edit, delete, rollback).
- **Streaming** (`stream_update`): only update the LAST message bubble's content. No new bubble creation, no diffing — just `updateBubbleContent(lastBubble)`.

### 3. No Polling, No Periodic Refreshes
- UI updates ONLY happen when WebSocket messages arrive. No intervals, no timeouts for rendering.

### 4. Simple > Clever
- No content keys, no lastRenderedCount tracking, no incremental delta appending with `appendStreamingDelta`. Just: clear & re-render on state change, update last bubble during streaming.

### 5. Decoupled Activity Monitor
- Activity bar moves to its own tab (like approvals). Shows a FIFO token queue for the currently visible agent tab only. Hooks directly into `stream_update` — closest to where tokens arrive.

### 6. ZERO Root/Sub-agent Distinction in Rendering
- **This is the most important principle.** The root agent is just another entry in `state.subAgents`. It gets the same tab, the same panel, the same rendering code. No `isRoot` checks anywhere in rendering. No separate functions. No special cases.
- The ONLY place `getRootAgentName()` / `isRootAgentName()` is used: determining the tab title icon (💬 for root, 🤖 for sub-agents) and deciding which agent to send messages to.

---

## What to DELETE

### Functions / Sections to Remove Entirely

| Function/Section | Lines (approx) | Why | Status |
|-----------------|----------------|-----|--------|
| `renderMessages()` | ~100 lines | Dead code in unified branch — root rendered via renderSubAgents now | ✅ Removed (dead code comments cleaned in `93d8901`) |
| `fullRender()` | ~5 lines | Replaced by simpler clear + re-render pattern | ✅ Removed (dead code comments cleaned in `93d8901`) |
| `appendStreamingDelta()` | ~30 lines | Overly complex incremental delta — not needed with new approach | ✅ Already absent (never existed in unified branch) |
| `setInnerHtmlWithState()` | ~20 lines | Inlined into updateBubbleContent | ✅ Implemented (`24c68c5`) |
| `togglePulseElements()` + `_lastPulseToggle` / `PULSE_THROTTLE_MS` | ~15 lines | Pulse animation handled by CSS, no JS toggle needed | ⏸ Deferred |
| `UIState` object | ~15 lines | Unused — `state` object is sufficient | ⏸ Deferred |
| All `contentKey` logic | scattered in renderSubAgentPanel | No longer needed | ⏸ Deferred |
| All `lastRenderedCount` dataset tracking | scattered in renderSubAgentPanel | No longer needed | ⏸ Deferred |
| All `prevContent` / `prevReasoning` bubble dataset tracking | in updateBubbleContent | No longer needed | ⏸ Deferred |
| Throttle timestamp fields in genStats: `lastChatRender`, `lastChatContentKey` | state definition | Unused after unification | ✅ Removed (`93d8901`) |
| Tiered throttle logic in stream_update handler (subThrottleContent with 150/300/750ms) | ~20 lines | Replaced by simpler streaming-only update | ⏸ Deferred |
| Incremental append path in renderSubAgentPanel (the `else` branch after `currentCount < lastCount`) | ~40 lines | Replaced by full re-render on state events + incremental only during streaming | ⏸ Deferred |

### ALL isRoot-specific Logic to Remove

| Location | What to Remove | Why | Status |
|----------|---------------|-----|--------|
| `renderSubAgentPanel` line ~2337 | `displayMsgs = isRoot ? msgs.filter(m => m.role !== 'system') : msgs` | System messages shown for ALL agents equally | ⏸ Deferred |
| `renderSubAgentPanel` line ~2334 | `const isActive = isRoot ? (state.generating && !agentData?.is_halted) : (agentData?.active ?? false)` | Use `agentData.active` for all. Root agent gets `active` flag from server like everyone else | ⏸ Deferred |
| `renderSubAgentPanel` lines ~2448-2457 | Special token/word count handling for root | Use `agentData.total_tokens` / `state.totalTokens` uniformly — pass via a simple helper | ⏸ Deferred |
| `createMessageEl` line ~1430-1432 | `isGenerating` check using config or subAgents with special root handling | Simplified: just check if this is the last message and the agent is active | ⏸ Deferred |
| All CSS class helpers (`msgClass`, `headerClass`, `contentClass`, `nameLabelClass`) | They take `isRoot` param but return the same thing anyway | Replace with simple constants or inline strings | ⏸ Deferred |
| `getRootAgentConfig()` / `getSubAgentConfig()` | Config factory pattern that adds abstraction without value | Inline: just pass `instanceName` directly | ⏸ Deferred |
| `configOverride` parameter in `renderAgentConversation` | Adds a layer of indirection for no reason | Renamed to `renderOpts` — kept the parameter but simplified fallback logic | ✅ Implemented (`7e47c93`) |

### DOM Elements to Remove

| Element | Why |
|---------|-----|
| Per-panel activity bars (`main-activity-bar` inside each agent panel) | Activity moved to its own tab |
| Per-panel pause/terminate buttons (inside activity bar) | Moved to global controls area |

---

## What to KEEP

### Core Infrastructure (No Changes)

| Component | Reason |
|-----------|--------|
| `state` object (with cleanup — remove unused genStats fields) | Single source of truth |
| WebSocket connect/disconnect/reconnect logic (`connect()`, `scheduleReconnect()`) | Solid, no issues |
| `send()` function | Clean and correct |
| `handleServerMessage()` dispatch structure | Good switch/case pattern |
| Markdown setup (marked, hljs) | No issues |
| Settings persistence (`saveSettings()`, `loadSettings()`, `getGenerateCfg()`) | Works well |
| `escapeHtml()`, `formatMultimodalContent()`, `autoResize()` | Utilities — no changes |
| Image/document handling | No issues |
| Session manager (fetchSessions, renderSessions) | No issues |
| Audio context / playSound | No issues |
| AFK logic | No issues |
| Telemetry panel | No issues |
| API router management | No issues |

### Rendering (Modified, Not Deleted)

| Component | Changes |
|-----------|---------|
| `createMessageEl()` | **Major simplification** — remove config param entirely. Just `(msg, index, instanceName)`. Same code path for root and sub-agents. |
| `renderMarkdown()` | Keep as-is. It's clean. |
| `renderThinkingBlock()`, `renderToolCall()`, `renderToolResult()` | Keep as-is |
| `isToolFailure()` | Keep as-is |
| `updateBubbleContent()` | **Massively simplified** — remove all incremental delta logic, prevContent tracking. Just re-render the bubble content. No config param — just `(bubble, msg)`. |
| `renderAgentConversation()` | Simplified — just iterate messages and create elements. No configOverride. |

### Message CRUD (Modified)

| Component | Changes |
|-----------|---------|
| `startEdit()`, `finishEdit()`, `cancelEdit()` | Simplify — remove config abstraction, just use instanceName directly |
| `deleteMessage()` | Keep as-is |

### Agent Management (Modified)

| Component | Changes |
|-----------|---------|
| `renderSubAgents()` → renamed to `renderAgentTabs()` | **Major simplification** — iterate agents, create/update tabs and panels. Same logic for root and all sub-agents. No isRoot branching. |
| `switchMainTab()` | Keep but simplify — no need to call renderSubAgents on tab switch (lazy rendering via visibility check) |
| `updateControls()` | Simplified — remove pulse/inner HTML manipulation of root tab |

---

## What to WRITE (New)

### New Functions

#### 1. `clearPanel(instanceName)`
**Purpose**: Clear a panel's message container for full re-render.

```javascript
function clearPanel(instanceName) {
    const panel = document.getElementById('panel-' + instanceName);
    if (!panel) return;
    const msgsEl = panel.querySelector('.messages');
    if (msgsEl) msgsEl.innerHTML = '';
}
```

#### 2. `renderPanelMessages(instanceName)`
**Purpose**: Full re-render of all messages in a panel. Called on state/done events. Same code for root and sub-agents.

```javascript
function renderPanelMessages(instanceName) {
    const agentData = state.subAgents[instanceName];
    if (!agentData || !agentData.messages) return;
    
    const panel = document.getElementById('panel-' + instanceName);
    if (!panel) return;
    
    const msgsEl = panel.querySelector('.messages');
    if (!msgsEl) return;
    
    // Clear and re-render all messages — system messages included, no filtering
    msgsEl.innerHTML = '';
    
    for (let i = 0; i < agentData.messages.length; i++) {
        const msg = agentData.messages[i];
        const el = createMessageEl(msg, i, instanceName);
        msgsEl.appendChild(el);
    }
    
    // Update context bar — same for all agents
    updateContextBarForPanel(instanceName, agentData);
}
```

#### 3. `updateLastBubble(instanceName)`
**Purpose**: During streaming, only update the last message bubble. No new bubbles, no diffing. Same code for root and sub-agents.

```javascript
function updateLastBubble(instanceName) {
    const agentData = state.subAgents[instanceName];
    if (!agentData || !agentData.messages?.length) return;
    
    const panel = document.getElementById('panel-' + instanceName);
    if (!panel) return;
    
    const msgsEl = panel.querySelector('.messages');
    if (!msgsEl || !msgsEl.lastElementChild) return;
    
    const lastMsg = agentData.messages[agentData.messages.length - 1];
    updateBubbleContent(msgsEl.lastElementChild, lastMsg);
}
```

#### 4. `appendNewBubble(instanceName)`
**Purpose**: When a new message arrives during streaming (tool call, tool result, new turn), append exactly one bubble. Same code for root and sub-agents.

```javascript
function appendNewBubble(instanceName) {
    const agentData = state.subAgents[instanceName];
    if (!agentData || !agentData.messages?.length) return;
    
    const panel = document.getElementById('panel-' + instanceName);
    if (!panel) return;
    
    const msgsEl = panel.querySelector('.messages');
    if (!msgsEl) return;
    
    const newMsgIndex = agentData.messages.length - 1;
    const lastMsg = agentData.messages[newMsgIndex];
    const el = createMessageEl(lastMsg, newMsgIndex, instanceName);
    msgsEl.appendChild(el);
}
```

#### 5. `streamActivityUpdate(delta)`
**Purpose**: Push incoming token text to the activity tab's FIFO queue. Called from stream_update handler before any rendering decision. Only for the currently visible agent tab.

```javascript
const ACTIVITY_QUEUE_MAX = 120; // Max chars in activity display
let activityQueue = '';

function streamActivityUpdate(delta) {
    activityQueue += delta;
    if (activityQueue.length > ACTIVITY_QUEUE_MAX) {
        activityQueue = activityQueue.slice(-ACTIVITY_QUEUE_MAX);
    }
    
    const feedEl = document.getElementById('activityFeed');
    if (feedEl) {
        feedEl.textContent = activityQueue;
    }
}
```

#### 6. `renderActivityTab()`
**Purpose**: Render the decoupled activity/status tab. Shows FIFO token queue, generation stats.

```javascript
function renderActivityTab() {
    const statusEl = document.getElementById('activityStatusText');
    if (statusEl) {
        const tps = state.genStats.tokenCount && state.genStats.active 
            ? `${state.genStats.tokenCount} tokens (${formatTokensPerSec()})` 
            : '';
        statusEl.textContent = state.generating ? `Generating... ${tps}` : 'Idle';
    }
    
    // Update active agent name in activity tab
    const activeAgentEl = document.getElementById('activityActiveAgent');
    if (activeAgentEl) {
        const topAgent = state.activeStack.length > 0 
            ? state.activeStack[state.activeStack.length - 1] 
            : getRootAgentName();
        activeAgentEl.textContent = topAgent;
    }
}
```

#### 7. `showSystemMessageBanner(text)`
**Purpose**: Show a temporary banner when a system message arrives during an active session (like the approval bar).

```javascript
function showSystemMessageBanner(text) {
    const bar = document.getElementById('systemMessageBar');
    if (!bar) return;
    
    bar.innerHTML = `<div class="system-message-banner">${renderMarkdown(text)}</div>`;
    bar.style.display = 'block';
    
    // Auto-hide after 5 seconds
    clearTimeout(bar._hideTimer);
    bar._hideTimer = setTimeout(() => { bar.style.display = 'none'; }, 5000);
}
```

### New HTML Elements (to be added in index.html)

#### Activity Tab + Panel

```html
<!-- In main-tab-bar -->
<button class="main-tab" data-tab="activity" id="tabActivity">
    <span class="main-tab-icon">⚡</span> Activity
</button>

<!-- In main-tab-panels -->
<div class="main-tab-panel" id="panel-activity">
    <div class="activity-feed-container">
        <div class="activity-feed-label">Streaming Feed — Agent: <span id="activityActiveAgent">—</span></div>
        <pre class="activity-feed" id="activityFeed"></pre>
    </div>
    <div class="activity-status-text" id="activityStatusText">Idle</div>
</div>
```

#### System Message Banner (above input area, like approval bar)

```html
<div class="system-message-bar" id="systemMessageBar" style="display:none;"></div>
```

---

## Data Flow Architecture

### WebSocket → State → Rendering Pipeline

```
WebSocket message arrives
        │
        ▼
  handleServerMessage(data)
        │
        ├── type: 'state' or 'done'
        │     │
        │     ├── Update state.subAgents from data (same for root and all agents)
        │     ├── Mark ALL panels dirty
        │     ├── scheduleFullRerender()  ← runs once, renders all dirty panels
        │     └── updateControls()
        │
        ├── type: 'stream_update'
        │     │
        │     ├── streamActivityUpdate(delta)  ← feeds activity tab FIRST (visible agent only)
        │     ├── Update state.subAgents (merge root + sub-agent messages, same code)
        │     ├── For each agent with changed messages:
        │     │     ├── If message count increased → appendNewBubble(instanceName)
        │     │     └── updateLastBubble(instanceName)  ← only last bubble
        │     ├── updateGenStats()         ← throttled ~2Hz
        │     └── updateControls()         ← throttled ~1Hz
        │
        └── type: 'approvals' / 'error' / etc.
              └── Handle normally (no rendering changes)
```

### What Triggers a Full Re-render vs Incremental Update?

| Trigger | Action | Why |
|---------|--------|-----|
| `state` event (initial load, session load) | Full re-render ALL panels | State is complete, render everything |
| `done` event (generation ended) | Full re-render visible panel | Messages may have been committed differently |
| Message edit via server response | Full re-render the affected panel | Content changed arbitrarily |
| Message delete via server response | Full re-render the affected panel | Message count changed |
| Compression/summary event | Full re-render ALL panels | Entire message array changed |
| Rollback event | Full re-render ALL panels | Messages went backwards |
| `stream_update` — content streaming (same message count) | Update last bubble ONLY | Only the tail is growing |
| `stream_update` — new message arrived | Append ONE new bubble + update last bubble | New turn started, but don't re-render old messages |

### Key Insight: The Two-Phase Streaming Model

**Phase 1 — Content Growing**: During streaming, the last message grows. We only call `updateBubbleContent(lastBubble)`. This is fast because it's one DOM element.

**Phase 2 — New Message Arrives**: When a new message appears (tool call, tool result, new user message), we create ONE new bubble and append it. Then continue Phase 1 with the new last bubble.

This means during streaming, we NEVER re-render old messages. Only:
1. Update the growing tail (one element)
2. Append new bubbles as they arrive (one element each)

---

## Rendering Pipeline

### Before (Broken — Too Complex)

```
renderSubAgents() [called every 150-750ms during streaming]
    ├── For each agent:
    │   └── renderSubAgentPanel(panel, agentData, name)
    │       ├── isRoot check → different active state derivation ← DIVERGENCE
    │       ├── isRoot check → filter system messages ← DIVERGENCE
    │       ├── Check contentKey (composite of count:length:reasoning:funcCall:activeFlag)
    │       ├── If key matches → early exit (but computing the key is expensive)
    │       ├── If count decreased → full clear + re-render
    │       └── If count same or increased:
    │           ├── Incremental append from lastCount to currentCount
    │           ├── updateBubbleContent(lastBubble) with incremental delta
    │           │   ├── Check prevContent/prevReasoning
    │           │   ├── Try appendStreamingDelta (raw text injection)
    │           │   └── Fall back to full re-render if delta fails
    │           └── Update context bar (throttled)
    └── togglePulseElements() [JS-driven animation]
```

**Problems**: contentKey computation is O(N), prevContent tracking is fragile, appendStreamingDelta breaks markdown formatting, the whole thing is 300+ lines of branching logic. And root/sub-agent divergence makes it impossible to reason about.

### After (Clean — Simple, No Root/Sub-agent Distinction)

```
On 'state' or 'done':
    └── For each dirty panel:
            ├── clearPanel(instanceName)                    // innerHTML = ''
            ├── renderPanelMessages(instanceName)           // full re-render, same code for all
            └── unmark dirty

On 'stream_update':
    ├── streamActivityUpdate(delta)                          // feed activity tab (visible agent only)
    ├── For each agent with state change:
    │   ├── If message count increased:
    │   │   └── appendNewBubble(instanceName)               // append one bubble, same code for all
    │   └── updateLastBubble(instanceName)                  // update tail, same code for all
    └── updateGenStats() [throttled ~2Hz]

renderPanelMessages(instanceName):
    ├── msgsEl.innerHTML = ''
    └── For each msg in agentData.messages:  ← NO filtering, NO isRoot check
            └── msgsEl.appendChild(createMessageEl(msg, i, instanceName))

updateLastBubble(instanceName):
    └── updateBubbleContent(msgsEl.lastElementChild, lastMsg)  ← same code for all

appendNewBubble(instanceName):
    └── msgsEl.appendChild(createMessageEl(lastMsg, newIdx, instanceName))  ← same code for all

updateBubbleContent(bubble, msg):
    ├── contentDiv = bubble.querySelector('.msg-content')
    └── contentDiv.innerHTML = renderMessageHtml(msg)       // full re-render of one element
```

**Why this is better**: 
- No contentKey computation — just clear + rebuild on state events (which are rare)
- During streaming, only ONE element is re-rendered per tick
- `updateBubbleContent` is trivial — no prevContent tracking, no delta logic
- The browser's innerHTML assignment on a single bubble is fast enough even for long messages
- **Zero divergence between root and sub-agent rendering** — same functions, same code paths

### Performance Analysis

**Full re-render cost**: For a panel with ~50 messages averaging 2KB each = 100KB of text. `marked.parse()` runs in ~10-50ms total. This happens only on state/done events (maybe 3-5 times per generation session).

**Streaming update cost**: One bubble, maybe 5KB of text. `marked.parse()` runs in ~2-5ms. This happens every 100-200ms during streaming. Total overhead: negligible.

**No re-render on old messages during streaming**: This is the key optimization. Once a message is done streaming, it's never touched again until a state/done event.

---

## Tab Structure

### Tabs (in order)

```
┌─────────────────────────────────────────────────────┐
│  [💬 Maine_root]×  [🤖 coder]×  [⚡ Activity]              │
└─────────────────────────────────────────────────────┘
```

| Tab | ID | Description | Closable |
|-----|----|-------------|----------|
| Root agent | `sub-{sessionName}_root` | Main conversation including system messages | Yes (terminate session) |
| Sub-agents | `sub-{name}` | Each active sub-agent's conversation | Yes (terminate agent) |
| Activity | `activity` | Streaming token feed for visible tab | No (static) |

### Panel Structure (per agent — SAME for root and sub-agents)

```
┌─────────────────────────────────┐
│  Context Bar                    │  ← context usage indicator
├─────────────────────────────────┤
│                                 │
│  .messages                      │  ← message bubbles
│  ┌───────────────────────────┐  │
│  │ ⚙️ System: You are a...   │  │  ← system messages shown!
│  ├───────────────────────────┤  │
│  │ You: Hello                │  │
│  ├───────────────────────────┤  │
│  │ Assistant: Hi there       │  │
│  └───────────────────────────┘  │
│                                 │
└─────────────────────────────────┘
```

**No per-panel activity bar!** The activity bar is its own tab. Agent-specific controls (pause, terminate) are on the tab itself or in the global controls area.

### Global Controls (next to Stop button)

```html
<div class="input-controls">
    <button id="continueBtn">⏩ Continue</button>
    <button id="mainRetryBtn">🔄 Retry</button>
    <button id="stopBtn">⏹ Stop</button>
    <button id="pauseBtn">⏸ Pause</button>
    <button id="terminateBtn" title="Terminate active agent">💀 Terminate</button>
    <span class="status-text" id="statusText"></span>
</div>
```

The **Terminate** button terminates whatever agent is active (root if no sub-agent, or the top of the active stack). It replaces the per-panel terminate buttons.

---

## Activity Bar — Decoupled Status Tab

### Design

The Activity tab shows a real-time streaming token feed for whichever agent tab is currently visible. This is the closest UI element to where tokens actually arrive — it hooks directly into `stream_update`.

```
┌─────────────────────────────────┐
│  ⚡ Activity                    │
├─────────────────────────────────┤
│  Status: Generating... 1234 t/s │
├─────────────────────────────────┤
│  ┌───────────────────────────┐  │
│  │ Streaming Feed            │  │
│  │                           │  │
│  │ ...the quick brown fox    │  │  ← FIFO queue, max 120 chars
│  │ jumps over the lazy dog   │  │
│  └───────────────────────────┘  │
├─────────────────────────────────┤
│  Active Agent: coder            │
├─────────────────────────────────┤
│  Tokens this turn: 2456        │
└─────────────────────────────────┘
```

### Data Flow

```
stream_update arrives
    │
    ├── streamActivityUpdate(delta)   ← FIRST thing that happens (visible agent only)
    │   └── Append delta to activityQueue (FIFO, max 120 chars)
    │   └── Update activityFeed.textContent
    │
    ├── Update state.subAgents...
    └── renderAgentTabs() [throttled]
```

### Why This Works

- **Closest to the source**: Activity tab updates happen BEFORE any rendering logic, so it's always fresh
- **FIFO queue**: Shows the most recent tokens, giving a sense of "is it still streaming?" without showing the full message (that's in the agent tab)
- **Visible-tab only**: Only shows activity for the agent you're looking at. Switch tabs → different feed
- **No coupling**: Activity tab doesn't depend on any panel rendering state

---

## System Message Handling

### Current Behavior (Broken)

Root agent filters system messages: `displayMsgs = isRoot ? msgs.filter(m => m.role !== 'system') : msgs`
This means system prompts are invisible in the chat, which is confusing — users can't see what the agent is told to do.

### New Behavior

**System messages are rendered normally in ALL tabs**, just like any other message. They have a distinct visual style (different color, icon) but they're part of the conversation. No filtering at all.

**Additionally**, when a system message arrives during an active session (not initial load), a temporary banner appears above the input area — similar to how the approval bar works:

// In handleServerMessage, stream_update handler — for each new message:
if (msg.role === 'system') {
    showSystemMessageBanner(msg.content);  // Banner auto-hides to avoid cluttering chat
}


### Visual Distinction for System Messages

In CSS:
```css
.msg-system {
    border-left: 3px solid var(--system-accent, #6c757d);
    background: var(--system-bg, rgba(108, 117, 125, 0.1));
}

.msg-system .msg-name::before {
    content: '⚙️ ';
}
```

---

## Implementation Order

### Phase 1: Foundation — Simplify State & Data Flow (Highest Priority)

**Goal**: Get the data flowing correctly before touching rendering.

1. **Clean up `state` object**
   - Remove unused genStats fields: `lastChatRender`, `lastChatContentKey`
   - Keep: `startTime`, `firstTokenTime`, `tokenCount`, `active`, throttle timestamps

2. **Simplify `stream_update` handler** (the biggest win)
   - Remove tiered throttle logic (150/300/750ms)
   - Replace with: update state → call `updateLastBubble()` for each changed agent → call `appendNewBubble()` if count increased
   - Add `streamActivityUpdate()` call at the TOP of stream_update (before any rendering)

3. **Simplify `state`/`done` handler**
   - Update state
   - Mark all panels dirty: `panel.dataset.dirty = 'true'`
   - Call deferred full re-render

4. **Remove message duplication guard complexity**
   - The server sends `historyCount` in stream_update — trust it. Truncate to historyCount, push responseMsgs. No overlap checking needed if we trust the server.

### Phase 2: Rendering Pipeline — Full Rewrite (No Root/Sub-agent Distinction)

**Goal**: Replace all incremental rendering with simple clear + rebuild on state events, and last-bubble-only updates during streaming. Same code for ALL agents.

5. **Write new core render functions**:
   - `clearPanel(instanceName)` 
   - `renderPanelMessages(instanceName)` — full re-render, no filtering, no isRoot
   - `updateLastBubble(instanceName)` — streaming update
   - `appendNewBubble(instanceName)` — append one bubble during streaming

6. **Simplify `updateBubbleContent(bubble, msg)`**
   - Remove all prevContent/prevReasoning tracking
   - Remove config param — just take (bubble, msg)
   - Just: `contentDiv.innerHTML = renderMessageHtml(msg)`
   - Keep details/code-block scroll restoration but inline it

7. **Simplify `createMessageEl(msg, index, instanceName)`**
   - Remove config param entirely — just pass instanceName directly for edit/delete operations
   - System messages: renderable, with distinct styling
   - **No isRoot check anywhere in this function**

8. **Rewrite `renderAgentTabs()`** (was renderSubAgents)
   - Iterate ALL agents including root — same loop body
   - Create/update tabs and panels identically
   - Tab title icon: 💬 for root, 🤖 for others (this is the ONLY place isRoot matters)
   - For dirty panels: call `clearPanel` + `renderPanelMessages`
   - Remove togglePulseElements call

9. **Remove ALL isRoot-specific logic**:
   - System message filtering in renderSubAgentPanel
   - Different active state derivation for root vs sub-agents
   - Special token/word count handling for root
   - CSS class helpers that take isRoot param but return the same thing
   - Config factory pattern (getRootAgentConfig, getSubAgentConfig)
   - configOverride parameter in renderAgentConversation

### Phase 3: Activity Tab & System Messages

**Goal**: Add the new features.

10. **Add Activity tab HTML** to index.html
11. **Implement `streamActivityUpdate()` and `renderActivityTab()`**
12. **Add system message banner** HTML + `showSystemMessageBanner()`
13. **Wire up activity tab** — always present, not closable

### Phase 4: UI Cleanup

**Goal**: Remove dead weight and polish.

14. **Remove per-panel activity bars** (HTML from panel initialization)
15. **Add Terminate button** next to Stop button in global controls
16. **Remove `appendStreamingDelta()`** — no longer needed
17. **Remove `togglePulseElements()`** — CSS handles pulse animation
18. **Remove `UIState`** object — unused
19. **Remove `setInnerHtmlWithState()`** — inlined

### Phase 5: Testing & Polish

**Goal**: Verify everything works.

20. **Test streaming** — verify only last bubble updates, no duplication
21. **Test state/done events** — verify full re-render on compression/edit/delete/rollback
22. **Test activity tab** — verify FIFO feed updates for visible agent only
23. **Test system messages** — verify they appear in chat AND trigger banner
24. **Test root agent rendering** — verify it uses the EXACT same code path as sub-agents
25. **Performance test** — verify no jank during long generations

---

## Reference: Main Branch Comparison

The main branch (`N:\work\WD\AgentCascade\web_ui\`) has a cleaner separation:

- `renderMessages()` / `fullRender()` for root agent with `state.messages`
- `renderSubAgents()` / `renderSubAgentPanel()` for sub-agents with `state.subAgents[name]`
- Activity bar is the simple `mainActivityBar` element below messages in the root panel

The unified branch tried to merge these into one path and it became spaghetti. The redesign keeps the unified data model (root agent in subAgents) but uses a much simpler rendering approach inspired by what worked in main: **full re-render on state change, update last bubble during streaming**. No content keys, no incremental delta appending, no prevContent tracking.

**Key difference from main branch**: We DON'T split root and sub-agent rendering — they go through the same functions. But the rendering logic is simple enough that there's no spaghetti: just iterate messages → create elements → append. The simplicity prevents divergence. The ONLY place root is treated differently is the tab icon (💬 vs 🤖).

---

## Appendix: Key Line References in Current app.js

| Feature | Approx Lines |
|---------|-------------|
| State definition | 37-75 |
| WebSocket handlers | 665-1217 |
| `handleServerMessage` — state/done | 774-912 |
| `handleServerMessage` — stream_update | 914-1133 |
| Rendering helpers (msgClass, headerClass, etc.) | 1267-1306 |
| `renderAgentConversation` | 1320-1344 |
| `createMessageEl` | 1346-1469 |
| `appendStreamingDelta` | 1492-1514 |
| `updateBubbleContent` | 1516-1589 |
| `setInnerHtmlWithState` | 1591-1618 |
| `renderMarkdown` | 1620-1679 |
| `togglePulseElements` | 2686-2693 |
| `updateControls` | 2695-2746 |
| `renderSubAgents` | 2197-2326 |
| `renderSubAgentPanel` | 2328-2541 |
| `switchMainTab` | 2545-2573 |