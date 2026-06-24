# OpenAI API Message Alternation Analysis for Compression Notifications & Markers

## Date: 2025-06-24
## Scope: Confirm that USER-role compression notifications/markers break OpenAI API alternation rules, and validate the `_inject_compression_warning` pattern as a viable alternative.

---

## 1. How llm_messages Are Constructed

**File:** `agent_cascade/execution_engine.py` (lines 920-1080)

The turn loop builds message lists in two stages:
1. **Phase 1 (`_setup_turn`, line 730):** Loads conversation from pool, injects system prompt with dynamic metadata, then calls `slice_history_for_llm()` to produce the working set sent to the LLM API.
2. **`slice_history_for_llm()`** (agent_pool.py lines 1486-1537): Finds compression markers, stacks them near the start, and returns `[SYSTEM] + [markers...] + [tail messages]`.

```
Pool state after forced compression:
  [0]: SYSTEM
  [1]: USER (compression marker from previous compressions)
  [...]: USER (more stacked markers if multiple compressions occurred)
  [N]:   ASSISTANT (tool call with parallel functions)
  [N+1]: FUNCTION (result for tool 1)
  [N+2]: FUNCTION (result for tool 2)
  ...
```

**No explicit role alternation validation exists.** The `validate_message_pool()` function in `pool_validation.py` checks:
- Pool not empty
- First message is SYSTEM
- No excessive duplicate consecutive messages (>10%)
- All roles are valid non-empty strings
- No unexpected types (booleans, None)

**It does NOT check that consecutive messages alternate roles.** OpenAI API requires strict alternation: `SYSTEM → USER ↔ ASSISTANT` with FUNCTION results interleaved after ASSISTANT tool calls.

---

## 2. Typical Conversation After Forced Compression

### The Flow

Forced compression happens in `_pre_llm_checks()` (line 1207-1228), which runs **before** the LLM call but **after** tool execution from the previous turn. Here's the actual sequence:

```
Turn N:
  [ASSISTANT] → "I'll read these files" + parallel tool calls
    ↓ _execute_detected_tools() appends FUNCTION results one-by-one
  [FUNCTION]  → result for tool 1 (role=FUNCTION)
  [FUNCTION]  → result for tool 2 (role=FUNCTION)   ← consecutive same-role is fine (OpenAI allows this)
  
Turn N+1:
  _pre_llm_checks() detects >95% usage
    ↓ compression_handler.execute_force_compression()
      ↓ compress_context() trims pool, inserts USER marker at line 272 of core.py
        → build_marker_message() returns Message(role=USER, ...)
      ↓ handler.py lines 249-276 injects notification message:
        → Message(role=USER, "[SYSTEM] Context exceeded...")
    ↓ _rebuild_working_set() replaces working sets with fresh pool copy
    
  Now the conversation tail looks like:
    [...FUNCTION results from turn N...]
    [USER marker]          ← inserted by compress_context at line 335 of core.py
    [USER notification]    ← appended at handler.py line 272-273
```

**Two consecutive USER messages:** The compression marker AND the notification are both `role=USER`. This is a valid alternation break.

### Parallel Tool Chain Scenario

During a single turn, multiple tools execute in sequence (lines 1866-2043):
```python
for out in turn_output:          # Each tool call from one ASSISTANT message
    fn_msg = Message(role=FUNCTION, ...)
    self._append_to_working_sets(instance, fn_msg)
```

If forced compression triggers between turns (which it does at `_pre_llm_checks`), the sequence becomes:
```
[ASSISTANT] → [FUNCTION] → [FUNCTION] → ... → [USER marker] → [USER notification]
```

**This is fine** because FUNCTION results don't need alternation — they're just appended after their parent ASSISTANT message. But the two USER messages (marker + notification) ARE consecutive same-role, which technically violates OpenAI's alternation rule.

### Async Result Injection During SLEEPING State

When waking from SLEEPING (lines 2580-2592):
```python
# Inject async results FIRST → role=USER via _make_async_result_message()
self._drain_and_inject(..., factory=self._make_async_result_message)
# THEN drain queued user messages → also role=USER via _make_user_message()
self._drain_and_inject(..., factory=self._make_user_message)
```

