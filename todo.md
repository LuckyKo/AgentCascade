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
[x] implement async shell_cmd launch (immediate tool response that it was launched, runs in background while agent keeps running and return final output as user message when done, can have heartbeat value that will periodically send console output back to caller agent) — DONE: AsyncShellTracker module with per-agent ID counters, max 5 concurrent shells, heartbeat injection via message queue, process tree cleanup on dismiss
[x] make cmd_shell pop open a console window in the back so the user can inspect or interact with it if needed. — DONE: CREATE_NEW_CONSOLE flag on Windows Popen launch in AsyncShellTracker
[ ] add auto-rollback feature on edit_file fail
[ ] implement a live scratchpad tool that injects text/image data into the last few FUNCTION/USER messages. the tool can load a live view of a file's content, console output of a program by PID, interface capture data of a program by PID, set persistence distance (nr of messages in tail agent pool retaining the data, older messages get the data trimmed). agent can call this tool to enable disable this scratchpad (disable by setting persistence to 0, defaults on 2) 
[ ] add a stop button to shell_cmd messages so user can terminate them early
[x] disable tools for the last turn of an agent so its forced to return a final answer — FIXED: disabled all tool schemas on last turn via instance._generate_cfg_override['disabled_tools'], cleaned up after LLM call returns.

# BUGS:

- [ ] no agent tab refresh during tool call streaming causes `Activity` bar to be still during tool writing process
- [ ] manually asking for security agent opinion does not fill it in and stop the security agent info once it reached conclusion, only happens on [YES]
- [ ] telemetry `Output Tokens (est)` severely undercounts
- [ ] we are pushing wrong summary from the inner loop detector if the compressor fails and gets stuck in a loop `[SYSTEM ERROR: Empty LLM response]` 


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
