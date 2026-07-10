# Agent Cascade

Agent Cascade is a modular, multi-agent system for complex operations, designed for maximum resilience and self-improvement.
The goal is to create a system that can operate autonomously for extended periods, learning from its mistakes and continuously improving itself.
It uses a modular, multi-agent architecture with a unique supervisor-worker dynamic that enables rapid adaptation and recovery from errors.

# Capabilities

- **Rollback on loops** - detect repeating patterns and roll back to a previous state. Overseer agent will get pinged to check why agents are looping and take action - including dismissing the misbehaving agent if necessary, with a notification.
- **Full memory persistence** - Agent logs are continuously written to a file and can be restored to any point in time.
- **Message queuing** - Agents can receive new messages while working on another task, and will process them in order.
- **Smart Truncation** - The system monitors incoming tool responses and truncates them based on user defined limits (nr of characters or tokens) to prevent overloading the context window. Spillover files are provided with full content.
- **Active Self-Improvement** - The Overseer agent checks working agents performance regularly, evaluates the system performance and suggests improvements to the prompts, configuration and even the framework itself (including the very prompts and configuration). All configurations and prompts are stored in the DNA directory, with plans to expand to multiple versions for A/B testing. Overseer will handle tracking and performance evaluation if different configs. We aim for most tasks completed successfully with least amount of token usage.

# TODO:

[ ] Add skills (custom agent loading)
[ ] Add an Overseer agent that periodically checks on the health of the system, reads logs and telemetry, check if running agents got stuck in undetectable loops or migrated goals towards something that the user never asked for, suggests fixes and improvements into a suggestion box. Main agent will pull from the suggestion box during idle times when user is AFK to self improve the agents or the framework during our daily operation - do the whole DNA A/B testing thing. Overseer agent will always get its full working queue compressed when it finishes and save it into the suggestion box (no chat messages) - should be persistent across sessions. We'll set the interval at which it activates, it will silently interrupt running agents when it activates and resume them like it never happens when its done (unless it decides to kill an agent), or work in parallel using a different API endpoint. - big task, will do it after we stabilize the framework
[ ] need a memory consolidation task ran periodically - takes all summaries in log and arranges them in a neat continuous package like long term memory -> replaces last summary
[x] warn agents about turn limit at 90%, and a final warning one when it has only 1 turn left to wrap it up. — FIXED: added turn limit warnings in execution_engine.py using `_append_system_notification` with guard prefix dedup; warns at 90% (turns_available == 10% of max_turns) and on the final remaining turn, appending to both `messages` and `llm_messages` lists
[ ] implement async shell_cmd launch (immediate tool response that it was launched, runs in background while agent is running and return final output as user message when done)
[ ] make cmd_shell pop open a console window in the back so the user can inspect or interact with it if needed.
[x] improve list_dir tool — FIXED (3ec490c): added recursive listing, glob filtering (include/exclude), sorting (name/size/date/type), human-readable sizes, timestamps, summary stats, max_entries cap, symlink cycle detection
[x] add a banner above the user chat entry that shows queued messages (with an X to dismiss each one individually) — FIXED: backend exposes queued_messages list in state payload, new dismiss_queue_message() method with thread-safe _queue_lock, WebSocket handler for individual/clear-all dismiss, frontend banner with live rendering and per-message ✕ buttons
[x] change USE_PREV_ARG system to an argument and (certain) tool output caching system — FIXED: new ArgumentCachePool class with rolling deque buffer, CacheEntry dataclass, per-instance scope, thread-safe operations, all tool args cached + outputs > 1000 chars cached, {USE_CACHED_ENTRY_N} resolution via shared resolve_cached_entry_refs() function, system_info displays cache pool state, toggle on/off via PoolSettings (cache_pool_enabled/cache_pool_size/cache_threshold_chars), config handlers for live updates, backward compatible with __USE_PREV_ARG__
[ ] document cache system info in SYSTEM_DOCS.md
[ ] add nr of times cached entries were used to telemetry info
[x] add `delete_and_insert` match_mode to edit_file tool: the `old_content` argument takes a python range `start:end` (but start with 1) that will be deleted before the new content is inserted at position `start`. leaving `new_content` empty will just delete that line range, providing just `start` in range will be pure insert of `new_content`. range can go negative, a start of -1 will insert at tail-1, 0 will append at the end, 1 will insert at start.
[x] add `shift` mode to re_indet tool, a mode where we just add or remove indent units from the start of the line. (the old `shit` mode will be renamed to `min`)
[ ] add auto-rollback feature on edit_file fail
[ ] store all agent instance IDs called by current agent so when dismissed, dismiss all the agents it called too. (it should result in a tree of dismissals recursively dismissing the whole branch)
[x] change read_logs argument to use `range` in the same indexing style as other tools like edit_file — FIXED: replaced start_index/nr_of_entries/last_n_messages with unified `range` string parameter (1-indexed, inclusive, e.g. "1:10", "5:", ":20", negative indices). Added _parse_range() static method. Updated dna.py metadata and audit documentation.
 
