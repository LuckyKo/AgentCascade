# WebUI Unification Plan — Merge Chat and Agent Instance Tabs

## Goal

Make the main chat tab and agent instance tabs look/feel identical. All agents are just nodes in a call tree, so they should be treated equally with unified rendering, styling, and behavior. The only visual distinction should be subtle hints (agent name, slight indentation) that show hierarchy without creating a "second-class" feel for sub-agents.

---

## Current State Analysis

### Architecture Overview

The codebase has **two rendering paths** that are partially unified but still have differences:

| Aspect | Main Chat (Root) | Agent Instance (Sub) |
|--------|-----------------|---------------------|
| Message element creation | `createMessageEl(msg, index, config)` | `createSubMsgEl()` — **DEAD CODE** |
| Bubble content update | `updateBubbleContent(bubble, msg, config)` | `updateSubBubbleContent()` — **DEAD CODE** |
| CSS classes for messages | `.message msg-{role}` + `[data-agent-type="root"]` | `.sub-msg sub-msg-{role}` (old) / `.message msg-{role}` + `[data-agent-type="sub"]` (new path) |
| Panel structure | `#panelChat > .context-bar + .messages.messages-scroll + .main-activity-bar` | `#panelSub-{name} > .context-bar + .sub-agent-messages.messages-scroll + .sub-agent-activity-bar` |
| Scroll container class | `.messages.messages-scroll` | `.sub-agent-messages.messages-scroll` |
| Activity bar class | `.main-activity-bar` | `.sub-agent-activity-bar` (has inline pause/terminate buttons) |
| Rendering entry point | `renderMessages()` | `renderSubAgentPanel(panel, agentData, name)` |
| Incremental append | `renderAgentConversation('root', msgs, 0, indexMap)` | `renderAgentConversation(name, msgs, 1, indexMap)` — **already unified** |

### Key Finding: Partial Unification Already Exists

The codebase already has a unified rendering path via `renderAgentConversation()` → `createMessageEl()` with config. Both main chat and sub-agent panels now use this for incremental appends (lines 2543-2563 in app.js). However, the **dead code** (`createSubMsgEl`, `updateSubBubbleContent`) is still present but not called from the new path.

### Specific Differences to Fix

#### 1. Dead Code Removal (Low Risk)
- **`createSubMsgEl`** (line 2571-2648): Creates elements with `.sub-msg`, `.sub-msg-header`, `.sub-msg-content`, `.sub-msg-label` — all different from the unified classes
- **`updateSubBubbleContent`** (line 2651-2719): Reads `.sub-msg-content`, uses `dataset.prevSubContent`/`dataset.prevSubReasoning` instead of `dataset.prevContent`/`dataset.prevReasoning`

#### 2. Panel Structure Differences (Medium Risk — JS selector references)
- Main chat panel: `<div id="panelChat"> > .context-bar + .messages.messages-scroll + .main-activity-bar`
- Sub-agent panel: `<div id="panelSub-{name}"> > .context-bar + .sub-agent-messages.messages-scroll + .sub-agent-activity-bar`

**CRITICAL:** Four JS locations reference `.sub-agent-messages` by selector that must be updated before changing the CSS class:
- Line 846: `mainTabPanels.querySelectorAll('.sub-agent-messages')` — cache invalidation for edits/deletes
- Line 1977 (`startEdit`): `#panelSub-${instanceName} .sub-agent-messages`
- Line 2107 (`confirmEdit`): `#panelSub-${instanceName} .sub-agent-messages`
- Line 2128 (`cancelEdit`): `#panelSub-${instanceName} .sub-agent-messages`

#### 3. Activity Bar Differences (Medium Risk)
- Main: `.main-activity-bar` with IDs `mainQueuedMsg`, no inline buttons (uses global bottom bar)
- Sub: `.sub-agent-activity-bar` with inline pause/terminate buttons, different structure

#### 4. CSS Visual Hierarchy (Medium Risk)
Current `[data-agent-type="sub"]` styling (line 1036-1045):
```css
[data-agent-type="sub"] {
    border-left: 2px solid var(--border-color, #333);
    margin-left: 16px;
}
[data-agent-type="sub"] .msg-name {
    font-size: 11px;
    color: var(--text-muted);
    font-weight: 600;
    margin-bottom: 2px;
}
```

