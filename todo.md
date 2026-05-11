# TODO:

[x] Add telemetry for agent performace and tool usage effectiveness tracking
[ ] Add multiple API endpoints for all LLMs, shown in a list to access in priority order (movable up/down via arrows). A toggle for each one if it's enabled or not, with a button to expand it for API KEY/model details.
[x] Add a button for DNA and agent soul refresh at any time. (replace the Retry button in left tab). Also change the Reset button to "New Session".
[x] Parametrize all internal prompts to easy swap for A-B testing (eventually creating a DNA that saves a specific configuration of the framework with propts and other parameters
[ ] Add skills (or cron job like system) 
[x] Add read_logs tool, reading our logs using a special middle point truncation of our message entry lines - will alow quick inspection of the chat history logs without overloading the context window.
[x] On context compression we'll insert the compressed summary back into the message queue of the logs, at the same point it would be in our cached message queue; on session load/restore we'll read from latest summary onwards.
[x] Add context summary viewing/editing to Web UI.
[x] Change security prompt to be an individual security Expert agent, the Ask Agent during Aprooval operation becomes a regular agent call that is not included in our agent chat log (will be a separate verification system). Will give security extended ability to verify the safety of the file changes before aproving them, or even do internet searches for more info..
[x] Add a generalist agent focused on efficiency and speed.
[ ] Add an Overseer agent that periodically check on the heath of the system, reads logs and telemetry to suggest fixes and improvements. Main agent will pull from the sugesstion box during idle times when user is AFK to self improve the agents or the framework during our daily operation.
[ ] Improve Activity bar when not streaming in tokens: show if we are in a process (compression/security audit etc) or waiting for a tool to respond.
[x] Add an close buton to subagent tabs to allow closing them (dismisses agent just like the tool call does).