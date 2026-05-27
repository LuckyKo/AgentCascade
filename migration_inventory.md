# Migration Inventory — Old Code → New Unified Architecture

> Generated: 2026-05-28  
> Scope: `agent_orchestrator.py`, `agent_pool.py` (old root-level), `agent_logger.py` → `agent_cascade/agent_pool.py`, `agent_cascade/execution_engine.py`, `agent_cascade/api_integration.py`

---

## 1. ALREADY MIGRATED

Features confirmed to exist in the new unified code:

| Feature | Old Location | New Location | Notes |
|---------|-------------|--------------|-------|
| AgentPool core lifecycle | `agent_pool.py:30-150` | `agent_cascade/agent_pool.py:197-333` | create_instance, get_instance, remove_instance, dismiss_instance, terminate_instance |
| Stop flag (threading.Event) | `agent_pool.py:88-158` | `agent_cascade/agent_pool.py:268-293` | Event-backed stopped property |
| Per-instance halt/resume | `agent_pool.py:177-216` | `agent_cascade/agent_pool.py:360-385, 862-872` | halt_instance, resume_instance, is_instance_halted, halt_all_instances, resume_all_instances |
| _compression_halted tracking | `agent_pool.py:100-102` | `agent_cascade/agent_pool.py:360-379` | Separate set preserves manual halts |
| Active stack management | `agent_pool.py:81` | `agent_cascade/agent_pool.py:458-491` | active_stack property + mutation methods (append, remove, clear, pop_at) |
| Message queue routing | `agent_pool.py:262-301` | `agent_cascade/agent_pool.py:843-858` | send_message, enqueue_message, drain_queue, has_messages |
| Conversation management | `agent_pool.py:585-663` | `agent_cascade/agent_pool.py:793-839` | get_conversation, slice_history_for_llm, find_last_marker, get_compression_target_set |
| Surgical rollback | `agent_pool.py:353-403` | `agent_cascade/agent_pool.py:904-971` | Safety caps, FUNCTION boundary refinement, logger truncation |
| Snapshot capture/rollback | `agent_pool.py:333-352` | `agent_cascade/agent_pool.py:506-531` | capture_snapshots, rollback_to_snapshots |
| Agent discovery | `agent_pool.py:430-445` | `agent_cascade/agent_pool.py:988-1012` | _discover_agents from *_soul.md |
| Agent loading | `agent_pool.py:491-512` | `agent_cascade/agent_pool.py:711-732` | get_agent, load_agent |
| List agents / reset | `agent_pool.py:581-692` | `agent_cascade/agent_pool.py:418-446` | list_agents, reset |
| Tool arg caching (__USE_PREV_ARG__) | `agent_pool.py:83-85` | `agent_cascade/agent_pool.py:257` | last_tool_args dict |
| Instance summaries | `agent_pool.py:74-75` | `agent_cascade/agent_pool.py:258` | instance_summaries dict |
| ParallelAgentManager | `agent_orchestrator.py:268-351` | `agent_cascade/agent_pool.py:1017-1088` | has_active_tasks, count_by_class, submit_task |
| Loop detection algorithm | `agent_orchestrator.py:76-180` | `agent_cascade/execution_engine.py:852-920` | _detect_loop with feature extraction and pattern matching |
| Forced compression (>95%) | `agent_orchestrator.py:759-836` | `agent_cascade/execution_engine.py:198-260` | _force_compression with halt/resume and rebuild_working_set |
| Compression warning (>85%) | `agent_orchestrator.py:838-848` | `agent_cascade/execution_engine.py:262-272` | _inject_compression_warning |
| Tool result truncation | `agent_orchestrator.py:569-753` | `agent_cascade/execution_engine.py:975-1038` | _truncate_tool_result with token budget and wild read detection |
| Auto-continue on truncation | `agent_orchestrator.py:1349-1362` | `agent_cascade/execution_engine.py:387-398` | Detect finish_reason='length' and inject continuation |
| __USE_PREV_ARG__ resolution | `agent_orchestrator.py:1398-1528` | `agent_cascade/execution_engine.py:549-563` | resolve_prev_arg_placeholders in _execute_tool |
| call_agent handling | `agent_orchestrator.py:1735-2247` | `agent_cascade/execution_engine.py:565-613` | _handle_call_agent with concurrency limits and parallel/sync paths |
| dismiss_agent handling | — | `agent_cascade/execution_engine.py:615-642` | _handle_dismiss_agent (new, was previously handled via SubAgentFunctionProxy) |
| compress_context handling | — | `agent_cascade/execution_engine.py:644-691` | _handle_compress_context |
| System prompt identity injection | `agent_orchestrator.py:927-993` | Partial in ExecutionEngine._create_and_run_agent | Session metadata injection for sub-agents |
| Sub-agent feedback extraction | `agent_orchestrator.py:2253-2290` | In agent_cascade.compression.helpers.extract_sub_agent_feedback | extract_sub_agent_feedback |
| Telemetry integration | `agent_orchestrator.py:1212-1642` (scattered) | `agent_cascade/execution_engine.py` (various) | record_llm_call_start/end, record_tool_call_start/end, record_loop_detected |
| Session restore from log | `agent_pool.py:854-1005` | `agent_cascade/agent_pool.py:533-686` | load_session_from_log with compression marker handling |
| /compress command | `agent_orchestrator.py:1017-1078` | Not in ExecutionEngine — see NEEDS MIGRATION below | Manual compression via user command |

