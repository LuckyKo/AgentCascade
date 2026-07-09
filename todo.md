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
 
# BUGS:

- [ ] no agent tab refresh during tool call streaming
- [x] retry is broken, it duplicates the user message — FIXED (066b7db): reordered snapshot rollback before trimming
- [x] max tokens does not change when a new API endpoint is acquired 
- [x] make llm sampler options toggleable per entry (add a toggle on the right side of each one); add custom sampling toggle per API endpoint; move vision enabled per API endpoint — DONE: added use_custom_sampling flag, all 8 sampler params to dataclass + UI, vision toggle in header, collapsible sampling section
- [x] we have about 10-15% discrepancy (less) between the nr of tokens we measure and the actual count that LMStudio processes — FIXED: reasoning_content now always counted, all magic numbers centralized in settings.py 
- [ ] `Terminate` doesn't really terminate the agent properly, it keeps streaming, sometime left as an unreachable background thread.
- [ ] session loading sometime merges the old session with the new (mostly on server restart). should properly clean old session on load, just like it does a new session then loads.
- [x] agent tab needs refresh when switching to it from another — FIXED: invalidate panel contentKey/lastRenderedCount cache in switchMainTab() to force re-render on tab switch
- [ ] manually asking for security agent opinion does not fill it in and stop the security agent once it reached conclusion
- [ ] if user unchecked auto-ask while the security agent is running the approval window pops up correctly but its stuck in a weird flashing mode and cant press any button to approve/reject
- [ ] telemetry `Avg TPS` is wrongly calculated, `Output Tokens (est)` also most likely undercounts
- [ ] `REJECTED BY USER: SECURITY REJECTED:` is pre-pended to rejection messages when Security rejects it. it should properly distinguish when User or Security rejected it.

# Errors to investigate:

# noisy fallback
2026-07-07 05:49:55,830 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:49:57,369 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:49:58,368 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:50:00,142 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:50:00,929 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 27985, Available tokens: 108931
2026-07-07 05:50:14,643 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:50:44,849 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 29929, Available tokens: 108931
2026-07-07 05:50:45,150 - oai.py - 315 - INFO - LLM infrastructure changed. Re-detecting context for: https://opencode.ai/zen/v1
2026-07-07 05:50:45,614 - oai.py - 280 - DEBUG - Could not identify a target model in https://opencode.ai/zen/v1/models for context length detection.
2026-07-07 05:50:45,615 - oai.py - 77 - DEBUG - [CACHE] MISS creating new client key=('https://opencode.ai/zen/v1', 'sk-6yjmx8gEYAi0Cv0ShnQyfpm7x9ntBavdr0GW6kTPyyFalWIegs4FpI1D4RW9Ayxe')
2026-07-07 05:50:46,579 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:50:51,435 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:01,767 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:01,769 - log.py - 41 - WARNING - [APIRouter] Endpoint 'Hy3-free' @ https://opencode.ai/zen/v1 attempt 1/2: Maximum number of retries (2) exceeded.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 978, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 920, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 523, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 383, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 507, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1017, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1055, in _raise_or_delay
    raise ModelServiceError(exception=Exception(f'Maximum number of retries ({max_retries}) exceeded.')) from None
agent_cascade.llm.base.ModelServiceError: Maximum number of retries (2) exceeded.
[APIRouter] Endpoint 'Hy3-free' @ https://opencode.ai/zen/v1 attempt 1/2: Maximum number of retries (2) exceeded.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 978, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 920, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 523, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 383, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 507, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1017, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1055, in _raise_or_delay
    raise ModelServiceError(exception=Exception(f'Maximum number of retries ({max_retries}) exceeded.')) from None
agent_cascade.llm.base.ModelServiceError: Maximum number of retries (2) exceeded.

2026-07-07 05:51:02,865 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 29929, Available tokens: 108931
2026-07-07 05:51:03,411 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:08,302 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:09,615 - ws_handlers.py - 737 - INFO - [update_endpoints] Received: 15 endpoints, 7 agent priority mappings
2026-07-07 05:51:17,946 - base.py - 1052 - WARNING - ModelServiceError - Error code: 401 - {'type': 'error', 'error': {'type': 'ModelError', 'message': 'Model Hy3-free is not supported'}}
2026-07-07 05:51:17,947 - log.py - 41 - WARNING - [APIRouter] Endpoint 'Hy3-free' @ https://opencode.ai/zen/v1 attempt 2/2: Maximum number of retries (2) exceeded.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 978, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 920, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 523, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 383, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 507, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1017, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1055, in _raise_or_delay
    raise ModelServiceError(exception=Exception(f'Maximum number of retries ({max_retries}) exceeded.')) from None
agent_cascade.llm.base.ModelServiceError: Maximum number of retries (2) exceeded.
[APIRouter] Endpoint 'Hy3-free' @ https://opencode.ai/zen/v1 attempt 2/2: Maximum number of retries (2) exceeded.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 978, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\api_router.py", line 920, in execute_with_sem
    first_chunk = next(it)
                  ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 523, in _convert_messages_iterator_to_target_type
    for messages in messages_iter:
                    ^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 383, in _format_and_cache
    for o in output:
             ^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 507, in _postprocess_messages_iterator
    for pre_msg in messages:
                   ^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1017, in retry_model_service_iterator
    num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\llm\base.py", line 1055, in _raise_or_delay
    raise ModelServiceError(exception=Exception(f'Maximum number of retries ({max_retries}) exceeded.')) from None
agent_cascade.llm.base.ModelServiceError: Maximum number of retries (2) exceeded.

2026-07-07 05:51:17,964 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 29929, Available tokens: 108931
2026-07-07 05:51:18,257 - oai.py - 315 - INFO - LLM infrastructure changed. Re-detecting context for: http://127.0.0.1:1234/v1
2026-07-07 05:51:18,278 - oai.py - 257 - DEBUG - Missing context metadata in list. Trying specific endpoint: http://127.0.0.1:1234/v1/models/qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved
2026-07-07 05:51:18,294 - oai.py - 278 - INFO - Model qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved found, but could not detect context length via API.
2026-07-07 05:51:59,520 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 31788, Available tokens: 108931
2026-07-07 05:52:25,720 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 33429, Available tokens: 108931
2026-07-07 05:52:43,135 - base.py - 953 - INFO - Agent [Coder] - ALL tokens: 34803, Available tokens: 108931

# code_interpreter output splitting - should be merged in one block
stdout:

```
2026-07-09 07:38:41,625 - log.py - 41 - INFO - done

```

stdout:

```
done
```

stdout:

```


```

stderr:

```
[33mWARNING: Running pip as the 'root' user can result in broken permissions and conflicting behaviour with the system package manager, possibly rendering your system unusable. It is recommended to use a virtual environment instead: https://pip.pypa.io/warnings/venv. Use the --root-user-action option if you know what you are doing and want to suppress this warning.[0m[33m
[0m
[1m[[0m[34;49mnotice[0m[1;39;49m][0m[39;49m A new release of pip is available: [0m[31;49m25.0.1[0m[39;49m -> [0m[32;49m26.1.2[0m
[1m[[0m[34;49mnotice[0m[1;39;49m][0m[39;49m To update, run: [0m[32;49mpip install --upgrade pip[0m

```


# EOF
