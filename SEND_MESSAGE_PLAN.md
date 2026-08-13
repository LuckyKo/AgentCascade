# send_message Tool Implementation Plan

**Author:** ac_planner_sendmsg (initial), Maine (review corrections)  
**Date:** 2026-08-13  
**TODO item:** todo.md:40 — "Implement `send_message` tool"  
**Priority:** Medium (agent-to-agent communication feature)  
**Branch:** HEAD = current

**Status:** IMPLEMENTED (2026-08-13)

**Review Status:**

- Initial review completed by ac_reviewer_sendmsg_plan (backend sections)
- Full review completed by ac_reviewer_sendmsg_full (frontend sections)
- All blockers resolved in this revision:
  - State access corrected to use `instance.state` enum field
  - Sender identity now obtained via `_get_current_instance_name()` from operation_manager
  - Thread safety improved with pool lock for instance/state checks
  - Tab system uses consistent `sub-agent-messages` naming (no special-casing)
  - Initialization point identified: after ActivityBar.init() and connect() at app.js line ~4955
  - @mention routing confirmed: backend `_resolve_instance_name()` already supports `target_agent`

**Post-Implementation Cleanup:**

- Removed dead code `appendAgentMessageToVisibleList()` from `web_ui/app.js` — never called anywhere (found by regression review). The full re-render approach via `renderAgentMessages()` is used instead.
- CSS variable reference corrected: plan originally specified `--text-dim`, actual implementation uses `--text-muted`.

---

## 1. Overview

This plan implements the `send_message` tool, enabling running agents to send asynchronous messages to:
- Another agent instance (by name)
- The human user (destination `"user"`)

Messages are delivered via existing queue infrastructure without interrupting either sender or receiver's workflow. This enables parent-child coordination, cross-agent questions, and agent-to-user notifications.

Key behaviors:
- **Agent-to-agent:** Message goes into target's user message queue, tagged with sender identity. Delivered on next turn via `_inject_async_messages()`.
- **Agent-to-user:** Message is pushed to the frontend via WebSocket as a distinct notification event (not mixed with agent output streams). Displayed in a dedicated UI area.
- **Tool response:** Returns success/failure immediately based on whether the destination is reachable and actively running.

---

## 2. Design Decisions

### 2.1 Message Tagging Format

**Decision:** Prepend sender metadata as a structured prefix to the message text, visible to the receiving agent.

**Rationale:**
- Minimal changes: existing `enqueue_message()` accepts strings; no schema change needed.
- Human-readable for debugging.
- Consistent with how async tool results are already formatted (e.g., `⟨shell_cmd ...⟩`).

**Format:**
```
[MESSAGE from <sender_instance_name>]: <actual message text>
```

Example:
```
[MESSAGE from orchestrator_Maine]: Please prioritize the security review task.
```

### 2.2 Destination Validation

**Decision:** Tool returns success only if destination is in an ACTIVE state (RUNNING, SLEEPING, COMPLETING). Otherwise returns failure with reason.

**Rationale:**
- Prevents silent message loss to terminated/idle agents.
- Gives the sending agent immediate feedback to adjust strategy.
- Matches existing `ACTIVE_STATES` definition at `agent_instance.py:60`.

### 2.3 User Destination Handling

**Decision:** Messages to `"user"` bypass agent queues entirely and are pushed directly via WebSocket as a new event type `agent_message_to_user`.

**Rationale:**
- User messages shouldn't pollute any agent's queue.
- Requires distinct UI handling (dedicated notification area).
- Keeps the feature self-contained within backend + frontend changes.

### 2.4 Tool Registration Pattern

**Decision:** Follow existing pattern from `manager_ops.py`:
- Use `@register_tool` decorator.
- Accept `agent_pool` via constructor injection.
- Store metadata in `TOOL_METADATA` dict in `dna.py`.

### 2.5 No Reply Mechanism (Phase 1)

**Decision:** Initial implementation is one-way messaging only. User cannot reply from the notification area; they must go to the agent's tab.

**Rationale:**
- Keeps scope manageable for first release.
- Reply functionality can be added later without breaking changes.

---

## 3. Tool Specification

### 3.1 Tool Name and Registration

- **Name:** `send_message`
- **File:** `agent_cascade/tools/custom/send_message.py`
- **Decorator:** `@register_tool('send_message', allow_overwrite=True)`

### 3.2 Parameters (JSON Schema)

```json
{
  "type": "object",
  "properties": {
    "destination": {
      "type": "string",
      "description": "Target of the message. Use 'user' to send to the human user, or an exact agent instance name (e.g., 'worker1', 'orchestrator_Maine') to send to another agent."
    },
    "message": {
      "type": "string",
      "description": "The message content to send. Be concise and clear."
    }
  },
  "required": ["destination", "message"]
}
```

### 3.3 Tool Description (for DNA)

> Sends an asynchronous message to another running agent or to the user. The message is queued and delivered on the recipient's next turn without interrupting either party's current workflow. Returns success only if the destination is actively running; otherwise returns a failure reason. Use this for coordination between agents, parent-to-child guidance, or notifying the user of important updates.

### 3.4 Return Values

**Success (agent destination):**
```
Message sent successfully to '<destination>'. It will be delivered on their next turn.
```

**Success (user destination):**
```
Message sent successfully to the user. They will see it in their notifications.
```