---

## 2. NEEDS MIGRATION

### 2.1 From agent_orchestrator.py

#### 2.1.1 System Prompt Injection for Main Agent (Orchestrator)
- **Old code**: `agent_orchestrator.py:927-993` — Injects identity, session metadata, available resources/tools, and argument reuse instructions into the system prompt of the main orchestrator's first message
- **What's missing**: The new ExecutionEngine._setup_turn() does NOT inject system prompt metadata for the main agent. It only handles sub-agent system messages in _create_and_run_agent(). The main agent's system prompt is assumed to be pre-built by api_server.py.
- **Why it matters**: If the available tools or agents list changes during runtime (e.g., a new agent is loaded), the orchestrator won't see updated resource lists until restart. Also, Argument Reuse instructions aren't injected for the main agent in the new path.
- **Effort**: Small — add injection logic to _setup_turn() or create_main_agent_instance()

#### 2.1.2 /compress Manual Command Handling
- **Old code**: `agent_orchestrator.py:1017-1078` — Detects `/compress [fraction]` user command, generates preview summary, requests user approval via operation_manager.request_user_approval(), then applies compression if approved
- **What's missing**: No manual /compress command handling in ExecutionEngine._pre_llm_checks() or _setup_turn(). Users can't trigger compression via chat.
- **Why it matters**: Users lose the ability to manually request context compression with approval workflow. The compress_context tool still works, but the /compress command provides a cleaner UX with preview and approval.
- **Effort**: Small — add command detection in _setup_turn() or _pre_llm_checks()

#### 2.1.3 Monkey-Patch Compression Hook on Sub-Agent LLM Calls
- **Old code**: `agent_orchestrator.py:2000-2072` — Monkey-patches sub-agent's _call_llm to inject async message injection, compression check/force at >95%, and auto-continue on truncation during the sub-agent's LLM streaming
- **What's missing**: The new unified path in ExecutionEngine._handle_call_agent() → _execute_agent_sync() → engine.run(inst) goes through the same engine loop, which has its own _pre_llm_checks(). However, the monkey-patch pattern does additional things:
  - Async message injection during sub-agent LLM streaming (not just before LLM calls)
  - Per-sub-agent compression check that's called DURING the LLM stream (not just between turns)
  - Auto-continue on truncation WITHIN the sub-agent's LLM stream (recursive hooked_call_llm)
- **Why it matters**: Sub-agents running through the unified engine loop DO get compression checks and auto-continue from _pre_llm_checks(). However, async message injection during streaming is NOT replicated — messages are only injected before LLM calls in _pre_llm_checks(), not mid-stream. This means parallel agents finishing during a sub-agent's LLM call won't inject their results until the next turn boundary.
- **Effort**: Medium — need to add mid-stream async message injection to ExecutionEngine._call_llm_with_injection()

#### 2.1.4 Recursive Self-Call Cloning
- **Old code**: `agent_orchestrator.py:1762-1773` — When an agent calls itself (instance_name already in active_stack), the system clones its conversation into a new instance name like `{name}_child{count}` to prevent state corruption
- **What's missing**: The new _handle_call_agent() does NOT check for recursive self-calls. If an agent calls itself, it will share the same conversation and corrupt state.
- **Why it matters**: Agents that delegate to themselves (e.g., a coder asking another coder to review) will have their conversations merge, causing out-of-order UI rendering and message duplication.
- **Effort**: Small — add active_stack check in _handle_call_agent()

#### 2.1.5 Class Mismatch Detection and History Clearing
- **Old code**: `agent_orchestrator.py:1774-1783` — If an existing instance is called with a different agent_class, the system clears its history to avoid context mix-ups
- **What's missing**: The new _handle_call_agent() does NOT check for class mismatches on existing instances. An instance created as "coder" could be re-called as "researcher" without clearing history.
- **Why it matters**: Context pollution when agent_class changes mid-session. The orchestrator might inject wrong tool schemas or system messages.
- **Effort**: Small — add class comparison in _handle_call_agent()

#### 2.1.6 Multimodal Image Propagation to Sub-Agents
- **Old code**: `agent_orchestrator.py:1893-1927` — Scans manager_history for images referenced in the task text and passes them as multimodal content to the sub-agent's message
- **What's missing**: The new _create_and_run_agent() in ExecutionEngine builds a plain text task message without scanning for images. Sub-agents cannot receive image context from the orchestrator's conversation.
- **Why it matters**: If an agent asks "look at this screenshot" and delegates to another agent, the sub-agent won't see the image in the new unified path.
- **Effort**: Medium — add multimodal content scanning in _create_and_run_agent()

#### 2.1.7 Disabled Tools Propagation to Sub-Agents
- **Old code**: `agent_orchestrator.py:1970-1990` — Copies disabled_tools from orchestrator's llm.generate_cfg to sub-agent's llm.generate_cfg so UI tool restrictions are inherited
- **What's missing**: The new _create_and_run_agent() does NOT propagate disabled_tools. Sub-agents can use tools that the UI has disabled for the orchestrator.
- **Why it matters**: Security/UX — if a user disables "shell_cmd" in the UI, sub-agents should also respect that restriction. Currently they don't in the new path.
- **Effort**: Small — add disabled_tools propagation in _create_and_run_agent()

#### 2.1.8 Sub-Agent Max Turns and Auto-Continue Propagation
- **Old code**: `agent_orchestrator.py:1976-1984` — Sets sub-agent's max_turns, auto_continue_enabled, and max_input_tokens from the orchestrator's settings
- **What's missing**: The new _create_and_run_agent() creates instances with max_turns=None (default 50) and doesn't propagate auto_continue_enabled or max_input_tokens. Sub-agents use hardcoded defaults instead of inheriting from the supervisor.
- **Why it matters**: If user sets max_turns to 100 in UI, sub-agents still only get 50 turns. Auto-continue behavior may not match user expectations for sub-agents.
- **Effort**: Small — add settings propagation in _create_and_run_agent()

#### 2.1.9 WebUI Sub-Agent State Updates During Execution
- **Old code**: `agent_orchestrator.py:1956-1968` — Initializes and updates sub_agent_state dict during sub-agent execution, including state['messages'] = list(conv) + list(resp) for UI rendering
- **What's missing**: The new _create_and_run_agent() does NOT update pool.sub_agent_state during sub-agent execution. The WebUI won't show real-time sub-agent output in the new path.
- **Why it matters**: Users lose the ability to see what sub-agents are doing in real-time through the UI's sub-agent tabs.
- **Effort**: Medium — add sub_agent_state updates in _create_and_run_agent() or as a hook

