# Tools Subsystem — Deep Audit Report

**Generated:** 2026-06-24  
**Auditor:** ToolAudit (Maine)  
**Scope:** `agent_cascade/tools/`, `agent_cascade/tool_dispatcher.py`, `agent_cascade/tool_utils.py`, and related files.

---

## 1. ARCHITECTURE OVERVIEW

### 1.1 Directory Structure
```
agent_cascade/
├── tool_dispatcher.py        # Main routing engine (700 lines)
├── tool_utils.py             # Shared utilities (178 lines)
└── tools/
    ├── __init__.py           # Exports TOOL_REGISTRY + all tool classes
    ├── base.py               # BaseTool, register_tool(), TOOL_REGISTRY dict  (233 lines)
    │
    ├── custom/               # Agent-facing tools used by ALL agents
    │   ├── file_ops.py       # read_file, view_image, write_file, edit_file, list_dir, grep, delete_file, copy_file, move_file, re_indent
    │   ├── manager_ops.py    # call_agent, dismiss_agent, list_agents
    │   ├── compression_tools.py  # compress_context
    │   ├── shell_cmd.py      # shell_cmd
    │   ├── system_info.py    # system_info
    │   ├── read_logs.py      # read_logs
    │   ├── calculation.py    # calculate
    │   ├── code_map.py       # code_map
    │   └── forget_last_tool.py  # forget_last
    │
    ├── _agent_instance_proxy.py  # call_agent schema proxy (68 lines)
    ├── code_interpreter.py   # Docker-based Python sandbox (~1,125 lines)
    ├── web_extractor.py      # Web page content extraction (44 lines)
    ├── python_compiler.py    # Syntax checker (57 lines)
    │
    └── search_tools/         # RAG/search infrastructure (NOT exposed to agents)
        ├── base_search.py
        ├── keyword_search.py
        ├── vector_search.py
        ├── hybrid_search.py
        └── front_page_search.py
```

### 1.2 Tool Definition Pattern
Every tool is a class inheriting from `BaseTool` (or `BaseToolWithFileAccess`). Each defines:
- **`name`** — unique string identifier
- **`description`** — human-readable description (sourced from `TOOL_METADATA` in `prompts/dna.py`)
- **`parameters`** — JSON Schema object describing accepted arguments
- **`call(params, **kwargs)`** — abstract method implementing the tool's logic

---

## 2. TOOL REGISTRATION MECHANISM

### 2.1 The Global Registry (`base.py:25–60`)
```python
TOOL_REGISTRY = {}  # dict[str -> ToolClass]

def register_tool(name, allow_overwrite=False):
    def decorator(cls):
        cls.name = name
        TOOL_REGISTRY[name] = cls
        return cls
    return decorator
```
- **Decorator-based**: `@register_tool('tool_name')` registers a class into the global dict.
- **Name enforcement**: The class's `.name` attribute must match the decorator argument.
- **Uniqueness guard**: Raises `ValueError` if name already exists (unless `allow_overwrite=True`).

### 2.2 How Tools Flow from "Defined" → "Available to Agent"

```
Step 1: Tool Class Defined
        └── e.g., class ReadFile(BaseTool) in tools/custom/file_ops.py

Step 2: Agent Instantiated
        └── agent_factory.load_agent() creates an Agent instance
            with empty function_map = {}

Step 3: register_standard_tools(agent, pool, name) called
        (agent_factory.py:22–150)
        └── Creates tool INSTANCES and assigns them to agent.function_map['tool_name']
            Each tool gets agent_pool / agent_name injected for file ops + approvals

Step 4: Execution Engine Routes Calls
        └── ToolDispatcher.execute_tool() (tool_dispatcher.py:83–146)
            - call_agent → intercepted, handled specially
            - dismiss_agent → intercepted, handled specially
            - compress_context → delegated to CompressionHandler
            - All others → template._call_tool(tool_name, resolved_args, ...)

Step 5: Agent._call_tool() (agent.py:229–273)
        └── Looks up tool in self.function_map[tool_name]
        └── Calls tool.call(args, **kwargs)
```

