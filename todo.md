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
- [ ] approval timeout occurs even when explicitly disabled in options, when it was set on auto-ask mode

# Errors to investigate:

# Fail to catch malformed message - tool call in reasoning block, agent gets stuck in a loop retrying the same message over and over even if i kick off the model
```
<think>
Let me fix the code and run it properly:
<function=code_interpreter>
<parameter=code>

Verify that certain "safe" git subcommands actually have write operations
dangerous_variants = {
'stash': ['pop', 'apply', 'drop'], # These modify stash/wd
'config': ['--set', '--add'], # These modify config files
'worktree': ['add', 'move', 'remove'], # These create/remove worktrees
'notes': ['add', 'append', 'message'], # These write notes
'pack-refs': [], # This packs refs (writes to packed-refs)
'prune': ['default prunes (writes)'], # Default removes unreachable objects
'replace': [], # Creates/replaces refs
}

Print what we know about each subcommand's safety profile
print("Git subcommands in _SAFE_GIT_SUBCOMMANDS that have write operations:")
for cmd, variants in dangerous_variants.items():
status = 'DANGEROUS' if variants else 'WRITE-ONLY (pack-refs/prune/replace)'
print(f" {cmd}: {status}")

Also check: are pack-refs and prune actually write operations?
print("\nNote: git pack-refs writes to .git/packed-refs file")
print("Note: git prune removes unreachable objects from object database")
print("Note: git replace creates ref replacement entries")

False
```

LMStudio dump:
```
2026-07-18 06:28:04  [INFO]
 [agents-a1-35b-mtp] Prompt processing progress: 100.0%
2026-07-18 06:28:07 [DEBUG]
 0.53.304.984 I slot print_timing: id  0 | task 1318 | n_decoded =    251, tg =  83.55 t/s, tg_3s =  83.55 t/s
2026-07-18 06:28:10 [DEBUG]
 0.55.923.703 I slot print_timing: id  0 | task 1318 | prompt eval time =     361.80 ms /   423 tokens (    0.86 ms per token,  1169.14 tokens per second)
0.55.923.708 I slot print_timing: id  0 | task 1318 |        eval time =    5622.78 ms /   460 tokens (   12.22 ms per token,    81.81 tokens per second)
0.55.923.709 I slot print_timing: id  0 | task 1318 |       total time =    5984.58 ms /   883 tokens
0.55.923.710 I slot print_timing: id  0 | task 1318 |    graphs reused =       1724
2026-07-18 06:28:10 [DEBUG]
 0.55.924.655 I slot      release: id  0 | task 1318 | stop processing: n_tokens = 37376, truncated = 0
0.55.924.848 W common_chat_peg_parse: unparsed peg-native output: <tool_call>
<function=code_interpreter>
<parameter=code>
# Verify that certain "safe" git subcommands actually have write operations
dangerous_variants = {
    'stash': ['pop', 'apply', 'drop'],        # These modify stash/wd
    'config': ['--set', '--add'],              # These modify config files
    'worktree': ['add', 'move', 'remove'],     # These create/remove worktrees
    'notes': ['add', 'append', 'message'],     # These write notes
    'pack-refs': [],                           # This packs refs (writes to packed-refs)
    'prune': ['default prunes (writes)'],  # Default removes unreachable objects
    'replace': [],                             # Creates/replaces refs
}

# Print what we know about each subcommand's safety profile
print("Git subcommands in _SAFE_GIT_SUBCOMMANDS that have write operations:")
for cmd, variants in dangerous_variants.items():
    status = 'DANGEROUS' if variants else 'WRITE-ONLY (pack-refs/prune/replace)'
    print(f"  {cmd}: {status}")

# Also check: are pack-refs and prune actually write operations?
print("\nNote: git pack-refs writes to .git/packed-refs file")
print("Note: git prune removes unreachable objects from object database")
print("Note: git replace creates ref replacement entries")

</parameter>
<parameter=fix_paths>
False
</parameter>
</function>
</tool_call>

</think>

<tool_call>
<function=code_interpreter>
<parameter=code>
# Check what git subcommands are in the set and identify potential issues
safe_git_subcommands = {
    'diff', 'status', 'log', 'show', 'branch', 'tag', 'remote',
    'rev-parse', 'config', 'merge-base', 'describe', 'ls-files',
    'ls-tree', 'stash', 'shortlog', 'blame', 'name-rev', 'hash-object',
    'cat-file', 'for-each-ref', 'var', 'symbolic-ref',
    'version', 'rev-list', 'reflog', 'worktree',
    'count-objects', 'interpret-trailers',
    'notes', 'pack-refs', 'prune', 'replace', 'rerere',
    'verify-commit', 'verify-tag', 'verify-pack',
}

# Identify subcommands that can write data
dangerous = []
for cmd in safe_git_subcommands:
    if cmd == 'stash':
        # git stash list is safe, but git stash pop/apply are not
        dangerous.append((cmd, "can pop/apply with additional args"))
    elif cmd == 'config':
        # git config --list is safe, but --set/--add modify config
        dangerous.append((cmd, "can modify with --set/--add"))
    elif cmd in ('worktree',):
        # worktree can create/remove worktrees
        dangerous.append((cmd, "can add/move/remove worktrees"))
    elif cmd in ('notes',):
        # notes can add/append data
        dangerous.append((cmd, "can write notes"))
    elif cmd in ('pack-refs', 'prune', 'replace'):
        # These are inherently write operations
        dangerous.append((cmd, "inherently writes to git database"))

print("Subcommands that can write data:")
for cmd, reason in dangerous:
    print(f"  - {cmd}: {reason}")

</parameter>
<parameter=fix_paths>
false
</parameter>
</function>
</tool_call>
2026-07-18 06:28:10 [DEBUG]
 0.55.925.066 W srv          stop: cancel task, id_task = 1318
2026-07-18 06:28:10 [ERROR]
 [agents-a1-35b-mtp] Engine protocol predict stream returned an error: {"code":500,"message":"The model produced output that does not match the expected peg-native format","type":"server_error"}. Error Data: n/a, Additional Data: n/a
 ```