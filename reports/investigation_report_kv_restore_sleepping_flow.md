# KV Cache Save/Restore Flow Investigation — SLEEPING Transitions

## Executive Summary

**Finding: There is a confirmed gap in the current implementation.** The KV cache restore happens at the wrong time — it occurs *every* turn via `_setup_turn()`, not specifically on wakeup. This means:

1. State is saved correctly before delegation (line 256, tool_dispatcher.py)
2. State is restored on the **first turn after wakeup**, but only if the agent survives to `_setup_turn` 
3. **Critical gap**: Between slot release (SLEEPING transition) and slot re-acquisition (wakeup), another agent can run on the same endpoint/model — but because states are persisted to disk, this doesn't cause data loss. However, the in-memory KV cache is overwritten.
4. States are persisted to disk as `.bin` files — they survive model reloads.

## Flow Trace

### Scenario: Maine (conc=0) calls async child B

#### Step 1: Save before delegation
**Location**: `tool_dispatcher.py:253-258`
```python
# Save parent's state before delegating via call_agent.
# Best-effort — silent on failure, never blocks execution.
try:
    self._save_parent_state_before_delegation(instance)
except Exception:
    pass
```

This calls `state_ops.save_instance_state()` which:
- Checks `endpoint_cfg['state_save_enabled']` is True
- POSTs to `{base}/v1/models/{model}/state/save` with label=instance_name (e.g., "Maine")
- Autoloader saves KV cache to disk: `{save_state_dir}/{model_id}.{label}.bin`
- Stores the label on `instance._state_label`

**Verdict**: ✅ State is saved to disk before child delegation.

#### Step 2: Transition to SLEEPING, release slot
**Location**: `execution_engine.py:4704-4748`
```python
def _transition_to_sleeping(self, instance):
    with instance._state_lock:
        if instance.state == AgentState.RUNNING:
            # Release slot under lock
            if instance._slot_release is not None:
                release_cb = instance._slot_release
                instance._slot_release = None
                release_cb()  # <-- SLOT RELEASED HERE
            
            self.pool._mark_activity(instance.instance_name)
            instance._transition(AgentState.SLEEPING)
```

**Key detail**: For conc=0 (sequential) endpoints, all such endpoints share `_shared_sequential_slot_` with capacity=1 (`api_router.py:326-328`). Releasing this slot means **any other agent on any conc=0 endpoint can now run**.

#### Step 3: Child B runs on same endpoint
Child B acquires the released `_shared_sequential_slot_` and runs. If using the same model as Maine:

- Autoloader's llama.cpp backend serves requests on its KV cache
- **The in-memory KV cache is now being used by B**, not Maine
- However, Maine's saved state file (`{model}.Maine.bin`) remains untouched on disk

#### Step 4: Maine wakes up after B completes
**Location**: `execution_engine.py:4793-4817`

When B finishes and its result is queued to Maine's message queue:
```python
if messages_list:
    # Wake up on ANY message
    with instance._state_lock:
        instance._transition(AgentState.RUNNING)  # SLEEPING → RUNNING
    
    # Inject drained messages
    self._drain_and_inject(...)
    
    # Re-acquire concurrency slot
    self._acquire_slot_with_logging(instance, "after_message_wakeup")
    
    return SleepAction.CONTINUE_LOOP, None
```

**Observation**: The wakeup path does **NOT** restore KV state. It only:
1. Transitions to RUNNING
2. Injects queued messages
3. Re-acquires the slot

#### Step 5: Maine restores state on next turn
**Location**: `execution_engine.py:1629-1630` (inside `_setup_turn`)

After wakeup returns CONTINUE_LOOP, the main loop continues and calls `_setup_turn()`:
```python
def _setup_turn(self, instance):
    # Restore state if agent has a saved label.
    from agent_cascade.state_ops import restore_instance_state
    restore_instance_state(instance)  # <-- RESTORE HAPPENS HERE
```

This POSTs to `{base}/v1/models/{model}/state/load` with Maine's label.
Autoloader loads the `.bin` file into the in-memory KV cache, overwriting whatever B left.

**Verdict**: ✅ State IS restored, but only at the start of the next turn.

## Answers to Specific Questions

### Q1: When does Maine restore its saved KV state?

**Answer**: At line 1630 in `_setup_turn()`, which runs at the **start of each turn iteration** (line 1207). After wakeup from SLEEPING, the flow is:

1. `_handle_sleeping_state()` returns CONTINUE_LOOP
2. Loop continues → eventually reaches line 1207
3. `_setup_turn()` is called → `restore_instance_state()` runs

The restore does **NOT** happen in the wakeup path itself (lines 4793-4817). It happens on the next loop iteration when Maine prepares its next turn.

**Important**: Although `restore_instance_state()` is called every turn, it's a no-op after the first successful restore because:
- Returns immediately if `label` is None/empty (state_ops.py:94-96)
- Label is cleared after successful restore (state_ops.py:118-120)

