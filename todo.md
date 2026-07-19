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
[ ] investigate possible use of `https://github.com/eugeniughelbur/obsidian-second-brain`for our lessons file management

# BUGS:

- [ ] no agent tab refresh during tool call streaming causes `Activity` bar to be still during tool writing process
- [ ] manually asking for security agent opinion does not fill it in and stop the security agent info once it reached conclusion, only happens on [YES]
- [ ] telemetry `Output Tokens (est)` severely undercounts
- [ ] we are pushing wrong summary from the inner loop detector if the compressor fails and gets stuck in a loop `[SYSTEM ERROR: Empty LLM response]` 
- [x] `Auto-continue` option fixed — extended detection beyond token truncation to catch incomplete states (mid-reasoning, mid-tool_call with unclosed JSON). Turn counter resets on auto-continue. Hover tooltip added explaining the feature.
- [x] agents get stopped randomly in the middle of streaming long reasoning — fixed: max-output-token guard + LLM backend defaults raised from 2048 → 8192 across all layers (execution_engine, transformers_llm, openvino, UI, JS fallback, API server). Template fallback bug fixed. Log level raised to INFO.
- [x] Add shell_cmd calls with `cd <path> && git diff` and similar safe read-only git operations to auto approval. — DONE: Extended _is_safe_readonly_shell_command to auto-approve safe read-only git operations (diff, status, log, etc.) including 'cd && git' patterns, while blocking chained commands and dangerous git subcommands/arguments. Handles -C/--git-dir flags. Note: does not handle git aliases.
- [ ] inner loop detector is almost unusable how many false positives generates, `char run` is the only good mode...
- [x] approval timeout occurs even when explicitly disabled in options, when it was set on auto-ask mode — DONE: Security advisor used hard-coded 180s timeout constant instead of reading from operation_manager settings. Fixed `security_handler.run_check()` to dynamically read `enable_timeout` and `approval_timeout_seconds` from operation manager. Timeout message now shows actual configured value. Added None guards for safety.
- [ ] I dont want truncation of the user messages in the que (UI user que display)

# Errors to investigate:

# Auto continue error: System went into a sending same message stack loop when the agent had this in reasoning block
```
The depth counting is wrong - it's just counting all 'with' statements, not actual nesting. Let me check the actual structure properly:

import re
Check actual nesting by looking at indentation
with open(r"N:\work\WD\AgentCascade\api_server.py", 'r', encoding='utf-8') as f:
lines = f.readlines()

print("Checking for actual nested locking...")
stack = [] # track (line_num, indent_level) of with session_lock
nested_found = []

for i, line in enumerate(lines, 1):
stripped = line.lstrip()
indent = len(line) - len(stripped)

if 'with session_lock:' in stripped:
    if stack and stack[-1][1] <= indent:
        # This is a sibling or nested at same level - check indentation
        pass
    stack.append((i, indent))

# When we see dedent, pop from stack
elif stack and indent < stack[-1][1]:
    # Pop all that are greater than current indent
    while stack and indent < stack[-1][1]:
        stack.pop()
print(f"Stack ended with {len(stack)} items - not useful")

Better approach: just look for consecutive 'with' blocks inside each other
for i, line in enumerate(lines, 1):
if 'with session_lock:' in line:
# Look at next lines to see if another 'with' is inside
for j in range(i+1, min(i+5, len(lines))):
next_line = lines[j-1]
next_stripped = next_line.lstrip()
next_indent = len(next_line) - len(next_stripped)
current_indent = len(line) - len(line.lstrip())

        if next_indent > current_indent and 'with' in next_stripped:
            nested_found.append((i, j, line.strip(), next_line.strip()))
        # If we hit a dedent, stop looking
        if next_indent <= current_indent and next_stripped and not next_stripped.startswith('#'):
            break
print(f"Found {len(nested_found)} potential nesting cases")
for outer, inner, outer_txt, inner_txt in nested_found:
print(f"Line {outer}: {outer_txt}")
print(f" Line {inner}: {inner_txt}")
 ```

# Auto continue repeat limit is not respected / not detecting empty response? - terminate agent fails to properly shut this down, had to restart server
2026-07-18 23:58:05,415 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 26901, Available tokens: 124320
2026-07-18 23:58:11,138 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 27757, Available tokens: 124320
2026-07-18 23:58:25,472 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 28468, Available tokens: 124320
2026-07-18 23:58:33,999 - execution_engine.py - 2947 - INFO - Detected incomplete state (reasoning-only) for reviewer_fixes. Auto-continuing.
2026-07-18 23:58:33,999 - execution_engine.py - 1208 - DEBUG - [AUTO-CONTINUE] Turn counter reset for reviewer_fixes: 250 turns remaining (consecutive resets: 1).
2026-07-18 23:58:34,008 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 28475, Available tokens: 124320
2026-07-18 23:58:39,855 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 28475, Available tokens: 124320
2026-07-18 23:58:45,204 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 28475, Available tokens: 124320
2026-07-18 23:58:51,063 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 28475, Available tokens: 124320
2026-07-18 23:58:57,219 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 28475, Available tokens: 124320
2026-07-18 23:59:00,821 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 28475, Available tokens: 124320
2026-07-18 23:59:05,663 - base.py - 994 - INFO - Agent [Reviewer] - ALL tokens: 28475, Available tokens: 124320