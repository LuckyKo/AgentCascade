# Investigation Report — Sync Agent Interrupted When Async Agent Finishes

**Investigator:** researcher (investigator_async_interrupt)
**Date:** 2026-08-06
**Ticket:** todo.md line 90 — "Sync Coder gets interrupted when async Researcher finishes?"
**Status:** Root cause identified (verified via code + incident logs)

---

## Executive Summary

The synchronous coder sub-agent (`async_shell_fixer`) was **not actually interrupted by the async researcher finishing**. It was halted by **forced context compression** triggered on the concurrent researcher (`bug_investigator_2`) when that agent's context hit 96.8% of limit.

The compression path calls `pool.halt_all_instances()` — which marks **every** other active agent (including the running sync coder) as *halted*. The coder's engine run-loop treats "halted" as a terminal condition and `break`s out of its turn loop mid-work. After `resume_all_instances()` runs, nothing restarts the sub-agent mid-turn, so it comes back to the orchestrator as *stopped with no final output*.

The async-completion timing is an *occasion*, not a *cause*: the researcher's async result is drained into its conversation, which triggers a proactive compression check at 96.8% context. Any high-context compression (forced, post-tool, or async-drain) would halter concurrent agents.

Confidence: **High** (root cause mechanism verified in code; incident logs corroborate the exact sequence).

---

## Key Findings

### Finding 1 — Forced compression halts all OTHER agents (primary cause)
- **File:** `agent_cascade/compression/handler.py` line 564
  ```python
  self.pool.halt_all_instances(except_instances=exempt)
  ```
- Exempt list (lines 556-562) = compress target + its parent + all `Compressor_*`. A **concurrent sub-agent is not exempt** and gets added to `pool._halted_instances`.
- Note the docstring/lesson mismatch: `compress_context` *can* be invoked without halt, but the **forced** path (`execute_force_compression`) always halts. Logs: `[bug_investigator_2] post-tool proactive check: context at 96.8% ... forcing compression (attempt #1)` at 00:25:12,907 — during which `async_shell_fixer` was mid-run.

### Finding 2 — Halt → engine treats as terminal and BREAKs the loop
- **File:** `agent_cascade/execution_engine.py` lines 1828-1831 (`_is_stopped`)
  ```python
  return (self.pool.stopped or
          self._my_generation != self.pool._run_generation or
          inst_name in self.pool._halted_instances or
          self.pool.is_instance_terminated(inst_name))
  ```
- **Halt (compression) and Manual-halt and Terminate and Stop are conflated** into one `_is_stopped()` terminal condition.
- Break points where the coder's run exits permanently:
  - Line 1381-1384: `if not terminated_during_stream and self._is_stopped(...): break`
  - Lines 3405-3412: cooperative pause-wait then `_is_stopped` → break (in tool exec)
  - Lines 4184-4185 / 4210-4211: sleeping-state handler BREAK_LOOP
  - `_create_and_run_agent` lines 4574-4576: `if self._is_stopped(instance_name): break`

### 3 — No auto-resume for a mid-turn sub-agent
- `resume_all_instances()` only clears the halt flags (`agent_pool.py:897-901`, called in `handler.py:643` `finally`). The coder's `_create_and_run_agent`/`run()` loop has **already broken**, so clearing the flag does not restart the turn.
- The coder transitions `RUNNING→IDLE` (log 00:25:13,048) and returns via `_create_and_run_agent` with `reason=aborted` (`_create_completed` stays False).

### 4 — The "was stopped by user" label is a mislabel
- **File:** `agent_cascade/child_runner.py` lines 32-41 `_check_status()`:
  ```python
  stop_flag = pool.stopped
  halted_flag = pool.is_instance_halted(instance_name)
  was_terminated = ...
  return (stop_flag or halted_flag), was_terminated
  ```
- **Compression-halt sets `halted_flag`, so `was_stopped=True`** → `_format_result(was_stopped=True)` (line 25) prints `[Agent 'async_shell_fixer' Stopped]: Execution was stopped by user.` even when **no user action occurred**. This is the misleading message at todo.md line 92.

### 5 — Wrong log file path in the warning
- **File:** `agent_cascade/compression/helpers.py` lines 346-348:
  ```python
  return (f"WARNING: Sub-agent {instance_name} terminated with a tool result "
          f"(no final text output). Check log for details: {instance_name}.log")
  ```
  Uses a bare `{instance_name}.log`, but real logs live under `AgentWorkspace/logs/` (e.g. `coder_async_shell_fixer_20260806_002042.jsonl`). Cosmetic; the `{instance_name}.log` path doesn't exist. Real path is available via `pool.get_logger(instance_name, agent_class).log_path`.
