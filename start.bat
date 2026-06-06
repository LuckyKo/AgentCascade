@echo off
REM Start the API server with root sub-agent mode enabled by default.
REM Feature flags:
REM   QWEN_USE_ROOT_SUBAGENT=1       -> enable UI/server root sub-agent flow
REM   QWEN_AUTO_START_ROOT_AGENT=1   -> auto-start the resolved root sub-agent at launch
REM   QWEN_ROOT_AGENT_CLASS=<name>    -> (optional) force which agent class to use

set QWEN_AGENT_IDLE_TIMEOUT=900
set QWEN_USE_ROOT_SUBAGENT=1
REM Ensure any inherited auto-start flag is cleared so the root sub-agent waits for user input.
set QWEN_AUTO_START_ROOT_AGENT=
REM To auto-start the root sub-agent at launch, set QWEN_AUTO_START_ROOT_AGENT=1 in this script or your environment.
REM To force a specific root agent class, uncomment and set the following:
REM set QWEN_ROOT_AGENT_CLASS=researcher

python start_api_server.py