# Done Notification Sound Analysis

**Date:** 2026-06-14  
**Author:** NotificationSoundResearcher  
**Target:** Implement a "done" notification sound that only plays when the ROOT agent (main orchestrator, e.g., "Maine") transitions to IDLE state — not when sub-agents finish.

---

## 1. Current Behavior (What Triggers the Sound Now and Why It's Wrong)

### Current Trigger Logic

The "completed" sound is triggered in `web_ui/app.js` at **lines 1435-1442**:

```javascript
// Trigger sounds based on state changes
const newApprovalsCount = (state.approvals || []).length;
if (newApprovalsCount > prevApprovalsCount) {
  playSound('intervention');
} else if (wasGenerating && !state.generating) {
  playSound('completed');
  checkAfkAutoReply();
}
```

The `wasGenerating` variable is captured at the very beginning of `handleServerMessage()` at **line 952**:

```javascript
function handleServerMessage(data) {
  const wasGenerating = state.generating;
  const prevApprovalsCount = (state.approvals || []).length;
  // ...
```

### When `state.generating` Changes

`state.generating` is set from server data in two places:

1. **'state'/'done' handler** (line 990): `state.generating = data.generating ?? false;`
2. **'stream_update' handler** (line 1258): `state.generating = true;`

The `generating` field comes from the server:
- `build_state_from_pool()` in `api_integration.py` line 511: `'generating': generating,` (parameter passed in)
- `build_stream_update_from_pool()` in `api_integration.py` line 689: `'generating': True,` (always true during streaming)
- `run_agent_unified.py` line 225: The final broadcast sends `generating=False`

### The Problem

**`state.generating` is a GLOBAL flag** that indicates whether the system is currently generating. It does NOT distinguish between the root agent and sub-agents. When ANY agent finishes generating (root or sub), the final state broadcast sets `generating=False`, causing the completed sound to fire.

---

## 2. Root Cause of the Problem (Why Sub-Agent Completions Also Trigger the Sound)

### Architecture Overview

In the unified architecture:
- **ALL agents** (root + sub-agents) are `AgentInstance` objects in the pool
- Each agent has its own `parent_instance` field — the root agent has `parent_instance=None`
- Each agent has its own `agent_state` (IDLE, RUNNING, SLEEPING, COMPLETING, TERMINATED)
- The frontend receives state for ALL agents via `data.agent_instances` (a dict keyed by instance name)

### Data Flow for Sub-Agent Completion

When a sub-agent (e.g., "Coder1") finishes:

1. **ExecutionEngine** transitions Coder1's state from RUNNING → IDLE in the finally block (`execution_engine.py` lines 673-702)
2. **run_agent_unified.py** continues its tick loop — the main agent thread is still running
3. The next tick broadcasts a `stream_update` with `generating: True` (since the root is still generating)
4. BUT the final `done` event is only sent when the MAIN agent thread exits (`run_agent_unified.py` lines 221-237)

### The Race Condition

The issue is that `state.generating` is a **single boolean** shared across all agents. Consider this scenario:

1. Root agent (Maine) starts → `state.generating = true`
2. Maine calls sub-agent (Coder1) → Coder1 runs, then finishes
3. Coder1's state → IDLE, but Maine is still generating → `state.generating` stays `true`
4. Maine finishes → `state.generating = false` → **sound plays** ✓

This works correctly. BUT the problem occurs when:

