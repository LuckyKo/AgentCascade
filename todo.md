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

# BUGS:

- [x] stop button should halt all operations too (like code_intepreter or shell_cmds)
- [x] resume button does not restart halted processes
- [x] cmd_shell timeout does not kill some python processes that hang, just the cmd window, leaving the python process stray and blocking the system
- [x] having issues with edit_file heuristic match mode, duplicating comments and messing up indentation. <- FIXED: removed comment stripping from heuristic matching pipeline. Heuristic mode now matches on raw content with only whitespace normalization. If old_content comments differ from file → match fails → LLM gets feedback to retry with correct content. Simpler, more predictable, prevents silent data loss.
- make the sub-agent tab names shrink to fit the width of the screen when they multiply beyond the visible limit / or they could pile on additional rows
- [x] compression agent polling times out at max_polls=1000 during forced compression (>95% context) — each LLM streaming chunk = one iteration, large tasks exceed 1000. Fix: replaced with time-based timeout (300s, monotonic clock).
- [x] dismiss_agent tool should return the list of log paths of closed agents in case they need to be resurrected.
- [x] dismissing an agent does not close the UI tab in real time, it current needs f5 to clear it.
- [x] unused/idle agents should be automatically dismissed after a period of inactivity — they sit idle consuming memory and context when not needed [x] FIXED: configurable idle_timeout (default 5min) with activity heartbeat on dispatch + completion, CLI args (--idle-timeout/--idle-check-interval) and env var fallbacks
- system_info tool does not show the mapped Docker paths for each work dir
- [x] when grep spillover is toggled in options we don't inform the model of the destination file — FIXED: spill path passed from orchestrator down through Grep tool to operation_manager; all 4 truncation points (subprocess early-trunc, subprocess re-check, Python fallback, single-file) write full output to spill file and include its workspace-relative path in the truncation notice. Orchestrator skips its own spillover writes for grep to avoid double-writing degraded content. Subprocess returns was_truncated flag for clean coordination.
- [x] orchestrator _truncate_tool_result strips grep truncation notice with spill path — FIXED: check for existing "[TOOL RESPONSE TRUNCATED" in tool_result and skip replacement if found, preserving operation_manager's spill file notice
- [x] subprocess grep truncation can result in "Found 0 matches" when all lines exceed char_limit — FIXED: _sub_truncated=True with count==0 no longer falls through to Python fallback; summary shows "Matches found [TRUNCATED]" instead of misleading "Found 0 matches"
- [x] inconsistent truncation notices — subprocess path omitted character count but Python fallback included it — FIXED: all 4 truncation points now include ({chars} chars); _try_subprocess_grep returns original_output_size for consistent reporting
- [x] both grep spillover and grep char limit get reset on refresh — FIXED: added explicit save lines with correct hyphenated keys in saveSettings()
- user injected messages cause context reprocessing. investigate message stack trashing
- streaming is fixed in sub-agent tabs but inconsistent on main chat (why are we even using different formatting path? they should look the same). display speed seems to not keep up with generation speed so maybe if a new bubble pops, we should just fill in the full prev bubble and continue streaming into the new one... or use some faster formatting code. (i hope we're not processing older bubbles that do not change)
- continue should not insert a new user message, just send the active agent_pool too the LLM so it can resume if it wants.
- the approval window sometimes disappears (when there are a lot of agent tabs mostly), have to hit F5 to make it show up again.
- message bubbles use no `md` formatting sometimes, its inconsistent.
- auto agent discard timer value should be added to agent settings

# EOF
