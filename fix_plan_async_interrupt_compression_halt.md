# Fix Plan: Async-Interrupt-Compression-Halt Bug (Revised)

**Author:** fix_planner_async_interrupt  
**Date:** 2026-08-06  
**Revision:** Updated to cover all 17 `_is_stopped()` call sites after reviewer findings  
**Status:** Ready for implementation  

---

## Executive Summary

Forced compression calls `pool.halt_all_instances()` before compressing, which marks every concurrent agent as halted. The engine's `_is_stopped()` conflates "halted-by-compression" with terminal conditions (user stop, terminate, generation change), causing agents to break out of their run loops mid-turn permanently. After `resume_all_instances()` clears the halt flag, no auto-resume occurs — the agent is dead.

This plan addresses all 4 TODO items using a hybrid approach: treat compression-halt as suspendable (not terminal) in `_is_stopped()`, with cooperative wait loops at key break points. This is Option A from the investigation report, refined.

---

## Chosen Approach: Hybrid Option A — Suspendable Compression-Halt

**Rationale:**
- Option A (suspendable halt) is minimal and safest — changes only how `_is_stopped()` interprets flags and adds cooperative waits at existing break points.
- Option B (compression lock) would require restructuring the streaming loop to block instead of yield, which is more invasive.
- The pool already tracks `_compression_halted` separately from `_halted_instances` — we just need to respect this distinction in the engine.

**Key design decision:** Split terminal vs. suspendable conditions in the engine:
- **Terminal (break loop):** `pool.stopped`, generation mismatch, terminate
- **Suspendable (wait then resume):** compression-halt only

---

## Complete Call Site Inventory

There are **17 call sites** of `_is_stopped()` in execution_engine.py. They fall into three categories:

| # | Line | Location | Action on True | Category | Fix Needed? |
|---|------|----------|----------------|----------|-------------|
| 1 | 1126 | `run()` pre-slot-acquire | `return` (exit generator) | Startup guard | Yes — terminal only |
| 2 | 1133 | `run()` post-slot-acquire | `return` (exit generator + release slot) | Startup guard | Yes — terminal only |
| 3 | 1381 | Main loop post-LLM | `yield; break` | Main loop | Yes — wait on compression-halt |
| 4 | 1809 | `_check_stop_conditions()` | returns True → skips LLM call | Pre-call gate | Yes — terminal only |
| 5 | 1855 | `_check_stream_termination()` | returns early tuple → break at caller | Mid-stream poll | Yes — terminal only (see note) |
| 6 | 2792 | Streaming loop check #1 | cleanup + `yield None; break` | Mid-stream defense | Yes — terminal only (see note) |
| 7 | 2820 | Streaming loop check #2 | cleanup + `yield None; break` | Mid-stream defense | Yes — terminal only (see note) |
| 8 | 3319 | `_handle_truncation_or_incomplete()` | conditional gate for auto-continue | Logic gate | Yes — see auto-continue section |
| 9 | 3405 | Inside `while is_paused()` loop | `break` from while (continues outer) | Pause-wait escape | Yes — terminal only |
| 10 | 3411 | Pre-tool-execution check | `break` from tool dispatch loop | Tool loop | Yes — wait on compression-halt |
| 11 | 3638 | Orphan tool handling | enters if-branch (fills placeholders) | Logic gate | No change needed |
| 12 | 4039 | `_process_response()` entry guard | `return False` → breaks main loop | Post-turn gate | Yes — wait on compression-halt |
| 13 | 4184 | Sleeping handler entry | `return BREAK_LOOP, None` | Sleep entry | Yes — terminal only |
| 14 | 4210 | Post-slot-acquire after wakeup | `return BREAK_LOOP, None` | Sleep recovery | Yes — terminal only |
| 15 | 4276 | Post-slot-acquire after stable drain | `return BREAK_LOOP, None` | Sleep recovery | Yes — terminal only |
| 16 | 4575 | `_create_and_run_agent` consumer loop | `break` from for-loop | Outer consumer | No change (inner loop handles it) |
| 17 | 4635 | Lambda to `run_auto_skill_proposal` | passed as callback | External caller | Yes — terminal only |

---

## Change 1: Core Method Split in `_is_stopped()`

**File:** `agent_cascade/execution_engine.py`  
**Lines affected:** 1800–1831 (methods `_check_stop_conditions`, `_is_stopped`)

### Current Behavior

```python
def _check_stop_conditions(self, instance: AgentInstance) -> bool:
    """Check if we should skip the LLM call due to stop conditions."""
    inst_name = instance.instance_name
    return self._is_stopped(inst_name)

def _is_stopped(self, inst_name: str) -> bool:
    """Check if pool is stopped, run superseded, or instance terminated.
    
    Centralized stop condition check used throughout execution_engine.py to avoid
    duplicating logic. Covers: global stop flag, generation mismatch (old run
    superseded by newer one), per-instance halt, and termination.
    
    Pause should not interrupt execution; it should just wait and resume.

    Args:
        inst_name: Instance name to check halt/termination status

    Returns:
        True if any stop condition met, False otherwise.
    """
    return (self.pool.stopped or
            self._my_generation != self.pool._run_generation or
            inst_name in self.pool._halted_instances or
            self.pool.is_instance_terminated(inst_name))
```

### Proposed Behavior

Add two new helper methods and update `_is_stopped()` documentation:

```python
def _is_terminal_stop(self, inst_name: str) -> bool:
    """Check if this instance has a TERMINAL stop condition (cannot resume).
    
    Returns True only for conditions that mean execution must end permanently:
    - Global pool stop (user clicked Stop)
    - Generation mismatch (superseded by newer run)
    - Instance terminated
    
    Does NOT return True for compression-halt or manual halt — those are suspendable.
    """
    return (self.pool.stopped or
            self._my_generation != self.pool._run_generation or
            self.pool.is_instance_terminated(inst_name))

def _is_suspended_by_compression(self, inst_name: str) -> bool:
    """Check if this instance is suspended because of forced compression.
    
    Returns True only when the instance was halted by forced compression's
    halt_all_instances(). These agents should wait cooperatively and resume
    automatically when resume_all_instances() clears the flag.
    """
    return inst_name in self.pool._compression_halted

def _check_stop_conditions(self, instance: AgentInstance) -> bool:
    """Check if we should skip the LLM call due to stop conditions.
    
    Only returns True for terminal stops — compression-halt agents should
    wait and retry rather than skipping the LLM call permanently.
    """
    inst_name = instance.instance_name
    return self._is_terminal_stop(inst_name)

def _is_stopped(self, inst_name: str) -> bool:
    """Check if execution should stop for this instance.
    
    Legacy name kept for backward compatibility at call sites that only need
    a boolean 'should I stop now'. Returns True for terminal stops OR any halt.
    
    IMPORTANT: Callers that break/return on this check must distinguish between
    terminal stops (break permanently) and compression-halt (wait then resume).
    Use _is_terminal_stop() and _is_suspended_by_compression() directly at those sites.
    
    Pause should not interrupt execution; it should just wait and resume.

    Args:
        inst_name: Instance name to check halt/termination status

    Returns:
        True if any stop condition met, False otherwise.
    """
    return (self._is_terminal_stop(inst_name) or
            inst_name in self.pool._halted_instances or
            self.pool.is_paused())
```

---

## Change 2: Per-Call-Site Fixes

Each call site is handled based on its category. Sites marked "No change" are explained.

### Site 1: Line 1126 — Pre-slot-acquire guard

**Current:**
```python
if self._is_stopped(instance.instance_name):
    return  # Exit early if stopped
```

**New:**
```python
if self._is_terminal_stop(instance.instance_name):
    return  # Terminal stop — don't start work
# Compression-halt at startup: just proceed (compression is transient)
instance._slot_release = None
self._acquire_slot_with_logging(instance, "initial")
```

**Rationale:** If compression halts an agent right as it's starting, that's transient. Let it acquire its slot and run. Only terminal stops should prevent startup.

### Site 2: Line 1133 — Post-slot-acquire guard

**Current:**
```python
if self._is_stopped(instance.instance_name):
    self._release_slot(instance, instance.instance_name)
    return  # Exit generator immediately instead of continuing with stale state
```

**New:**
```python
if self._is_terminal_stop(instance.instance_name):
    self._release_slot(instance, instance.instance_name)
    return  # Terminal stop — release slot and exit
# Compression-halt after slot acquire: keep the slot, proceed to run loop
```

**Rationale:** Same as Site 1. The agent has its slot; compression-halt is transient.

### Site 3: Line 1381 — Main loop post-LLM check (PRIMARY FIX)

**Current:**
```python
if not terminated_during_stream and self._is_stopped(instance.instance_name):
    logger.debug("halted/stopped/superseded - %s", instance.instance_name)
    yield response
    break  # ── Fix TODO #41: Break immediately instead of continuing loop ──
```

**New:**
```python
if not terminated_during_stream:
    if self._is_terminal_stop(instance.instance_name):
        logger.debug("terminal stop - %s", instance.instance_name)
        yield response
        break
    elif self._is_suspended_by_compression(instance.instance_name):
        # Compression is running — wait cooperatively, then continue the loop
        logger.debug("suspended by compression, waiting - %s", instance.instance_name)
        while self._is_suspended_by_compression(instance.instance_name):
            if self._is_terminal_stop(instance.instance_name):
                yield response
                break
            self.pool.wait_if_paused(timeout=1.0)
        else:
            # Resumed (compression done), continue the main loop with this turn's output
            yield response
            continue
        # Fell through via break from terminal stop inside while
        break
```

**Rationale:** This is the PRIMARY fix location — where the original bug manifests. Agent completes LLM call, then finds itself halted by compression. Instead of breaking permanently, it waits for compression to finish and continues with tool execution/next turn.

### Site 4: Line 1809 — `_check_stop_conditions()` wrapper

**Current:**
```python
def _check_stop_conditions(self, instance: AgentInstance) -> bool:
    inst_name = instance.instance_name
    return self._is_stopped(inst_name)
```

**New:** (see Change 1 above — already updated to call `_is_terminal_stop`)

This method is called before the LLM streaming begins. If it returns True, the LLM call is skipped entirely and the main loop breaks at line 1381. Since we now use `_is_terminal_stop`, compression-halt agents won't skip their LLM calls — they'll proceed normally (or wait at Site 3 if halted mid-loop).

### Site 5: Line 1855 — `_check_stream_termination()` mid-stream poll

**Current:**
```python
if stream_tick % 20 == 0 and self._is_stopped(inst_name):
    logger.debug("[TERMINATE] Stopped mid-stream after %d ticks - %s", stream_tick, inst_name)
    return (response + turn_output + partial_msgs, False)
return None
```

**New:**
```python
if stream_tick % 20 == 0 and self._is_terminal_stop(inst_name):
    logger.debug("[TERMINATE] Stopped mid-stream after %d ticks - %s", stream_tick, inst_name)
    return (response + turn_output + partial_msgs, False)
return None
```