### 2.3 Registration Flow Diagram
```
TOOL_REGISTRY (global dict)          agent_factory.register_standard_tools()
         │                                    │
         │  @register_tool('read_file')       │  Creates ReadFile instance
         ▼                                   ▼
   {'read_file': ReadFile, ...}    →  agent.function_map['read_file'] = ReadFile(...)
                                                      │
                                                      ▼
                                            ToolDispatcher.execute_tool()
                                                      │
                                            ┌─────────┴──────────┐
                                            │ Special routing:   │
                                            │ call_agent         │
                                            │ dismiss_agent      │
                                            │ compress_context   │
                                            └─────────┬──────────┘
                                                      │ Standard tools:
                                                      ▼
                                            template._call_tool() → tool.call(args)
```

---

## 3. COMPLETE TOOL INVENTORY

### Category A: File Operations (9 tools)

| # | Tool Name | Class | File | Required Params | Optional Params | Description |
|---|-----------|-------|------|-----------------|-----------------|-------------|
| 1 | `read_file` | `ReadFile` | `tools/custom/file_ops.py:35` | `path` (str) | `start_line` (int, default 1), `limit` (int, -1 for unlimited) | Read file content with pagination. Handles text and binary files via hex dump. Character-limited reads. |
| 2 | `view_image` | `ViewImage` | `tools/custom/file_ops.py:205` | `path` (str) | — | View image files (PNG, JPG, GIF, WEBP, SVG→PNG auto-convert, BMP). Returns ContentItem list. |
| 3 | `write_file` | `WriteFile` | `tools/custom/file_ops.py:346` | `path` (str), `content` (str) | `justification` (str) | Create or overwrite file with auto-backup. Requires user approval for existing files. |
| 4 | `edit_file` | `EditFile` | `tools/custom/file_ops.py:414` | `path` (str), `old_content` (str), `new_content` (str) | `match_mode` (enum: exact/heuristic/heuristic_agnostic, default 'exact'), `justification` (str) | Surgical text replacement. Preserves rest of file content. |
| 5 | `list_dir` | `ListDir` | `tools/custom/file_ops.py:510` | `path` (str) | — | List files and subdirectories in a path. |
| 6 | `grep` | `Grep` | `tools/custom/file_ops.py:539` | `pattern` (str) | `path` (str, default '.'), `include` (glob, default '*'), `exclude` (glob, default ''), `ignore_vcs` (bool, default True), `context` (int, default 0), `smart_case` (bool, default True) | Regex-based file search. Respects .gitignore. |
| 7 | `delete_file` | `DeleteFile` | `tools/custom/file_ops.py:618` | `path` (str) | — | Delete with timestamped backup. Requires approval for unowned files. |
| 8 | `copy_file` | `CopyFile` | `tools/custom/file_ops.py:648` | `source` (str), `destination` (str) | — | Copy file/directory. Auto-backup on overwrite. |
| 9 | `move_file` | `MoveFile` | `tools/custom/file_ops.py:683` | `source` (str), `destination` (str) | — | Move file/directory. Auto-backup on overwrite. Requires approval. |

### Category B: Code Operations (3 tools)

| # | Tool Name | Class | File | Required Params | Optional Params | Description |
|---|-----------|-------|------|-----------------|-----------------|-------------|
| 10 | `code_interpreter` | `CodeInterpreter` | `tools/code_interpreter.py:215` | `code` (str) | `files` (list[str]), `timeout` (int), `fix_paths` (bool, default True) | Docker-based Python sandbox. Full Jupyter kernel with matplotlib, numpy, pandas support. |
| 11 | `code_map` | `CodeMap` | `tools/custom/code_map.py:16` | `path` (str) | `force_as` (str — language override) | AST-based code structure mapping for Python; regex heuristic for other languages. Returns class/function line numbers. |
| 12 | `re_indent` | `ReIndent` | `tools/custom/file_ops.py:718` | `path` (str), `lines` (str range), `indent` (int), `indent_type` (enum: space/tab) | `mode` (enum: shift/flat/convert, default 'shift') | Re-indent code blocks. Shift, flatten, or convert between tabs/spaces. |

### Category C: Agent Management (3 tools + 1 proxy)

| # | Tool Name | Class | File | Required Params | Optional Params | Description |
|---|-----------|-------|------|-----------------|-----------------|-------------|
| 13 | `call_agent` | `_AgentInstanceFunctionProxy` (schema) / handled by `ToolDispatcher` | `tools/_agent_instance_proxy.py:51` + `tool_dispatcher.py:150` | `agent_class` (str), `instance_name` (str), `task` (str) | `context` (str), `log_file` (str — for session restore) | Delegate tasks to specialized sub-agents. Sync or async execution with slot collision detection. |
| 14 | `dismiss_agent` | `DismissAgent` | `tools/custom/manager_ops.py:213` | (none, but needs one of below) | `instance_name` (str), `all_idle` (bool) | Clear sub-agent conversation context and backups. Fires WebSocket callback for UI. |
| 15 | `list_agents` | `ListAgents` | `tools/custom/manager_ops.py:315` | — | — | List all agent templates + active instances with status, context usage, log paths. |