1. Multiple sub-agents are called in parallel (or sequentially during main agent's turn)
2. The `generating` flag doesn't accurately reflect which agent is generating
3. If the root agent's `generating` is set to false (e.g., due to a pause/stop), the sound fires even though no "task completed" occurred

### Key Insight

The fundamental issue is that **`state.generating` is not per-agent**. It's a system-level flag. The frontend has no way to distinguish "root agent finished" from "sub-agent finished" from "system paused."

---

## 3. How Root Agent IDLE State Is Tracked Server-Side

### AgentState Enum (`agent_cascade/agent_instance.py` lines 20-40)

```python
class AgentState(Enum):
    IDLE = auto()
    RUNNING = auto()
    SLEEPING = auto()
    COMPLETING = auto()
    TERMINATED = auto()
```

### State Machine Transitions (`agent_instance.py` lines 128-149)

```python
valid_transitions = {
    AgentState.RUNNING: {AgentState.SLEEPING, AgentState.COMPLETING, AgentState.TERMINATED, AgentState.IDLE},
    AgentState.SLEEPING: {AgentState.RUNNING, AgentState.COMPLETING, AgentState.TERMINATED, AgentState.IDLE},
    AgentState.COMPLETING: {AgentState.TERMINATED, AgentState.IDLE},
    AgentState.TERMINATED: set(),  # Terminal state
    AgentState.IDLE: {AgentState.RUNNING, AgentState.TERMINATED},
}
```

### Where IDLE Transition Happens (`execution_engine.py` lines 673-702)

```python
with instance._state_lock:
    current_state = instance.state
    if current_state == AgentState.RUNNING:
        instance._transition(AgentState.IDLE)
    elif current_state == AgentState.SLEEPING:
        instance._transition(AgentState.IDLE)
    elif current_state == AgentState.COMPLETING:
        instance._transition(AgentState.IDLE)
```

This happens in the **finally block** of `ExecutionEngine.run()`, which is called for ALL agents (root and sub-agents alike).

### Root Agent Identification Server-Side (`api_integration.py` lines 453-457)

```python
root_instances = [
    name for name, inst in instance_snapshot.items()
    if inst.parent_instance is None
]
session_name = root_instances[0] if root_instances else instance_name
```

The root agent is identified by `parent_instance=None`. The `session_name` is set to the root instance's name.

### Serialization (`api_integration.py` lines 1043-1053)

```python
result = {
    'instance_name': inst.instance_name,
    'agent_class': inst.agent_class,
    'active': current_state == AgentState.RUNNING,
    'agent_state': current_state.name,  # ← KEY FIELD: "RUNNING", "IDLE", etc.
    'is_halted': pool.is_instance_halted(inst.instance_name),
    'parent_instance': inst.parent_instance,
    ...
}
```

**Every agent instance is serialized with its `agent_state` field**, including the root agent.

---

## 4. How Root Agent IDLE State Reaches the Frontend

### Data Path

1. **`build_state_from_pool()`** → serializes ALL instances via `_serialize_instance()` → includes `agent_state` for each
2. **`build_stream_update_from_pool()`** → same, for streaming updates
3. **`run_agent_unified.py`** → broadcasts `stream_update` events during execution, then `done` event at the end
4. **Frontend `handleServerMessage()`** → merges `data.agent_instances` into `state.subAgents[name]`

### Frontend State Structure (`app.js` lines 51-90)

```javascript
const state = {
  subAgents: {},           // { "Maine": { agent_state: "IDLE", ... }, "Coder1": {...} }
  activeStack: [],
  generating: false,       // GLOBAL flag — NOT per-agent
  sessionName: "Maine",    // Root agent name
  // ...
};
```

### Frontend Root Agent Identification (`app.js` lines 136-146)

```javascript
function isSessionPrimaryAgent(name) {
  return name === state.sessionName;
}
```

### How `agent_state` Is Used Frontend-Side

The `agent_state` field IS received and stored per-agent in `state.subAgents[name].agent_state`, but it's only used for:
- **Tab indicator coloring** (`app.js` lines 2521-2540): Shows RUNNING/SLEEPING indicators on tabs
- **NOT** for tracking state transitions or triggering sounds

### The `done` Event (`app.js` lines 956-957)

```javascript
case 'state':
case 'done':
  // Full state update — ALL agents flow through agent_instances, root included
```

The `done` event is handled identically to `state` — it merges all `agent_instances` including the root agent's `agent_state`.

---

## 5. Recommended Solution

### Analysis of Options

| Option | Pros | Cons |
|--------|------|------|
| **A) Frontend: Track root `agent_state`** | No server changes needed; minimal code; uses existing `agent_state` field | Requires tracking per-agent state transitions |
| **B) Server: New event/flag** | Clean semantic separation; explicit intent | Requires server changes; more complex |
| **C) Hybrid** | Best of both worlds | Most complex |

### Recommended: Option A (Frontend-Only)

This is the cleanest solution with minimal code changes. The `agent_state` field is already being sent for every agent instance. We just need to:

1. Track the root agent's previous `agent_state` in the frontend
2. Play the sound only when the root agent transitions from RUNNING → IDLE

### Specific Code Changes

#### Change 1: Add Root Agent State Tracker to Frontend State

**File:** `web_ui/app.js`  
**Location:** Lines 51-90 (state object initialization)

Add a new field to the `state` object:

```javascript
const state = {
  subAgents: {},
  activeStack: [],
  approvals: [],
  generating: false,
  agents: [],
  agentIndex: 0,
  viewingAgentIndex: 0,
  sessionName: localStorage.getItem('agent-cascade-session-name') || DEFAULT_SESSION_NAME,
  connected: false,
  editingIndex: null,
  activeSubTab: null,
  // ── NEW: Track root agent state for sound trigger ──
  _lastRootAgentState: null,  // Previous agent_state of the root agent (e.g., "RUNNING", "IDLE")
  // ── End new field ──
  genStats: {
    // ... rest unchanged
```