**Rationale:** This runs DURING LLM streaming (every 20 ticks). If we treated compression-halt as "stop" here, we'd abort the streaming response mid-way. That's wrong — compression is transient and shouldn't kill an in-flight LLM call. Only terminal stops should terminate a stream.

**Trade-off documented:** The LLM call continues even if compression halts the agent. This is acceptable because:
- Compression typically completes in <5 seconds; the stream tick check runs every ~20 ticks (much longer)
- Aborting mid-stream would waste tokens and leave partial output
- After streaming completes, Site 3 will handle any compression-halt by waiting

### Site 6: Line 2792 — Streaming loop defense-in-depth check #1

**Current:**
```python
if self._is_stopped(inst_name):
    # Telemetry cleanup...
    try: gen.close()
    except RuntimeError: pass
    yield None  # Signal UI that stop was detected mid-stream
    break
```

**New:**
```python
if self._is_terminal_stop(inst_name):
    # Telemetry cleanup...
    try: gen.close()
    except RuntimeError: pass
    yield None  # Signal UI that stop was detected mid-stream
    break
# Compression-halt during streaming: let the stream complete, handle at Site 3
```

**Rationale:** Same as Site 5 — don't abort an in-flight LLM stream for transient compression-halt.

### Site 7: Line 2820 — Streaming loop defense-in-depth check #2

**Same change as Site 6.** This is a duplicate defense-in-depth check after UI update.

```python
if self._is_terminal_stop(inst_name):
    # Telemetry cleanup...
    try: gen.close()
    except RuntimeError: pass
    yield None
    break
```

### Site 8: Line 3319 — Auto-continue gating

**Current:**
```python
if (is_truncated or is_incomplete) and not self._is_stopped(inst_name) and self.pool.settings.auto_continue:
```

**New:**
```python
if (is_truncated or is_incomplete) and not self._is_terminal_stop(inst_name) and self.pool.settings.auto_continue:
```

**Rationale:** This is a conditional gate, NOT a break. It prevents auto-continue when stopped. With the old code, compression-halt would block auto-continue even after resume — the agent would be stuck with truncated output. Using `_is_terminal_stop` ensures only real stops prevent auto-continue.

**Behavior change:** If an agent is truncated and compression halts it mid-check, auto-continue proceeds once compression finishes. This is correct — the agent needs to continue its work.

### Site 9: Line 3405 — Inside pause-wait loop escape

**Current:**
```python
while self.pool.is_paused():
    if self._is_stopped(inst_name):
        break
    self.pool.wait_if_paused(timeout=1.0)
```

**New:**
```python
while self.pool.is_paused():
    if self._is_terminal_stop(inst_name):
        break  # Terminal stop — exit pause-wait, will break at next _is_stopped check
    self.pool.wait_if_paused(timeout=1.0)
```

**Rationale:** This is an escape hatch from the pause-wait loop. If a terminal stop occurs while paused, we break out to handle it. Compression-halt doesn't make sense here because:
- The agent is already in a wait loop (paused globally)
- Compression-halt adds the instance to `_halted_instances`, but `is_paused()` is the primary condition
- If compression finishes and resumes the pool, this loop exits naturally

### Site 10: Line 3411 — Pre-tool-execution check

**Current:**
```python
if self._is_stopped(inst_name):
    break
```

**New:**
```python
if self._is_terminal_stop(inst_name):
    break
elif self._is_suspended_by_compression(inst_name):
    # Compression halted us before tool execution — wait, then retry this tool
    logger.debug("tool exec suspended by compression - %s", inst_name)
    while self._is_suspended_by_compression(inst_name):
        if self._is_terminal_stop(inst_name):
            break
        self.pool.wait_if_paused(timeout=1.0)
    else:
        continue  # Resumed, re-enter tool dispatch loop with current tool
    break  # Terminal stop during wait
```

**Rationale:** Agent is about to execute a tool when compression halts it. Wait for compression, then execute the tool. This prevents tools from being skipped due to transient halt.

### Site 11: Line 3638 — Orphan tool handling

**Current:**
```python
if self._is_stopped(inst_name):
    # Fill placeholder FUNCTION messages for unexecuted tools
```

**New:** NO CHANGE.

**Rationale:** This is NOT a break point — it's a conditional that decides whether to fill orphan placeholders. The logic "if stopped, we may have skipped some tools" is still correct with compression-halt. Even if compression halted mid-tool-loop, the placeholder filling is harmless (executed_set contains what was actually run). No functional change needed.

### Site 12: Line 4039 — `_process_response()` entry guard

**Current:**
```python
if self._is_stopped(inst_name):
    return False  # Stop detected — break from loop
```

**New:**
```python
if self._is_terminal_stop(inst_name):
    return False  # Terminal stop — break from main loop
elif self._is_suspended_by_compression(inst_name):
    # Compression halted during post-turn processing — wait, then re-process
    logger.debug("post-turn processing suspended by compression - %s", inst_name)
    while self._is_suspended_by_compression(inst_name):
        if self._is_terminal_stop(inst_name):
            return False
        self.pool.wait_if_paused(timeout=1.0)
    # Resumed — fall through to process the response normally
```

**Rationale:** After LLM returns, before processing the response (checking for tools, truncation, etc.), we check if stopped. If compression halted here, waiting and re-processing is correct — the response is already complete, we just need to handle it.

### Site 13: Line 4184 — Sleeping handler entry