# BUGS:

- [ ] no agent tab refresh during tool call streaming
- [x] retry is broken, it duplicates the user message — FIXED (066b7db): reordered snapshot rollback before trimming
- [x] max tokens does not change when a new API endpoint is acquired 
- [x] make llm sampler options toggleable per entry (add a toggle on the right side of each one); add custom sampling toggle per API endpoint; move vision enabled per API endpoint — DONE: added use_custom_sampling flag, all 8 sampler params to dataclass + UI, vision toggle in header, collapsible sampling section
- [x] we have about 10-15% discrepancy (less) between the nr of tokens we measure and the actual count that LMStudio processes — FIXED: reasoning_content now always counted, all magic numbers centralized in settings.py 
- [ ] `Terminate` doesn't really terminate the agent properly, it keeps streaming, sometime left as an unreachable background thread.
- [ ] session loading sometime merges the old session with the new (mostly on server restart). should properly clean old session on load, just like it does a new session then loads.
- [x] agent tab needs refresh when switching to it from another — FIXED: invalidate panel contentKey/lastRenderedCount cache in switchMainTab() to force re-render on tab switch
- [ ] manually asking for security agent opinion does not fill it in and stop the security agent once it reached conclusion
- [ ] telemetry `Avg TPS` is wrongly calculated, `Output Tokens (est)` also most likely undercounts
- [ ] `REJECTED BY USER: SECURITY REJECTED:` is pre-pended to rejection messages when Security rejects it. it should properly distinguish when User or Security rejected it.
- [ ] call_agent returns `[SYSTEM ERROR: Empty LLM response]` if the agent failed a inner loop check
- [ ] losing connection drops sub-agent back to caller instead of retrying connection or fallback to other API endpoints
- [ ] if call_agent was initiated with custom max_turns argument, append that info to the context field in the request to the called agent. also, add a 50% turn limit warning (similar to the 90% one) and change the last final turn warning from an in-message insertion to a separate user message insert
- [ ] weird UI issue with Auto-Ask toggle, seems to reset when i click it so i need multiple clicks to get it to what i want.

# Errors to investigate:

# noisy fallback
2026-07-07 05:49:55,830 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:49:57,369 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:49:58,368 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:50:00,142 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:50:00,929 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 27985, Available tokens: 108931
2026-07-07 05:50:14,643 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:50:44,849 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 29929, Available tokens: 108931
2026-07-07 05:50:45,150 - oai.py - 315 - INFO - LLM infrastructure changed. Re-detecting context for: https://opencode.ai/zen/v1
2026-07-07 05:50:45,614 - oai.py - 280 - DEBUG - Could not identify a target model in https://opencode.ai/zen/v1/models for context length detection.
2026-07-07 05:50:45,615 - oai.py - 77 - DEBUG - [CACHE] MISS creating new client key=('https://opencode.ai/zen/v1', 'sk-6yjmx8gEYAi0Cv0ShnQyfpm7x9ntBavdr0GW6kTPyyFalWIegs4FpI1D4RW9Ayxe')
2026-07-07 05:50:46,579 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:50:51,435 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:01,767 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:01,769 - log.py - 41 - WARNING - [APIRouter] Endpoint 'Hy3-free' @ https://opencode.ai/zen/v1 attempt 1/2: Maximum number of retries (2) exceeded.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 978, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 920, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 523, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 383, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 507, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1017, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1055, in _raise_or_delay
    raise ModelServiceError(exception=Exception(f'Maximum number of retries ({max_retries}) exceeded.')) from None
