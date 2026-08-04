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
[x] full audit of the API endpoint allocation logic/async agent calls, with full testing coverage — DONE: Architecture audit + 6 new test files (111 tests) covering endpoint scheduler stress, async shell failures, cursor rotation/fallback chain, rate-limit concurrency, generator finalization, and async result handling. All deterministic (no flaky markers), passing cleanly.
[ ] make view_image tool take in special arguments in path like `__screen_capture`, `__window_capture:PID` - self explanatory
[x] make out path helper that tools use resolve extra_rw/ro paths just like code_intepreter does
[x] make shell_cmd automatically launch on async mode if the agent sets timeout bigger than 60s and doesn't specify the mode as sync.
[x] check put shell_cmd async commands. add a `__wait` command to simply wait for next heartbeat - a no reply tool call basically (needed because most LLMs dont understand the concept of shutting up to get in SLEEPING state). make sure all these commands dont need justification field.
[x] decouple `enable skills` toggle from auto-skill logic, add `enable auto-skill generation`
[x] make a way to elegantly acquire a skill at run time (beside agent init)
[x] add a `range` argument to edit_file tool for the delete_and_insert mode so its less confusing than reusing `old_contnet`
[x] add quick and easy requirements file for default docker containers
[x] pass the supervisor's log file together with the name in the system prompt metadata, like: `Supervisor: Maine (orchestrator_Maine_20260731_023711.jsonl)` so sub-agents can easily find it if instructions are unclear — DONE: Added `_get_supervisor_log_filename()` helper in execution_engine.py. Modified `_build_session_metadata()` to append supervisor's log filename (basename only) when available. Graceful fallback to name-only when logger unavailable.
[x] check for multiple AC instances launched in parallel — implemented instance separation via AGENT_CASCADE_INSTANCE_ID env var + --instance-id CLI flag. Instance-specific paths for console logs, pool settings, telemetry dirs, agent logs. Validation prevents path traversal. 33 unit tests passing. See INSTANCE_SEPARATION_PLAN.md and agent_cascade/instance_id.py
[ ] extra work paths could be tied to each session, they'd have to be loaded when we load existing sessions from the metadata entry.
[ ] full inner loop mode audit, case by case investigation. make sure all modes add value or if they need trimming. they all need to catch actual loops (like [A,B,C,D,D,D] and never fall for repetitions that are NOT loops, like [A,D,B,C,D,E,D])


# BUGS:

