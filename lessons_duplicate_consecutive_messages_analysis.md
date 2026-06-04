# Root Cause Analysis: Duplicate Consecutive Messages After Forced Compression

**Date**: 2026-05-27  
**Author**: DupDebugger  
**Status**: ✅ FIXED AND REVIEWED

---

## Executive Summary

**Root Cause: STALE MESSAGE RE-INJECTION at line 2256 of agent_orchestrator.py.** After forced compression during a sub-agent turn, the `final_resp` variable still contains messages accumulated from previous iterations of FnCallAgent._run's while loop (before compression). These messages were already synced to the pool by the pre-compression sync (lines 810-817), then potentially discarded by compress_context. But at line 2256 (`conv.extend(final_resp)`), they're added back to the compressed pool, creating duplicates.

**Severity**: MEDIUM — Actual data corruption in the message pool.

**Fix**: Added `_forced_compression_ran` flag per instance. When forced compression succeeds during a sub-agent turn, the flag is set and `conv.extend(final_resp)` is skipped because the pool already has correct data via the sync→compress→rebuild sequence. Flag resets at the start of each new sub-agent turn.

---

## Detailed Analysis

### The Bug: Step-by-Step Trace

Consider a sub-agent that has been running for several turns. Here's what happens when forced compression triggers mid-turn:

#### Phase 1: Sub-Agent Running Normally
```
FnCallAgent._run while loop:
  Iteration 1: LLM calls read_file → assistant msg A + function result A added to `response` and `messages`
  Iteration 2: LLM calls grep      → assistant msg B + function result B added to `response` and `messages`
  Iteration 3: _call_llm called...
```

At this point, the sub-agent's local working set (`messages`) contains:
- Initial pool messages (from conv, sliced via slice_history_for_llm)
- Messages from current turn: [A_assistant, A_function, B_assistant, B_function]

And `response` = [A_assistant, A_function, B_assistant, B_function]

#### Phase 2: Forced Compression Triggers (inside hooked_call_llm)
```python
# agent_orchestrator.py lines 810-817
pool_conv = self.agent_pool.get_conversation(instance_name)
if len(messages) > len(pool_conv):
    pool_conv.clear()
    pool_conv.extend(copy.deepcopy(messages))  # ← ALL messages synced to pool
```

Now the pool contains: [SYSTEM, USER, A_assistant, A_function, B_assistant, B_function, ...]

Then compress_context runs:
```python
# compression/core.py line 275
new_history = history[:active_start_idx] + [marker_message] + history[insert_pos:]
```

This discards the oldest active messages (which may include A_assistant, A_function, etc.) and inserts a marker. The pool now has fewer messages.

Then rebuild_working_set replaces local `messages`:
```python
# helpers.py lines 84-85
messages_list.clear()
messages_list.extend(copy.deepcopy(compressed))  # ← compressed data
```

Now `messages` (local) = compressed pool data (smaller, with marker).

#### Phase 3: Back in FnCallAgent._run
```python
# fncall_agent.py lines 89-119
output: List[Message] = []
for output in output_stream:    # ← hooked_call_llm returned early, no output
    if output:
        yield response + output

if output:                      # ← False (output is empty)
    ...                        # ← Skipped
else:                          # ← Falls through here
    break                       # ← Exits while loop
yield response                  # ← Yields stale `response`!
```

**`response` still contains [A_assistant, A_function, B_assistant, B_function]** — the pre-compression messages accumulated in earlier while loop iterations!

#### Phase 4: Back in _stream_sub_agent_call
```python
# agent_orchestrator.py line 2157
final_resp = resp  # ← Contains stale pre-compression messages

# Line 2184-2185: conv is re-synced to compressed pool
if self._compress_tracker.get(instance_name, False):
    conv = self.agent_pool.get_conversation(instance_name)  # ← Compressed data

# Line 2256: THE BUG
conv.extend(final_resp)  # ← Adds stale messages back to compressed pool!
```

