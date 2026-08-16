# Investigation Report: Security Advisor Slot Deadlock/Timeout

**Date:** 2026-08-16  
**Investigator:** sec_slot_investigator (researcher)  
**Status:** Root cause identified — fix plan ready for implementation  
**Related memory:** `[[slot-inheritance-self-deadlock]]` (prior Tier-2 inheritance fix, 2026-08-15)

---

## Executive Summary

The Security advisor check times out because the shared sequential slot (`_shared_sequential_slot_`, capacity 1) is held by agent instance `screen_capture_fix` and never released before the Security agent attempts to acquire it. The existing "slot yield" mechanism in `security_handler.py` (lines 563-572) is **fragile and has multiple failure modes** that can prevent the caller's slot from being released, causing a persistent deadlock.

**Design goal (per user directive, 2026-08-16):** There must be NO slot borrowing/inheritance. Every agent meters against its OWN resolved endpoint pool. When a tool invokes the Security agent, the caller's slot MUST be released before the Security agent runs. The current implementation attempts this via a "yield" but does so in a way that is not guaranteed to succeed.

The root cause is a combination of:
1. **The yield condition only checks `caller_inst_sec._slot_release is not None`** (security_handler.py:565). If this is False for ANY reason — timing, reuse-path clearing at lifecycle_manager.py:502-503, or any other path that clears `_slot_release` without releasing the pool permit — NO yield happens and the slot remains held by the caller.
2. **There is NO pool-level verification** — the code never checks whether the SlotPool actually shows the caller as a holder. It only checks the instance's `_slot_release` callback. If there's a mismatch (callback is None but pool still holds a permit), the yield silently fails with no log message.
3. **There is NO force-release fallback** — if `_slot_release` is missing but the pool shows the caller as a holder, there is no mechanism to detect and force-release the leaked permit. The `SlotPool.release()` method exists (slot_queue.py) but is only reachable via the `_slot_release` callback.
4. **The security check runs in a separate daemon thread** (security_handler.py:315-322), creating a race window where the caller's state can change between when the approval is created and when the yield logic checks it.

The result: Security agent waits 300s for the shared sequential slot → times out → check fails with "REJECTED: Security check error: Timed out after Nones waiting for endpoint slot... Currently held by: screen_capture_fix".

**Confirmed in this incident (log `coder_screen_capture_fix_20260816_062151.jsonl`):**
- Line 20 (06:22:48): `screen_capture_fix` calls `edit_file` → blocks on approval while holding the shared sequential slot.
- Line 21 (06:27:48): `REJECTED: Security check error: Timed out after Nones waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: screen_capture_fix (screen_capture_fix)`.
- The yield did NOT release the slot — `screen_capture_fix` was still holding it at timeout.

---

## Key Findings (with file:line evidence)

### Finding 1: The slot holder is `screen_capture_fix` — a coder agent blocked on approval

**Evidence:**
- Log line 142 (todo.md): `"Currently held by: screen_capture_fix (screen_capture_fix)"`
- Log file `coder_screen_capture_fix_20260816_062151.jsonl` line 1: metadata shows `agent_class=coder, instance_name=screen_capture_fix, supervisor=Maine`
- Log line 145 (todo.md): `Endpoint allocation updated for coder: {'api_base': 'http://127.0.0.1:1234/v1', 'concurrency_limit': 0}` → conc=0 maps to `_shared_sequential_slot_` (capacity 1) per api_router.py:269-270

**Mechanism:**
- `screen_capture_fix` is a coder agent spawned by Maine via call_agent.
- It runs `engine.run()` which acquires the shared sequential slot at execution_engine.py:1154 (`self._acquire_slot_with_logging(instance, "initial")`).
- It then blocks inside `request_user_approval` (approval.py:84-152) waiting for user approval on an `edit_file` tool call.
- While blocked, it holds the slot via `_slot_release` callback (set at execution_engine.py:889).

