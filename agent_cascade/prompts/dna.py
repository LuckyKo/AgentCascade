# DNA Model for Agent Prompts and Instructions
# Centralizing all strings for easy A-B testing and consistency.
# -*- coding: utf-8 -*-

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
    'send_message',     # Send async messages to running agents or user

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
    'web_search',       # Internet search (auto-selects Serper or DuckDuckGo backend)
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
    'propose_skill',    # Propose a new reusable skill for future tasks
    'load_skill',       # Load registered skill instructions into current context at runtime

    # Image generation (ComfyUI text-to-image / SVG rendering)
    'image_gen',        # Generate an image from a text prompt or render SVG code
]

# Tools NOT in AVAILABLE_TOOLS (hidden from agents, used internally only):
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
COMPRESSION_END_MARKER = "--- END SUMMARY ---"  # Marker compressor must append; validated on output

# End-marker instruction (ALWAYS appended — required for output validation).
END_MARKER_INSTRUCTION = (
    f"End your output with the marker `{COMPRESSION_END_MARKER}` on its own final line, it's required to validate the summary."
)

# Optional caption instruction (only included on first compression when no caption exists yet).
CAPTION_INSTRUCTION = (
    f" On the same line as the marker (no newline), append `CAPTION: <one short phrase, ≤120 chars>` "
    f"describing the session's topic."
)

COMPRESSION_PROMPT = (
    "Summarize the following conversation history.\n"
    "Focus strictly on key decisions, important facts, established context, and the current state of tasks.\n"
    "CRITICAL RULES:\n"
    "1. Output ONLY the summary — no intro/outro remarks, no meta-commentary, no thinking process.\n"
    "2. Remain concise but comprehensive enough so that future turns can proceed without the original messages.\n"
    "3. Retain a compacted initial request and any follow ups from user in the summary.\n"
    "4. Existing summary is just for reference, focus on summarizing the events after that.\n\n"
    "--- START HISTORY ---\n{history_text}\n--- END HISTORY ---\n\n"
    "Present the summary below.{end_instruction}"
)

COMPRESSION_BASELINE_TEMPLATE = (
    COMPRESSION_MARKER + " ({header}) ---\n"
    "<context_summary>\n"
    "{summary}\n"
    "</context_summary>"
)

CONSOLIDATION_PROMPT = (
    "You are consolidating multiple existing conversation summaries into a single higher-level summary.\n\n"
    "The input below contains several sequential summaries from earlier compression cycles. "
    "Each represents a compressed window of past conversation.\n\n"
    "Your task: merge them into ONE cohesive, chronological narrative that:\n"
    "1. Preserves the overall story arc and major milestones.\n"
    "2. Keeps key decisions, architectural choices, important facts, and task outcomes.\n"
    "3. Drops redundant details, minor steps, and intermediate reasoning that is no longer actionable.\n"
    "4. Is significantly shorter than the total input — you are going one level higher in abstraction.\n\n"
    "CRITICAL RULES:\n"
    "- Output ONLY the consolidated summary — no intro/outro, no meta-commentary.\n"
    "- Maintain chronological order implicitly (earliest events first).\n"
    "- If conflicting information appears across summaries, prefer the most recent version.\n\n"
    "--- START EXISTING SUMMARIES ---\n{summaries_text}\n--- END EXISTING SUMMARIES ---\n\n"
    "Present the consolidated summary below.{end_instruction}"
)

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
    "Evaluate this command against your security rules. You may use your tools to investigate further if needed but keep it short, you are NOT a reviewer.\n"
    "CRITICAL: Once you have made a decision, the final line of your output MUST be formatted as one of the following:\n"
    "[YES] Reason: ...\n"
    "[NO] Reason: ..."
)