This makes sub-agent messages look "indented and demoted."

#### 5. Label Differences (Low Risk)
- Main chat: "You", "Assistant", "Tool Result"
- Sub-agent: "📤 Task", "Agent", "result" (via `roleName()` at line 1417)

#### 6. `isGenerating` Only Checks Root State (Low Risk, High Value)
In `createMessageEl` (line 1557):
```js
const isGenerating = state.generating && index === state.messages.length - 1;
```
This only checks root state. For sub-agents streaming while root is idle, thinking blocks won't show the "streaming" animation during full re-renders.

---

## Target Unified Design

### Visual Design Principles

1. **Equal treatment**: All agents look the same — same message bubble style, same spacing, same font sizes
2. **Subtle hierarchy hints**: Agent name in tab bar and message header shows which agent is speaking; slight left border color coding per agent (optional)
3. **Consistent labels**: Use "You" for user messages in all tabs, use the agent's actual name (e.g., "Coder", "Reviewer") instead of generic "Agent"

### Target Architecture

```
Unified rendering path:
  renderMessages() ──────────────────────────────┐
                                                 │
  renderSubAgentPanel() ─→ renderAgentConversation() → createMessageEl(msg, idx, config)
    (unified via renderAgentConversation)         │
                                                 │
  updateBubbleContent(bubble, msg, config) ←──────┘
```

---

## Implementation Steps (Ordered by Risk: Safest First)

### Step 1: Remove Dead Code ✅ (Lowest Risk)

**Files affected:** `web_ui/app.js` — lines 2571-2719

**Changes:**
1. Delete `createSubMsgEl()` function (lines 2571-2648)
2. Delete `updateSubBubbleContent()` function (lines 2651-2719)

**Rationale:** These functions are not called anywhere in the new unified path. Removing them reduces code duplication and prevents accidental use.

---

### Step 2: Fix JS Selector References ✅ (Medium Risk — Prerequisite for Step 4)

**Files affected:** `web_ui/app.js`

**Changes:**
Change all `.sub-agent-messages` references to `.messages-scroll` (which is present in both main and sub-agent panels):

| Line | Current | After |
|------|---------|-------|
| 846 | `mainTabPanels.querySelectorAll('.sub-agent-messages')` | `mainTabPanels.querySelectorAll('.messages-scroll')` |
| 1977 | `#panelSub-${instanceName} .sub-agent-messages` | `#panelSub-${instanceName} .messages-scroll` |
| 2107 | `#panelSub-${instanceName} .sub-agent-messages` | `#panelSub-${instanceName} .messages-scroll` |
| 2128 | `#panelSub-${instanceName} .sub-agent-messages` | `#panelSub-${instanceName} .messages-scroll` |

**Rationale:** `.messages-scroll` is the shared class present on both main chat and sub-agent scroll containers. Using it avoids breaking edit/delete/cancel operations when we change the CSS class in Step 4.

---

### Step 3: Unify Visual Hierarchy CSS ✅ (Medium Risk — Pure CSS, No JS Dependencies)

**Files affected:** `web_ui/styles.css` — lines 1036-1046

**Changes:**

Replace current `[data-agent-type="sub"]` styling with subtler hierarchy hints:

```css
/* Before: Indented + muted sub-agent messages */
[data-agent-type="sub"] {
    border-left: 2px solid var(--border-color, #333);
    margin-left: 16px;
}
[data-agent-type="sub"] .msg-name {
    font-size: 11px;
    color: var(--text-muted);
    font-weight: 600;
    margin-bottom: 2px;
}

/* After: Equal treatment with subtle distinction */
[data-agent-type="sub"] {
    border-left: 2px solid transparent; /* Same width, but subtle accent when active */
}

/* Show agent-specific dot in name label */
[data-agent-type="sub"][data-instance-name] .msg-name::before {
    content: '';
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent);
    margin-right: 4px;
}

/* Sub-agent name styling — same font size, but slightly different color to distinguish */
[data-agent-type="sub"] .msg-name {
    font-size: 12px; /* Same as root */
    color: var(--text-secondary); /* Slightly muted but readable */
    font-weight: 600;
}
```

