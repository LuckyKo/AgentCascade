# Implementation Plan: Proper dismiss_agent Thread Termination

**Date:** 2026-08-10
**Author:** planner_dismiss_termination
**Status:** REVISION 3 — ready for implementation
**Related:** investigation_report_dismiss_agent_thread_termination.md, .agent_lessons/dismiss-agent-cooperative-termination.md

---

## Executive Summary

Current `dismiss_agent` is cooperative-only: it marks an instance terminated but cannot stop a running thread mid-operation. This plan makes dismissal significantly more responsive by:

1. **Fixing the latent termination-signal bug** (durable `is_terminated` flag)
2. **Adding stop-checks at critical gaps** (slot acquire, wait_for_message, rate-limit waits)
3. **Introducing a new exception type** for clean abort propagation through sync children
4. **Improving cleanup ordering** to prevent signal loss

Python cannot safely force-kill threads, so this plan stays within cooperative termination but closes the windows where "cooperative" means "waits up to 30+ seconds".

---

## Problem Statement (Recap)

From investigation report:
- `terminate_instance()` adds name to `terminated_instances` set but never sets `inst.is_terminated = True`
- `remove_instance()` then discards the set entry (`terminated_instances.discard()`)
- After dismissal, `_is_terminal_stop()` → `is_instance_terminated()` returns False because neither signal persists
- Stop-check gaps: 30s semaphore acquire, pre-first-token HTTP call, rate-limit waits, wait_for_message blocks, long sync tools
- Sync children run inline in parent thread — dismissing them doesn't unblock the parent

---

## Design Principles

1. **Backward compatible** — no breaking changes to existing APIs or tool contracts
2. **Thread-safe** — all shared state modified under existing locks
3. **No new deadlocks** — careful ordering with existing lock hierarchy
4. **Exception-based abort** — use a dedicated exception type so callers can distinguish "dismissed" from "error"
5. **Best-effort only** — we cannot guarantee instant termination (Python limitation), but we reduce latency from minutes to seconds

---

## Changes Overview

### Phase 1: Fix the Latent Bug (Highest Priority, Lowest Risk)

Make `inst.is_terminated = True` durable so it survives pool removal.

### Phase 2: Add Stop-Checks at Critical Gaps

Add termination checks during blocking operations that currently have none.

### Phase 3: Introduce Abort Exception and Propagate Through Sync Children

Allow a dismissed sync child to abort early and return control to its parent.

### Phase 4: Improve Cleanup Ordering

Ensure signals are set before resources are cleaned up.

---

## Detailed Changes by File

### 1. agent_cascade/exceptions.py — New Abort Exception

**Purpose:** Provide a dedicated exception type for "instance was dismissed, abort cleanly" that can be caught and handled differently from errors.

**Change:** Add new exception class after line 59:

```python
class InstanceDismissedError(Exception):
    """Raised when an agent instance is dismissed mid-execution.

    Used to propagate the dismissal signal through call stacks (especially sync
    children running inline in parent threads) so they can abort promptly rather
    than waiting for long operations to complete.

    This is NOT an error — it's a clean abort signal. Callers should catch this
    and return early without retrying or logging as a failure.
    """
    def __init__(self, instance_name: str):
        self.instance_name = instance_name
        super().__init__(f"Instance '{instance_name}' has been dismissed")
```

**Risk:** Low — new exception type, no behavior change until used.

---

### 2. agent_cascade/agent_instance.py — New terminate() Method

**Purpose:** Encapsulate self-contained termination logic into the AgentInstance class so it can be reused by agent_pool.terminate_instance(), lifecycle_manager reuse path, and any future callers. This keeps termination as a proper object method rather than scattered across pool code.

**Design:** The terminate() method handles only instance-internal state changes:
- Sets `is_terminated = True` (durable flag)
- Transitions state to TERMINATED (if not already terminal)
- Clears streaming responses and volatile state

It does NOT handle pool-level operations (cascading children, killing shells, removing from pool) — those remain in agent_pool.py calling into this method.

**Change:** Add new method after `get_state_name()` (around line 605), before the PoolSettings class:

```python
    def terminate(self) -> None:
        """Mark this instance as terminated and clean up volatile state.

        This is the canonical way to terminate an AgentInstance. It sets the
        durable is_terminated flag, transitions to TERMINATED state (if in an
        active state), and clears streaming responses so partial content is
        discarded.

        Pool-level operations (cascading children, killing shells, removing from
        pool) are handled by the caller (typically agent_pool.terminate_instance()).

        Safe to call multiple times (idempotent). Thread-safe: uses _state_lock
        and _compression_lock as appropriate.

        Side effects:
        - Sets is_terminated = True (durable, survives pool removal)
        - Transitions state to TERMINATED if currently in an active state
        - Clears _streaming_responses list
        - Clears _state_label and _last_endpoint_config

        Does NOT:
        - Cascade-terminate children (caller responsibility)
        - Kill background shells or cancel async tools (caller responsibility)
        - Remove instance from pool (caller responsibility)
        """
        # Set durable termination flag — this persists even after pool removal,
        # allowing the running thread to detect dismissal via its local reference.
        self.is_terminated = True

        # Transition to TERMINATED if in an active state (RUNNING/SLEEPING/COMPLETING).
        # If already terminal (TERMINATED) or idle, skip the transition — idempotent.
        from agent_cascade.agent_instance import ACTIVE_STATES

        with self._state_lock:
            if self.state in ACTIVE_STATES:
                self._transition(AgentState.TERMINATED)

        # Clear volatile streaming state so partial LLM output is discarded.
        try:
            with self._compression_lock:
                self._streaming_responses.clear()
        except Exception:
            pass  # Best-effort — don't fail termination on lock issues

        # Clear cached endpoint config to avoid stale references.
        with self._state_lock:
            self._state_label = None
            self._last_endpoint_config = None
```