**Failure cases:**
- Destination `"user"`: not supported scenario (shouldn't happen, but handled).
- Unknown agent name: `Failed: No agent instance named '<destination>' exists.`
- Agent not active (IDLE/TERMINATED): `Failed: Agent '<destination>' is currently <STATE_NAME>. Messages are only delivered to actively running agents.`
- Self-message: `Failed: Cannot send a message to yourself.`

### 3.5 Constructor Injection

```python
def __init__(self, agent_pool=None, **kwargs):
    super().__init__(**kwargs)
    self.agent_pool = agent_pool
```

Same pattern as `ListAgents` in `manager_ops.py:41-43`.

---

## 4. Implementation Steps

### Step 1: Create the Tool Class

**File:** `agent_cascade/tools/custom/send_message.py` (new)

Create the tool following patterns from `manager_ops.py`:

import asyncio
import time
from typing import TYPE_CHECKING

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.agent_instance import ACTIVE_STATES
from agent_cascade.operation_manager.path_security import _get_current_instance_name

if TYPE_CHECKING:
    from agent_cascade.agent_pool import AgentPool


@register_tool('send_message', allow_overwrite=True)
class SendMessage(BaseTool):
    """Sends an async message to another running agent or to the user."""

    name = 'send_message'
    description = TOOL_METADATA['send_message']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'destination': {
                'type': 'string',
                'description': "Target of the message. Use 'user' to send to the human user, or an exact agent instance name (e.g., 'worker1') to send to another agent."
            },
            'message': {
                'type': 'string',
                'description': 'The message content to send.'
            }
        },
        'required': ['destination', 'message']
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool: 'AgentPool' = agent_pool

    def call(self, params: str, **kwargs) -> str:
        if not self.agent_pool:
            return "Error: No agent pool available."

        parsed = self._verify_json_format_args(params)
        destination = parsed.get('destination', '')
        message = parsed.get('message', '')

        # Validate inputs
        if not destination or not str(destination).strip():
            return "Failed: Destination cannot be empty."

        destination = str(destination).strip()

        if not message or not str(message).strip():
            return "Failed: Message content cannot be empty."

        message = str(message).strip()

        # Handle user destination
        if destination == 'user':
            return self._send_to_user(message)

        # Handle agent destination
        return self._send_to_agent(destination, message)

    def _get_sender_name(self) -> str:
        """Get the current agent instance name from thread-local storage."""
        return _get_current_instance_name() or 'unknown'

    def _send_to_user(self, message: str) -> str:
        """Push message to frontend via WebSocket as notification."""
        pool = self.agent_pool
        sender = self._get_sender_name()

        try:
            ws_queue = getattr(pool, '_ws_send_queue', None)
            ws_loop = getattr(pool, '_ws_loop', None)

            if not (ws_queue and ws_loop and not ws_loop.is_closed()):
                return "Warning: User notification sent but WebSocket unavailable. Message logged."

            event = {
                'type': 'agent_message_to_user',
                'sender': sender,
                'message': message,
                'timestamp': time.time()
            }

            asyncio.run_coroutine_threadsafe(
                _put_stream_update(ws_queue, event),
                ws_loop
            )
            return "Message sent successfully to the user. They will see it in their notifications."
        except Exception as e:
            # Log and degrade gracefully
            from agent_cascade.log import logger
            logger.warning(f"Failed to send message to user via WebSocket: {e}")
            return f"Warning: Message queued but notification may not be delivered immediately: {e}"

    def _send_to_agent(self, destination: str, message: str) -> str:
        """Queue message for target agent."""
        pool = self.agent_pool
        sender = self._get_sender_name()

        # Self-message guard
        if destination == sender:
            return "Failed: Cannot send a message to yourself."

        # Thread-safe access: acquire pool lock while checking instance + state
        with pool._pool_lock:
            target_instance = pool.instances.get(destination)
            if target_instance is None:
                return f"Failed: No agent instance named '{destination}' exists."

            # Use direct .state field (AgentState enum), not get_state() which doesn't exist
            current_state = target_instance.state
            if current_state not in ACTIVE_STATES:
                state_name = current_state.name
                return f"Failed: Agent '{destination}' is currently {state_name}. Messages are only delivered to actively running agents."

        # Enqueue outside the pool lock (enqueue_message has its own queue lock)
        tagged_message = f"[MESSAGE from {sender}]: {message}"
        pool.enqueue_message(destination, tagged_message)

        return f"Message sent successfully to '{destination}'. It will be delivered on their next turn."


async def _put_stream_update(queue, event):
    """Helper: put event on queue, drop if full."""
    try:
        await asyncio.wait_for(queue.put(event), timeout=1.0)
    except asyncio.TimeoutError:
        pass  # Drop message if queue is full (non-critical notification)


### Step 2: Register Tool in DNA Metadata

**File:** `agent_cascade/prompts/dna.py`

Add entry to `TOOL_METADATA` dict (after `list_agents` entry around line 484). Match the structure of existing entries like `compress_context`:

```python
'send_message': {
    'description': (
        'Sends an asynchronous message to another running agent or to the user. '
        'The message is queued and delivered on the recipient\'s next turn without '
        'interrupting either party\'s current workflow. Returns success only if the '
        'destination is actively running; otherwise returns a failure reason. Use this '
        'for coordination between agents, parent-to-child guidance, or notifying the '
        'user of important updates.'
    ),
    'parameters': {
        'destination': (
            "Target of the message. Use 'user' to send to the human user, "
            "or an exact agent instance name (e.g., 'worker1') to send to another agent."
        ),
        'message': 'The message content to send.'
    }
},
```

