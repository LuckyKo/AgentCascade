# Lessons Learned — Phase 1 Unified Architecture Migration

## File Locations (absolute paths)
- `N:\work\WD\AgentCascade_unified\agent_cascade\agent_instance.py` — AgentInstance, CompressResult, LoopDetectedError, PoolSettings
- `N:\work\WD\AgentCascade_unified\agent_cascade\agent_pool.py` — Lean AgentPool (replaces old god-object at project root)
- `N:\work\WD\AgentCascade_unified\agent_cascade\execution_engine.py` — ExecutionEngine skeleton with phase-based run() loop
- `N:\work\WD\AgentCascade_unified\agent_cascade\__init__.py` — Package exports

## Key Design Decisions
1. **All fields in AgentInstance are mandatory** — the design doc says "Every field is mandatory". `create_instance()` is the factory that sets them properly. Don't construct AgentInstance directly.
2. **Halt state lives on the pool**, not on AgentInstance — single source of truth across threads
3. **_compression_halted set is separate from _halted_instances** — this preserves manual halts during compression cycles (resume_all_instances only clears compression-halted, not manual)
4. **terminated_instances set tracks termination intent** — consumed by ExecutionEngine._post_turn_checks when implemented

## What Works Now
- AgentPool can be instantiated and will discover agents from *_soul.md files
- Instance lifecycle: create_instance, get_instance, remove_instance, dismiss_instance, terminate_instance
- Halt/resume: halt_instance, resume_instance, is_instance_halted, halt_all_instances, resume_all_instances
- Message queues: send_message, enqueue_message, drain_queue, has_messages
- Conversation management: add_message, find_last_marker, surgical_rollback
- Activity tracking: _mark_activity

## What Needs Phase 2 Implementation
- ExecutionEngine phase methods are all stubs (_setup_turn, _pre_llm_checks, _call_llm_with_injection, _process_response, _post_turn_checks)
- ParallelAgentManager.submit_task returns error string (parallel not available yet)
- LoggerManager and IdleManager are placeholder classes
- _create_and_run_agent is a stub

## Important Integration Points for Phase 2
- ExecutionEngine._post_turn_checks should check `self.pool.terminated_instances` per old orchestrator pattern (agent_orchestrator.py lines 2181-2182)
- When implementing _handle_call_agent, the sync path goes through _create_and_run_agent → engine.run(inst)
- The pool reference in ExecutionEngine is needed for: halt checks, tool delegation, agent creation — it's "stateless" only in turn-level state

## Review History
- Review 1: FAIL (3 critical bugs — _discover_agents crash, NotImplementedError on parallel, missing _create_and_run_agent)
- Review 2: NEEDS WORK (5 major behavioral regressions — terminate_instance semantics, _compression_halted tracking, dismiss_instance missing)
- Review 3: PASS (minor nits only — fixed default_factory and docstring qualification)