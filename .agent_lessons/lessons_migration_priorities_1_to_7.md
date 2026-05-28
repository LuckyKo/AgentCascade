# Migration Priorities 1-7 — Implementation Notes

> Date: 2026-05-28
> Agent: MigrationImplementer1
> Status: All implemented, syntax verified

---

## PRIORITY 1: LoggerManager — Replace NoOpLogger with Real Logging ✅

**Files created:**
- `agent_cascade/logger/__init__.py` — module init, imports AgentInstanceLogger
- `agent_cascade/logger/agent_instance_logger.py` — full logger implementation (ported from agent_logger.py)

**Files modified:**
- `agent_cascade/agent_pool.py` — LoggerManager now returns real AgentInstanceLogger instead of NoOpLogger. Removed NoOpLogger class and unused `warnings` import.
- `agent_cascade/execution_engine.py` — Added `log_message()` calls in `_process_response()` after messages are appended to all working sets.

**Key design decisions:**
- Logger writes to Layer 1 (JSONL file). Pool owns Layer 2 (in-memory via AgentInstance.conversation). No dual-state sync.
- Logging failures are silently ignored — they must never break the execution loop.

---

## PRIORITY 2: Recursive Self-Call Cloning ✅

**File modified:** `agent_cascade/execution_engine.py:_handle_call_agent()`

When an agent calls itself (instance_name already in active_stack), it's cloned with `{name}_child{count}` suffix. This prevents state corruption on self-delegation.

---

## PRIORITY 3: Disabled Tools Propagation to Sub-Agents ✅

**File modified:** `agent_cascade/execution_engine.py:_create_and_run_agent()`

Before running a sub-agent, the orchestrator's `disabled_tools` config is copied to the sub-agent's template LLM generate_cfg. This ensures security policies cascade through the delegation chain.

---

## PRIORITY 4: Gemma Thought Tag Normalization ✅

**File modified:** `agent_cascade/execution_engine.py:_process_response()`

Added normalization of `<|channel>thought` tags (Gemma-specific) in `_process_response()`. Also strips thinking blocks from function call arguments to prevent tag pollution.

---

## PRIORITY 5: Class Mismatch Detection ✅

**File modified:** `agent_cascade/execution_engine.py:_handle_call_agent()`

When an existing instance is called with a different agent_class, the conversation history is cleared first to prevent context mix-ups.

---

## PRIORITY 6: Sub-Agent Settings Propagation ✅

**File modified:** `agent_cascade/execution_engine.py:_create_and_run_agent()`

Before running a sub-agent, propagates:
- `max_turns` from the caller's settings
- `max_input_tokens` (context window limit) from the orchestrator's LLM config to the sub-agent's template

---

## PRIORITY 7: System Prompt Injection for Main Agent ✅

**File modified:** `agent_cascade/execution_engine.py:_setup_turn()`

When setting up the turn for an Orchestrator-class instance, injects into the system message:
1. Identity line update ("You are [instance_name].")
2. Session Metadata (supervisor, working dir, log path, extra paths)
3. Available Resources (sub-agents and enabled tools, excluding disabled_tools)
4. Argument Reuse instructions

---

## BONUS: Idle Agent Auto-Dismissal ✅

**File modified:** `agent_cascade/agent_pool.py`

Implemented full `IdleManager` class with background daemon thread:
- Checks every `idle_check_interval` seconds (default 60s)
- Dismisses agents idle for more than `idle_timeout_seconds` (default 300s = 5min)
- Never dismisses the main orchestrator, active agents, or halted agents
- Wired into AgentPool via `self._idle = IdleManager(self)` 
- Started via `AgentPool.start()` method
- Stopped when `pool.stopped = True` (via setter)
- `remove_instance()` now also cleans up the logger entry to prevent memory leaks

---

## Things to Watch For

1. **IdleManager thread safety**: The idle checker reads from `self.pool.instances.keys()` without holding a lock, then checks each instance individually. This is safe because `remove_instance` only pops after all checks pass.

2. **Logger cleanup on dismiss**: Added logger dict cleanup in `remove_instance()` — prevents stale logger instances accumulating in memory.

3. **NoOpLogger removal**: The old NoOpLogger class and its one-time warnings are gone. If anything depends on RuntimeWarnings being emitted, it will now be silent (but logging actually works).

4. **Pool.start() must be called**: The IdleManager is lazy-started via `AgentPool.start()`. If the pool is created but never started, idle checking won't happen. The api_server needs to call `pool.start()` after creating the pool.