### Category D: Context Management (2 tools)

| # | Tool Name | Class | File | Required Params | Optional Params | Description |
|---|-----------|-------|------|-----------------|-----------------|-------------|
| 16 | `compress_context` | `CompressContext` | `tools/custom/compression_tools.py:16` | `fraction` (number, 0.3–1.0) | `mode` (enum: auto/manual), `summary_text` (str), `force` (bool) | Summarize conversation history to free context space. Delegates to unified `compress_context()`. |
| 17 | `forget_last` | `ForgetLast` | `tools/custom/forget_last_tool.py:13` | — | `count` (int, default 1, max 100) | Retroactively truncate last N tool call responses. Affects both in-memory pool and JSONL log file. |

### Category E: System & Information (4 tools)

| # | Tool Name | Class | File | Required Params | Optional Params | Description |
|---|-----------|-------|------|-----------------|-----------------|-------------|
| 18 | `system_info` | `SystemInfo` | `tools/custom/system_info.py:15` | — | — | OS, time/date, working dirs with Docker mount paths, Python version, session stats. |
| 19 | `shell_cmd` | `ShellCmd` | `tools/custom/shell_cmd.py:5` | `command` (str), `justification` (str) | `cwd` (str), `timeout` (int, default 30) | Execute shell commands. Auto-approved for read-only commands. |
| 20 | `read_logs` | `ReadLogs` | `tools/custom/read_logs.py:7` | `log_file` (str) | `max_chars_per_message` (int, default 1000), `range` (str, e.g. "1:10", "5:", ":20"; negative indices supported; omit to default to last 20 entries) | Read agent JSONL log files with middle-point truncation. |
| 21 | `calculate` | `Calculate` | `tools/custom/calculation.py:23` | `expression` (str) | — | Evaluate math expressions. Supports arithmetic, trig, logs, random functions. Restricted eval (no imports). |

### Category F: Web & Data (2 tools)

| # | Tool Name | Class | File | Required Params | Optional Params | Description |
|---|-----------|-------|------|-----------------|-----------------|-------------|
| 22 | `web_extractor` | `WebExtractor` | `tools/web_extractor.py:21` | `url` (str) | — | Extract webpage content using SimpleDocParser. Saves to local work dir. |

### Category G: Internal/Infrastructure Tools (NOT exposed to agents)
These are registered in `TOOL_REGISTRY` but intentionally NOT added to `agent.function_map` by `register_standard_tools()`:

| # | Tool Name | Class | File | Purpose |
|---|-----------|-------|-------|---------|
| 23 | `code_interpreter` | `CodeInterpreter` | — | Docker Python sandbox (1125 lines, includes kernel management, watchdog, container lifecycle) |
| 24 | `web_search` | `WebSearch` | `tools/web_search.py:27` | DuckDuckGo web search |
| 25 | `amap_weather` | `AmapWeather` | `tools/amap_weather.py:24` | Amap weather API (Chinese mapping service) |
| 26 | `doc_parser` | `DocParser` | `tools/doc_parser.py:57` | Document parsing with multiple backends |
| 27 | `simple_doc_parser` | `SimpleDocParser` | `tools/simple_doc_parser.py:384` | Lightweight doc parser (used by web_extractor internally) |
| 28 | `image_gen` | `ImageGen` | `tools/image_gen.py:24` | Image generation |
| 29 | `image_search` | `ImageSearch` | `tools/image_search.py:138` | Reverse image search |
| 30 | `image_zoom_in_qwen3vl` | `ImageZoomInToolQwen3VL` | `tools/image_zoom_in_qwen3vl.py:31` | Image zoom/crop for Qwen3-VL models |
| 31 | `python_executor` | `PythonExecutor` | `tools/python_executor.py:95` | Direct Python execution (non-Docker) |
| 32 | `storage` | `Storage` | `tools/storage.py:27` | Key-value storage for agent memory |
| 33 | `retrieval` | `Retrieval` | `tools/retrieval.py:42` | RAG retrieval system (dynamically loads search tools) |
| 34 | `extract_doc_vocabulary` | `ExtractDocVocabulary` | `tools/extract_doc_vocabulary.py:28` | Vocabulary extraction from documents |
| 35 | Search tools | `KeywordSearch`, `VectorSearch`, `HybridSearch`, `FrontPageSearch` | `tools/search_tools/*.py` | RAG search backends |

