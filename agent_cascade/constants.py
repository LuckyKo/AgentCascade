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
DEFAULT_SECURITY_DISABLED_TOOLS: frozenset[str] = ALL_USER_APPROVAL_TOOLS


# Default disabled tools for Compressor agent.
# Compressor agent needs all approval tools disabled PLUS sub-agent management tools
# to prevent it from spawning new agents during compression.
DEFAULT_COMPRESSOR_DISABLED_TOOLS: frozenset[str] = (
    ALL_USER_APPROVAL_TOOLS | frozenset({
        'call_agent',   # Delegate tasks to specialized agent instances
        'dismiss_agent',  # End sub-agent sessions and clear context
        'list_agents',  # List available agent classes and active instances
    })
)


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