#### Change 2: Update Root Agent State on Every State/Stream Update

**File:** `web_ui/app.js`  
**Location:** Inside the 'state'/'done' handler, after merging agent_instances (around line 978)

After the block:
```javascript
// Merge ALL agent_instances including root — single source of truth, no legacy fallbacks
if (data.agent_instances) {
  for (const [name, sa] of Object.entries(data.agent_instances)) {
    state.subAgents[name] = sa;
  }
  cleanupStaleSubAgents(data, state);
}
```

Add:
```javascript
  // Track root agent state transition for sound trigger
  const rootAgentData = state.subAgents[state.sessionName];
  if (rootAgentData) {
    state._lastRootAgentState = rootAgentData.agent_state || null;
  }
```

**Also add to the 'stream_update' handler** (around line 1232, inside the agent_instances loop):

After the loop that processes `data.agent_instances`, add:
```javascript
  // Track root agent state for sound trigger (only on full updates, not partials)
  const rootAgentData = state.subAgents[state.sessionName];
  if (rootAgentData && !rootAgentData.is_partial) {
    state._lastRootAgentState = rootAgentData.agent_state || null;
  }
```

#### Change 3: Modify the Sound Trigger Condition

**File:** `web_ui/app.js`  
**Location:** Lines 1435-1442

**BEFORE:**
```javascript
  // Trigger sounds based on state changes
  const newApprovalsCount = (state.approvals || []).length;
  if (newApprovalsCount > prevApprovalsCount) {
    playSound('intervention');
  } else if (wasGenerating && !state.generating) {
    playSound('completed');
    checkAfkAutoReply();
  }
```

**AFTER:**
```javascript
  // Trigger sounds based on state changes
  const newApprovalsCount = (state.approvals || []).length;
  if (newApprovalsCount > prevApprovalsCount) {
    playSound('intervention');
  } else if (wasGenerating && !state.generating) {
    // Only play "completed" sound when the ROOT agent transitions RUNNING → IDLE
    const rootAgentData = state.subAgents[state.sessionName];
    const rootWasRunning = state._lastRootAgentState === 'RUNNING';
    const rootIsNowIdle = rootAgentData && rootAgentData.agent_state === 'IDLE';
    
    if (rootWasRunning && rootIsNowIdle) {
      playSound('completed');
      checkAfkAutoReply();
    }
    // If root agent didn't complete (e.g., sub-agent finished, or system paused),
    // don't play the sound — just update the UI silently
  }
```

### Alternative: Even Simpler Approach (Check `data.agent_instances` Directly)

If we want to minimize state tracking, we can check the root agent state directly from the incoming `data` object in the 'done' handler, before the state changes:

**File:** `web_ui/app.js`  
**Location:** Line 952 (start of `handleServerMessage`)

**BEFORE:**
```javascript
function handleServerMessage(data) {
  const wasGenerating = state.generating;
  const prevApprovalsCount = (state.approvals || []).length;
```

**AFTER:**
```javascript
function handleServerMessage(data) {
  const wasGenerating = state.generating;
  const prevApprovalsCount = (state.approvals || []).length;
  
  // Capture root agent state BEFORE state changes (for 'done' event)
  let rootWasRunning = false;
  let rootIsNowIdle = false;
  if (data.type === 'done' && data.agent_instances) {
    const rootData = data.agent_instances[state.sessionName];
    if (rootData) {
      rootWasRunning = state.subAgents[state.sessionName]?.agent_state === 'RUNNING';
      rootIsNowIdle = rootData.agent_state === 'IDLE';
    }
  }
```

Then in the sound trigger (lines 1439-1441):

**BEFORE:**
```javascript
  } else if (wasGenerating && !state.generating) {
    playSound('completed');
    checkAfkAutoReply();
  }
```

**AFTER:**
```javascript
  } else if (wasGenerating && !state.generating && data.type === 'done' && rootWasRunning && rootIsNowIdle) {
    playSound('completed');
    checkAfkAutoReply();
  }
```

This approach is even simpler — no new state fields, just check the incoming data directly.

---

## 6. Edge Cases to Consider

### 6.1 Root Agent Paused Mid-Stream

**Scenario:** User pauses the system while the root agent is generating.

- Server sends `done` event with `generating: false` and root `agent_state: IDLE` (or `COMPLETING` → `IDLE`)
- With our fix: If root was RUNNING and is now IDLE, sound plays. This is arguably correct — the task was interrupted, and the user should be notified.
- **Recommendation:** This behavior is acceptable. The user explicitly paused, so the sound serves as confirmation.