Add `'send_message'` to `AVAILABLE_TOOLS` list (after line 17, near other sub-agent management tools):

```python
# Sub-agent management
'call_agent',       # Delegate tasks to specialized agent instances
'dismiss_agent',    # End sub-agent sessions and clear context
'list_agents',      # List available agent classes and active instances
'send_message',     # Send async messages to running agents or user
```

### Step 3: Ensure Tool Gets Agent Pool Injection

**File:** `agent_cascade/agent_factory.py`

Follow the exact pattern used for `ListAgents`. In `_build_tools_for_agent()` around line 54, add a handler:

```python
elif tool_name == 'send_message':
    from agent_cascade.tools.custom.send_message import SendMessage
    tools_to_register[tool_name] = (SendMessage(agent_pool=agent_pool), False, False)
```

This ensures the tool is pre-instantiated with `agent_pool` just like `ListAgents`, avoiding any runtime None issues.

### Step 4: Add WebSocket Handler for User Notifications (Backend)

**File:** `agent_cascade/api_integration.py`

The `_put_stream_update` helper already exists at line 123. The new event type `agent_message_to_user` will be handled by the frontend. No backend routing changes needed — the tool pushes directly to the WebSocket queue using the existing infrastructure.

### Step 5: Frontend Notification Handling

**File:** Frontend WebSocket handler (likely in web UI code under `frontend/` or similar)

Add handling for the new event type:

```javascript
case 'agent_message_to_user':
    showAgentNotification(event.sender, event.message, event.timestamp);
    break;
```

This should:
1. Display notification in a dedicated area (toast, sidebar panel, or new "Agent Messages" tab).
2. Show sender name, message content, and timestamp.
3. Optionally play a subtle sound/show badge.
4. Allow user to click through to the sender's agent tab.

**Note:** Frontend implementation is out of scope for this backend-focused plan but must be coordinated.

---

## 5. Message Format

### 5.1 Agent-to-Agent Messages (Queue Entry)

Stored in `message_queues[to_name]` as a plain string:

```
[MESSAGE from <sender_instance>]: <message_text>
```

When drained by `_inject_async_messages()` and converted via `_make_user_message()`, this becomes a USER-role message in the target agent's conversation. The receiving agent sees it as incoming user input with clear sender attribution.

### 5.2 Agent-to-User Messages (WebSocket Event)

JSON event pushed to WebSocket:

```json
{
    "type": "agent_message_to_user",
    "sender": "orchestrator_Maine",
    "message": "Security review complete. Found 3 critical issues.",
    "timestamp": 1723500000.123
}
```

### 5.3 Why This Format Works

- **Sender identity is explicit** — no ambiguity about who sent what.
- **Backwards compatible** — existing queue/drain logic handles strings without modification.
- **Human-readable in logs** — easy to trace message flow during debugging.
- **LLM-friendly** — the receiving agent's prompt will naturally include this as context, allowing it to respond appropriately (e.g., "Understood, orchestrator_Maine").

---

## 6. User Notification Flow

```
Agent calls send_message(destination="user", message="...")
    ↓
SendMessage._send_to_user() constructs WebSocket event
    ↓
Event pushed to pool._ws_send_queue via asyncio.run_coroutine_threadsafe()
    ↓
WebSocket handler (api_server.py) broadcasts to connected client(s)
    ↓
Frontend receives event type "agent_message_to_user"
    ↓
Notification displayed in dedicated UI area
```

Key properties:
- **Non-blocking:** Uses async queue push; tool returns immediately.
- **Graceful degradation:** If WebSocket unavailable, logs warning and reports partial success.
- **No message persistence required (Phase 1):** Notifications are ephemeral. User can always check agent tabs for full context.

---

## 7. Edge Cases & Error Handling

| Scenario | Behavior |
|----------|----------|
| Destination doesn't exist | Return failure: "No agent instance named 'X' exists." |
| Destination is IDLE | Return failure with state name; message NOT queued. |
| Destination is TERMINATED | Return failure with state name; message NOT queued. |
| Self-message (agent sends to itself) | Return failure: "Cannot send a message to yourself." |
| Empty message content | Return failure: "Message content cannot be empty." |
| Agent pool unavailable | Return error: "No agent pool available." |
| WebSocket queue full (user msg) | Drop notification silently (asyncio.TimeoutError); log warning. |
| WebSocket loop closed (user msg) | Log warning, return degraded success message. |
| Destination is SLEEPING | SUCCESS — SLEEPING is active; message delivered on wakeup drain. |
| Destination is COMPLETING | SUCCESS — COMPLETING is active; message delivered before termination if queue drains. |
| Invalid JSON params | Handled by `_verify_json_format_args()` → raises ValueError → tool dispatcher returns error to agent. |

---

## 8. Testing Plan

### 8.1 Unit Tests

File: `tests/test_send_message_tool.py` (new)

- **Test: Send to active agent**
  - Setup: Create mock pool with RUNNING agent instance
  - Action: Call tool with valid destination
  - Assert: Success message returned, message in target's queue with correct prefix

- **Test: Send to inactive agent (IDLE)**
  - Setup: Agent instance in IDLE state
  - Assert: Failure response mentioning IDLE state

