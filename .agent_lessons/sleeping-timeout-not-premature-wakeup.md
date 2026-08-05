---
tags: [sleeping-state, async-agents, timeout, compression, root-cause]
aliases: [premature-sleeping-wakeup, sleeping-timeout-300s]
related: [[lessons_async_call_agent]], [[lessons_parent_slot_fix]], [[lessons_idle_timer_fix]]
confidence: verified
---

# Root agent "premature SLEEPING wakeup" is actually the 300s SLEEPING TIMEOUT

## Fact

The reported bug — "root agent drops out of SLEEPING during long async calls,
possibly triggered by compression in child agents" — is **NOT a premature
wakeup and NOT caused by child compression**. It is the **SLEEPING TIMEOUT**
(300s default) transitioning the root `COMPLETING→IDLE` while the child keeps
running. The child's result then arrives later as a queued USER message.

## Evidence (logs, 2026-08-04 10:06–10:26, console.log.1)

- `10:06:28` Maine launches `compression_timing_investigator` (researcher) ASYNC
  (tool_dispatcher.py:538 "Taking ASYNC path").
- `10:06:31` Maine enters SLEEPING: `execution_engine.py:3842/3978` "Pending
  async tools for Maine. Transitioning to SLEEPING."
- `10:06:31 → 10:11:31` Maine logs `SLEEPING - waiting Ns` every 5s, 300s total.
- `10:11:31` `execution_engine.py:4129/4264` WARNING **"SLEEPING TIMEOUT - Maine
  waited 300.0s (timeout=300s)"** → `EXIT - Maine COMPLETING→IDLE`.
- `10:17:46–10:19:26` Child researcher's Compressor_1 runs (compression of the
  child's own history) — **Maine is already IDLE at this point** (8 min after).
- `10:23:14` Child completes. `10:26:37` Result arrives as a USER message
  ("well?" + "[Agent ... Completed]") — user had to prompt; Maine then resumed
  normally. No `RESUMED from SLEEPING` log for Maine in this window.

## Why compression does NOT wake the parent (code refs)

1. Compressor runs **synchronously on the caller's thread** via
   `agent_invoker.py:276-289` (`_skip_slot_acquire = True`, `engine.run()` loop,
   no `register_async_call`).
2. Nothing in the compression path writes to `pool._async_results`
   (AsyncResultBuffer) — the ONLY wakeup signal for a SLEEPING agent
   (`execution_engine.py:4202-4204` drains `drain_async_results`).
3. `_config_version` (`agent_pool.py:2041`) only causes a working-set rebuild,
   never a state transition.
4. `_run_generation` (stop detection, `execution_engine.py:1830-1833`) is only
   incremented on stop/resume (`ws_handlers.py:330`, `run_agent_unified.py:94`),
   never during compression.

## Where SLEEPING is managed (current line refs)

- Transition in: `execution_engine.py:3955-3982` (`_transition_to_sleeping_if_pending`),
  `4110-4159` (`_transition_to_sleeping`).
- Wake/loop: `execution_engine.py:4165-4349` (`_handle_sleeping_state`),
  guard at `1236-1252` in `run()`.
- Timeout: `execution_engine.py:4264-4278` — `sleeping_duration >= sleeping_timeout`
  → `COMPLETING` + `BREAK_LOOP`. Default 300s (`settings.py:89-90`,
  `AGENT_SLEEPING_TIMEOUT`), tunable via UI (`config_handlers.py:636-647`).
- Pending check: `agent_pool.py:2393-2412` (`has_pending` → AsyncToolRegistry +
  AsyncShellTracker); result buffer `async_tools.py:215-298`.

## Fix direction (if desired behavior is "wait indefinitely")

- Raise `AGENT_SLEEPING_TIMEOUT` / expose in UI, OR
- Make timeout non-destructive: on timeout keep instance SLEEPING but stop
  burning CPU (log-only), OR
- Have the child completion path re-deliver the result as a USER message to a
  now-IDLE root (already happens via message queue — the user message at
  10:26:37 shows this works, but root was IDLE not SLEEPING).

## Related observation

Nested async chains (root → child → grandchild) can easily exceed 300s; each
level's timeout applies independently. See
[[lessons_async_call_agent]] and [[lessons_parent_slot_fix]] for the async slot
design that makes long waits expected.