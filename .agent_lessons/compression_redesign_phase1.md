# Lessons — Compression Redesign Phase 1

## What Was Done (Phase 1)

Created the foundation files for the compression system redesign at `agent_cascade/compression/`:

### Files Created

| File | Purpose |
|------|---------|
| `result.py` | `CompressResult` dataclass — structured return type with success, summary_text, marker_message, messages_discarded, tail_count, error, mode |
| `helpers.py` | Three helper functions: `compute_discard_count()`, `build_marker_message()`, `rebuild_working_set()` |
| `agent_invoker.py` | `invoke_compression_agent()` — encapsulates the Compression Agent invocation pattern from compression_tools.py |
| `core.py` | `compress_context()` — the unified synchronous function that handles all compression triggers |
| `__init__.py` | Package init exporting `CompressResult` and `compress_context` |

## Key Design Decisions

### 1. Synchronous, Not Async
The entire orchestrator pipeline is generator-based (uses `yield from`), not asyncio. `compress_context()` is synchronous and uses generator iteration to invoke the Compression Agent — matching the existing `_stream_sub_agent_call` pattern.

### 2. Clean Trim Model
Unlike the old cumulative design (which inserted markers but kept all old messages), the new design **actually deletes** discarded messages from the pool:
```python
del history[active_start_idx : insert_pos]   # trim
history.insert(insert_pos, marker_message)    # insert
```

### 3. Fail-Safe Pool Mutation
Pool is only mutated AFTER a valid summary is obtained. If compression fails at any point (agent error, empty summary, etc.), pool is untouched.

### 4. Agent Invoker Pattern
The invoker uses `comp_agent.run(history, agent_instance_name='compression_agent')` directly (same as existing code). While the plan mentions using `call_agent`, the `_stream_sub_agent_call` machinery in the orchestrator is a generator that yields through the orchestrator chain — our `compress_context()` runs synchronously inside tool calls, not within the generator chain. The direct `comp_agent.run()` pattern is correct here.

### 5. Guard Validation Order
The validation order matters (from the plan):
1. Manual mode needs summary_text (checked first)
2. Active set exists
3. Not already optimally compressed (<3 msgs AND <200 tokens)
4. Discard count > 0 (unless force=True)

### 6. Existing Summary Compounding
When there's already a compression marker in the pool, the existing summary text is extracted from between `<context_summary>` tags and passed to the Compression Agent as context for compounding. This prevents losing information across multiple compressions.

### 7. Message Format
Messages in the agent_pool are stored as dicts (with 'role', 'content' keys). The marker_message is built as a dict `{'role': USER, 'content': summary_text}` — matching the existing pool format.

## Important Code Patterns to Follow

- **Pool is single source of truth**: After compression, callers rebuild their working sets via `rebuild_working_set()` which does `copy.deepcopy()` from pool state
- **Logger notification**: `insert_compression_marker(marker_message, tail_count)` — log preserves full history while pool is trimmed
- **tail_count semantics**: Number of messages AFTER the marker. Logger computes `insert_pos = len(log_history) - tail_count`

## What Comes Next (Phases 2-4)

- Phase 2: Wire in — replace all 4 code paths with calls to compress_context()
- Phase 3: Cleanup — remove dead code, simplify pool methods
- Phase 4: Testing & validation