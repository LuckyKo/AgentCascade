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
- [x] backup file paths in tool feedback need to be absolute path
- [x] edit_file (and other file operations) feedback message needs to be more useful to the LLM receiving it - plan a more informative and tighter message format
- [ ] retry is broken, it deleted the user message too
- [ ] max tokens does not change when a new API endpoint is acquired 
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
# EOF
