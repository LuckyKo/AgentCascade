# Sub-Agent Tab Refresh/Render Flow — AgentCascade Main Branch

## Complete Architecture Overview

This document traces the full lifecycle of sub-agent tabs from spawn → real-time streaming → completion/termination across both backend and frontend.

---

## 1. Sub-Agent Spawning & Tab Creation

### Backend: Orchestrator calls `_stream_sub_agent_call`
**File:** `agent_orchestrator.py` (lines 1985–2346)

When the orchestrator agent decides to call a sub-agent, it invokes `_stream_sub_agent_call`:

```
Line 2000: conv = self.agent_pool.get_conversation(instance_name)
Line 2002: conv.append(user_msg)                              # Add task message
Line 2005: logger_inst.log_message(user_msg)                  # Persist to JSONL
Line 2008: self.agent_pool.active_stack.append(instance_name)  # Push onto active stack
Line 2018-2024: Initialize streaming state dict and store in agent_pool.sub_agent_state
```

**Key initialization (lines 2017–2028):**
```python
state = {
    'active': True,
    'agent_name': f"{instance_name} ({agent_class})",
    'messages': copy.deepcopy(conv),
}
self.agent_pool.sub_agent_state[instance_name] = state

# Force an immediate yield so the WebUI detects the new active_stack entry
yield current_response
```

This initial `yield` triggers the first streaming update to the frontend, which then creates the tab.

### Frontend: Tab creation in `renderSubAgents()`
**File:** `web_ui/app.js` (lines 2123–2230)

When `state.subAgents` changes (populated from `data.sub_agents` in the WebSocket handler at line 742), `renderSubAgents()` is called:

```javascript
// Lines 2128-2135: Remove stale tabs for agents no longer in state
mainTabBar.querySelectorAll('.main-tab[data-tab^="sub-"]').forEach(tab => {
    const agentName = tab.dataset.tab.substring(4);
    if (!names.includes(agentName)) {
        tab.remove();
        document.getElementById('panelSub-' + agentName)?.remove();
    }
});

// Lines 2147-2173: Create new tab button (only if doesn't exist)
let tabBtn = mainTabBar.querySelector(`.main-tab[data-tab="${tabId}"]`);
if (!tabBtn) {
    tabBtn = document.createElement('button');
    tabBtn.className = 'main-tab';
    tabBtn.dataset.tab = tabId;  // e.g., "sub-worker1"
    // ... adds icon, label, close button ...
    mainTabBar.appendChild(tabBtn);
}

// Lines 2200-2216: Create new panel (only if doesn't exist)
let panel = document.getElementById('panelSub-' + name);
if (!panel) {
    panel = document.createElement('div');
    panel.className = 'main-tab-panel sub-agent-panel';
    panel.id = 'panelSub-' + name;
    // ... adds context bar, scroll container, activity bar ...
    mainTabPanels.appendChild(panel);
}

// Line 2219: Render messages into the panel
renderSubAgentPanel(panel, sa[name], name);
```

### Frontend: Tab switching via `switchMainTab()`
**File:** `web_ui/app.js` (lines 2525–2560)

```javascript
function switchMainTab(tabId) {
    // Toggle active class on tab buttons and panels
    // If 'chat': show main chat panel, reset lastRenderedCount for full sync
    // If 'sub-*': show sub-agent panel, call renderSubAgents() to refresh content
    state.activeSubTab = tabId;  // Tracks which tab is visible
    
    if (tabId === 'chat') {
        lastRenderedCount = Infinity;  // Force full re-render on return
        renderMessages();
    } else {
        renderSubAgents();  // Re-renders the sub-agent panel content
    }
}
```

---

## 2. Real-Time Streaming: Backend → Frontend Data Flow

### The Two-Layer Broadcast System

The backend uses a **two-tier messaging system** for efficiency:

1. **`stream_update`** — Lightweight delta sent every ~150ms during active generation
2. **`state`** — Full snapshot sent on significant events (spawn, completion, reset)

### Backend Streaming Loop
**File:** `api_server.py` (lines 1019–1188)

