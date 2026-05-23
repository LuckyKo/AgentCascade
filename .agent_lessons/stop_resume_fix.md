# Lessons: Stop/Resume Fix

## Bug 1: Stop button doesn't halt running operations (code_interpreter, shell_cmd) ✅ FIXED
- Root cause: `agent_pool.stopped` and `agent_pool.is_halted()` checks only happened at the top of each LLM turn loop iteration in agent_orchestrator.py (lines 1043-1053). Once a tool started executing via `_call_tool` (line 1420), there was no check for stopped/halted state during execution.
- Fix: Added stop/halt checks at 3 strategic points:
  1. **Before any tool execution** (line ~1307): After yielding the tool call request, before entering either the streaming or synchronous tool path. Checks both `stopped` and `is_halted()`.
  2. **Race condition guard for dict tool_args** (line ~1434): Right before `_call_tool` in the dict branch, after argument resolution.
  3. **Race condition guard for non-dict tool_args** (line ~1467): Same guard for the else branch.
  4. **Parallel sub-agent task_wrapper** (line ~318): Added `is_halted(instance_name)` check alongside existing `stopped` check.

## Bug 2: Resume button doesn't restart halted processes ✅ FIXED
- Root cause: `resume_instance()` only set `_instance_halted[instance_name] = False`. The agent's main loop exited when halt was detected, and no mechanism existed to restart generation from where it left off.
- Fix: Added a new WebSocket message type "resume" in api_server.py (line ~1833) that:
  1. Checks if the instance was actually halted before triggering regeneration
  2. For main session + halted + generating: signals stop first, waits 0.1s via `asyncio.sleep`, then restarts with continuation message
  3. For main session + halted + idle: injects continuation message and starts new generation thread
  4. For sub-agents + halted: injects continuation message into their queue for next orchestrator call
  5. For non-halted instances: graceful no-op (just broadcasts state update)

## Thread Safety Fix
- Added `self._halt_lock = threading.Lock()` in agent_pool.py to protect `_instance_halted` dict
- All three methods (`is_halted`, `halt_instance`, `resume_instance`) now use the lock
- Fixed `halt_all_instances` to use `self.is_halted(inst)` instead of direct dict access

## Architecture notes:
- Stop handler in api_server.py:1827 sets `session['stop_requested'] = True` and `agent_pool.stopped = True`
- Halt handler in api_server.py:1538 calls `agent_pool.halt_instance(instance_name)`
- The agent pool uses `_stopped_event` (threading.Event) for cross-thread visibility of stop state
- Per-instance halt now uses `_instance_halted` dict with `_halt_lock` for thread safety
- The generation thread runs `agent_runner.run()` which yields responses in a loop
- `session['generating']` is set to True before the thread starts, False in the finally block
- The `generation_id` increment prevents stale threads from appending their results