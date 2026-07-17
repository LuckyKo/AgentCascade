# DNA Model for Agent Prompts and Instructions
# Centralizing all strings for easy A-B testing and consistency.

from typing import Dict, List, Set

# ── Available Tools Registry ────────────────────────────────────────────────
# Master list of ALL tools that agents can use. Toggle True/False to enable/disable
# a tool system-wide. Per-agent enable/disable is handled via UI disabled_tools settings.
#
# Order: sub-agent management → file ops → search → code/shell → context mgmt → misc
AVAILABLE_TOOLS: List[str] = [
    # Sub-agent management
    'call_agent',       # Delegate tasks to specialized agent instances
    'dismiss_agent',    # End sub-agent sessions and clear context
    'list_agents',      # List available agent classes and active instances

    # Read-only file ops
    'read_file',        # Read file contents
    'view_image',       # View image files
    'list_dir',         # List directory contents
    'grep',             # Search for text patterns in files

    # Mutating file ops
    'write_file',       # Create or overwrite files
    'edit_file',        # Surgical text replacement in existing files
    're_indent',        # Re-indent code blocks
    'delete_file',      # Delete files (with backup)
    'copy_file',        # Copy files or directories

    # Code & shell execution
    'code_interpreter', # Python sandbox (Docker-based)
    'shell_cmd',        # Execute host shell commands

    # Web & search
    'ddg_search',       # DuckDuckGo web search
    'web_extractor',    # Extract webpage content

    # Context management
    'compress_context', # Summarize conversation history to free context space
    'forget_last',      # Truncate recent tool call outputs

    # Information & utilities
    'system_info',      # System info, workspace paths, session stats
    'read_logs',        # Read JSON/JSONL log files (arrays, objects, or mixed/malformed content)
    'code_map',         # Quick file structure overview
    'calculate',        # Evaluate mathematical expressions
    'syntax_check',     # Check file syntax without execution
    'scan_skills',      # Scan registered skills and return matching skills with relevance scores
]

# Tools NOT in AVAILABLE_TOOLS (hidden from agents, used internally only):
#   image_gen      — Image generation service
#   storage        — Internal storage tool
#   retrieval      — RAG retrieval engine
#   doc_parser     — Document parser
#   simple_doc_parser  — Simple document parser
#   extract_doc_vocabulary — Vocabulary extraction
#   move_file      — Move file/directory (copy+delete achieves same result)


# --- XML Transport Settings ---
# Fields that should be placed in XML tags instead of inside JSON strings.
XML_CONTENT_FIELDS: Set[str] = {
    'content', 
    'old_content', 
    'new_content', 
    'old_string', 
    'new_string', 
    'full_content', 
    'code', 
    'command',
    'justification',
    'summary'
}

# Minimum length for a field value to be emitted as XML instead of JSON.
XML_MIN_LENGTH: int = 40

# --- Agent Persona & System Messages ---
DEFAULT_SYSTEM_MESSAGE: str = 'You are a helpful assistant.'

# --- Memory Compression ---
COMPRESSION_MARKER = "--- CONTEXT COMPRESSED"

COMPRESSION_PROMPT = (
    "Summarize the following conversation history.\n"
    "Focus strictly on key decisions, important facts, established context, and the current state of tasks.\n"
    "CRITICAL RULES:\n"
    "1. Output ONLY the summary. Do not include introductory or concluding remarks (e.g. 'Here is a summary').\n"
    "2. Do not include meta-commentary or thinking process.\n"
    "3. Remain concise but comprehensive enough so that future turns can proceed without the original messages.\n"
    "4. Retain initial request and progress of the task in the summary.\n\n"
    "--- START HISTORY ---\n{history_text}\n--- END HISTORY ---\n\n"
    "Summary:"
)

COMPRESSION_BASELINE_TEMPLATE = (
    COMPRESSION_MARKER + " ({header}) ---\n"
    "<context_summary>\n"
    "{summary}\n"
    "</context_summary>"
)

COMPRESSION_NOTICE_TEMPLATE = ""  # Unused — header is now minimal