**Current:**
```python
if self._is_stopped(inst_name):
    return SleepAction.BREAK_LOOP, None
```

**New:**
```python
if self._is_terminal_stop(inst_name):
    return SleepAction.BREAK_LOOP, None
# Compression-halt while sleeping: the sleep loop already polls; just continue waiting
```

**Rationale:** A sleeping agent is already in a passive wait state. Compression-halt doesn't change that — it will wake up naturally when messages arrive or async tools complete. The `_compression_halted` flag being cleared by `resume_all_instances()` happens independently. No special handling needed beyond not breaking on compression-halt.

### Site 14: Line 4210 — Post-slot-acquire after message wakeup

**Current:**
```python
if self._is_stopped(inst_name):
    return SleepAction.BREAK_LOOP, None
```

**New:**
```python
if self._is_terminal_stop(inst_name):
    return SleepAction.BREAK_LOOP, None
# Compression-halt after wakeup: proceed to main loop (Site 3 will wait if needed)
```

**Rationale:** Agent just woke up and re-acquired its slot. If compression halted it, the main loop at Site 3 will handle waiting. Don't break here — let it proceed.

### Site 15: Line 4276 — Post-slot-acquire after stable drain

**Current:**
```python
if self._is_stopped(inst_name):
    logger.debug(f"[SLOT_STOP_CHECK] Stale slot detected after stable drain for {inst_name}, exiting")
    return SleepAction.BREAK_LOOP, None
```

**New:**
```python
if self._is_terminal_stop(inst_name):
    logger.debug(f"[SLOT_STOP_CHECK] Terminal stop after stable drain for {inst_name}, exiting")
    return SleepAction.BREAK_LOOP, None
# Compression-halt: proceed to main loop (Site 3 will wait if needed)
```

**Rationale:** Same as Site 14. Agent completed stable drain and re-acquired slot; let it proceed to the main loop where compression-halt is handled properly.

### Site 16: Line 4575 — `_create_and_run_agent` generator consumer

**Current:**
```python
for resp in self.run(inst):
    if self._is_stopped(instance_name):
        break
```

**New:** NO CHANGE needed, but update comment for clarity:

```python
for resp in self.run(inst):
    # Inner run() loop handles compression-halt via cooperative wait at Site 3.
    # Only break here on terminal stops (which cause run() to yield final state and end).
    if self._is_terminal_stop(instance_name):
        break
```

**Rationale:** This is the outer consumer of the `run()` generator. If compression halts the agent, the inner loop at Site 3 waits and then continues yielding. The outer loop should only break on terminal stops that cause `run()` to finalize. Changing this to `_is_terminal_stop` makes the intent explicit and prevents a race where the outer loop breaks before the inner loop has a chance to wait.

### Site 17: Line 4635 — Lambda to `run_auto_skill_proposal`

**Current:**
```python
is_stopped=lambda: self._is_stopped(instance_name),
```

**New:**
```python
is_stopped=lambda: self._is_terminal_stop(instance_name),
```

**Rationale:** This lambda is passed to an external helper that uses it to check whether to abort skill proposal mid-run. Auto-skill proposal should only abort on terminal stops, not transient compression-halt. If compression runs during skill proposal, let it complete or wait naturally.

---

## Change 3: Fix Misleading "Stopped by User" Message

**File:** `agent_cascade/child_runner.py`  
**Lines affected:** 32–41 (`_check_status`) and 16–29 (`_format_result`)

### Current Behavior

```python
def _check_status(pool, instance_name: str) -> tuple[bool, bool]:
    stop_flag = pool.stopped
    halted_flag = pool.is_instance_halted(instance_name)
    was_terminated = instance_name in pool.terminated_instances
    return (stop_flag or halted_flag), was_terminated
```

Returns `was_stopped=True` when agent was halted by compression → prints:
> `[Agent 'async_shell_fixer' Stopped]: Execution was stopped by user.`

### Proposed Behavior

```python
def _check_status(pool, instance_name: str) -> tuple[bool, bool]:
    """Check if an agent was stopped/halted or terminated.
    
    Returns:
        (was_stopped_by_user, was_terminated) tuple.
        
    Note: Compression-halt is NOT treated as user stop — it's a transient
    suspension that clears automatically via resume_all_instances(). Only 
    pool.stopped (global user stop) and manual halt count as 'stopped'.
    """
    stop_flag = pool.stopped
    # Only count MANUAL halt (in _halted_instances but NOT in _compression_halted) 
    # as "stopped". Compression-halt is transient.
    was_manual_halt = (instance_name in pool._halted_instances and 
                       instance_name not in pool._compression_halted)
    was_terminated = instance_name in pool.terminated_instances
    return (stop_flag or was_manual_halt), was_terminated
```

**Effect:** 
- User stop (`pool.stopped=True`) → "stopped by user" ✓
- Manual halt via API → "stopped" ✓  
- Compression-halt → NOT "stopped by user", formats as "Completed" with actual output ✓
- Terminate → "terminated" ✓

---

## Change 4: Fix Wrong Log Path in Warning Messages

**File:** `agent_cascade/compression/helpers.py`  
**Lines affected:** 314–358 (`extract_instance_output`)

### Current Behavior

```python
def extract_instance_output(messages, instance_name, was_terminated=False):
    # ...
    if msg_role == FUNCTION:
        return (f"WARNING: Sub-agent {instance_name} terminated with a tool result "
                f"(no final text output). Check log for details: "
                f"{instance_name}.log")
```