---

## 4. TOOL DISPATCH MECHANISM

### 4.1 ToolDispatcher (`tool_dispatcher.py`)
```
ToolDispatcher.__init__(pool)          # Line 61 — receives AgentPool only
    ↓
dispatcher.set_engine(engine)          # Line 77 — two-phase init after engine ready
    ↓
execute_tool(instance, name, args, messages, function_id)   # Line 83
    ├── call_agent       → handle_call_agent()     [Line 150]
    │                       ├── _validate_call_agent_args()  [449]
    │                       ├── recursive self-call cloning  [186-190]
    │                       ├── class mismatch detection     [193-198]
    │                       ├── _check_nesting_depth()       [480]
    │                       └── slot collision → sync/async path
    │                           ├── _run_child_sync()   [270] — caller holds slot
    │                           └── _run_child_async()  [363] — no slot held
    │
    ├── dismiss_agent    → handle_dismiss_agent()  [Line 233]
    │                       ├── self/supervisor guard
    │                       └── pool.dismiss_instance()
    │
    ├── compress_context → compression_handler     [Line 124-128]
    │
    └── all others       → template._call_tool()   [Line 139-146]
                               ↓
                           agent.function_map[name].call(args)
```

### 4.2 Special Routing for `call_agent`
The `call_agent` tool is NOT executed through the normal `tool.call()` path. Instead:
1. **Schema proxy** (`_AgentInstanceFunctionProxy`) exists only so LLM sees it in function list
2. **ExecutionEngine intercepts** at `ToolDispatcher.execute_tool()` line 111
3. **Sync vs Async**: Decided by slot collision detection (lines 220-231)
   - If caller holds concurrency semaphore → sync path (releases slot, runs child, re-acquires)
   - Otherwise → async path (registers via `pool.register_async_call()`)

### 4.3 Argument Resolution Pipeline
```
LLM emits tool call with JSON args
    ↓
ToolDispatcher.execute_tool() calls engine._resolve_placeholders()
    ├── Handles __USE_PREV_ARG__ placeholder resolution (tool_utils.py:100-178)
    │   └── Looks up previous args from agent_pool.last_tool_args cache
    └── Returns resolved dict or error string
    ↓
resolved args passed to tool handler
    ↓
engine._cache_tool_args() stores for future __USE_PREV_ARG__ resolution
```

---

## 5. TOOL UTILITIES (`tool_utils.py`)

### 5.1 Functions
| Function | Line | Purpose |
|----------|------|---------|
| `mark_tool_call_truncated()` | 19 | Thread-local truncation state tracking |
| `was_tool_call_truncated()` | 35 | Check if tool call was truncated |
| `clear_truncation_state()` | 51 | Reset truncation markers per thread |
| `generate_spillover_filename()` | 61 | Unique filename generation with collision detection (up to 1000 retries) |
| `resolve_prev_arg_placeholders()` | 100 | Resolve `__USE_PREV_ARG__` tokens from last tool call args |

### 5.2 Spillover System
When tool results exceed context thresholds:
- Full output written to `<workspace>/logs/spillover/{instance}_{tool}_{timestamp}.txt`
- Capped at 50MB (`MAX_SPILL_SIZE`)
- Truncation notice includes spillover path for agent to re-read

---

## 6. TOOL RESULT TRUNCATION (`tool_dispatcher.py:509-637`)

### 6.1 Truncation Logic
```
truncate_tool_result(result, tool_name, messages):
    ├── Exempt tools: compress_context, read_file, write_file, edit_file, delete_file, copy_file, move_file
    ├── Token counting via Qwen tokenizer
    ├── Wild-read detection (> DEFAULT_TOOL_RESULT_MAX_CHARS chars)
    │   └── Configurable via env var QWEN_AGENT_TOOL_RESULT_MAX_CHARS or pool.llm_cfg
    ├── Per-tool threshold: 25% of available tokens
    └── Total threshold: 95% of max tokens (minus system message tokens)
```

### 6.2 Truncation Tiers
1. **No truncation**: Result fits within per-tool threshold AND total under 95%
2. **Normal truncation**: Cut to `per_tool_threshold` tokens, write spillover file
3. **Wild-read truncation**: Cut to 500 tokens (aggressive), write spillover file

