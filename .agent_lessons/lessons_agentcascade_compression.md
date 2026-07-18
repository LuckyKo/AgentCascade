# AgentCascade Compression System - Learned Lessons

## Date: 2026-06-16
## Author: CompressionFixPlan (via Maine's research)

---

## Root Cause Discovery

The compression system had a cascading failure pattern where pool corruption led to multiple downstream bugs. The key insight was that the **working set sync at orchestrator line 858** could overwrite the pool's system message because the `messages` list passed in might not include the system message (depending on how the working set was built).

### Key Finding:
```python
# Before FIX 1 - vulnerable code:
pool_conv.clear()
pool_conv.extend(copy.deepcopy(messages))  # messages might not have SYSTEM at index 0!
```

When this corrupted pool was then processed by `get_compression_target_set()`, it calculated `active_start_idx=0` (because no system message detected), and the compression formula `history[:0] + [marker] + history[insert_pos:]` produced an empty prefix = **lost system message**.

---

## The Four Fixes Implemented

### FIX 1: Pool Corruption Prevention (Root Cause)
**Location:** `agent_orchestrator.py` line ~858-895

**Pattern to use when syncing mutable state:**
```python
# Check if source has what target expects; if not, preserve from target
if pool_has_system and messages_missing_system:
    sync_messages = [copy.deepcopy(system_message)] + copy.deepcopy(messages)
else:
    sync_messages = copy.deepcopy(messages)
```

**Lesson:** When syncing state between two sources, always check for invariants that might be lost. Don't assume the incoming data has all required elements.

---

### FIX 2: Defensive Guard on Index Calculation  
**Location:** `agent_pool.py` line ~671-680

**Pattern to use:**
```python
# After calculating an index, add a defensive guard for known invariants
if history and len(history) > 0:
    first_role = history[0].get(ROLE) if isinstance(history[0], dict) else getattr(history[0], ROLE, '')
    if first_role == SYSTEM and active_start_idx == 0:
        logger.warning(f"[COMPRESSION FIX] Forced active_start_idx from 0 to 1...")
        active_start_idx = 1
```

**Lesson:** Index calculations are vulnerable to corrupted input. Add defensive guards that check for known invariants (e.g., "system message should never be compressed away").

---

### FIX 3: Variable Initialization Before Branching
**Location:** `agent_cascade/compression/agent_invoker.py` line ~126-145

**Pattern to use:**
```python
# Initialize variables that will be used across exception handlers BEFORE branching
subagent_return_value = None  # Initialize here, not inside if block
try:
    if condition:
        # ... use subagent_return_value
    else:
        # ... might raise exception, but subagent_return_value is still defined
except Exception as e:
    # Can safely reference subagent_return_value here
```

**Lesson:** When a variable is referenced in an exception handler that wraps multiple execution paths, initialize it BEFORE the branching logic to avoid UnboundLocalError.

---

### FIX 4: Enhanced Slice Guard
**Location:** `agent_pool.py` line ~629-647

**Pattern to use:**
```python
# When slicing history, always check if critical elements are preserved
if system_msg:
    has_system_in_slice = any(
        msg.get(ROLE) == SYSTEM for msg in sliced
    )
    if not has_system_in_slice:
        return [system_msg] + list(sliced)  # Prepend missing element
```

**Lesson:** Slicing operations can accidentally exclude critical elements. Always verify invariants are preserved after slicing.

---

## Testing Recommendations

1. **Test sync with incomplete data:** Create scenarios where working set doesn't include system message, then trigger forced compression. Verify pool still has system at index 0.

2. **Test multiple compressions:** Run through 3+ consecutive compressions to verify the defensive guards work when markers accumulate.

3. **Test exception paths:** Trigger exceptions in both the call_agent path and direct run() path of agent_invoker.py to verify no UnboundLocalError.

4. **Test edge cases:** 
   - Pool with only system message
   - Pool with system + 1 user message
   - Pool where latest_summary_idx points exactly at system message position

---

## Related Files and Functions

- `agent_orchestrator.py`: `_inject_compression_warning_for_agent()` (line ~840), `validate_message_pool()` (line ~356)
- `agent_pool.py`: `get_compression_target_set()` (line ~642), `slice_history_for_llm()` (line ~619)
- `agent_cascade/compression/core.py`: `apply_compression()` (line ~16), `compress_context()` (line ~136)
- `agent_cascade/compression/agent_invoker.py`: `invoke_compression_agent()` (line ~63)

---

## Future Improvements to Consider

1. **Add invariant assertions:** Consider adding runtime assertions in debug mode that check for system message presence after critical operations.

2. **Consider immutable history:** Long-term, consider using immutable list operations instead of `clear()` + `extend()` to prevent race conditions.

3. **Add pool validation hook:** Add a hook that runs `validate_message_pool()` after every compression and logs warnings (not just errors).

4. **Migration to Root architecture:** Note in code comments that `agent_orchestrator.py` is being phased out by Root - these fixes may need revisiting during migration.

---

## Quick Reference: Compression Flow

```
1. Orchestrator detects high context usage (>95%)
2. Calls _inject_compression_warning_for_agent()
3. Syncs working set to pool (FIX 1 applied here)
4. Calls compress_context() which:
   a. Gets compression target set via get_compression_target_set() (FIX 2 applied here)
   b. Invokes Compression Agent via invoke_compression_agent() (FIX 3 applied here)
   c. Calls apply_compression() to atomically update pool and log
5. Rebuilds working set from pool via slice_history_for_llm() (FIX 4 applied here)
6. Continues agent execution with compressed context
```

---

## Metrics for Success

After implementing all fixes, the following should be true:
- ✅ Pool always has SYSTEM message at index 0 after sync
- ✅ `active_start_idx` is never 0 when history[0] is SYSTEM
- ✅ No UnboundLocalError in agent_invoker.py exception handlers
- ✅ `slice_history_for_llm()` always returns output with SYSTEM message if it exists in input
- ✅ `validate_message_pool()` passes after every compression