Uses bare `{instance_name}.log` which doesn't exist.

### Proposed Behavior

Add `pool` parameter and resolve actual log path:

```python
def extract_instance_output(
    messages, 
    instance_name: str, 
    was_terminated: bool = False,
    pool=None  # Optional: AgentPool to resolve actual log path
):
    """Extract final output text from agent conversation.
    
    Args:
        messages: Conversation messages list
        instance_name: Agent instance name
        was_terminated: Whether agent was terminated by user
        pool: Optional AgentPool for resolving actual log file paths
    
    Returns:
        Extracted result string or warning message.
    """
    if not messages:
        return f"Sub-agent {instance_name} produced no output."
    
    # Helper to get the best available log path hint
    def _get_log_path_hint():
        if pool is not None:
            try:
                logger_obj = pool.get_logger(instance_name)
                if hasattr(logger_obj, 'log_path') and logger_obj.log_path:
                    return str(logger_obj.log_path)
            except Exception:
                pass
        # Fallback: give a useful hint about where to look
        return f"logs/ (search for '{instance_name}' in AgentWorkspace/logs/)"

    # Get the last message in the conversation
    last_msg = messages[-1]

    if isinstance(last_msg, dict):
        msg_role = last_msg.get('role', '')
    else:
        msg_role = getattr(last_msg, 'role', '')

    # Guard: if the last message is a tool result (function role), the agent
    # likely terminated incorrectly without producing a final text response.
    if msg_role == FUNCTION:
        log_hint = _get_log_path_hint()
        return (f"WARNING: Sub-agent {instance_name} terminated with a tool result "
                f"(no final text output). Check log for details: {log_hint}")

    result_str = extract_text_from_message(last_msg, add_upload_info=False).strip()

    if not result_str:
        if was_terminated:
            log_hint = _get_log_path_hint()
            return (f"Sub-agent {instance_name} was terminated by user. "
                    f"Check log for details: {log_hint}")
        return f"WARNING: Sub-agent {instance_name} produced no text output in its final message (role={msg_role})."

    return result_str
```

**Caller update in child_runner.py line 101:**

```python
# Change from:
result = extract_instance_output(conv, instance_name, was_terminated=was_terminated)
# To:
result = extract_instance_output(conv, instance_name, was_terminated=was_terminated, pool=pool)
```

---

## Change 5: `halt_all_instances` During Forced Compression — Decision Documented

**File:** `agent_cascade/compression/handler.py`  
**Line affected:** 564

### Decision: Keep `halt_all_instances`, no change needed

The suspendable-halt approach (Changes 1–2) solves the problem without modifying the compression handler. Agents that are halted by compression will wait cooperatively at Sites 3, 10, and 12, then resume when `_compression_halted` is cleared.

**Why not switch to pause mechanism?**
- Pause (`pool.is_paused()`) only guards tool dispatch (Site 9), not LLM streaming
- Would need additional wait points in the streaming loop
- `halt_all_instances` with suspendable handling is cleaner

**Why not exclude sub-agents from halting?**
- Risky — concurrent agents could mutate conversations during compression
- The compression lock (`instance._compression_lock`) protects the target, not bystanders

**Why not use a pool-level compression RLock?**
- Would require restructuring streaming loop to acquire/release lock around mutation
- More invasive than the current fix

---

## Implementation Order

1. **Change 1 first** (`execution_engine.py` lines 1800–1831) — add `_is_terminal_stop()` and `_is_suspended_by_compression()`. All other changes depend on these helpers existing.

2. **Change 2 second** (`execution_engine.py` call sites) — update all 17 call sites in this order:
   - Sites 6, 7 (streaming loop: lines 2792, 2820) — safest to fix first since they're mid-stream critical
   - Site 5 (line 1855) — related to streaming
   - Site 3 (line 1381) — PRIMARY fix, add wait loop
   - Site 10 (line 3411) — tool execution wait loop
   - Site 12 (line 4039) — post-turn wait loop
   - Sites 1, 2, 4, 8, 9, 13, 14, 15, 16, 17 — simple `_is_terminal_stop` substitution
   - Site 11 — no change needed

3. **Change 3 third** (`child_runner.py`) — messaging fix, independent of engine changes.

4. **Change 4 fourth** (`helpers.py` + `child_runner.py`) — cosmetic log path fix, fully independent.

5. **Change 5** — documentation only, no code change.

---

## Potential Side Effects and Mitigations

### Side Effect 1: Agents blocked during long compressions

**Risk:** If compression takes >30 seconds, concurrent agents spin in `while _is_suspended_by_compression()` loops calling `wait_if_paused(timeout=1.0)`.

**Mitigation:** 
- Compression typically completes in <5 seconds
- The 1-second timeout uses Event-based waiting (efficient, not busy-spinning)
- Terminal stop check inside the wait loop prevents deadlock if user stops during compression

### Side Effect 2: Streaming continues during compression-halt

**Risk:** Sites 5, 6, 7 now ignore compression-halt during streaming — the LLM call completes even though the agent is "halted."

**Mitigation:** This is intentional and correct:
- Aborting mid-stream wastes tokens and leaves partial output
- Compression is fast (<5s); stream tick check is every ~20 ticks (much longer)
- After streaming completes, Site 3 handles compression-halt properly by waiting

### Side Effect 3: Auto-continue behavior change

**Risk:** Site 8 now allows auto-continue even if compression-halted at that exact moment.

**Mitigation:** This is the correct behavior — the agent needs to continue its truncated work. Compression-halt shouldn't block recovery from truncation.

