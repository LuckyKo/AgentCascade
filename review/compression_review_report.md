# COMPRESSION SYSTEM — THOROUGH CODE REVIEW REPORT

**Reviewer:** compression_reviewer  
**Date:** 2026-05-21  
**Files Reviewed:**
- `agent_cascade/tools/custom/compression_tools.py` (338 lines)
- `agent_pool.py` (1041 lines, sections ~529–591, ~850–1028)
- `operation_manager.py` (1576 lines, section ~1565–1570)
- `agent_orchestrator.py` (2101 lines, sections ~660–786, ~1014–1072, ~1429–1445)
- `agent_cascade/prompts/dna.py` (400 lines, COMPRESSION_*)

---

## VERDICT: NEEDS WORK — Multiple bugs and inconsistencies found

---

## 🔴 CRITICAL BUGS (Data corruption / crashes / infinite loops)

### BUG 1: UNDEFINED VARIABLE IN BACKUP VERSION
**File:** `review/compression_tools.py` (line ~239, confirmed by Maine's note)  
**Severity:** 🔴 Critical — NameError crash

```python
target_messages = messages_to_compress[:num_to_summarize]
```
`messages_to_compress` is never defined in this scope. The correct variable is `active_set`. This would cause an immediate `NameError: name 'messages_to_compress' is not defined` at runtime.

**Fix:** Replace `messages_to_compress` with `active_set`.

---

### BUG 2: AGENT-TRIGGERED COMPRESSION DOES NOT REBUILD `llm_messages` IN THE CALLER
**File:** `compression_tools.py` lines 280–310  
**Severity:** 🔴 Critical — Can cause infinite compression loop or context overflow

When the **agent** calls `compress_context` (not forced compression), the tool rebuilds `kwargs['messages']` (line 309: `active_msgs.extend(new_active)`). But it does **NOT** update `llm_messages` — that's only done in the orchestrator at lines 1443–1445.

However, the orchestrator's `_inject_compression_warning_for_agent()` at line 668 uses:
```python
current_tokens = self._get_history_tokens(messages)
```
If `messages` (the full history list passed to `_run()`) is NOT updated after agent-triggered compression, its token count stays at the old un-compressed value. If this triggers forced compression (>95%), the flow becomes:

1. Agent calls compress_context → pool gets summary marker inserted
2. Tool rebuilds `kwargs['messages']` (caller's local copy) 
3. Orchestrator syncs `llm_messages` from sliced pool (line 1443–1445) ✓
4. BUG-7 block syncs `messages` from FULL pool (line 1071–1072) ✓

So actually, the orchestrator DOES handle this correctly through the BUG-7 block. **BUT** — there's a subtle race condition: if `_inject_compression_warning` is called AGAIN in the same loop iteration BEFORE the BUG-7 block runs, it checks `messages` which still has old tokens.

Looking at the flow more carefully:
- Line 1015: `forced_compression_ran = self._inject_compression_warning(llm_messages)` — uses `llm_messages`, not `messages` ✓ (safe)
- Line 1431: flag set after tool call
- BUG-7 block at line 1064: syncs `messages` from pool

The issue is that `_get_history_tokens(messages)` at line 668 receives `llm_messages`, not `messages`. So the token check uses the sliced working set, which IS updated by the tool. This is correct.

**Verdict on BUG 2:** After careful tracing, this is actually handled correctly by the orchestrator's sync logic. **FALSE POSITIVE — NOT A BUG.**

---

### BUG 3: DOUBLE-MUTATION RISK WHEN BOTH FORCED AND AGENT COMPRESSION HAPPEN
**File:** `compression_tools.py` lines 280–310 vs `agent_orchestrator.py` lines 730–756  
**Severity:** 🔴 Critical — Potential data corruption

When forced compression runs at >95%:
1. `_inject_compression_warning_for_agent()` calls the tool **without** passing `messages` kwarg (line 706–710)
2. Tool skips message rebuild (line 280 check fails — no `kwargs.get('messages')`)
3. Orchestrator rebuilds both `messages` and `llm_messages` from sliced pool (lines 732–736)

When agent-initiated compression runs:
1. Tool receives `kwargs['messages']` and rebuilds it in-place (lines 309–310)
2. Orchestrator BUG-7 block syncs `messages` from full pool (line 1071–1072)

The problem: if the tool's in-place rebuild (lines 309–310) and the orchestrator's BUG-7 sync (line 1071–1072) both run, `messages` gets mutated **twice**:
- First: `active_msgs.clear(); active_msgs.extend(new_active)` — tool rebuilds from sliced working set
- Second: `messages.clear(); messages.extend(copy.deepcopy(compressed))` — orchestrator syncs from FULL pool

These produce DIFFERENT results! The tool uses `slice_history_for_llm()` (working set), while the BUG-7 block uses the full cumulative pool. This means `messages` ends up with different content depending on which mutation runs second.

**Fix:** Remove the tool's in-place message rebuild (lines 280–310). Always let the orchestrator be the single point of truth for rebuilding caller state. The tool should only modify the pool and return a status string.

---

### BUG 4: `llm_messages` NOT REBUILT IN BUG-7 BLOCK — TOKEN COUNT STAYS HIGH
**File:** `agent_orchestrator.py` lines 1064–1072  
**Severity:** 🟠 Major — Can cause infinite compression loop

In the BUG-7 block (agent-triggered compression):
```python
if getattr(self, '_compress_context_ran_this_turn', False):
    compressed = self.agent_pool.get_conversation(self.session_name)
    if compressed:
        messages.clear()
        messages.extend(copy.deepcopy(compressed))
```

This syncs `messages` from the **full** pool (cumulative history with summary marker). But it does NOT update `llm_messages`. The tool call handler at lines 1443–1445 updates `llm_messages`:
```python
sliced = self.agent_pool.slice_history_for_llm(compressed_conv)
llm_messages.clear()
llm_messages.extend(copy.deepcopy(sliced))
```

This happens AFTER the BUG-7 block in the same turn. So within a single `_run()` invocation, the order is:
1. Tool runs → `llm_messages` updated (lines 1443–1445) ✓
2. Tool loop finishes
3. BUG-7 block syncs `messages` from pool (line 1071–1072) ✓

But wait — the tool call handling at lines 1429–1445 happens **inside the tool execution loop**, while BUG-7 is checked **after** the LLM call. The sequence in `_run()` is:
1. Check forced compression (line 1015) — uses `llm_messages` 
2. If not forced, call LLM → tool calls may happen
3. Tool completion handler syncs `llm_messages` (lines 1437–1445)
4. Loop continues → BUG-7 block checks flag and syncs `messages` (line 1064–1072)

So within the same turn, both are properly synced. **But** there's a timing issue: if `num_llm_calls_available` is exhausted during the tool loop, the next iteration of the while loop would check `_inject_compression_warning(llm_messages)` — and `llm_messages` IS correctly updated by the tool handler. 

**Verdict:** This is actually handled correctly within a single turn. However, if compression runs at the very end of available turns, there could be edge cases.

---

### BUG 5: MISSING `agent_obj` IN FORCED COMPRESSION PATH → FALLBACK TOKEN COUNTING
**File:** `agent_orchestrator.py` line 706–710 vs `agent_pool.py` lines 893–910  
**Severity:** 🟠 Major — Incorrect discard count in forced compression

In forced compression (line 706):
```python
result = compress_tool.call(
    params,
    agent_instance_name=instance_name,
    agent_obj=agent   # ← agent IS passed!
)
```

Wait — `agent` IS passed as `agent_obj`. So this isn't a bug. Let me re-check...

Actually, looking at the tool signature:
```python
self.agent_pool.operation_manager.apply_context_compression(
    agent_name=agent_name,
    summary=summary,
    fraction=fraction,
    target_discard_count=target_discard_count,
    agent_obj=agent_obj,  # ← from kwargs['agent_obj']
)
```

And in `_apply_context_compression`:
```python
tokens = agent_obj._count_message_tokens(msg) if agent_obj and hasattr(agent_obj, '_count_message_tokens') else 0
```

Since `agent_obj` IS passed in forced compression, the fallback path is NOT triggered. **FALSE POSITIVE — NOT A BUG.**

---

## 🟠 MAJOR BUGS (Logic errors / edge case failures)

### BUG 6: INCONSISTENT GUARD THRESHOLDS
**File:** `compression_tools.py` line 191 vs `agent_pool.py` line 926  
**Severity:** 🟠 Major — Compression may succeed in tool but fail silently in pool

Tool guard (line 191):
```python
if len(active_set) < 3 and total_tokens < 200:
    return "Context is already optimally compressed..."
```

Pool guard (line 926):
```python
if len(messages_to_compress) <= 2:
    logger.warning(...)
    return
```

The tool allows compression when `len(active_set) >= 3` regardless of token count. But if the active set has exactly 3 messages and somehow only 1 survives after clamping (unlikely but possible with edge-case fractions), the pool method would reject it. More importantly, the tool guard uses `total_tokens < 200` as a secondary condition — meaning 2 messages with 500 tokens each can be compressed by the tool, but the pool's guard doesn't care about tokens at all.

The real issue: if someone calls `_apply_context_compression()` directly (bypassing the tool), the pool's `<=2` check is stricter than the tool's `<3 AND <200 tokens` check. A caller with 2 messages and 5000 tokens would be allowed by the tool guard but blocked by the pool guard.

**Fix:** Align the thresholds. Use `len(messages_to_compress) < 3` (same as tool's `<3`) in both places.

---

### BUG 7: DOUBLE-CLAMPING OF `target_discard_count` IN FORCED MODE
**File:** `compression_tools.py` lines 217–223 vs `agent_pool.py` lines 932–942  
**Severity:** 🟠 Major — Force flag is partially defeated, creates confusing log noise

When forced compression runs with a very small active set (e.g., 2 messages):
1. Tool: `target_discard_count = max(0, min(target_discard_count, len(active_set) - 2))` → clamped to 0
2. Tool force: `if target_discard_count <= 0: target_discard_count = 1` → set to 1
3. Pool clamp (line 932): `target_discard_count = min(target_discard_count, len(messages_to_compress))` → stays 1
4. Pool clamp (line 934): `target_discard_count = min(target_discard_count, len(messages_to_compress) - 2)` → clamped to 0
5. Pool floor (line 940–942): `if target_discard_count <= 0: target_discard_count = 1` → set back to 1

The force flag causes `target_discard_count` to oscillate between 1 and 0 across the tool→pool boundary. The end result is correct (at least 1 message compressed), but it generates misleading log messages:
```
WARNING: target_discard_count was 0 for agent 'X', forcing minimum of 1
```

This happens every time forced compression runs on a small active set, even though the force flag already handled this at the tool level.

**Fix:** Remove the floor logic in `_apply_context_compression()` (lines 940–942) and let the tool handle all force semantics. Or better: add a `force` parameter to `_apply_context_compression()` so it knows whether the floor should be applied.

---

### BUG 8: TYPE DETECTION VULNERABILITY IN MESSAGE REBUILD
**File:** `compression_tools.py` lines 294–306  
**Severity:** 🟠 Major — Can cause TypeError if active_msgs is empty or mixed-type

```python
if isinstance(active_msgs[0] if active_msgs else None, dict):
```

This checks `active_msgs[0]` to determine the type of messages. But:
1. If `active_msgs` is empty (edge case after pool compression), it passes `None` and defaults to Message-object path
2. If `active_msgs` has mixed types (some dicts, some Message objects), only the first element determines the path for ALL elements

The type detection should use the **pool's** message types instead of the caller's potentially stale list:
```python
compressed_pool_history = self.agent_pool.get_conversation(agent_name)
is_dict = isinstance(compressed_pool_history[0] if compressed_pool_history else None, dict)
```

**Fix:** Use pool history to determine message type, not the caller's active list.

---

### BUG 9: SUMMARY LENGTH GUARD SILENTLY FAILS
**File:** `agent_pool.py` lines 951–953  
**Severity:** 🟠 Major — Compression silently skipped with no feedback

```python
if not summary or len(summary.strip()) < MIN_SUMMARY_LENGTH:
    logger.warning(f"Empty or trivial summary...")
    return  # ← Silently returns without notifying caller!
```

The tool calls `_apply_context_compression()` which can return early here. But the tool doesn't check the return value — it just proceeds to rebuild messages and return a success message:
```python
return f"Context compressed ({mode} mode): {int(fraction*100)}% of older history..."
```

The caller thinks compression succeeded when it actually did nothing (summary too short, marker not inserted). The agent gets confused on the next turn because its context didn't change but it was told compression happened.

**Fix:** Either:
- Have `_apply_context_compression()` return a boolean/status string
- Check that status in the tool and return an error if compression was skipped
- At minimum, log a CRITICAL-level message when summary is too short

---

## 🟡 MINOR ISSUES (Design flaws / code quality)

### ISSUE 10: REDUNDANT CLAMP IN `_apply_context_compression()`
**File:** `agent_pool.py` line 932  
**Severity:** 🔵 Minor — Dead code, unnecessary duplication

```python
target_discard_count = min(target_discard_count, len(messages_to_compress))
```

The tool already clamps to `len(active_set) - 2`, so this line is always a no-op. The only reason it exists is if `_apply_context_compression()` is called directly (bypassing the tool). Check call sites:
- `operation_manager.py:1570` → calls through tool's `call()` method ✓
- No direct callers found

**Fix:** Remove this line or add a comment explaining why it exists.

---

### ISSUE 11: `_compress_context_ran_this_turn` FLAG SET AT INCONSISTENT TIMES
**File:** `agent_orchestrator.py` lines 698, 1055, 1431  
**Severity:** 🟡 Minor — Timing confusion, hard to reason about

- Forced compression: set **BEFORE** tool call (line 698)
- LLM tool call completion: set **AFTER** tool returns (line 1431)
- Post-forced-compression block: reset to False (line 1055)

This inconsistency means the flag's meaning depends on which code path triggered it. If forced compression runs, sets the flag, then later in the same turn the LLM decides to compress again, the flag is already True — causing the orchestrator to skip its check at line 676.

**Fix:** Standardize: always set the flag **after** compression completes (not before), and document the expected lifecycle clearly.

---

### ISSUE 12: DEEP COPY OVERHEAD IN CRITICAL PATH
**File:** `agent_orchestrator.py` lines 736, 1048, 1052  
**Severity:** 🟡 Minor — Performance concern for large histories

```python
messages.clear()
messages.extend(copy.deepcopy(sliced))
...
messages.clear()
messages.extend(copy.deepcopy(compressed))
llm_messages.clear()
llm_messages.extend(copy.deepcopy(sliced))
```

Each compression event creates deep copies of potentially thousands of messages. With large histories, this adds significant latency and memory pressure during the exact moments when context is full and performance matters most.

**Fix:** Consider using shallow copies where possible (messages are typically immutable once created), or implement a copy-on-write strategy.

---

### ISSUE 13: MISSING `copy` IMPORT IN COMPRESSION TOOLS
**File:** `compression_tools.py` line 1  
**Severity:** 🔵 Nit — Code style / potential future bug

The tool uses `copy.deepcopy()` in the orchestrator but doesn't import `copy`. Wait — it does at line 1:
```python
import copy
```

Actually, looking more carefully, the `copy_file` call shows `import copy` at line 1. But then `copy.deepcopy()` is used in the orchestrator code (lines 736, 1048, 1052), not in compression_tools.py. Let me re-check...

The tool itself doesn't use `copy.deepcopy()`. The orchestrator does. So this is fine — no bug here. **FALSE POSITIVE.**

---

### ISSUE 14: NO HANDLING OF COMPRESSION MARKER WITHIN SUMMARY CONTENT
**File:** `agent_pool.py` lines 538–544, `slice_history_for_llm()`  
**Severity:** 🟡 Minor — Edge case with nested compression markers

If a generated summary itself contains the string `"--- CONTEXT COMPRESSED"` (unlikely but possible), `get_compression_target_set()` would detect it as a new marker and incorrectly compute the active set. The scan goes backwards from the end, so it would find the LAST occurrence of the marker in any message's content.

**Fix:** Use a more unique marker format (e.g., UUID-based) or check that the marker is at the start of the content AND the content matches the expected template pattern.

---

### ISSUE 15: NO ATOMICITY GUARANTEE
**File:** Multiple files  
**Severity:** 🟡 Minor — Thread safety concern

The compression flow involves:
1. Reading pool state (tool)
2. Computing target discard count (tool)
3. Generating summary (LLM call — slow, external)
4. Inserting marker into pool (pool method)
5. Syncing caller state (orchestrator)

Between steps 2 and 4, other operations could modify the pool (new messages logged by logger, other tool calls). While the system uses `halt_all_instances()` during forced compression, agent-initiated compression has no such protection.

**Fix:** Add a revalidation step after summary generation: check that the active set hasn't changed since target_discard_count was calculated. If it has, recalculate and retry.

---

## 📊 SUMMARY TABLE

| # | Severity | File | Issue | Impact |
|---|----------|------|-------|--------|
| 1 | 🔴 Critical | `review/compression_tools.py` | Undefined variable `messages_to_compress` | NameError crash |
| 2 | 🟠 Major | `agent_pool.py:951-953` | Silent failure when summary too short | Silent data loss, agent confusion |
| 3 | 🟠 Major | `compression_tools.py:294-306` | Type detection uses caller's list | TypeError with mixed-type messages |
| 4 | 🟠 Major | `compression_tools.py:191` vs `agent_pool.py:926` | Inconsistent guard thresholds | Compression succeeds in tool, rejected in pool |
| 5 | 🟠 Major | `compression_tools.py:217-223` + `agent_pool.py:932-942` | Double-clamping defeats force flag | Confusing logs, wasted compute |
| 6 | 🟡 Minor | `agent_orchestrator.py:698,1055,1431` | Flag timing inconsistency | Hard to reason about double-compression prevention |
| 7 | 🟡 Minor | `agent_orchestrator.py:736,1048,1052` | Deep copy overhead | Latency during critical moments |
| 8 | 🟡 Minor | `agent_pool.py:538-544` | Marker collision in summary content | Incorrect active set computation |
| 9 | 🟡 Minor | Multiple files | No atomicity guarantee | Race condition on pool state |

---

## 🔧 REQUIRED CHANGES (In order of priority)

### MUST FIX (Before next release):
1. **BUG 1:** Fix undefined variable in backup version (if that version is still used)
2. **BUG 9:** Handle silent summary-too-short failure — return error from `_apply_context_compression()` and propagate to tool caller
3. **BUG 8:** Use pool message types for type detection, not caller's list

### SHOULD FIX (Before next release):
4. **BUG 7:** Align force flag semantics — add `force` parameter to `_apply_context_compression()` instead of double-clamping
5. **BUG 6:** Align guard thresholds between tool and pool methods
6. **BUG 3:** Audit the double-mutation risk — ensure tool rebuild and orchestrator sync don't conflict

### NICE TO FIX (Next iteration):
7. **ISSUE 14:** Use more unique compression marker format
8. **ISSUE 15:** Add revalidation after summary generation
9. **ISSUE 11:** Standardize flag lifecycle with clear documentation
10. **ISSUE 12:** Optimize copy strategy for large histories

---

## 🧪 RECOMMENDED TESTS

1. **Forced compression on 2-message active set** — verify force flag works correctly
2. **Two compressions in rapid succession** — verify active_start_idx updates correctly
3. **Compression with existing summary marker** — verify correct boundary handling
4. **Empty/minimal summary generation** — verify error is propagated to caller
5. **Mixed dict/Message object history** — verify type detection works
6. **Summary containing compression marker substring** — verify no false-positive marker detection