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

[x] Implement code_map tool to quickly map large code files - with line numbers of functions, classes, and variables. Should give a quick overview where each item is located (line nr of the item) for targeted reads. Should recognise multiple programming languages (Python, C++, Java, JS, etc) but also force parsing in a specific programming language (if the agent calls it with force_as: python for example)
[x] Implement parralel agents (subagents) running at the same time on diffrent tasks.
[x] Add multiple API endpoints for all LLMs, shown in a list to access in priority order (movable up/down). A toggle for each one if it's enabled or not, with a button to expand it for API KEY/model details.
[x] Add a button for DNA and agent soul refresh at any time. (replace the Retry button in left tab). Also change the Reset button to "New Session".
[x] Parametrize all internal prompts to easy swap for A-B testing (eventually creating a DNA that saves a specific configuration of the framework with propts and other parameters
[ ] Add skills (or cron job like system)
[x] Add read_logs tool, reading our logs using a special middle point truncation of our message entry lines - will alow quick inspection of the chat history logs without overloading the context window.
[x] On context compression we'll insert the compressed summary back into the message queue of the logs, at the same point it would be in our cached message queue; on session load/restore we'll read from latest summary onwards.
[x] Add context summary viewing/editing to Web UI.
[x] Change security prompt to be an individual security Expert agent, the Ask Agent during Aprooval operation becomes a regular agent call that is not included in our agent chat log (will be a separate verification system). Will give security extended ability to verify the safety of the file changes before aproving them, or even do internet searches for more info.
[x] Add a generalist agent focused on efficiency and speed.
[ ] Add an Overseer agent that periodically checks on the heath of the system, reads logs and telemetry, check if running agents got stuck in undetectable loops or migrated goals towards something that the user never asked for, suggests fixes and improvements into a sugesstion box. Main agent will pull from the sugesstion box during idle times when user is AFK to self improve the agents or the framework during our daily operation - do the whole DNA A/B testing thing. Overseeer agent will always get its full working que compressed when it finishes and save it into the suggestion box (no chat messages) - should be persistent across sessions. We'll set the interval at whitch it activates, it will silently interrupt running agents when it activates and resume them like it never happend when its done (unless it decides to kill an agent), or work in parralel using a different API endpoint.
[ ] Improve Activity bar when not streaming in tokens: show if we are in a process (compression/security audit etc) or waiting for a tool to respond.
[x] Add an close buton to subagent tabs to allow closing them (dismisses agent just like the tool call does).
[x] Change User messaging logic to an async queue system, able to receive new commnads from API calls, working similar to our existing async interruption system. Depending on what agent tab we have open in the web_ui, the messages will be sent to that particular agent (if a subagent is working and we switch tabs to maine and send one there, that message will be added to maine's message queue and will be processed once subagent finishes and returns focus to maine). The API calls needs to have E2E encryption, we'll make a separate app to interract with it for testing purposes, eventually it should be able to be called from regular messaging apps (like Telegram/Whatsapp etc).
[x] Change the way we do the output text limitation of all tools. Instead of limit the nr of lines read we'll use volume of characters. if it goes past a high limit (eg. 5000-10000 chars - set in settings) we can assume that it did some wild read and the output is probably useless, we'll truncate it as usual to around 500 tokens max, with spillover file. Properly done read_file with defined nr of lines to read will avoid this safety cap. We'll still keep that 25% of max token window hard limit too. We also need to avoid doing this on images, since that text gets reduced to around 512 tokens max anyway.