- [x] telemetry `Output Tokens (est)` severely undercounts
- [x] instance separation precedence bug — when AGENT_CASCADE_INSTANCE_ID was set globally, starting AC without --instance-id would still use that value because empty string CLI default was treated as falsy. Fixed start_api_server.py and start_multi_agent.py to use None default so CLI can explicitly clear via --instance-id=.
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
- [x] odd useless truncation message on `list_dir` tool, should contain spillover path (should use helper truncation function like other tools, is there another one?): [TRUNCATED — Character limit exceeded.]. also needs the char limit added to the UI
- [x] overly aggressive stick to bottom function, active when streaming even when the user is actively scrolling up. it should NOT be fighting the user (fixed — immediate unlock on scroll up, visibility guard prevents auto-scroll on hidden tabs, lock released when tab becomes invisible)
- [x] sub agent kicked back to caller when the API connection dropped mid normal assistant message streaming (fixed — broadened retry_model_service_iterator in llm/base.py to catch all Exceptions during streaming, not just ModelServiceError; network errors now retry with exponential backoff up to max_retries)
- [x] inner loop detection does not seem to pick up if loop is happening within a tool call streaming. Fixed: in execution_engine.py streaming feed path, now also extracts and feeds function_call name+arguments into _total_text so the inner-loop detector sees tool-call argument content during streaming (previously only fed content+reasoning_content, which are empty for tool-call messages). Max-output-token guard also benefits from same fix.
- [x] Write a proper README.md that describes the project as a whole and offers easy install & use instructions (completed — comprehensive README covering architecture, features, installation, usage with correct port/endpoints, programmatic access, and troubleshooting)
- [x] context token estimation (the one in base.py) is off by about 10% less than what llama.cpp reports as receiving. (fixed — added CHAT_TEMPLATE_TOKEN_OVERHEAD=5 per message to get_message_stats() in utils/utils.py, and unified _count_history_tokens() in execution_engine.py to use get_message_stats() instead of raw qwen_count(); error dropped from ~37% to ~4%)
- [x] approval timeout doesn't seem to take into account the enable toggle in UI. Fixed: three compounding bugs — (1) frontend .length guard on single DOM element prevented enable flag from being sent, changed to direct truthy check; (2) added approval settings to POOL_SETTINGS_MAP for server→UI sync; (3) backend now restores approval timeout settings on startup via pending config pattern and persists runtime values on save. 
- [x] loading settings did not properly set the disabled tools for each agent. Fixed: added disabled_tools sync in syncPoolSettings() to apply server's pool_settings.json disabled_tools to UI on first load when no local preference exists, plus deferred renderAgentSelect until after syncPoolSettings runs to avoid race condition.
- [x] if the compressor assigned model does not have enough context window we dont fallback to next endpoint, we keep retrying the same point over and over
- [x] in UI change `Auto-Ask` to `Auto-Security` and make sure its also saved over refresh/restart like the other settings. Fixed: renamed label in index.html line 225, persistence was already working via localStorage key 'auto-security'.
- [x] switching auto-ask off during Security processing makes the notification tab pop back up again once the process has been aproved/denied, and it can't be closed back without refresh. Fixed: added guard in security_response handler to skip stale responses when approval already processed, plus cleanup of securityResponses/activeSecurityChecks in approveRequest/rejectRequest functions.
- [x] grep fails to use fast path sometimes. example: `{"pattern": "--swa-full", "path": "N:\\work\\WD\\llama.cpp"}`. Fixed: patterns starting with `-`/`--` were interpreted as CLI flags by ripgrep (exit code 2), causing silent fallback to slow Python path. Applied `-e` flag in `_try_subprocess_grep()` for both ripgrep and GNU grep branches to protect patterns from CLI parsing, plus added warning log for unexpected exit codes instead of silently falling back.
- [x] call_agent and dismiss_agent tool toggles do not get exported properly when we export/import settings. same for Auto-Security. Fixed: added auto_security to EXTRA_PERSIST_KEYS in config_handlers.py, included it in export payload (ws_handlers.py handle_export_settings), restored on import with defensive hasattr check (handle_import_settings), added bounded retry loops in app.js for both tool toggle re-render and auto-security toggle update when importing while settings panel may not be visible.
- [x] Improved `Self-Augmentation` skill to be prescriptive instead of aspirational. Key changes: concrete triggers ("when task mentions technology/framework/library/tool → scan_skills immediately"), clear distinction between load_skill tool (self-context) vs call_agent load_skill parameter (sub-agent context), proper tool invocation syntax, edge case guidance (no skills found, multiple matches), AUTO/NONE mode documentation, imperative language for skill creation. Went through 3 review iterations before commit.
- [x] lazy forced compression logic, it launches the compressor after an agent send a message past that limit, instead of checking right after a function return that it would put it past the threshold. Fixed: 4-layer defense-in-depth (see docs/compression_fix_plan.md). Phase 1: proactive post-tool check at end of _execute_detected_tools (88% threshold with reserve tokens). Phase 2: proactive check inside _drain_and_inject for async child results. Phase 3: hardened pre-LLM guard — fresh max_tokens, full recount when cache invalidated near threshold, cooldown override at 95%+, raises ContextWindowExceeded on max-attempts exceeded. Phase 4: non-destructive overflow detection in llm/base.py before silent truncation (3% tolerance). New settings: COMPRESSION_PROACTIVE_THRESHOLD=88%, COMPRESSION_CONTEXT_RESERVE_TOKENS=3000. All checks use _compression_lock around counts, wrapped in try/except. Passed full review cycle with critical fixes applied.
- [x] make sure `--- CURRENT AVAILABLE RESOURCES (Auto-Injected) ---` gets added to the system prompt before the skills, and simplify to `## AVAILABLE AGENTS`
- [ ] sub-agent spawned without UI tab — observed generalist_compressor_worker_20260804_121626.jsonl running in background with no visible tab. Tab should always appear when agent is spawned so user can monitor activity. Needs investigation: check tab creation logic in index.html/app.js and call_agent handler to see why some agents don't get tabs.
- [ ] i think dismissing all idle agents clears the UI tabs or running async agents too. needs a check.
- [ ] async agent calls that fail due to slot limitations leaves started child processes hanging. should have used the fallback to another API if the slot is busy, that was the whole job of the router.
```
[Agent 'phase1_reviewer_worker_child2' Failed]:
Timed out after 30s waiting for endpoint slot on https://opencode.ai/zen/v1. Current active count: 1, max allowed: 1. Currently held by: phase1_reviewer_worker (generalist)
```
- [x] the `Self-Augmentation` skill does not always get inserted on new agent call — fixed: now always injected when skills toggle is enabled, regardless of load_skill mode (AUTO or explicit list). Previously gated behind AUTO-only check.
- [x] add launched agent's log file name to the async call agent reply. Fixed: in _run_child_async() (tool_dispatcher.py), call pool.get_logger() before returning the confirmation message, include os.path.basename(log_path) in the message format.
- [x] remove the tools info from `list_agents`, make sure we catch all agent states (it's missing some). Fixed: removed tools/capabilities display from template listing in manager_ops.py; replaced binary ACTIVE/IDLE with full state mapping (IDLE/RUNNING/SLEEPING/COMPLETING/TERMINATED) plus HALTED overlay via _format_agent_status helper; added thread-safe get_state_name() accessor to AgentInstance.
- [x] UI refine: make the blinking motion of the activity bubble in the agent tabs (the dot in front of the name) only blink for the ones actively streaming. Fixed: changed pulse visibility logic in web_ui/app.js renderSubAgents() and updateControls() to use per-agent is_partial flag (true streaming state from backend) instead of execution state (active/SLEEPING). Pulse now only appears when agent is actively streaming LLM output.
- [ ] telemetry: add `Malformed` info to session-stats telemetry (how many times we hit the Auto-continue logic); in `loops detected` count the inner loops too

# Errors to investigate:

# root agent dropped out of sleeping during a long async call? (maybe because the child agent ran a compression that switched the model?)
2026-08-04 10:04:03,235 - config_handlers.py - 625 - DEBUG - [update_config] LLM config unchanged
2026-08-04 10:04:03,235 - config_handlers.py - 625 - DEBUG - [update_config] LLM config unchanged
2026-08-04 10:04:03,236 - config_handlers.py - 625 - DEBUG - [update_config] LLM config unchanged
2026-08-04 10:04:03,236 - config_handlers.py - 243 - WARNING - [THREAD_POOL] resize_executor skipped — executor is None (pool just initialized?)
2026-08-04 10:04:03,237 - __init__.py - 128 - INFO - [Workspace] Tiered folders updated: RO=0, RW=3
2026-08-04 10:04:03,237 - agent_pool.py - 2026 - DEBUG - [CONFIG] Global configuration version incremented to 1
2026-08-04 10:04:03,237 - config_handlers.py - 148 - DEBUG - [work_folders] Extra work folders unchanged
2026-08-04 10:04:32,722 - agent_pool.py - 692 - DEBUG - Idle checker restarted
2026-08-04 10:04:32,722 - agent_pool.py - 708 - DEBUG - Async registry executor recreated
2026-08-04 10:04:32,724 - agent_pool.py - 711 - DEBUG - Stopped flag cleared — ready for new execution
2026-08-04 10:04:32,724 - ws_handlers.py - 208 - DEBUG - Starting generation gen_id=1, instances={'Maine': 'IDLE'}, active_stack=0
2026-08-04 10:04:32,725 - agent_pool.py - 692 - DEBUG - Idle checker restarted
2026-08-04 10:04:32,725 - agent_pool.py - 708 - DEBUG - Async registry executor recreated
2026-08-04 10:04:32,726 - agent_pool.py - 711 - DEBUG - Stopped flag cleared — ready for new execution
2026-08-04 10:04:32,726 - execution_engine.py - 1051 - DEBUG - engine.run() ENTRY - instance=Maine
2026-08-04 10:04:32,726 - agent_pool.py - 2413 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://127.0.0.1:1234/v1, concurrency_limit=0
2026-08-04 10:04:32,726 - execution_engine.py - 857 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-08-04 10:04:32,727 - execution_engine.py - 1129 - DEBUG - [TURN_START] Calling _setup_turn for Maine
2026-08-04 10:04:32,734 - execution_engine.py - 1629 - INFO - [CACHE_REBUILD] Rebuilding working set for Maine (conv_len=1)
2026-08-04 10:04:32,735 - execution_engine.py - 1740 - INFO - [CACHE_REBUILD] System prompt content CHANGED for Maine (len 7587→8453, first_diff@8: orig='You are Orchestrator.
Technical lead a' new='You are Maine.
Technical lead and oper')
2026-08-04 10:04:32,737 - agent_instance_logger.py - 486 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260804_100402.jsonl with 1 messages.
2026-08-04 10:04:32,737 - execution_engine.py - 1164 - DEBUG - [TURN_DONE] Got messages=1, llm_messages=1
2026-08-04 10:04:32,746 - execution_engine.py - 1247 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
2026-08-04 10:04:32,749 - execution_engine.py - 2951 - INFO - Endpoint allocation updated for orchestrator: {'endpoint': 'LMS-27B-unc-MTP', 'api_base': 'http://127.0.0.1:1234/v1', 'model': 'qwen3.6-27b-fable-fus-mtp', 'max_input_tokens': 90000, 'rate_limit_rpm': 0, 'concurrency_limit': 0, 'prev_max_input_tokens': 0}
2026-08-04 10:04:32,749 - base.py - 1031 - INFO - Agent [Orchestrator] - ALL tokens: 30, Available tokens: 88152
2026-08-04 10:04:32,750 - oai.py - 77 - DEBUG - [CACHE] MISS creating new client key=('http://127.0.0.1:1234/v1', 'EMPTY')
2026-08-04 10:04:45,860 - base.py - 1031 - INFO - Agent [Orchestrator] - ALL tokens: 907, Available tokens: 88152
2026-08-04 10:04:54,716 - execution_engine.py - 1546 - DEBUG - EXIT - Maine RUNNING→IDLE
2026-08-04 10:05:48,850 - agent_pool.py - 692 - DEBUG - Idle checker restarted
2026-08-04 10:05:48,850 - agent_pool.py - 708 - DEBUG - Async registry executor recreated
2026-08-04 10:05:48,853 - agent_pool.py - 711 - DEBUG - Stopped flag cleared — ready for new execution
2026-08-04 10:05:48,853 - ws_handlers.py - 208 - DEBUG - Starting generation gen_id=2, instances={'Maine': 'IDLE'}, active_stack=0
2026-08-04 10:05:48,854 - agent_pool.py - 692 - DEBUG - Idle checker restarted
2026-08-04 10:05:48,854 - agent_pool.py - 708 - DEBUG - Async registry executor recreated
2026-08-04 10:05:48,855 - agent_pool.py - 711 - DEBUG - Stopped flag cleared — ready for new execution
2026-08-04 10:05:48,855 - execution_engine.py - 1051 - DEBUG - engine.run() ENTRY - instance=Maine
2026-08-04 10:05:48,855 - agent_pool.py - 2413 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://127.0.0.1:1234/v1, concurrency_limit=0
2026-08-04 10:05:48,856 - execution_engine.py - 857 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-08-04 10:05:48,856 - execution_engine.py - 1129 - DEBUG - [TURN_START] Calling _setup_turn for Maine
2026-08-04 10:05:48,856 - execution_engine.py - 1624 - DEBUG - [CACHE_HIT] Reusing cached messages=5, llm_messages=5
2026-08-04 10:05:48,857 - execution_engine.py - 1164 - DEBUG - [TURN_DONE] Got messages=5, llm_messages=5
2026-08-04 10:05:48,867 - execution_engine.py - 1247 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
2026-08-04 10:05:48,869 - base.py - 1031 - INFO - Agent [Orchestrator] - ALL tokens: 1086, Available tokens: 88152
2026-08-04 10:06:27,506 - tool_dispatcher.py - 665 - DEBUG - call_agent nesting - Maine depth=1/10
2026-08-04 10:06:28,834 - tool_dispatcher.py - 538 - DEBUG - Taking ASYNC path - Maine calls compression_timing_investigator/researcher at depth 1
2026-08-04 10:06:28,837 - tool_dispatcher.py - 552 - DEBUG - ASYNC - compression_timing_investigator launched by Maine
2026-08-04 10:06:28,837 - tool_dispatcher.py - 130 - DEBUG - handle_call_agent returned type=str
2026-08-04 10:06:28,843 - base.py - 1031 - INFO - Agent [Orchestrator] - ALL tokens: 1879, Available tokens: 88152
2026-08-04 10:06:28,869 - execution_engine.py - 4389 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target=compression_timing_investigator, class=researcher, caller=Maine, nest_depth=1, force_fresh=False
2026-08-04 10:06:28,869 - lifecycle_manager.py - 194 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for compression_timing_investigator
2026-08-04 10:06:28,887 - execution_engine.py - 4476 - DEBUG - starting engine.run() for compression_timing_investigator
2026-08-04 10:06:28,888 - execution_engine.py - 1051 - DEBUG - engine.run() ENTRY - instance=compression_timing_investigator
2026-08-04 10:06:28,888 - agent_pool.py - 2413 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=researcher, instance_name=compression_timing_investigator, api_base=https://opencode.ai/zen/v1, concurrency_limit=1
2026-08-04 10:06:28,889 - execution_engine.py - 857 - DEBUG - [SLOT_ACQUIRE] initial - instance=compression_timing_investigator, class=researcher
2026-08-04 10:06:28,889 - execution_engine.py - 1129 - DEBUG - [TURN_START] Calling _setup_turn for compression_timing_investigator
2026-08-04 10:06:28,889 - execution_engine.py - 1629 - INFO - [CACHE_REBUILD] Rebuilding working set for compression_timing_investigator (conv_len=2)
2026-08-04 10:06:28,890 - execution_engine.py - 1740 - INFO - [CACHE_REBUILD] System prompt content CHANGED for compression_timing_investigator (len 4167→4698, tail_diff)
2026-08-04 10:06:28,890 - agent_instance_logger.py - 486 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\researcher_compression_timing_investigator_20260804_100628.jsonl with 2 messages.
2026-08-04 10:06:28,891 - execution_engine.py - 1164 - DEBUG - [TURN_DONE] Got messages=2, llm_messages=2
2026-08-04 10:06:28,894 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 579, Available tokens: 164502
2026-08-04 10:06:28,897 - oai.py - 77 - DEBUG - [CACHE] MISS creating new client key=('https://opencode.ai/zen/v1', 'sk-jnrvmjpYtgqgzOGcImpuN4rJwiLmCQKimU9gCOG6ksDYhMoOaVylobuWHopaEHnv')
2026-08-04 10:06:31,364 - execution_engine.py - 3842 - DEBUG - Pending async tools for Maine. Transitioning to SLEEPING.
2026-08-04 10:06:31,365 - execution_engine.py - 4152 - DEBUG - WAITING for background tools - Maine (0.0s)
2026-08-04 10:06:32,158 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 2738, Available tokens: 164502
2026-08-04 10:06:35,990 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 10323, Available tokens: 164502
2026-08-04 10:06:36,417 - execution_engine.py - 4146 - INFO - SLEEPING - Maine waiting 5.0s for background tools
2026-08-04 10:06:36,417 - execution_engine.py - 4152 - DEBUG - WAITING for background tools - Maine (5.0s)
2026-08-04 10:06:41,461 - execution_engine.py - 4146 - INFO - SLEEPING - Maine waiting 10.1s for background tools
2026-08-04 10:06:41,461 - execution_engine.py - 4152 - DEBUG - WAITING for background tools - Maine (10.1s)
2026-08-04 10:06:43,103 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 15409, Available tokens: 164502
2026-08-04 10:06:46,509 - execution_engine.py - 4146 - INFO - SLEEPING - Maine waiting 15.1s for background tools
2026-08-04 10:06:46,509 - execution_engine.py - 4152 - DEBUG - WAITING for background tools - Maine (15.1s)
...
2026-08-04 10:13:37,213 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 141575, Available tokens: 164502
2026-08-04 10:14:47,281 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 142932, Available tokens: 164502
2026-08-04 10:16:27,364 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 144218, Available tokens: 164502
2026-08-04 10:16:32,103 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 148111, Available tokens: 164502
2026-08-04 10:17:46,671 - lifecycle_manager.py - 194 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for Compressor_1
2026-08-04 10:17:46,827 - execution_engine.py - 1051 - DEBUG - engine.run() ENTRY - instance=Compressor_1
2026-08-04 10:17:46,840 - execution_engine.py - 1129 - DEBUG - [TURN_START] Calling _setup_turn for Compressor_1
2026-08-04 10:17:46,842 - execution_engine.py - 1629 - INFO - [CACHE_REBUILD] Rebuilding working set for Compressor_1 (conv_len=2)
2026-08-04 10:17:46,842 - execution_engine.py - 1767 - DEBUG - [CACHE_REBUILD] System prompt for Compressor_1 textually identical — skipping pool update
2026-08-04 10:17:46,842 - execution_engine.py - 1164 - DEBUG - [TURN_DONE] Got messages=2, llm_messages=2
2026-08-04 10:17:46,845 - base.py - 1031 - INFO - Agent [Compressor] - ALL tokens: 91745, Available tokens: 124582
2026-08-04 10:19:26,448 - execution_engine.py - 1546 - DEBUG - EXIT - Compressor_1 RUNNING→IDLE
2026-08-04 10:19:26,553 - handler.py - 427 - DEBUG - Logger sync after compress_context tool for 'compression_timing_investigator': pool_len=104, using reset_history() for full sync
2026-08-04 10:19:26,594 - agent_instance_logger.py - 594 - INFO - Synced compression marker in n:\work\WD\AgentWorkspace\logs\researcher_compression_timing_investigator_20260804_100628.jsonl (204 messages).
2026-08-04 10:19:26,619 - execution_engine.py - 2190 - DEBUG - Rebuilt working sets for compression_timing_investigator: messages=104, llm_messages=104
2026-08-04 10:19:26,628 - execution_engine.py - 2190 - DEBUG - Rebuilt working sets for compression_timing_investigator: messages=104, llm_messages=104
2026-08-04 10:19:26,643 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 59915, Available tokens: 164502
2026-08-04 10:19:58,144 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 62985, Available tokens: 164502
2026-08-04 10:20:02,001 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 64173, Available tokens: 164502
2026-08-04 10:20:06,448 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 65850, Available tokens: 164502
2026-08-04 10:21:02,480 - agent_pool.py - 2977 - INFO - [idle_checker] Auto-dismissing idle system agent (Compressor) 'Compressor_1' (idle for 96s, threshold=60s)
2026-08-04 10:21:02,480 - agent_pool.py - 810 - DEBUG - Instance conversation cleanup key missing (expected): 'Compressor_1'
2026-08-04 10:21:02,481 - agent_pool.py - 2902 - INFO - [idle_checker] Auto-dismissed 1 idle agent(s): Compressor_1
2026-08-04 10:21:33,660 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 75740, Available tokens: 164502
2026-08-04 10:21:39,526 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 77234, Available tokens: 164502
2026-08-04 10:21:44,840 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 77841, Available tokens: 164502
2026-08-04 10:21:50,527 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 78593, Available tokens: 164502
2026-08-04 10:21:56,706 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 79825, Available tokens: 164502
2026-08-04 10:22:10,417 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 80604, Available tokens: 164502
2026-08-04 10:22:14,466 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 81133, Available tokens: 164502
2026-08-04 10:22:18,687 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 81574, Available tokens: 164502
2026-08-04 10:22:23,641 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 82253, Available tokens: 164502
2026-08-04 10:22:28,939 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 82925, Available tokens: 164502
2026-08-04 10:22:33,236 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 83466, Available tokens: 164502
2026-08-04 10:22:38,012 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 84282, Available tokens: 164502
2026-08-04 10:22:41,901 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 84636, Available tokens: 164502
2026-08-04 10:22:47,389 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 84964, Available tokens: 164502
2026-08-04 10:22:53,241 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 85336, Available tokens: 164502
2026-08-04 10:22:59,865 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 85967, Available tokens: 164502
2026-08-04 10:23:04,020 - base.py - 1031 - INFO - Agent [Researcher] - ALL tokens: 87132, Available tokens: 164502
2026-08-04 10:23:14,814 - execution_engine.py - 1546 - DEBUG - EXIT - compression_timing_investigator RUNNING→IDLE
2026-08-04 10:23:14,841 - execution_engine.py - 4591 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=compression_timing_investigator, reason=completed, inst_type=AgentInstance, conv_len=2, final_resp_len=262