**Rationale:** Sub-agents should look equal, not demoted. The left border and small dot provide subtle visual hierarchy without indentation.

---

### Step 4: Unify Panel CSS Classes ✅ (Medium Risk — Now Safe After Step 2)

**Files affected:**
- `web_ui/app.js` — line 2436 (sub-agent scroll container creation)
- `web_ui/styles.css` — lines 271-278, 525-530

**Changes in app.js:**
1. Change `.sub-agent-messages.messages-scroll` → `.messages.messages-scroll` for sub-agent panels (line 2436)

**Changes in styles.css:**
1. Remove or merge `.sub-agent-messages` selector into `.messages` (lines 271-278) — verify all needed properties are already in `.messages` at lines 921-929
2. Remove `.sub-agent-panel .sub-agent-messages` selector (lines 525-530) since it's now redundant with `.messages`

**Verification:** The unified `.messages` selector at line 921 already includes:
- `flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 8px; scroll-behavior: smooth;`
This covers ALL properties previously split across `.sub-agent-messages` and `.sub-agent-panel .sub-agent-messages`.

**Rationale:** Same HTML structure = same CSS applies automatically. Reduces CSS duplication.

---

### Step 5: Unify Activity Bar Structure ✅ (Medium Risk)

**Files affected:** `web_ui/app.js` — lines 2440-2475; `web_ui/styles.css` — lines 532-549

