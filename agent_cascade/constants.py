# ============================================================================
# AgentCascade Constants Module
# ============================================================================
# Centralized constants for the agent_cascade package.
# This module provides shared configuration values, tool sets, and other
# constants to avoid duplication across multiple files.
# ============================================================================

from __future__ import annotations


# ────────────────────────────────────────────────────────────────────────────
# Tool Sets (frozensets for immutability and set operations)
# ────────────────────────────────────────────────────────────────────────────

# Tools that require user approval before execution.
# Auto-launched agents should not use these tools to prevent unexpected side effects.
ALL_USER_APPROVAL_TOOLS: frozenset[str] = frozenset({
    'shell_cmd',      # Execute shell commands on the host system
    'code_interpreter',  # Run Python code in a sandboxed environment
    'write_file',     # Create or overwrite files (requires approval if not agent-owned)
    'edit_file',      # Edit existing files (requires approval if not agent-owned)
    'delete_file',    # Delete files (requires approval if not agent-owned)
    'copy_file',      # Copy files or directories
})


# ── Agent-class default disabled tools (defense-in-depth) ────────────────────
# These frozensets are the authoritative source for Security and Compressor tool
# restrictions.  They are enforced automatically by the centralized resolver:
#     agent_cascade.utils.disabled_tools.resolve_disabled_tools_for_agent()
# Do NOT duplicate these constants in inline code — the resolver applies them
# as a final safety net regardless of upstream config overrides.


# Default disabled tools for Security agent.
# Security agent performs read-only analysis, so it should not use user-approval tools.
DEFAULT_SECURITY_DISABLED_TOOLS: frozenset[str] = frozenset({
    'call_agent',       # Delegate tasks to specialized agent instances
    'code_interpreter',  # Run Python code in a sandboxed environment
    'compress_context',  # Summarize conversation history
    'copy_file',        # Copy files or directories
    'delete_file',      # Delete files
    'dismiss_agent',    # End sub-agent sessions and clear context
    'edit_file',        # Edit existing files
    'forget_last',      # Forget/truncate last tool response
    'load_skill',       # Load registered skill instructions
    'propose_skill',    # Propose new reusable skills
    're_indent',        # Re-indent file blocks
    'scan_skills',      # Scan registered skills
    'shell_cmd',        # Execute shell commands on the host system
    'write_file',       # Create or overwrite files
})


# Default disabled tools for Compressor agent.
# Compressor agent needs all tools disabled — it only performs compression internally.
DEFAULT_COMPRESSOR_DISABLED_TOOLS: frozenset[str] = frozenset({
    'calculate',        # Evaluate mathematical expressions
    'call_agent',       # Delegate tasks to specialized agent instances
    'code_interpreter',  # Run Python code in a sandboxed environment
    'code_map',         # Map code file structure
    'compress_context',  # Summarize conversation history
    'copy_file',        # Copy files or directories
    'delete_file',      # Delete files
    'dismiss_agent',    # End sub-agent sessions and clear context
    'edit_file',        # Edit existing files
    'forget_last',      # Forget/truncate last tool response
    'grep',             # Search for text patterns in files
    'list_agents',      # List available agent classes and active instances
    'list_dir',         # List files and subdirectories
    'load_skill',       # Load registered skill instructions
    'propose_skill',    # Propose new reusable skills
    're_indent',        # Re-indent file blocks
    'read_file',        # Read file contents
    'read_logs',        # Read JSON/JSONL log files
    'scan_skills',      # Scan registered skills
    'shell_cmd',        # Execute shell commands on the host system
    'syntax_check',     # Check file syntax without executing
    'system_info',      # Retrieve current system information
    'view_image',       # View image files or capture screen content
    'web_extractor',    # Get content of a webpage
    'web_search',       # Search for information from the internet
    'write_file',       # Create or overwrite files
})


# Default disabled tools for Generalist agent.
DEFAULT_GENERALIST_DISABLED_TOOLS: frozenset[str] = frozenset({
    'call_agent',       # Delegate tasks to specialized agent instances
    'dismiss_agent',    # End sub-agent sessions and clear context
})


# Default disabled tools for Orchestrator agent.
DEFAULT_ORCHESTRATOR_DISABLED_TOOLS: frozenset[str] = frozenset({
    'forget_last',      # Forget/truncate last tool response
})


