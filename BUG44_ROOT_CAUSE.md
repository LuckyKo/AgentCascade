# Bug 44 — Root Cause Analysis: Function Call Content Missing from Compressor Input

## Summary

When the system compresses an agent's conversation history, **function call messages (tool calls and their associated metadata) are not properly represented in the input sent to the compression agent**. The compression prompt receives only the `role` and `content` fields of each message, completely dropping:

1. **Assistant → Tool call direction**: The `function_call` field containing the tool name and arguments
2. **Tool result messages' identity**: The `name` field identifying which tool produced the result

This means the compression agent generates summaries that have no knowledge of what tools were called or with what parameters — a critical loss of operational context.

---

## Data Flow Overview

```
User/Agent Conversation (full messages with function_call, name, etc.)
  │
  ▼
compress_context() [compression/core.py:15]
  │
  ├─ get_compression_target_set() → active_set, target_messages
  │   Reads from agent_pool.get_conversation() → instance.conversation
  │   Target messages are correctly extracted as full message objects
  │   ✅ Messages ARE complete at this stage
  │
  ▼
invoke_compression_agent() [compression/agent_invoker.py:65]
  │
  ├─ _format_messages_for_summary(target_messages)  ← ROOT CAUSE HERE
  │   Converts messages to plain text for LLM prompt
  │   Only extracts: role + content
  │   ❌ DROPS: function_call, name fields
  │
  ▼
Compression Agent receives truncated history text
  └─ Summary is generated WITHOUT knowledge of tool calls
```

---

## Root Cause Location

**File**: `agent_cascade/compression/agent_invoker.py`  
**Function**: `_format_messages_for_summary()`  
**Lines**: 26–62

```python
def _format_messages_for_summary(target_messages):
    """
    Format a list of messages into plain text for the compression prompt.

    Handles both dict and Message objects, including multi-modal content lists.

    Args:
        target_messages: List of messages (dicts or Message objects) to format.

    Returns:
        A single string with role-prefixed message contents.
    """
    history_text = ""
    for msg in target_messages:
        if isinstance(msg, dict):
            role = msg.get('role', 'unknown').upper()
            content = msg.get('content', '')       # ← ONLY extracts content
        else:
            role = getattr(msg, 'role', 'unknown').upper()
            content = getattr(msg, 'content', '') # ← ONLY extracts content

        # Handle multi-modal content (list of items)
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get('text', '') or ''
                    if text:
                        text_parts.append(str(text))
                else:
                    text = getattr(item, 'text', None)
                    if text:
                        text_parts.append(str(text))
            content = " ".join(text_parts)
        history_text += f"{role}: {content}\n\n"
    return history_text
```

---

## What Gets Lost

### 1. Assistant Messages with Tool Calls (`function_call` field)

When the LLM decides to call a tool, the response message looks like this:

**In conversation history (Message object or dict):**
```python
# As Message object
Message(
    role="assistant",
    content="",
    function_call=FunctionCall(name="read_file", arguments='{"path": "data.txt"}'),
    extra={'function_id': 'call_abc123'}
)

# Or as dict
{
    "role": "assistant",
    "content": "",
    "function_call": {"name": "read_file", "arguments": '{"path": "data.txt"}'},
    "extra": {"function_id": "call_abc123"}
}
```

**What compression agent receives:**
```
ASSISTANT: 

```

The tool name (`read_file`) and arguments (`{"path": "data.txt"}`) are **completely lost**. The compression agent cannot know that a file was read.

### 2. Tool Result Messages' `name` Field

Tool result messages look like this:

**In conversation history:**
```python
Message(
    role="function",
    name="read_file",        # ← Identifies which tool produced this result
    content="file contents here..."
)

# Or as dict
{
    "role": "function",
    "name": "read_file",
    "content": "file contents here..."
}
```

**What compression agent receives:**
```
FUNCTION: file contents here...
```

The `name` field (`read_file`) is **lost**. The result appears as a generic function output with no association to the specific tool.

### 3. Impact on Summary Quality

Without tool call information, the compression agent's summary will:
- Not mention which tools were used
- Not include tool arguments (critical for reproducing actions)
- Have function results that appear disconnected from their calls
- Potentially omit important decisions that were based on tool outputs

---

## Why Messages Are NOT Dropped Entirely

It's important to note that the messages are **not dropped** — they pass through `compress_context()` → `get_compression_target_set()` intact. The `target_messages` list in `core.py:153-162` correctly contains full message objects including all fields.

The loss occurs **only during formatting** for the LLM prompt, not during data collection or pool management.

---

## Supporting Evidence from Codebase

### 1. Function calls are stored correctly in conversation history

In `execution_engine.py:932-940`, tool result messages are properly constructed with `name` field:
```python
fn_msg = Message(
    role=FUNCTION,
    name=tool_name,           # ← Name IS set
    content=tool_result,
    extra={
        'function_id': extra_data.get('function_id', '1'),
        ...
    }
)
messages.append(fn_msg)
llm_messages.append(fn_msg)
instance.conversation.append(fn_msg)
```

