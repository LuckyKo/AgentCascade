# Slot Timeout and Bool/List Leak - Fix Analysis

## Executive Summary

Two related issues identified:
1. **Slot timeout**: PortPlanReviewer times out waiting for endpoint slot with active_count=1
2. **Bool/list leak**: Unexpected boolean and list values in conversation history

## Root Cause Analysis

### Issue 1: Slot Timeout

#### Symptoms
- PortPlanReviewer exits at 13:21:13 (RUNNING → IDLE)
- Maine tries to acquire at 13:21:13 (11ms later)  
- No "Agent released slot" log between exit and acquire
- PortPlanReviewer times out at 13:23:25 with active_count=1

#### Root Cause Hypothesis

The most likely cause is a **failed re-acquire scenario** in the synchronous call_agent path:

```python
# execution_engine.py lines 2078-2091
# Release caller's slot so the child can acquire it inside engine.run()
if caller_slot_holder and hasattr(caller_slot_holder, '_slot_release') and caller_slot_holder._slot_release is not None:
    self._release_slot(caller_slot_holder, caller_name, "sync child")  # Line 2080

try:
    inst, conv = self._create_and_run_agent(...)  # Child runs here
    
    # Re-acquire caller's slot so it can continue its turn
    if not _reacquire_slot(caller_slot_holder, caller_name, "sync child"):  # Line 2089
        # Helper already logged warnings and set _slot_release = None
        pass
```

**Bug at line 2075**: When `_reacquire_slot` fails after 2 attempts, it sets `slot_holder._slot_release = None`. This means:

1. Caller releases slot at line 2080 (active_count decremented)
2. Child acquires and runs (active_count incremented)
3. Child completes and releases (active_count decremented)  
4. Caller tries to re-acquire at line 2089 but **fails** (timeout or exception)
5. Line 2075 sets `caller._slot_release = None`
6. Caller's finally block at line 624 finds `_slot_release is None`, does nothing
7. **But the caller never successfully re-acquired**, so there's nothing to release

However, this doesn't fully explain why active_count stays at 1. The more likely scenario is:

**Alternative Hypothesis**: The child agent (PortPlanReviewer) itself has a bug where it acquires but doesn't properly release. This could happen if:
- Exception occurs before finally block runs
- `_slot_release` gets set to None prematurely
- Double-release protection prevents the actual release

#### Evidence Supporting Alternative Hypothesis

The error message says "Failed to acquire endpoint slot for **PortPlanReviewer**" - not Maine. This suggests PortPlanReviewer is the one trying to acquire and timing out, meaning there's already an active agent holding the slot.

If PortPlanReviewer exited at 13:21:13 but didn't release properly, then:
- Active count stays at 1
- Any subsequent agent (including a new PortPlanReviewer instance) will timeout

### Issue 2: Bool/List Leaking

#### Symptoms
```
get_history_stats: skipping unexpected list item in messages list
get_history_stats: skipping unexpected bool value in messages list: True
```

#### Root Cause

The `get_history_stats` function expects only Message objects or dicts, but finds booleans and lists. This suggests somewhere in the code, raw values are being appended to conversation history instead of proper Message objects.

**Potential sources**:
1. Tool result injection paths
2. LLM response parsing errors  
3. JSON deserialization returning True/False for "true"/"false" strings
4. Async result processing bugs

## Proposed Fixes

### Fix 1: Add Debug Logging to Slot Release

Add more detailed logging to track slot acquisition and release per instance:

```python
# execution_engine.py line 624
self._release_slot(instance, instance.instance_name)

# Change _release_slot to log what's happening
def _release_slot(slot_holder, holder_name, context=""):
    if not hasattr(slot_holder, '_slot_release'):
        logger.debug(f"[SLOT_DEBUG] No _slot_release attribute on {holder_name}")
        return
    
    if slot_holder._slot_release is None:
        logger.warning(f"[SLOT_DEBUG] _slot_release is None for {holder_name} during {context}")
        return
    
    logger.info(f"[SLOT_DEBUG] Releasing slot for {holder_name} during {context}")
    release_callback = slot_holder._slot_release
    slot_holder._slot_release = None
    try:
        release_callback()
        logger.info(f"[SLOT_DEBUG] Successfully released slot for {holder_name}")
    except Exception as e:
        logger.error(f"[SLOT_RELEASE_ERROR] Failed to release slot for {holder_name}: {e}", exc_info=True)
```

### Fix 2: Fix Failed Re-acquire Logic

The `_reacquire_slot` helper sets `_slot_release = None` on failure, but this is correct behavior. The issue is that the finally block then has nothing to release. We need to ensure the finally block only tries to release if the reacquire succeeded:

**Option A**: Track whether reacquire succeeded
```python
# execution_engine.py line 2089
reacquire_success = _reacquire_slot(caller_slot_holder, caller_name, "sync child")
if not reacquire_success:
    # Don't try to release in finally if we never acquired
    caller_slot_holder._skip_final_release = True
```

**Option B**: Check if we actually hold the slot before releasing
```python
# In _release_slot, add check for whether callback is valid
if slot_holder._slot_release is not None:
    # Try to call it - if it's a stale callback, the closure's _released flag should handle it
    release_callback = slot_holder._slot_release
    slot_holder._slot_release = None
    release_callback()  # This will check _released flag internally
```

### Fix 3: Track Slot State Per Instance

Add instance-specific tracking to debug slot issues:

```python
# In AgentInstance __init__
self._slot_acquired_at = None
self._slot_release_count = 0

# In _acquire_slot
instance._slot_acquired_at = time.monotonic()

# In _release_slot  
instance._slot_release_count += 1
logger.debug(f"[SLOT_DEBUG] {holder_name} release count: {instance._slot_release_count}")
```

### Fix 4: Investigate Bool/List Leak Source

Add logging where messages are appended to identify the source:

```python
# execution_engine.py line 1316
with instance._compression_lock:
    for item in turn_output:
        if not isinstance(item, (Message, dict)):
            logger.warning(f"[MSG_DEBUG] Non-Message item in turn_output: {type(item)}={item}")
    instance.conversation.extend(turn_output)
```

## Testing Plan

1. **Reproduce the issue**: Run the same workflow that triggers PortPlanReviewer timeout
2. **Add debug logs**: Implement Fix 1 to see what's happening with slots
3. **Check for duplicate instances**: Verify if there are multiple PortPlanReviewer instances
4. **Trace bool/list source**: Implement Fix 4 to find where booleans leak

## Files to Modify

1. `agent_cascade/execution_engine.py` - Slot release logic, debug logging
2. `agent_cascade/api_router.py` - Add more detailed slot tracking logs
3. `agent_cascade/utils/utils.py` - Add source tracking for bool/list items

## Next Steps

1. Implement Fix 1 (debug logging) to get more visibility
2. Run the workflow again and capture new logs
3. Analyze logs to confirm root cause
4. Implement appropriate fix based on findings
5. Test thoroughly before merging