**Approval creation:**
- `edit_file` → `request_user_approval(agent_name='screen_capture_fix', ...)` at file_operations.py:1019-1024.
- The approval dict stores `'agent_name': 'screen_capture_fix'` (approval.py:186, via `list_pending_approvals`).

### Finding 2: The security check resolves `caller_agent` correctly in this incident — but the yield mechanism is fragile

**Evidence:**
- security_handler.py:290-292:
  ```python
  caller_agent = ap.get('agent_name')  # = 'screen_capture_fix'
  if not caller_agent or (self.agent_pool and self.agent_pool.get_instance(caller_agent) is None):
      caller_agent = instance_name  # fallback to session name (e.g., 'Maine')
  ```
- In this incident, `ap['agent_name']` = `'screen_capture_fix'`, so `caller_agent` = `'screen_capture_fix'` — the CORRECT instance.

**However**, the yield logic at lines 563-572 has a critical flaw:
```python
if caller_inst_sec and hasattr(caller_inst_sec, '_state_lock'):
    with caller_inst_sec._state_lock:
        if getattr(caller_inst_sec, "_slot_release", None) is not None:
            _yielded_slot = True  # Mark BEFORE releasing (under lock)

if _yielded_slot:
    engine._release_slot(caller_inst_sec, caller_agent, "before_security_check")
```

**Failure mode:** If `caller_inst_sec._slot_release` is None at the moment this check runs, `_yielded_slot` stays False and NO yield happens. This can occur when:
- The instance was reused and `_slot_release` was cleared at lifecycle_manager.py:502-503 WITHOUT releasing the pool permit (a slot leak).
- There's a race where the security daemon thread checks `_slot_release` before `engine.run()` has set it (if the security check is triggered very early in the agent's lifecycle).
- Any other code path clears `_slot_release` without releasing the pool permit.

### Finding 3: The security check runs in a SEPARATE daemon thread — creating a race window

**Evidence:**
- security_handler.py:315-322:
  ```python
  threading.Thread(
      target=self._run_check_worker,
      args=(ap, sec_inst, rid, auto_apply, instance_name, caller_agent, ...),
      daemon=True,
  ).start()
  ```
- The security check is triggered via WebSocket `ask_security` (ws_handlers.py:976-991) from the WebUI's auto-security mode.
- This runs CONCURRENTLY with the caller agent (`screen_capture_fix`) which is blocked on approval.

**Race scenario:**
1. `screen_capture_fix` calls `edit_file` → `request_user_approval` blocks (approval.py:132: `approval.event.wait(timeout=0.1)` in a polling loop).
2. WebUI detects the pending approval and sends `ask_security` via WebSocket.
3. Security daemon thread starts, resolves `caller_agent='screen_capture_fix'`, checks `_slot_release`.
4. **If at this exact moment `screen_capture_fix._slot_release` is None** (e.g., due to a prior reuse-path clear, or a timing gap), no yield happens.
5. Security agent calls `engine.run()` → `_acquire_slot_with_logging` → `pool._acquire_slot` → blocks on `_shared_sequential_slot_` which is still held by `screen_capture_fix`.
6. After 300s, timeout → "Currently held by: screen_capture_fix".

### Finding 4: There is NO fallback to force-release the pool permit if `_slot_release` is missing

**Evidence:**
- The yield logic (security_handler.py:563-572) ONLY releases via the `_slot_release` callback.
- If `_slot_release` is None but the pool still shows the instance as a holder (a leaked permit), there is NO mechanism to detect and force-release it.
- Compare with `reacquire_for()` (execution_engine.py:4678-4756) which has proper timeout handling, but this is for RE-acquiring, not for force-releasing a leaked permit.