---

## 7. TOOL CATEGORIES SUMMARY

| Category | Count | Tools | Access Level |
|----------|-------|-------|-------------|
| File Operations | 9 | read_file, view_image, write_file, edit_file, list_dir, grep, delete_file, copy_file, move_file | Read: free / Write: user approval |
| Code Operations | 3 | code_interpreter, code_map, re_indent | Free |
| Agent Management | 3+1 | call_agent, dismiss_agent, list_agents (+ schema proxy) | Free |
| Context Management | 2 | compress_context, forget_last | Free |
| System & Info | 4 | system_info, shell_cmd, read_logs, calculate | Free (shell: approval for non-read-only) |
| Web & Data | 1 | web_extractor | Free |
| **Total Agent-Facing** | **22** | — | — |
| Internal/Infrastructure | 13+ | code_interpreter, search tools, storage, etc. | TOOL_REGISTRY only |

---

## 8. OBSERVATIONS & FINDINGS

### 8.1 Strengths
1. **Clean separation**: `TOOL_REGISTRY` (global class registry) vs `function_map` (per-agent instance map). Registry stores classes; each agent gets its own tool instances with injected dependencies.
2. **Two-phase initialization**: ToolDispatcher uses lazy engine init to avoid circular imports.
3. **Centralized metadata**: All descriptions live in `TOOL_METADATA` dict (`prompts/dna.py`) — single source of truth for LLM-facing descriptions.
4. **Thread-safe truncation**: Uses thread-local storage instead of string matching for truncation detection.
5. **Spillover system**: Prevents data loss when tool results are truncated.

### 8.2 Potential Issues
1. **Duplicate registration**: `call_agent`, `dismiss_agent`, and `list_agents` use `@register_tool(..., allow_overwrite=True)` — they're registered BOTH in the global registry AND manually instantiated by `register_standard_tools()`. The proxy schema for `call_agent` is separate from the actual handler logic.
2. **Tool count mismatch**: 35+ tools are registered globally but only ~22 are wired to agents. Some (web_search, amap_weather) appear unused in standard agent configurations.
3. **No tool versioning**: Tools have no version field; schema changes could silently break existing sessions.
4. **Hardcoded exemptions**: Truncation exempt list (`compress_context`, `read_file`, etc.) is hardcoded in `tool_dispatcher.py:539`. Adding new tools requires code changes.
5. **Parameter naming inconsistency**: Some tools use legacy parameter names (e.g., `start_line` → `offset`, `old_string` → `old_content`) with normalization logic inside `call()`. This creates fragility.

### 8.3 Design Patterns Identified
- **Decorator registration** (`@register_tool`)
- **Template method** (`BaseTool.call()` abstract, subclasses implement)
- **Lazy initialization** (two-phase init for ToolDispatcher)
- **Proxy pattern** (`_AgentInstanceFunctionProxy` for call_agent schema)
- **Strategy pattern** (different tool categories with different execution paths)
- **Slot-based concurrency control** (semaphore management for sync/async child agents)

---

## 9. FILE REFERENCE INDEX

| File | Lines | Purpose |
|------|-------|---------|
| `agent_cascade/tools/base.py` | 233 | BaseTool, TOOL_REGISTRY, register_tool(), ToolServiceError |
| `agent_cascade/tool_dispatcher.py` | 700 | Main routing: execute_tool(), call_agent/dismiss_agent handlers, truncation |
| `agent_cascade/tool_utils.py` | 178 | Shared utilities: spillover filenames, arg placeholder resolution, truncation state |
| `agent_cascade/agent_factory.py` | 273 | register_standard_tools() — wires tools to agents |
| `agent_cascade/agent.py` | 340 | Agent base class with _call_tool(), _init_tool(), function_map management |
| `agent_cascade/tools/custom/file_ops.py` | 793 | All file operation tools (10 classes) |
| `agent_cascade/tools/custom/manager_ops.py` | 410 | call_agent, dismiss_agent, list_agents |
| `agent_cascade/tools/custom/compression_tools.py` | ~154 | compress_context tool |
| `agent_cascade/tools/_agent_instance_proxy.py` | ~34 | _AgentInstanceFunctionProxy class + re-exports schemas from dna.py |
| `agent_cascade/prompts/dna.py` | ~549 | TOOL_METADATA (single source of truth for all tool schemas including call_agent/dismiss_agent) |

---

*End of audit report.*