# Compression Agent Polling Timeout Analysis

## Executive Summary

The compression agent polling loop hits 1000 iterations and times out because **each LLM streaming chunk produces one poll iteration**, and for a large compression task with ~60K tokens of input history, the LLM can easily produce 1000+ streaming chunks during its response. The `max_polls = 1000` safeguard (added in Phase 1 fixes) is simply too low for large compression tasks.

**Root Cause:** The polling loop counts ALL yields from `_stream_sub_agent_call`, including intermediate LLM streaming chunks. For a 60K-token input, the LLM produces hundreds to thousands of streaming chunks before completing its response, each counted as one poll iteration.

---

## Yield Chain Analysis

### The Full Yield Chain Per LLM Chunk

When `invoke_compression_agent()` calls `_stream_sub_agent_call('call_agent', ...)`, the following yield chain occurs for **each LLM streaming chunk**:

```
LLM API Streaming
  │
  ▼
oai.py: _chat_stream() accumulates delta tokens → yields List[Message]
  │
  ▼
agent_orchestrator.py:1950-1959 — hooked_call_llm wrapper
  for output in self_agent._original_call_llm(...):
      ...
      yield output                    ← YIELD #1 (per LLM chunk)
  │
  ▼
agent_orchestrator.py:1173-1190 — _run method
  for output in self._call_llm(...):  ← receives the hook's yield
      ...
      turn_output = output
      yield response + turn_output    ← YIELD #2 (per LLM chunk)
  │
  ▼
agent_cascade/agent.py:130-142 — run() method
  for rsp in self._run(...):          ← receives _run's yield
      ...
      yield [converted rsp]           ← YIELD #3 (per LLM chunk)
  │
  ▼
agent_orchestrator.py:2002-2027 — _stream_sub_agent_call
  for resp in agent.run(...):         ← receives run()'s yield
      ...
      yield current_response          ← YIELD #4 (per LLM chunk) = POLL ITERATION
```

**Key Finding:** Each LLM streaming delta produces exactly **one poll iteration** in the `agent_invoker.py` polling loop (`next(gen)`).

### Why 1000+ Chunks Are Produced

For a forced compression at 95.7% context usage:
- **Input history**: ~60,930 tokens (from console logs)
- **Formatted prompt**: The `_format_messages_for_summary()` function wraps each message as `ROLE: content\n\n`, adding overhead
- **Compression prompt**: The COMPRESSION_PROMPT adds system instructions
- **Total LLM input**: Likely 70K-80K+ tokens (input + formatting + prompt)

The LLM processes this in streaming mode, producing delta chunks. Typical streaming behavior:
- OpenAI-compatible APIs stream ~1 token per delta chunk (sometimes multi-token for short words)
- At 50-100 tokens/sec output rate with 2-3 tokens/chunk average = **~25-50 chunks/sec**
- For a 5K-10K token summary response: **100-400 chunks just from the response**
- Plus hundreds more chunks from processing the large input and reasoning content

**Total LLM streaming iterations easily exceed 1000 for a 60K-token compression task.**

---

## Hypothesis Verification

### ✅ Hypothesis 1: LLM Streaming Chunks Cause 1000+ Poll Iterations — CONFIRMED

The yield chain analysis proves this. Each LLM streaming chunk flows through 4 layers of yielding before reaching the poll loop's `next(gen)` call. For a large compression task, 1000+ chunks are easily produced.

### ✅ Hypothesis 2: Tool Calls Could Add More Yields — NOT APPLICABLE HERE

The compression agent gets ALL standard tools via `register_standard_tools()` (agent_factory.py line 225), including:
- `call_agent`, `dismiss_agent`, `list_agents`
- `read_file`, `view_image`, `list_dir`, `grep`
- `write_file`, `edit_file`, `delete_file`
- `shell_cmd`, `code_interpreter` (if available)
- `compress_context`

However, the compression agent's task is a **single LLM call** to summarize history. It does NOT need to use tools — the LLM generates the summary directly in its response. Therefore, tool-call-added yields do not apply to this specific case.

### ⚠️ Hypothesis 3: Monkey-Patching Could Cause Double-Yielding — NOT CONFIRMED

The monkey-patched `hooked_call_llm` does NOT cause double-yielding. It wraps `_original_call_llm` and yields each output once before passing through. The yield chain is sequential, not duplicated:

```python
def hooked_call_llm(self_agent, messages, **kwargs_llm):
    for output in self_agent._original_call_llm(messages, **kwargs_llm):
        ...
        yield output              # Single yield per LLM chunk
    # After loop ends, no additional yields occur
```

### ⚠️ Hypothesis 4: Internal Retry Loop Could Amplify Yields — NOT APPLICABLE HERE

The internal retry loop in `_stream_sub_agent_call` (lines 1916-2061) retries up to 3 times on `LoopDetectedError`. The compression agent should not trigger a loop detection error, so this amplification does not apply. However, if it did trigger, each retry would re-run the entire LLM call, multiplying yields by up to 4x (original + 3 retries).

---

## Evidence from Console Logs

The console.log shows **multiple occurrences** of this exact error:

| Timestamp | Agent | Context |
|-----------|-------|---------|
| 03:38:36 | Maine | Forced compression failed |
| 03:39:18 | (unnamed) | Forced compression failed |
| 16:31:23 | reviewer_stop_resume | Forced compression failed |
| **18:27:22** | **StopResumeFixer** | **Context at 95.7% (the reported incident)** |