**Related leak path:** lifecycle_manager.py:502-503 clears `_slot_release` and `_slot_key` to None on instance reuse WITHOUT releasing the pool permit:
```python
# SLOT_TIMEOUT FIX: Clear _slot_release to prevent stale callback issues
instance._slot_release = None
instance._slot_key = None
```
This is a known leak vector (documented in `[[lessons_slot_timeout_fix]]`) but the "fix" was only about preventing stale callbacks, not about releasing the underlying pool permit.

### Finding 5: Secondary bug — "Timed out after Nones" message

**Evidence:**
- api_router.py:370:
  ```python
  raise TimeoutError(
      f"Timed out after {timeout}s waiting for endpoint slot on {api_base}. "
      ...
  )
  ```
- The `timeout` parameter (line 288) is `Optional[float] = None`. When not provided, it defaults to None.
- Line 332 computes `effective_timeout = timeout if timeout is not None else (QUEUE_WAIT_TIMEOUT or ENDPOINT_SLOT_ACQUIRE_TIMEOUT)` — but line 370 uses the RAW `timeout` parameter (None) instead of `effective_timeout`.
- Result: "Timed out after **Nones** waiting for endpoint slot..."

**Fix:** Change line 370 to use `effective_timeout` instead of `timeout`.

### Finding 6: The prior Tier-2 inheritance fix (2026-08-15) addressed a DIFFERENT deadlock

**Evidence:**
- Memory `[[slot-inheritance-self-deadlock]]` documents that Security/Compressor agents inherited the caller's endpoint via Tier-2 fallback in api_router.py, causing them to meter against the SAME `_shared_sequential_slot_` and wait on their own freed permit.
- That fix (removing Tier-2 inheritance) was implemented and verified (365 tests passed).
- **This new incident is NOT a self-deadlock** — it's a case where the CALLER (`screen_capture_fix`) holds the slot and the Security agent waits for it. The yield mechanism should have released the caller's slot, but failed to do so.

### Finding 7: Stale "Rule 4 inheritance" comment vestige (AMENDED)

**Evidence:**
- agent_pool.py:2689 (BEFORE fix): `# inline on parent's thread and inherit its permit via Rule 4 (no separate slot needed).`
- This comment described the OLD Stage-2/3 "Rule 4" behavior where sync children inherited the parent's slot permit. This behavior was REMOVED in the Stage 3/4 slot consolidation.
- The current behavior (per tool_dispatcher.py:496-504) is that sync children do NOT inherit — the caller RELEASES its slot and the child acquires its own via engine.run().

**Impact:** This stale comment was misleading documentation that contradicted the actual code behavior. It could cause future developers to reintroduce inheritance logic or misunderstand the slot model. Per the user directive ("any old documentation must be amended to reflect the current implementation goal"), this comment has been AMENDED to clearly state the no-borrowing design goal.

**Fix applied:** agent_pool.py:2687-2695 now reads:
```python
# NOTE: Slot acquisition happens later when the child agent actually runs,
# not at spawn time. Spawn just registers the async task.
#
# DESIGN GOAL (2026-08-16): There is NO slot borrowing/inheritance. Every agent
# meters against its OWN resolved endpoint pool. Sync children run inline on the
# parent's thread, but they do NOT inherit the parent's permit — the parent
# RELEASES its slot (tool_dispatcher._run_child_sync) and the child acquires its
# own via engine.run(). The old "Rule 4: sync children inherit parent's permit"
# behavior was removed in the Stage 3/4 slot consolidation.
```

**Verification:** A grep for `inherit|borrow|caller's slot` across the codebase confirmed that:
- api_router.py:828-831 and 1031-1033 correctly state "Tier-2 caller inheritance was removed" (accurate, no change needed).
- security_handler.py:430 correctly states "(Security resolves its OWN endpoint pool — no caller inheritance.)" (accurate).
- execution_engine.py:1147-1148 correctly states "Nested agents (Security, Compressor) now yield their caller's slot before running, so they acquire normally here instead of inheriting the parent's slot." (accurate).
- The ONLY stale comment was agent_pool.py:2689 (now amended).

