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

[ ] Add skills (or cron job like system)
[ ] Add an Overseer agent that periodically checks on the heath of the system, reads logs and telemetry, check if running agents got stuck in undetectable loops or migrated goals towards something that the user never asked for, suggests fixes and improvements into a suggestion box. Main agent will pull from the suggestion box during idle times when user is AFK to self improve the agents or the framework during our daily operation - do the whole DNA A/B testing thing. Overseeer agent will always get its full working que compressed when it finishes and save it into the suggestion box (no chat messages) - should be persistent across sessions. We'll set the interval at which it activates, it will silently interrupt running agents when it activates and resume them like it never happens when its done (unless it decides to kill an agent), or work in parallel using a different API endpoint. - big task, will do it after we stabilize the framework
[x] move tool output spillover dir to \logs\spillover
[ ] unify the chat and subagent tabs (merge best of both). same for other logic inside - there should be no difference between orchestrator and other subagents, its only a call tree.
[ ] implement "branch" button on main chat message bubbles, branching an agent history from that point into a new session.
[ ] implement rate limits for each API endpoint to avoid spamming and getting locked out.
[ ] need a memory consolidation task ran periodically - takes all summaries in log and arranges them in a neat continuous package like long term memory -> replaces last summary
[ ] warn agents about message limit at 90%
[ ] make cmd_shell pop open a console window in the back so the user can inspect or interact with it if needed.
[ ] improve list_dir tool to be as useful and even more than any shell command
[ ] add a banner above the user chat entry that shows queued messages (with an X to dismiss each one individually)
[ ] implement syntax_check tool, a global tool that auto-detects code type and verifies syntax (will replace python_compile)  
[ ] implement re_indent tool, used to realign blocks of code
[ ] Message stack update rules:
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
- [ ] we are STILL getting reprocessing on the user message que drain, mostly after returning from a call_agent.
- [ ] delete_file tool should move the files into the backup folder, similar to the edit_file backups.
- [ ] loop detectors kicks back to parent agent instead of doing rollbacks when the toggle is on
- [ ] streaming seems to be odd on Security and Compressor agents
- [ ] Compression formatting got slimmed down too much, missing the <summary> ... </summary>
- [ ] retry is broken, it deleted the user message too
- [ ] continue duplicates the last message
- [ ] images don't get properly pasted in chat
- [ ] max context tokens return by the api router doesn't match the on in the endpoint setting
- [ ] investigate if we can make shell cmd accept special character and multi-line `python -c` commands
ERROR: 'charmap' codec can't encode character '\u2717' in position 0: character maps to <undefined>