### 6.2 Root Agent Stopped Mid-Stream

**Scenario:** User clicks "Stop" while the root agent is generating.

- Similar to pause. The `done` event will have `generating: false` and root `agent_state: IDLE`.
- Sound plays. This is correct behavior — the user should be notified that the agent stopped.

### 6.3 Root Agent Terminated

**Scenario:** Root agent reaches TERMINATED state (e.g., max turns exceeded, error).

- In `execution_engine.py` line 693: TERMINATED state has no transition to IDLE
- Root `agent_state` would be `TERMINATED`, not `IDLE`
- With our fix: `rootIsNowIdle` would be `false`, so the sound does NOT play.
- **Recommendation:** Consider whether a TERMINATED state should also trigger a sound. If so, modify the condition:
  ```javascript
  const rootCompleted = rootIsNowIdle || rootAgentData?.agent_state === 'TERMINATED';
  ```

### 6.4 Root Agent in SLEEPING State

**Scenario:** Root agent calls a sub-agent and enters SLEEPING state, then wakes up and finishes.

- The `agent_state` transitions: RUNNING → SLEEPING → RUNNING → IDLE
- Sound only plays on the final RUNNING → IDLE transition
- **This is correct behavior.**

### 6.5 Multiple WebSocket Connections

**Scenario:** Multiple browser tabs connected to the same session.

- Each tab has its own `state` object and `handleServerMessage`
- Each tab independently checks the root agent state
- No synchronization needed — each tab plays the sound independently
- **Potential issue:** Sound plays multiple times if multiple tabs are open
- **Recommendation:** Acceptable behavior. Users can disable sounds per-tab via settings.

### 6.6 Session Reset / New Session

**Scenario:** User resets the session or starts a new one.

- `state.generating` is set to `false`
- Root agent state is reset to IDLE
- With our fix: `_lastRootAgentState` would be `null` (never set), so `rootWasRunning` is `false`, and the sound does NOT play.
- **This is correct behavior.**

### 6.7 Stream Update vs. Done Event

**Scenario:** Sub-agent finishes during streaming.

- Stream update is sent with `generating: true` (root still generating)
- `wasGenerating` is `true`, `state.generating` stays `true`
- Sound condition `wasGenerating && !state.generating` is `false`
- **Sound does NOT play.** Correct behavior.

### 6.8 Error During Execution

**Scenario:** Root agent encounters an error and stops.

- Error event is sent (`data.type === 'error'`)
- `state.generating` is set to `false` (line 1427)
- Sound condition: `wasGenerating && !state.generating` is `true`
- But `data.type` is `'error'`, not `'done'`
- With the alternative approach (checking `data.type === 'done'`), the sound does NOT play for errors.
- **Recommendation:** This is correct — errors should not trigger the "completed" sound.

### 6.9 `agent_state` Field Not Present in Data

**Scenario:** Server sends incomplete data (e.g., old client, partial serialization).

- `rootAgentData?.agent_state` would be `undefined`
- `rootIsNowIdle` would be `false` (undefined !== 'IDLE')
- Sound does NOT play
- **This is safe behavior** — fail silently rather than playing incorrectly.

---

## 7. Summary

### Current Problem
The "completed" sound plays whenever `state.generating` transitions from `true` to `false`, which happens for ANY agent completion (root or sub-agent) because `state.generating` is a global flag.

### Root Cause
`state.generating` is a system-level boolean with no per-agent granularity. The frontend has no way to distinguish which agent finished.

### Solution
Track the root agent's `agent_state` field (already sent by the server for every agent) and only play the sound when the root agent specifically transitions from RUNNING → IDLE.

### Implementation Effort
- **Files to modify:** `web_ui/app.js` only (1 file)
- **Lines to add/modify:** ~15 lines total
- **Server changes needed:** None
- **Risk level:** Low (uses existing data, no new protocol changes)

### Key Files Referenced
| File | Relevant Lines | Purpose |
|------|---------------|---------|
| `web_ui/app.js` | 952, 1435-1442, 956-957, 972-978 | Sound trigger, state handling |
| `web_ui/app.js` | 51-90 | State object definition |
| `web_ui/app.js` | 136-146 | Root agent identification |
| `agent_cascade/api_integration.py` | 453-457 | Root agent identification server-side |
| `agent_cascade/api_integration.py` | 1043-1053 | Instance serialization with `agent_state` |
| `agent_cascade/agent_instance.py` | 20-40, 128-149 | AgentState enum and state machine |
| `agent_cascade/execution_engine.py` | 673-702 | IDLE transition in finally block |
| `agent_cascade/run_agent_unified.py` | 221-237 | `done` event broadcast |