# Migration Phase 7 — Critical Missing Features

## Summary of Changes
Migrated 7 critical/high/medium priority features from old code (agent_orchestrator.py, agent_logger.py) to the new unified architecture. **Key design principle: no more "sub-agent" or "orchestrator" terminology** — all agents are equal in the new architecture.

## Files Created
- `agent_cascade/logger/__init__.py` — Module init importing AgentInstanceLogger
- `agent_cascade/logger/agent_instance_logger.py` — Full logger implementation ported from agent_logger.py

## Files Modified
- `agent_cascade/agent_pool.py` — LoggerManager now creates real AgentInstanceLogger instances; removed NoOpLogger and unused warnings import
- `agent_cascade/execution_engine.py` — All 7 priorities implemented

## Priority Details

### P1: LoggerManager (CRITICAL) ✅
**Problem:** LoggerManager returned NoOpLogger, so session persistence was completely broken.
**Solution:** 
- Created `AgentInstanceLogger` class in `agent_cascade/logger/agent_instance_logger.py`
- Updated `LoggerManager.get_logger()` to instantiate real loggers with a `<workspace>/logs/` directory
- Added `log_message()` calls in `_process_response()` after messages are appended
- Added logging of initial system+task messages in `_create_and_run_agent()`

### P2: Recursive Self-Call Cloning (HIGH) ✅
**Problem:** An agent calling itself via call_agent would corrupt state.
**Solution:** In `_handle_call_agent()`, check `active_stack` — if the target name already exists on the stack, clone with `{name}_child{count}` suffix.

### P3: Disabled Tools Propagation (HIGH) ✅
**Problem:** When an orchestrator disabled a tool, sub-agents still had it enabled.
**Solution:** In `_create_and_run_agent()`, read `disabled_tools` from caller's template LLM config and propagate to the new agent's template LLM config. Variable renamed from `orchestrator_disabled` → `caller_disabled_tools`.

### P4: Gemma Thought Tag Normalization (HIGH) ✅
**Problem:** Gemma models output `<|channel>thought` tags that pollute conversation history.
**Solution:** In `_process_response()`, detect and extract Gemma thought tags into `reasoning_content`, strip from content, and clean function call arguments of thinking blocks.

### P5: Class Mismatch Detection (HIGH) ✅
**Problem:** Requesting an existing instance with a different agent_class would silently mix contexts.
**Solution:** In `_handle_call_agent()`, compare requested class with existing via `pool.instance_classes`. If mismatched, clear the conversation and reuse the existing template.

### P6: Settings Propagation (HIGH) ✅
**Problem:** Sub-agents didn't inherit max_turns or max_input_tokens from their caller.
**Solution:** In `_create_and_run_agent()`, propagate `max_turns` and `max_input_tokens` from caller's template config to the new agent's instance/template.

### P7: System Prompt Injection (MEDIUM) ✅
**Problem:** Root agent didn't get session metadata, available resources, or argument reuse instructions injected into its system prompt.
**Solution:** In `_setup_turn()`, if `instance.parent_instance is None` (root agent), inject:
1. Identity line update
2. Session Metadata section (working dir, log path, extra paths)
3. Available agent types and enabled tools list
4. Argument Reuse instructions

## Important Design Decisions
- **P7 uses `parent_instance is None`** instead of `agent_class == 'orchestrator'` — any root agent gets system prompt injection, not just a specific class
- **All propagation is best-effort** with try/except — failures should never break agent creation or execution
- **Logging is also best-effort** — exceptions in log_message are silently caught

## Terminology Cleanup
Removed all "sub-agent" and "orchestrator" references from comments, docstrings, and variable names. In the new architecture:
- Root agent = first agent created (parent_instance is None)
- Other agents = agents with a parent_instance set
- No hierarchy — just delegation chains