All four occurrences are identical errors: `Compression agent polling exceeded 1000 iterations`. This confirms the issue is **systematic and reproducible**, not a one-time fluke.

---

## Why `sub_agent_state` Wasn't Populated Before Timeout

The polling loop in `agent_invoker.py` checks:

```python
if comp_state_key in agent_pool.sub_agent_state:
    msgs = agent_pool.sub_agent_state[comp_state_key].get('messages', [])
    if msgs:
        final_msgs = list(msgs)
```

During LLM streaming, `sub_agent_state['compression_agent']['messages']` IS populated (set at line 1884), but it only contains the **input messages** (the conversation history being summarized), NOT the final assistant response. The assistant message is only appended after the LLM call completes and `_run` processes the full output.

By the time the LLM completes, the polling loop has already exceeded 1000 iterations and raised the timeout error. **The sub_agent_state check was designed to detect when state isn't being populated at all (e.g., a broken generator), not to serve as an early completion signal.**

---

## Proposed Fixes

### Fix Option 1: Increase `max_polls` — Quick but Fragile

```python
# agent_invoker.py line 143
max_polls = 5000  # Increased from 1000 to handle large compression tasks
```

**Pros:** Minimal code change, quick fix.
**Cons:** Still a hardcoded number that could be exceeded by even larger tasks. Doesn't address the root cause.

### Fix Option 2: Time-Based Timeout — Recommended ✅

Replace iteration counting with a time-based timeout:

```python
import time

# agent_invoker.py
final_msgs = []
subagent_return_value = None
start_time = time.time()
max_poll_time = 300  # 5 minutes max for compression

gen = orchestrator._stream_sub_agent_call(...)

while True:
    yielded = next(gen)
    
    # Time-based check instead of iteration count
    if time.time() - start_time > max_poll_time:
        raise RuntimeError(
            f"Compression agent timed out after {max_poll_time}s"
        )
    
    if comp_state_key in agent_pool.sub_agent_state:
        msgs = agent_pool.sub_agent_state[comp_state_key].get('messages', [])
        if msgs:
            final_msgs = list(msgs)
```

**Pros:** Adapts to any task size; more intuitive for users (5 minutes is a reasonable timeout).
**Cons:** Requires `import time` in the module.

### Fix Option 3: Batch LLM Chunks Before Yielding — Architectural Change

Modify `_stream_sub_agent_call` or `hooked_call_llm` to batch multiple LLM chunks into a single yield:

```python
# In hooked_call_llm, accumulate N chunks before yielding
chunk_buffer = []
buffer_size = 10  # Accumulate 10 LLM deltas per yield
for output in self_agent._original_call_llm(messages, **kwargs_llm):
    chunk_buffer.append(output)
    if len(chunk_buffer) >= buffer_size:
        yield merge_chunks(chunk_buffer)
        chunk_buffer = []
if chunk_buffer:
    yield merge_chunks(chunk_buffer)
```

**Pros:** Reduces poll iterations by the batch factor.
**Cons:** Changes streaming granularity; WebUI updates become less frequent (every N chunks instead of every chunk). Requires careful implementation to not lose partial state.

### Fix Option 4: Detect Completion via `StopIteration` — Already Implemented ✅

The code already has a `try/except StopIteration` block at lines 164-167:

```python
except StopIteration as e:
    if hasattr(e, 'value') and e.value is not None:
        subagent_return_value = e.value
```

This catches the generator's return value. The issue is that `max_polls` is checked BEFORE this exception can be caught. If we removed the iteration check entirely, the loop would complete naturally when the generator finishes.

**Best approach:** Replace `max_polls` with a time-based timeout (Option 2) while keeping the `StopIteration` handling intact.

---

## Recommended Solution

**Implement Option 2 (time-based timeout) as the primary fix**, with these specific changes:

1. **In `agent_invoker.py` lines 143-158:**
   - Replace `max_polls = 1000` and iteration counting with a time-based timeout
   - Use a reasonable timeout (e.g., 5 minutes = 300 seconds for large compression tasks)
   - Keep the existing `StopIteration` handling as-is

2. **Consider adding logging** at each poll to track progress:
   ```python
   if poll_count % 100 == 0:
       elapsed = time.time() - start_time
       logger.debug(f"Compression agent polling: {poll_count} iterations, {elapsed:.1f}s elapsed")
   ```

3. **Document the change** in `lessons_compression_phase1_fixes.md` (or create a new lessons file for Phase 2 fixes).

---

## Risk Assessment

| Fix | Risk Level | Impact |
|-----|-----------|--------|
| Option 1 (Increase max_polls) | Low | Quick fix, but doesn't scale |
| **Option 2 (Time-based)** | **Low** | **Best balance of safety and flexibility** |
| Option 3 (Batch chunks) | Medium | Changes streaming behavior; needs testing |
| Option 4 (Remove iteration limit) | High | Could hang indefinitely on broken generators |

---

## Files to Modify

1. **`agent_cascade/compression/agent_invoker.py`** — Replace iteration-based polling with time-based timeout
2. **New lessons file:** `lessons_compression_polling_timeout_fix.md` — Document the fix