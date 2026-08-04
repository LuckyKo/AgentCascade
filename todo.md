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
- [x] odd useless truncation message on `list_dir` tool, should contain spillover path (should use helper truncation function like other tools, is there another one?): [TRUNCATED — Character limit exceeded.]. also needs the char limit added to the UI
- [x] overly aggressive stick to bottom function, active when streaming even when the user is actively scrolling up. it should NOT be fighting the user (fixed — immediate unlock on scroll up, visibility guard prevents auto-scroll on hidden tabs, lock released when tab becomes invisible)
- [x] sub agent kicked back to caller when the API connection dropped mid normal assistant message streaming (fixed — broadened retry_model_service_iterator in llm/base.py to catch all Exceptions during streaming, not just ModelServiceError; network errors now retry with exponential backoff up to max_retries)
- [ ] inner loop detection does not seem to pick up if loop is happening within a tool call streaming. (very rare event, but worth noting)
- [x] Write a proper README.md that describes the project as a whole and offers easy install & use instructions (completed — comprehensive README covering architecture, features, installation, usage with correct port/endpoints, programmatic access, and troubleshooting)
- [x] context token estimation (the one in base.py) is off by about 10% less than what llama.cpp reports as receiving. (fixed — added CHAT_TEMPLATE_TOKEN_OVERHEAD=5 per message to get_message_stats() in utils/utils.py, and unified _count_history_tokens() in execution_engine.py to use get_message_stats() instead of raw qwen_count(); error dropped from ~37% to ~4%)
- [ ] approval timeout doesn't seem to take into account the enable toggle in UI 
- [x] loading settings did not properly set the disabled tools for each agent. Fixed: added disabled_tools sync in syncPoolSettings() to apply server's pool_settings.json disabled_tools to UI on first load when no local preference exists, plus deferred renderAgentSelect until after syncPoolSettings runs to avoid race condition.
- [ ] if the compressor assigned model does not have enough context window we dont fallback to next endpoint, we keep retrying the same point over and over
- [x] in UI change `Auto-Ask` to `Auto-Security` and make sure its also saved over refresh/restart like the other settings. Fixed: renamed label in index.html line 225, persistence was already working via localStorage key 'auto-security'.
- [x] switching auto-ask off during Security processing makes the notification tab pop back up again once the process has been aproved/denied, and it can't be closed back without refresh. Fixed: added guard in security_response handler to skip stale responses when approval already processed, plus cleanup of securityResponses/activeSecurityChecks in approveRequest/rejectRequest functions.
- [x] grep fails to use fast path sometimes. example: `{"pattern": "--swa-full", "path": "N:\\work\\WD\\llama.cpp"}`. Fixed: patterns starting with `-`/`--` were interpreted as CLI flags by ripgrep (exit code 2), causing silent fallback to slow Python path. Applied `-e` flag in `_try_subprocess_grep()` for both ripgrep and GNU grep branches to protect patterns from CLI parsing, plus added warning log for unexpected exit codes instead of silently falling back.
- [x] call_agent and dismiss_agent tool toggles do not get exported properly when we export/import settings. same for Auto-Security. Fixed: added auto_security to EXTRA_PERSIST_KEYS in config_handlers.py, included it in export payload (ws_handlers.py handle_export_settings), restored on import with defensive hasattr check (handle_import_settings), added bounded retry loops in app.js for both tool toggle re-render and auto-security toggle update when importing while settings panel may not be visible.
- [x] Improved `Self-Augmentation` skill to be prescriptive instead of aspirational. Key changes: concrete triggers ("when task mentions technology/framework/library/tool → scan_skills immediately"), clear distinction between load_skill tool (self-context) vs call_agent load_skill parameter (sub-agent context), proper tool invocation syntax, edge case guidance (no skills found, multiple matches), AUTO/NONE mode documentation, imperative language for skill creation. Went through 3 review iterations before commit.
- [ ] lazy forced compression logic, it launches the compressor after an agent send a message past that limit, instead of checking right after a function return that it would put it past the threshold.
- [x] make sure `--- CURRENT AVAILABLE RESOURCES (Auto-Injected) ---` gets added to the system prompt before the skills, and simplify to `## AVAILABLE AGENTS`
- [ ] i think dismissing all idle agents clears the UI tabs or running async agents too. needs a check.
- [ ] async agent calls that fail due to slot limitations leaves started child processes hanging. should have used the fallback to another API if the slot is busy, that was the whole job of the router.
```
[Agent 'phase1_reviewer_worker_child2' Failed]:
Timed out after 30s waiting for endpoint slot on https://opencode.ai/zen/v1. Current active count: 1, max allowed: 1. Currently held by: phase1_reviewer_worker (generalist)
```
- [x] the `Self-Augmentation` skill does not always get inserted on new agent call — fixed: now always injected when skills toggle is enabled, regardless of load_skill mode (AUTO or explicit list). Previously gated behind AUTO-only check.
- [x] add launched agent's log file name to the async call agent reply. Fixed: in _run_child_async() (tool_dispatcher.py), call pool.get_logger() before returning the confirmation message, include os.path.basename(log_path) in the message format.
- [x] remove the tools info from `list_agents`, make sure we catch all agent states (it's missing some). Fixed: removed tools/capabilities display from template listing in manager_ops.py; replaced binary ACTIVE/IDLE with full state mapping (IDLE/RUNNING/SLEEPING/COMPLETING/TERMINATED) plus HALTED overlay via _format_agent_status helper; added thread-safe get_state_name() accessor to AgentInstance.
- [ ] UI refine: make the blinking motion of the activity bubble in the agent tabs (the dot in front of the name) only blink for the ones actively streaming
- [ ] telemetry: add `Malformed` info to session-stats telemetry (how many times we hit the Auto-continue logic); in `loops detected` count the inner loops too

# Errors to investigate:


