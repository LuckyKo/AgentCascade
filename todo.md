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

[x] Add skills (custom agent loading?). there are some pre-existing modules from Qwen agent that deal with skills that havent been integrated, investigate hw they could be incorporated in AC, alternatives or improvements to that.
[ ] Add an Overseer agent that periodically checks on the health of the system, reads logs and telemetry, check if running agents got stuck in undetectable loops or migrated goals towards something that the user never asked for, suggests fixes and improvements into a suggestion box. Main agent will pull from the suggestion box during idle times when user is AFK to self improve the agents or the framework during our daily operation - do the whole DNA A/B testing thing. Overseer agent will always get its full working queue compressed when it finishes and save it into the suggestion box (no chat messages) - should be persistent across sessions. We'll set the interval at which it activates, it will silently interrupt running agents when it activates and resume them like it never happens when its done (unless it decides to kill an agent), or work in parallel using a different API endpoint. - big task, will do it after we stabilize the framework
[ ] need a memory consolidation task ran periodically, like when we accumulate 8 summaries, takes all summaries-1 from log and compact them again using the compressor agent, replace it in marker-1 position and remove the first 6 from log. he final output should have only this compaction and the last marker.
[x] implement async shell_cmd launch (immediate tool response that it was launched, runs in background while agent keeps running and return final output as user message when done, can have heartbeat value that will periodically send console output back to caller agent) — DONE: AsyncShellTracker module with per-agent ID counters, max 5 concurrent shells, heartbeat injection via message queue, process tree cleanup on dismiss
[x] make cmd_shell pop open a console window in the back so the user can inspect or interact with it if needed. — DONE: CREATE_NEW_CONSOLE flag on Windows Popen launch in AsyncShellTracker
[ ] add auto-rollback feature on edit_file fail
[ ] implement a live scratchpad tool that injects text/image data into the last few FUNCTION/USER messages. the tool can load a live view of a file's content, console output of a program by PID, interface capture data of a program by PID, set persistence distance (nr of messages in tail agent pool retaining the data, older messages get the data trimmed). agent can call this tool to enable disable this scratchpad (disable by setting persistence to 0, defaults on 2) 
[ ] add a stop button to shell_cmd messages so user can terminate them early
[ ] investigate possible use of `https://github.com/eugeniughelbur/obsidian-second-brain`for our lessons file management
[ ] full audit of the API endpoint allocation logic/async agent calls, with full testing coverage
[ ] make view_image tool take in special arguments in path like `__screen_capture`, `__window_capture:PID` - self explanatory
[x] make out path helper that tools use resolve extra_rw/ro paths just like code_intepreter does
[x] make shell_cmd automatically launch on async mode if the agent sets timeout bigger than 60s and doesn't specify the mode as sync.
[x] check put shell_cmd async commands. add a `__wait` command to simply wait for next heartbeat - a no reply tool call basically (needed because most LLMs dont understand the concept of shutting up to get in SLEEPING state). make sure all these commands dont need justification field.
[x] decouple `enable skills` toggle from auto-skill logic, add `enable auto-skill generation`
[ ] make a way to elegantly acquire a skill at run time (beside agent init)
[ ] add a `range` argument to edit_file tool for the delete_and_insert mode so its less confusing than reusing `old_contnet`


# BUGS:

- [ ] telemetry `Output Tokens (est)` severely undercounts
- [ ] we are pushing wrong summary from the inner loop detector if the compressor fails and gets stuck in a loop `[SYSTEM ERROR: Empty LLM response]`. it should try another API endpoint instead 
- [ ] inner loop detector is almost unusable how many false positives generates, `char run` is the only good mode. pls make tests that simulate streaming as it happens normally, use existing logs to check for false positives.
- [x] approval timeout occurs even when explicitly disabled in options, when it was set on auto-ask mode — DONE: Security advisor used hard-coded 180s timeout constant instead of reading from operation_manager settings. Fixed `security_handler.run_check()` to dynamically read `enable_timeout` and `approval_timeout_seconds` from operation manager. Timeout message now shows actual configured value. Added None guards for safety.
- [x] I dont want truncation of the user messages in the que (UI user que display) — DONE: Renamed `get_queue_previews` → `get_queue_messages`, removed `max_length` truncation (100 chars). Method now returns full message strings. Updated all 4 call sites.
- [ ] UI streaming stops on `pause`. it should not, pause should ONLY stop the tool response logic.
- [x] some of the UI setting are getting reset on system restart (they stick just fine on refresh though) — DONE: PoolSettings was never persisted to disk. Added `to_dict()`/`from_dict()` methods to PoolSettings dataclass, `_save_pool_settings()`/`_load_pool_settings()` to AgentPool with thread-safe lock, save calls to all 25+ config handlers and startup override paths. Settings now persist to `config/pool_settings.json`. Also added missing `enable_agent_budgeting` field to PoolSettings that was being set but not defined.
- [x] After changes to Security agent soul shell_cmd fails with this: `REJECTED: Security check error: No template for agent class Security`
- [x] forced compression seems lazy, waits for a agent call to already happen when over the limit instead of triggering before that (fixed - always use _count_history_tokens for proactive check)
- [x] remove context window limit truncation of tool response, we already have wild read truncation for extremes and with the fix from above it should be unnecessary (removed truncate_tool_result + dead code cleanup)
- [x] inner loop API fallback should only apply if we hit the `char run` detect specifically, not for the others types of detection hits
- [x] compression task message included in image embeds of a message that is was not even in the compressed range of messages. the image embeds should not be sent at all to compressor, it already receives the caption data (fixed — added agent_class param to build_task_message, skip image embedding for Compressor, removed post-hoc stripping code)
- [x] add truncation with helper to list_dir, keep head mode. (done - uses truncate_with_spillover, head mode, char_limit=3000 default)
- [x] UI issue: auto scroll to bottom keeps dropping after long tool outputs or reasoning (fixed — replaced requestAnimationFrame with immediate scroll, added programmaticScrollCount guard, debounce timer cleanup, tab switch lock reset)
- [ ] add inner loop counter to telemetry's loop detected
- [x] odd useless truncation message on `list_dir` tool, should contain spillover path (should use helper truncation function like other tools, is there another one?): [TRUNCATED — Character limit exceeded.]. also needs the char limit added to the UI
- [x] overly aggressive stick to bottom function, active when streaming even when the user is actively scrolling up. it should NOT be fighting the user (fixed — immediate unlock on scroll up, visibility guard prevents auto-scroll on hidden tabs, lock released when tab becomes invisible)
- [ ] extra work paths need to be tied to each session, they have to be loaded when we load existing sessions from the metadata entry.
- [x] sub agent kicked back to caller when the API connection dropped mid normal assistant message streaming (fixed — broadened retry_model_service_iterator in llm/base.py to catch all Exceptions during streaming, not just ModelServiceError; network errors now retry with exponential backoff up to max_retries)
- [ ] inner loop detection does not seem to pick up if loop is happening within a tool call.
- [ ] Write a proper README.md that describes the project as a whole and offers easy install & use instructions
- [x] context token estimation (the one in base.py) is off by about 10% less than what llama.cpp reports as receiving. (fixed — added CHAT_TEMPLATE_TOKEN_OVERHEAD=5 per message to get_message_stats() in utils/utils.py, and unified _count_history_tokens() in execution_engine.py to use get_message_stats() instead of raw qwen_count(); error dropped from ~37% to ~4%)

# Errors to investigate:

# Truncated extra paths in system message caused full prompt rebuild