### Side Effect 4: `_create_and_run_agent` outer loop change (Site 16)

**Risk:** Changing Site 16 from `_is_stopped` to `_is_terminal_stop` could theoretically allow the outer loop to continue when the inner `run()` generator has ended due to compression-halt.

**Mitigation:** This cannot happen because:
- The inner `run()` generator only ends (stops yielding) on terminal conditions or natural completion
- Compression-halt causes `run()` to WAIT at Site 3, then CONTINUE yielding
- If a terminal stop occurs, both loops break consistently

### Side Effect 5: Backward compatibility

**Risk:** External code may call `_is_stopped()` expecting it to return True for any halt.

**Mitigation:** 
- `_is_stopped()` still returns True for halted instances — we only changed internal callers
- The new helper methods are additive; no API removed
- Document the distinction in `_is_stopped()` docstring

---

## Testing Plan

All tests should be verified by grepping agent log files under `AgentWorkspace/logs/` and/or `AgentCascade/logs/console.log`. Log assertions use exact strings from the new code.

---

### Test 1: Regression test for original bug (Sites 3, 10, 12)

**Setup:**
1. Start orchestrator with two concurrent sub-agents: Agent A (sync coder, task requiring ≥5 turns), Agent B (async researcher, high-context consumer padded to trigger forced compression at >95%)
2. Force Agent B to hit forced-compression threshold while Agent A is mid-turn

**Expected behavior:**
- Agent A enters cooperative wait loop at Site 3 when compression-halted
- After `resume_all_instances()` clears the flag, Agent A continues its turn
- Agent A completes successfully with final output
- No "Execution was stopped by user" message for Agent A

**Log assertions (grep these exact strings):**
```bash
# Agent A enters wait loop
grep "suspended by compression, waiting - agent_a" AgentWorkspace/logs/*.jsonl

# Compression completes and resumes all
grep "resume_all_instances" AgentCascade/logs/console.log

# Agent A continues with tool execution (shows it didn't break at Site 3)
grep "tool used.*agent_a looping" AgentWorkspace/logs/*.jsonl

# OR agent A auto-continues after compression resume
grep "AUTO-CONTINUE.*agent_a" AgentWorkspace/logs/*.jsonl

# Successful completion
grep "SLOT_SYNC_CHILD_COMPLETE.*agent_a" AgentCascade/logs/console.log
```

**Negative assertions (must NOT appear):**
```bash
# Must NOT see terminal stop or user-stop label for agent_a
! grep "terminal stop - agent_a" AgentWorkspace/logs/*.jsonl
! grep "Execution was stopped by user.*agent_a" AgentCascade/logs/console.log
! grep "halted/stopped/superseded - agent_a" AgentWorkspace/logs/*.jsonl  # old log string gone
```

---

### Test 2: Compression-halt during slot acquisition (Sites 1 & 2)

**Setup:**
1. Launch an agent whose startup coincides with forced compression on another agent
2. This can be triggered by launching Agent C immediately after forced-compression fires on Agent B

**Expected behavior:**
- Agent C proceeds to acquire its slot despite being in `_compression_halted` set at startup time
- Agent C runs normally, does not exit early

**Log assertions:**
```bash
# Agent C acquires slot (no early return)
grep "acquiring.*slot.*agent_c\|SLOT_ACQUIRED.*agent_c" AgentCascade/logs/console.log

# Agent C completes turns
grep "LLM_DONE.*agent_c\|tool used.*agent_c" AgentWorkspace/logs/*.jsonl
```

**Negative assertions:**
```bash
# Must NOT see early exit at startup
! grep "Exit generator immediately.*agent_c" AgentWorkspace/logs/*.jsonl
! grep "Stale slot detected.*agent_c" AgentWorkspace/logs/*.jsonl  # not at startup time
```

---

### Test 3: Concurrent compression and streaming — stream completes, then Site 3 waits

**Setup:**
1. Use an agent configured with a slow LLM endpoint or large expected response (many tokens)
2. Trigger forced compression on another agent while this agent is mid-stream

**Expected behavior:**
- Streaming continues to completion despite compression-halt flag being set
- After stream ends, Site 3 detects `_is_suspended_by_compression`, enters wait loop
- Agent resumes processing after compression finishes

**Streaming timing assumption documented:** It is OK for compression to take longer than streaming. If the stream completes before compression finishes, Site 3's wait loop handles it. If compression finishes before the stream ends, the agent simply proceeds past Site 3 without waiting (flag already cleared). Both orderings are safe.

**Log assertions:**
```bash
# Stream continues during compression-halt (no mid-stream abort)
grep "end.*stream\|LLM_DONE" AgentWorkspace/logs/*.jsonl | grep "<agent_name>"

# After stream, Site 3 wait loop activates
grep "suspended by compression, waiting - <agent_name>" AgentWorkspace/logs/*.jsonl

# No gen.close() called mid-stream due to compression-halt.
# Check: if gen.close() appears for this agent, it should ONLY be in a "finally" block context,
# not paired with compression-halt stop logic (which would abort the stream).
! grep "gen.close()" AgentWorkspace/logs/*.jsonl | grep "<agent_name>" | grep -v "finally" | grep -v "RuntimeError"
```

---

### Test 4: resume_all_instances() called DURING a wait loop — no deadlock

**Setup:**
1. Agent A is in the Site 3 wait loop (`while _is_suspended_by_compression`)
2. Compression completes and calls `resume_all_instances()` which clears `_compression_halted`