#### 2.1.10 Grep Spillover Path Pre-Computation
- **Old code**: `agent_orchestrator.py:539-567` — Pre-computes spillover file path for grep results before tool execution, so operation_manager can include it in truncation notices
- **What's missing**: The new ExecutionEngine._execute_tool() does NOT pre-compute grep spillover paths. Tool calls don't pass spill_file_path to call_kwargs.
- **Why it matters**: Grep results that exceed char limit won't have the spillover file path in their truncation notice, making it harder for agents to read full grep output.
- **Effort**: Small — add _compute_grep_spill_path logic in _execute_tool()

#### 2.1.11 Post-Execution Success Detection and Telemetry Enrichment
- **Old code**: `agent_orchestrator.py:1612-1642` — Detects tool execution errors from returned strings (not just exceptions), records detailed telemetry with result_chars, truncated status, error messages
- **What's missing**: The new ExecutionEngine._process_response() does NOT do post-execution success detection. Tool results are blindly trusted as successful unless they raise an exception. Telemetry recording is minimal/missing.
- **Why it matters**: Silent tool failures go undetected. Telemetry data is incomplete, making debugging harder.
- **Effort**: Medium — add error string detection and enriched telemetry in _process_response()

#### 2.1.12 Message Pool Validation After Compression
- **Old code**: `agent_orchestrator.py:354-402` + calls at lines 1145, 1198, 1605 — validate_message_pool() checks for empty pools, invalid first messages, duplicate consecutive messages, and invalid roles after compression
- **What's missing**: The new ExecutionEngine does NOT call validate_message_pool() after forced compression or compress_context tool execution. Recovery from corrupted message pools is not implemented.
- **Why it matters**: If compression corrupts the message pool (known issue), agents will continue with corrupted state instead of attempting recovery.
- **Effort**: Medium — add validate_message_pool() calls in _force_compression() and after compress_context handling

#### 2.1.13 Logger Sync After Forced Compression
- **Old code**: `agent_orchestrator.py:1168-1176` — After forced compression, explicitly syncs the logger's internal data["history"] to match pool state
- **What's missing**: The new ExecutionEngine._force_compression() does NOT sync the logger after rebuilding working set. Logger may diverge from pool state.
- **Why it matters**: After forced compression, the JSONL log file may not reflect the compressed state, causing issues on session restore.
- **Effort**: Small — add logger update_history() call in _force_compression()

#### 2.1.14 Gemma Thought Tag Normalization
- **Old code**: `agent_orchestrator.py:1281-1331` — Normalizes Gemma-style thinking blocks (<|channel>thought) into reasoning_content field, strips thinking blocks from function call arguments
- **What's missing**: The new ExecutionEngine._process_response() only does basic thinking block stripping. It doesn't handle Gemma-specific tags or clean function call arguments.
- **Why it matters**: Gemma models' output will pollute history with raw thought tags in the new path. Function call arguments may contain thinking tags that confuse parsers.
- **Effort**: Small — add Gemma tag normalization and FC arg cleaning in _process_response()

#### 2.1.15 Turn Budget Restoration on Forced Compression
- **Old code**: `agent_orchestrator.py:1138-1140` — When forced compression runs, the turn budget is restored (num_llm_calls_available += 1) since no LLM call was actually made
- **What's missing**: The new ExecutionEngine._pre_llm_checks() returns True to continue the loop, but turns_available has already been decremented at line 78. Need to verify this is handled.
- **Why it matters**: If turns_available isn't restored after forced compression, the agent loses a turn for nothing.
- **Effort**: Small — check and fix if needed (may already be correct since _pre_llm_checks runs BEFORE the decrement at line 78)

#### 2.1.16 Active Function Injection During LLM Streaming
- **Old code**: `agent_orchestrator.py:1224-1255` — Gets active_functions via _get_active_functions() and passes to _call_llm during streaming, with telemetry recording of first token time
- **What's missing**: The new ExecutionEngine._call_llm_with_injection() gets active_functions from template._get_active_functions(), but doesn't have the same telemetry integration (first token recording, input/output token estimation).
- **Why it matters**: Telemetry data is less granular in the new path. First-token latency isn't tracked.
- **Effort**: Small — add telemetry hooks in _call_llm_with_injection()

### 2.2 From agent_pool.py (old)