- [ ] compression failed with: `Compression corrupted pool: Forced compression and recovery both failed for GitInvestigator. Agent halted to prevent corruption.`
2026-06-22 01:57:29,498 - pool_validation.py - 61 - WARNING - [MSG POOL VALIDATION] Duplicate consecutive msg at index 87 for 'GitInvestigator'
2026-06-22 01:57:29,499 - pool_validation.py - 61 - WARNING - [MSG POOL VALIDATION] Duplicate consecutive msg at index 94 for 'GitInvestigator'
2026-06-22 01:57:29,500 - pool_validation.py - 61 - WARNING - [MSG POOL VALIDATION] Duplicate consecutive msg at index 95 for 'GitInvestigator'
2026-06-22 01:57:29,500 - pool_validation.py - 61 - WARNING - [MSG POOL VALIDATION] Duplicate consecutive msg at index 96 for 'GitInvestigator'
2026-06-22 01:57:29,500 - pool_validation.py - 68 - ERROR - [MSG POOL VALIDATION] Excessive duplicates (21/101) for agent 'GitInvestigator'
2026-06-22 01:57:29,501 - handler.py - 266 - ERROR - Recovery from log also failed — message pool may be corrupted
2026-06-22 01:57:29,501 - handler.py - 296 - DEBUG - Logger already synced after compression for 'GitInvestigator': pool=53, logged=101
2026-06-22 01:57:29,505 - execution_engine.py - 809 - DEBUG - [SLOT_FINAL] Before finally release - instance=GitInvestigator, slot_held=True
2026-06-22 01:57:29,510 - execution_engine.py - 2398 - DEBUG - [SLOT_RELEASE] Successfully released for GitInvestigator during cleanup
2026-06-22 01:57:29,511 - execution_engine.py - 819 - DEBUG - [SLOT_FINAL] After finally release - instance=GitInvestigator, slot_still_held=False
2026-06-22 01:57:29,511 - execution_engine.py - 854 - DEBUG - EXIT - GitInvestigator RUNNING→IDLE
2026-06-22 01:57:29,512 - execution_engine.py - 2813 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=GitInvestigator, reason=completed, inst_type=AgentInstance, conv_len=100, final_resp_len=67
2026-06-22 01:57:29,512 - tool_dispatcher.py - 320 - DEBUG - [SLOT_SYNC_CHILD_COMPLETE] Sync child 'GitInvestigator' completed in 381.62s
2026-06-22 01:57:29,513 - tool_dispatcher.py - 327 - DEBUG - [SLOT_SYNC_REACQUIRE] Attempting to re-acquire slot for 'Maine' after sync child
2026-06-22 01:57:29,513 - agent_pool.py - 1527 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-06-22 01:57:29,514 - tool_dispatcher.py - 336 - DEBUG - [SLOT_SYNC_REACQUIRED] Successfully re-acquired slot for 'Maine'. Total SYNC path elapsed: 381.62s
2026-06-22 01:57:29,514 - tool_dispatcher.py - 116 - DEBUG - handle_call_agent returned type=str
2026-06-22 01:57:29,528 - tool_dispatcher.py - 498 - DEBUG - call_agent nesting - Maine depth=1/10
2026-06-22 01:57:29,528 - tool_dispatcher.py - 302 - DEBUG - [SLOT_SYNC_RELEASE] Releasing slot for 'Maine' before running sync child 'GitInvestigator'
2026-06-22 01:57:29,529 - execution_engine.py - 2398 - DEBUG - [SLOT_RELEASE] Successfully released for Maine during sync child
2026-06-22 01:57:29,529 - tool_dispatcher.py - 306 - DEBUG - [SLOT_SYNC_RELEASE] Slot released for 'Maine', active agents can now acquire
2026-06-22 01:57:29,530 - tool_dispatcher.py - 316 - DEBUG - [SLOT_SYNC_CHILD_START] Starting sync child 'GitInvestigator' (coder) for caller 'Maine'
2026-06-22 01:57:29,530 - execution_engine.py - 2705 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target=GitInvestigator, class=coder, caller=Maine, nest_depth=1, force_fresh=False
2026-06-22 01:57:29,531 - lifecycle_manager.py - 150 - DEBUG - [INSTANCE REUSE] 'GitInvestigator' (coder) reusing existing inactive instance. Conversation history will be preserved and extended.
2026-06-22 01:57:29,535 - agent_instance_logger.py - 466 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\coder_GitInvestigator_20260622_014731.jsonl with 104 messages.
2026-06-22 01:57:29,537 - execution_engine.py - 2746 - DEBUG - starting engine.run() for GitInvestigator
2026-06-22 01:57:29,537 - execution_engine.py - 642 - DEBUG - engine.run() ENTRY - instance=GitInvestigator
2026-06-22 01:57:29,545 - execution_engine.py - 523 - DEBUG - [SLOT_ACQUIRE] initial - instance=GitInvestigator, class=coder
2026-06-22 01:57:29,546 - agent_pool.py - 1527 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=coder, instance_name=GitInvestigator, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-06-22 01:57:29,546 - execution_engine.py - 530 - DEBUG - [SLOT_ACQUIRED] initial - instance=GitInvestigator, has_callback=True
2026-06-22 01:57:29,547 - execution_engine.py - 915 - INFO - [CACHE_MISMATCH] GitInvestigator: conv=55, cached=57 — forcing rebuild to resync
2026-06-22 01:57:29,547 - execution_engine.py - 925 - INFO - [CACHE_REBUILD] Rebuilding working set for GitInvestigator
2026-06-22 01:57:29,548 - execution_engine.py - 1004 - DEBUG - [CACHE_REBUILD] System prompt content CHANGED for GitInvestigator
2026-06-22 01:57:29,551 - agent_instance_logger.py - 466 - INFO - Rewrote agent log n:\work\WD\AgentWorkspace\logs\coder_GitInvestigator_20260622_014731.jsonl with 104 messages.
2026-06-22 01:57:29,552 - execution_engine.py - 809 - DEBUG - [SLOT_FINAL] Before finally release - instance=GitInvestigator, slot_held=True
2026-06-22 01:57:29,552 - execution_engine.py - 2398 - DEBUG - [SLOT_RELEASE] Successfully released for GitInvestigator during cleanup
2026-06-22 01:57:29,553 - execution_engine.py - 819 - DEBUG - [SLOT_FINAL] After finally release - instance=GitInvestigator, slot_still_held=False
2026-06-22 01:57:29,557 - execution_engine.py - 854 - DEBUG - EXIT - GitInvestigator RUNNING→IDLE
2026-06-22 01:57:29,558 - execution_engine.py - 2813 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=GitInvestigator, reason=completed, inst_type=AgentInstance, conv_len=55, final_resp_len=0
2026-06-22 01:57:29,558 - tool_dispatcher.py - 320 - DEBUG - [SLOT_SYNC_CHILD_COMPLETE] Sync child 'GitInvestigator' completed in 0.03s
2026-06-22 01:57:29,563 - tool_dispatcher.py - 327 - DEBUG - [SLOT_SYNC_REACQUIRE] Attempting to re-acquire slot for 'Maine' after sync child
2026-06-22 01:57:29,563 - agent_pool.py - 1527 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-06-22 01:57:29,564 - tool_dispatcher.py - 336 - DEBUG - [SLOT_SYNC_REACQUIRED] Successfully re-acquired slot for 'Maine'. Total SYNC path elapsed: 0.05s
2026-06-22 01:57:29,565 - tool_dispatcher.py - 116 - DEBUG - handle_call_agent returned type=str
2026-06-22 01:57:29,590 - base.py - 946 - INFO - Agent [Orchestrator] - ALL tokens: 6872, Available tokens: 78323