**Note:** Need to import ACTIVE_STATES at the top of agent_instance.py or reference it inline. Currently ACTIVE_STATES is defined in agent_pool.py (line ~50): `ACTIVE_STATES = {AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING}`. We should either:
- Define it in agent_instance.py near the AgentState enum (preferred — it's about instance states), or
- Import from agent_pool.py

**Recommendation:** Move ACTIVE_STATES definition to agent_instance.py right after the AgentState enum, then import it into agent_pool.py. This is cleaner architecturally since ACTIVE_STATES describes instance state semantics.

Add after line 54 (after TERMINATED = auto()):

```python
# States considered "active" — agent is executing or waiting, not idle/terminated.
# Used for termination checks, dismissal guards, and activity tracking.
ACTIVE_STATES: set[AgentState] = {AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING}
```

Then in agent_pool.py, replace the local ACTIVE_STATES definition with:
```python
from .agent_instance import AgentState, ACTIVE_STATES
```

**Risk:** Low. The terminate() method is additive; existing code continues to work until we refactor callers to use it. Moving ACTIVE_STATES is a simple relocation with no behavior change.

---

### 3. agent_cascade/agent_pool.py — Use Instance.terminate() and Fix Signal Durability

**Purpose:** Refactor pool-level termination to call into the new AgentInstance.terminate() method, and fix the latent bug where the termination signal was lost after pool removal.

**Change A: terminate_instance() — Delegate instance state changes to instance.terminate()**

Location: Lines 966-1050. Replace the inline state transition and streaming cleanup with a call to `inst.terminate()`.

Before (lines 989-1047):
```python
self.terminated_instances.add(instance_name)
inst = self.instances.get(instance_name)

# FIX: Thread-safe state read - snapshot under lock before checking ACTIVE_STATES
is_active = False
if inst:
    with inst._state_lock:
        is_active = inst.state in ACTIVE_STATES

if is_active:
    # Bug5 Fix #1: Only set global _stopped_event when explicitly requested
    if set_global_stopped:
        self._stopped_event.set()  # Global signal for ALL agents
    
    # RECOMMENDED FIX: Mark activity before transitioning to TERMINATED for consistency
    self._mark_activity(instance_name)
    
    with inst._state_lock:
        inst._transition(AgentState.TERMINATED)

# ── Fix TODO #41 Root Cause 1: Cancel pending async tool tasks ────────
# ... (async cancel, shell kill, queue clear code) ...

# Clear _streaming_responses to discard half-completed LLM output.
if inst:
    try:
        with inst._compression_lock:
            inst._streaming_responses.clear()
    except Exception as e:
        logger.debug(f"Clearing streaming responses for {instance_name} failed (non-critical): {e}")

self._clear_state_label(inst)
```

After (refactored):
```python
# Add to terminated_instances set BEFORE calling terminate() so that
# is_instance_terminated() returns True during the entire termination process.
self.terminated_instances.add(instance_name)
inst = self.instances.get(instance_name)

if inst:
    # Delegate instance-internal termination state changes to the instance itself.
    # This sets is_terminated=True, transitions to TERMINATED if active, and clears
    # streaming responses — all in one encapsulated call.
    inst.terminate()
    
    # Bug5 Fix #1: Only set global _stopped_event when explicitly requested
    if set_global_stopped:
        self._stopped_event.set()  # Global signal for ALL agents
    
    # Mark activity before pool-level cleanup
    self._mark_activity(instance_name)

# ── Cancel pending async tool tasks ────────
if hasattr(self, '_async_registry'):
    try:
        cancelled = self._async_registry.clear_pending(instance_name)
        if cancelled:
            logger.debug(f"Cancelled {cancelled} pending async tool(s) for {instance_name}")
    except Exception as e:
        logger.debug(f"Cancelling async tools for {instance_name} failed (non-critical): {e}")

# Kill all background shell processes for this agent
if hasattr(self, '_async_shell_tracker'):
    try:
        killed = self._async_shell_tracker.kill_all(instance_name)
        if killed:
            logger.debug(f"Killed {killed} async shell process(es) for {instance_name}")
            import time as _time
            _time.sleep(0.3)
    except Exception as e:
        logger.debug(f"Killing async shells for {instance_name} failed (non-critical): {e}")

# Clear message queue to prevent stale messages from being processed
with self._queue_lock:
    if instance_name in self.message_queues:
        try:
            self.message_queues[instance_name].clear()
        except Exception as e:
            logger.debug(f"Clearing message queue for {instance_name} failed (non-critical): {e}")
```

Note: The `_clear_state_label()` call is no longer needed because `inst.terminate()` now clears it. Remove or update that method accordingly.

**Change B: remove_instance() — KEEP terminated_instances.discard(), no change needed**

Reviewer finding: Removing the discard() would cause memory leaks and name-reuse bugs (terminated_instances would grow unbounded as names are recycled). The durable signal is `inst.is_terminated = True`, not membership in this set.

Correct behavior:
1. terminate_instance() adds name to terminated_instances AND calls inst.terminate() (sets is_terminated=True)
2. Running thread with local reference sees is_terminated=True → aborts
3. When remove_instance() eventually runs, it discards from the set to clean up
4. The durable flag on the instance object persists regardless of pool state

No change needed here — the original discard() stays as-is. This was a misunderstanding in the initial plan.

**Change C: dismiss_instance() — Use instance.terminate() for non-active dismissal too**

Location: Lines 1064-1130. Currently only calls terminate_instance() when the instance is in ACTIVE_STATES. For non-active instances (IDLE/TERMINATED), it skips termination and goes straight to remove_instance().

Fix: Ensure `inst.terminate()` is called regardless of state, so the durable flag is always set:

After line 1093 (after the existing terminate_instance call for active instances):
```python
# Ensure termination flag is set even if instance wasn't in ACTIVE_STATES
# when dismissed (e.g., was IDLE/COMPLETING). Call terminate() directly since
# terminate_instance() only transitions state for active instances.
inst = self.instances.get(instance_name)
if inst and not inst.is_terminated:
    inst.terminate()
```

**Risk:** Low-Medium. The refactoring is mostly extract-method; behavior should be identical except that `inst.is_terminated` is now reliably set. Removing line 880 needs verification that no code relies on "not in terminated_instances after removal".

---

### 4. agent_cascade/api_router.py — Add Stop-Checks During Blocking Operations

**Purpose:** Reduce latency when an agent is dismissed while waiting for a slot or rate-limit window.

**Change A: Interruptible semaphore acquire in EndpointScheduler.acquire()**

Location: Around line 303 (`sched['sem'].acquire(timeout=ENDPOINT_SLOT_ACQUIRE_TIMEOUT)`)

Current behavior: Blocks up to 30 seconds with no check.

Fix: Replace the single long acquire with a loop of shorter acquires, checking termination between attempts:

```python
# Interruptible slot acquisition: check termination every CHECK_INTERVAL seconds
# instead of blocking for the full ENDPOINT_SLOT_ACQUIRE_TIMEOUT
import time as _time

CHECK_INTERVAL = 1.0  # Check termination every second while waiting
acquire_start = _time.monotonic()
deadline = acquire_start + ENDPOINT_SLOT_ACQUIRE_TIMEOUT

while True:
    remaining = deadline - _time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            f"Timed out after {ENDPOINT_SLOT_ACQUIRE_TIMEOUT}s waiting for endpoint slot on {api_base}. "
            f"Current active count: {sched['active_count']}, max allowed: {sched['max_active']}"
        )
    
    # Check if instance was terminated while waiting
    if pool and instance_name and pool.is_instance_terminated(instance_name):
        raise InstanceDismissedError(instance_name)
    
    wait_time = min(remaining, CHECK_INTERVAL)
    if sched['sem'].acquire(timeout=wait_time):
        break  # Successfully acquired
```

Note: The `pool` parameter is passed by the caller (agent_pool.py `_acquire_endpoint_slot`). See the "EndpointScheduler.acquire() — Call Site Mapping and Signature Analysis" section below for the signature change details.

**Change B: Termination check during rate-limit wait in call_with_fallback()**

Location: Around line 1401 (`time.sleep(wait_time)`)

Current behavior: Sleeps for the full rate-limit wait time with no check.

Fix: Replace `time.sleep(wait_time)` with an interruptible loop:

```python
if wait_time > 0:
    logger.debug(
        f"[APIRouter] Rate limit reached for '{endpoint_name}' @ {endpoint_base}. "
        f"Waiting {wait_time:.1f}s before next call ({rate_limit_rpm} rpm)"
    )
    # Interruptible sleep: check termination every 0.5s during rate-limit wait
    import time as _time
    rate_wait_start = _time.monotonic()
    while _time.monotonic() - rate_wait_start < wait_time:
        if self._pool and _inst_name and self._pool.is_instance_terminated(_inst_name):
            raise InstanceDismissedError(_inst_name)
        _time.sleep(min(0.5, wait_time - (_time.monotonic() - rate_wait_start)))
```

**Change C: Termination check during retry backoff**

Location: In the retry loop's backoff section (around line 1480-1520 depending on current code).

Similar pattern: replace `time.sleep(backoff)` with interruptible loop checking termination.

**Risk:** Low. All changes are within existing error-handling paths. InstanceDismissedError will propagate up and be caught by execution_engine.py (see Change 6 below).

---

### EndpointScheduler.acquire() — Call Site Mapping and Signature Analysis

**Purpose:** Document all callers of acquire() to ensure any signature changes are safe and backward compatible.

**Current signature (api_router.py line 229):**
```python
def acquire(self, api_base: str, concurrency_limit: int, instance_name: str = "unknown", agent_class: str = "unknown") -> Optional[Callable[[], None]]:
```

**Call sites found:**

1. **agent_pool.py line 2523** — Primary caller via `router.scheduler.acquire()`:
   ```python
   return router.scheduler.acquire(api_base, concurrency_limit, instance_name, agent_class)
   ```
   Called from `_acquire_endpoint_slot()` method. This is the only direct caller of acquire().

2. **tool_dispatcher.py line 102** — Reference in docstring only (not an actual call).

3. **api_router.py lines 285, 1328** — Internal semaphore operations on raw Semaphore objects, not calls to acquire() method.

**Decision: Add optional `pool` parameter**

To enable termination checks inside the interruptible acquire loop, we add an optional pool parameter. This is the cleanest approach since:
- Only one caller exists (agent_pool.py line 2523) — trivial to update
- Backward compatible via default value of None
- Avoids coupling EndpointScheduler to APIRouter's `_pool` reference pattern

**New signature:**
```python
def acquire(self, api_base: str, concurrency_limit: int, instance_name: str = "unknown", agent_class: str = "unknown", pool: Optional['AgentPool'] = None) -> Optional[Callable[[], None]]:
```

**Type hint note:** Uses forward reference string `'AgentPool'` to avoid circular import issues. api_router.py already imports from agent_pool.py (e.g., for PoolSettings), so a direct type annotation would create a cycle at module load time. The string annotation is evaluated lazily by type checkers only.

**Caller update (agent_pool.py line 2523):**
```python
return router.scheduler.acquire(api_base, concurrency_limit, instance_name, agent_class, pool=self)
```

---

### 6. agent_cascade/agent_pool.py — Interruptible wait_for_message()

**Purpose:** Allow a dismissed agent to wake from wait_for_message promptly.

Location: Lines 2434-2467

Current behavior: `self._message_condition.wait(timeout=min(remaining, 1.0))` blocks up to 1s at a time, which is acceptable but can be improved.

Fix: Add termination check inside the while loop, before each wait:

```python
def wait_for_message(self, instance_name: str, timeout: float = 30.0) -> Optional[str]:
    with self._message_condition:
        deadline = None if timeout is None else time.time() + timeout

        while True:
            # Check termination before each wait iteration
            if self.is_instance_terminated(instance_name):
                return None  # Dismissed — wake up and let caller handle it
            
            msgs = self.message_queues.get(instance_name)
            if msgs and len(msgs) > 0:
                return msgs.pop(0)

            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    if instance_name in self.message_queues and not self.message_queues[instance_name]:
                        del self.message_queues[instance_name]
                    return None
                self._message_condition.wait(timeout=min(remaining, 1.0))
            else:
                # For indefinite waits, use a shorter timeout to allow periodic termination checks
                self._message_condition.wait(timeout=2.0)
```

**Risk:** Low. Returning None on dismissal is consistent with timeout behavior; the caller (__wait tool or SLEEPING parent wake loop) already handles None.

### wait_for_message() — Caller Audit

**Purpose:** Verify all callers handle None correctly when called on a dismissed instance.

**Audit results:**

- **Definition:** agent_pool.py line 2434
- **Direct callers found:** NONE. The method is defined but never called anywhere in the codebase (grep for `.wait_for_message(` returns zero matches).
- **Indirect references:** The `__wait` command in shell_cmd.py (line 370) does NOT call pool.wait_for_message(). Instead, it polls task state directly via `task._lock`, `task.stdout_lines`, etc.

**Conclusion:** wait_for_message() is currently unused code. Adding a termination check to it is defensive programming for future use, but there are zero callers that need verification today. The None return behavior (matching timeout) is safe by design.

---

### 7. agent_cascade/execution_engine.py — Handle InstanceDismissedError

**Purpose:** Catch the abort exception and exit cleanly without retrying or logging as an error.

**Change A: In the main run() loop's LLM call section**

Location: Around line 1380-1450 where `api_router.call_with_fallback()` is called and exceptions are handled.

Add to the existing exception handling (which already checks for "has been terminated" RuntimeError):

```python
except InstanceDismissedError as e:
    logger.debug(f"[DISMISSAL] Instance '{e.instance_name}' dismissed during LLM call, aborting cleanly")
    break  # Exit run() loop cleanly — no retry, no error message
except RuntimeError as e:
    _is_termination_abort = "has been terminated" in str(e.args[0]) if e.args else False
    if _is_termination_abort:
        logger.debug(f"[TERMINATION] Instance terminated during LLM call, aborting cleanly")
        break
    # ... existing handling ...
```

**Change B: In the retry loop for loop detection / auto-rollback**

Location: Around line 5140-5220 where `run_agent_in_pool_with_recovery` handles retries.

Add InstanceDismissedError to the list of exceptions that should abort without retry:

```python
except (InstanceDismissedError, RuntimeError) as e:
    if isinstance(e, InstanceDismissedError):
        logger.debug(f"[DISMISSAL] Aborting recovery loop for {instance_name}: dismissed")
        raise  # Propagate up — don't retry a dismissed instance
    _is_termination_abort = "has been terminated" in str(e.args[0]) if e.args else False
    if _is_termination_abort:
        logger.debug(f"[TERMINATION] Aborting recovery loop for {instance_name}: terminated")
        raise
    # ... existing handling ...
```

**Change C: In the tool execution loop**

Location: Around line 3958-4020 where tools are dispatched.

Add check before long-running tool execution:

```python
# Stop/halt check BEFORE tool execution
if self._is_terminal_stop(inst_name):
    break
elif self._is_suspended_by_compression(inst_name):
    # ... existing handling ...
    
# Additional check: raise InstanceDismissedError if this instance was dismissed
# This allows sync children to propagate the signal up to their parent
inst = self.pool.get_instance(inst_name)
if inst and inst.is_terminated:
    from agent_cascade.exceptions import InstanceDismissedError
    raise InstanceDismissedError(inst_name)
```

**Risk:** Low-Medium. The existing RuntimeError handling for "has been terminated" is already in place; this just adds a cleaner exception type alongside it.

### InstanceDismissedError — Complete Propagation Path Analysis

**Purpose:** Ensure all code paths that can block or take significant time are covered by either raising or catching InstanceDismissedError appropriately.

#### Where It's Raised (Abort Points)

1. **api_router.py — EndpointScheduler.acquire() interruptible loop:** When instance is terminated while waiting for slot.
2. **api_router.py — call_with_fallback() rate-limit wait loop:** When instance is terminated during rate-limit backoff.
3. **api_router.py — call_with_fallback() retry backoff loop:** When instance is terminated between retries.
4. **execution_engine.py — tool execution pre-check:** Before each long-running tool, checks `inst.is_terminated` and raises if true.
5. **agent_pool.py — wait_for_message():** Returns None (not raising) since it's a polling-style API; caller handles None as "dismissed/timeout".

#### Where It's Caught (Abort Handlers)

1. **execution_engine.py — run() main loop:** Catches during LLM call → breaks loop cleanly, no retry.
2. **execution_engine.py — recovery loop:** Catches during retry/recovery → aborts recovery attempt.
3. **tool_dispatcher.py — _run_child_sync():** Catches during sync child execution → returns "Dismissed" message to parent.

#### Coverage of Blocking Operations

| Operation | Interruptible? | Mechanism | Max Latency |
|-----------|---------------|-----------|-------------|
| Semaphore acquire (slot wait) | Yes | Interruptible loop, 1s check interval | ~1s + partial wait |
| Rate-limit backoff sleep | Yes | Interruptible loop, 0.5s check | ~0.5s + partial wait |
| Retry backoff sleep | Yes | Interruptible loop, 0.5s check | ~0.5s + partial wait |
| LLM streaming (SSE) | Partially | Existing 20-tick check; InstanceDismissedError after first token if rate-limited | Up to one full response (~30-60s worst case for very long responses) |
| HTTP request in-flight | No | Python limitation — cannot safely interrupt blocking I/O | Request timeout (configurable, typically 30-120s) |
| Sync tool execution | Partially | Pre-check before tool; during-tool depends on tool implementation | Until tool completes or next stop-check |
| wait_for_message() | Yes | Returns None on termination check | ~1s (existing tick interval) |

#### Async Tool Path Analysis

When an async tool is dispatched:
1. **Before submission to ThreadPoolExecutor:** The termination check in async_tools._execute() catches it immediately → returns early with "Dismissed" message. ✅ Covered.
2. **After submission, task queued but not started:** future.cancel() is attempted in terminate_instance(). If successful, task never runs. ✅ Covered (existing behavior).
3. **Task already running in worker thread:** Cannot be interrupted at Python level. The result will be discarded when the parent checks termination after waiting. This is existing behavior and acceptable — the async tool's work completes but has no effect. ⚠️ Known limitation.

#### HTTP-Level Behavior

Current HTTP client (requests library) uses blocking I/O:
- **Cannot be cancelled** mid-request without complex socket-level manipulation that risks corrupting connection state.
- **Mitigation:** The existing timeout settings (typically 30-120s per request) bound the worst case. After the response completes, the next stop-check or tick will detect termination and abort further processing.
- **Future improvement:** Switch to httpx with cancellation support for true HTTP-level interruptibility.

#### Edge Case: Dismiss During Tool Result Processing

If an instance is dismissed while execution_engine is processing tool results (between tool return and next LLM call):
- The existing `_is_terminal_stop()` check at the top of each loop iteration catches this.
- Our additional `inst.is_terminated` pre-tool-check adds redundancy for cases where the flag was set during a previous operation.

#### Summary of Gaps

The following remain non-interruptible (Python/platform limitations):
1. In-flight HTTP requests — bounded by timeout settings
2. Truly blocking sync tools (e.g., shell_cmd waiting on pipe read) — bounded by tool's own timeout or completion
3. Running async tool worker threads — results discarded but thread completes

These are acceptable limitations given the design principle of "cooperative termination preferred, no unsafe forced kills."

---

### 8. agent_cascade/tool_dispatcher.py — Abort Sync Children on Dismissal

**Purpose:** When a sync child is dismissed, abort its execution and return control to the parent promptly.

Location: `_run_child_sync()` around lines 518-609

Current behavior: Runs `run_child_core()` which calls `engine.run()`. If the child is dismissed mid-execution, the parent thread is blocked until the LLM call completes.

Fix: Wrap the run_child_core call to catch InstanceDismissedError and return early:

```python
try:
    from agent_cascade.child_runner import run_child_core
    from agent_cascade.exceptions import InstanceDismissedError
    
    result = run_child_core(
        engine=self.engine,
        pool=self.pool,
        agent_class=agent_class,
        instance_name=instance_name,
        args=args,
        caller_name=caller_name,
        child_depth=child_depth,
        prefix="Agent",
    )
    
    # ... existing state save/restore code ...
    
except InstanceDismissedError as e:
    logger.debug(f"[DISMISSAL] Sync child '{e.instance_name}' aborted due to dismissal")
    return f"[Agent '{instance_name}' Dismissed]: Agent was dismissed before completing."
except Exception as e:
    # ... existing error handling ...
```

Also add a termination check at the start of `_run_child_sync()` and periodically during execution if possible. Since sync children run inline, we can't easily add mid-execution checks without modifying engine.run() (which Change 5C above does).

**Risk:** Low. The exception is caught locally; the parent gets a clean "Dismissed" message instead of waiting for completion.

---

### 9. agent_cascade/child_runner.py — Check Termination After Engine Run

**Purpose:** Ensure that if an instance was dismissed during execution, we report it correctly.

Location: Lines 104-115

Current behavior: Checks `terminated_instances` set after engine.run() returns. If the instance was removed from the pool, this returns False.

Fix: Also check `inst.is_terminated` (set by terminate()):

```python
def _check_status(pool, instance_name: str) -> tuple[bool, bool]:
    stop_flag = pool.stopped
    was_manual_halt = (instance_name in pool._halted_instances and
                       instance_name not in pool._compression_halted)
    
    # Check both the set (authoritative while in pool) and the instance flag (durable after removal)
    was_terminated = instance_name in pool.terminated_instances
    
    # Also check the instance object directly if still accessible
    inst = pool.get_instance(instance_name)
    if inst:
        was_terminated = was_terminated or inst.is_terminated
    
    return (stop_flag or was_manual_halt), was_terminated
```

**Risk:** Low. Only changes reporting behavior, not execution flow.

---

### 10. agent_cascade/async_tools.py — Check Termination Before Tool Execution

**Purpose:** Allow async tools (including async children) to abort if their parent is dismissed while the tool is queued but not yet started.

Location: `_execute()` around line 129

Current behavior: Runs `entry.tool_call()` immediately with no check.

Fix: Add a termination check at the start of _execute():

```python
def _execute(self, entry: BackgroundToolEntry):
    try:
        # Check if the owning instance was terminated before starting execution
        if self.pool and self.pool.is_instance_terminated(entry.agent_instance_name):
            logger.debug(
                f"[AsyncToolRegistry] Skipping tool for '{entry.agent_instance_name}': "
                f"instance was dismissed before execution started"
            )
            entry.result = f"[Skipped]: Agent '{entry.agent_instance_name}' was dismissed."
        else:
            entry.result = entry.tool_call()
    except Exception as e:
        entry.error = str(e)
    finally:
        # ... existing completion handling ...
```

**Risk:** Low. Only affects tools that haven't started yet; already-running threads complete normally (documented behavior).

---

## Lock Ordering Analysis

**Purpose:** Document the actual lock hierarchy in the codebase to ensure our changes don't introduce deadlocks. This analysis is based on direct inspection of agent_pool.py and agent_instance.py.

### Actual Locks That Exist

#### AgentPool-level locks:

| Lock | Type | Protects | Line |
|------|------|----------|------|
| `_settings_save_lock` | Lock | PoolSettings JSON save operations | 277 |
| `_children_lock` | RLock | `pool.children` dict + instance._child_instances | 297 |
| `_queue_lock` | Lock | `message_queues` dict | 313 |
| `_ui_disabled_tools_lock` | RLock | `_ui_disabled_tools` dict | 341 |

#### AgentInstance-level locks:

| Lock | Type | Protects | Location |
|------|------|----------|----------|
| `_state_lock` | RLock | State transitions (`inst.state`) | agent_instance.py:230 |
| `_compression_lock` | RLock | Conversation cache, streaming responses | agent_instance.py:244 |

#### Other locks:

| Lock | Type | Protects | Location |
|------|------|----------|----------|
| `LoggerManager._lock` | Lock | `_loggers` dict | agent_pool.py:2877 |
| `EndpointScheduler._lock` | Lock | `_schedules`, `_slot_holders` | api_router.py:219 |

### Critical Finding: No Lock for instances/terminated_instances

**There is NO lock protecting `self.instances` or `self.terminated_instances`.** These are accessed directly without synchronization throughout the codebase:

- **terminate_instance() (lines 983-1049):** Reads/writes `instances`, `terminated_instances`, and `children` — uses `_children_lock` only for reading children list, but NOT for instances/terminated_instances operations.
- **remove_instance() (lines 871-925):** Pops from `instances`, discards from `terminated_instances` — no lock used.
- **dismiss_instance() (lines 1064-1130):** Reads from `instances`, calls terminate_instance/remove_instance — no lock used.
- **is_instance_terminated() (lines 2661-2664):** Reads both `terminated_instances` and `instances` — no lock used.

This is existing behavior. The codebase relies on:
1. Most operations happening in the agent's own thread or from a single management context
2. Python's GIL providing atomicity for simple dict/set operations (get, add, discard)
3. TOCTOU races being acceptable for these particular data structures

### Our Changes Must Follow This Pattern

Since existing code accesses `instances` and `terminated_instances` without locks, our changes must do the same for consistency. Adding a new lock around these structures would:
- Require auditing every access point (50+ occurrences)
- Risk introducing deadlocks if not perfectly ordered with existing locks
- Be out of scope for this termination-focused plan

**Decision:** Our terminate() method and related changes will NOT introduce new locks for pool-level structures. We follow the existing lock-free pattern for `instances` and `terminated_instances`.

### Lock Hierarchy (for locks that DO exist)

Based on observed usage patterns, the hierarchy is:

```
Pool-level locks → Instance-level locks
(children_lock) → (state_lock, compression_lock)
(queue_lock, settings_save_lock, ui_disabled_tools_lock are independent)
```

Key observations:
- `_children_lock` is acquired before accessing instance._child_instances
- `terminate_instance()` acquires `_children_lock` briefly to snapshot children list, then releases before recursing
- Instance locks (`_state_lock`, `_compression_lock`) are NEVER held while acquiring pool-level locks

### Verification of Our Changes Against Lock Rules

1. **AgentInstance.terminate():** Only acquires instance-level locks (`_state_lock`, `_compression_lock`). No pool locks held. ✅ Safe.

2. **terminate_instance() calling inst.terminate():** Current code does NOT hold any lock when it reaches the state transition section (line 992+). Our refactored version calls `inst.terminate()` at the same point — no pool lock is held. ✅ Safe.

3. **Interruptible semaphore acquire:** Checks `is_instance_terminated()` which accesses `terminated_instances` without a lock — same as existing pattern. ✅ Consistent.

4. **wait_for_message() termination check:** Same as above — calls `is_instance_terminated()` without acquiring pool lock. ✅ Consistent.

### Thread-Safety Limitations (Honest Assessment)

The existing code has known TOCTOU races:
- Between checking `inst = self.instances.get(name)` and acting on it, another thread could call `remove_instance(name)`
- Between adding to `terminated_instances` and the running thread checking it, `remove_instance()` could discard the entry (the latent bug we're fixing)

Our changes do NOT make these worse:
- Setting `inst.is_terminated = True` is atomic (simple attribute assignment under GIL)
- The flag lives on the instance object, which persists after pool removal
- We don't introduce new locking that could deadlock with existing patterns

### Conclusion

The codebase uses a minimalist locking strategy: locks only protect specific structures that need them (state transitions, message queues, children tracking), while `instances` and `terminated_instances` are accessed lock-free relying on GIL atomicity. Our changes follow this same pattern faithfully. No new deadlocks introduced.

---

## Implementation Order

### Phase Dependencies

```
Phase 0 (Foundation) → Phase 1 (Latent Bug Fix) → Phase 2 (Stop-Checks) → Phase 3 (Abort Exception) → Phase 4 (Async + Reporting)
      │                      │                          │                       │                        │
      └──────────────────────┴──────────────────────────┴───────────────────────┴────────────────────────┘
                               All later phases depend on terminate() and InstanceDismissedError from Phases 0-1
```

### Detailed Order

1. **Phase 0 — Foundation (exceptions.py, agent_instance.py)**
   - Add InstanceDismissedError exception class
   - Move ACTIVE_STATES from agent_pool.py to agent_instance.py (after AgentState enum)
   - Add terminate() method to AgentInstance
   
2. **Phase 1 — Fix Latent Bug + Refactor (agent_pool.py)**
   - Update agent_pool.py to import ACTIVE_STATES from agent_instance.py
   - Refactor terminate_instance() to call inst.terminate() instead of inline state changes
   - Ensure dismiss_instance() calls terminate() for non-active instances too
   - NO CHANGE to remove_instance(): keep terminated_instances.discard() as-is

3. **Phase 2 — Add Stop-Checks (api_router.py, agent_pool.py)** [requires Phase 0 InstanceDismissedError]
   - Interruptible semaphore acquire in EndpointScheduler.acquire()
   - Interruptible rate-limit wait in call_with_fallback()
   - Termination check in wait_for_message()

4. **Phase 3 — Handle Abort Exception (execution_engine.py, tool_dispatcher.py)** [requires Phase 0 InstanceDismissedError + Phase 2 stop-checks]
   - Catch InstanceDismissedError in run() loop
   - Catch InstanceDismissedError in recovery loop
   - Add termination check before long-running tool execution
   - Handle InstanceDismissedError in _run_child_sync()

5. **Phase 4 — Async Tool Check + Status Reporting (async_tools.py, child_runner.py)** [requires Phase 0 terminate()]
   - Check termination at start of async_tools._execute()
   - Update child_runner._check_status() to also check inst.is_terminated

**Dependency notes:**
- Phases 2-4 all require InstanceDismissedError from Phase 0 (they raise or catch it).
- Phase 3 requires Phase 2's stop-checks because the abort exception is raised there; without Phase 2, Phase 3's handlers would never fire.
- Phase 4 requires terminate() from Phase 0 to check inst.is_terminated reliably.
- Each phase after Phase 1 is independently testable and can be rolled back if issues arise. Phases 0-1 together fix the core latent bug and are mandatory for all subsequent phases.

---

## Edge Cases

### Agent mid-API call (HTTP request in flight)
- **Cannot be interrupted** without HTTP layer changes (cancelling the underlying connection). This is acceptable — the response will complete, but subsequent processing will check termination and abort.
- **Mitigation:** The existing 20-tick stream check catches dismissal during streaming responses.

### Agent in long sync tool execution (shell_cmd waiting on pipe, code_interpreter running)
- **Cannot be interrupted** at the Python level without killing the subprocess.
- **Mitigation:** Background shells are already killed via `async_shell_tracker.kill_all()` in terminate_instance(). For synchronous shell_cmd, this is a known limitation — future work could move all long-running tools to async mode.

### Dismissing an already-dismissed agent
- Current behavior: Second dismissal returns `[status=not_found]` because instance was removed from pool. This is correct and unchanged.
- With our changes: `inst.is_terminated = True` persists, so if somehow called again on the same object reference, it's idempotent (setting True when already True).

### Sync child dismissed while parent holds slot
- Parent releases slot before running sync child (line 549-553), child acquires its own. If child is dismissed mid-execution:
  - InstanceDismissedError propagates up through run_child_core() → _run_child_sync()
  - finally block re-acquires parent's slot (lines 595-609)
  - Parent resumes with a "Dismissed" result
- **Risk:** If the child holds the slot when dismissed, we need to ensure it's released. The existing `_release_slot()` in execution_engine.py's finally block handles this.

### Race: dismiss called between termination check and blocking operation
- Example: Thread checks `is_terminal_stop()` → False, then calls `semaphore.acquire()`, then dismiss happens.
- **Mitigation:** Our interruptible acquire loop checks every 1s, so worst-case latency is 1s + current blocking duration. This is significantly better than the previous 30s timeout.

### Instance removed from pool while thread still running
- Thread holds local reference to `inst` object → `inst.is_terminated = True` is visible.
- Pool-mediated checks return False for removed instances (by design — gone means not running).
- **Acceptable:** The thread will complete its current operation and exit naturally on the next stop-check or when it tries to use pool resources that are now gone.

---

## Risk Assessment

| Change | Risk Level | Rationale | Rollback Plan |
|--------|-----------|-----------|---------------|
| Add InstanceDismissedError | Low | New exception type, no behavior change until used | Delete the class |
| Move ACTIVE_STATES to agent_instance.py | Low | Simple relocation; same value, different file | Move back to agent_pool.py |
| Add terminate() method to AgentInstance | Low | New method, additive; existing code unaffected until callers updated | Remove the method |
| Refactor terminate_instance() to use instance.terminate() | Low-Medium | Extract-method refactoring; should be behavior-preserving but touches hot path. Must verify lock ordering preserved. | Restore inline state changes |
| Interruptible semaphore acquire + pool parameter | Low-Medium | Changes timing behavior of slot acquisition; adds optional parameter (backward compatible). Could expose race conditions if termination check is too aggressive. | Restore single acquire(timeout=30), remove pool param |
| Interruptible rate-limit wait | Low | Only affects waiting threads; termination check is additive | Restore time.sleep() |
| Termination check in wait_for_message | Low | Returns None on dismissal, consistent with timeout. Method currently unused so zero runtime risk. | Remove the check |
| Catch InstanceDismissedError in execution_engine | Low | Additive exception handling; doesn't change existing paths | Remove the except block |
| Handle InstanceDismissedError in _run_child_sync | Low | Caught locally; parent gets clean message | Remove the except block |
| Check termination in async_tools._execute | Low | Only affects not-yet-started tools | Remove the check |

**Reviewer findings addressed:**
- **removed_instances.discard():** No longer being modified — stays as-is. Removed from risk table (no change = no risk).
- **Lock ordering (REVISION 2):** Corrected analysis — there is NO _instance_lock in the codebase. instances/terminated_instances are accessed lock-free (existing pattern). Our changes follow this same pattern. No new locking introduced.
- **Phase dependencies:** Made explicit; Phases 2-4 require Phase 0 artifacts.

**Overall Risk:** Low-Medium. Most changes are additive (new checks, new exception handling). The riskiest change is modifying semaphore acquire timing, but it's bounded and testable.

---

## Testing Recommendations

### Unit Tests
1. **Test AgentInstance.terminate() idempotency:** Call terminate() multiple times on same instance → verify no errors, is_terminated remains True
2. **Test inst.is_terminated durability:** Create instance → call terminate() → simulate pool removal → verify `inst.is_terminated == True` via local reference
3. **Test InstanceDismissedError propagation:** Mock a dismissed instance during LLM call → verify clean abort without retry
4. **Test interruptible acquire:** Mock semaphore that never releases + dismiss during wait → verify early exit with InstanceDismissedError
5. **Test sync child dismissal:** Parent calls sync child → dismiss child mid-execution → verify parent resumes promptly with "Dismissed" message

### Integration Tests
1. **Async child dismissed mid-LLM call:** Launch async child with slow endpoint → dismiss immediately → verify thread exits within ~20 ticks (streaming check) or next stop-check
2. **Agent waiting for slot when dismissed:** Hold slot with one agent, start second agent needing same slot → dismiss second while waiting → verify early exit
3. **Nested sync children:** A→B(sync)→C(sync), dismiss C mid-execution → verify A resumes promptly

### Manual Tests
1. Use UI to terminate an active sub-agent tab → observe tab disappears and activity stops within seconds (not minutes)
2. Call `dismiss_agent` on a running child from parent → verify clean "Dismissed" result returned
3. Verify no deadlocks or hangs after repeated dismiss/launch cycles

### Regression Tests
1. Verify existing behavior: dismissing idle agents still works correctly
2. Verify root agent cannot be dismissed by children (ownership check unchanged)
3. Verify all_idle dismissal skips active agents as before
4. Verify SLEEPING parent wakeup when async child dismissed (existing fix should still work)

---

## Future Improvements (Out of Scope)

These are noted for future consideration but are NOT part of this plan:

1. **HTTP-level cancellation:** Add support for cancelling in-flight HTTP requests via `requests.Session` or `httpx.AsyncClient` cancellation tokens. Would allow true interruption of mid-API-call agents.

2. **Process-level isolation for long tools:** Run long-running synchronous tools (shell_cmd, code_interpreter) in separate processes that can be killed. This is the only safe way to force-kill a truly blocking operation.

3. **Timeout enforcement for async tools:** Currently `BackgroundToolEntry.timeout` is not enforced. Adding timeout-based cancellation would complement dismissal.

4. **UX improvement:** Show "[dismissed — thread finishing]" status instead of immediately claiming full dismissal, to set accurate expectations.

---

## Summary of Files to Modify

| File | Changes |
|------|---------|
| `agent_cascade/exceptions.py` | Add InstanceDismissedError class |
| `agent_cascade/agent_instance.py` | Move ACTIVE_STATES here; add terminate() method |
| `agent_cascade/agent_pool.py` | Import ACTIVE_STATES from agent_instance; refactor terminate_instance() to use inst.terminate(); add pool param when calling acquire(); add check in wait_for_message() |
| `agent_cascade/api_router.py` | Add optional pool param to acquire(); interruptible semaphore acquire loop; interruptible rate-limit/backoff waits |
| `agent_cascade/execution_engine.py` | Catch InstanceDismissedError; add termination check before tool execution |
| `agent_cascade/tool_dispatcher.py` | Handle InstanceDismissedError in _run_child_sync() |
| `agent_cascade/child_runner.py` | Check inst.is_terminated in _check_status() |
| `agent_cascade/async_tools.py` | Check termination at start of _execute() |

**Estimated effort:** ~100-120 lines of code changes across 8 files. Most changes are small, focused additions to existing patterns. The terminate() method on AgentInstance is the largest single addition (~40 lines including docstring).

---

## Approval Checklist

Before implementation begins:
- [ ] Plan reviewed by Maine (supervisor)
- [ ] Independent review by reviewer agent for edge cases and consistency
- [ ] No conflicts with pending changes in other plans
- [ ] Testing strategy agreed upon