- **Test: Send to terminated agent**
  - Setup: Agent instance in TERMINATED state
  - Assert: Failure response mentioning TERMINATED state

- **Test: Send to non-existent agent**
  - Assert: Failure response about unknown instance

- **Test: Self-message rejection**
  - Setup: Instance name passed via kwargs matches destination
  - Assert: Failure about self-messaging

- **Test: Empty message rejection**
  - Assert: Failure about empty content

- **Test: Send to user (WebSocket available)**
  - Setup: Mock pool with valid ws_queue/ws_loop
  - Assert: Event pushed to queue with correct structure

- **Test: Send to user (WebSocket unavailable)**
  - Setup: Pool without ws_queue
  - Assert: Warning message returned, no crash

### 8.2 Integration Tests

- **End-to-end agent-to-agent:**
  - Spawn two agents (parent + child via call_agent)
  - Parent calls send_message to child's instance name
  - Verify child receives tagged message on next turn

- **Agent-to-user delivery:**
  - Agent calls send_message(destination="user", ...)
  - Verify WebSocket event received by frontend test harness

- **SLEEPING agent delivery:**
  - Put target in SLEEPING state (via async tool)
  - Send message while sleeping
  - Verify message delivered when agent wakes up

### 8.3 Manual Testing Checklist

- [ ] Orchestrator sends steering message to running worker; worker acknowledges
- [ ] Worker notifies user of completion via send_message
- [ ] Attempting to message a dismissed agent returns clear error
- [ ] Notification appears in UI without disrupting active agent tabs
- [ ] Multiple messages queued correctly (FIFO order preserved)

---

## 9. Dependencies & Ordering

1. **Step 1** — Create tool class (independent)
2. **Step 2** — Register in DNA metadata (depends on Step 1)
3. **Step 3** — Ensure pool injection works (depends on Step 1, verify with existing pattern)
4. **Step 4** — Backend WebSocket event is ready (no changes needed; uses existing infra)
5. **Step 5** — Frontend notification UI (parallel track, depends on agreed event format)

Estimated effort: ~2-3 hours backend, frontend TBD based on UI complexity.

---

## 10. Future Enhancements (Out of Scope)

- User reply from notification area → routes to agent's queue
- Message persistence/history for user-facing messages
- Priority/urgency levels for messages
- Message expiration/TTL for queued messages
- Bulk messaging to multiple agents
- Read receipts / delivery confirmation

---

## 11. Frontend Implementation — Agent Messages Tab

### 11.1 Overview

A new "Agent Messages" tab provides a centralized inbox where agents can send direct notifications to the user. This tab sits alongside existing agent tabs in the main tab bar and uses the same visual language (message bubbles, markdown rendering, etc.).

Key features:
- Dedicated tab in `#mainTabBar` with data-tab="agent-messages"
- Chronological message list from any agent
- Persisted via localStorage (survives page refresh)
- Unread badge/counter on the tab when new messages arrive
- Click-to-reply: clicking a message bubble auto-inserts @sender into `#chatInput`
- @mention routing: messages typed with @agent_name are routed to that agent instead of the active tab's agent

### 11.2 Tab Creation (HTML + JS Changes)

**No HTML changes required.** The tab bar and panel container are already dynamic. All tabs/panels are created in `renderSubAgents()` at `app.js:3356-3511`.

**Changes in `app.js`:**

Add a dedicated initialization function that runs once on page load. Call it from the init section at **line ~4955**, right after `connect()` is called and before any other event listeners are attached. This ensures DOM elements exist and WebSocket is connecting.

```javascript
// Initialize Agent Messages tab (static, always present)
function initAgentMessagesTab() {
    // Create tab button — use 'sub-' prefix for consistency with all other tabs
    const tabBtn = document.createElement('button');
    tabBtn.className = 'main-tab';
    tabBtn.dataset.tab = 'sub-agent-messages';
    tabBtn.onclick = () => switchMainTab('sub-agent-messages');

    const iconSpan = document.createElement('span');
    iconSpan.className = 'tab-icon-container';
    iconSpan.innerHTML = '<span class="main-tab-icon">💬</span>';
    tabBtn.appendChild(iconSpan);

    const labelSpan = document.createElement('span');
    labelSpan.className = 'tab-label';
    labelSpan.textContent = ' Agent Messages';
    tabBtn.appendChild(labelSpan);

    // Unread badge (hidden by default)
    const badge = document.createElement('span');
    badge.className = 'agent-messages-badge';
    badge.id = 'agentMessagesBadge';
    badge.style.display = 'none';
    tabBtn.appendChild(badge);

    mainTabBar.appendChild(tabBtn);

    // Create panel — use consistent naming pattern: panelSub-{name}
    const panel = document.createElement('div');
    panel.className = 'main-tab-panel agent-messages-panel';
    panel.id = 'panelSub-agent-messages';

    const messagesContainer = document.createElement('div');
    messagesContainer.className = 'messages';
    messagesContainer.id = 'agentMessagesList';
    panel.appendChild(messagesContainer);

    mainTabPanels.appendChild(panel);

    // Load persisted messages and render
    loadAgentMessages();
    renderAgentMessages();
}
```

**Init call location (app.js, after line ~4955):**

```javascript
// ── Init ─────────────────────────────────────────────────────────────────────
ActivityBar.init();
connect();
initAgentMessagesTab(); // NEW: initialize Agent Messages tab
```