---

## Exact Sequence of Events (this incident)

```
06:21:51.797  screen_capture_fix (coder) created by Maine via call_agent
06:21:51.845  Endpoint allocation for coder → conc=0 → _shared_sequential_slot_
06:21:51.???  screen_capture_fix calls engine.run() → acquires shared sequential slot
              (execution_engine.py:1154) → _slot_release set
06:22:??      screen_capture_fix calls edit_file → request_user_approval blocks
              (approval.py:84-152, polling loop at line 132)
06:22:48.240  WebUI auto-security sends ask_security via WebSocket
06:22:48.240  Security daemon thread starts (_run_check_worker)
06:22:48.244  _create_system_agent creates Security_op_465352c3
06:22:48.276  engine.run(sec_instance) called
06:22:48.285  _acquire_slot → blocks on _shared_sequential_slot_ (held by screen_capture_fix)
              [YIELD SHOULD HAVE HAPPENED HERE — but did not]
06:27:48.300  SlotPool acquire timeout after 300s
06:27:48.305  "Currently held by: screen_capture_fix (screen_capture_fix)"
06:27:48.308  Security check fails → REJECTED
```

**Why the yield didn't happen:** The most likely cause is that `screen_capture_fix._slot_release` was None at the moment the security daemon thread checked it (line 565), even though the pool still held a permit for `screen_capture_fix`. This could be due to:
- A race condition where `_slot_release` was cleared by another code path (e.g., lifecycle_manager.py:502 on reuse) without releasing the pool permit.
- Or, less likely, a timing gap where the security check ran before `engine.run()` had set `_slot_release`.

Without more detailed logging (the log doesn't show whether `[SECURITY_SLOT_YIELD]` was emitted), we cannot definitively determine which sub-cause triggered this specific incident. However, the code structure makes it clear that the yield mechanism is fragile and has no fallback for leaked permits.

---

## Why the Existing "Slot Yield" Mechanism Fails

The yield mechanism (security_handler.py:563-572) has these design flaws:

1. **Single-point-of-failure on `_slot_release`:** The entire yield depends on `caller_inst_sec._slot_release` being non-None. If it's None for ANY reason (timing, reuse-path clear, bug), no yield happens and the deadlock persists.

2. **No pool-level verification:** The code never checks whether the pool actually shows the caller as a holder. It only checks the instance's `_slot_release` callback. If there's a mismatch (callback is None but pool still holds a permit), the yield silently fails.

3. **No force-release fallback:** There is no mechanism to force-release a leaked pool permit. The `SlotPool.release()` method exists (slot_queue.py:260-271) but is only called via the `_slot_release` callback. If the callback is lost, the permit is stuck forever.

4. **Race condition with daemon thread:** The security check runs in a separate thread from the caller. Any state change to `caller_inst_sec._slot_release` between the time the approval is created and the time the security thread checks it can cause the yield to fail.

5. **No logging on yield failure:** If `_yielded_slot` is False, there's no log message indicating that the yield was SKIPPED. This makes debugging extremely difficult (as evidenced by this investigation — we can't tell from the logs whether the yield was attempted).

---

## Recommended Fix Plan

### Goal
Ensure that when a tool invokes the Security agent, the caller's slot is ALWAYS released before the Security agent runs, and re-acquired after. Eliminate all slot-borrowing logic (per user directive).

### Fix 1: Add pool-level verification + force-release fallback in security_handler.py

**Location:** security_handler.py, lines 563-572 (the yield block)

**Change:** Before deciding whether to yield, check if the pool shows the caller as a holder. If it does but `_slot_release` is None, force-release via the pool's release mechanism.