**The pool now contains:** [SYSTEM, USER, MARKER, ..., A_assistant, A_function, B_assistant, B_function]

If any of A/B messages were already in the pool before compression (they were synced at step 2), and they were NOT discarded by compression (because they're in the tail), then they now appear TWICE → **DUPLICATES!**

### Why the ~5-Message Periodicity?

The pattern (duplicates at indices 6, 11, 16, 17) corresponds to the natural cadence of a sub-agent turn. Each FnCallAgent._run while loop iteration adds exactly 2 messages (assistant + function result). So:

```
Index 0: SYSTEM
Index 1: USER  
Index 2: A_assistant (from previous turn's conv)
Index 3: A_function
Index 4: B_assistant
Index 5: B_function
Index 6: C_assistant ← DUPLICATE (re-injected from final_resp)
Index 7: C_function
Index 8: D_assistant
Index 9: D_function
Index 10: E_assistant
Index 11: E_function ← DUPLICATE
...
```

The periodicity of ~5 messages comes from the compressed pool having a certain number of tail messages, and then `final_resp` adding duplicate pairs at regular intervals.

### Why It Only Appears After Forced Compression

Forced compression is the only path that triggers this sequence:
1. Pre-compression sync puts all messages in the pool
2. compress_context discards some (but not all) active messages
3. rebuild_working_set replaces local data
4. But final_resp still has stale data
5. conv.extend(final_resp) re-injects it

Normal compression (agent-triggered via compress_context tool call) doesn't cause this because the pool sync happens differently — through the compression tool's own path in compression_tools.py.

---

## The Fix (Implemented and Reviewed)

### Added tracking dict (line 429):
```python
self._forced_compression_ran = {}  # instance_name -> bool
```

### Set flag when forced compression succeeds (line 845):
```python
self._forced_compression_ran[instance_name] = True
```

### Reset flag at start of each sub-agent turn (line 2145) — **CRITICAL**: explicit assignment, NOT setdefault:
```python
self._forced_compression_ran[instance_name] = False  # reset each turn
```

### Skip conv.extend and UI state update when flag is set:
- Line ~2200: Guard UI state: `state['messages'] = list(conv)` (no stale resp)
- Line ~2274: Guard pool extend: skip `conv.extend(final_resp)`

### Cleanup in finally block (line ~2332):
```python
self._compress_tracker.pop(instance_name, None)
self._forced_compression_ran.pop(instance_name, None)
```

**Key insight from review**: `_forced_compression_ran` must use explicit assignment (`= False`) at turn start, NOT `setdefault`. The `_compress_tracker` uses `setdefault` because it tracks "compression ran ANYTIME this turn" (idempotent). But `_forced_compression_ran` tracks "forced compression ran THIS SPECIFIC turn" — it must reset every turn or stale data would be suppressed forever.

---

## Evidence Summary

| Observation | Explanation |
|------------|-------------|
| Duplicates at indices 6, 11, 16, 17 (~every 5) | fn_call adds pairs (assistant+function) per while loop iteration; stale re-injection creates duplicate pairs |
| Only appears after forced compression | Forced compression is the only path with sync→compress→rebuild→extend sequence |
| No actual data corruption symptoms beyond duplicates | The agent continues to function — the extra messages are just redundant |
| Data flow proves the bug | Sync at 810-817 → compress at 275 → rebuild at 84-85 → extend at 2256 = re-injection |

---

## Files Involved

| File | Line(s) | Relevance |
|------|---------|-----------|
| `agent_orchestrator.py` | 810-817 | Pre-compression sync (puts all messages in pool) |
| `compression/core.py` | 275 | compress_context discards old messages |
| `compression/helpers.py` | 84-85 | rebuild_working_set replaces local data |
| `agent_orchestrator.py` | 2157 | final_resp captures stale response |
| `agent_orchestrator.py` | 2184-2185 | conv re-synced to compressed pool |
| `agent_orchestrator.py` | **2256** | **THE BUG: conv.extend(final_resp) re-injects stale messages** |