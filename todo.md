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
- [ ] compression is fucking up the logs again, syncing them with agent pool instead of keeping the compressed messages
- [x] edit_file (and other file operations) feedback message needs to be more useful to the LLM receiving it - plan a more informative and tighter message format
- [ ] retry is broken, it deleted the user message too
- [ ] max tokens does not change when a new API endpoint is acquired 
- [ ] running into early timeout on code_intepreter
- [ ] get cmd_shell to output console result even on timeout
- [ ] we have about 10-15% discrepancy (less) between the nr of tokens we measure and the actual count that LMStudio processes 
- [x] randomly duplicated compression markers in agent log
- [x] first compression doesn't include the first user message; compressions with existing markers include the last marker twice: once in existing summary, second time in history (FIXED 2026-06-30)
- [ ] stop breaks something because i cant resume activity after, probably leaves allocate API slots stuck - it should clear up ALL the API slots. after 1000 fixed this still happens!
- [x] loop rollback system was appending to agent pool the first user message after rollback (FIXED 2026-07-01: simplified rollback — detect → inline rollback via shared _rollback_instance → append ONE hint message → continue same turn loop. Eliminated exception-throwing, retry loops, agent re-creation, and re-initialization that caused cache mismatch and message duplication.)
- [ ] images don't get properly pasted in chat
- [ ] Pause function interferes with streaming and halts the system in an odd state, it should only affect tool response startup.
- [ ] manually asking for security agent opinion does not fill it in and stop the security agent once it reached conclusion
- [x] auto-ask security sometimes returns this even if the response was fine: REJECTED BY USER: Security check error: There is no current event loop in thread 'Thread-43 (_run_check_worker)'. (Fixed 2026-06-30: replaced asyncio.get_event_loop() with _get_ws_loop helper that uses agent_pool._ws_loop)
- [x] approval window does not show justification for edit file operation (Fixed 2026-06-30: wired justification through tool classes → operation manager methods → tool_args → PendingApproval. Also fixed WriteFile non-JSON fallback path that silently dropped justification.)
- [ ] investigate if we can make shell cmd accept special character and multi-line `python -c` commands
      ERROR: 'charmap' codec can't encode character '\u2717' in position 0: character maps to <undefined>

# Errors to investigate:
- drift?
2026-07-01 08:13:54,798 - base.py - 949 - INFO - Agent [Coder] - ALL tokens: 18098, Available tokens: 89184
2026-07-01 08:14:13,936 - base.py - 949 - INFO - Agent [Coder] - ALL tokens: 18620, Available tokens: 89184
2026-07-01 08:14:25,988 - execution_engine.py - 853 - DEBUG - EXIT - PauseFixCoder RUNNING→IDLE
2026-07-01 08:14:25,990 - execution_engine.py - 2955 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=PauseFixCoder, reason=completed, inst_type=AgentInstance, conv_len=229, final_resp_len=195
2026-07-01 08:14:25,996 - tool_dispatcher.py - 382 - DEBUG - [SLOT_SYNC_CHILD_COMPLETE] Sync child 'PauseFixCoder' completed in 1326.09s
2026-07-01 08:14:25,997 - tool_dispatcher.py - 396 - DEBUG - [SLOT_SYNC_REACQUIRE] Attempting to re-acquire slot for 'Maine' after sync child
2026-07-01 08:14:25,998 - agent_pool.py - 1675 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-07-01 08:14:25,998 - tool_dispatcher.py - 405 - DEBUG - [SLOT_SYNC_REACQUIRED] Successfully re-acquired slot for 'Maine'. Total SYNC path elapsed: 1326.09s
2026-07-01 08:14:25,999 - tool_dispatcher.py - 106 - DEBUG - handle_call_agent returned type=str
2026-07-01 08:14:26,025 - base.py - 949 - INFO - Agent [Orchestrator] - ALL tokens: 5674, Available tokens: 88327
2026-07-01 08:15:02,145 - tool_dispatcher.py - 545 - DEBUG - call_agent nesting - Maine depth=1/10
2026-07-01 08:15:02,145 - tool_dispatcher.py - 362 - DEBUG - [SLOT_SYNC_RELEASE] Releasing slot for 'Maine' before running sync child 'PauseFixReviewer'
2026-07-01 08:15:02,148 - tool_dispatcher.py - 366 - DEBUG - [SLOT_SYNC_RELEASE] Slot released for 'Maine', active agents can now acquire
2026-07-01 08:15:02,148 - execution_engine.py - 2843 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target=PauseFixReviewer, class=reviewer, caller=Maine, nest_depth=1, force_fresh=False
2026-07-01 08:15:02,149 - lifecycle_manager.py - 176 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for PauseFixReviewer
2026-07-01 08:15:02,170 - tail_sync_check.py - 200 - WARNING - [TAIL SYNC DRIFT] 'PauseFixReviewer' after session_init: pool_tail=2 (conv_len=2, marker=no_marker) vs jsonl_tail=68 (total_msgs=68, marker=no_marker)
2026-07-01 08:15:02,173 - execution_engine.py - 2888 - DEBUG - starting engine.run() for PauseFixReviewer
2026-07-01 08:15:02,173 - execution_engine.py - 619 - DEBUG - engine.run() ENTRY - instance=PauseFixReviewer
2026-07-01 08:15:02,173 - agent_pool.py - 1675 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=reviewer, instance_name=PauseFixReviewer, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-07-01 08:15:02,174 - execution_engine.py - 461 - DEBUG - [SLOT_ACQUIRE] initial - instance=PauseFixReviewer, class=reviewer
2026-07-01 08:15:02,174 - execution_engine.py - 924 - INFO - [CACHE_REBUILD] Rebuilding working set for PauseFixReviewer
2026-07-01 08:15:02,175 - execution_engine.py - 1002 - DEBUG - [CACHE_REBUILD] System prompt content CHANGED for PauseFixReviewer
2026-07-01 08:15:02,179 - base.py - 949 - INFO - Agent [Reviewer] - ALL tokens: 442, Available tokens: 124204


