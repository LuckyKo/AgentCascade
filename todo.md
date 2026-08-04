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
- [ ] add inner loop counter to telemetry's loop detected
- [x] odd useless truncation message on `list_dir` tool, should contain spillover path (should use helper truncation function like other tools, is there another one?): [TRUNCATED — Character limit exceeded.]. also needs the char limit added to the UI
- [x] overly aggressive stick to bottom function, active when streaming even when the user is actively scrolling up. it should NOT be fighting the user (fixed — immediate unlock on scroll up, visibility guard prevents auto-scroll on hidden tabs, lock released when tab becomes invisible)
- [x] sub agent kicked back to caller when the API connection dropped mid normal assistant message streaming (fixed — broadened retry_model_service_iterator in llm/base.py to catch all Exceptions during streaming, not just ModelServiceError; network errors now retry with exponential backoff up to max_retries)
- [ ] inner loop detection does not seem to pick up if loop is happening within a tool call.
- [x] Write a proper README.md that describes the project as a whole and offers easy install & use instructions (completed — comprehensive README covering architecture, features, installation, usage with correct port/endpoints, programmatic access, and troubleshooting)
- [x] context token estimation (the one in base.py) is off by about 10% less than what llama.cpp reports as receiving. (fixed — added CHAT_TEMPLATE_TOKEN_OVERHEAD=5 per message to get_message_stats() in utils/utils.py, and unified _count_history_tokens() in execution_engine.py to use get_message_stats() instead of raw qwen_count(); error dropped from ~37% to ~4%)
- [ ] approval timeout doesn't seem to take into account the enable toggle in UI 
- [x] loading settings did not properly set the disabled tools for each agent. Fixed: added disabled_tools sync in syncPoolSettings() to apply server's pool_settings.json disabled_tools to UI on first load when no local preference exists, plus deferred renderAgentSelect until after syncPoolSettings runs to avoid race condition.
- [ ] if the compressor assigned model does not have enough context window we dont fallback to next endpoint, we keep retrying the same point over and over
- [x] in UI change `Auto-Ask` to `Auto-Security` and make sure its also saved over refresh/restart like the other settings. Fixed: renamed label in index.html line 225, persistence was already working via localStorage key 'auto-security'.
- [x] switching auto-ask off during Security processing makes the notification tab pop back up again once the process has been aproved/denied, and it can't be closed back without refresh. Fixed: added guard in security_response handler to skip stale responses when approval already processed, plus cleanup of securityResponses/activeSecurityChecks in approveRequest/rejectRequest functions.
- [ ] grep fails to use fast path sometimes. example:
```
{
  "pattern": "--swa-full",
  "path": "N:\\work\\WD\\llama.cpp"
}
```
- [x] call_agent and dismiss_agent tool toggles do not get exported properly when we export/import settings. same for Auto-Security. Fixed: added auto_security to EXTRA_PERSIST_KEYS in config_handlers.py, included it in export payload (ws_handlers.py handle_export_settings), restored on import with defensive hasattr check (handle_import_settings), added bounded retry loops in app.js for both tool toggle re-render and auto-security toggle update when importing while settings panel may not be visible.
- [x] Improved `Self-Augmentation` skill to be prescriptive instead of aspirational. Key changes: concrete triggers ("when task mentions technology/framework/library/tool → scan_skills immediately"), clear distinction between load_skill tool (self-context) vs call_agent load_skill parameter (sub-agent context), proper tool invocation syntax, edge case guidance (no skills found, multiple matches), AUTO/NONE mode documentation, imperative language for skill creation. Went through 3 review iterations before commit.
- [ ] lazy forced compression logic, it launches the compressor after an agent send a message past that limit, instead of checking right after a function return that it would put it past the threshold.
- [ ] make sure `--- CURRENT AVAILABLE RESOURCES (Auto-Injected) ---` gets added to the system prompt before the skills, and simplify to `## AVAILABLE AGENTS`
- [ ] i think dismissing all idle agents clears the UI tabs or running async agents too. needs a check.
- [ ] async agent calls that fail due to slot limitations leaves started child processes hanging. should have used the fallback to another API if the slot is busy, that was the whole job of the router.
```
[Agent 'phase1_reviewer_worker_child2' Failed]:
Timed out after 30s waiting for endpoint slot on https://opencode.ai/zen/v1. Current active count: 1, max allowed: 1. Currently held by: phase1_reviewer_worker (generalist)
```

