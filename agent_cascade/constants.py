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
    'move_file',      # Move files or directories
})


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
    'max_parallel_agents',
    
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