# TOOL TIMING TRACE UNIFIED BRANCH
2026-07-03 07:58:47,481 - config_handlers.py - 140 - WARNING - [THREAD_POOL] resize_executor skipped — executor is None (pool just initialized?)
2026-07-03 07:58:47,482 - __init__.py - 128 - INFO - [Workspace] Tiered folders updated: RO=0, RW=2
2026-07-03 07:58:47,482 - agent_pool.py - 1382 - DEBUG - [CONFIG] Global configuration version incremented to 1
2026-07-03 07:58:47,482 - config_handlers.py - 72 - DEBUG - [update_config] Extra work folders unchanged (RO=0, RW=2)
2026-07-03 07:58:58,629 - api_integration.py - 352 - INFO - Created main agent instance: Maine
2026-07-03 07:58:58,631 - execution_engine.py - 619 - DEBUG - engine.run() ENTRY - instance=Maine
2026-07-03 07:58:58,632 - agent_pool.py - 1675 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-07-03 07:58:58,632 - execution_engine.py - 461 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-07-03 07:58:58,632 - execution_engine.py - 931 - INFO - [CACHE_REBUILD] Rebuilding working set for Maine
2026-07-03 07:58:58,633 - execution_engine.py - 1009 - DEBUG - [CACHE_REBUILD] System prompt content CHANGED for Maine
2026-07-03 07:58:58,634 - agent_instance_logger.py - 458 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260703_075856.jsonl with 1 messages.
2026-07-03 07:58:58,644 - execution_engine.py - 737 - DEBUG - [TOOL_RECOVERY] Maine Phase3 START LLM call (after tool recovery)
2026-07-03 07:58:58,644 - execution_engine.py - 1707 - DEBUG - [TOOL_RECOVERY] Maine _execute_llm_call ENTRY
2026-07-03 07:58:58,645 - execution_engine.py - 1755 - INFO - Endpoint allocation updated for orchestrator: {'endpoint': 'LMS-27B-unc-MTP', 'api_base': 'http://localhost:1234/v1', 'model': 'qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved', 'max_input_tokens': 110000, 'rate_limit_rpm': 0, 'concurrency_limit': 0, 'prev_max_input_tokens': 0}
2026-07-03 07:58:58,645 - execution_engine.py - 1800 - DEBUG - [TOOL_RECOVERY] Maine LLM API CALL START (llm.chat)
2026-07-03 07:58:58,645 - base.py - 949 - INFO - Agent [Orchestrator] - ALL tokens: 12, Available tokens: 108315
2026-07-03 07:58:58,646 - oai.py - 351 - DEBUG - [TOOL_RECOVERY] _chat_stream _chat_complete_create START model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:58:59,043 - oai.py - 167 - INFO - [UNIFIED_DEBUG] client_lookup=395.0ms httpx_timeout=Timeout(connect=5.0, read=600, write=600, pool=600) pool=pool_type=ConnectionPool, connections=0, keepalive_expiry=5.0, conns=[]
2026-07-03 07:58:59,043 - oai.py - 168 - INFO - [UNIFIED_DEBUG] local_api_kwargs={'base_url': 'http://localhost:1234/v1', 'api_key': 'EMPTY'}
2026-07-03 07:58:59,045 - oai.py - 169 - INFO - [UNIFIED_DEBUG] call_args: model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved, messages_count=2, stream=True, extra_body={'top_k': 20, 'repetition_penalty': 1.0, 'repeat_penalty': 1.0, 'min_p': 0.0}, timeout=None
2026-07-03 07:58:59,045 - oai.py - 170 - INFO - [TOOL_RECOVERY] HTTP POST ABOUT TO LEAVE — model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:59:01,389 - oai.py - 176 - INFO - [UNIFIED_DEBUG] POST_call=2344.2ms total=2739.3ms status=N/A
2026-07-03 07:59:01,389 - oai.py - 353 - DEBUG - [TOOL_RECOVERY] _chat_stream _chat_complete_create END (got response iterator)
2026-07-03 07:59:08,867 - oai.py - 376 - DEBUG - [TOOL_RECOVERY] _chat_stream FIRST CHUNK RECEIVED model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:59:08,868 - execution_engine.py - 1560 - DEBUG - [TOOL_RECOVERY] Maine LLM API CALL FIRST YIELD elapsed=10.2340s
2026-07-03 07:59:10,293 - execution_engine.py - 1640 - DEBUG - [TOOL_RECOVERY] Maine LLM API CALL COMPLETE (all streaming done) elapsed=11.6560s
2026-07-03 07:59:10,294 - execution_engine.py - 774 - DEBUG - [TOOL_RECOVERY] Maine Phase3 END LLM call elapsed=11.6560s
2026-07-03 07:59:10,295 - execution_engine.py - 2315 - DEBUG - [TOOL_RECOVERY] Maine START _process_response
2026-07-03 07:59:10,295 - execution_engine.py - 2101 - DEBUG - [TOOL_RECOVERY] Maine START execute_tool 'list_dir'
2026-07-03 07:59:10,302 - execution_engine.py - 2106 - DEBUG - [TOOL_RECOVERY] Maine END execute_tool 'list_dir' elapsed=0.0150s
2026-07-03 07:59:10,305 - execution_engine.py - 2406 - DEBUG - [TOOL_RECOVERY] Maine END _process_response elapsed=0.0150s
2026-07-03 07:59:10,305 - execution_engine.py - 785 - DEBUG - [TOOL_RECOVERY] Maine engine.run() yield response (tool path) len=4
2026-07-03 07:59:10,314 - execution_engine.py - 737 - DEBUG - [TOOL_RECOVERY] Maine Phase3 START LLM call (after tool recovery)
2026-07-03 07:59:10,315 - execution_engine.py - 1707 - DEBUG - [TOOL_RECOVERY] Maine _execute_llm_call ENTRY
2026-07-03 07:59:10,315 - execution_engine.py - 1800 - DEBUG - [TOOL_RECOVERY] Maine LLM API CALL START (llm.chat)
2026-07-03 07:59:10,316 - base.py - 949 - INFO - Agent [Orchestrator] - ALL tokens: 11433, Available tokens: 108315
2026-07-03 07:59:10,317 - oai.py - 351 - DEBUG - [TOOL_RECOVERY] _chat_stream _chat_complete_create START model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:59:10,317 - oai.py - 167 - INFO - [UNIFIED_DEBUG] client_lookup=0.0ms httpx_timeout=Timeout(connect=5.0, read=600, write=600, pool=600) pool=pool_type=ConnectionPool, connections=0, keepalive_expiry=5.0, conns=[]
2026-07-03 07:59:10,317 - oai.py - 168 - INFO - [UNIFIED_DEBUG] local_api_kwargs={'base_url': 'http://localhost:1234/v1', 'api_key': 'EMPTY'}
2026-07-03 07:59:10,317 - oai.py - 169 - INFO - [UNIFIED_DEBUG] call_args: model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved, messages_count=4, stream=True, extra_body={'top_k': 20, 'repetition_penalty': 1.0, 'repeat_penalty': 1.0, 'min_p': 0.0}, timeout=None
2026-07-03 07:59:10,318 - oai.py - 170 - INFO - [TOOL_RECOVERY] HTTP POST ABOUT TO LEAVE — model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:59:12,376 - oai.py - 176 - INFO - [UNIFIED_DEBUG] POST_call=2058.1ms total=2058.1ms status=N/A
2026-07-03 07:59:12,376 - oai.py - 353 - DEBUG - [TOOL_RECOVERY] _chat_stream _chat_complete_create END (got response iterator)
2026-07-03 07:59:24,079 - oai.py - 376 - DEBUG - [TOOL_RECOVERY] _chat_stream FIRST CHUNK RECEIVED model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:59:24,079 - execution_engine.py - 1560 - DEBUG - [TOOL_RECOVERY] Maine LLM API CALL FIRST YIELD elapsed=13.7660s
2026-07-03 07:59:28,833 - execution_engine.py - 1640 - DEBUG - [TOOL_RECOVERY] Maine LLM API CALL COMPLETE (all streaming done) elapsed=18.5320s
2026-07-03 07:59:28,836 - execution_engine.py - 774 - DEBUG - [TOOL_RECOVERY] Maine Phase3 END LLM call elapsed=18.5320s
2026-07-03 07:59:28,837 - execution_engine.py - 2315 - DEBUG - [TOOL_RECOVERY] Maine START _process_response
2026-07-03 07:59:28,838 - execution_engine.py - 2101 - DEBUG - [TOOL_RECOVERY] Maine START execute_tool 'read_file'
2026-07-03 07:59:28,842 - execution_engine.py - 2106 - DEBUG - [TOOL_RECOVERY] Maine END execute_tool 'read_file' elapsed=0.0000s
2026-07-03 07:59:28,843 - execution_engine.py - 2101 - DEBUG - [TOOL_RECOVERY] Maine START execute_tool 'read_file'
2026-07-03 07:59:28,849 - execution_engine.py - 2106 - DEBUG - [TOOL_RECOVERY] Maine END execute_tool 'read_file' elapsed=0.0150s
2026-07-03 07:59:28,850 - execution_engine.py - 2101 - DEBUG - [TOOL_RECOVERY] Maine START execute_tool 'read_file'
2026-07-03 07:59:28,853 - execution_engine.py - 2106 - DEBUG - [TOOL_RECOVERY] Maine END execute_tool 'read_file' elapsed=0.0000s
2026-07-03 07:59:28,853 - execution_engine.py - 2101 - DEBUG - [TOOL_RECOVERY] Maine START execute_tool 'read_file'
2026-07-03 07:59:28,857 - execution_engine.py - 2106 - DEBUG - [TOOL_RECOVERY] Maine END execute_tool 'read_file' elapsed=0.0000s
2026-07-03 07:59:28,860 - execution_engine.py - 2101 - DEBUG - [TOOL_RECOVERY] Maine START execute_tool 'read_file'
2026-07-03 07:59:28,862 - execution_engine.py - 2106 - DEBUG - [TOOL_RECOVERY] Maine END execute_tool 'read_file' elapsed=0.0000s
2026-07-03 07:59:28,863 - execution_engine.py - 2406 - DEBUG - [TOOL_RECOVERY] Maine END _process_response elapsed=0.0150s
2026-07-03 07:59:28,863 - execution_engine.py - 785 - DEBUG - [TOOL_RECOVERY] Maine engine.run() yield response (tool path) len=15
2026-07-03 07:59:28,868 - execution_engine.py - 737 - DEBUG - [TOOL_RECOVERY] Maine Phase3 START LLM call (after tool recovery)
2026-07-03 07:59:28,868 - execution_engine.py - 1707 - DEBUG - [TOOL_RECOVERY] Maine _execute_llm_call ENTRY
2026-07-03 07:59:28,869 - execution_engine.py - 1800 - DEBUG - [TOOL_RECOVERY] Maine LLM API CALL START (llm.chat)
2026-07-03 07:59:28,870 - base.py - 949 - INFO - Agent [Orchestrator] - ALL tokens: 14219, Available tokens: 108315
2026-07-03 07:59:28,872 - oai.py - 351 - DEBUG - [TOOL_RECOVERY] _chat_stream _chat_complete_create START model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:59:28,872 - oai.py - 167 - INFO - [UNIFIED_DEBUG] client_lookup=0.0ms httpx_timeout=Timeout(connect=5.0, read=600, write=600, pool=600) pool=pool_type=ConnectionPool, connections=0, keepalive_expiry=5.0, conns=[]
2026-07-03 07:59:28,872 - oai.py - 168 - INFO - [UNIFIED_DEBUG] local_api_kwargs={'base_url': 'http://localhost:1234/v1', 'api_key': 'EMPTY'}
2026-07-03 07:59:28,872 - oai.py - 169 - INFO - [UNIFIED_DEBUG] call_args: model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved, messages_count=10, stream=True, extra_body={'top_k': 20, 'repetition_penalty': 1.0, 'repeat_penalty': 1.0, 'min_p': 0.0}, timeout=None
2026-07-03 07:59:28,872 - oai.py - 170 - INFO - [TOOL_RECOVERY] HTTP POST ABOUT TO LEAVE — model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:59:30,937 - oai.py - 176 - INFO - [UNIFIED_DEBUG] POST_call=2063.7ms total=2063.7ms status=N/A
2026-07-03 07:59:30,937 - oai.py - 353 - DEBUG - [TOOL_RECOVERY] _chat_stream _chat_complete_create END (got response iterator)
2026-07-03 07:59:35,229 - oai.py - 376 - DEBUG - [TOOL_RECOVERY] _chat_stream FIRST CHUNK RECEIVED model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:59:35,229 - execution_engine.py - 1560 - DEBUG - [TOOL_RECOVERY] Maine LLM API CALL FIRST YIELD elapsed=6.3590s
2026-07-03 07:59:41,930 - execution_engine.py - 1640 - DEBUG - [TOOL_RECOVERY] Maine LLM API CALL COMPLETE (all streaming done) elapsed=13.0620s
2026-07-03 07:59:41,931 - execution_engine.py - 774 - DEBUG - [TOOL_RECOVERY] Maine Phase3 END LLM call elapsed=13.0620s
2026-07-03 07:59:41,932 - execution_engine.py - 2315 - DEBUG - [TOOL_RECOVERY] Maine START _process_response
2026-07-03 07:59:41,932 - execution_engine.py - 2406 - DEBUG - [TOOL_RECOVERY] Maine END _process_response elapsed=0.0000s
2026-07-03 07:59:41,933 - execution_engine.py - 860 - DEBUG - EXIT - Maine RUNNING→IDLE