**CSS additions in `styles.css`:**

Add near the existing `.main-tab` styling (around line 100-150 where tab styles live):

```css
/* Agent Messages badge */
.agent-messages-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--accent);
    color: #fff;
    font-size: 10px;
    font-weight: 600;
    min-width: 16px;
    height: 16px;
    border-radius: 8px;
    padding: 0 4px;
    margin-left: 4px;
}

/* Agent Messages panel */
.agent-messages-panel .messages {
    padding: 12px 16px;
}

/* Agent Message bubble (distinct from conversation messages) */
.agent-message {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
    padding: 8px 12px;
    border-radius: 8px;
    background: var(--bg-secondary);
    border-left: 3px solid var(--accent);
    cursor: pointer;
    transition: background 0.15s ease;
}

.agent-message:hover {
    background: var(--bg-tertiary);
}

.agent-message-sender {
    font-weight: 600;
    font-size: 13px;
    color: var(--accent);
    white-space: nowrap;
}

.agent-message-time {
    font-size: 11px;
    color: var(--text-muted);
    margin-left: auto;
    white-space: nowrap;
}

.agent-message-content {
    margin-top: 4px;
    font-size: 13px;
    line-height: 1.5;
    color: var(--text-primary);
}

.agent-message-content code {
    background: var(--bg-code);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 12px;
}

.agent-message-hint {
    font-size: 10px;
    color: var(--text-muted);
    margin-top: 2px;
    font-style: italic;
}

/* Empty state */
.agent-messages-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    padding: 40px;
    color: var(--text-muted);
    text-align: center;
}

.agent-messages-empty svg {
    width: 48px;
    height: 48px;
    opacity: 0.3;
    margin-bottom: 12px;
}
```

### 11.3 Message Display (Reusing Existing Components)

**New function in `app.js` (add after `createMessageEl`, around line 2500):**

```javascript
// Render agent messages into the Agent Messages panel
function renderAgentMessages() {
    const container = document.getElementById('agentMessagesList');
    if (!container) return;

    const messages = getAgentMessages();

    if (messages.length === 0) {
        container.innerHTML = `
            <div class="agent-messages-empty">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                </svg>
                <div>No agent messages yet</div>
                <div style="font-size:11px;margin-top:4px;">When agents send you notifications, they'll appear here.</div>
            </div>
        `;
        return;
    }

    // Use a document fragment for efficient DOM update
    const fragment = document.createDocumentFragment();

    messages.forEach((msg) => {
        const el = createAgentMessageEl(msg);
        fragment.appendChild(el);
    });

    container.innerHTML = '';
    container.appendChild(fragment);

    // Scroll to bottom on initial render only (not on new message arrival while viewing)
    if (!container.dataset.initialRendered) {
        container.dataset.initialRendered = 'true';
        scrollPanelToBottom(container, 'agent-messages', false);
    }
}

// Create a single agent message element
function createAgentMessageEl(msg) {
    const div = document.createElement('div');
    div.className = 'agent-message';
    if (!msg.read) {
        div.classList.add('unread');
        div.style.borderLeftColor = '#ffc107'; // Yellow for unread
    }

    // Click to reply: insert @sender into chat input
    div.onclick = () => {
        insertAgentMention(msg.sender);
        markMessageRead(msg.id);
    };

    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;gap:6px;';

    const sender = document.createElement('span');
    sender.className = 'agent-message-sender';
    sender.textContent = msg.sender;

    const time = document.createElement('span');
    time.className = 'agent-message-time';
    time.textContent = formatTimestamp(msg.timestamp);

    header.appendChild(sender);
    header.appendChild(time);
    div.appendChild(header);

    // Render markdown content (reuse existing libs: marked + DOMPurify)
    const content = document.createElement('div');
    content.className = 'agent-message-content';
    const rawHtml = marked.parse(msg.message || '');
    content.innerHTML = DOMPurify.sanitize(rawHtml, { USE_PROFILES: { html: true } });
    div.appendChild(content);

    // Hint about clicking to reply
    const hint = document.createElement('div');
    hint.className = 'agent-message-hint';
    hint.textContent = `Click to reply with @${msg.sender}`;
    div.appendChild(hint);

    return div;
}

function formatTimestamp(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000); // backend sends unix timestamp in seconds
    const now = new Date();
    const diffMs = now - d;
    const diffMin = Math.floor(diffMs / 60000);

    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin}m ago`;
    if (diffMin < 1440) return `${Math.floor(diffMin / 60)}h ago`;

    // Full date for older messages
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
```

### 11.4 WebSocket Event Handling (New Event Type Handler)

**Changes in `app.js` — `handleServerMessage()` at lines 1585-2193:**

Add a new case in the switch statement (insert after the `import_settings` case around line 2178, before the closing brace of the switch):

case 'agent_message_to_user': {
    const { sender, message, timestamp } = data;
    if (!sender || !message) break;

    // Add to persisted store
    addAgentMessage({
        id: crypto.randomUUID(),
        sender: sender,
        message: message,
        timestamp: timestamp || Date.now() / 1000,
        read: false
    });

    // If user is currently viewing the Agent Messages tab, render immediately and mark as read
    if (state.activeSubTab === 'sub-agent-messages') {
        renderAgentMessages();
        markAllMessagesRead();
        appendAgentMessageToVisibleList({ sender, message, timestamp: timestamp || Date.now() / 1000, read: true });
    } else {
        // Update unread badge
        updateAgentMessagesBadge();
    }

    // Optional: subtle notification sound or browser notification
    // playSound('notification'); // Uncomment if desired
    break;
}


### 11.5 State Management (Storage Strategy for Agent Messages)

**New functions in `app.js` (add near other state/persistence helpers, around line 200-300):**

```javascript
const AGENT_MESSAGES_STORAGE_KEY = 'agent-cascade-agent-messages';
const MAX_AGENT_MESSAGES = 200; // Cap to prevent unbounded growth