# Default disabled tools for Reviewer agent.
DEFAULT_REVIEWER_DISABLED_TOOLS: frozenset[str] = frozenset({
    'call_agent',       # Delegate tasks to specialized agent instances
    'dismiss_agent',    # End sub-agent sessions and clear context
})


# Default disabled tools for Writer agent.
DEFAULT_WRITER_DISABLED_TOOLS: frozenset[str] = frozenset({
    'shell_cmd',        # Execute shell commands on the host system
})


# Default disabled tools for dynamically discovered agents without explicit config.
# Security baseline: newly loaded agents start READ-ONLY until user grants more access.
# Applied ONLY when agent has zero disabled_tools configuration from Layers 1-2 AND is not
# orchestrator/security/compressor (core system agents excluded). See Layer 4 in
# agent_cascade.utils.disabled_tools.resolve_disabled_tools_for_agent().
DEFAULT_NEW_AGENT_DISABLED_TOOLS: frozenset[str] = frozenset({
    # Host/system access
    'shell_cmd',  # Execute shell commands on host system
    'code_interpreter',  # Run Python code in sandbox
    
    # File mutations (agents start read-only)
    'write_file',  # Create/overwrite files
    'edit_file',  # Modify existing files
    'delete_file',  # Delete files
    'copy_file',  # Copy files/directories
    're_indent',  # Re-indent file blocks (mutates files on disk)
    
    # Network access
    'web_search',  # External search queries
    'web_extractor',  # Fetch web page content
    
    # System modification
    'propose_skill',  # Create new reusable skills
    
    # MCP tools sentinel — disables all dynamically registered MCP tools by default
    '__all_mcp_tools__',  # Sentinel: block ALL MCP tools (e.g., memory-get, filesystem-read)
})

# Tools kept ENABLED by default for dynamically loaded agents:
# - call_agent, dismiss_agent, list_agents — essential for agent coordination
# - read_file, view_image, list_dir, grep — safe read-only file operations
# - compress_context, forget_last — context management (not security risks)
# - system_info, read_logs, code_map, calculate, syntax_check — info utilities
# - scan_skills, load_skill — skill access (read-only)


# Tools that are registered at runtime via agent_factory.py (not in TOOL_REGISTRY)
# but should be accepted by disabled_tools validation
RUNTIME_REGISTERED_TOOLS: frozenset[str] = frozenset({
    'call_agent',
    'dismiss_agent',
    'list_agents',
    '__all_mcp_tools__',  # Sentinel value for MCP tool blocking (not a real tool)
})


# ────────────────────────────────────────────────────────────────────────────
# Configuration Keys (tuples for use in membership tests)
# ────────────────────────────────────────────────────────────────────────────

# Config keys that should be broadcast from pool.llm_cfg to all agents.
# Used by api_integration.py to propagate tool char limits and image settings.
POOL_SETTINGS_TO_BROADCAST: tuple[str, ...] = (
    'tool_result_max_chars',
    'grep_char_limit',
    'grep_spillover',
    'shell_char_limit',
    'code_char_limit',
    'list_dir_char_limit',
    'max_images_for_llm',
)

# Default value for max_images_for_llm when not explicitly configured.
# -1 = keep all images, N >= 0 = keep only last N with base64 data.
MAX_IMAGES_FOR_LLM_DEFAULT: int = 2

# Config keys that should NOT be passed to the LLM API.
# These are operational settings used by the execution engine, not model parameters.
# This tuple merges ALL items from api_integration.py, api_server.py, and agent_invoker.py.
NON_LLM_KEYS: tuple[str, ...] = (
    # Execution control settings
    'max_auto_rollbacks',
    'auto_rollback_on_loop',
    'auto_continue',
    'max_turns',
    'enable_agent_budgeting',
    'max_parallel_agents',
    'max_input_tokens',  # Execution control (input truncation threshold) — not an LLM API parameter
    
    # MCP and workspace configuration
    'mcpServers',
    'work_access_folders',
    
    # Tool result limits
    'tool_result_max_chars',
    'grep_char_limit',
    'grep_spillover',
    'shell_char_limit',
    'code_char_limit',
    
    # Image base64 management (read from generate_cfg in _preprocess_messages, not sent to LLM API)
    'max_images_for_llm',
    
    # Tool-specific settings
    'disabled_tools',
    'seed',
    'read_file_limit',
    
    # Endpoint-identifying keys (exclude to let agents use their own API Router config)
    'model',
    'model_server',
    'api_base',
    'base_url',
    'api_key',
    'model_type',
)