#### 2.2.1 Idle Agent Auto-Dismissal System
- **Old code**: `agent_pool.py:694-848` — Full idle detection system with background thread, configurable timeout/interval, _mark_activity, _get_idle_seconds, _is_agent_idle, _idle_checker_loop, _auto_dismiss_idle_agent
- **What's missing**: The new AgentPool has a TODO comment at line 1015: "TODO: Implement IdleManager for idle detection and auto-dismissal (Phase 2)". The new pool has _mark_activity() but no background checker thread or auto-dismissal logic.
- **Why it matters**: Sub-agents that go idle will never be cleaned up, leading to memory leaks and stale instances in the pool. The old system auto-dismissed agents after 5 minutes of inactivity.
- **Effort**: Large — requires implementing IdleManager with background thread, activity tracking, and safe cleanup

#### 2.2.2 Instance Discovery (Session Recovery from Logs)
- **Old code**: `agent_pool.py:217-258` — discover_instances() scans the logs directory for *.jsonl files and recovers existing sessions on startup
- **What's missing**: The new AgentPool does NOT have a discover_instances() method. Sessions are not recovered from log files on startup.
- **Why it matters**: After a restart, all sub-agent sessions are lost. Users lose their work context and have to manually restore sessions.
- **Effort**: Medium — add discover_instances() that scans logs and calls load_session_from_log

#### 2.2.3 update_llm_cfg Propagation
- **Old code**: `agent_pool.py:514-579` — Updates global LLM config and propagates to all loaded agents, respecting APIRouter specialized routing (skips infrastructure override for agents with custom endpoints)
- **What's missing**: The new AgentPool does NOT have update_llm_cfg(). When UI settings change, the old code would propagate them to all agents. The new pool has no equivalent.
- **Why it matters**: UI setting changes (temperature, max_tokens, etc.) won't be propagated to sub-agents. Agents with specialized routing might get their endpoints overwritten.
- **Effort**: Medium — add update_llm_cfg() with APIRouter awareness

#### 2.2.4 _cleanup_history (Deduplication)
- **Old code**: `agent_pool.py:1007-1089` — Ultra-robust deduplicator that prunes adjacent identical messages, detects echo duplication around compression markers, and removes repeated sequences
- **What's missing**: The new AgentPool does NOT have _cleanup_history(). Session restore from log doesn't include this cleanup step.
- **Why it matters**: After session restore, conversations may contain duplicate messages that confuse the LLM or waste context window.
- **Effort**: Medium — add _cleanup_history() and call it in load_session_from_log

#### 2.2.5 get_agent_info
- **Old code**: `agent_pool.py:1091-1102` — Returns agent info (name, tagline, tools, description) from agent_configs
- **What's missing**: The new AgentPool does NOT have get_agent_info(). It uses templates instead of agents+agent_configs.
- **Why it matters**: The /api/agents endpoint and system prompt resource injection need this data to show available agents with descriptions.
- **Effort**: Small — add get_agent_info() that reads from template metadata

#### 2.2.6 OperationManager Initialization in Pool
- **Old code**: `agent_pool.py:53-54` — AgentPool creates its own OperationManager
- **What's missing**: The new AgentPool receives operation_manager as an injected dependency. If not provided, it may be None.
- **Why it matters**: If api_server doesn't inject operation_manager, tools that depend on it (like compress_context approval) will fail with AttributeError.
- **Effort**: Small — add defensive checks or default initialization

#### 2.2.7 APIRouter Initialization in Pool
- **Old code**: `agent_pool.py:61-64` — AgentPool creates its own APIRouter
- **What's missing**: The new AgentPool receives api_router as an injected dependency. Same issue as OperationManager.
- **Why it matters**: If not injected, LLM calls will fail with no routing/failover.
- **Effort**: Small — same defensive pattern as above

#### 2.2.8 Telemetry Initialization in Pool
- **Old code**: `agent_pool.py:57-58` — AgentPool creates its own TelemetryCollector
- **What's missing**: The new AgentPool receives telemetry as an injected dependency.
- **Why it matters**: If not injected, telemetry recording will silently fail.
- **Effort**: Small — same defensive pattern

