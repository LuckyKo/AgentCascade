# Streaming Bubble Race Condition Analysis

## Bug Description
New chat bubbles are created before the current message bubble has finished receiving all its streamed content. The user reports that bubbles should always be fully filled before the next one appears.

---

## 1. Server-Side Streaming Architecture

### Key Files: `api_server.py`, `agent_orchestrator.py`, `agent_cascade/llm/oai.py`

#### The LLM Streaming Pipeline

```
LLM API → oai.py:_chat_stream() → base.py:_postprocess_messages_iterator()
        → agent_orchestrator.py:run() → api_server.py:broadcast() → WebSocket → Client
```

**Step-by-step flow:**

1. **LLM streaming (oai.py lines 276-350)**: Each tick accumulates text/content/tool_calls into `full_response`, `full_reasoning_content`, and `full_tool_calls`. A list of Message objects is yielded:
   ```python
   res = []
   if full_reasoning_content or full_response:
       res.append(Message(role=ASSISTANT, content=full_response, ...))
   if full_tool_calls:
       res += full_tool_calls
   yield res  # Can be empty on first tick!
   ```

2. **Orchestrator streaming (agent_orchestrator.py lines 1124-1142)**:
   ```python
   turn_output = output  # From LLM streaming
   yield response + turn_output  # response = accumulated history from previous turns
   ```

3. **API server throttling (api_server.py lines 1060-1112)**: Stream updates are sent every ~150ms OR on events:
   ```python
   if now - last_send > 0.15 or stack_changed or len_changed or has_tool_event:
       delta = build_stream_update(responses, ...)
       send_queue.put({'type': 'stream_update', **delta})
   ```

4. **Message serialization (api_server.py lines 774)**: Each response message is serialized and sent as `response_messages`.

---

## 2. Client-Side Rendering Architecture

### Key File: `web_ui/app.js`

#### Message State Management

```javascript
// Lines 839-843: Merge incoming stream_update
if (historyCount <= state.messages.length) {
    state.messages.length = historyCount;  // Truncate if needed
}
state.messages.push(...responseMsgs);  // Append new messages
```

#### Bubble Creation Logic (renderMessages, lines 1069-1157)

```javascript
// Lines 1110-1123: Create bubbles for NEW messages
if (currentCount > lastRenderedCount) {
    for (let i = lastRenderedCount; i < currentCount; i++) {
        container.appendChild(createMessageEl(msgs[i], i));
    }
    lastRenderedCount = currentCount;
}

// Lines 1125-1132: Update EXISTING message bubble (streaming)
if (lastContent !== lastLastContent && container.lastElementChild) {
    updateBubbleContent(lastBubble, msgs[currentCount - 1]);
}
```

#### Incremental Content Update (updateBubbleContent, lines 1337-1400)

```javascript
// Lines 1354-1367: Incremental streaming update
if (curContent.startsWith(prevContent)) {
    const newText = curContent.slice(prevContent.length);
    contentDiv.insertAdjacentHTML('beforeend', newHtml);  // O(1) append
}
```

---

## 3. Message Boundary Communication

**There is NO explicit message boundary signal during streaming.** Boundaries are implicit:

- **Server side**: `response` list grows with each LLM turn's output (via `response.extend(output)` at line 1220)
- **Client side**: New messages appear when `state.messages.length` increases
- **No message IDs or "end of turn" markers** are sent to the client

The only explicit boundary signal is `finish_reason` in the LLM output's `extra` field, but this is used server-side for truncation detection, not communicated as a "message complete" signal.

---

## 4. The Specific Flow: Multi-Turn Streaming

### Scenario: Assistant speaks → calls tool → speaks again

**Turn 1:**
```
LLM streaming ticks:
  Tick 1: turn_output = [Assistant(content="")]          ← empty content!
  Tick 2: turn_output = [Assistant(content="Let me ")]
  Tick 3: turn_output = [Assistant(content="Let me search for that."), ToolCall(name="search", args="{\"query":")]
  Tick 4: turn_output = [Assistant(content="Let me search for that."), ToolCall(name="search", args='{"query":"X"}')]

Server yields (after throttling):
  Update N: response_msgs = [...history, Assistant("Let me..."), ToolCall(search)]
```

**Tool Execution:**
```python
# Line 1257: Yield tool call before execution
yield response  # Client sees assistant text + tool call bubble

# After tool completes, append result
response.append(fn_msg)  # FunctionResult
yield response  # Client sees all 3 bubbles fully populated
```

**Turn 2:**
```
LLM streaming ticks:
  Tick 1: turn_output = [Assistant(content="")]          ← empty content again!
  Tick 2: turn_output = [Assistant(content="Here's ")]
  
Server yields:
  Update N+1: response_msgs = [...history, Assistant("Let me..."), ToolCall, FnResult, Assistant("")]
  Update N+2: response_msgs = [...history, Assistant("Let me..."), ToolCall, FnResult, Assistant("Here's ")]
```

---

## 5. Root Cause Analysis

### Primary Bug: First-Tick Empty Message Yielding

**Location**: `agent_cascade/llm/oai.py` lines 337-350

**The Problem**: On the very first tick of any LLM streaming turn, `full_response = ''` and `full_reasoning_content = ''`. The condition at line 337:
```python
if full_reasoning_content or full_response:
    res.append(Message(role=ASSISTANT, content=full_response, ...))
```

