# Bug 36: Agent Activity Detector Fails — UI Returns to Root Agent

## Executive Summary

The frontend's active stack handling has a **data type mismatch**: the backend sends `active_stack` as an array of tuples `[["name", depth], ...]`, but the frontend treats each element as a plain string. This causes all auto-switch logic to fail, so the UI never switches to sub-agent tabs and always remains on or returns to the root agent tab.

---

## Root Cause

### Backend Data Format (Correct)

In `agent_cascade/agent_pool.py`, `ParallelAgentManager.active_stack` is explicitly typed as:

```python
# agent_pool.py, line 1150
self.active_stack: List[tuple] = []  # Stack of (instance_name, nest_depth) tuples
```

Elements are appended as tuples at `execution_engine.py:1816`:
```python
self.pool._execution.active_stack.append((instance_name, inst._nest_depth))
```

When serialized in `api_integration.py:455`, the active_stack is copied as-is:
```python
active_stack = list(pool._execution.active_stack) if hasattr(pool, '_execution') else []
```

This produces JSON like: `[["Maine", 0], ["Agent_Coder", 1]]`

### Frontend Data Handling (Incorrect)

The frontend at `web_ui/app.js` treats every element of `activeStack` as a string. This breaks in **five** places:

#### Bug Location #1 — Auto-switch condition (willSwitchTab), lines 1181-1182
```javascript
const willSwitchTab = stackChanged && (
  (state.activeStack.length > 0 && state.subAgents?.[state.activeStack[state.activeStack.length - 1]] && 
   state.activeSubTab !== 'sub-' + state.activeStack[state.activeStack.length - 1]) ||
```

- `state.activeStack[state.activeStack.length - 1]` returns `["Agent_Coder", 1]` (an array)
- `state.subAgents?.[["Agent_Coder", 1]]` → `undefined` (array key doesn't exist in subAgents)
- `'sub-' + ["Agent_Coder", 1]` → `"sub-Agent_Coder,1"` (wrong tab ID format)
- **Result**: `willSwitchTab` is always `false` when a sub-agent is on the stack

#### Bug Location #2 — Auto-switch execution, lines 1193-1195
```javascript
const topAgent = state.activeStack[state.activeStack.length - 1];
if (state.subAgents && state.subAgents[topAgent] && state.activeSubTab !== 'sub-' + topAgent) {
  switchMainTab('sub-' + topAgent);
}
```

- `topAgent` is `["Agent_Coder", 1]` (array, not string)
- `state.subAgents[["Agent_Coder", 1]]` → `undefined`
- **Result**: `switchMainTab` is never called for sub-agents

#### Bug Location #3 — Activity indicator, line 2271 and 2330
```javascript
const activeTop = state.activeStack.length > 0 ? state.activeStack[state.activeStack.length - 1] : null;
...
if (activeTop === name) {
  tabBtn.classList.add('has-activity');
}
```

- `activeTop` is `["Agent_Coder", 1]`, `name` is `"Agent_Coder"` (string)
- `activeTop === name` → `false` (array never equals string)
- **Result**: Active sub-agent tabs don't show the activity indicator

#### Bug Location #4 — Stack change detection, lines 1029 and 1129
```javascript
const oldStackStr = (state.activeStack || []).join(',');
...
const newStackStr = (state.activeStack || []).join(',');
```

- `["Maine", 0, "Agent_Coder", 1]` instead of `"Maine,Agent_Coder"`
- **Result**: Stack change detection is noisy but still works (both old and new stacks have same format)

---

## Why This Bug Manifests as "Returns to Root Agent"

The auto-switch logic never triggers because `state.subAgents[topAgent]` always returns `undefined`. The UI therefore:

1. **Never switches** to the sub-agent tab when a sub-agent starts
2. **Stays on** whatever tab was last manually selected (likely root)
3. When the sub-agent completes and the stack empties, the `else` branch at line 1198 correctly switches back to root — but since we never switched away in the first place, this is a no-op visually

Additionally, if the user had previously selected a sub-agent tab and then a stream_update arrives with an empty stack (e.g., after a brief pause or during tool execution), the `else` branch at lines 1198-1203 triggers:
```javascript
} else {
  const primaryTab = getAgentTabId(state.sessionName);
  if (state.activeSubTab !== primaryTab) {
    switchMainTab(primaryTab);
  }
}
```

This switches back to root because the `stackChanged` flag is true and the stack is empty. But this only happens when `stackChanged` is true — which requires a difference between old and new `active_stack`. If the backend's active_stack doesn't change (e.g., sub-agent still running, same tuple on stack), no switch occurs.

---

## Connection to cleanupStaleSubAgents() / switchMainTab()

The recent additions of `cleanupStaleSubAgents()` and modifications to `switchMainTab()` did **not** introduce this bug directly. The bug existed before these changes because it's a fundamental data type mismatch between backend and frontend.

However, `cleanupStaleSubAgents()` could exacerbate the issue in one edge case:
- If `cleanupStaleSubAgents` resets `state.activeSubTab` to `null` (line 167) when dismissing an agent, and then a subsequent stream_update has an empty stack with `stackChanged = true`, the auto-switch at line 1202 would switch to root.
- But this is correct behavior for dismissed agents — it only looks incorrect because the auto-switch never works for active sub-agents in the first place.

---

## Fix Required

The frontend needs to extract the agent name from each tuple element. The fix should be applied at all five locations:

```javascript
// Instead of: state.activeStack[state.activeStack.length - 1]
// Use: state.activeStack[state.activeStack.length - 1][0]

// Or better: normalize active_stack on receive by extracting just names:
if (data.active_stack) {
  state.activeStack = data.active_stack.map(entry => 
    Array.isArray(entry) ? entry[0] : entry
  );
}
```

This normalization should happen at line 1118 (`stream_update` handler) and line 908 (`state`/`done` handler):
```javascript
// Line 1118:
if (data.active_stack) {
  state.activeStack = data.active_stack.map(e => Array.isArray(e) ? e[0] : e);
}

// Line 908:
state.activeStack = (data.active_stack || []).map(e => Array.isArray(e) ? e[0] : e);
```

---

## Files Affected

| File | Lines | Issue |
|------|-------|-------|
| `web_ui/app.js` | 1181-1182 | `willSwitchTab` condition uses array key |
| `web_ui/app.js` | 1193-1195 | `switchMainTab` never called for sub-agents |
| `web_ui/app.js` | 2271, 2330 | Activity indicator broken |
| `web_ui/app.js` | 1029, 1129 | Stack change detection noisy |
| `agent_cascade/agent_pool.py` | 1150 | Backend type: `List[tuple]` |
| `agent_cascade/execution_engine.py` | 1816 | Appends `(name, depth)` tuples |
| `agent_cascade/api_integration.py` | 455 | Serializes raw tuples to JSON |

---

## Severity

**High** — This is a critical UI bug that completely breaks the sub-agent tab switching feature. Users cannot view active sub-agent output through the auto-switch mechanism, making the cascading agent workflow unusable from the frontend perspective.