```python
# Line 1043: The agent runner yields partial responses in a generator loop
for partial in agent_runner.run(working_history, **llm_safe_cfg):
    responses = partial
    
    # Lines 1052-1064: Change detection — only broadcast on meaningful changes
    current_stack = list(get_active_stack())
    stack_changed = (current_stack != getattr(agent_pool, '_last_seen_stack', None))
    len_changed = (resp_len != last_resp_len)
    has_tool_event = _get_msg_func_call(last_m) or _get_msg_role(last_m) == FUNCTION
    
    # Line 1065: Throttle to max ~6.7 Hz (150ms interval), but force on changes
    if now - last_send > 0.15 or stack_changed or len_changed or has_tool_event:
        # Lines 1068-1102: Sub-agent state update strategy
        # Only recompute get_sub_agent_state when something actually changed
        if _sa_changed or any_sa_active:
            sub_agents_cache = get_sub_agent_state(streaming=True)
        
        # Line 1110: Build lightweight delta
        delta = build_stream_update(responses, cached_h_stats=cached_h_stats, 
                                     sub_agents=sub_agents_cache, telemetry=_telem_payload)
        
        # Lines 1115-1117: Queue for async WebSocket broadcast
        asyncio.run_coroutine_threadsafe(
            send_queue.put({'type': 'stream_update', **delta}), loop
        )
```

### `build_stream_update` — Lightweight Delta
**File:** `api_server.py` (lines 756–849)

This function is optimized to avoid re-serializing stable history:

```python
def build_stream_update(responses, cached_h_stats=None, sub_agents=None, telemetry=None):
    # Only serialize the CHANGING response messages (history already on client)
    response_msgs = [serialize_message(m, history_count + i) for i, m in enumerate(responses)]
    
    # Sub-agent state: None means "no update this tick" — frontend reuses cached state
    return {
        'history_count': history_count,
        'response_messages': response_msgs,
        'sub_agents': sub_agents,  # May be None for intermediate ticks
        'active_stack': get_active_stack(),
        'generating': True,
        'total_tokens': ...,
        ...
    }
```

### `get_sub_agent_state` — Sub-Agent State Serialization
**File:** `api_server.py` (lines 507–631)

This function serializes all sub-agent state for the frontend:

```python
def get_sub_agent_state(streaming=False):
    result = {}
    for name, state in agent_pool.sub_agent_state.items():
        msgs = state.get('messages', [])
        
        # Lines 607-615: Streaming optimization — send only last 3 messages if tab inactive
        if streaming and state.get('active') and len(msgs) > 5:
            serialized_msgs = [serialize_message(m, i) for m in msgs[-3:], start_idx]
            is_partial = True
        else:
            serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]
            is_partial = False
        
        result[name] = {
            'active': state.get('active', False),
            'agent_name': agent_class,
            'messages': serialized_msgs,
            'is_partial': is_partial,
            'history_count': len(msgs),
            'total_tokens': stats['tokens'],
            'total_words': stats['words'],
            'max_tokens': max_tokens,
            'summary': summary,
            'has_queued_messages': ...,
            'is_waiting': ...,
            'is_halted': ...,
        }
    return result
```

### WebSocket Broadcast Mechanism
**File:** `api_server.py` (lines 851–867)

```python
async def broadcast(data):
    """Send JSON to all connected WebSocket clients.
    Uses frozenset snapshot to avoid RuntimeError from set-size-changed-during-iteration."""
    text = json.dumps(data, ensure_ascii=False, default=str)
    snapshot = frozenset(ws_connections)
    for conn in snapshot:
        try:
            await conn.send_text(text)
        except Exception:
            ws_connections.discard(conn)
```

### Sender Loop — Message Router
**File:** `api_server.py` (lines 1403–1420)

```python
async def _sender_loop():
    while True:
        data = await send_queue.get()
        
        # Special handling for dismissal signals
        if data.get('type') == 'dismissal':
            await broadcast({'type': 'state', **build_state()})  # Full rebuild
        else:
            await broadcast(data)  # Pass through (stream_update, done, error, etc.)
```

### Frontend WebSocket Handler
**File:** `web_ui/app.js` (lines 728–986)