- **Why was last message a tool result?** The coder mid-turn issued an `edit_file` tool call (last entry: role=assistant, `function_call` = `edit_file` to async_shell.py, finish_reason=tool_calls at 01:15:52... actually the log's last update was 01:10 — see note below). The engine broke out of the loop before it could produce a final assistant text message, leaving the conversation ending on a tool-call/tool-result → hence "terminated with a tool result / no final text output".

### 6 — What async-completion "triggered" (why the timing matched)
- `execution_engine.py:1008-1010`: `_drain_and_inject(...)` calls `_proactive_compression_check(..., check_label="async-drain")` after injecting async results. At 96.8% context it fired `_force_compression` → halt_all_instances. The researcher's async finish is the *trigger event* that happens to coincide — not the mechanistic cause.

---

## Incident timeline (AgentCascade/logs/console.log)
| Time | Event |
|------|-------|
| 00:20:42 | Maine launches sync coder `async_shell_fixer` via call_agent (SLOT_SYNC_RELEASE) |
| ~00:20-00:25 | Researcher `bug_investigator_2` (async) and Coder run concurrently |
| 00:25:12,907 | `[bug_investigator_2] post-tool proactive check: context at 96.8% ... forcing compression` |
| 00:25:13,048 | `EXIT - async_shell_fixer RUNNING→IDLE` (halted by compression's halt_all_instances) |
| 00:25:13 | `_create_and_run_agent EXIT — target=async_shell_fixer, reason=completed?? conv_len=2, final_resp_len=60` (final_resp_len=60 = the tool result warning text) |
| 00:25:13 | Compressor_1 launched (the forced compression compressed bug_investigator_2) |
| 00:25:19 | `[SLOT_SYNC_CHILD_COMPLETE] Sync child 'async_shell_fixer' completed in 277.55s` |

The `reason=completed yet final_resp_len=60` and "no final text output" are consistent with the halted coder returning its warning string.

---

## Recommendation (fix options)

**Option A (minimal, safest) — compression should pause, not halt.**
Change `execute_force_compression` to not call `halt_all_instances` — or make _is_stopped distinguish compression-halt from terminal halt. In `_is_stopped`, treat `_compression_halted` members as **suspendable** (clean ft back at the top of the loop / return CONTINUE_LOOP after `resume_all_instances`), and only `break` on manual halt/terminate/global-stop/generation-change.

**Option B — Dedicated compression lock instead of halt-all.**
Serialize the forced-compression mutation under a pool-level RLock that concurrent agents respect (block, don't break), instead of marking them halted. Mirrors existing `_compression_lock`.

**Option C — Restart sub-agent after resumed halt.**
After `resume_all_instances()`, inject a "continue" message into agent queues for any instance that was interrupted, so the engine picks it up instead of returning immediately. (This matches the existing manual resume pattern in stop_resume_fix.)

**Also fix (separate, cosmetic):**
- `compression/helpers.py:346-348` → emit real log path via `pool.get_logger(...).log_path`.
- `child_runner.py:38-41`/`_format_result` → distinguish "halted by compression" from real user stop; do not print "Execution was stopped by user" for a forced-compression-oriented halt.

---

## Alternatives / Risks
- **Pause vs halt**: pause machinery exists at `execution_engine.py:3404-3407` (`while self.pool.is_paused(): wait_if_paused`) but only guards tool *dispatch*; it does not stop LLM streaming. Would need a second cooperative wait point in the streaming loop. Risk: sleeping agents and mid-stream agents may need wakeups.
- **Hold off compression while sub-agents run**: simplest, but delays compression → OOM risk at high context. Not ideal as primary fix.

---

## Confidence Levels
- **Confirmed:** forced compression calls `halt_all_instances` (handler.py:564).
- **Confirmed:** `_is_stopped` includes `halted` and triggers loop break (execution_engine.py:1828, 1384, 4576).
- **Confirmed:** `child_runner.py:38-41` maps halt → `was_stopped` → "stopped by user" label.
- **Confirmed:** incident logs show compression-at-high-context immediately precedes coder's RUNNING→IDLE EXIT.
- **High:** async completion is the coincidental trigger (via async-drain compression check), not the mechanism.

## Remaining Unknowns
- Whether a manual user stop was also involved around 00:25 (there was no `handle_stop`/bookstop in console.log — but partial console rollover could obscure it). The halt mechanism alone explains the symptom, so a user stop is not required.

---

## Suggested Next Actions
1. Implement fix Option A (treat `_compression_halted` as suspendable, not terminal) or Option B (pool compression lock).
2. Fix `compression/helpers.py:346` log path.
3. Add differentiation in `child_runner` result labeling.
4. Add regression test: run a sync sub-agent while forced compression fires on another → assert the sync agent is not aborted.