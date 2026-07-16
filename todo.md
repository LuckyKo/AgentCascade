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
[x] warn agents about turn limit at 90%, and a final warning one when it has only 1 turn left to wrap it up. — FIXED: added turn limit warnings in execution_engine.py using `_append_system_notification` with guard prefix dedup; warns at 90% (turns_available == 10% of max_turns) and on the final remaining turn, appending to both `messages` and `llm_messages` lists
[ ] implement async shell_cmd launch (immediate tool response that it was launched, runs in background while agent is running and return final output as user message when done)
[ ] make cmd_shell pop open a console window in the back so the user can inspect or interact with it if needed.
[x] improve list_dir tool — FIXED (3ec490c): added recursive listing, glob filtering (include/exclude), sorting (name/size/date/type), human-readable sizes, timestamps, summary stats, max_entries cap, symlink cycle detection
[x] add a banner above the user chat entry that shows queued messages (with an X to dismiss each one individually) — FIXED: backend exposes queued_messages list in state payload, new dismiss_queue_message() method with thread-safe _queue_lock, WebSocket handler for individual/clear-all dismiss, frontend banner with live rendering and per-message ✕ buttons
[x] change USE_PREV_ARG system to an argument and (certain) tool output caching system — FIXED: new ArgumentCachePool class with rolling deque buffer, CacheEntry dataclass, per-instance scope, thread-safe operations, all tool args cached + outputs > 1000 chars cached, {USE_CACHED_ENTRY_N} resolution via shared resolve_cached_entry_refs() function, system_info displays cache pool state, toggle on/off via PoolSettings (cache_pool_enabled/cache_pool_size/cache_threshold_chars), config handlers for live updates, backward compatible with __USE_PREV_ARG__
[ ] document cache system info in SYSTEM_DOCS.md
[ ] add nr of times cached entries were used to telemetry info
[x] add `delete_and_insert` match_mode to edit_file tool: the `old_content` argument takes a python range `start:end` (but start with 1) that will be deleted before the new content is inserted at position `start`. leaving `new_content` empty will just delete that line range, providing just `start` in range will be pure insert of `new_content`. range can go negative, a start of -1 will insert at tail-1, 0 will append at the end, 1 will insert at start.
[x] add `shift` mode to re_indet tool, a mode where we just add or remove indent units from the start of the line. (the old `shit` mode will be renamed to `min`)
[ ] add auto-rollback feature on edit_file fail
[ ] store all agent instance IDs called by current agent so when dismissed, dismiss all the agents it called too. (it should result in a tree of dismissals recursively dismissing the whole branch)
[x] change read_logs argument to use `range` in the same indexing style as other tools like edit_file — FIXED: replaced start_index/nr_of_entries/last_n_messages with unified `range` string parameter (1-indexed, inclusive, e.g. "1:10", "5:", ":20", negative indices). Added _parse_range() static method. Updated dna.py metadata and audit documentation.
[ ] implement a live scratchpad tool that injects text/image data into the last few FUNCTION/USER messages. the tool can load a live view of a file's content, console output of a program by PID, interface capture data of a program by PID, set persistence distance (nr of messages in tail agent pool retaining the data, older messages get the data trimmed). agent can call this tool to enable disable this scratchpad (disable by setting persistence to 0, defaults on 2) 
[x] disable tools for the last turn of an agent so its forced to return a final answer — FIXED: disabled all tool schemas on last turn via instance._generate_cfg_override['disabled_tools'], cleaned up after LLM call returns.
[ ] add diff to re_indent reply (same as edit_file)

# BUGS:

- [ ] no agent tab refresh during tool call streaming
- [x] we have about 10-15% discrepancy (less) between the nr of tokens we measure and the actual count that LMStudio processes — FIXED: reasoning_content now always counted, all magic numbers centralized in settings.py 
- [x] `Terminate` doesn't really terminate the agent properly — FIXED: (1) extracted `_check_stream_termination()` helper in execution_engine.py that checks `_is_stopped()` every 20 yield ticks during LLM streaming, (2) consolidated redundant `_is_stop_interrupted()` into `_is_stopped()`, (3) added `_halted_instances` + `is_instance_terminated()` to main loop in run_agent_unified.py for full stop condition coverage, (4) removed unnecessary time.sleep(0.1) from break path.
- [ ] session loading sometime merges the old session with the new (mostly on server restart). should properly clean old session on load, just like it does a new session then loads.
- [ ] manually asking for security agent opinion does not fill it in and stop the security agent info once it reached conclusion, only happens on [YES]
- [ ] telemetry `Output Tokens (est)` severely undercounts
- [x] read_logs needs line numbers same as read_file — FIXED: added "{line_num}: {content}" prefix to each log entry output, matching read_file format in file_ops.py
- [x] call_agent returns `[SYSTEM ERROR: Empty LLM response]` if the agent failed a inner loop check — FIXED: inner-loop and max-tokens detection exceptions were suppressed by inner except block, added re-raise for inner_loop: and max_tokens: prefixes so outer retry handler shows proper error messages (execution_engine.py)
- [x] inner loop detector severely missfires — FIXED: analyzed 48 false positive samples across 5 days, added UI controls for min_chars, score_threshold, per-mode toggles (char_run, sentence_rep, ngram_rep, block_rep, entropy), and dedicated loop_max_retries budget in Loop Detection Tuning panel. Toggles gate detection signals in inner_loop_detect.py, applied via pool settings from WebUI in real time
- [x] add turn info (x / y available) to system_info — FIXED: added _current_turn field to AgentInstance, tracked in execution_engine.py loop, displayed in system_info tool output as "Current Turn: X / Y", centralized DEFAULT_MAX_TURNS constant in settings.py, reset on instance reuse
- [ ] we are pushing wrong summary from the inner loop detector if the compressor fails and gets stuck in a loop
- [ ] the setting `DEFAULT_READ_FILE_MAX_LINES` does not seem to have any effect
- [ ] unhelpful message return when a child agent fails the inner loop detection `[SYSTEM ERROR: Empty LLM response]` 
- [ ] some UI setting get lost on refresh/restart
- [ ] there's an odd issue with LMStudio models getting stuck in repeating sequences like `??????` or `///////` permanently, and our inner loop detector catches them correctly. But the only fix is to reload the model (or switch to another). I'm thinking of switching to the fallback API on inner loop detect instead of kick to caller as that would fix it, basically treating inner loop detection as API connection loss.


# Errors to investigate:

# push of summary on loop detected
2026-07-14 22:46:58,682 - execution_engine.py - 716 - DEBUG - [TURN_START] Calling _setup_turn for Compressor_1
2026-07-14 22:46:58,683 - execution_engine.py - 1152 - INFO - [CACHE_REBUILD] Rebuilding working set for Compressor_1 (conv_len=2)
2026-07-14 22:46:58,683 - execution_engine.py - 1235 - DEBUG - [CACHE_REBUILD] System prompt content CHANGED for Compressor_1
2026-07-14 22:46:58,690 - agent_instance_logger.py - 458 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\compressor_Compressor_1_20260714_224658.jsonl with 2 messages.
2026-07-14 22:46:58,690 - execution_engine.py - 751 - DEBUG - [TURN_DONE] Got messages=2, llm_messages=2
2026-07-14 22:46:58,766 - base.py - 990 - INFO - Agent [Compressor] - ALL tokens: 83146, Available tokens: 124493
2026-07-14 22:46:58,772 - oai.py - 77 - DEBUG - [CACHE] MISS creating new client key=('http://127.0.0.1:1234/v1', 'EMPTY')
2026-07-14 22:47:49,610 - execution_engine.py - 1906 - DEBUG - [STREAM_GUARD] Detected generation loop: repeated ngram (score=360.0) for Compressor_1. Retrying…
2026-07-14 22:47:49,611 - execution_engine.py - 1951 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentCascade_unified\workspace\logs\loop_samples\samples_2026-07-14.jsonl
2026-07-14 22:47:49,611 - execution_engine.py - 1977 - DEBUG - [INNER_LOOP] Detection error for Compressor_1: inner_loop: repeated ngram
2026-07-14 22:47:49,612 - execution_engine.py - 2198 - WARNING - [ENDPOINT_RETRY] LLM call failed for Compressor_1, retry 1/5. Retrying in 1.1s with new endpoint... Error: inner_loop: repeated ngram
2026-07-14 22:47:50,685 - base.py - 990 - INFO - Agent [Compressor] - ALL tokens: 83146, Available tokens: 124493
2026-07-14 22:47:53,012 - execution_engine.py - 1906 - DEBUG - [STREAM_GUARD] Detected generation loop: repeated ngram (score=360.0) for Compressor_1. Retrying…
2026-07-14 22:47:53,013 - execution_engine.py - 1951 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentCascade_unified\workspace\logs\loop_samples\samples_2026-07-14.jsonl
2026-07-14 22:47:53,013 - execution_engine.py - 1977 - DEBUG - [INNER_LOOP] Detection error for Compressor_1: inner_loop: repeated ngram
2026-07-14 22:47:53,014 - execution_engine.py - 2198 - WARNING - [ENDPOINT_RETRY] LLM call failed for Compressor_1, retry 2/5. Retrying in 2.0s with new endpoint... Error: inner_loop: repeated ngram
2026-07-14 22:47:55,051 - base.py - 990 - INFO - Agent [Compressor] - ALL tokens: 83146, Available tokens: 124493
2026-07-14 22:47:57,485 - execution_engine.py - 1906 - DEBUG - [STREAM_GUARD] Detected generation loop: repeated ngram (score=360.0) for Compressor_1. Retrying…
2026-07-14 22:47:57,486 - execution_engine.py - 1951 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentCascade_unified\workspace\logs\loop_samples\samples_2026-07-14.jsonl
2026-07-14 22:47:57,486 - execution_engine.py - 1977 - DEBUG - [INNER_LOOP] Detection error for Compressor_1: inner_loop: repeated ngram
2026-07-14 22:47:57,486 - execution_engine.py - 2198 - WARNING - [ENDPOINT_RETRY] LLM call failed for Compressor_1, retry 3/5. Retrying in 4.3s with new endpoint... Error: inner_loop: repeated ngram
2026-07-14 22:48:01,766 - base.py - 990 - INFO - Agent [Compressor] - ALL tokens: 83146, Available tokens: 124493
2026-07-14 22:48:04,092 - execution_engine.py - 1906 - DEBUG - [STREAM_GUARD] Detected generation loop: repeated ngram (score=360.0) for Compressor_1. Retrying…
2026-07-14 22:48:04,093 - execution_engine.py - 1951 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentCascade_unified\workspace\logs\loop_samples\samples_2026-07-14.jsonl
2026-07-14 22:48:04,093 - execution_engine.py - 1977 - DEBUG - [INNER_LOOP] Detection error for Compressor_1: inner_loop: repeated ngram
2026-07-14 22:48:04,093 - execution_engine.py - 2198 - WARNING - [ENDPOINT_RETRY] LLM call failed for Compressor_1, retry 4/5. Retrying in 5.0s with new endpoint... Error: inner_loop: repeated ngram
2026-07-14 22:48:09,107 - base.py - 990 - INFO - Agent [Compressor] - ALL tokens: 83146, Available tokens: 124493
2026-07-14 22:48:11,447 - execution_engine.py - 1906 - DEBUG - [STREAM_GUARD] Detected generation loop: repeated ngram (score=352.1) for Compressor_1. Retrying…
2026-07-14 22:48:11,448 - execution_engine.py - 1951 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentCascade_unified\workspace\logs\loop_samples\samples_2026-07-14.jsonl
2026-07-14 22:48:11,448 - execution_engine.py - 1977 - DEBUG - [INNER_LOOP] Detection error for Compressor_1: inner_loop_exhausted: retried 5 times, giving up — last reason: repeated ngram
2026-07-14 22:48:11,450 - base.py - 990 - INFO - Agent [Compressor] - ALL tokens: 83146, Available tokens: 124493
2026-07-14 22:48:13,801 - execution_engine.py - 1906 - DEBUG - [STREAM_GUARD] Detected generation loop: repeated ngram (score=360.0) for Compressor_1. Retrying…
2026-07-14 22:48:13,802 - execution_engine.py - 1951 - DEBUG -   [LOOP_SAMPLE] Saved to n:\work\WD\AgentCascade_unified\workspace\logs\loop_samples\samples_2026-07-14.jsonl
2026-07-14 22:48:13,802 - execution_engine.py - 1977 - DEBUG - [INNER_LOOP] Detection error for Compressor_1: inner_loop_exhausted: retried 5 times, giving up — last reason: repeated ngram
2026-07-14 22:48:13,805 - execution_engine.py - 1080 - DEBUG - EXIT - Compressor_1 RUNNING→IDLE
2026-07-14 22:48:14,122 - handler.py - 843 - INFO - /compress applied for Maine: Context compressed (auto mode): 93 messages summarized for 'Maine'.
2026-07-14 22:48:14,122 - handler.py - 311 - DEBUG - Logger sync after /compress command for 'Maine': pool_len=28, using reset_history() for full sync
2026-07-14 22:48:14,153 - agent_instance_logger.py - 678 - INFO - Synced compression marker in n:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260714_224616.jsonl (315 messages).
2026-07-14 22:48:14,172 - execution_engine.py - 1645 - DEBUG - Rebuilt working sets for Maine: messages=28, llm_messages=28
2026-07-14 22:48:14,176 - execution_engine.py - 1457 - DEBUG - [PRE_LLM] Compress command handled for Maine
2026-07-14 22:48:14,176 - execution_engine.py - 824 - DEBUG - [PRE_LLM_CHECK] Condition met, continuing loop
