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

[ ] Add skills (custom agent loading?). there are some pre-existing modules from Qwen agent that deal with skills that havent been integrated, investigate hw they could be incorporated in AC, alternatives or improvements to that.
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

# Some errors in tools
2026-07-17 12:25:04,311 - base.py - 994 - INFO - Agent [Security] - ALL tokens: 6459, Available tokens: 164667
2026-07-17 12:25:06,453 - simple_doc_parser.py - 450 - INFO - Start parsing n:\work\WD\AgentWorkspace\file:///N:/work/WD/AgentCascade_unified/agent_cascade/execution_engine.py...
2026-07-17 12:25:06,455 - utils.py - 140 - ERROR - Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\tools\simple_doc_parser.py", line 446, in call
    parsed_file = self.db.get(cached_name_ori)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\tools\storage.py", line 92, in get
    raise KeyNotExistsError(f'Get Failed: {key} does not exist')
agent_cascade.tools.storage.KeyNotExistsError: Get Failed: 74f5fcaeecf752ea24624701337bae4e0e38896dcc6c7d2959455299f0370f53_ori does not exist

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "n:\work\WD\AgentCascade_unified\agent_cascade\utils\utils.py", line 347, in get_file_type
    content = read_text_from_file(path)
              ^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\utils\utils.py", line 302, in read_text_from_file
    with open(path, 'r', encoding='utf-8') as file:
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
OSError: [Errno 22] Invalid argument: 'n:\\work\\WD\\AgentWorkspace\\file:///N:/work/WD/AgentCascade_unified/agent_cascade/execution_engine.py'

2026-07-17 12:25:06,456 - agent.py - 251 - WARNING - Tool `web_extractor` reported a service error:

Error code: ValueError. Error message: Failed: The current parser does not support this file type! Supported types: pdf/docx/pptx/md/txt/html/csv/tsv/xlsx/xls
2026-07-17 12:25:06,475 - base.py - 994 - INFO - Agent [Security] - ALL tokens: 6549, Available tokens: 164667
2026-07-17 12:25:08,789 - base.py - 994 - INFO - Agent [Security] - ALL tokens: 7430, Available tokens: 164667


2026-07-17 13:32:51,973 - simple_doc_parser.py - 448 - INFO - Read parsed https://docs.langchain.com/oss/python/langchain/tools from cache.
2026-07-17 13:32:51,996 - tool_dispatcher.py - 747 - INFO - Wrote spillover file for 'web_extractor' result of langchain_tools_deep_dive_researcher: 50540 chars -> logs/spillover/langchain_tools_deep_dive_researcher_web_extractor_20260717_133251_993003.txt
2026-07-17 13:32:51,999 - base.py - 994 - INFO - Agent [Researcher] - ALL tokens: 19743, Available tokens: 124340
2026-07-17 13:32:55,903 - simple_doc_parser.py - 450 - INFO - Start parsing https://raw.githubusercontent.com/langchain-ai/langchain/main/libs/experimental/langchain_experimental/utilities/python_repl.py...
2026-07-17 13:32:56,165 - utils.py - 274 - INFO - Downloading https://raw.githubusercontent.com/langchain-ai/langchain/main/libs/experimental/langchain_experimental/utilities/python_repl.py to n:\work\WD\AgentWorkspace\tools\simple_doc_parser\f336f8f29434878dfb5059cb6019ae23c0ba29613e663579e5b5de5a719947ea\python_repl.py...
2026-07-17 13:32:56,263 - agent.py - 260 - WARNING - An error occurred when calling tool `web_extractor`:
ValueError: Can not download this file. Please check your network or the file link.
Traceback:
  File "n:\work\WD\AgentCascade_unified\agent_cascade\agent.py", line 248, in _call_tool
    tool_result = tool.call(tool_args, **kwargs)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\tools\web_extractor.py", line 44, in call
    parsed_web = self.simple_doc_parser.call({'url': url})
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\tools\simple_doc_parser.py", line 466, in call
    path = save_url_to_local_work_dir(path, tmp_file_root)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "n:\work\WD\AgentCascade_unified\agent_cascade\utils\utils.py", line 289, in save_url_to_local_work_dir
    raise ValueError('Can not download this file. Please check your network or the file link.')

2026-07-17 13:32:56,302 - base.py - 994 - INFO - Agent [Researcher] - ALL tokens: 20511, Available tokens: 124340


# Agent launched in async mode when it shouldnt?
2026-07-17 23:10:55,952 - execution_engine.py - 4103 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=code_execution_research_1, reason=completed, inst_type=AgentInstance, conv_len=14, final_resp_len=44
2026-07-17 23:10:55,955 - tool_dispatcher.py - 401 - DEBUG - [SLOT_SYNC_CHILD_COMPLETE] Sync child 'code_execution_research_1' completed in 87.01s
2026-07-17 23:10:55,955 - tool_dispatcher.py - 414 - DEBUG - [SLOT_SYNC_REACQUIRE] Attempting to re-acquire slot for 'skill_researcher_1' after sync child
2026-07-17 23:10:55,956 - agent_pool.py - 2085 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — agent_class=researcher, instance_name=skill_researcher_1, api_base=http://127.0.0.1:1234/v1, concurrency_limit=0
2026-07-17 23:10:55,957 - tool_dispatcher.py - 423 - DEBUG - [SLOT_SYNC_REACQUIRED] Successfully re-acquired slot for 'skill_researcher_1'. Total SYNC path elapsed: 87.01s
2026-07-17 23:10:55,957 - tool_dispatcher.py - 124 - DEBUG - handle_call_agent returned type=str
2026-07-17 23:10:55,975 - base.py - 994 - INFO - Agent [Researcher] - ALL tokens: 13273, Available tokens: 123165
2026-07-17 23:11:03,036 - tool_dispatcher.py - 563 - DEBUG - call_agent nesting - skill_researcher_1 depth=1/10
2026-07-17 23:11:03,037 - tool_dispatcher.py - 447 - DEBUG - Taking ASYNC path - skill_researcher_1 calls code_execution_research_2/researcher at depth 1
2026-07-17 23:11:03,040 - execution_engine.py - 3928 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target=code_execution_research_2, class=researcher, caller=skill_researcher_1, nest_depth=1, force_fresh=False
2026-07-17 23:11:03,040 - tool_dispatcher.py - 461 - DEBUG - ASYNC - code_execution_research_2 launched by skill_researcher_1
2026-07-17 23:11:03,040 - lifecycle_manager.py - 193 - DEBUG - [CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for code_execution_research_2
2026-07-17 23:11:03,041 - tool_dispatcher.py - 124 - DEBUG - handle_call_agent returned type=str
2026-07-17 23:11:03,041 - agent_pool.py - 603 - DEBUG - Instance conversation cleanup key missing (expected): 'code_execution_research_1'
2026-07-17 23:11:03,042 - agent_pool.py - 603 - DEBUG - Instance conversation cleanup key missing (expected): 'code_execution_research_2'
2026-07-17 23:11:03,054 - agent_instance_logger.py - 130 - DEBUG - Copied session from n:\work\WD\AgentWorkspace\logs\researcher_skill_researcher_1_20260717_230911.jsonl to n:\work\WD\AgentWorkspace\logs\researcher_code_execution_research_2_20260717_231103.jsonl
2026-07-17 23:11:03,057 - base.py - 994 - INFO - Agent [Researcher] - ALL tokens: 13498, Available tokens: 123165