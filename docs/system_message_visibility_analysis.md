# System Message Visibility Analysis

## Executive Summary

The system message (first message in the agent's conversation) is not visible at the top of the chat when a session loads if the agent is **currently RUNNING** and has **more than 50 messages** in its conversation. The root cause is the **tail optimization** in `_serialize_instance()` being incorrectly triggered during the **initial full state broadcast** from `build_state_from_pool()`.

---

## 1. Root Cause

### The Bug: `streaming=True` Passed During Full State Build

**File:** `agent_cascade/api_integration.py`, line 450

```python
# In build_state_from_pool()
all_instances[name] = _serialize_instance(inst, pool, include_messages=True, streaming=True, streaming_responses=inst_streaming)
```

`build_state_from_pool()` **unconditionally** passes `streaming=True` to `_serialize_instance()`, regardless of whether the agent is actually streaming or not. This causes the tail optimization to activate during full state snapshots when:

1. `streaming=True` (always passed by `build_state_from_pool()`)
2. `current_state == AgentState.RUNNING` (agent is actively generating)
3. `len(msgs) > 50` (conversation has more than 50 messages)

### The Tail Optimization

**File:** `agent_cascade/api_integration.py`, lines 1072-1081

```python
if streaming and current_state == AgentState.RUNNING and len(msgs) > 50:
    # During active generation only send the tail for large conversations (>50 messages)
    tail_size = max(5, len(msgs) // 10)  # Send at least 10% or 5 messages as tail
    start_idx = max(0, len(msgs) - tail_size)
    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs[-tail_size:], start_idx)]
    result['is_partial'] = True
```

When the tail optimization triggers, only the **last 10%** of messages (minimum 5) are serialized. The system message at **index 0** is excluded.

---

## 2. Detailed Flow Analysis

### Scenario A: Fresh Session, Agent NOT Generating (System Message Visible ✓)

```
WebSocket connects
  → build_state() [generating=False]
    → build_state_from_pool()
      → _serialize_instance(streaming=True)
        → current_state == IDLE (not RUNNING)
        → Tail optimization NOT triggered (condition 2 fails)
        → ALL messages serialized including system message
  → Frontend receives full state with system message ✓
```

### Scenario B: Fresh Session, Agent IS Generating with >50 Messages (System Message Hidden ✗)

```
WebSocket connects
  → build_state() [generating=True]
    → build_state_from_pool()
      → _serialize_instance(streaming=True)
        → current_state == RUNNING ✓
        → len(msgs) > 50 ✓
        → Tail optimization TRIGGERED
        → Only last 10% of messages serialized
        → System message at index 0 EXCLUDED ✗
  → Frontend receives partial state (is_partial=True) without system message
  → System message never appears until force_full broadcast (~10 seconds later)
```

### Scenario C: Existing Session, Agent NOT Generating (System Message Visible ✓)

Same as Scenario A. Tail optimization doesn't trigger because agent is IDLE.

### Scenario D: Existing Session, Agent IS Generating with >50 Messages (System Message Hidden ✗)

Same as Scenario B. The initial state broadcast is partial.

---

## 3. Why `force_full` Doesn't Always Fix It

The `force_full` mechanism in `build_stream_update_from_pool()` is designed to periodically send full state snapshots:

**File:** `agent_cascade/run_agent_unified.py`, lines 192-200

```python
force_full = (tick_num % 100 == 0)  # Every 100 ticks (~10 seconds)
stream_update = build_stream_update_from_pool(
    pool=pool,
    instance_name=instance_name,
    responses=turn_output,
    force_full=force_full,
)
```

**File:** `agent_cascade/api_integration.py`, lines 648-655

```python
is_full_refresh = force_full
if name == instance_name or current_version != _last_stream_versions.get(name) or is_full_refresh:
    all_instances[name] = _serialize_instance(inst, pool, include_messages=True, streaming=(not is_full_refresh), ...)
```

When `force_full=True`, `streaming=False` is passed, disabling the tail optimization.

**However**, this only works during **active generation** because:

1. `build_stream_update_from_pool()` is only called inside the streaming tick loop (`run_agent_thread_unified`)
2. The tick loop only broadcasts when `should_broadcast` is True (line 181-186):
   ```python
   should_broadcast = (
       is_streaming_tick
       or len_changed
       or has_tool_event
       or (now - last_send > 0.1)
   )
   ```
3. If the agent is IDLE, no stream updates are sent at all
4. If the agent is RUNNING but no messages changed, the cached instance data is reused (line 658-659)

**Result:** If the initial state broadcast is partial, the system message may not appear for up to ~10 seconds (or indefinitely if the agent is IDLE and the user never sees the initial state).

---

## 4. Frontend Merge Behavior

**File:** `web_ui/app.js`, lines 982-988

```javascript
// Merge ALL agent_instances including root — single source of truth, no legacy fallbacks
if (data.agent_instances) {
  for (const [name, sa] of Object.entries(data.agent_instances)) {
    state.subAgents[name] = sa;
  }
  cleanupStaleSubAgents(data, state);
}
```

The frontend **replaces** `state.subAgents[name]` entirely with the server data. It does NOT merge message arrays. This means:

- If the initial state is partial, the system message is lost from `state.subAgents`
- Partial stream updates (`is_partial=True`) are merged with existing messages (lines 1175-1220), but only if the frontend already has a full state to merge with
- If the initial state was partial, there's no prior state to merge with, so the system message is never recovered until a `force_full` broadcast

**File:** `web_ui/app.js`, lines 1175-1220 (partial merge logic)

```javascript
if (sa.is_partial) {
  if (existing && existing.messages) {
    // Merge partial messages with existing state
    // ...
  } else {
    // Fallback: if we don't have existing state, we can't merge partials
    state.subAgents[name] = sa;  // Just replaces with partial
  }
}
```

---

## 5. Rendering Confirmation

The frontend DOES render system messages correctly:

**File:** `web_ui/app.js`, line 2616-2617

```javascript
// Pool mirror: show ALL messages including system prompt — no filtering
const displayMsgs = msgs;
```

**File:** `web_ui/app.js`, line 1504-1505

```javascript
function msgClass(role) {
    return `message msg-${role}`;  // CSS class-based role differentiation
}
```

**File:** `web_ui/styles.css`, lines 1095-1100

```css
.msg-system {
  align-self: center;
  background: var(--system-bg);
  border-color: var(--system-border);
  border-left: none;
  font-size: 13px;
}
```

System messages are styled and visible — they just aren't being **received** from the server.

---

## 6. Recommended Fix

### Primary Fix: Change `streaming=True` to `streaming=False` in `build_state_from_pool()`

**File:** `agent_cascade/api_integration.py`, line 450

**Before:**
```python
all_instances[name] = _serialize_instance(inst, pool, include_messages=True, streaming=True, streaming_responses=inst_streaming)
```

**After:**
```python
all_instances[name] = _serialize_instance(inst, pool, include_messages=True, streaming=False, streaming_responses=inst_streaming)
```

**Rationale:**
- `build_state_from_pool()` is used for **full state snapshots** (initial broadcast, session load, final state)
- Full state snapshots should include ALL messages, not just the tail
- The `streaming` parameter controls the tail optimization — it should be `False` for full state builds
- Streaming responses are still appended after the main serialization (lines 1087-1116), regardless of the `streaming` flag

**Impact:**
- Initial state broadcasts will always include all messages (including system message)
- No performance impact: `streaming=False` means the tail optimization is skipped, but for full state builds this is the intended behavior
- The `is_partial` flag will be `False` for full state builds, telling the frontend to replace the entire message array

### Secondary Fix (Optional): Add `streaming` Parameter to `build_state_from_pool()`

For better API clarity, add an explicit `streaming` parameter:

**File:** `agent_cascade/api_integration.py`, lines 381-386

**Before:**
```python
def build_state_from_pool(
    pool: AgentPool,
    instance_name: str,
    responses: Optional[List[Message]] = None,
    generating: bool = False,
) -> Optional[Dict[str, Any]]:
```

**After:**
```python
def build_state_from_pool(
    pool: AgentPool,
    instance_name: str,
    responses: Optional[List[Message]] = None,
    generating: bool = False,
    streaming: bool = False,  # NEW: controls tail optimization
) -> Optional[Dict[str, Any]]:
```

Then use this parameter instead of hardcoding `streaming=True`:

```python
all_instances[name] = _serialize_instance(inst, pool, include_messages=True, streaming=streaming, streaming_responses=inst_streaming)
```

This provides explicit control over whether the tail optimization should apply.

---

## 7. Verification Steps

After applying the fix:

1. **Test 1:** Start a new session, send messages until conversation >50, then start generation. Connect WebSocket and verify system message appears.
2. **Test 2:** Load an existing session with >50 messages. Connect WebSocket and verify system message appears.
3. **Test 3:** Verify that streaming updates during active generation still use the tail optimization (for performance).
4. **Test 4:** Verify that `force_full` broadcasts still work correctly.

---

## 8. Summary Table

| Component | File | Line(s) | Issue |
|-----------|------|---------|-------|
| **Root Cause** | `api_integration.py` | 450 | `streaming=True` passed during full state build |
| **Tail Optimization** | `api_integration.py` | 1072-1081 | Triggers when `streaming=True AND RUNNING AND >50 msgs` |
| **Force Full** | `api_integration.py` | 648-655 | Works but only during active generation |
| **Frontend Merge** | `app.js` | 982-988 | Replaces entire agent data, no message array merge |
| **Frontend Partial** | `app.js` | 1175-1220 | Merges partials only if existing state exists |
| **Rendering** | `app.js` | 2616-2617 | Correctly renders ALL messages including system |
| **CSS** | `styles.css` | 1095-1100 | System messages styled, not hidden |

---

## 9. Conclusion

The system message visibility issue is a **server-side serialization bug**, not a frontend rendering issue. The fix is a one-line change in `api_integration.py` line 450: change `streaming=True` to `streaming=False` in the `_serialize_instance()` call within `build_state_from_pool()`.

This ensures that full state snapshots (initial broadcast, session load, final state) always include all messages, while streaming updates continue to use the tail optimization for performance.