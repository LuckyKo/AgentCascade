# LLM Iterator Flattening Bug in execution_engine.py

## The Bug
`'list' object has no attribute 'get'` error in `execution_engine.py` when running AC from the unified branch.

## Root Cause
The LLM's `chat()` method with `delta_stream=False` returns `Iterator[List[Message]]` — each yielded item is an **accumulated list** of messages, not individual messages. The `_call_llm_with_injection` method was yielding each list directly:

```python
for output in self._execute_llm_call(...):
    yield output  # yields List[Message] — BUG
```

This caused `turn_output = list(self._call_llm_with_injection(...))` to produce a **list of lists** (`List[List[Message]]`). Everything downstream (`_process_response`, conversation management, `_detect_tool`) expected individual `Message`/`dict` objects.

## The Fix (execution_engine.py lines 449-467)
```python
last_output = None
for output in self._execute_llm_call(...):
    last_output = output  # captured BEFORE halt check
    if self.pool.stopped or self.pool.is_instance_halted(inst_name):
        break

if not last_output:
    yield Message(role=ASSISTANT, content="[SYSTEM ERROR: Empty LLM response]")
else:
    for msg in last_output:
        yield msg  # yields individual Message objects — FIXED
```

Key points:
1. With `delta_stream=False`, each iteration yields the full accumulated response so far. Only the **last** one matters (it contains all messages).
2. `last_output = output` must be BEFORE the halt check, otherwise a mid-stream halt discards the most recent result.
3. Empty iterator handling prevents silent failures — yields an error message instead of nothing.

## How the Original Branch Handles It
In the original `agent.py`, the caller does:
```python
for rsp in self._run(messages=new_messages, **kwargs):
    for i in range(len(rsp)):  # iterates over individual messages within the list
        ...
```

The unified branch's execution engine needed the same flattening approach.

## Lesson
When integrating LLM chat with `delta_stream=False`, always remember: the iterator yields accumulated lists, not individual messages. Flatten before downstream consumers. Check type annotations — `Iterator[Message]` vs `Iterator[List[Message]]` makes this clear.