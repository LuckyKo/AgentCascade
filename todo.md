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
[x] full inner loop mode audit — DONE: Replaced all scoring-based modes (sentence/ngram/block/entropy) with two-phase semantic loop detector (heuristic suspicion → exact match confirmation → cooldown on failure). Char_run + max_chars preserved unchanged as last line of defense. 62 tests passing. See docs/inner_loop_audit_plan.md and docs/inner_loop_phase0_baseline.md for details.


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
- [ ] the `Self-Augmentation` skill does not always get inserted on new agent call, very inconsistent. (does `Enable skills` toggle require restart to take effect?)
- [x] add launched agent's log file name to the async call agent reply. Fixed: in _run_child_async() (tool_dispatcher.py), call pool.get_logger() before returning the confirmation message, include os.path.basename(log_path) in the message format.
- [x] remove the tools info from `list_agents`, make sure we catch all agent states (it's missing some). Fixed: removed tools/capabilities display from template listing in manager_ops.py; replaced binary ACTIVE/IDLE with full state mapping (IDLE/RUNNING/SLEEPING/COMPLETING/TERMINATED) plus HALTED overlay via _format_agent_status helper; added thread-safe get_state_name() accessor to AgentInstance.
- [x] UI refine: make the blinking motion of the activity bubble in the agent tabs (the dot in front of the name) only blink for the ones actively streaming. Fixed: changed pulse visibility logic in web_ui/app.js renderSubAgents() and updateControls() to use per-agent is_partial flag (true streaming state from backend) instead of execution state (active/SLEEPING). Pulse now only appears when agent is actively streaming LLM output.
- [ ] telemetry: add `Malformed` info to session-stats telemetry (how many times we hit the Auto-continue logic); in `loops detected` count the inner loops too
- [ ] UI issue: approval window pops back up after being aproved/rejected by user. cant be closed back

# Errors to investigate:

## [RESOLVED] Sync Coder gets interrupted when async Researcher finishes? — Root cause found
- **Root cause:** Forced compression calls `pool.halt_all_instances()`, which halts concurrent sub-agents. Their run-loop treats halt as terminal and breaks mid-turn; `resume_all_instances()` clears the flag but doesn't restart them. Async completion is coincidental trigger (drain → compression check). See [[async-interrupt-compression-halt]]
- **Investigation report:** `investigation_report_async_interrupt_compression_halt.md`

### TODO — Fixes needed:
- [x] Distinguish compression-halt from terminal halt in `_is_stopped()` so halted-by-compression agents auto-resume instead of breaking (execution_engine.py) — IMPLEMENTED
- [x] Decision: keep halt_all_instances, fix is in agent response behavior (see fix plan Change 5) — DOCUMENTED
- [x] Fix misleading "Execution was stopped by user" message when agent halted by compression (child_runner.py) — IMPLEMENTED
- [x] Fix wrong log path in warning messages — use actual log_path instead of bare instance_name.log (compression/helpers.py) — IMPLEMENTED
- [ ] Test the fix with concurrent agents triggering forced compression (see fix_plan test section)

2026-08-06 00:24:13,487 - base.py - 1052 - INFO - Agent [Researcher] - ALL tokens: 126306, Available tokens: 143639
2026-08-06 00:24:17,270 - base.py - 1052 - INFO - Agent [Coder] - ALL tokens: 34804, Available tokens: 118453
2026-08-06 00:24:17,803 - base.py - 1052 - INFO - Agent [Researcher] - ALL tokens: 127800, Available tokens: 143639
2026-08-06 00:24:20,936 - base.py - 1052 - INFO - Agent [Researcher] - ALL tokens: 128308, Available tokens: 143639
2026-08-06 00:24:22,631 - base.py - 1052 - INFO - Agent [Coder] - ALL tokens: 34879, Available tokens: 118453
2026-08-06 00:24:26,800 - base.py - 1052 - INFO - Agent [Coder] - ALL tokens: 34965, Available tokens: 118453
2026-08-06 00:24:30,684 - base.py - 1052 - INFO - Agent [Coder] - ALL tokens: 35238, Available tokens: 118453
2026-08-06 00:24:32,551 - base.py - 1052 - INFO - Agent [Researcher] - ALL tokens: 131224, Available tokens: 143639
2026-08-06 00:24:34,910 - base.py - 1052 - INFO - Agent [Coder] - ALL tokens: 37737, Available tokens: 118453
2026-08-06 00:25:03,213 - base.py - 1052 - INFO - Agent [Coder] - ALL tokens: 38332, Available tokens: 118453
2026-08-06 00:25:07,156 - base.py - 1052 - INFO - Agent [Coder] - ALL tokens: 38991, Available tokens: 118453
2026-08-06 00:25:12,907 - execution_engine.py - 2185 - INFO - [bug_investigator_2] post-tool proactive check: context at 96.8% (threshold 95.0%), triggering compression
2026-08-06 00:25:12,907 - handler.py - 567 - INFO - Context usage at 96.8% for bug_investigator_2 — forcing compression (attempt #1).
2026-08-06 00:25:13,048 - execution_engine.py - 1557 - DEBUG - EXIT - async_shell_fixer RUNNING→IDLE
2026-08-06 00:25:13,069 - execution_engine.py - 4670 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=async_shell_fixer, reason=completed, inst_type=AgentInstance, conv_len=2, final_resp_len=60
2026-08-06 00:25:13,307 - lifecycle_manager.py - 194 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for Compressor_1
2026-08-06 00:25:13,448 - execution_engine.py - 1065 - DEBUG - engine.run() ENTRY - instance=Compressor_1
2026-08-06 00:25:13,484 - execution_engine.py - 1143 - DEBUG - [TURN_START] Calling _setup_turn for Compressor_1
2026-08-06 00:25:13,484 - execution_engine.py - 1640 - INFO - [CACHE_REBUILD] Rebuilding working set for Compressor_1 (conv_len=2)
2026-08-06 00:25:13,491 - execution_engine.py - 1778 - DEBUG - [CACHE_REBUILD] System prompt for Compressor_1 textually identical — skipping pool update
2026-08-06 00:25:13,492 - execution_engine.py - 1178 - DEBUG - [TURN_DONE] Got messages=2, llm_messages=2
2026-08-06 00:25:13,496 - base.py - 1052 - INFO - Agent [Compressor] - ALL tokens: 103102, Available tokens: 124581
2026-08-06 00:25:19,672 - log.py - 80 - WARNING - State restore failed for Maine (label=Maine_1785964838), cleared label
State restore failed for Maine (label=Maine_1785964838), cleared label
2026-08-06 00:25:19,674 - tool_dispatcher.py - 492 - DEBUG - [SLOT_SYNC_CHILD_COMPLETE] Sync child 'async_shell_fixer' completed in 277.55s
2026-08-06 00:25:19,675 - tool_dispatcher.py - 505 - DEBUG - [SLOT_SYNC_REACQUIRE] Attempting to re-acquire slot for 'Maine' after sync child
2026-08-06 00:25:19,675 - agent_pool.py - 2413 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://127.0.0.1:1234/v1, concurrency_limit=0
2026-08-06 00:25:19,675 - tool_dispatcher.py - 514 - DEBUG - [SLOT_SYNC_REACQUIRED] Successfully re-acquired slot for 'Maine'. Total SYNC path elapsed: 277.55s
2026-08-06 00:25:19,678 - tool_dispatcher.py - 130 - DEBUG - handle_call_agent returned type=str
2026-08-06 00:27:07,051 - execution_engine.py - 1557 - DEBUG - EXIT - Compressor_1 RUNNING→IDLE
2026-08-06 00:27:07,079 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 59469, Available tokens: 118152
2026-08-06 00:27:07,464 - execution_engine.py - 2311 - DEBUG - Rebuilt working sets for bug_investigator_2: messages=73, llm_messages=73
2026-08-06 00:27:07,465 - handler.py - 415 - DEBUG - Logger sync after forced compression for 'bug_investigator_2': pool_len=73, using reset_history() for full sync
2026-08-06 00:27:07,488 - agent_instance_logger.py - 594 - INFO - Synced compression marker in n:\work\WD\AgentWorkspace\logs\researcher_bug_investigator_2_20260806_001307.jsonl (235 messages).
2026-08-06 00:27:07,558 - execution_engine.py - 2191 - WARNING - [bug_investigator_2] post-tool compression skipped (cooldown/max-attempts) at 96.8% — pre-LLM guard will catch overflow
2026-08-06 00:27:07,585 - base.py - 1052 - INFO - Agent [Researcher] - ALL tokens: 38550, Available tokens: 143639
2026-08-06 00:27:44,381 - agent.py - 260 - WARNING - An error occurred when calling tool `write_file`:
ValidationError: 'path' is a required property

Failed validating 'required' in schema:
    {'type': 'object',
     'properties': {'path': {'type': 'string',
                             'description': 'Path to the file, absolute or '
                                            'relative to the workspace '
                                            "root (e.g., 'src/main.py')."},
                    'content': {'type': 'string',
                                'description': 'The full content to write '
                                               'to the file.'},
                    'justification': {'type': 'string',
                                      'description': 'Why you need to '
                                                     'create or overwrite '
                                                     'this file'}},
     'required': ['path', 'content']}

On instance:
    {'content': '# AgentCascade UI Tab, Dismiss-idle, Slot Timeout, Skills '
                'Bugs\n'
                '\n'
                'Date: 2026-08-06 · Investigator: bug_investigator_2 · '
                'Report: '
                '`investigation_report_4bugs_ui_tabs_dismiss_slots_skills.md`\n'
                '\n'
                '## BUG 1 — Spawned sub-agent with no UI tab\n'
                '- Frontend tabs come ONLY from `stream_update` '
                '(`renderSubAgents` app.js:3248 building from '
                '`state.subAgents`), no `agent_spawned` event.\n'
                '- Stream updates are dropped on `QueueFull` '
                '(`_put_stream_update` api_integration.py:121, queue '
                'maxsize=128 api_server.py:352) and throttled.\n'
                '- `state.closedTabs` (localStorage '
                '`agent-cascade-closed-tabs`, app.js:114,3266) persists '
                'across browser refresh → re-spawned instance of a '
                'previously-closed-tab agent name stays hidden. **Most '
                'probable cause.**\n'
                '- Fix: clear closedTabs on re-spawn; add lossless '
                '`agent_spawned` event.\n'
                '\n'
                '## BUG 2 — dismiss_agent(all_idle) killing async/running '
                'agents\n'
                '- `handle_dismiss_agent` (tool_dispatcher.py:359) '
                'dismisses anything not in `active_stack`, not SLEEPING, '
                'not halted, not root.\n'
                '- Async child in ThreadPoolExecutor (async_tools.py:74, 4 '
                "workers) isn't in active_stack during the window before "
                'its own append → default IDLE → wrongly dismissible.\n'
                '- Fix: also consult `_async_registry._pending` and '
                'non-IDLE AgentState (RUNNING/COMPLETING/CREATED). '
                '`_is_idle()` (agent_pool.py:2916) is the correct guard '
                'model.\n'
                '\n'
                '## BUG 3 — Async slot timeout leaves child hanging\n'
                '- Error from `api_router.py:303-315` '
                'EndpointScheduler.acquire → TimeoutError (30s, '
                '`ENDPOINT_SLOT_ACQUIRE_TIMEOUT`).\n'
                '- Instance created & pushed to UI before slot acquired '
                '(execution_engine.py run() acquires at line 1129 inside '
                '`_create_and_run_agent`).\n'
                '- Commit `50befb0` adds `dismiss_instance` cleanup in '
                'agent_pool.py except block — but thread not cancelled; no '
                'endpoint fallback on busy (design deferred in '
                '`docs/async_slot_timeout_fix_plan.md`).\n'
                '- Fix: cancel pending registry entry; slot-aware fallback '
                'to another endpoint; acquire before create.\n'
                '\n'
                '## BUG 4 — Self-Augmentation skill inconsistent\n'
                '- Two injection paths: `_inject_self_augmentation_skill` '
                '(execution_engine.py:583, calls `_ensure_discovered()` at '
                '621) for root; sub-agent path rep executes '
                '`load_full_instructions` inside `_create_and_run_agent` '
                'line 4514 without independent `_ensure_discovered()`.\n'
                '- Skills toggle NONE→AUTO: '
                '`config_handlers._handle_default_load_skill_mode` clears '
                'registry (line 314) `_skills_registry.clear()` + '
                '`_rebuild_index()`) but does NOT invalidate discovery '
                'cache (`manager.py:204-214` short-circuits unless new '
                'TTL/signature differs) → registry stays empty until 30s '
                'TTL → no skills. **Reproducible.**\n'
                '- Idempotency guard: `_inject_skills_to_system_message` '
                'returns early if "## Active Skills" already in system '
                'msg.\n'
                '\n'
                '## Links\n'
                '- Seeds: `[[ui-tabs-slot-skill-investigation]]` / '
                '`[[agent-cascade-bug-investigation]]`...'}
Traceback:
  File "n:\work\WD\AgentCascade\agent_cascade\agent.py", line 248, in _call_tool
    tool_result = tool.call(tool_args, **kwargs)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\tools\custom\file_ops.py", line 594, in call
    params_json = self._verify_json_format_args(params)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\tools\base.py", line 176, in _verify_json_format_args
    jsonschema.validate(instance=sanitized_params, schema=self.parameters)
  File "C:\Python312\Lib\site-packages\jsonschema\validators.py", line 1332, in validate
    raise error

2026-08-06 00:27:44,412 - base.py - 1052 - INFO - Agent [Researcher] - ALL tokens: 40698, Available tokens: 143639
2026-08-06 00:27:51,408 - base.py - 1052 - INFO - Agent [Researcher] - ALL tokens: 41682, Available tokens: 143639
2026-08-06 00:27:59,420 - execution_engine.py - 1557 - DEBUG - EXIT - bug_investigator_2 RUNNING→IDLE
2026-08-06 00:27:59,434 - execution_engine.py - 4670 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=bug_investigator_2, reason=completed, inst_type=AgentInstance, conv_len=2, final_resp_len=20
2026-08-06 00:28:33,869 - agent_pool.py - 2981 - INFO - [idle_checker] Auto-dismissing idle system agent (Compressor) 'Compressor_1' (idle for 87s, threshold=60s)
2026-08-06 00:28:33,869 - agent_pool.py - 825 - DEBUG - Instance conversation cleanup key missing (expected): 'Compressor_1'
2026-08-06 00:28:33,871 - agent_pool.py - 2906 - INFO - [idle_checker] Auto-dismissed 1 idle agent(s): Compressor_1
2026-08-06 00:28:39,464 - ws_handlers.py - 943 - INFO - [update_endpoints] Received: 15 endpoints, 8 agent priority mappings
2026-08-06 00:28:41,222 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:41,222 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:41,226 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:41,226 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:41,227 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:41,227 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:41,228 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:41,229 - config_handlers.py - 247 - WARNING - [THREAD_POOL] resize_executor skipped — executor is None (pool just initialized?)
2026-08-06 00:28:41,230 - config_handlers.py - 152 - DEBUG - [work_folders] Extra work folders unchanged
2026-08-06 00:28:41,231 - config_handlers.py - 152 - DEBUG - [work_folders] Extra work folders unchanged
2026-08-06 00:28:41,232 - config_handlers.py - 180 - DEBUG - [update_config] Base workspace unchanged
2026-08-06 00:28:42,097 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:42,097 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:42,098 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:42,099 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:42,099 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:42,100 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:42,100 - config_handlers.py - 670 - DEBUG - [update_config] LLM config unchanged
2026-08-06 00:28:42,101 - config_handlers.py - 247 - WARNING - [THREAD_POOL] resize_executor skipped — executor is None (pool just initialized?)
2026-08-06 00:28:42,101 - config_handlers.py - 152 - DEBUG - [work_folders] Extra work folders unchanged
2026-08-06 00:28:42,102 - config_handlers.py - 152 - DEBUG - [work_folders] Extra work folders unchanged
2026-08-06 00:28:42,103 - config_handlers.py - 180 - DEBUG - [update_config] Base workspace unchanged
2026-08-06 00:28:42,206 - ws_handlers.py - 943 - INFO - [update_endpoints] Received: 15 endpoints, 8 agent priority mappings
2026-08-06 00:28:52,940 - execution_engine.py - 3072 - INFO - Endpoint allocation updated for orchestrator: {'endpoint': 'LMS-27B-unc-MTP', 'api_base': 'http://127.0.0.1:1234/v1', 'model': 'qwen3.6-27b-fable-fus-mtp', 'max_input_tokens': 100000, 'rate_limit_rpm': 0, 'concurrency_limit': 0, 'prev_max_input_tokens': 120000}
2026-08-06 00:28:52,966 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 60766, Available tokens: 98152
2026-08-06 00:28:56,203 - ws_handlers.py - 943 - INFO - [update_endpoints] Received: 15 endpoints, 8 agent priority mappings
2026-08-06 00:29:02,264 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 62572, Available tokens: 98152
2026-08-06 00:29:03,144 - ws_handlers.py - 943 - INFO - [update_endpoints] Received: 15 endpoints, 8 agent priority mappings
2026-08-06 00:29:12,209 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 63604, Available tokens: 98152
2026-08-06 00:29:27,312 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 64906, Available tokens: 98152
2026-08-06 00:29:52,515 - ws_handlers.py - 964 - INFO - [USER] Approving request: op_4e864cc4
2026-08-06 00:29:53,277 - async_shell.py - 490 - DEBUG - [AsyncShell] Launched tool_id=23 for Maine, PID=21888, cmd='powershell -NoProfile -Command "for ($i=1; $i -le 50; $i++) { Write-Host \"kill '
2026-08-06 00:29:53,285 - async_shell.py - 518 - DEBUG - [AsyncShell] Viewer process spawned for tool_id=23, PID=10112
2026-08-06 00:29:54,624 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 65336, Available tokens: 98152
2026-08-06 00:29:56,304 - async_shell.py - 908 - DEBUG - [async_shell] heartbeat with output agent=Maine tool_id=23 lines=8
2026-08-06 00:29:59,314 - async_shell.py - 908 - DEBUG - [async_shell] heartbeat with output agent=Maine tool_id=23 lines=15
2026-08-06 00:30:01,851 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 65612, Available tokens: 98152
2026-08-06 00:30:02,338 - async_shell.py - 908 - DEBUG - [async_shell] heartbeat with output agent=Maine tool_id=23 lines=14
2026-08-06 00:30:07,121 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 65896, Available tokens: 98152
2026-08-06 00:30:15,449 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 67633, Available tokens: 98152
2026-08-06 00:30:25,750 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 68461, Available tokens: 98152
2026-08-06 00:31:00,663 - base.py - 1052 - INFO - Agent [Orchestrator] - ALL tokens: 69135, Available tokens: 98152


# keep seeing these pop. is this an actual issue?
# RESOLVED: Not a bug — expected one-time resource injection during first cache rebuild per agent.
# Log level demoted from INFO to DEBUG (execution_engine.py:1751). See .agent_lessons/system_prompt_changed_log.md
```
2026-08-06 01:04:05,559 - execution_engine.py - 867 - DEBUG - [SLOT_ACQUIRE] initial - instance=refinement_reviewer, class=reviewer
2026-08-06 01:04:05,560 - execution_engine.py - 1143 - DEBUG - [TURN_START] Calling _setup_turn for refinement_reviewer
2026-08-06 01:04:05,561 - execution_engine.py - 1640 - INFO - [CACHE_REBUILD] Rebuilding working set for refinement_reviewer (conv_len=2)
2026-08-06 01:04:05,561 - execution_engine.py - 1751 - INFO - [CACHE_REBUILD] System prompt content CHANGED for refinement_reviewer (len 6069→6602, first_diff@2820: orig='f: PASS, NEEDS WORK, or FAIL

## Active Skills

### Skill 1
' new='f: PASS, NEEDS WORK, or FAIL



## AVAILABLE AGENTS

Availab')
2026-08-06 01:04:05,562 - agent_instance_logger.py - 486 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\reviewer_refinement_reviewer_20260806_010405.jsonl with 2 messages.
2026-08-06 01:04:05,562 - execution_engine.py - 1178 - DEBUG - [TURN_DONE] Got messages=2, llm_messages=2
2026-08-06 01:04:05,565 - base.py - 1052 - INFO - Agent [Reviewer] - ALL tokens: 198, Available tokens: 123497
2026-08-06 01:04:52,440 - base.py - 1052 - INFO - Agent [Reviewer] - ALL tokens: 4326, Available tokens: 123497
2026-08-06 01:04:56,479 - base.py - 1052 - INFO - Agent [Reviewer] - ALL tokens: 4615, Available tokens: 123497
```