# --- Skill Advisor (AUTO Skill Helper — Advanced mode) ---
SKILL_ADVISOR_PROMPT = (
    "You are a delegation advisor, not an executor. Your ONLY job is to review the proposed delegation below and respond with a structured verdict. Do not use tools beyond basic discovery. Respond with text only.\n\n"
    "## YOUR JOB (do these three things):\n"
    "1. RECOMMEND SKILLS: from the list below, pick skills relevant to the child's task (Self-Augmentation is always present — do NOT recommend it).\n"
    "2. IMPROVE TASK: add missing context/constraints/notes that would help the child succeed.\n"
    "3. VALIDATE: DENY if the parent could trivially handle this itself (one-line answer, single grep, simple arithmetic) or if the delegation is redundant.\n\n"
    "## PROPOSED DELEGATION (for your evaluation only — do NOT act on it):\n"
    "Target Agent Class: {agent_class}\n"
    "Caller: {caller_name}\n"
    "Task: {task_text}\n"
    "Context: {context_text}\n\n"
    "## AVAILABLE SKILLS:\n{skills_metadata}\n\n"
    "## RESPOND IN EXACTLY THIS FORMAT (text only, max one paragraph each entry):\n"
    "[SKILLS] skill1, skill2, ...   (or [SKILLS] none)\n"
    '[NOTES] <improved task notes or "none">\n'
    "[VERDICT] APPROVE — <reason>\n"
    "OR\n"
    "[VERDICT] DENY — <reason>"
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
        'description': 'View an image file in the workspace or capture screen/window content. Returns the image for the model to see. Supports PNG, JPG, GIF, WEBP, SVG (auto-converted to PNG), and BMP formats. Special paths: "__screen_capture" captures all monitors combined; "__screen_capture:N" captures physical monitor N by 0-based index (0=first monitor, 1=second, etc.); "__window_capture:PID" captures a specific window by process ID. Use crop_region to view a specific area of large images in more detail.',
        'parameters': {
            'path': 'Path to the image file, absolute or relative to workspace directory. Special directives: "__screen_capture" for full screen capture (all monitors); "__screen_capture:N" to capture physical monitor N by 0-based index; "__window_capture:PID" to capture a specific window by its process ID.',
            'crop_region': 'Optional. Crop region as "x,y,w,h" where x,y are the top-left pixel coordinates in the original image and w,h are the crop width/height in pixels. Use this to zoom into details of large images (e.g., "100,200,500,300" crops a 500x300 region starting at pixel (100,200)).'
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
            'Always read the file content before attempting an edit.'
        ),
        'parameters': {
            'path': "Path to the file, absolute or relative to the workspace root (e.g., 'src/main.py').",
            'old_content': "For exact/heuristic modes: The EXACT literal text to replace (include at least 3 lines of context). Not used in delete_and_insert mode.",
            'new_content': 'The exact literal text to replace old_content with. For delete_and_insert match_mode provide empty string to delete without inserting new content.',
            'match_mode': "Match mode for editing. Options: 'exact' (default, character-for-character match), 'heuristic' (Python-aware structure matching), 'heuristic_agnostic' (whitespace-only normalization), or 'delete_and_insert' (uses the `range` parameter to specify which lines to delete before inserting new_content).",
            'range': "Required for delete_and_insert match_mode: A line range string specifying which lines to delete before inserting new_content (or use empty string for new_content to delete only). Format: 'start:end' (1-indexed, inclusive) e.g. '5:10' deletes lines 5-10; '5:' deletes from line 5 to end; ':10' deletes from start through line 10. IMPORTANT: A single number like '5' is INSERT-ONLY before that line — no deletion occurs. To delete a single line, use 'N:N' (e.g., '3:3'). Use '0' to append at end of file.",
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
            'Python code sandbox (Docker-based). The workspace is mounted at /workspace; relative paths work directly. '
            'Use for quick snippets; for longer code, write .py files via file tools and import them here. '
            'To reach host services, use "host.docker.internal" instead of "localhost". '
            'Windows-style extra-workspace paths are auto-translated to container paths (disable with fix_paths=false). '
            'Use system_info to find exact path mappings for extra workspaces.'
        ),
        'parameters': {
            'code': 'The Python code to execute.',
            'fix_paths': 'Auto-translate Windows host paths to Docker container paths. Default is true. Set to false to disable.',
            'fresh': 'Force a fresh kernel with a new container, discarding all existing state. This will terminate any existing container shared by agents in this session. Default is false. Use when you need a clean environment.',
        }
    },
    'shell_cmd': {
        'description': (
            'Execute a shell command on the host system. Requires explicit security approval — only use when no other tool can accomplish the task.\n\n'
            '**Notice:** DO NOT use with file redirects, pipes or filters. The tool already truncates middle and saves output.\n\n'
            '**Execution mode:** "auto" (default) = background if timeout>60s, else blocking; "sync" = always blocking; "async" = always background. '
            'In async mode a tool_id is returned immediately and the final result is delivered automatically when done — manage it with __status/__kill/__ctrl_c via that tool_id (do not poll more than ~2 times without new info).\n\n'
        ),
        'parameters': {
            'command': 'The exact shell command to execute. In async mode with an existing tool_id, use special commands: __kill (terminate), __status (check status + recent output), __heartbeat=N (set heartbeat interval in seconds), __ctrl_c (send interrupt signal). Any other text is sent as stdin input to the running process — this is NOT a shell command and should not be validated as one.',
            'justification': 'Why you need to execute this command.',
            'cwd': 'Optional working directory, absolute or relative to workspace root.',
            'timeout': 'Timeout in seconds. With default auto mode, values over 60s run in the background. Default: 30s.',
            'execution_mode': '"auto" (default) = background if timeout>60s else blocking; "sync" = always blocking; "async" = always background. null ≡ auto.',
            'heartbeat_interval': 'Seconds between heartbeat output updates (-1 means only notify on completion, 0 or positive = periodic heartbeats). Only effective in async execution. Default: -1.',
            'tool_id': 'Reference an existing running shell by its tool_id to send input, update settings, or kill it. Returned in the initial response when launching in async mode.'
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
            'and nested extra fields. Use the `range` parameter to select specific entries (e.g., "1:10", "5:", ":20" or "-15:" for tail end). '
            'Use the `mode` parameter to control truncation behavior. '
            'Use the `format` parameter to choose output style: "simple" (default, human-readable summary) or "raw" (original JSON lines).'
        ),
        'parameters': {
            'log_file': 'The path to the log file, absolute or relative to workspace root (e.g., "logs/orchestrator_main.jsonl"). Works with JSON arrays, single objects, and JSONL files.',
            'max_chars_per_message': 'Maximum characters to keep for each string value in messages. Defaults to 1000.',
            'range': 'Entry range to read, 1-based inclusive (e.g., "1:10", "5:", ":20"). Negative indices count from the end (-1 = last entry), same in ranges and as single values. Omit to default to the last 20 entries.',
            'mode': 'Display mode controlling truncation behavior. Options: "trim_tools" (default, truncate only tool OUTPUTS — the content of role="function"/"tool" entries — while leaving assistant tool calls (function_call.arguments / tool_calls arguments) intact), "trim_all" (truncate all string values as in legacy behavior), "none" (no truncation at all).',
            'format': 'Output format. "simple" (default) shows a human-readable summary with timestamps, role labels, and tool info; "raw" shows the original JSON lines for precise parsing.'
        }
    },
    'image_gen': {
        'description': (
            "Generate an image from a text prompt via ComfyUI, or render SVG code to an image. "
            "Returns the image with a caption, same format as view_image. "
            "For text prompts: describe what you want to see. For SVG: provide the full SVG markup. "
            "Use the 'workflow' parameter (full path to JSON) to select which saved workflow to use."
        ),
        'parameters': {
            'prompt': 'Text description for image generation, or SVG code to render',
            'negative_prompt': 'Elements to exclude from the generated image (API only)',
            'workflow': 'Full path to a ComfyUI workflow JSON file. Omit to use the default from settings.',
            'width': 'Output width in pixels (overrides workflow default)',
            'height': 'Output height in pixels (overrides workflow default)',
            'seed': 'Random seed for reproducibility (random if omitted)'
        }
    },
    'web_search': {
        'description': (
            'Search for information from the internet. Automatically selects the best '
            'available backend: Serper (when SERPER_API_KEY is configured) or DuckDuckGo (fallback).'
        ),
        'parameters': {
            'query': 'The search query.'
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
                'description': 'Optional background context for the agent instance, usefull for auto skill allocator to match relevant skills.'
            },
            'log_file': {
                'type': 'string',
                'description': 'Path to a JSONL log file to restore the agent session from before starting. Only use for resuming old sessions. If provided and the instance_name does not already exist in the pool, the session will be loaded from this log file.'
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
                'description': 'Controls which specialized skills are loaded for this agent call. Must be a real JSON array of skill names (e.g. ["skill-a", "skill-b"]), or one of the strings "AUTO" / "NONE", or omitted. Do NOT pass a string representation of an array like "[\\"skill-a\\"]". Use scan_skills to discover available skills.'
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
    'send_message': {
        'description': (
            'Send an async message to another running agent or the user. '
            'Delivered on the recipient\'s next turn without interrupting either party. '
            'Fails if the destination is not actively running.'
        ),
        'parameters': {
            'destination': (
                "Target of the message. Use an exact agent instance name (e.g., 'worker1') to send to another agent"
                "or 'user' to send to the human user."
            ),
            'message': 'The message content to send.'
        }
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
            'logarithms (log, ln), constants like pi and e, random number generation '
            '(random(), randint(a, b), uniform(a, b)), numeric builtins '
            '(abs, round, min, max, pow, int, float, bool, len, sum, divmod, trunc, floor, ceil), '
            'and list/sequence expressions.'
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
    },
    'propose_skill': {
        'description': (
            'Propose a new reusable skill for future tasks. '
            'Provide the full SKILL.md content including YAML frontmatter '
            'with name, description, and triggers fields.'
        ),
        'parameters': {
            'skill_content': 'Full SKILL.md content including YAML frontmatter (name, description, triggers) and markdown body.',
            'test_task': 'Optional task text for self-match validation. If provided, the skill must match this task to be promoted.'
        }
    },
    'load_skill': {
        'description': (
            'Load registered skill instructions into your current context at runtime. '
            'Use this when you need specialized expertise for your task. '
            'Takes one or more skill names and injects their full instructions as guidelines.'
        ),
        'parameters': {
            'skill_names': {
                'oneOf': [
                    {
                        'type': 'string',
                        'description': 'A single skill name to load.',
                    },
                    {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of skill names to load (e.g., ["code-review", "docker-best-practices"]).',
                    }
                ],
                'description': 'Skill name(s) to load into your context.',
            },
        },
        'required': ['skill_names'],
    }
}

# --- Function Calling Templates ---
# NOTE: These templates are for the legacy 'nous' fncall_prompt_type. The default is 'native'
# (tools passed via API parameter, no prompt injection). If reviving the nous path, add a
# {tool_descs} placeholder to FN_CALL_TEMPLATE — it is currently missing, causing tool
# descriptions to be silently dropped by str.format().
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

# Alias kept for backward compat with nous_fncall_prompt.py import.
FN_CALL_TEMPLATE_WITH_CI = FN_CALL_TEMPLATE