**Design Decision:** Keep inline pause/terminate buttons in sub-agent activity bars (they're contextually appropriate for per-agent control), but merge the CSS classes so they look identical apart from buttons.

**Changes in app.js:**
1. Change `.sub-agent-activity-bar` → `.main-activity-bar` for sub-agent panels (line 2441)
2. Keep inline pause/terminate buttons as-is in sub-agent activity bars
3. Update `renderSubAgentPanel` to use `.main-activity-bar` selector (line 2478)

**Changes in styles.css:**
1. Remove `.sub-agent-activity-bar` from combined selector at line 532: change `.sub-agent-activity-bar, .main-activity-bar` → just `.main-activity-bar`
2. Remove `.sub-agent-activity-bar.active` from combined selector at line 548: change `.main-activity-bar.active, .sub-agent-activity-bar.active` → just `.main-activity-bar.active`

**Rationale:** Consistent styling with contextually appropriate per-agent controls.

---

### Step 6: Fix `isGenerating` in `createMessageEl` ✅ (Low Risk, High Value)

**Files affected:** `web_ui/app.js` — line 1557

**Changes:**
```javascript
// Before (line 1557):
const isGenerating = state.generating && index === state.messages.length - 1;

// After:
const isGenerating = config.isGenerating !== undefined 
  ? config.isGenerating 
  : (state.generating && index === state.messages.length - 1);
```

**Rationale:** When a sub-agent streams but root doesn't, thinking blocks need the "streaming" animation. The `config.isGenerating` flag is already passed for sub-agent streaming in `renderSubAgentPanel` at line 2562. This fix makes full re-renders (e.g., tab switch) show correct streaming state.

---

### Step 7: Unify Message Labels ✅ (Low Risk)

**Files affected:** `web_ui/app.js` — lines 1417-1429 (`roleName()` function)

**Changes:**
```javascript
// Before:
function roleName(role, isRoot, msg) {
    if (!isRoot) {
        if (role === 'user') return '📤 Task';
        if (role === 'assistant') return 'Agent';
        if (role === 'tool') return 'result';
        return role;
    }
    // Root labels
    if (role === 'user') return 'You';
    if (role === 'assistant') return 'Assistant';
    if (role === 'tool') return 'Tool Result';
    return role;
}

// After:
function roleName(role, isRoot, msg) {
    // User messages are always "You" regardless of which agent's tab you're in
    if (role === 'user') return 'You';
    
    // Assistant messages show the agent's actual name
    if (role === 'assistant') {
        if (isRoot) return 'Assistant';  // Root agent = "Assistant"
        return msg.name || 'Agent';      // Sub-agent shows its name (e.g., "Coder", "Reviewer")
    }
    
    if (role === 'tool' || role === 'function') return 'Tool Result';
    return role;
}
```

**Rationale:** Consistent terminology. "You" means the human in any context. Agent names are shown for clarity.

---

### Step 8: Unify Auto-Scroll Behavior ✅ (Medium Risk)

**Files affected:** `web_ui/app.js` — lines 2437, 2535, 2568

**Changes:**
Add the same `requestAnimationFrame` auto-scroll pattern to sub-agent panels that main chat uses. However, `isAutoScrollLocked` is a global variable, so we need per-panel lock state AND a scroll event listener per panel.

**Part A — Add scroll lock state and listener (inside the `!panel.dataset.initialized` block):**

```javascript
// After panel.appendChild(scrollContainer) at line 2437, add:
subAgentScrollLocks[name] = true; // Start locked (auto-scroll enabled by default)
scrollContainer.addEventListener('scroll', () => {
    const distFromBottom = scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight;
    subAgentScrollLocks[name] = (distFromBottom < 50);
});
```

This mirrors the main chat pattern at lines 109-113. **Critical:** Without this listener, user scroll events are never captured and the rAF logic has no way to know the user scrolled up.

**Part B — Replace synchronous scroll with rAF (line 2568):**

```javascript
// Before (line 2568):
if (wasAtBottom) scrollContainer.scrollTop = scrollContainer.scrollHeight;

// After:
requestAnimationFrame(() => {
    const atBottom = scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight < 50;
    
    if (!subAgentScrollLocks[name]) subAgentScrollLocks[name] = false;
    
    if (subAgentScrollLocks[name]) {
        subAgentScrollLocks[name] = false; // Unlock on each tick so user can scroll freely
        scrollContainer.scrollTop = scrollContainer.scrollHeight;
    } else if (atBottom) {
        subAgentScrollLocks[name] = true; // Re-lock if user scrolled back to bottom
    }
});
```

The `else if (atBottom)` re-lock is a **safety net** — it ensures auto-scroll resumes when the user scrolls back to the bottom, even if the scroll listener missed an event. This matches the main chat pattern at lines 1293-1294.

**Part C — Remove synchronous wasAtBottom (line 2535):**
Remove the `const wasAtBottom = ...` line since rAF now handles all scrolling logic.

**Rationale:** Smooth, consistent scrolling experience across all tabs. Prevents jank from synchronous scroll during render. User can scroll up mid-stream without fighting auto-scroll.

---

### Step 9: Add Context Bar Throttling for Sub-Agents ✅ (Low Risk)

**Files affected:** `web_ui/app.js` — lines 2546-2558

**Approach:** Use the shared global `state.genStats.lastContextBarUpdate` throttle (same as main chat). Simple and adequate since context bar visual changes are not time-critical.

**Changes:**
```javascript
// In renderSubAgentPanel, wrap context bar updates with throttle:
if (!state.genStats.lastContextBarUpdate) state.genStats.lastContextBarUpdate = 0;
const nowInRender = performance.now();
if (nowInRender - state.genStats.lastContextBarUpdate > 1000 || currentCount !== lastCount) {
    const fillEl = document.getElementById('subContextFill-' + name);
    if (fillEl) updateContextBar(fillEl, msgs, agentData.total_tokens, agentData.max_tokens);
    state.genStats.lastContextBarUpdate = nowInRender;
}
```

**Rationale:** Performance consistency. Prevents unnecessary DOM updates during rapid streaming.

---

### Step 10: Clean Up CSS and JS ✅ (Low Risk)

**Files affected:** `web_ui/styles.css`, `web_ui/app.js`

**Changes:**
1. Remove `.sub-agent-messages` selector if it still exists after Step 4 (lines 271-278)
2. Remove `.sub-agent-panel .sub-agent-messages` if it still exists after Step 4 (lines 525-530)
3. Verify all markdown content styles use `.msg-content` (already unified at line 280)
4. **Note:** `.sub-msg-*` CSS classes don't exist in the stylesheet — they were only used in dead JS code. No CSS cleanup needed for those.
5. Add `subAgentScrollLocks` cleanup: when a sub-agent tab is closed (in `renderSubAgents` at line 2316), delete its lock entry: `delete subAgentScrollLocks[agentName];`

---

### Step 11: Add Agent-Specific Visual Identity ✅ (Optional, Low Risk)

**Files affected:** `web_ui/styles.css` — new selectors; `web_ui/app.js` — minor additions

**Changes:**
1. Assign each agent a subtle color accent for the left border of its messages
2. Show this color in the tab icon as well

```css
/* Per-agent accent colors via CSS custom properties */
[data-instance-name="coder"] { --agent-accent: #58a6ff; }
[data-instance-name="reviewer"] { --agent-accent: #3fb950; }
[data-instance-name="security"] { --agent-accent: #f85149; }

[data-agent-type="sub"] {
    border-left-color: var(--agent-accent, var(--accent));
}
```

**Rationale:** Helps quickly identify which agent is speaking when scanning a conversation.

---

## File-by-File Change Summary

### `web_ui/app.js`

| Line(s) | Action | Risk | Description |
|---------|--------|------|-------------|
| 2571-2648 | **DELETE** | Low | Remove dead `createSubMsgEl()` function |
| 2651-2719 | **DELETE** | Low | Remove dead `updateSubBubbleContent()` function |
| 846 | **EDIT** | Medium | Change `.sub-agent-messages` → `.messages-scroll` |
| 1977 | **EDIT** | Medium | Change `.sub-agent-messages` → `.messages-scroll` in `startEdit` |
| 2107 | **EDIT** | Medium | Change `.sub-agent-messages` → `.messages-scroll` in `confirmEdit` |
| 2128 | **EDIT** | Medium | Change `.sub-agent-messages` → `.messages-scroll` in `cancelEdit` |
| 1557 | **EDIT** | Low | Fix `isGenerating` to check `config.isGenerating` first |
| 1417-1429 | **EDIT** | Low | Unify `roleName()` labels |
| 2436 | **EDIT** | Medium | Change sub-agent scroll container class from `.sub-agent-messages` to `.messages` |
| 2441 | **EDIT** | Medium | Change activity bar class from `.sub-agent-activity-bar` to `.main-activity-bar` |
| 2535, 2568 | **EDIT** | Medium | Add requestAnimationFrame auto-scroll pattern |
| 2437 | **EDIT** | Medium | Add per-panel scroll listener and subAgentScrollLocks initialization |
| ~109 | **ADD** | Low | Declare `const subAgentScrollLocks = {}` near existing `isAutoScrollLocked` |
| 2316 | **EDIT** | Low | Add `delete subAgentScrollLocks[agentName]` when agent tab is closed |
| 2546-2558 | **EDIT** | Low | Add context bar throttling for sub-agents |

### `web_ui/styles.css`

| Line(s) | Action | Risk | Description |
|---------|--------|------|-------------|
| 271-278 | **DELETE** | Low | Remove `.sub-agent-messages` selector (now using `.messages`) |
| 525-530 | **DELETE** | Low | Remove `.sub-agent-panel .sub-agent-messages` (redundant with `.messages`) |
| 532-549 | **EDIT** | Low | Merge `.sub-agent-activity-bar` into `.main-activity-bar` only |
| 1036-1046 | **EDIT** | Medium | Refine `[data-agent-type="sub"]` visual hierarchy |

### `web_ui/index.html`

No changes needed — HTML structure is already unified at the tab level. Sub-agent panels are created dynamically with matching structure after Step 5.

---

## Testing Checklist

After each step, verify:
- [ ] Main chat tab renders correctly
- [ ] Agent instance tabs render correctly
- [ ] Switching between tabs works without visual glitches
- [ ] Streaming updates work in both main chat and sub-agent tabs
- [ ] Auto-scroll works consistently across all tabs
- [ ] Edit/delete message buttons work for all message types (CRITICAL after Step 2)
- [ ] Context bars update properly
- [ ] Activity bars show correct status
- [ ] Pause/terminate buttons still function
- [ ] No console errors

---

## Rollback Plan

Steps are sequenced to minimize risk:
1. **Steps 1-3** are independent and safe — dead code removal, CSS-only changes
2. **Step 2** (JS selector fix) MUST be done before **Step 4** (CSS class change) — otherwise edit/delete breaks in sub-agent tabs
3. **Step 5** (activity bar) depends on Step 4 being complete
4. If issues arise after Steps 4-5, reverting Steps 2+4 together restores full functionality