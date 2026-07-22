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
[ ] make out path helper that tools use resolve extra_rw/ro paths just like code_intepreter does

# BUGS:

- [ ] no agent tab refresh during tool call streaming causes `Activity` bar to be still during tool writing process
- [ ] manually asking for security agent opinion does not fill it in and stop the security agent info once it reached conclusion, only happens on [YES]
- [ ] telemetry `Output Tokens (est)` severely undercounts
- [ ] we are pushing wrong summary from the inner loop detector if the compressor fails and gets stuck in a loop `[SYSTEM ERROR: Empty LLM response]`. it should try another API endpoint instead 
- [x] `Auto-continue` option fixed — extended detection beyond token truncation to catch incomplete states (mid-reasoning, mid-tool_call with unclosed JSON). Turn counter resets on auto-continue. Hover tooltip added explaining the feature.
- [x] agents get stopped randomly in the middle of streaming long reasoning — fixed: max-output-token guard + LLM backend defaults raised from 2048 → 8192 across all layers (execution_engine, transformers_llm, openvino, UI, JS fallback, API server). Template fallback bug fixed. Log level raised to INFO.
- [x] Add shell_cmd calls with `cd <path> && git diff` and similar safe read-only git operations to auto approval. — DONE: Extended _is_safe_readonly_shell_command to auto-approve safe read-only git operations (diff, status, log, etc.) including 'cd && git' patterns, while blocking chained commands and dangerous git subcommands/arguments. Handles -C/--git-dir flags. Note: does not handle git aliases.
- [ ] inner loop detector is almost unusable how many false positives generates, `char run` is the only good mode. pls make tests that simulate streaming as it happens normally, use rel existing logs to check for false positives.
- [x] approval timeout occurs even when explicitly disabled in options, when it was set on auto-ask mode — DONE: Security advisor used hard-coded 180s timeout constant instead of reading from operation_manager settings. Fixed `security_handler.run_check()` to dynamically read `enable_timeout` and `approval_timeout_seconds` from operation manager. Timeout message now shows actual configured value. Added None guards for safety.
- [ ] I dont want truncation of the user messages in the que (UI user que display)
- [x] scan_skills and propose_skill return `Error: Object of type coroutine is not JSON serializable`
- [x] auto-skill interferes with agent's final reply — DONE: multi-turn execution (AUTO_SKILL_EXTRA_TURNS=25), conversation rollback after skill creation, notice injected into last message 
- [x] something fucks up the agent jsonl log file and syncs it with the agent pool, submitting a version without messages between markers - it should NOT be in full sync, as per design doc. is it the auto-skill rollback?
- [x] max char limit on inner loop detector fails to trigger (also move the max char limit to the right inner loop section, or add a different one from the sampler options in global)
- [x] the auto-skill rollback leaves behind some leftover truncated message at the end in the UI tab history — DONE: replaced marker-based rollback with snapshot-based (target_length), added state reset to IDLE for SLEEPING/COMPLETING states
- [x] every time we want to test some AC modules in code_intepretet (fresh docker instance) the agents painstakingly fail and install each required package one by one wasting a LOT of time and tokens. FIXED: updated Dockerfile to pre-install AC core dependencies (pyyaml, pydantic, tiktoken, json5, jsonlines, jsonschema, python-dotenv, openai, dashscope, aiohttp, regex, eval_type_backport) alongside existing data science packages. Image rebuilt and verified.
- [x] Auto-skill interferes with `Stop` command — DONE: added optional `check_stop_fn` parameter to `trigger_auto_skill_reflection()`, both callers (execution_engine.py, run_agent_unified.py) pass stop check lambdas. Extra-turn loop breaks early before executing a turn if stopped.
- [x] resurrecting an agent from log does not bring up its agent tab in UI, on refresh I even lose the root agent tab. calling an agent with the same name as the caller (child spawning) also generates this issue. (FIX: _dismiss_all_instances preserves caller via exclude set, renderSubAgents uses direct DOM activation, call_agent skips dismiss-all step)
- [ ] UI streaming stops on `pause`. it should not, pause should ONLY stop the tool response logic.
- [ ] some of the UI setting are getting reset on browser/system restart (they stick on refresh though)
- [x] grep char limit truncation now provides spillover files — shared `truncate_with_spillover()` helper created in tool_utils.py, used by grep (4 sites) and shell_cmd (1 site). Backward compat `spill_file_path` parameter preserved.
- [x] async shell follow-up commands (`__status`, `__kill`, `__ctrl_c`, `__heartbeat=N`, stdin input) now auto-approved via `__` prefix check in `_is_safe_readonly_shell_command()`
- [x] After changes to Security agent soul shell_cmd fails with this: `REJECTED: Security check error: No template for agent class Security`
- [ ] forced compression seems lazy, waits for a agent call to already happen when over the limit instead of triggering before that
- [ ] remove context window limit truncation of tool response, we already have wild read truncation for extremes and with the fix from above it should be unnecessary
- [ ] inner loop API fallback should only apply if we hit the `char run` detect specifically, not for the others types (its a fix for a particular local LLM lock issue)
- [ ] check agent recall by instance name (existing agents on idle), i think it has been broken recently, change back whatever dub shit broke it


# Errors to investigate:


# API endpoint errors
Endpoint 'deepseek-v4-flash-free' @ https://opencode.ai/zen/v1 attempt 1/2: Messages can not be empty.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade\agent_cascade\api_router.py", line 1127, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\api_router.py", line 1068, in execute_with_sem
    result = call_fn(llm_cfg, *args, **kwargs)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\execution_engine.py", line 2774, in _do_call
    return llm.chat(
           ^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\llm\base.py", line 235, in chat
    raise ValueError('Messages can not be empty.')
ValueError: Messages can not be empty.

Endpoint 'deepseek-v4-flash-free' @ https://opencode.ai/zen/v1 attempt 2/2: Messages can not be empty.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade\agent_cascade\api_router.py", line 1127, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\api_router.py", line 1068, in execute_with_sem
    result = call_fn(llm_cfg, *args, **kwargs)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\execution_engine.py", line 2774, in _do_call
    return llm.chat(
           ^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\llm\base.py", line 235, in chat
    raise ValueError('Messages can not be empty.')
ValueError: Messages can not be empty.

Endpoint 'grok-4.1-fast' @ http://127.0.0.1:4315/v1 attempt 1/2: Messages can not be empty.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade\agent_cascade\api_router.py", line 1127, in call_with_fallback
    result = execute_with_sem(current_agent_name)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\api_router.py", line 1068, in execute_with_sem
    result = call_fn(llm_cfg, *args, **kwargs)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\execution_engine.py", line 2774, in _do_call
    return llm.chat(
           ^^^^^^^^^
  File "n:\work\WD\AgentCascade\agent_cascade\llm\base.py", line 235, in chat
    raise ValueError('Messages can not be empty.')
ValueError: Messages can not be empty.

Endpoint 'grok-4.1-fast' @ http://127.0.0.1:4315/v1 attempt 2/2: Messages can not be empty.
Traceback: Traceback (most recent call last):
  File "n:\work\WD\AgentCascade\agent_cascade\api_router.py", line 1127, in call_with_fallback
    result = execute_with_sem(current_agent_name)