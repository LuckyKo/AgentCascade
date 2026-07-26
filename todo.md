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
[ ] need a memory consolidation task ran periodically - takes all summaries in log and arranges them in a neat continuous package like long term memory -> replaces last summary
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

# BUGS:

- [ ] no agent tab refresh during tool call streaming causes `Activity` bar to be still during tool writing process
- [ ] manually asking for security agent opinion does not fill it in and stop the security agent info once it reached conclusion
- [ ] telemetry `Output Tokens (est)` severely undercounts
- [ ] we are pushing wrong summary from the inner loop detector if the compressor fails and gets stuck in a loop `[SYSTEM ERROR: Empty LLM response]`. it should try another API endpoint instead 
- [ ] inner loop detector is almost unusable how many false positives generates, `char run` is the only good mode. pls make tests that simulate streaming as it happens normally, use rel existing logs to check for false positives.
- [x] approval timeout occurs even when explicitly disabled in options, when it was set on auto-ask mode — DONE: Security advisor used hard-coded 180s timeout constant instead of reading from operation_manager settings. Fixed `security_handler.run_check()` to dynamically read `enable_timeout` and `approval_timeout_seconds` from operation manager. Timeout message now shows actual configured value. Added None guards for safety.
- [x] I dont want truncation of the user messages in the que (UI user que display) — DONE: Renamed `get_queue_previews` → `get_queue_messages`, removed `max_length` truncation (100 chars). Method now returns full message strings. Updated all 4 call sites.
- [ ] UI streaming stops on `pause`. it should not, pause should ONLY stop the tool response logic.
- [ ] some of the UI setting are getting reset on browser/system restart (they stick on refresh though)
- [x] After changes to Security agent soul shell_cmd fails with this: `REJECTED: Security check error: No template for agent class Security`
- [x] forced compression seems lazy, waits for a agent call to already happen when over the limit instead of triggering before that (fixed - always use _count_history_tokens for proactive check)
- [x] remove context window limit truncation of tool response, we already have wild read truncation for extremes and with the fix from above it should be unnecessary (removed truncate_tool_result + dead code cleanup)
- [x] inner loop API fallback should only apply if we hit the `char run` detect specifically, not for the others types of detection hits
- [x] compression task message included in image embeds of a message that is was not even in the compressed range of messages. the image embeds should not be sent at all to compressor, it already receives the caption data (fixed — added agent_class param to build_task_message, skip image embedding for Compressor, removed post-hoc stripping code)
- [x] add truncation with helper to list_dir, keep head mode. (done - uses truncate_with_spillover, head mode, char_limit=3000 default)
- [ ] security agent fails to timeout if it keeps failing to acquire API endpoint.
- [x] UI issue: auto scroll to bottom keeps dropping after long tool outputs or reasoning (fixed — replaced requestAnimationFrame with immediate scroll, added programmaticScrollCount guard, debounce timer cleanup, tab switch lock reset)
- [ ] add inner loop counter to telemetry's loop detected
- [ ] odd useless truncation message on `list_dir` tool, should contain spillover path (should use helper truncation function like other tools, is there another one?): [TRUNCATED — Character limit exceeded.]. also needs the char limit added to the UI
- [ ] overly aggressive stick to bottom function, active when streaming even when the user is actively scrolling up

# Errors to investigate:

# FIXED (2026-07-26): no cache hit on new user message — stale browser tab clearing work folders via update_config
# Root cause: Second browser tab with empty localStorage sent update_config with work_access_folders_ro/rw=[], clearing server config. Session Metadata lost Extra Paths → KV cache busted.
# Solution: Split into two paths:
#   1. update_config (auto-sync): empty arrays = no-op, prevents stale tabs from clearing valid config
#   2. set_work_folders (explicit Save button): allows clearing, only sent on intentional user action
# Changes: config_handlers.py (defensive empty check), ws_handlers.py (new handler), web_ui/app.js + index.html (Save buttons, no auto-sync)
2026-07-26 03:32:12,606 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 2.05.502.029 I slot print_timing: id  0 | task 180 | n_decoded =    329, tg =  18.53 t/s, tg_3s =  15.19 t/s
2026-07-26 03:32:15,653 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 2.08.548.805 I slot print_timing: id  0 | task 180 | n_decoded =    375, tg =  18.03 t/s, tg_3s =  15.10 t/s
2026-07-26 03:32:16,190 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 2.09.086.551 I slot print_timing: id  0 | task 180 | prompt eval time =    1587.88 ms /   299 tokens (    5.31 ms per token,   188.30 tokens per second)
2026-07-26 03:32:16,191 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 2.09.086.556 I slot print_timing: id  0 | task 180 |        eval time =   21340.74 ms /   381 tokens (   56.01 ms per token,    17.85 tokens per second)
2026-07-26 03:32:16,192 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 2.09.086.560 I slot print_timing: id  0 | task 180 |       total time =   22928.62 ms /   680 tokens
2026-07-26 03:32:16,192 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 2.09.086.561 I slot print_timing: id  0 | task 180 |    graphs reused =        156
2026-07-26 03:32:16,193 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 2.09.086.564 I slot print_timing: id  0 | task 180 | draft acceptance = 0.89604 (  181 accepted /   202 generated), mean len =  2.45
2026-07-26 03:32:16,193 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 2.09.088.714 I slot      release: id  0 | task 180 | stop processing: n_tokens = 68192, truncated = 0
2026-07-26 03:36:09,189 [INFO] autoloader: --> Incoming POST request to '/v1/chat/completions' (model param input: 'Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf')
2026-07-26 03:36:09,190 [INFO] autoloader: Resolved model ID: 'Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf'
2026-07-26 03:36:09,191 [INFO] autoloader: Forwarding POST /v1/chat/completions -> llama-server on port 9022
2026-07-26 03:36:09,514 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 6.02.409.758 I slot get_availabl: id  0 | task -1 | selected slot by LRU, t_last = 129050742
2026-07-26 03:36:12,208 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 6.05.104.069 I slot launch_slot_: id  0 | task 384 | processing task, is_child = 0
2026-07-26 03:36:12,427 [INFO] httpx: HTTP Request: POST http://127.0.0.1:9022/v1/chat/completions "HTTP/1.1 200 OK"
[32mINFO[0m:     127.0.0.1:58924 - "[1mPOST /v1/chat/completions HTTP/1.1[0m" [32m200 OK[0m
2026-07-26 03:36:16,104 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 6.09.000.141 I slot print_timing: id  0 | task 384 | prompt processing, n_tokens =   4096, progress = 0.06, t =   3.90 s / 1051.32 tokens per second
2026-07-26 03:36:17,980 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 6.10.876.382 I slot print_timing: id  0 | task 384 | prompt processing, n_tokens =   6144, progress = 0.09, t =   5.77 s / 1064.40 tokens per second
2026-07-26 03:36:19,066 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 6.11.961.148 I slot print_timing: id  0 | task 384 | prompt processing, n_tokens =   7178, progress = 0.11, t =   6.86 s / 1046.81 tokens per second
2026-07-26 03:36:19,715 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 6.12.611.403 I slot print_timing: id  0 | task 384 | prompt processing, n_tokens =   7622, progress = 0.11, t =   7.51 s / 1015.28 tokens per second

2026-07-26 03:36:08,694 - agent_pool.py - 495 - DEBUG - Idle checker restarted
2026-07-26 03:36:08,694 - agent_pool.py - 511 - DEBUG - Async registry executor recreated
2026-07-26 03:36:08,696 - agent_pool.py - 514 - DEBUG - Stopped flag cleared — ready for new execution
2026-07-26 03:36:08,697 - ws_handlers.py - 204 - DEBUG - Starting generation gen_id=3, instances={'Maine': 'IDLE'}, active_stack=0
2026-07-26 03:36:08,698 - agent_pool.py - 495 - DEBUG - Idle checker restarted
2026-07-26 03:36:08,699 - agent_pool.py - 511 - DEBUG - Async registry executor recreated
2026-07-26 03:36:08,699 - agent_pool.py - 514 - DEBUG - Stopped flag cleared — ready for new execution
2026-07-26 03:36:08,699 - execution_engine.py - 888 - DEBUG - engine.run() ENTRY - instance=Maine
2026-07-26 03:36:08,700 - agent_pool.py - 2185 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://127.0.0.1:1234/v1, concurrency_limit=0
2026-07-26 03:36:08,700 - execution_engine.py - 698 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-07-26 03:36:08,701 - execution_engine.py - 966 - DEBUG - [TURN_START] Calling _setup_turn for Maine
2026-07-26 03:36:08,702 - execution_engine.py - 1461 - INFO - [CACHE_REBUILD] Rebuilding working set for Maine (conv_len=174)
2026-07-26 03:36:08,702 - execution_engine.py - 1558 - INFO - [CACHE_REBUILD] System prompt content CHANGED for Maine (len 4696→5250, tail_diff, first_diff@163: orig=': N:\work\WD\AgentWorkspace
- Extra Paths (Read-Only): N:\wo' new=': N:\work\WD\AgentWorkspace
- Log Path: n:\work\WD\AgentWork')
2026-07-26 03:36:08,706 - agent_instance_logger.py - 486 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260726_002809.jsonl with 174 messages.
2026-07-26 03:36:08,707 - execution_engine.py - 1001 - DEBUG - [TURN_DONE] Got messages=174, llm_messages=174
2026-07-26 03:36:08,726 - execution_engine.py - 1084 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
2026-07-26 03:36:08,779 - base.py - 994 - INFO - Agent [Orchestrator] - ALL tokens: 58165, Available tokens: 88868
2026-07-26 03:37:50,717 - tool_dispatcher.py - 574 - DEBUG - call_agent nesting - Maine depth=1/10
2026-07-26 03:37:50,717 - tool_dispatcher.py - 389 - DEBUG - [SLOT_SYNC_RELEASE] Releasing slot for 'Maine' before running sync child 'final_review_settings_fix'
2026-07-26 03:37:50,719 - tool_dispatcher.py - 393 - DEBUG - [SLOT_SYNC_RELEASE] Slot released for 'Maine', active agents can now acquire
2026-07-26 03:37:50,720 - execution_engine.py - 4183 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target=final_review_settings_fix, class=reviewer, caller=Maine, nest_depth=1, force_fresh=False
2026-07-26 03:37:50,721 - lifecycle_manager.py - 194 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for final_review_settings_fix
2026-07-26 03:37:50,772 - execution_engine.py - 4265 - DEBUG - starting engine.run() for final_review_settings_fix
2026-07-26 03:37:50,777 - execution_engine.py - 888 - DEBUG - engine.run() ENTRY - instance=final_review_settings_fix
2026-07-26 03:37:50,777 - agent_pool.py - 2185 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=reviewer, instance_name=final_review_settings_fix, api_base=http://127.0.0.1:1234/v1, concurrency_limit=0
2026-07-26 03:37:50,777 - execution_engine.py - 698 - DEBUG - [SLOT_ACQUIRE] initial - instance=final_review_settings_fix, class=reviewer
2026-07-26 03:37:50,779 - execution_engine.py - 966 - DEBUG - [TURN_START] Calling _setup_turn for final_review_settings_fix
2026-07-26 03:37:50,779 - execution_engine.py - 1461 - INFO - [CACHE_REBUILD] Rebuilding working set for final_review_settings_fix (conv_len=2)
2026-07-26 03:37:50,780 - execution_engine.py - 1558 - INFO - [CACHE_REBUILD] System prompt content CHANGED for final_review_settings_fix (len 2643→3369, tail_diff)