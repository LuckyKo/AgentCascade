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
[ ] Improve Activity bar when not streaming in tokens: show if we are in a process (compression/security audit etc) or waiting for a tool to respond. also make it catch the streamed token earlier so we don't need to do any complicated recalculation, make it a simple FIFO list of words.
[ ] move tool output spillover dir to \logs\spillover
[ ] unify the chat and subagent tabs (merge best of both). same for other logic inside - there should be no difference between orchestrator and other subagents, its only a call tree.
[ ] implement "branch" button on main chat message bubbles, branching an agent history from that point into a new session.
[ ] implement rate limits for each API endpoint to avoid spamming and getting locked out.
[ ] add console logging to server.

# BUGS:

- context usage bar is out of sync often, we should try to hand it the values calculated by base.py (the ones showing in console as: "2026-05-18 04:02:03,042 - base.py - 818 - INFO - Agent [Unknown] - ALL tokens: 44875, Available tokens: 85994")
- security advisor still fails with ambiguous result, even though the output seems totally fine according to our formatting rules. needs a full outcome parsing path audit or further debugging. 
- there seems to be a mismatch between the context slice of the llm active message que and the log file (the index where last context summary was inserted). after server restart i'm loading a different context usage than before. -- not sure if its consistent... we have to keep a watch tho
- web_ui issues with steaming: sometimes we dont complete writing the last part of the content in a bubble before popping a new one
- second use of "reasoning_content" tags breaks the system, both the web_ui (doesn't support multiple tag usage and starts writing back into the first thinking block) and our parsing logic, stopping the agents and messing up the toll call flow.
- delete_file tool is unusually slow, check that logic.
- save bubble edit doesn't actually save new edit.
- compression agent didn't switch APIs? Needs a check, it doesnt happen often
- need timeout protection on grep. wild searches can scan for hours, we should time it out in 30s
- cache miss on return to caller agent (after a short task). somthing changes in the stack at start, needs fix

# EOF
