# Agent Cascade

Agent Cascade is a modular, multi-agent system for complex operations, designed for maximum resilience and self-improvement.
The goal is to create a system that can operate autonomously for extended periods, learning from its mistakes and continuously improving itself.
It uses a modular, multi-agent architecture with a unique supervisor-worker dynamic that enables rapid adaptation and recovery from errors.

# Capabilities

- **Rollback on loops** - detect repeating patterns and roll back to a previous state. Overseer agent will get pinged to check why agents are looping and take action - including dismissing the misssbehaving agent if nescessary, with a notification.
- **Full memory persistence** - Agent logs are continuously written to a file and can be restored to any point in time.
- **Message queuing** - Agents can receive new messages while working on another task, and will process them in order.
- **Smart Truncation** - The system monitors incoming tool responses and truncates them based on user defined limits (nr of characters or tokens) to prevent overloading the context window. Spillover files are provided with full content.
- **Active Self-Improvement** - The Overseer agent checks working agents performance regularly, evaluates the system performance and suggests improvements to the prompts, configuration and even the framework itself (including the very prompts and configuration). All configurations and prompts are stored in the DNA directory, with plans to expand to multiple versions for A/B testing. Overseer will handle tracking and performance evaluation if different configs. We aim for most tasks completed sucessfully with least amount of token usage.

# TODO:

[ ] Add skills (custom agent loading)
[ ] Add an Overseer agent that periodically checks on the heath of the system, reads logs and telemetry, check if running agents got stuck in undetectable loops or migrated goals towards something that the user never asked for, suggests fixes and improvements into a suggestion box. Main agent will pull from the suggestion box during idle times when user is AFK to self improve the agents or the framework during our daily operation - do the whole DNA A/B testing thing. Overseeer agent will always get its full working que compressed when it finishes and save it into the suggestion box (no chat messages) - should be persistent across sessions. We'll set the interval at which it activates, it will silently interrupt running agents when it activates and resume them like it never happens when its done (unless it decides to kill an agent), or work in parallel using a different API endpoint. - big task, will do it after we stabilize the framework
[ ] need a memory consolidation task ran periodically - takes all summaries in log and arranges them in a neat continuous package like long term memory -> replaces last summary
[ ] warn agents about message limit at 90%
[ ] make cmd_shell pop open a console window in the back so the user can inspect or interact with it if needed.
[ ] improve list_dir tool to be as useful and even more than any shell command
[ ] add a banner above the user chat entry that shows queued messages (with an X to dismiss each one individually)
[ ] change USE_PREV_ARG system to a argument and (certain) tool output caching system. all tool arguments and certain outputs (like the result of a call_agent) longer than a certain threshold (line 1000 chars) get cached in a pool and can be inserted with {**USE_CHACHED_ENTRY_N**} in other tool arguments. system_info will display the truncated state of the cache pool. we'll use a rolling index to overwrite old entries in the pool with new ones. the system will use a toggle on/off in settings.

# Message stack update rules:

- add message/tool response/user msg (append): agent_pool - add; logs - add; UI - add
- user history edit (edit): agent_pool - in place; logs - in place; UI - rebuild
- user history delete (edit): agent_pool - in place; logs - in place; UI - rebuild
- compression (regen): agent_pool - rebuild; logs - rebuild; UI - rebuild
- rollback (edit): agent_pool - trim tail; logs - trim tail; UI - rebuild
- retry (edit): agent_pool - trim tail; logs - trim tail; UI - rebuild
- continue (resend): agent_pool - no; logs - no; UI - no
- call_agent (new): agent_pool - new; logs - new; UI - new
- call_agent (existing): agent_pool - add; logs - add; UI - add
- call_agent return (append): agent_pool - add; logs - add; UI - add
- new session (reset): agent_pool - new; logs - new; UI - new
- session load (replace from json): agent_pool - rebuild; logs - rebuild; UI - rebuild
- server startup (load last root agent json log): : agent_pool - rebuild; logs - rebuild; UI - rebuild

# BUGS:

- [ ] Activity banner still doesn't change when tools are written
- [ ] loop detectors kicks back to parent agent instead of doing rollbacks when the toggle is on, rolling back parent instead of child instance.
- [ ] streaming seems to be odd (not really working) on Security and Compressor agents
- [ ] reading logs from workspace with code_intepreter seems to be an impossible task, investigate wtf is happening with out path mapping
- [ ] retry is broken, it deleted the user message too
- [ ] max tokens does not change when a new API endpoint is acquired 
- [ ] stop is not quickly terminating streams and breaks something because i cant resume activity after
- [ ] compressor can use tools when i specifically disabled everything for it, can also compress recursively. compressor token limit also seems to be wrong
- [ ] compression causes loop detector to trigger and kick the agent back to parrent
- [ ] images don't get properly pasted in chat
- [ ] max context tokens return by the api router doesn't match the on in the endpoint setting
- [ ] investigate if we can make shell cmd accept special character and multi-line `python -c` commands
      ERROR: 'charmap' codec can't encode character '\u2717' in position 0: character maps to <undefined>

# Errors to investigate:
2026-06-26 03:05:51,001 - base.py - 946 - INFO - Agent [Researcher] - ALL tokens: 3324, Available tokens: 124226
2026-06-26 03:05:52,039 - base.py - 946 - INFO - Agent [Researcher] - ALL tokens: 3375, Available tokens: 124226
2026-06-26 03:05:53,087 - base.py - 946 - INFO - Agent [Researcher] - ALL tokens: 3428, Available tokens: 124226
2026-06-26 03:05:54,115 - execution_engine.py - 1207 - WARNING - Loop detected for CodebaseInvestigator: Detected repeated sequence loop (assistant, function, assistant, function, assistant, function, assistant, function, assistant, function repeating 2 times)
2026-06-26 03:05:54,116 - execution_engine.py - 828 - DEBUG - [SLOT_FINAL] Before finally release - instance=CodebaseInvestigator, slot_held=True
2026-06-26 03:05:54,127 - execution_engine.py - 2444 - DEBUG - [SLOT_RELEASE] Successfully released for CodebaseInvestigator during cleanup
2026-06-26 03:05:54,129 - execution_engine.py - 838 - DEBUG - [SLOT_FINAL] After finally release - instance=CodebaseInvestigator, slot_still_held=False
2026-06-26 03:05:54,132 - execution_engine.py - 873 - DEBUG - EXIT - CodebaseInvestigator RUNNING→IDLE
2026-06-26 03:05:54,133 - execution_engine.py - 2862 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=CodebaseInvestigator, reason=aborted, inst_type=AgentInstance, conv_len=2, final_resp_len=31
2026-06-26 03:05:54,135 - tool_dispatcher.py - 327 - DEBUG - [SLOT_SYNC_REACQUIRE] Attempting to re-acquire slot for 'Maine' after sync child
2026-06-26 03:05:54,137 - agent_pool.py - 1639 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-06-26 03:05:54,142 - tool_dispatcher.py - 336 - DEBUG - [SLOT_SYNC_REACQUIRED] Successfully re-acquired slot for 'Maine'. Total SYNC path elapsed: 55.94s
2026-06-26 03:05:54,145 - execution_engine.py - 1921 - ERROR - Tool call_agent failed for Maine: Loop detected for CodebaseInvestigator: Detected repeated sequence loop (assistant, function, assistant, function, assistant, function, assistant, function, assistant, function repeating 2 times)
2026-06-26 03:05:54,147 - execution_engine.py - 828 - DEBUG - [SLOT_FINAL] Before finally release - instance=Maine, slot_held=True
2026-06-26 03:05:54,148 - execution_engine.py - 2444 - DEBUG - [SLOT_RELEASE] Successfully released for Maine during cleanup
2026-06-26 03:05:54,149 - execution_engine.py - 838 - DEBUG - [SLOT_FINAL] After finally release - instance=Maine, slot_still_held=False
2026-06-26 03:05:54,149 - execution_engine.py - 856 - DEBUG - [FINAL_SYNC] Maine: Catching up 2 unlogged messages (logged=2, conversation=4)
2026-06-26 03:05:54,150 - execution_engine.py - 873 - DEBUG - EXIT - Maine RUNNING→IDLE

# EOF
