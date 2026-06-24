# Forced Compression → Recovery Doubling Trace
## Root Cause Analysis for MaxTokensInvestigation (2026-06-24)

### Error Log Evidence
```
[05:21:59] Rebuilt working sets: messages=30, llm_messages=29
[05:21:59] Compression notification injected into pool
[05:21:59] Pool validation FAILED: 6 duplicates in 31 messages (threshold=3)
           Dup indices: 5, 6, 12, 13, 19, 24
[05:21:59] Recovery reads logger...
[05:21:59] Logger validation FAILED: 15 duplicates in 62 messages (threshold=6)
           Dup indices: 4, 5, 11, 12, 18, 19, 25, 26, 32, 37, 38, 44, 45, 51, 56
[05:21:59] Recovery from log also failed — message pool may be corrupted
```

---

## Exact Sequence of Events

### Phase 1: Normal Execution (05:15:30 → 05:20:55)
- Instance `MaxTokensInvestigation` created with system + user prompt (2 msgs)
- ~4 minutes of execution, token count grows: 565→19k→41k→61k→78k→82k→86k→92k→94k→95.4k
- Each turn adds messages to pool AND logger via `log_message()` calls from MULTIPLE paths

### Phase 2: Forced Compression Trigger (05:20:55)
```python
# handler.py line 227
result = _compress(agent_pool=self.pool, target_agent_name=inst_name, ...)
```
- Compressor agent runs, produces summary of ~30 messages

### Phase 3: Pool Mutation (core.py lines 335-336)
```python
new_history = history[:active_start_idx] + [marker_message] + history[insert_pos:]
agent_pool.instance_conversations[target_agent_name] = new_history
```
- Pool trimmed from ~60 → ~30 messages (compressed working set)

### Phase 4: Working Set Rebuild (handler.py line 237)
```python
self.engine._rebuild_working_set(messages, llm_messages, inst_name)
# execution_engine.py line 1338 logs: "Rebuilt working sets for MaxTokensInvestigation: messages=30"
```

### Phase 5: Notification Injection (handler.py lines 268-273)
```python
notification_msg = Message(role=USER, content="[SYSTEM] Context exceeded...")
instance.append_message(notification_msg)  # Updates conversation + caches atomically
```
Pool now has **31 messages** (30 compressed + 1 notification)

### Phase 6: Pool Validation Fails (handler.py lines 284-287)
```python
conv = self.pool.get_conversation(inst_name)  # → 31 messages
if not validate_message_pool(conv, inst_name):  # → finds 6 dups at indices 5,6,12,13,19,24
    # VALIDATION FAILS! threshold=3 for pool of 31
```

### Phase 7: Recovery Reads Logger (handler.py lines 289-290)
```python
recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
# recov has ~62 messages! The logger contains the FULL uncompressed history.
```

**Why?** Because `_sync_logger_after_compression` (line 312-313) hasn't run yet — it runs AFTER validation and recovery attempts.

### Phase 8: Logger Validation Also Fails (handler.py line 291)
```python
if recov and validate_message_pool(recov, inst_name):
    # → finds 15 duplicates at indices 4,5,11,12,18,19,25,26,32,37,38,44,45,51,56
```

Logger has **62 messages** with accumulated internal duplicates → pool DOUBLED from 31 to 62!

---

## Root Cause: Three Contributing Factors

### Factor 1: Logger Accumulates Duplicates During Normal Execution

Messages are logged via `log_message()` from multiple code paths:

| Code Path | Location | What it logs |
|-----------|----------|-------------|
| `_log_messages_to_jsonl()` partial sync | execution_engine.py:1746-1748 | Messages from `conv[already_logged_count:]` |
| `_log_messages_to_jsonl()` turn output | execution_engine.py:1753-1754 | All `turn_output` messages |
| Tool result logging (inline) | execution_engine.py:2047-2048 | Each function result message |

**Duplicate scenario**: A tool's function result is logged inline at line 2048, then the same message appears in `turn_output` and gets logged again at line 1754. This creates duplicate entries in `logger.data['history']`.

### Factor 2: Compression Only Trims the Pool — Not the Logger

