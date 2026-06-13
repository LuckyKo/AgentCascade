# AgentCascade - Streaming Rewrite & Frontend Refactor Plan

**Date:** 2026-06-13  
**Author:** PlanCreator (Coder Agent)  
**Supervisor:** Maine  
**Status:** Ready for Execution  

---

## Executive Summary

This document provides a comprehensive, actionable plan for two major initiatives:

1. **INITIATIVE 1**: Remove all existing agent tab message streaming/rendering/refresh code
2. **INITIATIVE 2**: Replace with a clean, minimal new streaming implementation  
3. **INITIATIVE 3**: Refactor `app.js` (4,180 lines) into ~13 ES6 modules

The plan is designed so that a fresh agent can execute each phase without needing context from previous attempts.

### Key Statistics

| Metric | Value |
|--------|-------|
| Backend streaming code | ~2,500 lines across 4 files |
| Frontend streaming code | ~1,500 lines in app.js |
| Total app.js lines | 4,180 lines (177 KB) |
| Natural module boundaries | 13 identified |
| Existing modules | 1 (ActivityBar object literal) |
| WebSocket message types | 7 server→client, 11 client→server |

---

## Table of Contents

1. [Phase Overview](#phase-overview)
2. [Initiative 1: Streaming Removal](#initiative-1-streaming-removal)
3. [Initiative 2: New Streaming Implementation](#initiative-2-new-streaming-implementation)
4. [Initiative 3: Frontend Refactoring](#initiative-3-frontend-refactoring)
5. [Testing Strategy](#testing-strategy)
6. [Rollback Plan](#rollback-plan)
7. [Effort Estimates](#effort-estimates)

---

## Phase Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         PROJECT TIMELINE                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Phase 0: Preparation     │ 1-2 days    │ Setup, backups, test harness     │
│  ├─ Branch creation       │             │                                  │
│  ├─ Backup current state  │             │                                  │
│  └─ Test infrastructure   │             │                                  │
│                                                                             │
│  Phase 1: Backend Removal  │ 2-3 days    │ Remove old streaming code        │
│  ├─ api_integration.py    │             │                                  │
│  ├─ run_agent_unified.py  │             │                                  │
│  ├─ api_server.py         │             │                                  │
│  └─ execution_engine.py   │             │                                  │
│                                                                             │
│  Phase 2: Frontend Removal │ 2-3 days    │ Remove old streaming code        │
│  ├─ app.js stream handler │             │                                  │
│  ├─ render functions      │             │                                  │
│  └─ ActivityBar component │             │                                  │
│                                                                             │
│  Phase 3: New Backend      │ 2-3 days    │ Implement clean streaming        │
│  ├─ Simple WebSocket push │             │                                  │
│  ├─ No complex throttling │             │                                  │
│  └─ Minimal serialization │             │                                  │
│                                                                             │
│  Phase 4: New Frontend     │ 3-4 days    │ Implement clean rendering        │
│  ├─ Append-only messages  │             │                                  │
│  ├─ Simple state sync     │             │                                  │
│  └─ Basic activity bar    │             │                                  │
│                                                                             │
│  Phase 5: Integration      │ 2-3 days    │ End-to-end testing               │
│  ├─ Full regression test  │             │                                  │
│  ├─ Performance baseline  │             │                                  │
│  └─ Bug fixes             │             │                                  │
│                                                                             │
│  Phase 6: Refactor app.js  │ 5-7 days    │ ES6 module conversion            │
│  ├─ Extract utilities     │             │ (Can run parallel to Phases 3-5)  │
│  ├─ Split message handlers│             │                                  │
│  ├─ Module conversion     │             │                                  │
│  └─ Build tool setup      │             │                                  │
│                                                                             │
│  Phase 7: Polish & Deploy  │ 1-2 days    │ Final testing, documentation     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

TOTAL ESTIMATED EFFORT: 18-27 days (depending on parallelization)
```

---

## INITIATIVE 1: Streaming Removal

### Objective

Remove ALL existing streaming/rendering/refresh code from backend and frontend, preserving only the core functionality needed for basic operation (approvals, telemetry, state snapshots).

### Risk Assessment: MEDIUM-HIGH

**Risks:**
- Approvals system depends on streaming infrastructure
- Telemetry updates use streaming messages
- Activity banner relies on stream updates
- Sub-agent tab creation uses stream broadcasts

**Mitigation:**
- Keep approval/telemetry message types temporarily
- Create minimal replacements before removing originals
- Test each removal incrementally

---

### Phase 1.1: Backend Removal Checklist

#### File 1: `agent_cascade/api_integration.py` (63,211 bytes)

**Functions to REMOVE:**

| Line | Function | Action | Notes |
|------|----------|--------|-------|
| 524-660 | `build_stream_update_from_pool()` | DELETE | Core streaming serialization |
| 153-172 | `_put_stream_update()` | DELETE | Helper for queue operations |
| 74-92 | `_build_activity_update()` | DELETE | Activity banner updates |
| 47-52 | `_last_stream_versions` | DELETE | Version tracking cache |
| 54-58 | `_stream_token_stats_cache` | DELETE | Token stats cache |

**Functions to KEEP:**

| Line | Function | Reason |
|------|----------|--------|
| 246 | `run_agent_in_pool()` | Core execution, yields messages |
| 375 | `build_state_from_pool()` | Used for 'done' state snapshots |
| 914 | `serialize_message()` | Message serialization (used elsewhere) |
| 962 | `_serialize_instance()` | Instance serialization (used by build_state) |

**Implementation Steps:**

```python
# Step 1: Remove caching globals (lines 47-58)
_last_stream_versions = {}  # DELETE
_stream_token_stats_cache = None  # DELETE
_cached_instance_data = {}  # DELETE

# Step 2: Remove _build_activity_update() (lines 74-92)
def _build_activity_update(...):  # DELETE ENTIRE FUNCTION

# Step 3: Remove _put_stream_update() (lines 153-172)  
async def _put_stream_update(...):  # DELETE ENTIRE FUNCTION

# Step 4: Remove build_stream_update_from_pool() (lines 524-660)
def build_stream_update_from_pool(...):  # DELETE ENTIRE FUNCTION

# Step 5: Update imports in other files that reference these
```

#### File 2: `agent_cascade/run_agent_unified.py` (19,348 bytes)

**Code Blocks to REMOVE:**

| Line Range | Description | Action |
|------------|-------------|--------|
| 165-192 | Stream update building logic | DELETE |
| 194-218 | Activity update logic | DELETE |
| 94-95 | `pool._ws_send_queue` assignment | KEEP (used by new streaming) |
| 94-95 | `pool._ws_loop` assignment | KEEP (used by new streaming) |

**Current Code (to remove):**

```python
# Lines 165-192 - DELETE THIS BLOCK
should_broadcast = (
    now - last_send > 0.15  # 150ms throttle
    or len_changed           # Message count changed
    or has_tool_event        # Tool call/result
)

if should_broadcast:
    stream_update = build_stream_update_from_pool(...)
    event = {'type': 'stream_update', **stream_update}
    asyncio.run_coroutine_threadsafe(
        _put_stream_update(send_queue, event),
        loop,
    )
    last_send = now

# Lines 194-218 - DELETE THIS BLOCK  
if now_time - exec_state['_last_activity_send'] >= 0.05:
    activity_update = _build_activity_update(...)
    asyncio.run_coroutine_threadsafe(
        send_queue.put(activity_update),
        loop,
    )
    exec_state['_last_activity_send'] = now_time
```

**Function to KEEP (but modify):**

| Line | Function | Modifications |
|------|----------|---------------|
| 37 | `run_agent_thread_unified()` | Remove streaming logic, keep core execution |
| 226-242 | Final 'done' broadcast | KEEP - sends completion state |
| 282 | `get_token_stats_unified()` | KEEP - used for telemetry |

#### File 3: `agent_cascade/api_server.py` (134,045 bytes)

**Functions to REMOVE:**

| Line | Function | Action | Notes |
|------|----------|--------|-------|
| 844-860 | `_sender_loop()` | DELETE | Main WebSocket broadcast loop |
| 675-682 | `build_stream_update()` | DELETE | Legacy wrapper function |

**Functions to MODIFY:**

| Line | Function | Changes |
|------|----------|---------|
| 819 | `startup()` | Remove `_sender_loop` initialization |
| 862 | `_approval_loop()` | KEEP - approvals still needed |

**Current `_sender_loop()` (to remove):**

```python
# Lines 844-860 - DELETE ENTIRE FUNCTION
async def _sender_loop():
    """Global loop: reads from send_queue → broadcasts to all clients."""
    while True:
        try:
            data = await send_queue.get()
            if data.get('type') == 'dismissal':
                await broadcast({'type': 'state', **build_state()})
            else:
                await broadcast(data)  # Includes stream_update messages
```

**Implementation Steps:**

```python
# Step 1: Remove _sender_loop() function (lines 844-860)

# Step 2: Update startup() to not start _sender_loop
async def startup():
    global send_queue
    send_queue = asyncio.Queue()
    
    # REMOVE THIS LINE:
    # ws_sender_task = asyncio.create_task(_sender_loop())
    
    # KEEP this:
    ws_approval_task = asyncio.create_task(_approval_loop())

# Step 3: Remove build_stream_update() wrapper (lines 675-682)
def build_stream_update(...):  # DELETE - delegates to build_stream_update_from_pool()
```

#### File 4: `agent_cascade/execution_engine.py` (186,790 bytes)

**Code Blocks to REMOVE:**

| Line Range | Context | Description |
|------------|---------|-------------|
| 2737-2751 | `_create_and_run_agent()` | Sub-agent stream broadcast |
| 2824-2840 | `_create_and_run_agent()` | System agent stream broadcast |
| 2894-2908 | `_handle_compress_command()` | Compression complete notification |
| 3040-3053 | `_create_system_agent()` | System agent creation broadcast |

**Pattern to Remove (appears in 4 locations):**

```python
# DELETE THIS PATTERN FROM ALL 4 LOCATIONS
ws_queue = getattr(self.pool, '_ws_send_queue', None)
ws_loop = getattr(self.pool, '_ws_loop', None)
if ws_queue and ws_loop and not ws_loop.is_closed():
    from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
    su = build_stream_update_from_pool(
        pool=self.pool,
        instance_name=caller,
        responses=None,
    )
    if su is not None:
        asyncio.run_coroutine_threadsafe(
            _put_stream_update(ws_queue, {'type': 'stream_update', **su}),
            ws_loop,
        )
```

**Implementation Steps:**

For each of the 4 locations, remove the 15-line block that imports and calls `build_stream_update_from_pool()`.

---

### Phase 1.2: Frontend Removal Checklist

#### File: `web_ui/app.js` (177,051 bytes)

**Section 1: State Properties to REMOVE**

| Line Range | Property | Purpose | Action |
|------------|----------|---------|--------|
| 68-78 | `lastGenStatsUpdate` | Throttle timestamp | DELETE |
| 68-78 | `lastSubAgentRender` | Throttle timestamp | DELETE |
| 68-78 | `lastContextBarUpdate` | Throttle timestamp | DELETE |
| 68-78 | `lastUiUpdate` | Throttle timestamp | DELETE |
| 68-78 | `lastControlsUpdate` | Throttle timestamp | DELETE |
| 68-78 | `lastTelemetryUpdate` | Throttle timestamp | DELETE |
| 68-78 | `subContextBarThrottle` | Per-agent throttle | DELETE |
| 68-78 | `_debugStreamCount` | Debug counter | DELETE |

**Code to Remove:**

```javascript
// Lines 68-78 - REMOVE THESE PROPERTIES FROM state object
state = {
    // ... keep other properties ...
    
    // DELETE THESE:
    lastGenStatsUpdate: 0,
    lastSubAgentRender: 0,
    lastContextBarUpdate: 0,
    lastUiUpdate: 0,
    lastControlsUpdate: 0,
    lastTelemetryUpdate: 0,
    subContextBarThrottle: {},
    _debugStreamCount: 0,
}
```

**Section 2: Functions to REMOVE**

| Line | Function | Lines | Action |
|------|----------|-------|--------|
| 949-1445 | `handleServerMessage()` | ~496 | MODIFY - remove stream_update case |
| 1121-1353 | `case 'stream_update':` | ~232 | DELETE ENTIRE CASE |
| 1237-1242 | Activity bar feed | ~6 | DELETE (part of stream_update) |
| 1283-1341 | Render throttling logic | ~58 | DELETE (part of stream_update) |
| 2431-2570 | `renderSubAgents()` | ~140 | DELETE |
| 2571-2750 | `renderSubAgentPanel()` | ~180 | DELETE |
| 1559-1713 | `createMessageEl()` | ~155 | KEEP - used by new streaming |
| 1714-1737 | `appendStreamingDelta()` | ~24 | DELETE |
| 1738-1829 | `updateBubbleContent()` | ~92 | DELETE |
| 1830-2022 | `renderMarkdown()` | ~193 | KEEP - used by new streaming |
| 130-345 | `ActivityBar` component | ~215 | DELETE (will recreate simplified) |

**Section 3: ActivityBar Component Removal**

The ActivityBar is already modular (object literal pattern). Remove entire block:

```javascript
// Lines 172-345 - DELETE ENTIRE BLOCK
const ActivityBar = {
    el: null,
    fifoEl: null,
    _lastImmediateKey: '',
    _immediateLocked: false,
    lastRenderTime: 0,
    
    init() { ... },           // ~15 lines
    push() { ... },            // ~25 lines
    pushImmediate() { ... },   // ~35 lines
    render() { ... }           // ~80 lines
};
```

**Section 4: handleServerMessage() Modification**

The `handleServerMessage()` function (lines 949-1445) contains a large switch statement. **Remove only the `stream_update` case:**

```javascript
// Lines 1121-1353 - DELETE THIS ENTIRE CASE BLOCK
case 'stream_update':
    state._debugStreamCount++;
    // ... 230+ lines of streaming logic ...
    break;

// KEEP these cases:
case 'state':      // Initial state snapshot
case 'done':       // Completion state snapshot  
case 'error':      // Error messages
case 'approvals':  // Approval requests (depends on streaming infra)
case 'activity_update':  // Activity banner (will be replaced)
case 'dismissal':  // Agent dismissal
case 'security_response':  // Security advisor
```

---

### Phase 1.3: What Depends on Streaming (Preserve Temporarily)

| Feature | Dependency | Temporary Solution |
|---------|-----------|-------------------|
| **Approvals** | Uses `stream_update.approvals` | Keep `approvals` message type separate |
| **Telemetry** | Uses `stream_update.telemetry` | Send periodic telemetry updates via new message type |
| **Activity Banner** | Uses `activity_update` + `stream_update` | Simplify to basic status indicator |
| **Sub-agent tabs** | Created via `stream_update.agent_instances` | Create tabs via `done` state snapshot |

---

## INITIATIVE 2: New Streaming Implementation

### Design Principles

1. **Simplicity over optimization** - Get it working first, optimize later
2. **Append-only model** - Messages grow, rarely replaced
3. **Single throttle layer** - One 100ms interval, no complex logic
4. **Minimal serialization** - Send only what changed
5. **WebSocket push** - Server pushes updates, client renders

### Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│ BACKEND (Simplified)                                    │
├─────────────────────────────────────────────────────────┤
│ Agent Thread                                             │
│   ↓                                                      │
│ ExecutionEngine.run() yields _StreamState               │
│   ↓                                                      │
│ Simple serializer: {type, instance_name, delta}         │
│   ↓                                                      │
│ send_queue.put({type: 'message_delta', ...})            │
│   ↓                                                      │
│ _sender_loop → broadcast to WebSocket clients           │
└─────────────────────────────────────────────────────────┘
                    │
                    │ WebSocket JSON
                    ↓
┌─────────────────────────────────────────────────────────┐
│ FRONTEND (Simplified)                                   │
├─────────────────────────────────────────────────────────┤
│ WebSocket.onmessage                                      │
│   ↓                                                      │
│ handleMessage({type: 'message_delta', ...})             │
│   ↓                                                      │
│ Find/append to existing bubble OR create new            │
│   ↓                                                      │
│ Simple 100ms render throttle                             │
│   ↓                                                      │
│ DOM update                                               │
└─────────────────────────────────────────────────────────┘
```

---

### Phase 2.1: New Backend Implementation

#### File: `agent_cascade/api_integration.py` - Add New Functions

**Add after line 962 (after `_serialize_instance()`):**

```python
# ============================================================================
# NEW STREAMING IMPLEMENTATION (Simple, Minimal)
# ============================================================================

def _build_message_delta(instance_name: str, new_messages: List[Message]) -> dict:
    """
    Build a minimal delta update for new messages in an instance.
    
    Args:
        instance_name: Name of the agent instance
        new_messages: List of new/partial messages to serialize
        
    Returns:
        Dict with: instance_name, messages (serialized), message_count
    """
    serialized_messages = [serialize_message(msg) for msg in new_messages]
    
    return {
        'instance_name': instance_name,
        'messages': serialized_messages,
        'message_count': len(serialized_messages),
        'timestamp': time.time(),
    }


def _put_message_delta(send_queue: asyncio.Queue, delta: dict) -> futures.Future:
    """
    Safely put a message_delta event onto the WebSocket send queue.
    
    Args:
        send_queue: asyncio.Queue for WebSocket messages
        delta: Message delta dict from _build_message_delta()
        
    Returns:
        asyncio Future for the put operation
    """
    event = {'type': 'message_delta', **delta}
    
    def _put():
        # Non-blocking put - drop if queue full
        try:
            send_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("WebSocket send queue full, dropping message_delta")
    
    loop = getattr(send_queue, '_loop', None)
    if loop and not loop.is_closed():
        return asyncio.run_coroutine_threadsafe(_put(), loop)
    else:
        # Fallback: direct put (shouldn't happen in normal operation)
        _put()
        return None
```

#### File: `agent_cascade/run_agent_unified.py` - Modify Streaming Logic

**Replace lines 165-192 (old throttling logic) with:**

```python
# NEW SIMPLE STREAMING LOGIC (replaces complex throttling)
_last_stream_send = 0

for partial_response in responses:
    # ... existing processing ...
    
    # Simple 100ms throttle - send every 100ms or on significant change
    now = time.time()
    is_significant_change = (
        len(partial_response) > len(_last_partial_response) or
        any(hasattr(msg, 'tool_call') for msg in partial_response)
    )
    
    if (now - _last_stream_send > 0.1) or is_significant_change:
        # Build minimal delta with only changed messages
        new_messages = [
            msg for msg in partial_response 
            if msg not in _last_partial_response
        ]
        
        if new_messages:
            delta = _build_message_delta(instance_name, new_messages)
            _put_message_delta(send_queue, delta)
            _last_stream_send = now
    
    _last_partial_response = partial_response.copy()
```

#### File: `agent_cascade/api_server.py` - Modify `_sender_loop()`

**Replace the old `_sender_loop()` (lines 844-860) with simplified version:**

```python
async def _sender_loop():
    """
    Simplified sender loop: reads from send_queue → broadcasts to clients.
    
    Message types handled:
    - message_delta: New streaming message updates
    - state: Full state snapshots
    - done: Completion notifications
    - approvals: Approval requests
    - error: Error messages
    """
    while True:
        try:
            data = await send_queue.get()
            
            # Broadcast to all connected WebSocket clients
            await broadcast(data)
            
        except Exception as e:
            logger.error(f"Sender loop error: {e}")
            await asyncio.sleep(0.1)  # Backoff on error
```

---

### Phase 2.2: New Frontend Implementation

#### File: `web_ui/app.js` - Add New Message Handler

**Add new case in `handleServerMessage()` (after removing old `stream_update` case):**

```javascript
// NEW SIMPLE MESSAGE DELTA HANDLER
case 'message_delta':
    handleMessageDelta(data);
    break;
```

**Add new function after line 1445 (after `handleServerMessage()`):**

```javascript
/**
 * Handle incoming message_delta from server.
 * 
 * Simplified streaming: append-only, minimal throttling.
 * 
 * @param {Object} data - Server message with:
 *   - instance_name: Agent instance name
 *   - messages: Array of serialized messages (new or updated)
 *   - message_count: Total message count for instance
 *   - timestamp: Server timestamp
 */
function handleMessageDelta(data) {
    const instanceName = data.instance_name;
    const newMessages = data.messages;
    
    if (!instanceName || !newMessages || newMessages.length === 0) {
        return;  // Skip empty updates
    }
    
    // Initialize agent in state if not exists
    if (!state.subAgents[instanceName]) {
        state.subAgents[instanceName] = {
            name: instanceName,
            messages: [],
            is_root: instanceName === 'root',
        };
    }
    
    const agentState = state.subAgents[instanceName];
    
    // Append new messages to agent state
    for (const msg of newMessages) {
        // Check if message already exists (by ID or content hash)
        const existingIndex = agentState.messages.findIndex(
            m => m.id === msg.id || 
                 (m.content === msg.content && m.role === msg.role)
        );
        
        if (existingIndex >= 0) {
            // Update existing message (streaming growth)
            agentState.messages[existingIndex] = {
                ...agentState.messages[existingIndex],
                ...msg,
            };
        } else {
            // Add new message
            agentState.messages.push(msg);
        }
    }
    
    // Trigger render with simple throttle
    scheduleRender(instanceName);
}


/**
 * Simple render scheduler with 100ms throttle.
 * 
 * @param {string} instanceName - Agent instance to render
 */
let _renderSchedule = {};

function scheduleRender(instanceName) {
    const now = Date.now();
    const LAST_RENDER_TIME = 100;  // 100ms throttle
    
    // Skip if rendered recently
    if (_renderSchedule[instanceName] && 
        (now - _renderSchedule[instanceName] < LAST_RENDER_TIME)) {
        return;
    }
    
    _renderSchedule[instanceName] = now;
    
    // Render immediately (no debouncing for simplicity)
    renderAgentPanel(instanceName);
}


/**
 * Render a single agent's panel (simplified version).
 * 
 * @param {string} instanceName - Agent instance to render
 */
function renderAgentPanel(instanceName) {
    const agentState = state.subAgents[instanceName];
    if (!agentState) return;
    
    const panel = document.getElementById(`agent-panel-${instanceName}`);
    if (!panel) return;
    
    const messagesContainer = panel.querySelector('.messages');
    if (!messagesContainer) return;
    
    // TODO: Implement simplified message rendering
    // - Create bubbles for new messages
    // - Append content to existing bubbles
    // - Scroll to bottom if visible
}
```

#### New Simplified Activity Bar

**Replace deleted ActivityBar with minimal version:**

```javascript
/**
 * Minimal activity bar - just shows current agent status.
 * No complex throttling, no FIFO queue.
 */
const SimpleActivityBar = {
    el: null,
    
    init() {
        this.el = document.getElementById('globalActivityBar');
        if (this.el) {
            this.update();
        }
    },
    
    update() {
        if (!this.el) return;
        
        const activeAgent = state.activeStack?.[state.activeStack.length - 1] || 'root';
        const isGenerating = state.generating;
        
        let statusText = `🔸 ${activeAgent}`;
        if (isGenerating) {
            statusText += ' ⏳ streaming...';
        }
        
        this.el.textContent = statusText;
    },
};
```

---

### Phase 2.3: WebSocket Message Protocol Changes

#### Old Message Types (Server → Client)

| Type | Status | Replacement |
|------|--------|-------------|
| `stream_update` | REMOVE | `message_delta` + `state` snapshots |
| `activity_update` | REMOVE | Inline in `message_delta` or remove |
| `state` | KEEP | Initial state snapshot |
| `done` | KEEP | Completion notification |
| `approvals` | KEEP | Approval requests |
| `error` | KEEP | Error messages |
| `dismissal` | KEEP | Agent dismissal |
| `security_response` | KEEP | Security advisor |

#### New Message Types (Server → Client)

| Type | Purpose | Format |
|------|---------|--------|
| `message_delta` | Streaming updates | `{type, instance_name, messages[], message_count, timestamp}` |
| `state` | Full snapshot | Existing format |
| `done` | Completion | Existing format |
| `approvals` | Approvals | Existing format |

---

## INITIATIVE 3: Frontend Refactoring

### Overview

Refactor `app.js` (4,180 lines) into ~13 ES6 modules following the natural boundaries identified in the analysis.

### Target File Structure

```
web_ui/
├── index.html              # Entry HTML (modified to load bundled JS)
├── styles.css              # All styles (unchanged)
├── package.json            # NEW - npm project config
├── vite.config.js          # NEW - Vite build config
├── app.js                  # EXISTING - will become orchestration only
│
└── src/                    # NEW - ES6 modules
    ├── index.js            # Main entry point
    │
    ├── state/
    │   ├── index.js        # state object + getters/setters
    │   └── agents.js       # Agent identity functions
    │
    ├── websocket/
    │   ├── connection.js   # WebSocket setup/reconnect
    │   ├── handlers.js     # Main message dispatcher
    │   └── handlers/
    │       ├── state.js    # 'state'/'done' handlers
    │       ├── delta.js    # 'message_delta' handlers (NEW)
    │       └── approvals.js # 'approvals' handlers
    │
    ├── render/
    │   ├── index.js        # Render orchestrator
    │   ├── messages.js     # createMessageEl(), message bubbles
    │   ├── markdown.js     # renderMarkdown(), tool rendering
    │   └── agents.js       # Agent panel/tab rendering
    │
    ├── ui/
    │   ├── activity-bar.js # SimpleActivityBar component
    │   ├── controls.js     # Buttons, generation stats
    │   ├── approvals.js    # Approval bar UI
    │   ├── telemetry.js    # Telemetry panel + API router
    │   └── tabs.js         # Tab switching
    │
    ├── settings/
    │   ├── persistence.js  # localStorage save/load
    │   └── config.js       # getGenerateCfg()
    │
    ├── actions/
    │   ├── messages.js     # sendMessage, retry, continue
    │   └── editing.js      # Message editing
    │
    ├── modules/
    │   ├── afk.js          # AFK auto-reply
    │   └── sessions.js     # Session CRUD
    │
    └── utils/
        ├── dom.js          # $ helper, DOM helpers
        ├── formatting.js   # escapeHtml, formatTokenCount
        ├── tokens.js       # estimateTokens()
        └── audio.js        # playSound()
```

---

### Phase 3.1: Build Tool Setup (Vite)

#### Step 1: Create package.json

**File: `web_ui/package.json`**

```json
{
  "name": "agentcascade-web-ui",
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "devDependencies": {
    "vite": "^5.0.0"
  },
  "dependencies": {
    "marked": "^11.0.0",
    "dompurify": "^3.0.0",
    "highlight.js": "^11.9.0"
  }
}
```

#### Step 2: Create Vite Config

**File: `web_ui/vite.config.js`**

```javascript
import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  build: {
    outDir: 'dist',
    rollupOptions: {
      input: {
        main: 'index.html',
      },
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/ws': {
        target: 'http://localhost:8000',
        ws: true,
      },
      '/api': {
        target: 'http://localhost:8000',
      },
    },
  },
});
```

#### Step 3: Update index.html

**File: `web_ui/index.html`** - Modify script loading:

```html
<!-- OLD -->
<script src="app.js"></script>

<!-- NEW (for development with Vite) -->
<script type="module" src="/src/index.js"></script>
```

---

### Phase 3.2: Module Extraction Order

#### Priority 1: Utilities (Lowest Risk)

**Extract to `src/utils/`:**

1. **`formatting.js`** - Pure functions, no dependencies
   ```javascript
   export function escapeHtml(text) { ... }
   export function formatTokenCount(count) { ... }
   export function formatDate(timestamp) { ... }
   export function formatSize(bytes) { ... }
   ```

2. **`tokens.js`** - Token estimation
   ```javascript
   export function estimateTokens(text) { ... }
   ```

3. **`audio.js`** - Sound playback
   ```javascript
   import { state } from '../state/index.js';
   export function playSound(type) { ... }
   ```

4. **`dom.js`** - DOM helpers
   ```javascript
   export const $ = (selector) => document.querySelector(selector);
   export function autoResize(textarea, options = {}) { ... }
   ```

#### Priority 2: ActivityBar (Already Modular)

**Extract to `src/ui/activity-bar.js`:**

```javascript
// Minimal version as designed in Initiative 2
export const SimpleActivityBar = {
    el: null,
    
    init() { ... },
    update() { ... },
};
```

#### Priority 3: Settings Persistence

**Extract to `src/settings/`:**

1. **`persistence.js`**:
   ```javascript
   import { getGenerateCfg } from './config.js';
   import { send } from '../websocket/connection.js';
   
   export function saveSettings() { ... }
   export function loadSettings() { ... }
   ```

2. **`config.js`**:
   ```javascript
   export function getGenerateCfg() { ... }
   export function validateConfig(cfg) { ... }
   ```

#### Priority 4: WebSocket Handlers (Split by Type)

**Extract to `src/websocket/handlers/`:**

1. **`state.js`** - Handle 'state' and 'done' messages (~180 lines):
   ```javascript
   import { state } from '../../state/index.js';
   import { renderSubAgents } from '../../render/index.js';
   
   export function handleStateMessage(data) { ... }
   export function handleDoneMessage(data) { ... }
   ```

2. **`delta.js`** - Handle 'message_delta' (~100 lines):
   ```javascript
   import { state } from '../../state/index.js';
   import { renderAgentPanel } from '../../render/agents.js';
   
   export function handleMessageDelta(data) { ... }
   ```

3. **`approvals.js`** - Handle 'approvals' (~50 lines):
   ```javascript
   import { renderApprovals } from '../../ui/approvals.js';
   
   export function handleApprovals(data) { ... }
   ```

#### Priority 5: Rendering Engine

**Extract to `src/render/`:**

1. **`messages.js`** - Message bubble creation (~300 lines):
   ```javascript
   import { escapeHtml } from '../utils/formatting.js';
   import { renderMarkdown } from './markdown.js';
   
   export function createMessageEl(msg, options = {}) { ... }
   export function appendToBubble(bubble, content) { ... }
   ```

2. **`markdown.js`** - Markdown/tool rendering (~200 lines):
   ```javascript
   import marked from 'marked';
   import DOMPurify from 'dompurify';
   import hljs from 'highlight.js';
   
   export function renderMarkdown(text) { ... }
   export function renderToolCall(tool) { ... }
   export function renderToolResult(result) { ... }
   ```

3. **`agents.js`** - Agent panel rendering (~200 lines):
   ```javascript
   import { createMessageEl } from './messages.js';
   
   export function renderAgentPanel(instanceName) { ... }
   export function renderAllAgentPanels() { ... }
   ```

#### Priority 6: State Management

**Extract to `src/state/`:**

1. **`index.js`** - Central state object:
   ```javascript
   export const state = {
       subAgents: {},
       activeStack: [],
       generating: false,
       // ... other properties
   };
   
   export function getState() { return state; }
   export function setState(updates) { Object.assign(state, updates); }
   ```

2. **`agents.js`** - Agent-specific functions:
   ```javascript
   import { state } from './index.js';
   
   export function getActiveAgentName() { ... }
   export function getAgentTabId(name) { ... }
   export function isSessionPrimaryAgent(name) { ... }
   ```

---

### Phase 3.3: Entry Point Orchestration

**File: `web_ui/src/index.js`**

```javascript
// Main entry point - orchestrates all modules

// State
import { state } from './state/index.js';

// WebSocket
import { connect, send } from './websocket/connection.js';
import { setupMessageHandlers } from './websocket/handlers.js';

// UI Components
import { SimpleActivityBar } from './ui/activity-bar.js';
import { initControls } from './ui/controls.js';
import { initApprovals } from './ui/approvals.js';
import { initTelemetry } from './ui/telemetry.js';
import { initTabs } from './ui/tabs.js';

// Settings
import { loadSettings } from './settings/persistence.js';

// Actions
import { setupMessageActions } from './actions/messages.js';
import { setupEditing } from './actions/editing.js';

// Modules
import { initAfk } from './modules/afk.js';
import { initSessions } from './modules/sessions.js';

/**
 * Application initialization
 */
function init() {
    console.log('AgentCascade UI initializing...');
    
    // Load settings first
    loadSettings();
    
    // Initialize UI components
    SimpleActivityBar.init();
    initControls();
    initApprovals();
    initTelemetry();
    initTabs();
    
    // Setup WebSocket
    setupMessageHandlers();
    connect();
    
    // Setup user actions
    setupMessageActions();
    setupEditing();
    
    // Initialize modules
    initAfk();
    initSessions();
    
    console.log('AgentCascade UI ready');
}

// Start when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

// Export for global access (if needed)
window.app = { state, send };
```

---

## Testing Strategy

### Test Pyramid

```
         ┌──────────────┐
         │  E2E Tests   │  ~10 tests (critical paths)
         │  (Cypress)   │
         ├──────────────┤
    ┌────┴──────────────┴────┐
    │   Integration Tests    │  ~20 tests (module interactions)
    │      (Jest)            │
    ├────────────────────────┤
    │   Unit Tests           │  ~100 tests (pure functions)
    │      (Jest)            │
    └────────────────────────┘
```

### Phase-by-Phase Testing

#### Phase 1: Backend Removal

**Tests:**
1. ✅ Agent execution completes without streaming code
2. ✅ State snapshots ('done' messages) still work
3. ✅ Approvals still function
4. ✅ WebSocket connection stable

**Test Script:**
```python
# tests/test_backend_removal.py
def test_agent_execution_without_streaming():
    """Agent can run and complete without build_stream_update_from_pool"""
    pool = AgentPool()
    result = run_agent_in_pool(pool, "test", "Hello")
    assert result.is_complete
    assert len(result.messages) > 0

def test_state_snapshot_still_works():
    """build_state_from_pool still produces valid state"""
    pool = AgentPool()
    # ... setup ...
    state = build_state_from_pool(pool)
    assert 'instances' in state
    assert 'active_stack' in state
```

#### Phase 2: Frontend Removal

**Tests:**
1. ✅ Page loads without streaming code
2. ✅ Initial state renders correctly
3. ✅ 'done' messages update UI
4. ✅ No console errors from missing functions

**Test Script:**
```javascript
// tests/frontend-removal.test.js
test('page loads without stream_update handler', async () => {
    await page.goto('http://localhost:3000');
    const errors = await page.evaluate(() => 
        window.consoleErrors || []
    );
    expect(errors).toHaveLength(0);
});

test('initial state renders correctly', async () => {
    await page.goto('http://localhost:3000');
    const rootPanel = await page.$('#agent-panel-root');
    expect(rootPanel).not.toBeNull();
});
```

#### Phase 3: New Backend Streaming

**Tests:**
1. ✅ message_delta messages sent correctly
2. ✅ Serialization includes all required fields
3. ✅ Queue doesn't overflow under load
4. ✅ 100ms throttle works as expected

**Test Script:**
```python
# tests/test_new_streaming.py
def test_message_delta_format():
    """Delta messages have correct structure"""
    delta = _build_message_delta('test', [msg])
    assert 'instance_name' in delta
    assert 'messages' in delta
    assert 'message_count' in delta
    assert 'timestamp' in delta

def test_streaming_throttle():
    """Messages throttled to ~10 per second"""
    # ... simulate rapid updates ...
    sent_messages = capture_sent_messages()
    assert len(sent_messages) <= 12  # Allow some variance
```

#### Phase 4: New Frontend Streaming

**Tests:**
1. ✅ message_delta handler appends messages correctly
2. ✅ Existing bubbles updated during streaming
3. ✅ New bubbles created for new messages
4. ✅ 100ms throttle prevents excessive renders

**Test Script:**
```javascript
// tests/new-streaming.test.js
test('message_delta appends to existing bubble', async () => {
    // Setup: create initial message
    handleMessageDelta({
        instance_name: 'root',
        messages: [{id: '1', content: 'Hello', role: 'assistant'}]
    });
    
    // Simulate streaming update
    handleMessageDelta({
        instance_name: 'root',
        messages: [{id: '1', content: 'Hello world', role: 'assistant'}]
    });
    
    // Verify: bubble content updated
    const bubble = document.querySelector('.message-bubble');
    expect(bubble.textContent).toBe('Hello world');
});

test('new message creates new bubble', async () => {
    // Setup: initial state
    handleMessageDelta({
        instance_name: 'root',
        messages: [{id: '1', content: 'First', role: 'assistant'}]
    });
    
    // New message
    handleMessageDelta({
        instance_name: 'root',
        messages: [{id: '2', content: 'Second', role: 'assistant'}]
    });
    
    // Verify: two bubbles exist
    const bubbles = document.querySelectorAll('.message-bubble');
    expect(bubbles.length).toBe(2);
});
```

#### Phase 5: Integration Testing

**Full Regression Test Checklist:**

- [ ] **Basic Chat**: Send message → receive response
- [ ] **Streaming**: Response appears incrementally
- [ ] **Tab Switching**: Switch tabs mid-stream, switch back
- [ ] **Sub-agents**: Create sub-agent, view its messages
- [ ] **Approvals**: Trigger approval, approve/reject
- [ ] **Settings**: Change setting, persists across reload
- [ ] **Sessions**: Save session, load session
- [ ] **File Upload**: Upload image/doc, appears in conversation
- [ ] **Message Edit**: Edit message, changes persist
- [ ] **Telemetry**: Stats update correctly

#### Phase 6: Module Refactoring Tests

**Unit Tests for Extracted Modules:**

```javascript
// tests/utils/formatting.test.js
import { escapeHtml, formatTokenCount } from '../../src/utils/formatting.js';

test('escapeHtml escapes special characters', () => {
    expect(escapeHtml('<script>')).toBe('&lt;script&gt;');
    expect(escapeHtml('&amp;')).toBe('&amp;amp;');
});

test('formatTokenCount formats large numbers', () => {
    expect(formatTokenCount(1234)).toBe('1.2k');
    expect(formatTokenCount(12345)).toBe('12.3k');
});

// tests/state/agents.test.js
import { getActiveAgentName, getAgentTabId } from '../../src/state/agents.js';

test('getActiveAgentName returns last in stack', () => {
    state.activeStack = ['root', 'coder', 'researcher'];
    expect(getActiveAgentName()).toBe('researcher');
});

test('getAgentTabId generates correct ID', () => {
    expect(getAgentTabId('root')).toBe('agent-panel-root');
    expect(getAgentTabId('test-agent')).toBe('agent-panel-test-agent');
});
```

---

## Rollback Plan

### Strategy: Branch-Based with Checkpoints

```
main (stable)
  │
  ├─ feature/streaming-rewrite
  │     │
  │     ├─ checkpoint/phase1-complete
  │     ├─ checkpoint/phase2-complete
  │     ├─ checkpoint/phase3-complete
  │     └─ checkpoint/phase4-complete
  │
  └─ backup/original-working-state
```

### Rollback Procedures

#### Level 1: Single File Rollback

**If a single file change breaks things:**

```bash
# Revert last commit to specific file
git checkout HEAD~1 -- agent_cascade/api_integration.py

# Or restore from backup
cp backups/api_integration.py.backup agent_cascade/api_integration.py
```

#### Level 2: Phase Rollback

**If a phase introduces critical bugs:**

```bash
# Checkout checkpoint branch
git checkout checkpoint/phase1-complete

# Or reset to checkpoint
git reset --hard checkpoint/phase1-complete
```

#### Level 3: Full Rollback

**If entire rewrite needs to be abandoned:**

```bash
# Create merge back to main
git checkout main
git merge backup/original-working-state

# Deploy known-good version
# Update deployment config to point to main branch
```

### Backup Checklist

Before each phase, create backups:

- [ ] `backups/phase0/app.js`
- [ ] `backups/phase0/api_integration.py`
- [ ] `backups/phase0/run_agent_unified.py`
- [ ] `backups/phase0/api_server.py`
- [ ] `backups/phase0/execution_engine.py`

**Backup Script:**

```bash
#!/bin/bash
# create_backup.sh

PHASE=$1
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p backups/${PHASE}_${TIMESTAMP}

cp web_ui/app.js backups/${PHASE}_${TIMESTAMP}/
cp agent_cascade/api_integration.py backups/${PHASE}_${TIMESTAMP}/
cp agent_cascade/run_agent_unified.py backups/${PHASE}_${TIMESTAMP}/
cp agent_cascade/api_server.py backups/${PHASE}_${TIMESTAMP}/
cp agent_cascade/execution_engine.py backups/${PHASE}_${TIMESTAMP}/

echo "Backup created: backups/${PHASE}_${TIMESTAMP}"
```

---

## Effort Estimates

### Detailed Breakdown

| Phase | Tasks | Estimated Effort | Risk |
|-------|-------|------------------|------|
| **Phase 0: Prep** | Branch, backup, test setup | 1-2 days | Low |
| **Phase 1: Backend Removal** | Remove streaming from 4 Python files | 2-3 days | Medium |
| **Phase 2: Frontend Removal** | Remove streaming from app.js | 2-3 days | Medium |
| **Phase 3: New Backend** | Implement simple streaming | 2-3 days | Medium |
| **Phase 4: New Frontend** | Implement simple rendering | 3-4 days | Medium-High |
| **Phase 5: Integration** | E2E testing, bug fixes | 2-3 days | Low |
| **Phase 6: Refactor** | ES6 module conversion | 5-7 days | Medium |
| **Phase 7: Polish** | Final testing, docs | 1-2 days | Low |

### Total Effort

- **Sequential**: ~18-27 days
- **Parallel (Phases 3-6 overlap)**: ~14-20 days

### Risk Matrix

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Approvals break during removal | Medium | High | Keep approvals message type separate |
| Telemetry stops updating | Low | Low | Accept temporary loss, add back later |
| New streaming too slow | Medium | Medium | Profile and optimize after MVP |
| Module extraction breaks imports | High | Low | Incremental extraction, test each module |
| Vite build issues | Medium | Low | Use native ES6 modules as fallback |

---

## Appendix A: Quick Reference - Files to Modify

### Backend Files

| File | Lines to Remove | Lines to Add | Net Change |
|------|-----------------|--------------|------------|
| `api_integration.py` | ~200 (stream functions) | ~50 (new delta functions) | -150 |
| `run_agent_unified.py` | ~80 (throttling logic) | ~40 (simple throttle) | -40 |
| `api_server.py` | ~30 (_sender_loop) | ~20 (simplified loop) | -10 |
| `execution_engine.py` | ~60 (4 broadcast blocks) | 0 | -60 |

### Frontend Files

| File | Lines to Remove | Lines to Add | Net Change |
|------|-----------------|--------------|------------|
| `app.js` | ~700 (streaming code) | ~150 (new handlers) | -550 |
| `app.js` | - | +2000 (extracted to modules) | Refactored |
| `src/**/*.js` | New files | ~4000 (total module lines) | +4000 |

---

## Appendix B: Glossary

| Term | Definition |
|------|------------|
| **stream_update** | Old WebSocket message type for streaming updates |
| **message_delta** | New WebSocket message type for streaming updates |
| **AgentPool** | Backend container for all agent instances |
| **AgentInstance** | Single agent with conversation history |
| **_StreamState** | Generator yield type during agent execution |
| **send_queue** | asyncio.Queue for WebSocket messages |
| **ActivityBar** | UI component showing current agent activity |
| **ES6 Modules** | Modern JavaScript module system (import/export) |
| **Vite** | Build tool for ES6 modules |

---

## Appendix C: Decision Log

### Decisions Made

1. **Use Vite over Webpack**: Simpler config, faster HMR
2. **Keep single state object**: Easier migration than introducing Redux
3. **Append-only streaming model**: Simpler than diff-based initially
4. **100ms throttle**: Balance between smoothness and performance
5. **Minimal ActivityBar**: Just status indicator, no complex FIFO queue

### Decisions Pending

1. **TypeScript adoption**: Defer to future phase
2. **State management library**: Keep custom for now
3. **Component framework**: Not needed yet (vanilla JS sufficient)

---

## Appendix D: Success Criteria

### Initiative 1 & 2 (Streaming Rewrite)

- [ ] Old streaming code removed from all 4 backend files
- [ ] Old streaming code removed from app.js
- [ ] New message_delta protocol working end-to-end
- [ ] Streaming renders at ~10fps (100ms throttle)
- [ ] No console errors in browser
- [ ] All regression tests pass

### Initiative 3 (Frontend Refactor)

- [ ] app.js reduced to ~500 lines (orchestration only)
- [ ] All 13 modules extracted to src/
- [ ] Vite dev server working (`npm run dev`)
- [ ] Vite production build working (`npm run build`)
- [ ] Unit tests passing for utility functions
- [ ] No global scope pollution (except state, ws)

---

*Plan created by PlanCreator (Coder Agent)*  
*For questions or clarifications, supervisor should reference specific sections*  
*Last updated: 2026-06-13*