### 2. Assistant tool call messages are stored with `function_call` field

In `oai.py:417-421`, the OpenAI transport layer creates assistant messages with `function_call`:
```python
result.append(
    Message(role=ASSISTANT,
            content='',
            function_call=FunctionCall(name=tc_name,
                                       arguments=tc_args),
            extra={'function_id': tc_id})
)
```

### 3. The `_detect_tool` function properly reads `function_call` from messages

In `execution_engine.py:2260-2273`:
```python
def _detect_tool(self, message: Message) -> Tuple[bool, str, Any, str]:
    func_call = (message.get('function_call') if isinstance(message, dict)
                 else getattr(message, 'function_call', None))
    ...
    if func_call:
        if isinstance(func_call, dict):
            return True, func_call.get('name'), func_call.get('arguments'), text
```

This confirms `function_call` is a real field that the rest of the system reads correctly.

### 4. The `_conv_agent_cascade_messages_to_oai` function converts `function_call` → `tool_calls` for API

In `llm/base.py:486-499`:
```python
if msg.get('function_call'):
    if not new_messages[-1].get('tool_calls'):
        new_messages[-1]['tool_calls'] = []
    fn_args = BaseChatModel._sanitize_fn_args(msg['function_call'].get('arguments', ''))
    new_messages[-1]['tool_calls'].append({
        'id': msg.get('extra', {}).get('function_id', '1'),
        'type': 'function',
        'function': {
            'name': msg['function_call']['name'],
            'arguments': fn_args
        }
    })
```

This shows the system is aware of `function_call` and knows how to handle it — but `_format_messages_for_summary` does not.

### 5. Tool-chain boundary protection exists in helpers.py

In `compression/helpers.py:39-70`, there's a "tool-chain boundary protection" feature that walks back from FUNCTION results to find paired ASSISTANT tool calls. This confirms the system **intentionally** groups tool call + result pairs for compression purposes. But the formatter still drops the actual content of those calls.

---

## The Fix

The `_format_messages_for_summary()` function needs to be enhanced to extract and include `function_call` data from assistant messages and `name` from function messages.

### Option A: Include structured tool call info in the formatted text (recommended)

```python
def _format_messages_for_summary(target_messages):
    history_text = ""
    for msg in target_messages:
        if isinstance(msg, dict):
            role = msg.get('role', 'unknown').upper()
            content = msg.get('content', '')
            function_call = msg.get('function_call')
            name = msg.get('name', '')
        else:
            role = getattr(msg, 'role', 'unknown').upper()
            content = getattr(msg, 'content', '')
            function_call = getattr(msg, 'function_call', None)
            name = getattr(msg, 'name', '')

        # Handle multi-modal content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get('text', '') or ''
                    if text:
                        text_parts.append(str(text))
                else:
                    text = getattr(item, 'text', None)
                    if text:
                        text_parts.append(str(text))
            content = " ".join(text_parts)

        # Format based on role
        if function_call:
            # Assistant with tool call
            if isinstance(function_call, dict):
                fn_name = function_call.get('name', 'unknown')
                fn_args = function_call.get('arguments', '')
            else:
                fn_name = getattr(function_call, 'name', 'unknown')
                fn_args = getattr(function_call, 'arguments', '')
            
            history_text += f"{role} (tool call: {fn_name}, args: {fn_args})\n"
        elif role == "FUNCTION":
            # Tool result with name
            history_text += f"TOOL_RESULT[{name}]: {content}\n\n"
        else:
            history_text += f"{role}: {content}\n\n"

    return history_text
```

### Option B: Pass the raw messages to the compression agent as structured input

This would require a more significant change — modifying the compression prompt and agent to accept structured message data rather than plain text.

---

## Files That Need Modification

| File | Function/Line | Change Required |
|------|--------------|-----------------|
| `agent_cascade/compression/agent_invoker.py` | `_format_messages_for_summary()` (lines 26-62) | Extract and format `function_call` from assistant messages, include `name` from function messages |

## Files That Confirm Correct Behavior (for reference)

| File | Function/Line | What It Shows |
|------|--------------|---------------|
| `agent_cascade/compression/core.py` | `compress_context()` (line 153-162) | Messages are correctly collected into `target_messages` |
| `agent_cascade/execution_engine.py` | `_execute_tool()` (lines 932-940) | Tool results are stored with `name` field |
| `agent_cascade/llm/oai.py` | `chat()` (lines 417-421) | Assistant tool calls are stored with `function_call` field |
| `agent_cascade/compression/helpers.py` | `compute_discard_count()` (lines 39-70) | Tool-chain boundary protection exists, confirming intent to preserve tool context |

---

## Severity Assessment

**Severity: High**  
**Impact**: The compression agent generates summaries that lack critical operational context. After compression, the agent may lose track of which tools were called, what parameters were used, and what results were obtained — potentially leading to incorrect decisions or inability to continue tasks that depend on prior tool results.

**Scope**: Affects all agents that use tool calls and trigger compression (either auto at >95% threshold or manual via `compress_context` tool).