```python
# core.py line 336: writes to pool
agent_pool.instance_conversations[target_agent_name] = new_history

# logger.data['history'] is untouched! It still has all ~60 original messages.
```

The `_sync_logger_after_compression` call (handler.py line 312) happens AFTER validation and recovery, so during the critical window:
- Pool = 30 compressed + 1 notification = **31**
- Logger = ~60 full history with internal dups = **~62**

### Factor 3: Recovery Reads Stale Logger Data

```python
# handler.py line 290
recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
```

This reads the logger's `data['history']` which contains:
- All original uncompressed messages (~60)
- Plus accumulated duplicates from multiple log_message() calls (+~2)
- Total: **62 messages**

Then `rebuild_conversation(list(recov))` replaces instance.conversation with these 62 messages.

---

## Type Mismatch Note (Minor)

Logger stores formatted **dicts** (via `_format_message()`), while pool expects **Message objects**:
```python
# handler.py line 293
instance.rebuild_conversation(list(recov))  # recov is list of dicts, not Messages
```

This works because `rebuild_conversation` accepts any iterable and both validation and message processing handle mixed types. But it means:
- Validation uses `msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')`
- Subtle bugs can arise from type inconsistency in edge cases

---

## Why the Pool "Doubled"

| Step | Pool Size | Logger Size | Action |
|------|-----------|-------------|--------|
| 1 | ~60 | ~60 | Normal execution |
| 2 | ~30 | ~60 | Compression trims pool only |
| 3 | ~30 | ~60 | Working set rebuilt from pool |
| 4 | **31** | ~60 | Notification appended to pool |
| 5 | **31** | ~62* | Validation fails, recovery reads logger |
| 6 | **~62** | ~62 | rebuild_conversation replaces pool with logger data |

\* Logger had accumulated duplicates during turns (same message logged multiple times)

---

## Fix Recommendations

### Quick Fix: Sync Logger BEFORE Recovery Reads It
In `handler.py`, move `_sync_logger_after_compression` to run immediately after compression but before validation:

```python
# After _rebuild_working_set at line 237, add logger sync:
self._sync_logger_after_compression(inst_name, instance.agent_class, "forced compression")
```

### Better Fix: Deduplicate Logger History During Recovery
Before using `recov` for recovery, deduplicate consecutive messages:

```python
# handler.py line 290-293
recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
if recov:
    # Dedup consecutive messages in logger history before using for recovery
    deduped_recov = [recov[0]] if recov else []
    for msg in recov[1:]:
        prev = deduped_recov[-1]
        # Compare role + content prefix
        prev_role = prev.get('role', '') if isinstance(prev, dict) else getattr(prev, 'role', '')
        curr_role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
        prev_content = str(prev.get('content', ''))[:500] if isinstance(prev, dict) else str(getattr(prev, 'content', ''))[:500]
        curr_content = str(msg.get('content', ''))[:500] if isinstance(msg, dict) else str(getattr(msg, 'content', ''))[:500]
        if not (curr_role == prev_role and curr_content == prev_content):
            deduped_recov.append(msg)
    recov = deduped_recov
```

### Best Fix: Prevent Duplicate Logging in First Place
Ensure `log_message()` is called exactly once per message by consolidating all logging through `_log_messages_to_jsonl()`. Remove inline logging at lines 1930 and 2048, letting the turn-end logging handle everything.

---

## Files Involved

| File | Lines | Role |
|------|-------|------|
| `agent_cascade/compression/handler.py` | 235-318 | Forced compression execution + recovery path |
| `agent_cascade/compression/core.py` | 276-340 | Pool mutation during compression |
| `agent_cascade/execution_engine.py` | 1283-1336 | `_rebuild_working_set()` implementation |
| `agent_cascade/execution_engine.py` | 1712-1756 | `_log_messages_to_jsonl()` — multiple log paths |
| `agent_cascade/logger/agent_instance_logger.py` | 234-239 | `log_message()` additive append |
| `agent_cascade/agent_pool.py` | 1445-1451 | `get_conversation()` returns copy of instance.conversation |
| `agent_cascade/utils/pool_validation.py` | 14-94 | Duplicate detection logic |