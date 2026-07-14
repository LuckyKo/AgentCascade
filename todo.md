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
- [x] we are sending custom sampling info when its disabled for the used API — FIXED: added _use_custom_sampling flag in api_router.py to_llm_cfg(), _build_merged_cfg() in execution_engine.py now strips stale sampling params from lower layers (template/UI) when endpoint has custom sampling disabled, SAMPLING_KEYS frozenset covers all 10 param variants, base.py agent_settings cleanup pops the internal flag

# Errors to investigate:

# noisy fallback
    raise ModelServiceError(exception=ex, code=code if code else None)
agent_cascade.llm.base.ModelServiceError: Error code: 400 - {'message': 'user input rejected (HTTP 400): API returned unexpected status code: 400: The `reasoning_content` in the thinking mode must be passed back to the API.', 'request_id': 'req_2a1cfae7', 'timestamp': '2026-07-12T21:53:39.009531400+00:00', 'trace_id': 'ac701ebec30a482086c367cbb31b8d02'}

2026-07-13 00:53:40,105 - base.py - 990 - INFO - Agent [Generalist] - ALL tokens: 233, Available tokens: 124208
2026-07-13 00:53:40,690 - log.py - 41 - WARNING - [APIRouter] Endpoint 'grok-4.1-fast' @ http://127.0.0.1:4315/v1 attempt 2/2: Error code: 400 - {'message': 'user input rejected (HTTP 400): API returned unexpected status code: 400: The `reasoning_content` in the thinking mode must be passed back to the API.', 'request_id': 'req_cd1fc011', 'timestamp': '2026-07-12T21:53:40.690175100+00:00', 'trace_id': '8ff20a70695c4169a9f0e12e102aa670'}
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 1053, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 999, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 524, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 384, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 508, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1054, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1077, in _raise_or_delay
    raise e from None
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1049, in retry_model_service_iterator
    for rsp in it_fn():
               ^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\oai.py", line 552, in _chat_stream
    raise ModelServiceError(exception=ex, code=code if code else None)
agent_cascade.llm.base.ModelServiceError: Error code: 400 - {'message': 'user input rejected (HTTP 400): API returned unexpected status code: 400: The `reasoning_content` in the thinking mode must be passed back to the API.', 'request_id': 'req_cd1fc011', 'timestamp': '2026-07-12T21:53:40.690175100+00:00', 'trace_id': '8ff20a70695c4169a9f0e12e102aa670'}
[APIRouter] Endpoint 'grok-4.1-fast' @ http://127.0.0.1:4315/v1 attempt 2/2: Error code: 400 - {'message': 'user input rejected (HTTP 400): API returned unexpected status code: 400: The `reasoning_content` in the thinking mode must be passed back to the API.', 'request_id': 'req_cd1fc011', 'timestamp': '2026-07-12T21:53:40.690175100+00:00', 'trace_id': '8ff20a70695c4169a9f0e12e102aa670'}
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 1053, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 999, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 524, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 384, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 508, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1054, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1077, in _raise_or_delay
    raise e from None
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1049, in retry_model_service_iterator
    for rsp in it_fn():
               ^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\oai.py", line 552, in _chat_stream
    raise ModelServiceError(exception=ex, code=code if code else None)
agent_cascade.llm.base.ModelServiceError: Error code: 400 - {'message': 'user input rejected (HTTP 400): API returned unexpected status code: 400: The `reasoning_content` in the thinking mode must be passed back to the API.', 'request_id': 'req_cd1fc011', 'timestamp': '2026-07-12T21:53:40.690175100+00:00', 'trace_id': '8ff20a70695c4169a9f0e12e102aa670'}

2026-07-13 00:53:40,695 - base.py - 990 - INFO - Agent [Generalist] - ALL tokens: 233, Available tokens: 124208
2026-07-13 00:53:40,698 - oai.py - 391 - INFO - LLM infrastructure changed. Re-detecting context for: http://localhost:1234/v1
2026-07-13 00:53:42,762 - oai.py - 333 - DEBUG - Missing context metadata in list. Trying specific endpoint: http://localhost:1234/v1/models/agents-a1-35b-mtp
2026-07-13 00:53:44,799 - oai.py - 354 - INFO - Model agents-a1-35b-mtp found, but could not detect context length via API.

# Web_extract error

2026-07-13 01:34:39,985 - base.py - 990 - INFO - Agent [Researcher] - ALL tokens: 11745, Available tokens: 124134
2026-07-13 01:34:43,966 - simple_doc_parser.py - 450 - INFO - Start parsing https://dl.acm.org/doi/10.1016/j.cosrev.2026.100902...
2026-07-13 01:34:43,989 - utils.py - 274 - INFO - Downloading https://dl.acm.org/doi/10.1016/j.cosrev.2026.100902 to n:\work\WD\AgentWorkspace\tools\simple_doc_parser\b941e841623e0e8c8ff8e9d12d620d8f79636b86082b8642cebaca02070bb5ae\j.cosrev.2026.100902...
2026-07-13 01:34:44,011 - agent.py - 260 - WARNING - An error occurred when calling tool `web_extractor`:
ValueError: Can not download this file. Please check your network or the file link.
Traceback:
  File "n:\work\WD\AgentCascade_unified\agent_cascade\agent.py", line 248, in _call_tool
    tool_result = tool.call(tool_args, **kwargs)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\tools\web_extractor.py", line 44, in call
    parsed_web = self.simple_doc_parser.call({'url': url})
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\tools\simple_doc_parser.py", line 466, in call
    path = save_url_to_local_work_dir(path, tmp_file_root)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\utils\utils.py", line 289, in save_url_to_local_work_dir
    raise ValueError('Can not download this file. Please check your network or the file link.')

2026-07-13 01:34:44,027 - simple_doc_parser.py - 450 - INFO - Start parsing https://www.deloitte.com/us/en/insights/industry/technology/technology-media-and-telecom-predictions/2026/ai-agent-orchestration.html...
2