# MAIN BRANCH
2026-07-03 07:56:09,716 - agent_pool.py - 882 - INFO - Idle agent checker started: timeout=900s, interval=60s
2026-07-03 07:56:12,044 - base.py - 852 - INFO - Agent [Orchestrator] - ALL tokens: 12, Available tokens: 62490
2026-07-03 07:56:12,044 - oai.py - 280 - INFO - [TIMING] _chat_stream START model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:12,429 - oai.py - 111 - INFO - [MAIN_DEBUG] client_created=383.3ms httpx_timeout=Timeout(connect=5.0, read=600, write=600, pool=600) pool=pool_type=ConnectionPool, connections=0, keepalive_expiry=5.0, conns=[]
2026-07-03 07:56:12,430 - oai.py - 112 - INFO - [MAIN_DEBUG] local_api_kwargs={'base_url': 'http://localhost:1234/v1', 'api_key': 'EMPTY'}
2026-07-03 07:56:12,432 - oai.py - 113 - INFO - [MAIN_DEBUG] call_args: model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved, messages_count=2, stream=True, extra_body={'top_k': 40, 'repetition_penalty': 1.05, 'repeat_penalty': 1.05, 'min_p': 0.05}, timeout=None
2026-07-03 07:56:12,432 - oai.py - 114 - INFO - [TOOL_RECOVERY] HTTP POST ABOUT TO LEAVE — model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:12,770 - oai.py - 118 - INFO - [MAIN_DEBUG] POST_call=337.4ms total=720.6ms
2026-07-03 07:56:12,770 - oai.py - 282 - INFO - [TIMING] _chat_stream GOT ITERATOR (headers received) model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:22,577 - agent_orchestrator.py - 805 - INFO - Truncated 'list_dir' result for Maine: 13169 chars -> 950 chars. Reason: A possible wild read without defined limits (over 10000 chars). Spill file: logs/spillover/Maine_list_dir_20260703_075622.txt
2026-07-03 07:56:22,591 - base.py - 852 - INFO - Agent [Orchestrator] - ALL tokens: 465, Available tokens: 62490
2026-07-03 07:56:22,592 - oai.py - 280 - INFO - [TIMING] _chat_stream START model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:22,929 - oai.py - 111 - INFO - [MAIN_DEBUG] client_created=336.7ms httpx_timeout=Timeout(connect=5.0, read=600, write=600, pool=600) pool=pool_type=ConnectionPool, connections=0, keepalive_expiry=5.0, conns=[]
2026-07-03 07:56:22,929 - oai.py - 112 - INFO - [MAIN_DEBUG] local_api_kwargs={'base_url': 'http://localhost:1234/v1', 'api_key': 'EMPTY'}
2026-07-03 07:56:22,931 - oai.py - 113 - INFO - [MAIN_DEBUG] call_args: model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved, messages_count=4, stream=True, extra_body={'top_k': 40, 'repetition_penalty': 1.05, 'repeat_penalty': 1.05, 'min_p': 0.05}, timeout=None
2026-07-03 07:56:22,931 - oai.py - 114 - INFO - [TOOL_RECOVERY] HTTP POST ABOUT TO LEAVE — model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:22,963 - oai.py - 118 - INFO - [MAIN_DEBUG] POST_call=31.1ms total=367.8ms
2026-07-03 07:56:22,964 - oai.py - 282 - INFO - [TIMING] _chat_stream GOT ITERATOR (headers received) model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:27,182 - agent_orchestrator.py - 805 - INFO - Truncated 'read_file' result for Maine: 10177 chars -> 950 chars. Reason: A possible wild read without defined limits (over 10000 chars). Original file: AgentCascade_parallel_strategy.md
2026-07-03 07:56:27,216 - base.py - 852 - INFO - Agent [Orchestrator] - ALL tokens: 3357, Available tokens: 62490
2026-07-03 07:56:27,218 - oai.py - 280 - INFO - [TIMING] _chat_stream START model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:27,563 - oai.py - 111 - INFO - [MAIN_DEBUG] client_created=344.4ms httpx_timeout=Timeout(connect=5.0, read=600, write=600, pool=600) pool=pool_type=ConnectionPool, connections=0, keepalive_expiry=5.0, conns=[]
2026-07-03 07:56:27,563 - oai.py - 112 - INFO - [MAIN_DEBUG] local_api_kwargs={'base_url': 'http://localhost:1234/v1', 'api_key': 'EMPTY'}
2026-07-03 07:56:27,565 - oai.py - 113 - INFO - [MAIN_DEBUG] call_args: model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved, messages_count=7, stream=True, extra_body={'top_k': 40, 'repetition_penalty': 1.05, 'repeat_penalty': 1.05, 'min_p': 0.05}, timeout=None
2026-07-03 07:56:27,565 - oai.py - 114 - INFO - [TOOL_RECOVERY] HTTP POST ABOUT TO LEAVE — model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:27,594 - oai.py - 118 - INFO - [MAIN_DEBUG] POST_call=28.1ms total=372.6ms
2026-07-03 07:56:27,594 - oai.py - 282 - INFO - [TIMING] _chat_stream GOT ITERATOR (headers received) model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:33,123 - base.py - 852 - INFO - Agent [Orchestrator] - ALL tokens: 6381, Available tokens: 62490
2026-07-03 07:56:33,125 - oai.py - 280 - INFO - [TIMING] _chat_stream START model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:33,467 - oai.py - 111 - INFO - [MAIN_DEBUG] client_created=340.9ms httpx_timeout=Timeout(connect=5.0, read=600, write=600, pool=600) pool=pool_type=ConnectionPool, connections=0, keepalive_expiry=5.0, conns=[]
2026-07-03 07:56:33,467 - oai.py - 112 - INFO - [MAIN_DEBUG] local_api_kwargs={'base_url': 'http://localhost:1234/v1', 'api_key': 'EMPTY'}
2026-07-03 07:56:33,469 - oai.py - 113 - INFO - [MAIN_DEBUG] call_args: model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved, messages_count=9, stream=True, extra_body={'top_k': 40, 'repetition_penalty': 1.05, 'repeat_penalty': 1.05, 'min_p': 0.05}, timeout=None
2026-07-03 07:56:33,469 - oai.py - 114 - INFO - [TOOL_RECOVERY] HTTP POST ABOUT TO LEAVE — model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:33,478 - oai.py - 118 - INFO - [MAIN_DEBUG] POST_call=8.0ms total=348.8ms
2026-07-03 07:56:33,478 - oai.py - 282 - INFO - [TIMING] _chat_stream GOT ITERATOR (headers received) model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:41,775 - base.py - 852 - INFO - Agent [Orchestrator] - ALL tokens: 8183, Available tokens: 62490
2026-07-03 07:56:41,779 - oai.py - 280 - INFO - [TIMING] _chat_stream START model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:42,115 - oai.py - 111 - INFO - [MAIN_DEBUG] client_created=336.0ms httpx_timeout=Timeout(connect=5.0, read=600, write=600, pool=600) pool=pool_type=ConnectionPool, connections=0, keepalive_expiry=5.0, conns=[]
2026-07-03 07:56:42,115 - oai.py - 112 - INFO - [MAIN_DEBUG] local_api_kwargs={'base_url': 'http://localhost:1234/v1', 'api_key': 'EMPTY'}
2026-07-03 07:56:42,117 - oai.py - 113 - INFO - [MAIN_DEBUG] call_args: model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved, messages_count=11, stream=True, extra_body={'top_k': 40, 'repetition_penalty': 1.05, 'repeat_penalty': 1.05, 'min_p': 0.05}, timeout=None
2026-07-03 07:56:42,117 - oai.py - 114 - INFO - [TOOL_RECOVERY] HTTP POST ABOUT TO LEAVE — model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:42,141 - oai.py - 118 - INFO - [MAIN_DEBUG] POST_call=23.7ms total=359.7ms
2026-07-03 07:56:42,142 - oai.py - 282 - INFO - [TIMING] _chat_stream GOT ITERATOR (headers received) model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:52,361 - base.py - 852 - INFO - Agent [Orchestrator] - ALL tokens: 10330, Available tokens: 62490
2026-07-03 07:56:52,364 - oai.py - 280 - INFO - [TIMING] _chat_stream START model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:52,701 - oai.py - 111 - INFO - [MAIN_DEBUG] client_created=336.1ms httpx_timeout=Timeout(connect=5.0, read=600, write=600, pool=600) pool=pool_type=ConnectionPool, connections=0, keepalive_expiry=5.0, conns=[]
2026-07-03 07:56:52,701 - oai.py - 112 - INFO - [MAIN_DEBUG] local_api_kwargs={'base_url': 'http://localhost:1234/v1', 'api_key': 'EMPTY'}
2026-07-03 07:56:52,703 - oai.py - 113 - INFO - [MAIN_DEBUG] call_args: model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved, messages_count=13, stream=True, extra_body={'top_k': 40, 'repetition_penalty': 1.05, 'repeat_penalty': 1.05, 'min_p': 0.05}, timeout=None
2026-07-03 07:56:52,704 - oai.py - 114 - INFO - [TOOL_RECOVERY] HTTP POST ABOUT TO LEAVE — model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:56:52,733 - oai.py - 118 - INFO - [MAIN_DEBUG] POST_call=29.0ms total=365.1ms
2026-07-03 07:56:52,733 - oai.py - 282 - INFO - [TIMING] _chat_stream GOT ITERATOR (headers received) model=qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-03 07:57:03,300 - api_server.py - 1403 - INFO - Syncing history from agent state - pool corruption detected. Pool has SYSTEM: False, tfm has SYSTEM: True. Pool length: 19, tfm length: 20.


# EOF