#### 2.2.9 async_message_queue Legacy Backward Compat
- **Old code**: `agent_pool.py:110-111` + drain_queue dedup logic at lines 272-293 — Maintains a legacy global async_message_queue and deduplicates across targeted queue and global queue
- **What's missing**: The new AgentPool does NOT have async_message_queue or the dedup logic in drain_queue. It only has message_queues dict.
- **Why it matters**: If any code path still pushes to the legacy global queue, those messages will be lost.
- **Effort**: Small — add backward compat shim if needed

### 2.3 From agent_logger.py

#### 2.3.1 Full AgentInstanceLogger Implementation (CRITICAL)
- **Old code**: `agent_logger.py:18-428` — Complete JSONL logging with metadata, message formatting with timestamps, _format_message, _append_line, _initial_save, update_timestamp, log_message, insert_compression_marker, update_history (additive sync with dedup), reset_history, rollback (soft/hard), truncate_to
- **What's missing**: The new LoggerManager returns NoOpLogger instances. ALL logging is a no-op. Messages are NOT persisted to JSONL files. Compression markers are NOT inserted into logs. History is NOT synced. Rollbacks are NOT recorded.
- **Why it matters**: 
  - **Session persistence is completely broken** — if the server restarts, all conversation history is lost
  - **Compression state is not persisted** — compression markers in JSONL are needed for session restore to find the correct working set
  - **No audit trail** — no record of what agents did, when, or how long they took
  - **Log-based recovery is impossible** — load_session_from_log won't work because there are no logs
- **Effort**: Large — need to implement full LoggerManager that returns AgentInstanceLogger instances. The old AgentInstanceLogger code can likely be reused with minimal changes.

#### 2.3.2 insert_compression_marker
- **Old code**: `agent_logger.py:134-185` — Inserts compression summary marker at correct position in the log, calculated as offset from end, with debug logging and file rewrite
- **What's missing**: NoOpLogger.insert_compression_marker() is a no-op. Compression markers are never written to JSONL files.
- **Why it matters**: Without compression markers in logs, session restore can't find the working set boundary. The entire compression history is lost.
- **Effort**: Part of 2.3.1

#### 2.3.3 update_history Additive Sync with Dedup
- **Old code**: `agent_logger.py:187-307` — Complex additive sync algorithm that uses timestamp-based identity matching, detects compression events, handles surgical insertions and content updates (manual edits)
- **What's missing**: NoOpLogger.update_history() is a no-op. History changes are never synced to the log file.
- **Why it matters**: The log file diverges from in-memory state. On crash/restart, the log is stale.
- **Effort**: Part of 2.3.1

#### 2.3.4 rollback (soft/hard) and truncate_to
- **Old code**: `agent_logger.py:377-428` — Soft rollback appends ROLLBACK marker; hard rollback truncates the file. truncate_to delegates to rollback.
- **What's missing**: NoOpLogger.truncate_to() is a no-op. Rollback events are not recorded in logs.
- **Why it matters**: Loop recovery rollbacks are invisible in the log. The log may contain messages that were rolled back from memory.
- **Effort**: Part of 2.3.1

---

## 3. OBSOLETED

Old features intentionally dropped (not bugs):

| Feature | Old Location | Reason for Removal |
|---------|-------------|-------------------|
| _SubAgentFunctionProxy | `agent_orchestrator.py:249-264` | Replaced by direct handling in ExecutionEngine._handle_call_agent/dismiss_agent |
| SubAgent schemas as placeholder tools | `agent_orchestrator.py:187-244` | call_agent/dismiss_agent are now handled natively in the engine, not via function_map |
| _cleanup_history on every session load | `agent_pool.py:999-1003` (called from load_session_from_log) | Not explicitly present in new code — may have been deemed unnecessary if compression markers are reliable. **NOTE: This might be a regression, see 2.2.4** |
| _create_example_agent | `agent_pool.py:446-489` | New pool doesn't auto-create example agents (cleaner) |
| Instance-level logger storage in pool dict | Old pool stored loggers directly; new uses LoggerManager | Architectural cleanup — delegation to focused manager |