```javascript
// Case 'state' / 'done' — Full state update (lines 729-834)
case 'state':
case 'done':
    state.messages = data.messages || [];
    state.subAgents = data.sub_agents || {};     // ← Populates sub-agent data
    state.activeStack = data.active_stack || [];
    renderMessages();
    renderSubAgents();                            // ← Triggers tab re-render
    updateControls();
    break;

// Case 'stream_update' — Lightweight delta (lines 836-986)
case 'stream_update': {
    // Merge response messages incrementally
    state.messages.length = historyCount;
    state.messages.push(...responseMsgs);
    
    // Lines 858-859: Detect new visible sub-agent message
    if (data.sub_agents) {
        for (const [name, sa] of Object.entries(data.sub_agents)) {
            // Compare message counts to detect new messages
            const prevMsgCount = existing ? existing.messages.length : 0;
            const isNewVisibleMessage = sa.messages.length > prevMsgCount 
                && state.activeSubTab === 'sub-' + name;
            if (isNewVisibleMessage) subAgentNewVisibleMessage = true;
        }
    }
    
    // Lines 961-973: Throttled sub-agent rendering with tiered rates
    if (stackChanged || subAgentNewVisibleMessage || now - lastSubAgentRender > subThrottleContent) {
        renderSubAgents();
        state.genStats.lastSubAgentRender = now;
        
        // Lines 975-985: Auto-switch to active sub-agent tab
        if (state.activeStack.length > 0) {
            const topAgent = state.activeStack[state.activeStack.length - 1];
            switchMainTab('sub-' + topAgent);
        }
    }
}
```

---

## 3. Sub-Agent Output Update Strategy (Incremental vs Batched)

### During Active Streaming (Visible Tab)
**File:** `web_ui/app.js` (lines 2345–2373, 2455–2523)

The frontend uses **incremental rendering**:

```javascript
// Lines 2349-2369: Only append NEW messages
if (currentCount < lastCount || lastCount === 0) {
    scrollContainer.innerHTML = '';              // Full rebuild on shrink/reset
    for (let i = 0; i < currentCount; i++) ...
} else {
    // Append only new messages (incremental)
    for (let i = lastCount; i < currentCount; i++) {
        const el = createSubMsgEl(msgs[i], i, name, agentData.active && i === currentCount - 1);
        scrollContainer.appendChild(el);
    }
    // Update the last message if still generating (live text growth)
    if (scrollContainer.lastElementChild) {
        updateSubBubbleContent(scrollContainer.lastElementChild, msgs[currentCount - 1], agentData.active);
    }
}
```

### Incremental Text Streaming for Last Message
**File:** `web_ui/app.js` (lines 2455–2523)

```javascript
function updateSubBubbleContent(bubble, msg, isGenerating) {
    const prevContent = bubble.dataset.prevSubContent;
    const curContent = msg.content || '';
    
    if (isGenerating && curContent.startsWith(prevContent)) {
        // Incremental: only render the NEW portion appended to existing text
        const newText = curContent.slice(prevContent.length);
        if (newText) {
            appendStreamingDelta(content, newText);  // DOM-efficient insertion
            bubble.dataset.prevSubContent = curContent;
            return;  // Skip full re-render
        }
    }
    // Fallback: full re-render for non-incremental changes
    ...
}
```

### During Inactive (Hidden Tab) — Partial Messages
**File:** `api_server.py` (lines 607–615)

When a sub-agent tab is NOT visible, the backend sends only the **last 3 messages** to reduce JSON payload size:

```python
if streaming and state.get('active') and len(msgs) > 5:
    start_idx = max(0, len(msgs) - 3)
    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs[-3:], start_idx)]
    is_partial = True
else:
    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]
    is_partial = False
```

### Content-Key Change Detection
**File:** `web_ui/app.js` (lines 2326–2340)

Before any DOM work, the frontend checks a composite hash to skip unchanged renders:

```javascript
const contentKey = msgs.length + ':' + lastMsgTextLen + ':' + reasoningLen + ':' + funcCallLen + ':' + agentData.active;

if (panel.dataset.contentKey === contentKey && state.editingIndex === null) {
    return;  // Nothing changed — skip all DOM work
}
```

---

## 4. Sub-Agent Completion & Cleanup

### Backend: `finally` block in `_stream_sub_agent_call`
**File:** `agent_orchestrator.py` (lines 2320–2346)

```python
finally:
    state['active'] = False                              # Mark inactive
    state['messages'] = list(conv)                        # Full history
    self.agent_pool.sub_agent_state[instance_name] = state
    
    # Remove from active stack (lines 2327-2333)
    for i in range(len(self.agent_pool.active_stack) - 1, -1, -1):
        if self.agent_pool.active_stack[i] == instance_name:
            self.agent_pool.active_stack.pop(i)
            break
    
    # If terminated (dismissed from UI), clear conversation (lines 2340-2342)
    if removed and instance_name in self.agent_pool.terminated_instances:
        self.agent_pool.clear_conversation(instance_name)
        self.agent_pool.terminated_instances.discard(instance_name)
```

