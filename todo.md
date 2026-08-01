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
[x] make a way to elegantly acquire a skill at run time (beside agent init)
[x] add a `range` argument to edit_file tool for the delete_and_insert mode so its less confusing than reusing `old_contnet`
[x] add quick and easy requirements file for default docker containers
[x] pass the supervisor's log file together with the name in the system prompt metadata, like: `Supervisor: Maine (orchestrator_Maine_20260731_023711.jsonl)` so sub-agents can easily find it if instructions are unclear — DONE: Added `_get_supervisor_log_filename()` helper in execution_engine.py. Modified `_build_session_metadata()` to append supervisor's log filename (basename only) when available. Graceful fallback to name-only when logger unavailable.


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
- [x] Write a proper README.md that describes the project as a whole and offers easy install & use instructions (completed — comprehensive README covering architecture, features, installation, usage with correct port/endpoints, programmatic access, and troubleshooting)
- [x] context token estimation (the one in base.py) is off by about 10% less than what llama.cpp reports as receiving. (fixed — added CHAT_TEMPLATE_TOKEN_OVERHEAD=5 per message to get_message_stats() in utils/utils.py, and unified _count_history_tokens() in execution_engine.py to use get_message_stats() instead of raw qwen_count(); error dropped from ~37% to ~4%)
- [ ] approval timeout doesn't seem to take into account the enable toggle in UI 
- [x] loading settings did not properly set the disabled tools for each agent. Fixed: added disabled_tools sync in syncPoolSettings() to apply server's pool_settings.json disabled_tools to UI on first load when no local preference exists, plus deferred renderAgentSelect until after syncPoolSettings runs to avoid race condition.
- [ ] if the compressor assigned model does not have enough context window we dont fallback to next endpoint, we keep retrying the same point over and over
- [x] in UI change `Auto-Ask` to `Auto-Security` and make sure its also saved over refresh/restart like the other settings. Fixed: renamed label in index.html line 225, persistence was already working via localStorage key 'auto-security'.
- [x] switching auto-ask off during Security processing makes the notification tab pop back up again once the process has been aproved/denied, and it can't be closed back without refresh. Fixed: added guard in security_response handler to skip stale responses when approval already processed, plus cleanup of securityResponses/activeSecurityChecks in approveRequest/rejectRequest functions.
- [x] grep fails to use the fast version: Fixed in agent_cascade/operation_manager/grep.py - removed _RG_DEFAULT_EXCLUDES constant (10 glob patterns evaluated per-file causing timeout on large dirs). Now relies on ripgrep's built-in ignore rules for .git, node_modules, __pycache__, etc. Only adds --glob when user explicitly provides include/exclude parameters. Also fixed standard grep path to respect ignore_vcs flag (was always excluding VCS dirs regardless of setting).

# Errors to investigate:

# reprocessing after state load? — INVESTIGATED (see test_state_restore_comparison.py results)
- Root cause: llama.cpp slot save/restore doesn't persist context checkpoints (upstream issue #25913). First post-restore request forces full reprocessing. Subsequent requests use cache correctly.
- Fix applied: Added `_warm_slot_cache()` after restore in autoloader `load_state()` endpoint (server.py line 1026) — works for dense models but NOT for MoE models like Agents-A1.
- Test results: qwen3.6-27b-fable-fus-mtp PASSES (99.9% cache hit after restore), Agents-A1-APEX-I-Quality FAILS (0% cache hit). Issue is specific to Agents-A1's MoE+MTP hybrid architecture.
- Workaround: Avoid using state save/restore with Agents-A1 endpoint, or accept first-turn reprocessing cost (~6s for ~18k tokens). Long-term fix requires upstream llama.cpp changes for MoE checkpoint persistence.