So the actual HTTP round-trip to autoloader happens exactly once per saved state, not every turn.

### Q2: Is there a window where another agent evicts Maine's saved state?

**Answer**: Yes, there is a window, but it doesn't cause data loss because states are disk-persisted.

The window is:
1. Maine releases slot at SLEEPING transition (line 4727)
2. Child B acquires slot and runs on same model
3. B's KV operations modify the **in-memory** cache
4. Maine wakes up, re-acquires slot (line 4812)
5. Maine calls `_setup_turn()` which restores from disk

During this window, the in-memory KV cache is overwritten by B's usage. But Maine's saved state file on disk is untouched. The restore at step 5 reloads Maine's state from disk correctly.

**Eviction risk**: If the autoloader's per-model LRU eviction (max 5 files) kicks in and deletes Maine's `.bin` file while sleeping, restore would fail and the label would be cleared (state_ops.py:112-113). This is unlikely under normal usage because eviction uses mtime-based LRU (server.py:1068), protecting recently saved files. Would require 6+ different agents saving states on the same model concurrently.

### Q3: Does autoloader persist states to disk or are they in-memory only?

**Answer**: States are **persisted to disk as `.bin` files**.

From `server.py`:
- Save endpoint (`POST /v1/models/{model_id}/state/save`) calls `save_state()` → writes to `{save_state_dir}/{model_id}.{label}.bin`
- Load endpoint (`POST /v1/models/{model_id}/state/load`) calls `load_state()` → reads from that `.bin` file
- Per-model eviction keeps max 5 most recent state files by mtime (`_cleanup_old_states`, line 1047)

The `.bin` files are llama.cpp KV cache snapshots written to the filesystem. They survive:
- Agent sleeping/waking cycles ✅
- Model reloads (autoloader can auto-restore on load, line 673) ✅
- Server restarts (files remain on disk) ✅

## Gaps and Concerns Identified

### Gap 1 (MINOR): Restore overhead is negligible

`restore_instance_state()` is called at the start of **every** turn via `_setup_turn()`. This looks like unnecessary overhead, but it's effectively a no-op after the first successful restore because:
- Returns immediately if `label` is None/empty (state_ops.py:94-96)
- Label is cleared after successful restore (state_ops.py:118-120)

The only cost is a lock acquire + None check per turn — trivial. The HTTP round-trip happens exactly once. This is good defensive design, not a real gap.

### Gap 2 (NOT A GAP): KV cache rebuild after wakeup is correct behavior

After wakeup, Maine's conversation has new messages (the async result from B). When `_setup_turn()` restores the KV state, it restores the cache as it was when saved — but the conversation now includes B's response. The KV cache matches the conversation up to the save point, and new tokens for B's response are computed fresh and appended. This is exactly the correct incremental rebuild behavior, not a problem.

### Gap 3 (BY DESIGN): State label overwrites on each save

The label is always `instance_name` (stable, per state_ops.py:149). Each save overwrites the same file. If Maine delegates multiple children sequentially without restoring in between, each save overwrites the previous one. This is intentional — only the most recent pre-delegation state is kept recoverable.

### Gap 4 (MINOR): Best-effort semantics, silent failures

State save/restore failures are logged at debug level and silently ignored. However, there's a fail-safe: the label is only stored on `instance._state_label` AFTER a successful save (state_ops.py:59-64). If save fails, `_state_label` remains None, so restore returns False immediately with no stale state to worry about. This means silent failures are actually safer than they appear.

### Gap 5 (REAL): No save-after-wakeup for updated conversation

After Maine wakes up and injects B's async result into its conversation, there's no save before the next delegation. If Maine then calls another child C, the saved state will be from BEFORE delegation to B (the original pre-delegation state), not including B's response. This means if C also runs on the same model, Maine's restored cache after C completes won't include B's context. This could lead to suboptimal cache reuse in chains of nested delegations.

## Confidence Level

**High Confidence** — based on direct code trace through:
- `tool_dispatcher.py` (save before delegation)
- `execution_engine.py` (SLEEPING transition, wakeup path, _setup_turn restore)
- `state_ops.py` (HTTP calls to autoloader)
- `llama-autoloader/server.py` (disk persistence as .bin files)

## Recommendations

1. **No change needed for restore placement**: The current design of restoring in `_setup_turn()` is fine — it's idempotent and the overhead is trivial (no HTTP after first restore). Moving it to wakeup path would add complexity without benefit.

2. **Consider save-after-wakeup (optional)**: After injecting async results into conversation on wakeup, consider saving state so subsequent delegations capture the updated context including children's responses. This would improve cache reuse in chains of nested delegations but is a nice-to-have optimization, not a correctness fix.

3. **No action needed for eviction**: The mtime-based LRU eviction with max 5 files per model provides adequate protection under normal usage patterns.