### Final State Broadcast
**File:** `api_server.py` (lines 1351–1353)

```python
final = build_state(generating=False)
halted = agent_pool.is_halted(session['session_name']) if agent_pool else False
asyncio.run_coroutine_threadsafe(
    send_queue.put({'type': 'done', **final, 'instance_halted': halted}), loop
)
```

The `done` message type is handled identically to `state` on the frontend (line 730: `case 'state': case 'done':`), triggering a full re-render of all tabs.

---

## 5. Sub-Agent Termination (User-Initiated Tab Removal)

### Frontend: User clicks close button
**File:** `web_ui/app.js` (lines 2166–2170)

```javascript
closeBtn.onclick = (e) => {
    e.stopPropagation();
    send({ type: 'terminate_sub_agent', instance_name: name });
    switchMainTab('chat');
};
```

### Backend: WebSocket handler processes termination
**File:** `api_server.py` (lines 1999–2004)

```python
elif msg_type == 'terminate_sub_agent':
    instance_name = data.get('instance_name')
    if instance_name and agent_pool:
        agent_pool.dismiss_instance(instance_name)
    # Force immediate state broadcast to update UI (remove tab)
    await broadcast({'type': 'state', **build_state()})
```

### `dismiss_instance` — Agent Pool cleanup
**File:** `agent_pool.py` (lines 316–332)

```python
def dismiss_instance(self, instance_name: str):
    if instance_name in self.active_stack:
        self.terminated_instances.add(instance_name)
        self._stopped_event.set()       # Signals the agent to stop
    else:
        # Not currently active — clear immediately
        self.clear_conversation(instance_name)
```

### LLM-Initiated Dismissal (via DismissAgent tool)
**File:** `api_server.py` (lines 1384–1400, 1408–1413)

When a sub-agent internally calls `dismiss_agent`:
1. `agent_pool._fire_on_dismissed(instance_name)` fires registered callbacks
2. Callback puts `{'type': 'dismissal', 'instance_name': ...}` on the send_queue
3. `_sender_loop` catches it, builds full state, and broadcasts as `type: 'state'`

---

## 6. `build_state` — Full State Snapshot Function
**File:** `api_server.py` (lines 652–754)

This is the comprehensive state builder used for initial WebSocket connection, reset, completion, and dismissal events:

```python
def build_state(responses=None, generating=None):
    msgs = list(session['history'])
    if responses:
        msgs.extend(responses)
    
    # Token stats with caching (lines 672-704)
    active_h = agent_pool.slice_history_for_llm(session['history'])
    h_stats = get_history_stats(active_h)  # With incremental cache
    
    return {
        'messages': [serialize_message(m, i) for i, m in enumerate(display_msgs)],
        'sub_agents': get_sub_agent_state(),          # ← Full sub-agent state
        'active_stack': get_active_stack(),            # ← Current recursion stack
        'approvals': get_approvals(),                  # ← Pending human approvals
        'generating': ...,
        'session_name': ...,
        'agent_index': session['agent_index'],
        'total_tokens': total_tokens,
        'agents': [...],                               # ← Agent definitions
        'api_router': ...,                             # ← Endpoint config
    }
```

---

## 7. State Data Structures Summary

### `AgentPool` (agent_pool.py)
| Attribute | Type | Purpose |
|-----------|------|---------|
| `instance_conversations` | `Dict[str, List]` | Persistent conversation history per instance |
| `sub_agent_state` | `Dict[str, dict]` | Live streaming state for WebUI (active flag, messages, tokens) |
| `active_stack` | `List[str]` | Stack of currently executing sub-agent names (supports recursion) |
| `instance_classes` | `Dict[str, str]` | Maps instance_name → agent_class |
| `instance_summaries` | `Dict[str, str]` | Active compression summary per instance |
| `_on_dismissed_callbacks` | `list` | Callbacks for LLM-initiated dismissal (real-time tab removal) |

### Sub-Agent State Dict (per instance in `sub_agent_state`)
| Key | Type | Purpose |
|-----|------|---------|
| `active` | `bool` | Currently executing |
| `agent_name` | `str` | Display name, e.g., "worker1 (Coder)" |
| `messages` | `List[dict]` | Conversation messages (full or partial during streaming) |
| `is_partial` | `bool` | Whether message list is truncated for hidden tabs |
| `history_count` | `int` | Total message count |
| `total_tokens` / `total_words` | `int` | Token/word stats |
| `max_tokens` | `int` | Context window limit |
| `summary` | `str` | Active compression summary |
| `has_queued_messages` | `bool` | Async messages waiting to be injected |
| `is_waiting` | `bool` | Waiting for API slot |
| `is_halted` | `bool` | Paused by user |

---

## 8. Throttle Rates Summary

| Event | Frontend Rate | Backend Trigger |
|-------|--------------|-----------------|
| Main chat during streaming | ~300ms | Every loop iteration (150ms threshold) |
| Sub-agent during streaming | ~750ms base, immediate for new visible message | Checked every 2 ticks, forced on stack change/tool event |
| Context bar update | ~1Hz (during streaming) | Throttled alongside main render |
| Telemetry in stream_update | Every ~3s (every 20 ticks) | `tick_num % 20 == 0` |

---

## 9. Complete Data Flow Diagram

```
[Orchestrator decides to call sub-agent]
    │
    ▼
agent_orchestrator.py:_stream_sub_agent_call()
    ├─ line 2008: active_stack.append(instance_name)
    ├─ line 2018-2024: sub_agent_state[name] = {active, agent_name, messages}
    └─ line 2028: yield current_response → triggers first broadcast
            │
            ▼
api_server.py: streaming loop (line 1043)
    ├─ line 1065: Throttle check (150ms or change detected)
    ├─ line 1100: get_sub_agent_state(streaming=True) → serializes all sub-agent state
    ├─ line 1110: build_stream_update() → lightweight delta
    └─ line 1115-1117: send_queue.put({'type': 'stream_update', ...})
            │
            ▼
_sender_loop (line 1403) → broadcast(data) to all WebSocket clients
            │
            ▼
Frontend WebSocket handler (app.js)
    ├─ case 'state' / 'done' (line 729): Full rebuild → renderSubAgents()
    └─ case 'stream_update' (line 836): Incremental merge → renderSubAgents() if changed
            │
            ▼
renderSubAgents() (line 2123)
    ├─ Remove stale tabs/panels (lines 2128-2135)
    ├─ Create tab button + panel if new (lines 2147-2216)
    └─ renderSubAgentPanel() (line 2219): Incremental message rendering

[Completion]
    agent_orchestrator.py finally block (line 2320)
        ├─ state['active'] = False
        ├─ active_stack.pop()
        └─ send_queue.put({'type': 'done', **build_state(generating=False)})
```

---

## Key Files Reference

| File | Key Line Numbers | Purpose |
|------|-----------------|---------|
| `agent_orchestrator.py` | 1985–2346 | `_stream_sub_agent_call()` — spawning, streaming loop, cleanup |
| `api_server.py` | 507–631 | `get_sub_agent_state()` — serialization |
| `api_server.py` | 652–754 | `build_state()` — full snapshot |
| `api_server.py` | 756–849 | `build_stream_update()` — lightweight delta |
| `api_server.py` | 851–867 | `broadcast()` — WebSocket fan-out |
| `api_server.py` | 1019–1188 | Streaming loop with change detection |
| `api_server.py` | 1403–1420 | `_sender_loop()` — message routing |
| `api_server.py` | 1999–2004 | `terminate_sub_agent` handler |
| `agent_pool.py` | 67–81 | Core data structures declaration |
| `agent_pool.py` | 316–332 | `dismiss_instance()` |
| `web_ui/app.js` | 729–834 | Full state WebSocket handler |
| `web_ui/app.js` | 836–986 | Stream update WebSocket handler |
| `web_ui/app.js` | 2123–2230 | `renderSubAgents()` — tab creation/rendering |
| `web_ui/app.js` | 2232–2373 | `renderSubAgentPanel()` — message rendering |
| `web_ui/app.js` | 2455–2523 | `updateSubBubbleContent()` — incremental text streaming |
| `web_ui/app.js` | 2525–2560 | `switchMainTab()` — tab switching |