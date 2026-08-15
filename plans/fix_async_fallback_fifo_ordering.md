# Fix: Async-to-Sync Fallback FIFO Ordering Violation (v3 — Final)

## Problem (todo.md line 111)

When an async agent falls back to a sync slot, models trash because each agent calls the
sync slot with different models without respecting FIFO scheduling order.

**Scenario:** Agent A (sync) calls B (async). B's endpoint fails, falls back to a conc=0
endpoint (shared sequential slot). B's LLM calls interleave with A's on that slot instead
of waiting for A to finish.

## Root Cause

Two independent concurrency control layers:
1. **SlotPool (Layer 1)**: FIFO, per-slot-key. Acquired at agent lifecycle level in `engine.run()`.
2. **Semaphore (Layer 2)**: Non-FIFO, per-api_base. Used per-API-call inside `call_with_fallback()`.

When an agent's actual endpoint (after fallback) differs from its configured endpoint,
Layer 1 doesn't protect the actual endpoint. Layer 2's semaphore is non-FIFO → interleaving.

## Fix Design

Route conc=0 LLM calls through SlotPool (FIFO) when the agent doesn't already hold a slot
for that endpoint.

### Step 1: Track slot key on AgentInstance

**`agent_cascade/agent_instance.py`** — Add field:
```python
_slot_key: Optional[str] = None  # Slot key of currently held SlotPool slot
```

**`agent_cascade/execution_engine.py`** — In `_acquire_slot_with_logging()`:
```python
def _acquire_slot_with_logging(self, instance: AgentInstance, context: str = "initial") -> None:
    ...
    instance._slot_release = self.pool._acquire_slot(
        instance.agent_class, instance.instance_name
    )
    # Store slot key for fallback detection in call_with_fallback.
    # Must pass caller_agent_type to handle Tier 2 endpoint inheritance.
    if instance._slot_release is not None:
        router = self.pool.api_router
        if router:
            caller_agent_type = None
            if getattr(instance, 'parent_instance', None):
                parent = self.pool.get_instance(instance.parent_instance)
                if parent and hasattr(parent, 'agent_class') and not getattr(parent, 'is_terminated', False):
                    caller_agent_type = parent.agent_class
            slot_info = router.get_agent_slot_info(
                instance.agent_class, caller_agent_type=caller_agent_type
            )
            instance._slot_key = slot_info.get('slot_key')
    ...
```

**Clear `_slot_key` in ALL places where `_slot_release` is set to None:**
- `execution_engine._release_slot()` (line ~4685)
- `execution_engine._transition_to_sleeping()` (line ~4720)
- `execution_engine.run()` finally block (line ~1558, via `_release_slot`)
- `tool_dispatcher._reacquire_caller_slot()` failure path (line ~760)

### Step 2: Modify call_with_fallback for conc=0 endpoints

**`agent_cascade/api_router.py`**

Add module-level constant near other constants:
```python
PER_CALL_TIMEOUT = 30.0  # Timeout for per-call SlotPool acquisition during fallback
```

Add imports at top:
```python
from agent_cascade.slot_queue import SlotQueueTimeout, SlotCancelled
```

In `call_with_fallback()`, inside the per-endpoint loop, after resolving `concurrency_limit`:

```python
# ── CONCURRENCY CONTROL ──
sem = None
if concurrency_limit >= 0:
    sem_size = max(1, concurrency_limit)
    with self._sem_lock:
        if endpoint_base not in self._semaphores or self._semaphores[endpoint_base][1] != sem_size:
            self._semaphores[endpoint_base] = (threading.Semaphore(sem_size), sem_size)
        sem = self._semaphores[endpoint_base][0]

# NEW: For conc=0 endpoints, use SlotPool (FIFO) instead of semaphore
# when the agent doesn't already hold a slot for this endpoint.
slotpool_release_cb = None
if concurrency_limit == 0 and _inst_name and self._pool:
    slot_key = '_shared_sequential_slot_'  # conc=0 always maps to shared sequential
    
    inst = self._pool.get_instance(_inst_name)
    already_holds = (inst is not None and getattr(inst, '_slot_key', None) == slot_key)
    
    if not already_holds:
        try:
            slotpool_release_cb = self.scheduler.acquire(
                api_base=endpoint_base,
                concurrency_limit=concurrency_limit,
                instance_name=_inst_name,
                agent_class=agent_type,
                timeout=PER_CALL_TIMEOUT,
            )
            if slotpool_release_cb is not None:
                logger.debug(
                    f"[APIRouter] Fallback slot acquired for '{_inst_name}' on "
                    f"'{endpoint_base}' (conc=0, FIFO via SlotPool)"
                )
        except SlotQueueTimeout:
            raise  # Propagate — retry logic handles it
        except SlotCancelled:
            raise  # Agent terminated during wait — clean abort
```