Multiple async results + queued messages can stack as consecutive USER messages before the next LLM call. **This is another alternation break point.**

---

## 3. The `_inject_compression_warning` Pattern

**File:** `execution_engine.py` lines 1266-1281

```python
def _inject_compression_warning(
    self, llm_messages: List[Message], usage_pct: float,
    current_tokens: int, max_tokens: int
):
    warning = (
        f"[SYSTEM WARNING: Context window at {usage_pct:.1f}% capacity "
        f"({current_tokens}/{max_tokens} tokens). "
        f"Consider using compress_context to free space.]"
    )
    self._append_system_notification(llm_messages, "[SYSTEM WARNING: Context", warning)
```

**How it works (`_append_system_notification`, lines 3188-3213):**
1. Takes the **last message in `llm_messages`** (could be SYSTEM, USER, ASSISTANT, or FUNCTION)
2. Appends the warning text to its content string: `new_content = content + f"\n\n{notification_text}"`
3. Dedup guard prevents re-injection if `[SYSTEM WARNING: Context` already exists in the message

**Key insight:** This doesn't create a new message — it **mutates the last existing message's content**. No role alternation can be broken because no new message is added. The warning lives inside an already-present message.

---

## 4. How Compression Markers Are Handled When Building llm_messages

**File:** `agent_pool.py` lines 1486-1537 (`slice_history_for_llm`)

Markers are NOT filtered out — they're **preserved and stacked**:
```python
# Find ALL marker indices
marker_indices = []
for i in range(len(history)):
    content = history[i].get('content', '') ...
    if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
        marker_indices.append(i)

# Stack markers near the start: [SYSTEM] + [markers...] + [tail]
return [history[0]] + marker_msgs + tail  # or just marker_msgs + tail (no system)
```

Markers are USER-role messages created by `build_marker_message()` in `compression/helpers.py` line 286:
```python
return Message(role=USER, content=str(content))
```

They're kept in the working set and sent to the LLM as part of context. **They're not filtered — they're structural elements.**

---

## 5. Summary of Findings

### Confirmed Issues

| Scenario | Consecutive USER Messages? | Breaks API Rules? |
|----------|---------------------------|-------------------|
| Forced compression (marker + notification) | ✅ Yes, always | Technically yes, but OpenAI merges consecutive same-role messages in newer APIs |
| Async result injection during SLEEPING wakeup | ✅ Possible if multiple results arrive | Same as above |
| Post-tool urgent message drain | ✅ Possible after FUNCTION chain | Same as above |

### The `_inject_compression_warning` Pattern

- **Viable alternative:** Appending to the last message's content avoids creating new messages entirely.
- **No alternation risk:** Since it mutates existing content rather than adding a new message, role order is preserved.
- **Dedup guard prevents spam:** The `guard_prefix` check ensures warnings don't accumulate.

### Recommendation

The forced compression notification at handler.py line 272 could use the same `_append_system_notification` pattern instead of creating a separate USER message:

```python
# Current (creates new USER message):
notification_msg = Message(role=USER, content=notification_text)
instance.append_message(notification_msg)

# Alternative (mutates last message like _inject_compression_warning does):
self.engine._append_system_notification(
    llm_messages, "[SYSTEM] Context exceeded", notification_text
)
```

**BUT note:** The notification is appended to the pool (persists across turns), while `_inject_compression_warning` only mutates `llm_messages` (local working set). If you want the notification to persist in the conversation log, you'd need to also append it to the instance's conversation via `instance.append_message()` on the mutated message or keep both paths.

### OpenAI API Notes

OpenAI's Chat Completions API:
- **Strict alternation:** SYSTEM → USER ↔ ASSISTANT (with FUNCTION interleaved after ASSISTANT)
- **Tolerant in practice:** Modern endpoints merge consecutive same-role messages automatically, but this is an implementation detail not guaranteed by the spec
- **No validation errors observed:** The system works correctly despite violations because OpenAI handles it gracefully

### Bottom Line

1. ✅ Compression markers and notifications ARE USER-role and CAN create consecutive USER sequences
2. ✅ The `_inject_compression_warning` pattern (mutating last message content) is a clean alternative that avoids the issue entirely
3. ⚠️ Forced compression notifications persist to the pool log, so switching to mutation-only would lose persistence unless handled separately