currency_limit=0
2026-07-27 08:54:19,257 - execution_engine.py - 698 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-07-27 08:54:19,258 - execution_engine.py - 967 - DEBUG - [TURN_START] Calling _setup_turn for Maine
2026-07-27 08:54:19,258 - execution_engine.py - 1462 - INFO - [CACHE_REBUILD] Rebuilding working set for Maine (conv_len=445)
2026-07-27 08:54:19,259 - execution_engine.py - 1559 - INFO - [CACHE_REBUILD] System prompt content CHANGED for Maine (len 5250→5387, first_diff@163: orig=': N:\work\WD\AgentWorkspace
- Log Path: n:\work\WD\AgentWork' new=': N:\work\WD\AgentWorkspace
- Extra Paths (Read-Only): N:\wo')
2026-07-27 08:54:19,267 - agent_instance_logger.py - 486 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260727_084424.jsonl with 653 messages.
2026-07-27 08:54:19,268 - execution_engine.py - 1002 - DEBUG - [TURN_DONE] Got messages=445, llm_messages=445
2026-07-27 08:54:19,291 - execution_engine.py - 1085 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
2026-07-27 08:54:19,387 - base.py - 994 - INFO - Agent [Orchestrator] - ALL tokens: 80130, Available tokens: 88819
2026-07-27 08:54:24,201 - agent_pool.py - 2734 - INFO - [idle_checker] Auto-dismissing idle system agent (Security) 'Security_op_dc743fe4' (idle for 115s, threshold=60s)
2026-07-27 08:54:24,201 - agent_pool.py - 613 - DEBUG - Instance conversation cleanup key missing (expected): 'Security_op_dc743fe4'
2026-07-27 08:54:24,204 - agent_pool.py - 2659 - INFO - [idle_checker] Auto-dismissed 1 idle agent(s): Security_op_dc743fe4


# Bad inner loop detect and error
2026-07-30 04:11:05,288 - base.py - 986 - INFO - Agent [Researcher] - ALL tokens: 36389, Available tokens: 123519
2026-07-30 04:11:08,450 - base.py - 986 - INFO - Agent [Researcher] - ALL tokens: 38369, Available tokens: 123519
2026-07-30 04:11:11,542 - base.py - 986 - INFO - Agent [Researcher] - ALL tokens: 41235, Available tokens: 123519
2026-07-30 04:11:16,498 - base.py - 986 - INFO - Agent [Researcher] - ALL tokens: 41323, Available tokens: 123519
2026-07-30 04:11:18,686 - execution_engine.py - 2373 - INFO - [STREAM_GUARD] Detected generation loop: repeated ngram (score=360.0) for settings_investigator. Retrying…
2026-07-30 04:11:18,686 - execution_engine.py - 2423 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentWorkspace\logs\loop_samples\samples_2026-07-30.jsonl
2026-07-30 04:11:18,689 - execution_engine.py - 2455 - DEBUG - [INNER_LOOP] Detection error for settings_investigator: inner_loop: repeated ngram
2026-07-30 04:11:18,689 - execution_engine.py - 2218 - INFO - [INNER_LOOP] Detection triggered for 'settings_investigator' (reason: repeated ngram), but not strong enough to advance cursor. Retrying same endpoint.
2026-07-30 04:11:18,690 - execution_engine.py - 2666 - WARNING - [ENDPOINT_RETRY] LLM call failed for settings_investigator, retry 1/3. Retrying in 1.1s... Error: inner_loop: repeated ngram
2026-07-30 04:11:19,752 - base.py - 986 - INFO - Agent [Researcher] - ALL tokens: 41323, Available tokens: 123519
2026-07-30 04:11:21,657 - base.py - 986 - INFO - Agent [Researcher] - ALL tokens: 42710, Available tokens: 123519
2026-07-30 04:11:24,445 - base.py - 986 - INFO - Agent [Researcher] - ALL tokens: 43240, Available tokens: 123519
2026-07-30 04:11:27,019 - execution_engine.py - 2373 - INFO - [STREAM_GUARD] Detected generation loop: repeated ngram (score=360.0) for settings_investigator. Retrying…
2026-07-30 04:11:27,020 - execution_engine.py - 2423 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentWorkspace\logs\loop_samples\samples_2026-07-30.jsonl
2026-07-30 04:11:27,021 - execution_engine.py - 2455 - DEBUG - [INNER_LOOP] Detection error for settings_investigator: inner_loop: repeated ngram
2026-07-30 04:11:27,022 - execution_engine.py - 2218 - INFO - [INNER_LOOP] Detection triggered for 'settings_investigator' (reason: repeated ngram), but not strong enough to advance cursor. Retrying same endpoint.
2026-07-30 04:11:27,023 - execution_engine.py - 2666 - WARNING - [ENDPOINT_RETRY] LLM call failed for settings_investigator, retry 1/3. Retrying in 1.0s... Error: inner_loop: repeated ngram
2026-07-30 04:11:28,080 - base.py - 986 - INFO - Agent [Researcher] - ALL tokens: 43240, Available tokens: 123519
2026-07-30 04:11:30,132 - execution_engine.py - 2373 - INFO - [STREAM_GUARD] Detected generation loop: repeated ngram (score=424.9) for settings_investigator. Retrying…
2026-07-30 04:11:30,132 - execution_engine.py - 2423 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentWorkspace\logs\loop_samples\samples_2026-07-30.jsonl
2026-07-30 04:11:30,134 - execution_engine.py - 2455 - DEBUG - [INNER_LOOP] Detection error for settings_investigator: inner_loop: repeated ngram
2026-07-30 04:11:30,134 - execution_engine.py - 2218 - INFO - [INNER_LOOP] Detection triggered for 'settings_investigator' (reason: repeated ngram), but not strong enough to advance cursor. Retrying same endpoint.
2026-07-30 04:11:30,135 - execution_engine.py - 2666 - WARNING - [ENDPOINT_RETRY] LLM call failed for settings_investigator, retry 2/3. Retrying in 2.2s... Error: inner_loop: repeated ngram
2026-07-30 04:11:32,300 - base.py - 986 - INFO - Agent [Researcher] - ALL tokens: 43240, Available tokens: 123519
2026-07-30 04:11:34,609 - execution_engine.py - 2373 - INFO - [STREAM_GUARD] Detected generation loop: repeated ngram (score=360.0) for settings_investigator. Retrying…
2026-07-30 04:11:34,609 - execution_engine.py - 2423 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentWorkspace\logs\loop_samples\samples_2026-07-30.jsonl
2026-07-30 04:11:34,611 - execution_engine.py - 2455 - DEBUG - [INNER_LOOP] Detection error for settings_investigator: inner_loop_exhausted: retried 3 times, giving up — last reason: repeated ngram
2026-07-30 04:11:34,612 - execution_engine.py - 1301 - ERROR - EXCEPTION - settings_investigator: CharacterRunDetected: inner_loop_exhausted: retried 3 times, giving up — inner_loop_exhausted: retried 3 times, giving up — last reason: repeated ngram
Traceback (most recent call last):
  File "n:\work\WD\AgentCascade\agent_cascade\execution_engine.py", line 2427, in _execute_llm_call_with_retry
    raise CharacterRunDetected(
agent_cascade.exceptions.CharacterRunDetected: inner_loop_exhausted: retried 3 times, giving up — last reason: repeated ngram

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "n:\work\WD\AgentCascade\agent_cascade\execution_engine.py", line 1148, in run
    for msg in gen:
               ^^^
  File "n:\work\WD\AgentCascade\agent_cascade\execution_engine.py", line 2722, in _call_llm_with_injection
    yield from self._execute_llm_call_with_retry(instance, llm_messages, template, active_functions)
  File "n:\work\WD\AgentCascade\agent_cascade\execution_engine.py", line 2634, in _execute_llm_call_with_retry
    self._handle_inner_loop_detection(instance, e, retry_count, loop_retry_count, _max_attempts)
  File "n:\work\WD\AgentCascade\agent_cascade\execution_engine.py", line 2196, in _handle_inner_loop_detection
    raise CharacterRunDetected(
agent_cascade.exceptions.CharacterRunDetected: inner_loop_exhausted: retried 3 times, giving up — inner_loop_exhausted: retried 3 times, giving up — last reason: repeated ngram
2026-07-30 04:11:34,615 - execution_engine.py - 1385 - DEBUG - EXIT - settings_investigator RUNNING→IDLE
2026-07-30 04:11:34,616 - execution_engine.py - 4437 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=settings_investigator, reason=completed, inst_type=AgentInstance, conv_len=2, final_resp_len=1
2026-07-30 04:11:34,619 - tool_dispatcher.py - 433 - DEBUG - [SLOT_SYNC_CHILD_COMPLETE] Sync child 'settings_investigator' completed in 107.97s
2026-07-30 04:11:34,619 - tool_dispatcher.py - 446 - DEBUG - [SLOT_SYNC_REACQUIRE] Attempting to re-acquire slot for 'Maine' after sync child
2026-07-30 04:11:34,619 - agent_pool.py - 2232 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://127.0.0.1:1234/v1, concurrency_limit=0
