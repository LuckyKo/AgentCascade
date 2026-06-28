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
[x] improve list_dir tool — FIXED (3ec490c): added recursive listing, glob filtering (include/exclude), sorting (name/size/date/type), human-readable sizes, timestamps, summary stats, max_entries cap, symlink cycle detection
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
- [x] loop detectors kicks back to parent agent instead of doing rollback — FIXED: added retry loop + shared _recover_from_loop() helper
- [x] streaming seems to be odd (not really working) on Security and Compressor agents
- [ ] reading logs from workspace with code_intepreter seems to be an impossible task, investigate wtf is happening with out path mapping
- [ ] retry is broken, it deleted the user message too
- [ ] max tokens does not change when a new API endpoint is acquired 
- [x] randomly duplicated agent log entries for tool outputs
- [x] stop breaks something because i cant resume activity after, probably leaves allocate API slots stuck
- [x] loop detector triggers and just kicks back to parent instead of applying rollback and retrying — FIXED (same as above)
- [ ] images don't get properly pasted in chat
- [x] dismiss: all_idle is borked — FIXED (8a1d3cf, 3064488): fixed tuple mismatch in active_stack check, hardcoded 'Maine' guard, missing SLEEPING/halted checks; replaced clear_conversation with dismiss_instance for full cleanup; extracted _capture_log_path helper; added null guards
- [ ] max_tokens does not get updated when the API endpoint changes
- [ ] investigate if we can make shell cmd accept special character and multi-line `python -c` commands
      ERROR: 'charmap' codec can't encode character '\u2717' in position 0: character maps to <undefined>

# Errors to investigate:
- [x] **FIXED** Security agent returns properly formatted answer but system remained halted — root cause was Python closure late-binding bug in `_security_check()` (api_server.py:1903). Variables `rid`, `auto_apply`, `ap` were captured by reference, so when another message arrived and reassigned them, the security thread approved the wrong operation. Fix: added default argument binding `def _security_check(rid=rid, auto_apply=auto_apply, ap=ap, loop=loop)` to capture by value.
2026-06-28 06:39:46,160 - execution_engine.py - 649 - DEBUG - engine.run() ENTRY - instance=Security_op_15493c14
2026-06-28 06:39:46,160 - execution_engine.py - 706 - DEBUG - [SLOT_BYPASS] Skipping slot acquire - instance=Security_op_15493c14, class=Security (nested invocation)
2026-06-28 06:39:46,161 - execution_engine.py - 975 - INFO - [CACHE_REBUILD] Rebuilding working set for Security_op_15493c14
2026-06-28 06:39:46,161 - execution_engine.py - 1053 - DEBUG - [CACHE_REBUILD] System prompt content CHANGED for Security_op_15493c14
2026-06-28 06:39:46,163 - agent_instance_logger.py - 504 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\security_Security_op_15493c14_20260628_063946.jsonl with 2 messages.
2026-06-28 06:39:46,166 - base.py - 949 - INFO - Agent [Security] - ALL tokens: 315, Available tokens: 164578
2026-06-28 06:39:49,796 - execution_engine.py - 852 - DEBUG - [SLOT_FINAL] Before finally release - instance=Security_op_15493c14, slot_held=False
2026-06-28 06:39:49,815 - execution_engine.py - 2585 - DEBUG - [SLOT_RELEASE] _slot_release already None for Security_op_15493c14 during cleanup
2026-06-28 06:39:49,816 - execution_engine.py - 862 - DEBUG - [SLOT_FINAL] After finally release - instance=Security_op_15493c14, slot_still_held=False
2026-06-28 06:39:49,816 - execution_engine.py - 904 - DEBUG - EXIT - Security_op_15493c14 RUNNING→IDLE
2026-06-28 06:39:49,817 - api_server.py - 2302 - INFO - [SECURITY] Automatic Approval for op_bff24d98 with justification: Read-only `git diff --stat` operation comparing tw...
2026-06-28 06:39:49,817 - api_server.py - 2372 - DEBUG - [SECURITY] Released active check for op_bff24d98
2026-06-28 06:39:50,587 - api_server.py - 1986 - WARNING - Security check already active for request op_15493c14, ignoring duplicate.

# EOF
