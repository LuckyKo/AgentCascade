---
tags: [async-shell, compression, halt, inter-agent, race]
aliases: [async-interrupt-compression-halt, sync-agent-stopped-by-async]
related: [[compression-bug-root-cause-analysis]], [[stop-resume-fix]]
confidence: verified
---

# Sync Agent Interrupted When Async Agent Finishes — Root Cause: Forced Compression Halts ALL Other Instances

## The Bug (todo.md line 90)

A synchronous coder agent (`async_shell_fixer`) running inside Maine's `call_agent` sync path got labeled `[Agent 'async_shell_fixer' Stopped]: Execution was stopped by user.` and returned "no final text output" right when a concurrent async researcher (`bug_investigator_2`) hit 96.8% context and triggered **forced compression** (logs: `execution_engine.py - 2185` at 2026-08-06 00:25:12,907, `EXIT - async_shell_fixer RUNNING→IDLE` at 00:25:13,048).

## Root Cause Chain

1. **Forced compression halts every other agent** — `compression/handler.py` line 564: `self.pool.halt_all_instances(except_instances=exempt)` before compressing. Exempt list = [compress target, its parent, all `Compressor_*`]. A concurrent sub-agent (the coder) is NOT exempt → it gets added to `pool._halted_instances`.
2. **`_is_stopped()` treats halt as terminal** — `execution_engine.py:1828-1831`:
   `return (pool.stopped or self._my_generation != pool._run_generation or inst_name in pool._halted_instances or pool.is_instance_terminated(inst_name))`
   The halted coder's `engine.run()` loop sees this and `break`s out (line 1381-1384 "halted/stopped/superseded", also 3405-3412 pre-tool check, 4184-4185 sleeping-state check).
3. **The break is permanent for that run** — `_create_and_run_agent` (execution_engine.py:4574-4576) also checks `if self._is_stopped(instance_name): break`. Once broken, the agent transitions RUNNING→IDLE and the generator finalizes. `resume_all_instances()` (handler.py:643 `finally`) clears the halt flag, but the run loop has already exited — there is NO auto-resume/continuation for a sub-agent mid-turn.
4. **Labeling**: `child_runner.py:38-41` `_check_status()` returns `was_stopped = pool.stopped or pool.is_instance_halted(instance_name)`. Halt via compression makes `was_stopped=True` → `_format_result(was_stopped=True)` prints `Execution was stopped by user.` even though NO user action occurred. (pool.stopped global stop also triggers this, but here halt alone suffices via `is_instance_halted`.)
5. **Wrong log path** — `compression/helpers.py:346-348` `extract_instance_output()`: when last message is a FUNCTION/tool result, it returns `Check log for details: {instance_name}.log` — a bare name, NOT the real path (`coder_async_shell_fixer_20260806_002042.jsonl` under `AgentWorkspace/logs/`). Cosmetic but confusing.

## Why Async Completion Looks Like the Trigger

- The async researcher's result is injected via `_drain_and_inject` → `_proactive_compression_check(..., check_label="async-drain")` (execution_engine.py:1008-1010). At 96.8% context this fires `_force_compression` → halt_all_instances. The "async agent finishing" event is the *occasion*, not the *cause* — any post-tool/async-drain compression at high context halts concurrent agents.
- In this incident the researcher (`bug_investigator_2`) was itself an async child of the orchestrator; its drain ran on the shared pool and halted the concurrently-running sync coder child.

## Secondary Contributing Factor

`_my_generation` note at execution_engine.py:1087-1092: shared engine attribute can be overwritten by sub-agents; defense-in-depth relies on `pool.stopped`/halt sets. Halt sets are pool-global (not per-engine), so the halt signal correctly reaches all engines — the flaw is that halt is treated as *terminal* rather than *suspendable*.

## Fix Directions (not yet implemented)

1. Forced compression should use the **pause** mechanism (cooperative wait, `pool.is_paused()`/`wait_if_paused()` at execution_engine.py:3404-3407) instead of `halt_all_instances`, OR
2. Halted-by-compression agents should auto-resume mid-turn (track `_compression_halted` membership in `_is_stopped()` — only break for manual halt/terminate, not compression halt), OR
3. Exclude active sub-agents from the halt list and serialize compression via a pool-level compression lock.
4. Fix `helpers.py:348` to emit the real log path (use `pool.get_logger(instance_name, agent_class).log_path`).

## Evidence

- Logs: `AgentCascade/logs/console.log` lines ~7610-7627 (compression trigger → EXIT async_shell_fixer → Compressor_1 → SLOT_SYNC_REACQUIRE)
- `agent_cascade/compression/handler.py:556-564, 642-643`
- `agent_cascade/execution_engine.py:1828-1831, 1381-1384, 3404-3412, 4574-4576, 1008-1010`
- `agent_cascade/child_runner.py:32-41, 98-107`
- `agent_cascade/compression/helpers.py:314-358`