When both are empty strings (truthy because they're non-empty string objects), this evaluates to `False` on the first tick. However, the orchestrator still processes this tick and may yield a message with empty content.

**But more critically**, the orchestrator at line 1141-1142:
```python
turn_output = output  # This is the list from LLM streaming
yield response + turn_output
```

If `output` contains an Assistant message with empty content (which happens when the LLM produces a tool call without preceding text), it gets yielded immediately. The server's throttling logic at line 1060 checks `len_changed`:
```python
len_changed = (resp_len != last_resp_len)
```

When a new assistant message (even with empty content) is added, `resp_len` increases, triggering an immediate stream_update.

### Secondary Bug: No "Message Complete" Gate Before Next Bubble

**Location**: `web_ui/app.js` lines 1110-1123

The client creates bubbles for ALL new messages in a single `renderMessages()` call:
```javascript
for (let i = lastRenderedCount; i < currentCount; i++) {
    container.appendChild(createMessageEl(msgs[i], i));
}
```

There's no mechanism to wait for the previous message to be "complete" before creating the next bubble. All messages in `response_messages` are treated as independent and rendered immediately.

### Why This Manifests as "Bubble Not Full Before Next"

Consider this timeline:

1. **T+0ms**: Turn 1 ends. Client renders bubbles for all completed messages.
2. **T+0.5ms**: Turn 2 starts. LLM streaming tick 1 yields `[Assistant(content="")]`.
3. **T+5ms**: Server detects `len_changed` (new message added). Sends stream_update.
4. **T+10ms**: Client receives update with 5 messages (Turn 1's 4 + Turn 2's empty Assistant).
5. **T+10ms**: `renderMessages()` creates bubble #5 for the empty content message.
6. **T+10-200ms**: Bubble #5 sits empty or with minimal content while streaming continues.
7. **T+160ms**: Next stream_update arrives with more content for bubble #5.

The user sees bubble #5 appear before it has meaningful content. Meanwhile, if the user's attention is on bubble #4 (which was already complete), they might perceive that "bubble #4 wasn't fully filled" because their attention shifted to the new bubble #5.

### Edge Case: LLM Produces Tool Call Without Preceding Text

When the LLM generates a tool call directly (no text preamble):
```python
# First meaningful tick:
res = []  # No assistant message with content
if full_tool_calls:  # Tool calls are present
    for tc in full_tool_calls:
        res.append(tc)
yield res  # Only tool calls, no assistant text message
```

In this case, `turn_output` has NO assistant message — only the tool call. The next tick might produce an assistant message with streaming content. But if the orchestrator's loop processes these out of order or if there's a timing gap, the client could see:

1. Tool call bubble created (from turn 1)
2. New assistant text bubble appears (from turn 2, before any text accumulated)

---

## 6. Conclusion and Recommendations

### Where the Bug Originates

**Primary origin: Client-side rendering (`web_ui/app.js`)**. The client creates bubbles for ALL incoming messages simultaneously without waiting for content accumulation. This is a design choice that optimizes for responsiveness but can create the perception of incomplete bubbles.

**Contributing factor: Server-side first-tick empty message yielding (`agent_cascade/llm/oai.py` + `agent_orchestrator.py`)**. The LLM streaming yields messages even when content is empty, and the orchestrator immediately includes these in the response list sent to the client.

### Recommended Fixes

#### Fix 1: Suppress First-Tick Empty Messages (Server-Side)
**File**: `agent_cascade/llm/oai.py` or `agent_orchestrator.py`

Skip yielding on the first tick if the only content is an empty assistant message with no tool calls:
```python
# In orchestrator's streaming loop
if output and all(
    not m.get('content', '') and not m.get('reasoning_content') 
    for m in output if getattr(m, 'role') == ASSISTANT or (isinstance(m, dict) and m.get('role') == ASSISTANT)
):
    continue  # Skip yielding empty messages
```

#### Fix 2: Message Completion Gate (Client-Side)
**File**: `web_ui/app.js` in `updateBubbleContent`

Add a "bubble complete" check before allowing new bubbles to be created. Track whether the last bubble has received at least N characters of content or has been streaming for more than M ms:
```javascript
// In stream_update handler, before calling renderMessages:
const lastMsg = responseMsgs[responseMsgs.length - 1];
if (lastMsg && lastMsg.content === '' && state.messages.length > 0) {
    // Don't create new bubble yet — wait for content to accumulate
    return;
}
```

#### Fix 3: Explicit Message Boundary Signal (Architecture Change)
Add a `message_id` and `turn_complete` flag to stream updates so the client knows when a message is "complete" vs "streaming in progress":
```python
# Server-side
delta = {
    'response_messages': response_msgs,
    'active_message_index': len(response_msgs) - 1,  # Only this one is still streaming
    'turn_complete': False,  # Signal for when turn ends
}
```

#### Fix 4: Throttle New Bubble Creation
Add a minimum content threshold before creating a new bubble. If the last message in `response_messages` has less than N characters of content AND there are already rendered messages, defer rendering until more content arrives:
```javascript
// In stream_update handler
const responseMsgs = data.response_messages || [];
if (responseMsgs.length > 0) {
    const lastMsg = responseMsgs[responseMsgs.length - 1];
    if (lastMsg && lastMsg.content && lastMsg.content.length < MIN_CONTENT_THRESHOLD) {
        // Defer rendering — wait for more content
        return;
    }
}
```

### Priority

| Fix | Effort | Impact | Risk |
|-----|--------|--------|------|
| Fix 1: Suppress empty first-tick | Low | Medium | Low |
| Fix 4: Throttle new bubble creation | Low | High | Low |
| Fix 2: Message completion gate | Medium | Medium | Medium |
| Fix 3: Explicit boundary signal | High | High | High |

**Recommended approach**: Start with **Fix 1** (quick win, low risk) + **Fix 4** (addresses the core UX issue). These can be implemented independently and tested without architectural changes.