**Expected behavior:**
- The wait loop condition `_is_suspended_by_compression()` becomes False on next iteration
- Agent A exits the while loop via the `else` branch (for-else semantics: loop completed without break)
- Agent A continues with `yield response; continue` back into main loop
- No deadlock, no timeout required

**Log assertions:**
```bash
# Wait loop entered
grep "suspended by compression, waiting - agent_a" AgentWorkspace/logs/*.jsonl

# Compression completes and resumes
grep "resume_all_instances" AgentCascade/logs/console.log

# Agent A continues main loop (the 'continue' after wait loop else-branch)
grep "tool used.*agent_a looping\|Phase 5.*agent_a" AgentWorkspace/logs/*.jsonl | head -1
```

**Verification of no deadlock:** Agent A's next turn starts within ~2 seconds of `resume_all_instances` log entry. If it takes >10 seconds, something is wrong (the wait loop uses 1-second timeout on `wait_if_paused`).

---

### Test 5: Terminal stop during compression — agent exits immediately, no spin

**Setup:**
1. Agent A is in a Site 3 wait loop (compression-halted)
2. User clicks "Stop" while compression is still running

**Expected behavior:**
- The terminal-stop check inside the wait loop detects `pool.stopped=True`
- Agent breaks out of wait loop immediately, does not continue spinning
- Agent yields final response and exits cleanly

**Log assertions:**
```bash
# Wait loop entered
grep "suspended by compression, waiting - agent_a" AgentWorkspace/logs/*.jsonl

# Terminal stop detected (inside wait loop)
grep "terminal stop - agent_a" AgentWorkspace/logs/*.jsonl

# Clean exit
grep "EXIT.*agent_a.*RUNNING→IDLE" AgentCascade/logs/console.log
```

**Negative assertions:**
```bash
# Must NOT see continued waiting after terminal stop
# (verify no more "suspended by compression" messages after "terminal stop")
```

---

### Test 6: Tool execution resumption after compression-halt (Site 10) — tool actually executes

**Setup:**
1. Agent produces a response with a tool call (e.g., `edit_file`)
2. Compression halts the agent right before tool dispatch loop begins
3. Compression completes, agent resumes

**Expected behavior:**
- Site 10 wait loop activates: `"tool exec suspended by compression"`
- After resume, the `continue` re-enters the tool dispatch loop
- The tool is actually executed (not skipped) — verify in logs that the tool function runs

**Log assertions:**
```bash
# Tool execution suspended
grep "tool exec suspended by compression - agent_a" AgentWorkspace/logs/*.jsonl

# Resume happens
grep "resume_all_instances" AgentCascade/logs/console.log

# Tool actually executes AFTER resume (this is the critical assertion)
grep "Executing tool.*edit_file.*agent_a\|Tool result.*agent_a" AgentWorkspace/logs/*.jsonl
```

**Negative assertions:**
```bash
# Must NOT see orphan tool handling triggered for this tool
! grep "orphaned tool call.*agent_a" AgentWorkspace/logs/*.jsonl | grep -v "different_tool_name"
```

---

### Test 7: Sleeping agents during compression-halt (Sites 13–15) — don't wake early, don't miss turn

**Setup:**
1. Agent A is in SLEEPING state (waiting for async tool results)
2. Forced compression halts Agent A via `halt_all_instances`
3. Compression completes and resumes all