# --- Security Advisor ---
SECURITY_ADVISOR_PROMPT = (
    "A sub-agent has requested to execute a tool. Please verify if this operation is safe.\n\n"
    "Tool: {tool_name}\n"
    "Description: {description}\n"
    "Arguments: {arguments}\n\n"
    "System limitations:\n"
    "- Operating System: {os_info}\n"
    "- Working directory and any file paths must be within the allowed workspaces.\n"
    "Allowed folders:\n{workspace_info}\n\n"
    "Evaluate this command against your security rules. You may use your tools to investigate further if needed.\n"
    "CRITICAL: Once you have made a decision, the final line of your output MUST be formatted as follows:\n"
    "[YES] Reason: ...\n"
    "[NO] Reason: ..."
)

# --- Knowledge Base Templates ---
KNOWLEDGE_TEMPLATE_ZH = """# 知识库

{knowledge}"""

KNOWLEDGE_TEMPLATE_EN = """# Knowledge Base

{knowledge}"""

KNOWLEDGE_TEMPLATE = {'zh': KNOWLEDGE_TEMPLATE_ZH, 'en': KNOWLEDGE_TEMPLATE_EN}

KNOWLEDGE_SNIPPET_ZH = """## 来自 {source} 的内容：

```
{content}
```"""

KNOWLEDGE_SNIPPET_EN = """## The content from {source}:

```
{content}
```"""

KNOWLEDGE_SNIPPET = {'zh': KNOWLEDGE_SNIPPET_ZH, 'en': KNOWLEDGE_SNIPPET_EN}