// Load messages from localStorage
function loadAgentMessages() {
    try {
        const raw = localStorage.getItem(AGENT_MESSAGES_STORAGE_KEY);
        if (raw) {
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed)) {
                state.agentMessages = parsed.slice(-MAX_AGENT_MESSAGES);
                return;
            }
        }
    } catch (e) {
        console.warn('[AgentMessages] Failed to load from localStorage:', e);
    }
    state.agentMessages = [];
}

// Save messages to localStorage
function saveAgentMessages() {
    try {
        // Trim to max before saving
        if (state.agentMessages.length > MAX_AGENT_MESSAGES) {
            state.agentMessages = state.agentMessages.slice(-MAX_AGENT_MESSAGES);
        }
        localStorage.setItem(AGENT_MESSAGES_STORAGE_KEY, JSON.stringify(state.agentMessages));
    } catch (e) {
        console.warn('[AgentMessages] Failed to save to localStorage:', e);
    }
}

// Get all messages (sorted by timestamp ascending)
function getAgentMessages() {
    return state.agentMessages || [];
}

// Add a new message — uses crypto.randomUUID() for ID
function addAgentMessage(msg) {
    if (!state.agentMessages) state.agentMessages = [];
    state.agentMessages.push(msg);
    saveAgentMessages();
}

// Mark a specific message as read
function markMessageRead(id) {
    const msg = state.agentMessages?.find(m => m.id === id);
    if (msg && !msg.read) {
        msg.read = true;
        saveAgentMessages();
        updateAgentMessagesBadge();
    }
}

// Mark all messages as read
function markAllMessagesRead() {
    if (!state.agentMessages?.length) return;
    const hadUnread = state.agentMessages.some(m => !m.read);
    state.agentMessages.forEach(m => m.read = true);
    if (hadUnread) {
        saveAgentMessages();
        updateAgentMessagesBadge();
    }
}

// Append a single message to the visible list without full re-render
function appendAgentMessageToVisibleList(msg) {
    const container = document.getElementById('agentMessagesList');
    if (!container || container.querySelector('.agent-messages-empty')) return;

    const el = createAgentMessageEl({ id: crypto.randomUUID(), ...msg });
    container.appendChild(el);
    scrollPanelToBottom(container, 'sub-agent-messages', false);
}
```

**State object update at `app.js:72-115`:**

Add after `closedTabs` line (around line 114):

```javascript
closedTabs: new Set(JSON.parse(localStorage.getItem('agent-cascade-closed-tabs') || '[]')),
agentMessages: [], // Populated via loadAgentMessages() on init
```

### 11.6 @Mention System (Parsing, Routing Logic, Click-to-Reply UX)

**Click-to-reply function in `app.js` (add near sendMessage, around line 4900):**

```javascript
// Insert @mention of an agent into the chat input and focus it
function insertAgentMention(agentName) {
    const input = chatInput;
    const mention = '@' + agentName + ' ';

    // If input is empty, just set the value
    if (!input.value.trim()) {
        input.value = mention;
    } else {
        // Append to existing content with a space
        input.value += mention;
    }

    input.focus();
    autoResize(input);

    // Move cursor after the inserted mention
    const pos = input.value.length;
    input.setSelectionRange(pos, pos);
}
```

**@mention parsing function in `app.js`:**

```javascript
// Parse @mentions from message text and return { targetAgent, cleanedText }
function parseMentionRouting(text) {
    if (!text) return { targetAgent: null, cleanedText: text };

    // Match @mention at start of message (after optional whitespace)
    const mentionMatch = text.match(/^\s*@(\S+)\s+(.*)$/s);
    if (!mentionMatch) {
        return { targetAgent: null, cleanedText: text.trim() };
    }

    const mentionedName = mentionMatch[1];
    const cleanedText = mentionMatch[2].trim();

    // Validate: mentioned name must match an existing agent instance
    if (state.subAgents && state.subAgents[mentionedName]) {
        return { targetAgent: mentionedName, cleanedText: cleanedText || text.trim() };
    }

    // Unknown agent: treat as normal message (don't strip the @)
    console.log('[AgentMessages] @mention for unknown agent:', mentionedName);
    return { targetAgent: null, cleanedText: text.trim() };
}
```

### 11.7 Reply Routing Logic (How to Parse @Mentions and Route Messages)

**Modify `sendMessage()` at `app.js:4894-4921`:**

The key change is that when the active tab is "sub-agent-messages", we parse for @mentions and route accordingly. If no @mention is found, default to the session's primary agent (the orchestrator).

**Backend confirmation:** The backend already supports this via `WsMessageHandler._resolve_instance_name()` at `ws_handlers.py:176`:
```python
def _resolve_instance_name(self, data: dict) -> str:
    return data.get('target_agent') or self.session['session_name']