# Errors to investigate:


## Full reprocess on high LCP similarity (llama-autoloader log) — FIXED

**Problem:** After state restore, SWA models fully reprocessed ~81K tokens (~108s) despite LCP similarity = 0.998 and valid restored KV cache. Root cause: llama.cpp's RAM-side checkpoint list isn't persisted by slot save/restore; for SWA models, empty checkpoints triggers `do_reset = true`.

**Fix:** Add `--swa-full` to model args (via sidecar JSON). Uses full-size SWA cache instead of windowed, bypassing the checkpoint requirement. No measurable performance impact (18.8 tps with or without it on long context creative run).

**Example config for Qwen3.6-27B:**
```json
{ "args": "--swa-full" }
```

See DOCUMENTATION.md in llama-autoloader for details.

Log dump preserved below for reference:

[32mINFO[0m:     127.0.0.1:54650 - "[1mPOST /v1/models/qwen3.6-27b-fable-fus-mtp/state/save HTTP/1.1[0m" [32m200 OK[0m
2026-08-03 13:12:34,457 [INFO] autoloader: List states requested for model=qwen3.6-27b-fable-fus-mtp, found 5 state(s)
[32mINFO[0m:     127.0.0.1:54777 - "[1mGET /v1/models/qwen3.6-27b-fable-fus-mtp/state HTTP/1.1[0m" [32m200 OK[0m
2026-08-03 13:12:34,816 [INFO] autoloader: Restoring slot 0 state from file: N:\work\stuff\Beta\llama-autoloader\states\Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf.Maine_1785751817.bin
2026-08-03 13:12:37,967 [INFO] httpx: HTTP Request: POST http://127.0.0.1:9163/slots/0?action=restore "HTTP/1.1 200 OK"
2026-08-03 13:12:37,967 [INFO] autoloader: State restored for model=qwen3.6-27b-fable-fus-mtp label=Maine_1785751817 path=Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf.Maine_1785751817.bin
[32mINFO[0m:     127.0.0.1:54780 - "[1mPOST /v1/models/qwen3.6-27b-fable-fus-mtp/state/load HTTP/1.1[0m" [32m200 OK[0m
2026-08-03 13:12:38,575 [INFO] autoloader: --> Incoming POST request to '/v1/chat/completions' (model param input: 'Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf')
2026-08-03 13:12:38,575 [INFO] autoloader: Resolved model ID: 'Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf'
2026-08-03 13:12:38,577 [INFO] autoloader: Forwarding POST /v1/chat/completions -> llama-server on port 9163
2026-08-03 13:12:38,943 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.08.463.552 I slot get_availabl: id  0 | task -1 | selected slot by LCP similarity, sim_best = 0.998 (> 0.100 thold), f_keep = 1.000
2026-08-03 13:12:38,946 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.08.465.876 I slot launch_slot_: id  0 | task 845 | processing task, is_child = 0
2026-08-03 13:12:38,947 [INFO] httpx: HTTP Request: POST http://127.0.0.1:9163/v1/chat/completions "HTTP/1.1 200 OK"
[32mINFO[0m:     127.0.0.1:54946 - "[1mPOST /v1/chat/completions HTTP/1.1[0m" [32m200 OK[0m
2026-08-03 13:12:42,930 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.12.449.238 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =   4096, progress = 0.05, t =   3.98 s / 1028.28 tokens per second
2026-08-03 13:12:44,853 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.14.372.519 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =   6144, progress = 0.07, t =   5.91 s / 1040.19 tokens per second
2026-08-03 13:12:46,475 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.15.995.159 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =   7881, progress = 0.10, t =   7.53 s / 1046.72 tokens per second
2026-08-03 13:12:46,686 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.16.206.029 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =   7976, progress = 0.10, t =   7.74 s / 1030.47 tokens per second
2026-08-03 13:12:48,690 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.18.209.920 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  10024, progress = 0.12, t =   9.74 s / 1028.73 tokens per second
2026-08-03 13:12:50,734 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.20.253.710 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  12072, progress = 0.15, t =  11.79 s / 1024.11 tokens per second
2026-08-03 13:12:52,819 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.22.339.564 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  14120, progress = 0.17, t =  13.87 s / 1017.76 tokens per second
2026-08-03 13:12:54,943 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.24.462.629 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  16168, progress = 0.20, t =  16.00 s / 1010.71 tokens per second
2026-08-03 13:12:55,926 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.25.446.432 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  17032, progress = 0.21, t =  16.98 s / 1003.03 tokens per second
2026-08-03 13:12:57,493 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.27.012.757 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  18352, progress = 0.22, t =  18.55 s / 989.49 tokens per second
2026-08-03 13:12:59,699 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.29.219.324 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  20400, progress = 0.25, t =  20.75 s / 982.97 tokens per second
2026-08-03 13:13:01,944 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.31.463.940 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  22448, progress = 0.27, t =  23.00 s / 976.08 tokens per second
2026-08-03 13:13:04,219 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.33.738.973 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  24496, progress = 0.30, t =  25.27 s / 969.25 tokens per second
2026-08-03 13:13:05,460 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.34.980.298 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  25394, progress = 0.31, t =  26.51 s / 957.74 tokens per second
2026-08-03 13:13:05,687 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.35.207.831 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  25446, progress = 0.31, t =  26.74 s / 951.54 tokens per second
2026-08-03 13:13:08,021 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.37.540.471 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  27494, progress = 0.34, t =  29.07 s / 945.64 tokens per second
2026-08-03 13:13:10,391 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.39.910.665 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  29542, progress = 0.36, t =  31.44 s / 939.49 tokens per second
2026-08-03 13:13:12,797 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.42.317.637 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  31590, progress = 0.39, t =  33.85 s / 933.19 tokens per second
2026-08-03 13:13:15,244 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.44.763.868 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  33638, progress = 0.41, t =  36.30 s / 926.72 tokens per second
2026-08-03 13:13:16,846 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.46.366.212 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  34909, progress = 0.43, t =  37.90 s / 921.07 tokens per second
2026-08-03 13:13:19,472 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.48.992.110 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  36957, progress = 0.45, t =  40.53 s / 911.93 tokens per second
2026-08-03 13:13:22,012 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.51.532.628 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  39005, progress = 0.48, t =  43.07 s / 905.69 tokens per second
2026-08-03 13:13:24,597 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.54.116.887 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  41053, progress = 0.50, t =  45.65 s / 899.28 tokens per second
2026-08-03 13:13:27,212 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.56.732.495 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  43101, progress = 0.53, t =  48.27 s / 892.98 tokens per second
2026-08-03 13:13:28,069 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.57.589.343 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  43624, progress = 0.53, t =  49.12 s / 888.05 tokens per second
2026-08-03 13:13:29,114 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.58.633.980 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  44272, progress = 0.54, t =  50.17 s / 882.47 tokens per second
2026-08-03 13:13:31,789 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.01.309.442 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  46320, progress = 0.57, t =  52.84 s / 876.55 tokens per second
2026-08-03 13:13:34,499 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.04.019.396 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  48368, progress = 0.59, t =  55.55 s / 870.66 tokens per second
2026-08-03 13:13:37,247 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.06.766.579 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  50416, progress = 0.62, t =  58.30 s / 864.76 tokens per second
2026-08-03 13:13:39,516 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.09.036.047 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  51863, progress = 0.63, t =  60.57 s / 856.25 tokens per second
2026-08-03 13:13:40,161 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.09.680.723 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  52060, progress = 0.64, t =  61.21 s / 850.45 tokens per second
2026-08-03 13:13:43,002 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.12.521.567 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  54108, progress = 0.66, t =  64.06 s / 844.70 tokens per second
2026-08-03 13:13:45,878 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.15.397.786 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  56156, progress = 0.69, t =  66.93 s / 839.00 tokens per second
2026-08-03 13:13:48,796 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.18.316.238 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  58204, progress = 0.71, t =  69.85 s / 833.27 tokens per second
2026-08-03 13:13:51,717 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.21.237.025 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  60115, progress = 0.73, t =  72.77 s / 826.08 tokens per second
2026-08-03 13:13:53,054 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.22.574.474 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  60810, progress = 0.74, t =  74.11 s / 820.55 tokens per second
2026-08-03 13:13:56,055 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.25.574.579 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  62858, progress = 0.77, t =  77.11 s / 815.19 tokens per second
2026-08-03 13:13:59,093 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.28.613.478 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  64906, progress = 0.79, t =  80.15 s / 809.83 tokens per second
2026-08-03 13:14:02,168 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.31.688.382 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  66954, progress = 0.82, t =  83.22 s / 804.52 tokens per second
2026-08-03 13:14:05,291 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.34.810.378 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  69002, progress = 0.84, t =  86.34 s / 799.15 tokens per second
2026-08-03 13:14:05,798 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.35.318.261 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  69255, progress = 0.85, t =  86.85 s / 797.39 tokens per second
2026-08-03 13:14:08,534 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.38.053.795 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  70890, progress = 0.87, t =  89.59 s / 791.29 tokens per second
2026-08-03 13:14:11,724 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.41.244.349 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  72938, progress = 0.89, t =  92.78 s / 786.15 tokens per second
2026-08-03 13:14:14,948 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.44.467.579 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  74986, progress = 0.92, t =  96.00 s / 781.09 tokens per second
2026-08-03 13:14:18,207 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.47.726.681 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  77034, progress = 0.94, t =  99.26 s / 776.08 tokens per second
2026-08-03 13:14:21,511 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.51.030.803 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  79082, progress = 0.97, t = 102.56 s / 771.04 tokens per second
2026-08-03 13:14:23,509 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.53.029.679 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  80323, progress = 0.98, t = 104.56 s / 768.17 tokens per second
2026-08-03 13:14:25,605 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.55.125.025 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  81424, progress = 0.99, t = 106.66 s / 763.40 tokens per second
2026-08-03 13:14:26,555 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.56.075.809 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  81784, progress = 1.00, t = 107.61 s / 760.00 tokens per second
2026-08-03 13:14:27,238 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 4.56.758.396 I slot print_timing: id  0 | task 845 | prompt processing, n_tokens =  81936, progress = 1.00, t = 108.29 s / 756.62 tokens per second
2026-08-03 13:14:31,505 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.01.024.258 I slot print_timing: id  0 | task 845 | n_decoded =    102, tg =  25.94 t/s, tg_3s =  25.94 t/s
2026-08-03 13:14:34,549 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.04.068.866 I slot print_timing: id  0 | task 845 | n_decoded =    175, tg =  25.08 t/s, tg_3s =  23.98 t/s
2026-08-03 13:14:37,615 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.07.134.943 I slot print_timing: id  0 | task 845 | n_decoded =    228, tg =  22.70 t/s, tg_3s =  17.29 t/s
2026-08-03 13:14:40,713 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.10.233.433 I slot print_timing: id  0 | task 845 | n_decoded =    292, tg =  22.22 t/s, tg_3s =  20.66 t/s
2026-08-03 13:14:43,784 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.13.303.999 I slot print_timing: id  0 | task 845 | n_decoded =    364, tg =  22.45 t/s, tg_3s =  23.45 t/s
2026-08-03 13:14:43,925 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.13.444.892 I slot print_timing: id  0 | task 845 | prompt eval time =  108625.62 ms / 81940 tokens (    1.33 ms per token,   754.33 tokens per second)
2026-08-03 13:14:43,926 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.13.444.898 I slot print_timing: id  0 | task 845 |        eval time =   16353.17 ms /   368 tokens (   44.44 ms per token,    22.50 tokens per second)
2026-08-03 13:14:43,927 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.13.444.899 I slot print_timing: id  0 | task 845 |       total time =  124978.79 ms / 82308 tokens
2026-08-03 13:14:43,927 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.13.444.900 I slot print_timing: id  0 | task 845 |    graphs reused =        526
2026-08-03 13:14:43,927 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.13.444.905 I slot print_timing: id  0 | task 845 | draft acceptance = 0.98333 (  236 accepted /   240 generated), mean len =  3.41
2026-08-03 13:14:43,928 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 5.13.447.372 I slot      release: id  0 | task 845 | stop processing: n_tokens = 82307, truncated = 0
2026-08-03 13:14:44,434 [INFO] autoloader: Saving slot 0 state to file: N:\work\stuff\Beta\llama-autoloader\states\Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf.Maine_1785752083.bin
2026-08-03 13:14:49,902 [INFO] httpx: HTTP Request: POST http://127.0.0.1:9163/slots/0?action=save "HTTP/1.1 200 OK"
2026-08-03 13:14:49,904 [INFO] autoloader: Cleaned up old state file: Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf.Maine_1785751286.bin (4581.9 MB)
2026-08-03 13:14:49,905 [INFO] autoloader: State saved for model=qwen3.6-27b-fable-fus-mtp label=Maine_1785752083 path=Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf.Maine_1785752083.bin size=5552941688 bytes