agent_cascade.llm.base.ModelServiceError: Maximum number of retries (2) exceeded.
[APIRouter] Endpoint 'Hy3-free' @ https://opencode.ai/zen/v1 attempt 1/2: Maximum number of retries (2) exceeded.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 978, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 920, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 523, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 383, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 507, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1017, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1055, in _raise_or_delay
    raise ModelServiceError(exception=Exception(f'Maximum number of retries ({max_retries}) exceeded.')) from None
agent_cascade.llm.base.ModelServiceError: Maximum number of retries (2) exceeded.

2026-07-07 05:51:02,865 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 29929, Available tokens: 108931
2026-07-07 05:51:03,411 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:08,302 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:09,615 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:51:17,946 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:17,947 - log.py - 41 - WARNING - [APIRouter] Endpoint 'Hy3-free' @ https://opencode.ai/zen/v1 attempt 2/2: Maximum number of retries (2) exceeded.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 978, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 920, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 523, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 383, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 507, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1017, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1055, in _raise_or_delay
    raise ModelServiceError(exception=Exception(f'Maximum number of retries ({max_retries}) exceeded.')) from None
agent_cascade.llm.base.ModelServiceError: Maximum number of retries (2) exceeded.
[APIRouter] Endpoint 'Hy3-free' @ https://opencode.ai/zen/v1 attempt 2/2: Maximum number of retries (2) exceeded.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 978, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 920, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 523, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 383, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 507, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1017, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1055, in _raise_or_delay
    raise ModelServiceError(exception=Exception(f'Maximum number of retries ({max_retries}) exceeded.')) from None
agent_cascade.llm.base.ModelServiceError: Maximum number of retries (2) exceeded.

2026-07-07 05:51:17,964 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 29929, Available tokens: 108931
2026-07-07 05:51:18,257 - oai.py - 315 - INFO - LLM infrastructure changed. Re-detecting context for: http://127.0.0.1:1234/v1
2026-07-07 05:51:18,278 - oai.py - 257 - DEBUG - Missing context metadata in list. Trying specific endpoint: http://127.0.0.1:1234/v1/models/qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-07 05:51:18,294 - oai.py - 278 - INFO - Model qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved found, but could not detect context length via API.
2026-07-07 05:51:59,520 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 31788, Available tokens: 108931
2026-07-07 05:52:25,720 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 33429, Available tokens: 108931
2026-07-07 05:52:43,135 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 34803, Available tokens: 108931

# Agents terminated without final result on turn limit reach
[Agent 'RegressionTester' Completed]:
WARNING: Sub-agent RegressionTester terminated with a tool result (no final text output). Check log for details: RegressionTester.log



# compression error - stack desync again?
2026-07-10 06:19:34,043 - agent_pool.py - 538 - DEBUG - Instance conversation cleanup key missing (expected): 'Security_op_dddc5d10'
2026-07-10 06:19:34,047 - agent_pool.py - 2376 - INFO - [idle_checker] Auto-dismissed 1 idle agent(s): Security_op_dddc5d10
2026-07-10 06:19:37,208 - execution_engine.py - 1004 - DEBUG - EXIT - Compressor_2 RUNNING→IDLE
2026-07-10 06:19:37,426 - handler.py - 843 - INFO - /compress applied for Maine: ERROR: Compression marker would be inserted before a FUNCTION response at position 228 — pool/active-set desync detected. Discard count=225, active_start_idx=3, history_len=457
2026-07-10 06:19:37,426 - handler.py - 311 - DEBUG - Logger sync after /compress command for 'Maine': pool_len=457, using reset_history() for full sync
2026-07-10 06:19:37,452 - agent_instance_logger.py - 677 - INFO - Synced compression marker in n:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260710_043633.jsonl (539 messages).
2026-07-10 06:19:37,478 - execution_engine.py - 1557 - DEBUG - Rebuilt working sets for Maine: messages=457, llm_messages=456
2026-07-10 06:19:37,574 - execution_engine.py - 1369 - DEBUG - [PRE_LLM] Compress command handled for Maine
2026-07-10 06:19:37,575 - execution_engine.py - 807 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
2026-07-10 06:19:37,847 - base.py - 953 - INFO - Agent [Orchestrator] - ALL tokens: 92233, Available tokens: 108209
2026-07-10 06:21:24,537 - code_interpreter.py - 217 - WARNING - Code interpreter watchdog: Kernel ci_Maine_775997_5748 inactive for 300s. Killing container.
2026-07-10 06:23:37,765 - base.py - 953 - INFO - Agent [Orchestrator] - ALL tokens: 92631, Available tokens: 108209
...
2026-07-10 06:29:29,371 - lifecycle_manager.py - 176 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for Compressor_3
2026-07-10 06:29:29,404 - execution_engine.py - 645 - DEBUG - engine.run() ENTRY - instance=Compressor_3
2026-07-10 06:29:29,405 - execution_engine.py - 706 - DEBUG - [TURN_START] Calling _setup_turn for Compressor_3
2026-07-10 06:29:29,405 - execution_engine.py - 1076 - INFO - [CACHE_REBUILD] Rebuilding working set for Compressor_3 (conv_len=2)
2026-07-10 06:29:29,406 - execution_engine.py - 1159 - DEBUG - [CACHE_REBUILD] System prompt content CHANGED for Compressor_3
2026-07-10 06:29:29,410 - agent_instance_logger.py - 458 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\compressor_Compressor_3_20260710_062929.jsonl with 2 messages.
2026-07-10 06:29:29,412 - execution_engine.py - 741 - DEBUG - [TURN_DONE] Got messages=2, llm_messages=2
2026-07-10 06:29:29,463 - base.py - 953 - INFO - Agent [Compressor] - ALL tokens: 59934, Available tokens: 124493
2026-07-10 06:30:29,956 - execution_engine.py - 1004 - DEBUG - EXIT - Compressor_3 RUNNING→IDLE
2026-07-10 06:30:30,212 - handler.py - 843 - INFO - /compress applied for Maine: ERROR: Compression marker would be inserted before a FUNCTION response at position 228 — pool/active-set desync detected. Discard count=225, active_start_idx=3, history_len=493
2026-07-10 06:30:30,213 - handler.py - 311 - DEBUG - Logger sync after /compress command for 'Maine': pool_len=493, using reset_history() for full sync
2026-07-10 06:30:30,246 - agent_instance_logger.py - 677 - INFO - Synced compression marker in n:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260710_043633.jsonl (575 messages).
2026-07-10 06:30:30,282 - execution_engine.py - 1557 - DEBUG - Rebuilt working sets for Maine: messages=493, llm_messages=492
2026-07-10 06:30:30,405 - execution_engine.py - 1369 - DEBUG - [PRE_LLM] Compress command handled for Maine
2026-07-10 06:30:30,405 - execution_engine.py - 807 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
2026-07-10 06:30:30,556 - handler.py - 463 - INFO - Context usage at 97.1% for Maine — forcing compression (attempt #1).
2026-07-10 06:30:30,779 - lifecycle_manager.py - 176 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for Compressor_4
2026-07-10 06:30:30,804 - execution_engine.py - 645 - DEBUG - engine.run() ENTRY - instance=Compressor_4
2026-07-10 06:30:30,804 - execution_engine.py - 706 - DEBUG - [TURN_START] Calling _setup_turn for Compressor_4
2026-07-10 06:30:30,805 - execution_engine.py - 1076 - INFO - [CACHE_REBUILD] Rebuilding working set for Compressor_4 (conv_len=2)
2026-07-10 06:30:30,806 - execution_engine.py - 1159 - DEBUG - [CACHE_REBUILD] System prompt content CHANGED for Compressor_4
2026-07-10 06:30:30,822 - agent_instance_logger.py - 458 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\compressor_Compressor_4_20260710_063030.jsonl with 2 messages.
2026-07-10 06:30:30,823 - execution_engine.py - 741 - DEBUG - [TURN_DONE] Got messages=2, llm_messages=2
2026-07-10 06:30:30,873 - base.py - 953 - INFO - Agent [Compressor] - ALL tokens: 59934, Available tokens: 124493
2026-07-10 06:31:22,721 - execution_engine.py - 1004 - DEBUG - EXIT - Compressor_4 RUNNING→IDLE
2026-07-10 06:31:23,318 - handler.py - 530 - ERROR - Forced compression failed for Maine: Compression marker would be inserted before a FUNCTION response at position 228 — pool/active-set desync detected. Discard count=225, active_start_idx=3, history_len=494
2026-07-10 06:31:23,320 - handler.py - 157 - INFO - Compression notification queued for injection into 'Maine'
2026-07-10 06:31:23,376 - base.py - 953 - INFO - Agent [Orchestrator] - ALL tokens: 104937, Available tokens: 108209