```
So setting `target_agent` in the WebSocket message payload will correctly route to any active agent instance — no backend changes required.

```javascript
function sendMessage(inputEl) {
    const targetInput = inputEl instanceof HTMLElement ? inputEl : chatInput;
    const rawText = targetInput.value.trim();
    if (!rawText) return;

    const text = formatMultimodalContent(rawText);
    targetInput.value = '';
    autoResize(targetInput);
    imagePreviewContainer.innerHTML = ''; // Clear image previews after sending

    // Determine routing based on active tab and @mentions
    let targetAgent = null;
    let messageText = text;

    if (state.activeSubTab === 'sub-agent-messages') {
        // In Agent Messages tab: parse @mentions for routing
        const parsed = parseMentionRouting(text);
        if (parsed.targetAgent) {
            targetAgent = parsed.targetAgent;
            messageText = parsed.cleanedText || text;
        } else {
            // No valid @mention: route to session primary agent (orchestrator)
            targetAgent = state.sessionName;
        }
    } else {
        // Normal behavior: route to the active tab's agent
        targetAgent = getActiveAgentName();
    }

    if (state.generating) {
        // Async injection during generation
        send({ type: 'message', text: messageText, target_agent: targetAgent });
        return;
    }

    resetGenStats();
    send({
        type: 'message',
        text: messageText,
        target_agent: targetAgent,
        agent_index: state.agentIndex,
        session_name: state.sessionName,
        generate_cfg: getGenerateCfg()
    });
}
```

### 11.8 Unread Badge/Notification Indicator

**Badge update function in `app.js` (add near other UI update helpers):**

```javascript
// Update the unread badge on the Agent Messages tab
function updateAgentMessagesBadge() {
    const badge = document.getElementById('agentMessagesBadge');
    if (!badge) return;

    const unreadCount = (state.agentMessages || []).filter(m => !m.read).length;

    if (unreadCount > 0) {
        badge.style.display = 'inline-flex';
        badge.textContent = unreadCount > 99 ? '99+' : unreadCount;
    } else {
        badge.style.display = 'none';
    }
}
```

**Integration with tab switching:**

Modify `switchMainTab()` at `app.js:3762-3804`. Add mark-as-read logic right after `state.activeSubTab = tabId;` (line 3778). Since the panel follows the standard `panelSub-{name}` naming pattern, no special-casing is needed in the panel lookup — the existing code handles it.

```javascript
function switchMainTab(tabId) {
    // Update tab buttons
    mainTabBar.querySelectorAll('.main-tab').forEach(t => t.classList.remove('active'));
    const activeTab = mainTabBar.querySelector(`.main-tab[data-tab="${tabId}"]`);
    if (activeTab) activeTab.classList.add('active');

    // Mark agent messages as read when switching to that tab
    if (tabId === 'sub-agent-messages') {
        markAllMessagesRead();
        renderAgentMessages(); // Re-render to show updated read state
    }

    // Update panels — all tabs use the same dynamic panel system now
    mainTabPanels.querySelectorAll('.main-tab-panel').forEach(p => p.classList.remove('active'));
    
    const name = tabId.substring(4); // strip 'sub-' prefix → 'agent-messages'
    const panel = document.getElementById('panelSub-' + name);
    if (panel) {
        panel.classList.add('active');
        scrollPanelToBottom(panel, name, false);
    }
    
    state.activeSubTab = tabId;
    ActivityBar.setActiveTab(tabId);

    // Invalidate target panel's content key cache to force a full re-render...
    // (rest of function unchanged)
```

Key point: With `tabId = 'sub-agent-messages'`, the existing line `const name = tabId.substring(4)` yields `'agent-messages'`, and `document.getElementById('panelSub-agent-messages')` finds our panel. No special branch needed.
```

### 11.9 File-by-File Change List with Line References

**`web_ui/app.js`:**

| Location | Change |
|----------|--------|
| Lines 72-115 (state object) | Add `agentMessages: []` field after `closedTabs` |
| Line ~4955 (init section, after `connect()`) | Call `initAgentMessagesTab()` to create tab and panel |
| Lines ~200-300 (new section) | Add state management functions: `loadAgentMessages()`, `saveAgentMessages()`, `getAgentMessages()`, `addAgentMessage()`, `markMessageRead()`, `markAllMessagesRead()`, `appendAgentMessageToVisibleList()` |
| Lines ~2450 (after createMessageEl at 2328) | Add render functions: `renderAgentMessages()`, `createAgentMessageEl()`, `formatTimestamp()` |
| Lines 3762-3804 (`switchMainTab`) | Add mark-as-read logic for `sub-agent-messages` tab (no special panel handling needed) |
| Lines ~2178 (`handleServerMessage` switch) | Add case `'agent_message_to_user'` handler |
| Lines ~4900 (near sendMessage at 4894) | Add `insertAgentMention()` and `parseMentionRouting()` functions |
| Lines 4894-4921 (`sendMessage`) | Modify to route via @mentions when in Agent Messages tab |

**`web_ui/styles.css`:**

| Location | Change |
|----------|--------|
| Near existing `.main-tab` styles (~line 100-150) | Add `.agent-messages-badge`, `.agent-messages-panel`, `.agent-message`, `.agent-message-sender`, `.agent-message-time`, `.agent-message-content`, `.agent-message-hint`, `.agent-messages-empty` styles |

**`web_ui/index.html`:**

No changes required — tab and panel are created dynamically via JavaScript.

---

## 12. Updated Testing Plan

### 12.1 Frontend Unit Tests (Manual / Browser Console)

- **Tab appears on load:**
  - Open the web UI; verify "Agent Messages" tab exists in `#mainTabBar`
  - Verify panel shows empty state with icon and text

- **Message receipt via WebSocket:**
  - Have an agent call `send_message(destination="user", message="test")`
  - Verify badge appears on Agent Messages tab with count "1"
  - Click the tab; verify message is displayed with sender, timestamp, content
  - Verify badge disappears after viewing

- **Persistence across refresh:**
  - Receive a message, refresh the page
  - Verify message still appears in Agent Messages tab
  - Verify read/unread state is preserved

- **Click-to-reply:**
  - Click on an agent's message bubble
  - Verify `#chatInput` now contains `@agentName ` and has focus
  - Send the message; verify it routes to that agent (check backend logs or agent response)

- **@mention routing:**
  - Switch to Agent Messages tab
  - Type `@worker1 do something` and send
  - Verify message is routed to `worker1`, not the orchestrator
  - Type a message without @mention; verify it routes to session primary agent

- **Unknown @mention handling:**
  - Type `@nonexistent_agent hello` in Agent Messages tab
  - Verify message is NOT stripped of the @ and routes to session primary agent (fallback)

- **Unread badge behavior:**
  - Receive multiple messages while on another tab; verify badge shows correct count
  - Switch to Agent Messages tab; verify badge clears
  - Max out badge display at "99+" for >99 unread messages

### 12.2 End-to-End Tests (Agent → User → Agent Flows)

- **Agent notifies user:**
  - Orchestrator spawns a worker agent
  - Worker completes a task and calls `send_message(destination="user", message="Task done!")`
  - User sees notification in Agent Messages tab with "worker1" as sender

- **User replies via @mention:**
  - User clicks the message bubble (auto-inserts @worker1)
  - User types additional instructions and sends
  - Verify worker receives the message and responds

- **Multi-agent coordination visible to user:**
  - Agent A sends notification to user: "Found issue in file X"
  - Agent B sends notification: "I can fix this"
  - User sees both messages in chronological order in Agent Messages tab
  - User replies with `@AgentB please fix it`

- **Cross-tab behavior preserved:**
  - User is on an agent's direct tab (e.g., sub-Maine)
  - Sends a message — verify it still goes to Maine (existing behavior unchanged)
  - Agent sends notification to user via send_message
  - Verify badge appears but user's current tab view is not disrupted

### 12.3 Integration with Existing Tests

Add the following scenarios to the existing test plan sections:

**To Section 8.2 Integration Tests:**
- **Agent-to-user + reply loop:** Agent calls send_message("user") → frontend displays → user replies via @mention → backend routes to correct agent → agent responds

**To Section 8.3 Manual Testing Checklist:**
- [ ] Notification appears in Agent Messages tab with correct sender/timestamp
- [ ] Badge shows unread count and clears on tab view
- [ ] Clicking a message inserts @mention into input
- [ ] @mentioned messages route to the correct agent
- [ ] Messages without @mention in Agent Messages tab go to orchestrator
- [ ] Direct agent tab messaging behavior is unchanged

---

## 13. Updated Dependencies & Ordering

### Backend Tasks (Existing, Reviewed)

1. **Step 1** — Create tool class `send_message.py` (independent)
2. **Step 2** — Register in DNA metadata `dna.py` (depends on Step 1)
3. **Step 3** — Ensure pool injection works in `agent_factory.py` (depends on Step 1)
4. **Step 4** — Backend WebSocket event infrastructure (no changes needed; uses existing infra)

### Frontend Tasks (New)

5. **Step 5a** — Add CSS styles for Agent Messages tab and badges (independent, can run in parallel with backend)
6. **Step 5b** — Implement state management functions (`loadAgentMessages`, `saveAgentMessages`, etc.) in `app.js` (depends on agreed storage format from Step 1)
7. **Step 5c** — Implement tab initialization and rendering (`initAgentMessagesTab`, `renderAgentMessages`, `createAgentMessageEl`) (depends on Step 5b)
8. **Step 5d** — Add WebSocket event handler for `agent_message_to_user` in `handleServerMessage` (depends on Step 5b, backend Step 1 complete for testing)
9. **Step 5e** — Implement @mention system (`parseMentionRouting`, `insertAgentMention`) and modify `sendMessage` routing logic (depends on Step 5c)
10. **Step 5f** — Implement unread badge and tab-switch mark-as-read behavior in `switchMainTab` (depends on Steps 5b, 5d)

### Testing Phase

11. **Step 6** — Integration testing: full agent→user→agent flow with @mentions (depends on all backend + frontend steps complete)

### Estimated Effort

- Backend: ~2-3 hours (Steps 1-4, already scoped)
- Frontend: ~3-4 hours (Steps 5a-5f)
- Testing: ~1-2 hours (Step 6)
- **Total: ~6-9 hours**

### Parallelization Opportunities

- CSS styling (5a) can be done independently
- State management + rendering (5b, 5c) can start as soon as the WebSocket event format is finalized (already defined in Section 5.2 of this plan)
- @mention system (5e) is independent of backend implementation — only depends on frontend state structure