Modify `execute_with_sem` to handle SlotPool release:

```python
def execute_with_sem(current_agent_name=None):
    # NEW: SlotPool path for conc=0 fallback (FIFO ordering)
    if slotpool_release_cb is not None:
        try:
            result = call_fn(llm_cfg, *args, **kwargs)
            if hasattr(result, '__iter__') and not isinstance(result, (list, dict, str)):
                it = iter(result)
                first_chunk = next(it)
                
                def slotpool_gen_wrapper(first, rest, _release=slotpool_release_cb):
                    yield first
                    try:
                        yield from rest
                    finally:
                        _release()
                        logger.debug(
                            f"[APIRouter] Fallback slot released for '{_inst_name}' on "
                            f"'{endpoint_base}'"
                        )
                return slotpool_gen_wrapper(first_chunk, it)
            else:
                slotpool_release_cb()  # Non-generator — release immediately
                return result
        except Exception:
            slotpool_release_cb()
            raise
    
    # Existing semaphore path (conc>0, or conc=0 when agent already holds slot)
    if not sem:
        return call_fn(llm_cfg, *args, **kwargs)
    ...  # rest unchanged
```

### Edge Cases

| Case | Behavior |
|------|----------|
| Agent conc=-1, fallback conc=0 | Acquires SlotPool slot. FIFO maintained. ✓ |
| Agent holds slot on X, fallback Y (conc=0) | Acquires Y's slot. Holds X+Y simultaneously (safe, different pools). ✓ |
| Agent already holds conc=0 slot, fallback same endpoint | Skip acquisition. ✓ |
| Agent inherits conc=0 from caller (Tier 2) | `_slot_key` computed with `caller_agent_type`. Detection works. ✓ |
| Timeout (30s) | SlotQueueTimeout raised. Retry logic handles. ✓ |
| Termination during wait | SlotCancelled raised. Clean abort. ✓ |
| Generator finalization | Release in `finally` after iteration completes. ✓ |

### Files to Modify

1. **`agent_cascade/agent_instance.py`**: Add `_slot_key: Optional[str] = None`.
2. **`agent_cascade/execution_engine.py`**: 
   - Set `_slot_key` in `_acquire_slot_with_logging()` (with `caller_agent_type`).
   - Clear `_slot_key` in `_release_slot()`, `_transition_to_sleeping()`.
3. **`agent_cascade/api_router.py`**: 
   - Add `PER_CALL_TIMEOUT = 30.0` module constant.
   - Import `SlotQueueTimeout`, `SlotCancelled`.
   - Modify `call_with_fallback()` for conc=0 SlotPool path.
4. **`agent_cascade/tool_dispatcher.py`**: Clear `_slot_key` in `_reacquire_caller_slot()` failure.

### Testing Plan

**Unit tests (new file: `tests/test_fallback_fifo_ordering.py`):**
1. Agent A holds shared sequential slot. Agent B falls back to same conc=0 endpoint → B waits FIFO.
2. Agent with conc=-1 configured falls back to conc=0 → SlotPool acquisition happens.
3. Agent already holds conc=0 slot, fallback same endpoint → no double acquisition.
4. Inherited conc=0 endpoint (Tier 2) → `_slot_key` correctly set with caller context.
5. Per-call timeout: mock busy slot → timeout after PER_CALL_TIMEOUT.
6. Generator finalization: release called after generator completes.
7. Termination during wait: SlotCancelled propagates cleanly.

**Regression:** Existing slot_queue and call_with_fallback tests pass unchanged.

### Risk: Low
- Only affects conc=0 fallback path. Normal operation unchanged.
- No deadlock possible (different pools, independent capacity).
- Slight overhead of SlotPool.acquire() for conc=0 fallback calls. Negligible.