---

## 4. PARTIAL MIGRATION

### 4.1 System Prompt Injection for Sub-Agents
- **Old**: `agent_orchestrator.py:1820-1855` — Full system prompt construction with identity, session metadata (working dir, log path, extra paths), and dynamic resource injection
- **New**: `agent_cascade/execution_engine.py:722-743` — Basic system message with identity and supervisor info only. Missing: working_dir, log_path, extra_paths_ro/rw injection
- **Gap**: Sub-agents in the new path don't get full session metadata in their system prompts. They can't reference their log paths or workspace directories.

### 4.2 Tool Execution Path (Missing Key Features)
- **Old**: `agent_orchestrator.py:1384-1678` — Full tool execution with telemetry, error detection, grep spillover, wild read detection with configurable limits
- **New**: `agent_cascade/execution_engine.py:519-447` — Basic tool execution. Missing: telemetry hooks, post-execution success detection, grep spillover path passing, configurable wild read limits from llm_cfg

### 4.3 Compression Threshold Configuration
- **Old**: Hardcoded 95% force / 85% warning in agent_orchestrator.py
- **New**: Uses PoolSettings with configurable thresholds (compression_force_threshold, compression_warning_threshold)
- **Gap**: Partially better — the new approach is more flexible. However, the default values need to match (need to verify).

### 4.4 Active Stack Cleanup on Sub-Agent Exit
- **Old**: `agent_orchestrator.py:2216-2233` — Clean up active_stack in finally block with lock protection
- **New**: `agent_cascade/execution_engine.py:772-774` — Active stack cleanup in _create_and_run_agent's finally block
- **Gap**: The new code uses assignment (`self.pool._execution.active_stack = [...]`) instead of the mutation method pattern. This may cause issues if external references hold onto the old list object.

### 4.5 Parallel Agent Manager Endpoint Scheduling
- **Old**: `agent_orchestrator.py:287-351` — submit_task acquires endpoint scheduler slot before submitting to thread pool, with proper release in finally block
- **New**: `agent_cascade/agent_pool.py:1045-1088` — submit_task does NOT acquire endpoint scheduler slots. Concurrency limits are checked via count_by_class but there's no actual scheduling/blocking on endpoint capacity.
- **Gap**: Parallel agents don't respect endpoint-level concurrency limits. They can overwhelm API endpoints.

### 4.6 find_last_marker — Missing Role Check
- **Old**: `agent_pool.py:592-612` — Checks both USER role AND COMPRESSION_MARKER prefix
- **New**: `agent_cascade/agent_pool.py:889-902` — Only checks COMPRESSION_MARKER prefix, does NOT verify USER role
- **Gap**: If an assistant message happens to start with the compression marker string (unlikely but possible), it would be incorrectly identified as a compression boundary.

---

## Summary Statistics

| Category | Count |
|----------|-------|
| Already Migrated | ~30 features |
| Needs Migration | 21 items |
| Obsoleted | 5 items |
| Partial Migration | 6 items |

### Critical Items (Must Fix Before Full Cutover)
1. **LoggerManager returns NoOpLogger** — Session persistence completely broken
2. **Idle agent auto-dismissal missing** — Memory leak risk
3. **Recursive self-call cloning missing** — State corruption on self-delegation
4. **Disabled tools not propagated to sub-agents** — Security/UX issue
5. **Gemma thought tag normalization missing** — History pollution

### High Priority
6. System prompt injection for main agent (dynamic resource lists)
7. /compress manual command handling
8. Class mismatch detection on existing instances
9. Multimodal image propagation to sub-agents
10. Message pool validation after compression
11. Logger sync after forced compression
12. Sub-agent WebUI state updates
13. Endpoint scheduling for parallel agents

### Medium Priority
14. Grep spillover path pre-computation
15. Post-execution success detection and telemetry enrichment
16. Telemetry granularity (first token tracking)
17. Instance discovery from logs on startup
18. update_llm_cfg propagation
19. _cleanup_history deduplication
20. get_agent_info for /api/agents endpoint

### Low Priority
21. async_message_queue legacy backward compat