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

# BUGS:

- [x] system prompt metadata lacks RO/RW paths set in UI — FIXED: reads from operation_manager now
- [ ] chat window must auto scroll on bottom, decouple if user scrolls up, recouple if he scrolls back to bottom
- [ ] no streaming can be seen in the message bubbles, not even the bubbles popping in. i have to fiddle with the UI to get any updates.
- [ ] no words change in activity bar during streaming
- [x] secondary agent tabs pop on late after call_agent and they are blank.


# EOF