**Expected behavior:**
- Site 13 check sees `_is_terminal_stop()` = False, does NOT return BREAK_LOOP
- Agent remains in SLEEPING state (doesn't wake prematurely due to compression-halt)
- When async tool results arrive, agent wakes via normal message drain path
- Sites 14/15 checks see terminal-stop = False, proceed to main loop
- Agent processes the async results normally

**Log assertions:**
```bash
# Agent is sleeping
grep "SLEEPING.*agent_a.*waiting.*for background tools" AgentWorkspace/logs/*.jsonl

# Compression happens (agent_a in _compression_halted)
grep "forcing compression" AgentCascade/logs/console.log

# Agent does NOT exit during sleep due to compression-halt
! grep "EXIT.*agent_a.*SLEEPING→IDLE\|BREAK_LOOP.*agent_a.*compression" AgentWorkspace/logs/*.jsonl

# Agent wakes normally on async result (not early)
grep "RESUMED from SLEEPING.*agent_a" AgentWorkspace/logs/*.jsonl

# Agent processes results after wake
grep "Phase 5\|tool used.*agent_a looping" AgentWorkspace/logs/*.jsonl | grep -A2 "RESUMED from SLEEPING"
```

---

### Test 8: Message labeling correct for compression-halt (Change 3)

**Setup:** Run Test 1 scenario where agent was compression-halted but completed.

**Expected behavior and assertions:**

```bash
# Agent formats as "Completed", NOT "Stopped"
grep "\[Agent 'agent_a' Completed\]" AgentCascade/logs/console.log

# Must NOT see user-stop label
! grep "Execution was stopped by user.*agent_a" AgentCascade/logs/console.log
```

**Additional sub-tests:**

- **User stop still labels correctly:** Trigger actual user stop → verify:
  ```bash
  grep "\[Agent 'agent_x' Stopped\]: Execution was stopped by user." AgentCascade/logs/console.log
  ```

- **Terminate labels correctly:** Terminate agent via API → verify:
  ```bash
  grep "\[Agent 'agent_y' Terminated\]" AgentCascade/logs/console.log
  ```

---

### Test 9: Log path is correct in warning messages (Change 4)

**Setup:** Create a scenario where `extract_instance_output` triggers the FUNCTION-role warning (agent ends on tool result with no final text).

**Expected behavior:** Warning message contains an actual file path, not bare `{instance_name}.log`.

**Log assertions:**
```bash
# Must see actual path with directory structure
grep "Check log for details:.*logs/.*\.jsonl" AgentCascade/logs/console.log

# Must NOT see bare name pattern
! grep "Check log for details: agent_x.log" AgentCascade/logs/console.log
```

---

### Test 10: Auto-continue after compression-halt (Site 8)

**Setup:** Agent is truncated, forced compression halts it during the truncation/incomplete check.

**Expected behavior:** After resume, auto-continue proceeds normally — agent doesn't get stuck with truncated output.

**Log assertions:**
```bash
# Truncation detected
grep "Detected truncation.*auto-continuing\|Detected incomplete state.*Auto-continuing" AgentWorkspace/logs/*.jsonl

# Compression-halt during check (optional to observe, timing-dependent)
grep "suspended by compression\|post-turn processing suspended by compression" AgentWorkspace/logs/*.jsonl

# Auto-continue succeeds after resume
grep "AUTO-CONTINUE.*agent_a" AgentWorkspace/logs/*.jsonl
```

---

### Test 11: Performance — long compression (>30 seconds) behavior

**Setup:** Simulate slow compression (e.g., artificially large context or patched compression handler with sleep).

**Expected behavior:**
- Agents in wait loops poll every 1 second via `wait_if_paused(timeout=1.0)`
- This is Event-based waiting, NOT busy-spinning — CPU usage remains low
- Terminal stop check inside wait loop ensures user can still abort
- No timeout limit imposed on the wait (compression will eventually complete)

**Verification:**
```bash
# Count how many times an agent loops during a 30+ second compression
# Each iteration logs nothing by default, but we can add a periodic debug log if needed:
grep "suspended by compression.*agent_a" AgentWorkspace/logs/*.jsonl | wc -l
# Should be exactly 1 (the initial entry), not repeated every second

# If compression takes 30+ seconds, agent resumes within ~1 second of resume_all_instances
```

**Safeguard documented:** If needed in the future, add a debug log every N iterations:
```python
wait_count = 0
while self._is_suspended_by_compression(inst_name):
    if wait_count > 0 and wait_count % 30 == 0:
        logger.debug("still waiting for compression to complete - %s (%ds)", inst_name, wait_count)
    wait_count += 1
```

---

### Test 12: Streaming timing edge case — compression finishes BEFORE stream ends

**Setup:** Very fast compression on small context while agent streams a large response.

**Expected behavior:**
- Agent is added to `_compression_halted`, then quickly removed by `resume_all_instances()`
- Stream completes; at Site 3, `_is_suspended_by_compression()` returns False (already cleared)
- Agent proceeds normally without ever entering wait loop

**Log assertions:**
```bash
# Compression starts and finishes quickly
grep "forcing compression" AgentCascade/logs/console.log
grep "resume_all_instances" AgentCascade/logs/console.log

# Stream completes normally
grep "LLM_DONE.*agent_a" AgentWorkspace/logs/*.jsonl

# No wait loop entered (flag already cleared by Site 3)
! grep "suspended by compression, waiting - agent_a" AgentWorkspace/logs/*.jsonl
```

This confirms the fix handles both orderings: compression-during-stream AND stream-during-compression.

---

## Code Summary

| File | Lines Changed | Description |
|------|---------------|-------------|
| `execution_engine.py` | 1800–1831 | Add `_is_terminal_stop()`, `_is_suspended_by_compression()`; update `_check_stop_conditions()` |
| `execution_engine.py` | 1126, 1133 | Startup guards: terminal only |
| `execution_engine.py` | 1381–1384 | Main loop: wait on compression-halt (PRIMARY FIX) |
| `execution_engine.py` | 1855 | Stream termination poll: terminal only |
| `execution_engine.py` | 2792, 2820 | Streaming defense checks: terminal only |
| `execution_engine.py` | 3319 | Auto-continue gate: terminal only |
| `execution_engine.py` | 3405 | Pause-wait escape: terminal only |
| `execution_engine.py` | 3411–3412 | Tool exec check: wait on compression-halt |
| `execution_engine.py` | 3638 | Orphan handling: no change |
| `execution_engine.py` | 4039–4040 | Post-turn gate: wait on compression-halt |
| `execution_engine.py` | 4184, 4210, 4276 | Sleep handlers: terminal only |
| `execution_engine.py` | 4575 | Outer consumer: terminal only |
| `execution_engine.py` | 4635 | Auto-skill lambda: terminal only |
| `child_runner.py` | 32–41, 101 | Distinguish compression-halt from user stop; pass pool to extract_instance_output |
| `compression/helpers.py` | 314–358 | Add pool parameter, use actual log_path |

---

## Notes for Implementation

- The `_is_suspended_by_compression()` check uses `pool._compression_halted` directly. This is acceptable — execution_engine already accesses other pool internals like `_halted_instances` and `_run_generation`.
- Consider adding a pool-level method `is_instance_compression_halted(name)` for encapsulation in a follow-up refactor, but not required for this fix.
- All new wait loops follow the same pattern: check terminal stop inside the loop to avoid deadlocks if user stops during compression.
- The streaming loop changes (Sites 5, 6, 7) are intentionally conservative — they only change to `_is_terminal_stop` without adding wait logic, because waiting mid-stream is unnecessary (stream completes quickly and Site 3 handles post-stream halt).