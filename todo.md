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
[ ] warn agents about message limit at 90%
[ ] make cmd_shell pop open a console window in the back so the user can inspect or interact with it if needed.
[x] improve list_dir tool — FIXED (3ec490c): added recursive listing, glob filtering (include/exclude), sorting (name/size/date/type), human-readable sizes, timestamps, summary stats, max_entries cap, symlink cycle detection
[ ] add a banner above the user chat entry that shows queued messages (with an X to dismiss each one individually)
[ ] change USE_PREV_ARG system to an argument and (certain) tool output caching system. all tool arguments and certain outputs (like the result of a call_agent) longer than a certain threshold (line 1000 chars) get cached in a pool and can be inserted with {**USE_CACHED_ENTRY_N**} in other tool arguments. system_info will display the truncated state of the cache pool. we'll use a rolling index to overwrite old entries in the pool with new ones. the system will use a toggle on/off in settings.
[x] add `delete_and_insert` match_mode to edit_file tool: the `old_content` argument takes a python range `start:end` (but start with 1) that will be deleted before the new content is inserted at position `start`. leaving `new_content` empty will just delete that line range, providing just `start` in range will be pure insert of `new_content`. range can go negative, a start of -1 will insert at tail-1, 0 will append at the end, 1 will insert at start.
[x] add `shift` mode to re_indet tool, a mode where we just add or remove indent units from the start of the line. (the old `shit` mode will be renamed to `min`)
[ ] vision capabilities switch from global to API endpoint property (add vision toggle to each API entry). each !image insert in the messages should have an accompanying text description that the non vision model can read, a dedicated caption agent will be called to fill it if an image insert is presented and the model does not support vision.
[ ] refactor tool assignment to work in real time, enabled tools acquired for each turn from the list assigned to the specific class of agent from the UI tool assignment list.
 
# Message stack update rules:

- add message/tool response/user msg (append): agent_pool - add; logs - add; UI - add
- user history edit (edit): agent_pool - in place; logs - in place; UI - rebuild
- user history delete (edit): agent_pool - in place; logs - in place; UI - rebuild
- compression (regen): agent_pool - rebuild; logs - rebuild; UI - rebuild
- rollback (edit): agent_pool - trim tail; logs - trim tail; UI - rebuild
- retry (edit): agent_pool - trim tail; logs - trim tail; UI - rebuild
- continue (resend): agent_pool - no; logs - no; UI - no
- call_agent (new): agent_pool - new; logs - new; UI - new
- call_agent (existing): agent_pool - add; logs - add; UI - add
- call_agent return (append): agent_pool - add; logs - add; UI - add
- new session (reset): agent_pool - new; logs - new; UI - new
- session load (replace from json): agent_pool - rebuild; logs - rebuild; UI - rebuild
- server startup (load last root agent json log): : agent_pool - rebuild; logs - rebuild; UI - rebuild

# BUGS:

- [ ] Activity banner still doesn't change when tools are written
- [x] retry is broken, it duplicates the user message — FIXED (066b7db): reordered snapshot rollback before trimming
- [x] max tokens does not change when a new API endpoint is acquired 
- [x] make llm sampler options toggleable per entry (add a toggle on the right side of each one); add custom sampling toggle per API endpoint; move vision enabled per API endpoint — DONE: added use_custom_sampling flag, all 8 sampler params to dataclass + UI, vision toggle in header, collapsible sampling section
- [x] we have about 10-15% discrepancy (less) between the nr of tokens we measure and the actual count that LMStudio processes — FIXED: reasoning_content now always counted, all magic numbers centralized in settings.py 
- [x] images don't get properly pasted in chat
- [ ] add auto-rollback feature on edit_file fail
- [ ] agent tab needs refresh when switching to it from another
- [ ] manually asking for security agent opinion does not fill it in and stop the security agent once it reached conclusion
- [ ] if user unchecked auto-ask while the security agent is running the approval window pops up correctly but its stuck in a weird flashing mode and cant press any button to approve/reject

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