# --- Tool Descriptions & Metadata ---
TOOL_METADATA = {
    'read_file': {
        'description': (
            'Reads and returns the content of a specified file. If the file is large, '
            'the content will be truncated. The tool\'s response will clearly indicate '
            'if truncation has occurred and will provide details on how to read more '
            'of the file using the \'start_line\' and \'limit\' parameters. Handles text files '
            'natively with streaming line-by-line reading. For binary files, displays a '
            'hex dump of the first N bytes with ASCII representation.'
        ),
        'parameters': {
            'path': "Path to the file, absolute or relative to the workspace root (e.g., 'src/main.py', 'data/input.csv').",
            'start_line': "Optional: 1-based line number to start reading from. Supports negative values (-1 = last line, -3 = third-to-last). Default is 1.",
            'limit': "Optional: For text files, maximum number of lines to read. Default is 1000 (configurable via QWEN_AGENT_READ_FILE_MAX_LINES env var / settings.py). Set to -1 for unlimited (uses higher internal line cap). Use with 'start_line' to paginate through large files."
        }
    },
    'view_image': {
        'description': 'View an image file in the workspace. Returns the image for the model to see. Supports PNG, JPG, GIF, WEBP, SVG (auto-converted to PNG), and BMP formats.',
        'parameters': {
            'path': 'Path to the image file, absolute or relative to workspace directory'
        }
    },
    'write_file': {
        'description': (
            'Creates a new file or overwrites an existing one with full content. '
            'If the file already exists, a backup is automatically created. '
            'This is auto-approved for new files. Overwriting an existing file '
            'requires user approval if you do not own it.'
        ),
        'parameters': {
            'path': "Path to the file, absolute or relative to the workspace root (e.g., 'src/main.py').",
            'content': 'The full content to write to the file.',
            'justification': 'Why you need to create or overwrite this file'
        }
    },
    'edit_file': {
        'description': (
            'Performs a surgical text replacement within an existing file. '
            'Always use this instead of write_file for modifying parts of a file, '
            'as it is safer and preserves the rest of the content. '
            'Requires user approval if you do not own the file. '
            'Always read the file content before attempting an edit.\n'
            'Include at least 3 lines of context matching whitespace and indentation precisely when content matching.'
        ),
        'parameters': {
            'path': "Path to the file, absolute or relative to the workspace root (e.g., 'src/main.py').",
            'old_content': "For exact/heuristic modes: The EXACT literal text to replace (include at least 3 lines of context). For delete_and_insert match_mode: A line range 'start:end' (1-indexed) specifying which lines to delete before inserting new_content, use `start` index only for insertions without delete, `0` for append at end of file.",
            'new_content': 'The exact literal text to replace old_content with. For delete_and_insert match_mode provide empty string to delete without inserting new content.',
            'match_mode': "Match mode for editing. Options: 'exact' (default, character-for-character match), 'heuristic' (Python-aware structure matching), 'heuristic_agnostic' (whitespace-only normalization), or 'delete_and_insert' (old_content is a line range start:end to delete before inserting new_content).",
            'justification': 'Why you need to edit this file'
        }
    },
    're_indent': {
        'description': (
            'Re-indents a specific block of code in a file. '
            'It allows shifting, flattening, converting indentation between tabs and spaces, or adjusting base indentation.'
        ),
        'parameters': {
            'path': "Path to the file, absolute or relative to the workspace root (e.g., 'src/main.py').",
            'lines': "Line range to re-indent, 1-based inclusive (e.g., '1:10', '5:', ':20').",
            'indent': "Target indent unit size: number of spaces per indent level (for 'min'/'flat' modes), or tab width in columns (for 'convert' mode). For 'shift' mode: number of indent characters to add/remove per line — positive adds, negative removes; result clamped to no leading whitespace minimum.",
            'indent_type': "Indentation character type: 'space' or 'tab'.",
            'mode': "Optional: Re-alignment mode. Can be 'min' (default, trims to minimum indentation level then applies target indent while preserving relative hierarchy), 'shift' (adds or removes indent units from each line; positive adds, negative removes), 'flat' (flattens entire block to target indent), or 'convert' (converts between tabs and spaces using visual column alignment where 1 tab = indent spaces)."
        }
    },
    'list_dir': {
        'description': (
            'Lists files and subdirectories within a specified directory path. '
            'Supports recursive traversal, glob-based filtering, sorting by name/size/date/type, '
            'and optional summary statistics.'
        ),
        'parameters': {
            'path': "Path to the directory, absolute or relative to the workspace root (e.g., '.', 'src', 'data/images')",
            'recursive': "When true, recurse into subdirectories. Default: false.",
            'max_depth': "Maximum recursion depth when recursive=true. -1 means unlimited, 0 or negative behaves like non-recursive. Default: -1.",
            'include': "Optional glob pattern to include only matching files (e.g., '*.py', 'test_*'). Simple globs only; '**' patterns are not supported.",
            'exclude': "Optional glob pattern to exclude matching entries (e.g., '__pycache__/*', '*.pyc').",
            'sort_by': 'Sorting order. Options: "name" (default), "size" (largest first), "date" (newest first), "type" (extension). For size and date, descending order is used.',
            'show_summary': "When true, append summary statistics (total files/dirs, total size) at the end. Default: false.",
            'max_entries': "Maximum number of entries to display before truncating output. Helps control verbosity in large directories. Default: 500."
        }
    },
    'grep': {
        'description': (
            'Search for a text pattern in files. Supports Python regex syntax.\n'
            '- Smart case by default: case-insensitive unless pattern contains uppercase letters.\n'
            '- Respects .gitignore/.rgignore when ignore_vcs is True (default).\n'
            '- Use "context" to show surrounding lines (like -C N in grep/ripgrep).\n'
            '- Matched text is prefixed with ">>>" when context is used; context lines have spaces.\n'
            '- Groups of matches are separated by "---".'
        ),
        'parameters': {
            'pattern': 'Text or regex pattern to search for (Python regex syntax)',
            'path': 'Directory to search in, absolute or relative to workspace root (default: ".")',
            'include': 'File glob pattern to include (e.g., "*.py", "*.md"). Default: "*"',
            'exclude': 'File glob pattern to exclude (e.g., "*_test.py", "docs/*"). Default: ""',
            'ignore_vcs': 'When True (default), skip .git/ and other VCS/build directories. Set False to search everything.',
            'context': 'Number of lines to show before/after each match (like -C N). Default: 0',
            'smart_case': 'When True (default), case-insensitive unless pattern contains uppercase letters. Set False for always case-insensitive.'
        }
    },
    'delete_file': {
        'description': (
            'Delete a file. Before deletion, the file is moved to a backup folder '
            '(similar to edit_file backups), so it can be restored if needed. '
            'Requires user approval before deletion for any files not owned by the current agent. '
            'Deleting files you created in this session is auto-approved.'
        ),
        'parameters': {
            'path': "Path to the file, absolute or relative to the workspace root (e.g., 'temp/scratch.py')"
        }
    },
    'copy_file': {
        'description': (
            'Copy a file or directory to a new location. If the destination already exists, '
            'a timestamped backup is created before overwriting. This is auto-approved if the destination is new. '
            'You become the owner of the copied file, allowing you to edit it freely without user approval.'
        ),
        'parameters': {
            'source': "Path to the source file/directory, absolute or relative to workspace root (e.g., 'src/old.py')",
            'destination': "Path to the destination, absolute or relative to workspace root (e.g., 'src/new.py')"
        }
    },
    
    'code_interpreter': {
        'description': (
            'Python code sandbox (Docker-based). The workspace directory is mounted into the container. '
            'Use this to test small snippets of code, for anything larger than a few lines write and edit py files that you can import here. DONT waste tokens writing the same code over and over in the prompt. Use the file tools to manage your code files efficiently. '
            'PATH MAPPING: Any path used with file tools (read_file, write_file, etc.) outside this tool '
            'maps to "/workspace/<path>" inside the container. For example, "src/main.py" becomes '
            '"/workspace/src/main.py". The container working directory is /workspace, so relative paths '
            '"src/main.py" also work directly in your code. Use system_info tool to find exact folder mapping for extra paths. '
            'To access services on the host machine (like local APIs), use "host.docker.internal" instead of "localhost". '
            'Absolute Windows style paths mapped to extra workspaces (e.g., N:\\work\\...) are auto-translated to container paths by default. '
            'Set fix_paths=false to disable this behavior.'
        ),
        'parameters': {
            'code': 'The Python code to execute.',
            'fix_paths': 'Auto-translate Windows host paths to Docker container paths. Default is true. Set to false to disable.',
            'fresh': 'Force a fresh kernel with a new container, discarding all existing state. This will terminate any existing container shared by agents in this session. Default is false. Use when you need a clean environment.',
        }
    },
    'shell_cmd': {
        'description': (
            'Execute a shell command on the host system. This ALWAYS requires explicit user approval so use it as a last resort tool only! '
            'Commands run with the workspace directory as the working directory.\n\n'
            '**Async Mode**: Set async_mode=true to run commands in the background — returns immediately with a tool_id and PID. '
            'The command runs while you continue working, sending periodic heartbeat updates (if heartbeat_interval > 0) and a final result message when done. '
            'Use the tool_id parameter to manage running shells: send input, check status (__status), kill (__kill), update heartbeat (__heartbeat=N seconds), or send Ctrl+C (__ctrl_c). '
            'Max 5 concurrent async shells per agent.'
        ),
        'parameters': {
            'command': 'The exact shell command to execute. In async mode with an existing tool_id, use special commands: __kill (terminate), __status (check status + recent output), __heartbeat=N (set heartbeat interval in seconds), __ctrl_c (send interrupt signal). Any other text is sent as stdin input.',
            'justification': 'Why you need to execute this command.',
            'cwd': 'Optional working directory, absolute or relative to workspace root.',
            'timeout': 'Optional timeout in seconds (default: 30 for sync mode, 3600 for async mode). Use a higher value for long-running commands.',
            'async_mode': 'Run the command in background and return immediately with tool_id + PID. The agent continues working while the command runs. Heartbeat updates are injected as user messages at intervals. Default: false (blocking/synchronous execution).',
            'heartbeat_interval': 'Seconds between heartbeat output updates (-1 means only notify on completion, 0 or positive = periodic heartbeats). Only effective when async_mode=true. Default: -1.',
            'tool_id': 'Reference an existing running shell by its tool_id to send input, update settings, or kill it. Returned in the initial response when launching with async_mode=true.'
        }
    },
    'system_info': {
        'description': (
            'Retrieves the current system information. '
            'This includes the operating system, current time and date, '
            'current work directories with their Docker container mount paths (e.g., host N:\\work\\WD\\AgentWorkspace maps to /workspace inside containers), '
            'Python version, and basic session stats. '
            'Use this when a path works on the host but fails inside a Docker container — the output shows exactly where each folder is mounted. '
        ),
        'parameters': {}
    },
    'read_logs': {
        'description': (
            'Read a JSON/JSONL log file (agent logs, JSON arrays, single objects, or files with mixed/malformed lines). '
            'Large message contents are truncated in the middle to prevent context overflow while '
            'retaining the beginning and end of each message. Handles other types of text files as well with the same middle truncation applied for each line, '
            'and nested extra fields. Use the `range` parameter to select specific entries (e.g., "1:10", "5:", ":20").'
        ),
        'parameters': {
            'log_file': 'The path to the log file, absolute or relative to workspace root (e.g., "logs/orchestrator_main.jsonl"). Works with JSON arrays, single objects, and JSONL files.',
            'max_chars_per_message': 'Maximum characters to keep for each string value in messages. Defaults to 1000.',
            'range': 'Entry range to read, 1-based inclusive (e.g., "1:10", "5:", ":20"). Supports negative indices (e.g., "-1" for the last entry; in ranges like "5:-1", -1 refers to the second-to-last). Omit to default to the last 20 entries.'
        }
    },
    'image_gen': {
        'description': (
            'An image generation service that takes text descriptions as input and returns a URL of the image.'
        ),
        'parameters': {
            'prompt': (
                'Detailed description of the desired content of the generated image. '
                'Please keep the specific requirements such as text from the original request fully intact. '
                'Omission is prohibited.'
            )
        }
    },
    'web_search': {
        'description': 'Search for information from the internet.',
        'parameters': {
            'query': 'The search query to use.'
        }
    },
    'amap_weather': {
        'description': '获取对应城市的天气数据 (Get weather data for a specific city).',
        'parameters': {
            'location': '城市/区具体名称，如`北京市海淀区`请描述为`海淀区` (Specific city/district name).'
        }
    },
    'doc_parser': {
        'description': 'Extract and chunk the content of a document, returning the chunked content.',
        'parameters': {
            'url': 'The path to the file to be parsed, which can be a local path or a downloadable http(s) link.'
        }
    },
    'web_extractor': {
        'description': 'Get content of one webpage.',
        'parameters': {
            'url': 'The webpage url.'
        }
    },
    'retrieval': {
        'description': (
            'Retrieve relevant content from a given list of files. '
            'Supports various file types (PDF, Word, PPT, Text, etc.).'
        ),
        'parameters': {
            'query': 'The query keywords for matching relevant document segments. Use comma-separated keywords for better matching.',
            'files': 'A list of file paths (local) or URLs (http/https) to be parsed and searched.'
        }
    },
    'call_agent': {
        'description': (
            'Delegate a task to a specialized agent instance. '
            'If the instance_name already exists, the session continues with the existing context. '
            'Otherwise, a new session is started using the specified agent_class.\n\n'
            'Example usage:\n'
            '{"name": "call_agent", "arguments": {"agent_class": "coder", "instance_name": "worker1", "task": "Write a script"}}'
        ),
        'parameters': {
            'agent_class': {
                'type': 'string',
                'description': 'The class of agent to call (e.g. "coder", "researcher"). Only required when starting a NEW instance.'
            },
            'instance_name': {
                'type': 'string',
                'description': 'A unique name for this agent instance. If this name exists, the existing session is continued regardless of agent_class.'
            },
            'task': {
                'type': 'string',
                'description': 'The task or question to delegate'
            },
            'context': {
                'type': 'string',
                'description': 'Optional background context for the agent instance'
            },
            'log_file': {
                'type': 'string',
                'description': 'Path to a JSONL log file to restore the agent session from before starting. Useful for resuming old sessions. If provided and the instance_name does not already exist in the pool, the session will be loaded from this log file.'
            },
            'max_turns': {
                'type': 'integer',
                'minimum': 1,
                'description': 'Optional turn limit for sub-agent execution. If omitted, defaults to caller\'s limit. Useful for short tasks requiring strict budget control. The sub-agent will be informed of its turn budget via context.'
            },
            'load_skill': {
                'oneOf': [
                    {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of skill names to load (e.g., ["httpx-connection-pooling", "code-review"]). Full instructions will be injected into the child agent.'
                    },
                    {
                        'type': 'string',
                        'enum': ['AUTO', 'NONE'],
                        'description': '"AUTO" = auto-match relevant skills from task context; "NONE" = no skill loading (saves tokens).'
                    },
                    {
                        'type': 'null',
                        'description': 'Omit skill loading (same as "NONE").'
                    }
                ],
                'description': 'Controls which specialized skills are loaded for this agent call. Use scan_skills to discover available skills.'
            },
        },
        'required': ['agent_class', 'instance_name', 'task'],
    },
    'dismiss_agent': {
        'description': (
            "End a sub-agent instance's current task and clear its conversation context. "
            "Use when you're done with a sub-agent and don't need its context anymore."
        ),
        'parameters': {
            'instance_name': {
                'type': 'string',
                'description': 'Name of the sub-agent instance to dismiss (optional if all_idle is true)'
            },
            'all_idle': {
                'type': 'boolean',
                'description': 'If true, dismiss all sub-agents that are currently IDLE. Default is false.'
            },
        },
        'required': [],  # lenient: both params optional (all_idle=true works alone)
    },
    'list_agents': {
        'description': (
            'List all available agent classes with their descriptions, '
            'plus any active instances currently running or previously used. Use this to find out how to call a specific agent or instance'
        ),
        'parameters': {}
    },
    'compress_context': {
        'description': (
            'Summarize the oldest part of the conversation history to free up context space. '
            'Supports two modes: "auto" (generated via specialized compression LLM) and "manual" (provided by agent via summary_text). '
            'A fraction of history is replaced by a concise summary.'
        ),
        'parameters': {
            'fraction': 'The fraction of history to summarize (e.g. 0.5 for 50%). Max 1.0.',
            'mode': "Compression mode: 'auto' (default) or 'manual'.",
            'summary_text': 'Your own summary of the conversation history portion that will be trimmed out. Required when mode=manual.',
            'force': 'Bypass validation guards (e.g., minimum message count). Used for critical threshold compression.'
        }
    },
    'calculate': {
        'description': (
            'Evaluates a mathematical expression and returns the result. '
            'Supports basic arithmetic (+, -, *, /, ^), trigonometry (sin, cos, tan), '
            'logarithms (log, ln), constants like pi and e, and basic random '
            'number generation (random(), randint(a, b), uniform(a, b)).'
        ),
        'parameters': {
            'expression': 'The mathematical expression to evaluate (e.g., "sin(pi/2) + randint(1, 10)").'
        }
    },
    'code_map': {
        'description': (
            'Quickly map a code file to see its structure (classes, functions, methods) and their line numbers. '
            'Use this for an overview of large files before performing targeted reads.'
        ),
        'parameters': {
            'path': 'Path to the file to map, absolute or relative to workspace root.',
            'force_as': 'Optional. Force parsing as a specific language (e.g., "python", "javascript", "cpp", "java").'
        }
    },
    'forget_last': {
        'description': (
            'Retroactively truncate the output of the last N tool call responses in the active conversation history. '
            'Each truncated response is shortened to ~100 characters max, with a marker indicating truncation. '
            'This frees up context space if the tool data is not useful. '
            'Affects both the in-memory pool and the log file.'
        ),
        'parameters': {
            'count': 'Number of recent tool call responses to truncate. Counts backwards from the most recent function result, skipping non-function messages. Default is 1.',
            'justification': 'Optional reason for truncation. Appended to the truncation marker for context awareness. Keep it very short (e.g. "useless data").',
        }
    },
    'syntax_check': {
        'description': (
            'Check a file for syntax errors without executing it. '
            'Auto-detects the language from the file extension and applies the '
            'appropriate syntax checker. Works with Python, JavaScript, TypeScript, '
            'JSON, YAML, TOML, XML, HTML, CSS, C, C++, C#, Java, Go, Rust, and more. '
            'Returns "Valid (<language>)" or a detailed error message.'
        ),
        'parameters': {
            'path': 'Path to the file to check, absolute or relative to the workspace root.'
        }
    },
    'scan_skills': {
        'description': (
            'Scan registered skills and return matching skills with relevance scores. '
            'Use this to discover which skills are available before calling call_agent with load_skill. '
            'Returns skill names, descriptions, and match scores for the given query.'
        ),
        'parameters': {
            'query': 'Search query or task description to match against available skills. Leave empty to list all registered skills.'
        }
    }
}

# --- Function Calling Templates ---
FN_CALL_TEMPLATE = """# Tools

You have access to a set of provided tools. You can call these tools natively to assist with the user's query.

When you need to call a tool, use your native function calling schema to emit the tool call. The system will parse the native tool call and execute the function.

**Rules for Tool Calling:**
1. **Native JSON Parameters**: All parameters MUST be passed within the tool call's JSON arguments. Do not use external XML tags for arguments.
2. **Proper Escaping**: When passing code, large text, or multiline content (e.g., to `write_file`, `edit_file`, or `code_interpreter`), ensure the text is properly escaped within the JSON string.
3. **Reasoning**: You may explain your thoughts and reasoning in the normal message content before making the tool call.
4. **Tool Results**: The results of the tool call will be provided back to you in the next message.

Do not try to output <tool_call> or <tools> XML tags manually; the system handles the tool schemas and execution natively via the API.
"""

FN_CALL_TEMPLATE_WITH_CI = FN_CALL_TEMPLATE # Now included in main template
