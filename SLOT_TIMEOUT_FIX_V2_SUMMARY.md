# Slot Timeout Fix v2 - Summary

## Problem

The slot timeout issue was still happening after 2 rounds of fixes. The error message showed:
```
Failed to acquire endpoint slot for Maine: Timed out after 300s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1
```

The key issues were:
1. **No visibility into WHO holds the slot** - When timeout occurred, we didn't know which instance was holding it
2. **Race conditions between parent and async children** - Both competing for the same shared sequential slot
3. **No mechanism to detect stuck slots** - Where active_count=1 but the holder never properly releases

## Root Cause Analysis

### Scenario 1: Child Agent Fails to Release Slot
1. Parent (e.g., PortPlanReviewer) releases slot → active_count=0
2. Child acquires slot → active_count=1  
3. Child exits but fails to release properly (schedule deleted, exception during release, etc.)
4. Parent tries to reacquire → blocks for 300s → timeout
5. Error says "Failed to acquire for PortPlanReviewer" but doesn't say WHO holds it

### Scenario 2: Async Child from Previous Turn Holds Slot
1. Maine calls async child (e.g., Security) via register_async_call
2. Maine's engine.run() exits, releases slot → active_count=0
3. New engine.run() for Maine starts immediately
4. Meanwhile, async child from PREVIOUS turn tries to acquire the slot
5. Child acquires first → active_count=1
6. Maine blocks on acquire → 300s timeout

### Scenario 3: Multiple Agents Competing for Shared Sequential Slot
All agents with `concurrency_limit=0` share `_shared_sequential_slot_`. If one agent holds it and another tries to acquire, they block. No visibility into which instance holds the slot.

## Solution Implemented

### Part 1: Track Slot Ownership (api_router.py)

Added `_slot_holders` dictionary to `EndpointScheduler` to track which instances hold slots:
```python
self._slot_holders: Dict[str, List[tuple]] = {}  # slot_key → [(instance_name, agent_class, acquired_at)]
```

Modified `acquire()` method:
- Accepts `instance_name` and `agent_class` parameters for tracking
- Records holder information when slot is acquired
- Includes holder info in timeout error messages

Modified `release()` method:
- Removes holder entry from `_slot_holders` when releasing
- Enhanced logging to show which instance is releasing

### Part 2: Enhanced Error Messages

When timeout occurs, the error message now includes:
```
Timed out after 300s waiting for endpoint slot on http://localhost:1234/v1. 
Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
```

### Part 3: Diagnostic Methods

Added new methods to `EndpointScheduler`:

1. **`get_slot_holders(slot_key=None)`** - Returns dictionary of slot holders for debugging
2. **`detect_stuck_slots(threshold_seconds=60.0)`** - Identifies slots held longer than threshold
3. **Enhanced `get_status()`** - Now includes slot holder information in status reports

### Part 4: Enhanced Logging (execution_engine.py)

Added detailed logging for SYNC path slot management:
- Logs when caller releases slot before child runs
- Logs when caller reacquires after child completes
- Includes timing information to detect delays

## Files Modified

1. **agent_cascade/api_router.py**
   - Added `_slot_holders` tracking dictionary
   - Modified `acquire()` to accept instance_name and agent_class parameters
   - Enhanced timeout error messages with holder information
   - Modified `release()` to remove holder entries
   - Added `get_slot_holders()` diagnostic method
   - Added `detect_stuck_slots()` diagnostic method
   - Enhanced `get_status()` to include slot holder info

2. **agent_cascade/agent_pool.py**
   - Modified `_acquire_slot()` to pass instance_name and agent_class to scheduler

3. **agent_cascade/execution_engine.py**
   - Added enhanced logging for SYNC path slot management
   - Added timing instrumentation for debugging race conditions

## Testing Recommendations

1. Run a scenario where an agent calls a child via call_agent
2. Check logs for slot ownership tracking messages:
   - `[EndpointScheduler] Agent 'X' (Y) acquired slot on 'Z'`
   - `[EndpointScheduler] Agent 'X' (Y) released slot on 'Z'`
3. If timeout occurs, error message should show which instance holds the slot
4. Use `api_router.scheduler.get_status()` to see current slot holders
5. Use `api_router.scheduler.detect_stuck_slots(60)` to find slots held > 60 seconds

## Expected Improvements

1. **Better visibility** - When a timeout occurs, we now know exactly which instance is holding the slot
2. **Faster debugging** - Slot holder information in logs helps identify race conditions
3. **Proactive detection** - `detect_stuck_slots()` can find problems before they cause timeouts
4. **Status monitoring** - `get_status()` includes slot holder info for real-time monitoring

## Next Steps

1. Deploy the fix and monitor logs for slot timeout occurrences
2. If timeouts still occur, use the enhanced error messages to identify the holding instance
3. Investigate why that specific instance isn't releasing its slot properly
4. Consider adding automatic stuck slot recovery if needed (force release after N seconds)