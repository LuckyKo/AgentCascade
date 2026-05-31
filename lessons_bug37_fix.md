# Bug #37 Fix: Stop Button Closes HTTP Streaming Connection

## Problem
When user clicks stop/halt, the framework sets `pool.stopped` or adds instance to `_halted_instances`. The execution engine breaks out of the Python for loop iterating over the LLM streaming response, but the underlying HTTP connection stays open — the model keeps generating tokens in the background, wasting API calls and resources.

## Root Cause
In all LLM streaming methods (`_chat_stream`), the generator returned by `response` was iterated with a bare `for chunk in response:` loop. When the caller breaks out of this loop (due to stop/halt), Python doesn't automatically close the underlying HTTP connection. The OpenAI SDK's `Stream` object has a `close()` method that must be explicitly called.

## Fix
Added `try/finally` blocks with `response.close()` calls in all streaming LLM methods:

### Modified Files (Absolute Paths)

1. **agent_cascade/llm/oai.py** — `_chat_stream()`: Wrapped the entire streaming loop (both `delta_stream=True` and `delta_stream=False` branches) in try/finally. Covers `TextChatAtOAI` and all subclasses:
   - `azure` → `TextChatAtAzure(TextChatAtOAI)` ✅
   - `qwenvl_oai` → `QwenVLChatAtOAI(TextChatAtOAI)` ✅
   - `qwenomni_oai` → `QwenOmniChatAtOAI(QwenVLChatAtOAI)` ✅

2. **agent_cascade/llm/qwen_dashscope.py** — `_delta_stream_output()` and `_full_stream_output()`: Both static methods now wrap their iteration in try/finally.

3. **agent_cascade/llm/qwenvl_dashscope.py** — `_chat_stream()`: Wrapped the streaming loop + post-loop audio processing in try/finally. Covers `QwenVLChatAtDS` and subclass:
   - `qwenvlo_dashscope` → `QwenVLoChatAtDS(QwenVLChatAtDS)` ✅

### Not Modified (No HTTP Connection)
- `openvino.py` — local model with `TextIteratorStreamer`, no HTTP
- `transformers_llm.py` — local HF model, no HTTP
- `qwenaudio_dashscope.py` — just registers a model class, no streaming logic

## Design Decisions

### try/finally (not try/except)
Used `try/finally` because we want cleanup to happen regardless of whether:
- The stream completes normally (all tokens consumed)
- The caller breaks out mid-stream (stop button)
- An exception is raised during iteration

### Nested try/finally in oai.py
The outer `try/except OpenAIError` block was already present. We added an inner `try/finally` specifically for stream cleanup:
```python
try:                          # outer: catch OpenAI errors
    response = create(...)
    try:                      # inner: ensure cleanup
        for chunk in response:
            yield ...
    finally:
        try:
            response.close()
        except Exception as e:
            logger.warning(...)
except OpenAIError:           # original error handling preserved
    raise ModelServiceError(...)
```

### Silent close failures → logged warnings
The `response.close()` is wrapped in its own try/except because:
- Older OpenAI SDK versions may not have `close()` on all response types
- DashScope responses may not implement `close()`
- After normal completion, the connection may already be closed (idempotent but could raise)

Instead of silently swallowing exceptions (`pass`), we now log a warning for observability:
```python
except Exception as e:
    logger.warning(f"Failed to close streaming response for TextChatAtOAI: {e}")
```

### Idempotency
httpx's `Response.close()` is idempotent — safe to call multiple times. This means calling `close()` after normal stream completion is harmless.

## Testing Notes
- Hard to test in CI without actual API keys/connections
- Manual testing: Start a long LLM generation, click stop button, verify no continued token generation in logs
- Look for "Failed to close streaming response" warnings only when there are genuine connection issues