# Original logs:
2026-08-01 11:51:57,516 [INFO] autoloader: [Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf] 3.08.586.141 I slot      release: id  0 | task 840 | stop processing: n_tokens = 21573, truncated = 0
2026-08-01 11:51:57,904 [INFO] autoloader: Saving slot 0 state to file: N:\work\stuff\Beta\llama-autoloader\states\Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf.tools_fixer2_1785574317.bin
2026-08-01 11:51:59,463 [INFO] httpx: HTTP Request: POST http://127.0.0.1:9046/slots/0?action=save "HTTP/1.1 200 OK"
2026-08-01 11:51:59,465 [INFO] autoloader: Cleaned up old state file: Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf.tools_fixer_1785572434.bin (816.8 MB)
2026-08-01 11:51:59,466 [INFO] autoloader: State saved for model=qwen3.6-27b-fable-fus-mtp label=tools_fixer2_1785574317 path=Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf.tools_fixer2_1785574317.bin size=1571220648 bytes
[32mINFO[0m:     127.0.0.1:52121 - "[1mPOST /v1/models/qwen3.6-27b-fable-fus-mtp/state/save HTTP/1.1[0m" [32m200 OK[0m
2026-08-01 11:51:59,824 [INFO] autoloader: List states requested for model=qwen3.6-27b-fable-fus-mtp, found 5 state(s)
[32mINFO[0m:     127.0.0.1:52123 - "[1mGET /v1/models/qwen3.6-27b-fable-fus-mtp/state HTTP/1.1[0m" [32m200 OK[0m
2026-08-01 11:52:00,240 [INFO] autoloader: Auto-loading model 'Agents-A1-APEX-I-Quality.gguf' before state restore
2026-08-01 11:52:00,240 [INFO] autoloader: Auto-evicting LRU model Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf (last used 15.8s ago) to free slot
2026-08-01 11:52:00,242 [INFO] autoloader: Unloading Qwen3.6-27B-Fable-Fus-MTP-Q4_K_M.gguf (port 9046)
2026-08-01 11:52:00,983 [INFO] autoloader: Launching llama-server (N:\work\stuff\Beta\llama-autoloader\backends\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.27.0\llama-server.exe) for Agents-A1-APEX-I-Quality.gguf on port 9047
2026-08-01 11:52:00,983 [INFO] autoloader: argv: N:\work\stuff\Beta\llama-autoloader\backends\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.27.0\llama-server.exe --model N:\work\stuff\Beta\agents-a1\Agents-A1-APEX-I-Quality\Agents-A1-APEX-I-Quality.gguf --host 127.0.0.1 --port 9047 --ctx-size 130512 --n-gpu-layers 999 --alias Agents-A1-APEX-I-Quality.gguf --slot-save-path N:\work\stuff\Beta\llama-autoloader\states --no-webui --parallel 1 --jinja --cache-ram 16384 --kv-unified --ctx-size 130000 --n-gpu-layers 999 -fa auto -ts 100,65 -ngl -1 -t 8 --no-mmap --mlock --temp 0.85 --top-p 0.95 --min-p 0.0 --top-k 20 --presence-penalty 1.1 --repeat-penalty 1.0 --spec-type none --spec-draft-p-min 0.75 --spec-draft-n-max 2
2026-08-01 11:52:00,990 [INFO] autoloader: Waiting for llama-server on port 9047 at http://127.0.0.1:9047/health...
2026-08-01 11:52:01,185 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.052.251 W DEPRECATED: argument '--ctx-size' specified multiple times, use comma-separated values instead (only last value will be used)
2026-08-01 11:52:01,185 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.052.259 W DEPRECATED: argument '--n-gpu-layers' specified multiple times, use comma-separated values instead (only last value will be used)
2026-08-01 11:52:01,188 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.052.622 I srv          init: The UI is disabled
2026-08-01 11:52:01,188 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.052.627 I srv          init: Use --ui/--no-ui (or deprecated --webui/--no-webui) to enable/disable
2026-08-01 11:52:01,188 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.052.765 W srv  llama_server: -----------------
2026-08-01 11:52:01,188 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.052.769 W srv  llama_server: CORS is set to allow all origins ('*') and no API key is set
2026-08-01 11:52:01,188 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.052.770 W srv  llama_server: this can be a security risk (cross-origin attacks)
2026-08-01 11:52:01,189 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.052.770 W srv  llama_server: more info: https://github.com/ggml-org/llama.cpp/pull/25655
2026-08-01 11:52:01,189 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.052.770 W srv  llama_server: -----------------
2026-08-01 11:52:01,193 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.060.369 I srv    load_model: loading model 'N:\work\stuff\Beta\agents-a1\Agents-A1-APEX-I-Quality\Agents-A1-APEX-I-Quality.gguf'
2026-08-01 11:52:01,646 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:02,018 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.00.885.742 W common_fit_params: failed to fit params to free device memory: model_params::tensor_split already set by user, abort
2026-08-01 11:52:02,161 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:02,673 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:03,175 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:03,692 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:04,191 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:04,718 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:05,236 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:05,752 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:06,264 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:06,763 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:07,267 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:07,783 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:08,298 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:08,825 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:09,340 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:09,937 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:10,464 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:10,966 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:11,451 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 503 Service Unavailable"
2026-08-01 11:52:11,511 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.10.378.670 I srv    load_model: initializing, n_slots = 1, n_ctx_slot = 130048, kv_unified = 'true'
2026-08-01 11:52:11,540 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.10.407.919 I srv  llama_server: model loaded
2026-08-01 11:52:11,540 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.10.407.930 I srv  llama_server: listening on http://127.0.0.1:9047
2026-08-01 11:52:12,087 [INFO] httpx: HTTP Request: GET http://127.0.0.1:9047/health "HTTP/1.1 200 OK"
2026-08-01 11:52:12,087 [INFO] autoloader: llama-server on port 9047 is READY! (took 11.1s)
2026-08-01 11:52:12,089 [INFO] autoloader: Model Agents-A1-APEX-I-Quality.gguf ready on port 9047
2026-08-01 11:52:12,089 [INFO] autoloader: Restoring slot 0 state from file: N:\work\stuff\Beta\llama-autoloader\states\Agents-A1-APEX-I-Quality.gguf.reviewer_race_fix_1785574126.bin
2026-08-01 11:52:12,807 [INFO] httpx: HTTP Request: POST http://127.0.0.1:9047/slots/0?action=restore "HTTP/1.1 200 OK"
2026-08-01 11:52:12,808 [INFO] autoloader: State restored for model=Agents-A1-APEX-I-Quality label=reviewer_race_fix_1785574126 path=Agents-A1-APEX-I-Quality.gguf.reviewer_race_fix_1785574126.bin
[32mINFO[0m:     127.0.0.1:52124 - "[1mPOST /v1/models/Agents-A1-APEX-I-Quality/state/load HTTP/1.1[0m" [32m200 OK[0m
2026-08-01 11:52:12,918 [INFO] autoloader: --> Incoming POST request to '/v1/chat/completions' (model param input: 'Agents-A1-APEX-I-Quality.gguf')
2026-08-01 11:52:12,918 [INFO] autoloader: Resolved model ID: 'Agents-A1-APEX-I-Quality.gguf'
2026-08-01 11:52:12,920 [INFO] autoloader: Forwarding POST /v1/chat/completions -> llama-server on port 9047
2026-08-01 11:52:13,054 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.11.921.867 I slot get_availabl: id  0 | task -1 | selected slot by LCP similarity, sim_best = 0.219 (> 0.100 thold), f_keep = 0.195
2026-08-01 11:52:13,390 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.12.257.700 I slot launch_slot_: id  0 | task 1 | processing task, is_child = 0
2026-08-01 11:52:13,390 [INFO] httpx: HTTP Request: POST http://127.0.0.1:9047/v1/chat/completions "HTTP/1.1 200 OK"
[32mINFO[0m:     127.0.0.1:52146 - "[1mPOST /v1/chat/completions HTTP/1.1[0m" [32m200 OK[0m
2026-08-01 11:52:17,061 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.15.928.757 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =   9317, progress = 0.29, t =   3.67 s / 2537.98 tokens per second
2026-08-01 11:52:17,755 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.16.623.473 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  11365, progress = 0.36, t =   4.37 s / 2603.22 tokens per second
2026-08-01 11:52:18,464 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.17.331.099 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  13413, progress = 0.42, t =   5.07 s / 2643.80 tokens per second
2026-08-01 11:52:19,182 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.18.049.445 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  15461, progress = 0.48, t =   5.79 s / 2669.50 tokens per second
2026-08-01 11:52:19,928 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.18.796.004 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  17274, progress = 0.54, t =   6.54 s / 2641.98 tokens per second
2026-08-01 11:52:20,563 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.19.431.253 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  17615, progress = 0.55, t =   7.17 s / 2455.56 tokens per second
2026-08-01 11:52:21,307 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.20.174.365 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  19663, progress = 0.61, t =   7.92 s / 2483.75 tokens per second
2026-08-01 11:52:22,064 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.20.931.732 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  21711, progress = 0.68, t =   8.67 s / 2502.99 tokens per second
2026-08-01 11:52:22,834 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.21.699.055 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  23759, progress = 0.74, t =   9.44 s / 2516.49 tokens per second
2026-08-01 11:52:23,607 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.22.475.290 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  25807, progress = 0.81, t =  10.22 s / 2525.75 tokens per second
2026-08-01 11:52:23,943 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.22.810.993 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  26593, progress = 0.83, t =  10.55 s / 2519.88 tokens per second
2026-08-01 11:52:25,298 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.24.165.400 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  28232, progress = 0.88, t =  11.91 s / 2370.91 tokens per second
2026-08-01 11:52:26,098 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.24.966.458 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  30280, progress = 0.95, t =  12.71 s / 2382.61 tokens per second
2026-08-01 11:52:26,614 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.25.480.938 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  31480, progress = 0.98, t =  13.22 s / 2380.66 tokens per second
2026-08-01 11:52:27,075 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.25.942.682 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  31839, progress = 1.00, t =  13.68 s / 2326.57 tokens per second
2026-08-01 11:52:27,358 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.26.225.876 I slot print_timing: id  0 | task 1 | prompt processing, n_tokens =  31992, progress = 1.00, t =  13.97 s / 2290.35 tokens per second
2026-08-01 11:52:30,519 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.29.386.202 I slot print_timing: id  0 | task 1 | n_decoded =    195, tg =  64.93 t/s, tg_3s =  64.93 t/s
2026-08-01 11:52:30,809 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.29.676.173 I slot print_timing: id  0 | task 1 | prompt eval time =   14125.05 ms / 31996 tokens (    0.44 ms per token,  2265.20 tokens per second)
2026-08-01 11:52:30,809 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.29.676.178 I slot print_timing: id  0 | task 1 |        eval time =    3293.39 ms /   214 tokens (   15.39 ms per token,    64.98 tokens per second)
2026-08-01 11:52:30,812 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.29.676.179 I slot print_timing: id  0 | task 1 |       total time =   17418.43 ms / 32210 tokens
2026-08-01 11:52:30,812 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.29.676.179 I slot print_timing: id  0 | task 1 |    graphs reused =        212
2026-08-01 11:52:30,812 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.29.677.437 I slot      release: id  0 | task 1 | stop processing: n_tokens = 32209, truncated = 0
2026-08-01 11:52:30,903 [INFO] autoloader: --> Incoming POST request to '/v1/chat/completions' (model param input: 'Agents-A1-APEX-I-Quality.gguf')
2026-08-01 11:52:30,903 [INFO] autoloader: Resolved model ID: 'Agents-A1-APEX-I-Quality.gguf'
2026-08-01 11:52:30,904 [INFO] autoloader: Forwarding POST /v1/chat/completions -> llama-server on port 9047
2026-08-01 11:52:31,050 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.29.917.731 I slot get_availabl: id  0 | task -1 | selected slot by LCP similarity, sim_best = 0.974 (> 0.100 thold), f_keep = 0.993
2026-08-01 11:52:31,052 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.29.919.999 I slot launch_slot_: id  0 | task 237 | processing task, is_child = 0
2026-08-01 11:52:31,077 [INFO] httpx: HTTP Request: POST http://127.0.0.1:9047/v1/chat/completions "HTTP/1.1 200 OK"
[32mINFO[0m:     127.0.0.1:52146 - "[1mPOST /v1/chat/completions HTTP/1.1[0m" [32m200 OK[0m
2026-08-01 11:52:33,835 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.32.702.819 I slot print_timing: id  0 | task 237 | prompt eval time =     943.18 ms /   853 tokens (    1.11 ms per token,   904.39 tokens per second)
2026-08-01 11:52:33,836 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.32.702.824 I slot print_timing: id  0 | task 237 |        eval time =    1839.61 ms /   109 tokens (   16.88 ms per token,    59.25 tokens per second)
2026-08-01 11:52:33,837 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.32.702.825 I slot print_timing: id  0 | task 237 |       total time =    2782.79 ms /   962 tokens
2026-08-01 11:52:33,838 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.32.702.826 I slot print_timing: id  0 | task 237 |    graphs reused =        319
2026-08-01 11:52:33,838 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.32.703.763 I slot      release: id  0 | task 237 | stop processing: n_tokens = 32953, truncated = 0
2026-08-01 11:52:33,936 [INFO] autoloader: --> Incoming POST request to '/v1/chat/completions' (model param input: 'Agents-A1-APEX-I-Quality.gguf')
2026-08-01 11:52:33,936 [INFO] autoloader: Resolved model ID: 'Agents-A1-APEX-I-Quality.gguf'
2026-08-01 11:52:33,938 [INFO] autoloader: Forwarding POST /v1/chat/completions -> llama-server on port 9047
2026-08-01 11:52:34,089 [INFO] autoloader: [Agents-A1-APEX-I-Quality.gguf] 0.32.956.271 I slot get_availabl: id  0 | task -1 | selected slot by LCP similarity, sim_best = 0.991 (> 0.100 thold), f_keep = 0.997