- [ ] System stuck after stop during call_agent tool write
2026-06-21 05:17:45,195 - api_server.py - 1401 - INFO - Stop: Transitioned Maine from RUNNING to IDLE
2026-06-21 05:17:45,196 - api_server.py - 1401 - INFO - Stop: Transitioned SessionLoadDebug3 from RUNNING to IDLE
2026-06-21 05:17:45,199 - api_server.py - 1435 - DEBUG - [STOP_STACK_CLEANUP] Cleaning active_stack: [('SessionLoadDebug3', 1)]
2026-06-21 05:17:45,199 - api_server.py - 1449 - DEBUG - [STOP_HALTED_CLEANUP] Cleared _halted_instances
2026-06-21 05:17:57,181 - execution_engine.py - 835 - DEBUG - [SLOT_FINAL] Before finally release - instance=SessionLoadDebug3, slot_held=True
2026-06-21 05:17:57,181 - execution_engine.py - 2418 - DEBUG - [SLOT_RELEASE] Successfully released for SessionLoadDebug3 during cleanup
2026-06-21 05:17:57,184 - execution_engine.py - 845 - DEBUG - [SLOT_FINAL] After finally release - instance=SessionLoadDebug3, slot_still_held=False
2026-06-21 05:17:57,184 - execution_engine.py - 884 - DEBUG - EXIT - SessionLoadDebug3 in IDLE state
2026-06-21 05:17:57,187 - execution_engine.py - 2833 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=SessionLoadDebug3, reason=completed, inst_type=AgentInstance, conv_len=2, final_resp_len=0
2026-06-21 05:17:57,199 - tool_dispatcher.py - 320 - DEBUG - [SLOT_SYNC_CHILD_COMPLETE] Sync child 'SessionLoadDebug3' completed in 19.44s
2026-06-21 05:17:57,199 - tool_dispatcher.py - 327 - DEBUG - [SLOT_SYNC_REACQUIRE] Attempting to re-acquire slot for 'Maine' after sync child
2026-06-21 05:17:57,199 - agent_pool.py - 1480 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-06-21 05:17:57,199 - tool_dispatcher.py - 336 - DEBUG - [SLOT_SYNC_REACQUIRED] Successfully re-acquired slot for 'Maine'. Total SYNC path elapsed: 19.44s
2026-06-21 05:17:57,200 - tool_dispatcher.py - 116 - DEBUG - handle_call_agent returned type=str
2026-06-21 05:17:57,281 - execution_engine.py - 601 - DEBUG - Draining 2 item(s) for Maine.
2026-06-21 05:17:57,281 - execution_engine.py - 835 - DEBUG - [SLOT_FINAL] Before finally release - instance=Maine, slot_held=True
2026-06-21 05:17:57,283 - execution_engine.py - 2418 - DEBUG - [SLOT_RELEASE] Successfully released for Maine during cleanup
2026-06-21 05:17:57,283 - execution_engine.py - 845 - DEBUG - [SLOT_FINAL] After finally release - instance=Maine, slot_still_held=False
2026-06-21 05:17:57,283 - execution_engine.py - 884 - DEBUG - EXIT - Maine in IDLE state
2026-06-21 05:18:28,194 - execution_engine.py - 668 - DEBUG - engine.run() ENTRY - instance=Maine
2026-06-21 05:18:28,194 - execution_engine.py - 548 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-06-21 05:18:28,196 - agent_pool.py - 1480 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-06-21 05:18:28,214 - execution_engine.py - 555 - DEBUG - [SLOT_ACQUIRED] initial - instance=Maine, has_callback=True
2026-06-21 05:18:28,216 - execution_engine.py - 601 - DEBUG - Draining 1 item(s) for Maine.
2026-06-21 05:19:10,866 - api_server.py - 1401 - INFO - Stop: Transitioned Maine from RUNNING to IDLE
2026-06-21 05:19:10,866 - api_server.py - 1435 - DEBUG - [STOP_STACK_CLEANUP] Cleaning active_stack: []
2026-06-21 05:19:10,868 - api_server.py - 1449 - DEBUG - [STOP_HALTED_CLEANUP] Cleared _halted_instances
2026-06-21 05:19:14,532 - execution_engine.py - 668 - DEBUG - engine.run() ENTRY - instance=Maine
2026-06-21 05:19:14,532 - execution_engine.py - 548 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-06-21 05:19:14,549 - agent_pool.py - 1480 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-06-21 05:19:18,218 - api_server.py - 1401 - INFO - Stop: Transitioned Maine from RUNNING to IDLE
2026-06-21 05:19:18,218 - api_server.py - 1435 - DEBUG - [STOP_STACK_CLEANUP] Cleaning active_stack: []
2026-06-21 05:19:18,220 - api_server.py - 1449 - DEBUG - [STOP_HALTED_CLEANUP] Cleared _halted_instances
2026-06-21 05:19:20,801 - execution_engine.py - 668 - DEBUG - engine.run() ENTRY - instance=Maine
2026-06-21 05:19:20,802 - execution_engine.py - 548 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-06-21 05:19:20,820 - agent_pool.py - 1480 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-06-21 05:19:22,317 - api_server.py - 1401 - INFO - Stop: Transitioned Maine from RUNNING to IDLE
2026-06-21 05:19:22,317 - api_server.py - 1435 - DEBUG - [STOP_STACK_CLEANUP] Cleaning active_stack: []
2026-06-21 05:19:22,319 - api_server.py - 1449 - DEBUG - [STOP_HALTED_CLEANUP] Cleared _halted_instances
2026-06-21 05:19:26,545 - execution_engine.py - 668 - DEBUG - engine.run() ENTRY - instance=Maine
2026-06-21 05:19:26,547 - execution_engine.py - 548 - DEBUG - [SLOT_ACQUIRE] initial - instance=Maine, class=orchestrator
2026-06-21 05:19:26,563 - agent_pool.py - 1480 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=orchestrator, instance_name=Maine, api_base=http://localhost:1234/v1, concurrency_limit=0
2026-06-21 05:19:44,556 - agent_pool.py - 1489 - ERROR - Failed to acquire endpoint slot for Maine: Timed out after 30s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
2026-06-21 05:19:44,556 - execution_engine.py - 560 - ERROR - [SLOT_ACQUIRE_FAILED] initial for Maine: Timed out after 30s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
2026-06-21 05:19:44,559 - api_integration.py - 392 - ERROR - Execution failed for Maine: Timed out after 30s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
2026-06-21 05:19:50,834 - agent_pool.py - 1489 - ERROR - Failed to acquire endpoint slot for Maine: Timed out after 30s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
2026-06-21 05:19:50,834 - execution_engine.py - 560 - ERROR - [SLOT_ACQUIRE_FAILED] initial for Maine: Timed out after 30s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
2026-06-21 05:19:50,836 - api_integration.py - 392 - ERROR - Execution failed for Maine: Timed out after 30s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
2026-06-21 05:19:56,568 - agent_pool.py - 1489 - ERROR - Failed to acquire endpoint slot for Maine: Timed out after 30s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
2026-06-21 05:19:56,568 - execution_engine.py - 560 - ERROR - [SLOT_ACQUIRE_FAILED] initial for Maine: Timed out after 30s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
2026-06-21 05:19:56,570 - api_integration.py - 392 - ERROR - Execution failed for Maine: Timed out after 30s waiting for endpoint slot on http://localhost:1234/v1. Current active count: 1, max allowed: 1. Currently held by: Maine (orchestrator)
2026-06-21 05:22:03,089 - code_interpreter.py - 142 - WARNING - Code interpreter watchdog: Kernel 0e66fb45-6026-4352-a7e6-636bdb5cc435_20048 inactive for 300s. Killing container.



# EOF