# compression fucked
2026-07-07 11:28:41,926 - agent_pool.py - 398 - DEBUG - Async registry executor recreated
2026-07-07 11:28:41,927 - agent_pool.py - 401 - DEBUG - Stopped flag cleared — ready for new execution
2026-07-07 11:28:41,928 - ws_handlers.py - 235 - DEBUG - Starting generation gen_id=2, instances={'Maine': 'IDLE'}, active_stack=0
2026-07-07 11:28:41,929 - agent_pool.py - 382 - DEBUG - Idle checker restarted
2026-07-07 11:28:41,929 - agent_pool.py - 398 - DEBUG - Async registry executor recreated
2026-07-07 11:28:41,929 - agent_pool.py - 401 - DEBUG - Stopped flag cleared — ready for new execution
2026-07-07 11:28:41,930 - execution_engine.py - 634 - DEBUG - engine.run() ENTRY - instance=Maine
2026-07-07 11:28:41,930 - agent_pool.py - 1793 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://127.0.0.1:1234/v1, concurrency_limit=0
2026-07-07 11:28:41,930 - execution_engine.py - 476 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-07-07 11:28:41,930 - execution_engine.py - 695 - DEBUG - [TURN_START] Calling _setup_turn for Maine
2026-07-07 11:28:41,931 - execution_engine.py - 990 - INFO - [CACHE_REBUILD] Rebuilding working set for Maine (conv_len=428)
2026-07-07 11:28:41,932 - execution_engine.py - 1068 - DEBUG - [CACHE_REBUILD] System prompt content CHANGED for Maine
2026-07-07 11:28:41,942 - agent_instance_logger.py - 458 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260707_112809.jsonl with 428 messages.
2026-07-07 11:28:41,943 - execution_engine.py - 727 - DEBUG - [TURN_DONE] Got messages=428, llm_messages=427
2026-07-07 11:28:41,962 - execution_engine.py - 781 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
2026-07-07 11:28:42,188 - lifecycle_manager.py - 176 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for Compressor_1
2026-07-07 11:28:42,256 - execution_engine.py - 634 - DEBUG - engine.run() ENTRY - instance=Compressor_1
2026-07-07 11:28:42,264 - execution_engine.py - 695 - DEBUG - [TURN_START] Calling _setup_turn for Compressor_1
2026-07-07 11:28:42,265 - execution_engine.py - 990 - INFO - [CACHE_REBUILD] Rebuilding working set for Compressor_1 (conv_len=2)
2026-07-07 11:28:42,266 - execution_engine.py - 1068 - DEBUG - [CACHE_REBUILD] System prompt content CHANGED for Compressor_1
2026-07-07 11:28:42,268 - agent_instance_logger.py - 458 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\compressor_Compressor_1_20260707_112842.jsonl with 2 messages.
2026-07-07 11:28:42,269 - execution_engine.py - 727 - DEBUG - [TURN_DONE] Got messages=2, llm_messages=2
2026-07-07 11:28:42,305 - base.py - 953 - INFO - Agent [Compressor] - ALL tokens: 40709, Available tokens: 124591
2026-07-07 11:28:42,307 - oai.py - 77 - DEBUG - [CACHE] MISS creating new client key=('http://127.0.0.1:1234/v1', 'EMPTY')
2026-07-07 11:29:31,098 - execution_engine.py - 918 - DEBUG - EXIT - Compressor_1 RUNNING→IDLE
2026-07-07 11:29:31,295 - handler.py - 816 - INFO - /compress applied for Maine: ERROR: Compression marker would be inserted before a FUNCTION response at position 228 — pool/active-set desync detected. Discard count=225, active_start_idx=3, history_len=429
2026-07-07 11:29:31,295 - handler.py - 290 - DEBUG - Logger sync after /compress command for 'Maine': pool_len=429, using reset_history() for full sync
2026-07-07 11:29:31,317 - agent_instance_logger.py - 677 - INFO - Synced compression marker in n:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260707_112809.jsonl (429 messages).
2026-07-07 11:29:31,350 - execution_engine.py - 1466 - DEBUG - Rebuilt working sets for Maine: messages=429, llm_messages=428
2026-07-07 11:29:31,468 - execution_engine.py - 1278 - DEBUG - [PRE_LLM] Compress command handled for Maine
2026-07-07 11:29:31,468 - execution_engine.py - 781 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
2026-07-07 11:29:31,611 - execution_engine.py - 1958 - INFO - Endpoint allocation updated for orchestrator: {'endpoint': 'LMS-27B-unc-MTP', 'api_base': 'http://127.0.0.1:1234/v1', 'model': 'qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved', 'max_input_tokens': 110000, 'rate_limit_rpm': 0, 'concurrency_limit': 0, 'prev_max_input_tokens': 0}
2026-07-07 11:29:31,674 - base.py - 953 - INFO - Agent [Orchestrator] - ALL tokens: 93502, Available tokens: 108322

# loop rollback causing desync?
2026-07-07 13:31:05,463 - file_operations.py - 404 - DEBUG - Error listing directory: name 'os' is not defined
2026-07-07 13:31:05,525 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 62346, Available tokens: 108930
2026-07-07 13:31:08,412 - file_operations.py - 404 - DEBUG - Error listing directory: name 'os' is not defined
2026-07-07 13:31:08,469 - execution_engine.py - 1295 - DEBUG - [LOOP_DETECTED] InnerLoopFixer: pattern=sequence (assistant, function) repeated 3 times, pop_count=4, messages=46
2026-07-07 13:31:08,501 - tail_sync_check.py - 200 - WARNING - [TAIL SYNC DRIFT] 'InnerLoopFixer' after rollback: pool_tail=42 (conv_len=42, marker=no_marker) vs jsonl_tail=39 (total_msgs=122, marker=marker@line=84)
2026-07-07 13:31:08,506 - execution_engine.py - 1466 - DEBUG - Rebuilt working sets for InnerLoopFixer: messages=43, llm_messages=43
2026-07-07 13:31:08,506 - execution_engine.py - 781 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
2026-07-07 13:31:08,567 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 62332, Available tokens: 108930
2026-07-07 13:31:12,117 - file_operations.py - 404 - DEBUG - Error listing directory: name 'os' is not defined
2026-07-07 13:31:12,192 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 62378, Available tokens: 108930


# EOF