```python
# ── Slot yield for Security advisor ────────────────────────────
caller_inst_sec = self.agent_pool.get_instance(caller_agent) if caller_agent else None
_yielded_slot = False

if caller_inst_sec and hasattr(caller_inst_sec, '_state_lock'):
    with caller_inst_sec._state_lock:
        # Check if the instance has a slot release callback
        if getattr(caller_inst_sec, "_slot_release", None) is not None:
            _yielded_slot = True  # Mark BEFORE releasing (under lock)

# NEW: Pool-level verification + force-release fallback
if not _yielded_slot and caller_inst_sec:
    # Check if the pool still shows this instance as a holder (leaked permit)
    router = self.agent_pool.api_router
    if router:
        slot_info = router.get_agent_slot_info(caller_inst_sec.agent_class)
        if slot_info and slot_info.get('needs_slot'):
            api_base = slot_info['api_base']
            concurrency_limit = slot_info['concurrency_limit']
            # Check if the pool shows this instance as a holder
            sched_pool = router.scheduler._get_or_create_pool(api_base, concurrency_limit)
            if sched_pool and caller_agent in sched_pool._running:
                logger.warning(
                    f"[SECURITY_SLOT_YIELD] LEAKED PERMIT DETECTED: '{caller_agent}' "
                    f"holds pool permit but _slot_release is None. Force-releasing."
                )
                # Force-release via the pool's release mechanism
                holder = sched_pool._running[caller_agent]
                sched_pool.release(holder)
                with caller_inst_sec._state_lock:
                    caller_inst_sec._slot_release = None
                    caller_inst_sec._slot_key = None
                _yielded_slot = True  # Mark as yielded so finally block re-acquires

if _yielded_slot:
    logger.debug(
        f"[SECURITY_SLOT_YIELD] Releasing slot for '{caller_agent}' before Security check"
    )
    engine._release_slot(caller_inst_sec, caller_agent, "before_security_check")
```

**Rationale:** This adds a safety net that detects leaked permits (where the pool shows a holder but the instance's `_slot_release` is None) and force-releases them. This ensures the Security agent can always acquire the slot.

### Fix 2: Add logging when yield is SKIPPED

**Location:** security_handler.py, after line 572

**Change:** Add an else branch that logs when the yield was skipped:

```python
if _yielded_slot:
    logger.debug(
        f"[SECURITY_SLOT_YIELD] Releasing slot for '{caller_agent}' before Security check"
    )
    engine._release_slot(caller_inst_sec, caller_agent, "before_security_check")
else:
    logger.warning(
        f"[SECURITY_SLOT_YIELD_SKIPPED] No slot to yield for '{caller_agent}'. "
        f"_slot_release is None and no leaked permit detected. "
        f"Security agent may block on shared sequential slot."
    )
```

**Rationale:** This makes it immediately obvious in the logs when the yield was skipped, enabling faster debugging of future incidents.

### Fix 3: Fix the "Timed out after Nones" message bug

**Location:** api_router.py:370

**Change:** Use `effective_timeout` instead of `timeout`:

```python
# BEFORE (line 369-372):
raise TimeoutError(
    f"Timed out after {timeout}s waiting for endpoint slot on {api_base}. "
    f"Current active count: {len(sched_pool._running)}, max allowed: {sched_pool.capacity}{holder_info}"
) from e

# AFTER:
raise TimeoutError(
    f"Timed out after {effective_timeout}s waiting for endpoint slot on {api_base}. "
    f"Current active count: {len(sched_pool._running)}, max allowed: {sched_pool.capacity}{holder_info}"
) from e
```

**Rationale:** `timeout` is the raw parameter (None if not provided), while `effective_timeout` is the actual timeout used (line 332). This fixes the misleading "Timed out after Nones" message.

### Fix 4: Add a slot leak detector to SlotPool (optional but recommended)

**Location:** slot_queue.py, in the `SlotPool` class

**Change:** Add a method to detect and log leaked permits (holders that have been held for an unusually long time without being released):

```python
def detect_leaked_permits(self, max_age_seconds: float = 600.0) -> List[Dict]:
    """Detect permits that have been held longer than max_age_seconds.
    
    Returns a list of dicts with holder info for diagnostics/logging.
    """
    now = time.monotonic()
    leaked = []
    with self._cond:
        for instance_name, holder in self._running.items():
            age = now - holder.granted_at
            if age > max_age_seconds:
                leaked.append({
                    'instance_name': instance_name,
                    'agent_name': holder.agent_name,
                    'held_duration': round(age, 2),
                    'acquisition_id': holder.acquisition_id,
                })
    return leaked
```

**Rationale:** This provides a diagnostic tool to identify and log leaked permits proactively, rather than waiting for a timeout to discover them.

### Fix 5: Document the design goal — NO slot borrowing

**Location:** Add a comment at the top of security_handler.py (near line 487) and in the project memory

**Change:** Clarify that the design goal is:
- Every agent meters against its OWN resolved endpoint pool.
- When a tool invokes the Security agent, the caller's slot MUST be released before the Security agent runs.
- There is NO slot borrowing — the Security agent acquires its own slot via the normal FIFO queue.

**Rationale:** This prevents future developers from reintroducing slot-borrowing logic or inheritance patterns that cause deadlocks.

---

## Secondary Issues Found

### Issue 1: "Timed out after Nones" message (api_router.py:370)
- **Severity:** Low (cosmetic, but confusing for debugging)
- **Fix:** See Fix 3 above.

### Issue 2: No logging when yield is skipped (security_handler.py:563-572)
- **Severity:** Medium (makes debugging extremely difficult)
- **Fix:** See Fix 2 above.

### Issue 3: Slot leak path in lifecycle_manager.py:502-503
- **Severity:** High (root cause of the leaked permit that triggers this deadlock)
- **Description:** When an instance is reused (`is_reuse=True`), `_slot_release` and `_slot_key` are cleared to None WITHOUT releasing the pool permit. This creates a leaked permit that blocks all other agents on the shared sequential slot.
- **Fix:** Before clearing `_slot_release`, check if it's non-None and release it first:
  ```python
  # BEFORE (lifecycle_manager.py:502-503):
  instance._slot_release = None
  instance._slot_key = None
  
  # AFTER:
  if instance._slot_release is not None:
      release_cb = instance._slot_release
      instance._slot_release = None
      instance._slot_key = None
      try:
          release_cb()
      except Exception as e:
          logger.error(f"[SLOT_RELEASE_ERROR] Failed to release slot for {instance.instance_name} on reuse: {e}", exc_info=True)
  else:
      instance._slot_key = None
  ```

### Issue 4: No pool-level verification in the yield logic
- **Severity:** High (allows leaked permits to persist undetected)
- **Fix:** See Fix 1 above.

---

## Suggested Test Scenarios

### Test 1: Basic security check with caller holding slot
- **Setup:** Create a coder agent, have it call `edit_file` (which blocks on approval), trigger auto-security via WebSocket.
- **Expected:** Security agent acquires the slot after the caller's slot is yielded, runs to completion, and the caller re-acquires its slot.
- **Verify:** No timeout, no "Currently held by" error in logs.

### Test 2: Leaked permit detection + force-release
- **Setup:** Simulate a leaked permit by manually clearing `caller_inst._slot_release` without releasing the pool permit (mimicking lifecycle_manager.py:502).
- **Expected:** The new pool-level verification detects the leaked permit, logs a warning, and force-releases it. Security agent proceeds normally.
- **Verify:** Log shows `[SECURITY_SLOT_YIELD] LEAKED PERMIT DETECTED` and the check completes without timeout.

### Test 3: Yield skipped logging
- **Setup:** Trigger a security check where the caller has no slot (e.g., conc=-1 unlimited endpoint).
- **Expected:** Log shows `[SECURITY_SLOT_YIELD_SKIPPED]` with a clear explanation.
- **Verify:** No false-positive "deadlock" errors; Security agent runs normally (no slot needed).

### Test 4: Concurrent security checks on different callers
- **Setup:** Two agents (A and B) both call `edit_file` simultaneously, triggering two concurrent security checks.
- **Expected:** Both checks complete without deadlock. The FIFO queue ensures fair ordering.
- **Verify:** No timeouts, no "Currently held by" errors.

### Test 5: Regression — prior Tier-2 inheritance fix still works
- **Setup:** Run the existing test suite for slot consolidation (`pytest tests/ -k "slot or security or compress"`).
- **Expected:** All 365+ tests pass (no regression from the new fixes).
- **Verify:** No `caller_agent_type` / `_resolve_inherited_endpoints` references remain.

### Test 6: "Timed out after Nones" fix verification
- **Setup:** Trigger a slot acquire timeout (e.g., by holding the shared sequential slot and requesting another agent to acquire it with a short timeout).
- **Expected:** Error message shows "Timed out after 300s" (or the actual timeout value), NOT "Timed out after Nones".
- **Verify:** Log/message contains the correct timeout value.

---

## Maine's Verification (2026-08-16, post-investigation)

I verified the investigation against the actual incident log (`logs/console.log`) and the current source. Key confirmations and refinements:

### Confirmed: The yield did NOT fire in this incident
Searching `console.log` for the 06:22–06:28 window around request `op_465352c3`:
- `06:22:48,276` `[SECURITY] Created AgentInstance 'Security_op_465352c3'` (security_handler.py:462)
- `06:22:48,285` `engine.run() ENTRY - instance=Security_op_465352c3` (execution_engine.py:1099)
- **NO `[SECURITY_SLOT_YIELD] Releasing slot for 'screen_capture_fix'` line exists.**

In every working incident in the log (e.g. `Releasing slot for 'Maine'`, `'api_hang_fix'`, `'quick_test_fixes'`), that yield line IS present. Its absence here proves `_yielded_slot` was `False` — i.e. `screen_capture_fix._slot_release` was `None` at the moment the security daemon thread checked it (security_handler.py:565), even though the pool still held a permit for `screen_capture_fix` (the timeout message "Currently held by: screen_capture_fix" is read live from `sched_pool._running`).

### Confirmed: caller_agent resolves correctly to `screen_capture_fix`
- Tools are instantiated with `agent_name=self.name` (agent.py:214/245), so `edit_file`'s `self.agent_name == 'screen_capture_fix'`.
- `request_user_approval(agent_name='screen_capture_fix')` stores `agent_name='screen_capture_fix'` in the approval (approval.py:108, 186).
- security_handler.py:290 reads `ap.get('agent_name')` → `caller_agent == 'screen_capture_fix'`. ✓

So the yield logic was looking at the CORRECT instance; that instance's `_slot_release` was simply None.

### Confirmed: normal release paths are correct (no double-release risk in Fix 1)
- `execution_engine._release_slot` (4643) and `_transition_to_sleeping` (4758) both capture the callback, null `_slot_release`, then invoke it — so a "normal" release would NOT leave a stale pool entry.
- `SlotPool.release()` (slot_queue.py:209-217) is **idempotent-safe**: it checks `existing.acquisition_id != holder.acquisition_id` and returns if mismatched. Therefore Fix 1's force-release via `sched_pool._running[caller_agent]` + `sched_pool.release(holder)` is safe — it releases a genuine leaked permit and is a no-op if the permit was already released.

### Refined understanding of the defect
The exact sub-cause that nulls `_slot_release` while leaving the pool permit (race vs. a specific code path) is not pinned down in this log, but it does not change the fix: **the yield mechanism has a single point of failure on `_slot_release` with no pool-level verification, no force-release fallback, and no logging when it skips.** Any sub-cause that produces "pool holds permit + `_slot_release` is None" will deadlock. The fix below makes the security path robust to all such cases and self-diagnosing.

### Plan refinements applied
1. **Fix 1 (force-release fallback)** — kept, verified safe. Additionally: when the yield is skipped (no leaked permit found), log the current pool holders so the next incident is immediately debuggable from logs alone.
2. **New Fix 6** — add a diagnostic log in the security acquire path showing who holds the shared sequential slot at acquire time (low-cost, high-value for this bug class).
3. Fixes 2/3/5 unchanged.

---

## Confidence Level

**High confidence** in the root cause analysis:
- The code structure clearly shows that the yield mechanism depends solely on `_slot_release` being non-None, with no fallback for leaked permits.
- The log evidence confirms that `screen_capture_fix` was holding the slot when Security timed out.
- The prior Tier-2 inheritance fix (documented in `[[slot-inheritance-self-deadlock]]`) addressed a different deadlock mode (self-wait), not this one (caller-holds-slot).

**Moderate confidence** in the specific sub-cause of WHY `_slot_release` was None in this incident:
- Without more detailed logging (the log doesn't show whether `[SECURITY_SLOT_YIELD]` was emitted), we cannot definitively determine whether it was a race condition, a reuse-path clear, or another cause.
- However, the code structure makes it clear that ANY of these causes would produce the same symptom, and the fix plan addresses all of them.

---

## Open Questions

1. **Was `[SECURITY_SLOT_YIELD]` logged in this incident?** The log excerpt in todo.md doesn't show it, but the full log file might have more detail. If it WAS logged, then the yield was attempted but failed silently (which would indicate a bug in `_release_slot`). If it was NOT logged, then `_yielded_slot` was False (which indicates `_slot_release` was None).

2. **Was `screen_capture_fix` reused before this incident?** If so, lifecycle_manager.py:502-503 would have cleared `_slot_release` without releasing the pool permit, creating a leaked permit. The log shows it was created at 06:21:51, but we don't know if it was reused before the security check at 06:22:48.

3. **Are there other code paths that clear `_slot_release` without releasing the pool permit?** A grep found 16 locations where `_slot_release = None` is set. Most are in proper release patterns (capture-nullify-release), but lifecycle_manager.py:502-503 is a known exception. A thorough audit of all 16 locations would be prudent.

---

## Suggested Next Actions

1. **Implement Fix 1 + Fix 2** (pool-level verification + force-release fallback + skip logging) — this is the critical fix that prevents the deadlock.
2. **Implement Fix 3** (timeout message bug) — quick cosmetic fix.
3. **Implement Fix 5** (document the design goal) — prevent future regressions.
4. **Audit all 16 locations where `_slot_release = None` is set** to ensure they all follow the capture-nullify-release pattern.
5. **Implement Fix 4** (slot leak detector) as a diagnostic tool for future incidents.
6. **Run the suggested test scenarios** to verify the fixes work and don't introduce regressions.
7. **Update the project memory** `[[slot-inheritance-self-deadlock]]` to reference this new incident and the additional fix plan.

---

## Files Modified (planned)

| File | Lines | Change |
|------|-------|--------|
| `security_handler.py` | 563-572 | Add pool-level verification + force-release fallback + skip logging |
| `api_router.py` | 370 | Use `effective_timeout` instead of `timeout` in error message |
| `lifecycle_manager.py` | 502-503 | Release slot before clearing `_slot_release` on reuse |
| `slot_queue.py` | (new method) | Add `detect_leaked_permits()` diagnostic method |

---

## Related Memories

- `[[slot-inheritance-self-deadlock]]` — Prior Tier-2 inheritance fix (2026-08-15). This new incident is a DIFFERENT deadlock mode.
- `[[lessons_slot_timeout_fix]]` — Earlier zombie-slot release fixes (capture-nullify-release pattern). The lifecycle_manager.py:502-503 leak path is related but was not fully addressed by that fix.
- `[[lessons_caller_context_endpoint_resolution]]` — Documents the original caller-context gap in endpoint resolution.
