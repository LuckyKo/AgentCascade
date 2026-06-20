# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
AgentCascade Utils Package

This package provides utility functions used across the agent_cascade module.
"""

from __future__ import annotations


def merge_disabled_tools_for_auto_agent(
    existing_disabled: list[str] | dict[str, list[str]] | None,
    agent_key: str,
    default_disabled_tools: frozenset[str],
) -> list[str] | dict[str, list[str]]:
    """Merge default disabled tools for an auto-launched agent.
    
    This function handles both the flat list format and per-agent dict format
    of disabled_tools configuration. It always returns a new object to avoid
    implicit reference semantics issues when modifying dicts in-place.
    
    Args:
        existing_disabled: Current disabled_tools value from config. May be:
            - None: No existing disabled tools configured
            - list[str]: Flat list of tool names to disable for all agents
            - dict[str, list[str]]: Per-agent mapping of agent keys to their
              disabled tool lists
        agent_key: Agent name key for dict format (e.g., 'Security', 'Compressor')
        default_disabled_tools: Frozenset of tools to always disable for this 
            agent type. These are merged with any existing disabled tools.
    
    Returns:
        Merged disabled_tools value ready for assignment back to config:
            - If input was dict: returns new dict with updated entry for agent_key
            - If input was list/tuple: returns new list with merged tools
            - If input was None: returns list of default_disabled_tools
    
    Example:
        >>> merge_disabled_tools_for_auto_agent(
        ...     {'Security': ['shell_cmd']}, 
        ...     'Security', 
        ...     frozenset({'write_file', 'edit_file'})
        ... )
        {'Security': ['shell_cmd', 'edit_file', 'write_file']}  # deterministic order
        
        >>> merge_disabled_tools_for_auto_agent(
        ...     ['grep'], 
        ...     'Compressor', 
        ...     frozenset({'call_agent'})
        ... )
        ['grep', 'call_agent']  # deterministic order
    """
    if isinstance(existing_disabled, dict):
        # Per-agent dict format: merge into the specific agent's entry (deterministic order)
        existing_tools = existing_disabled.get(agent_key, [])
        sorted_defaults = sorted(default_disabled_tools)  # Sort for deterministic ordering
        merged_list = existing_tools + sorted_defaults
        
        # Return new dict to avoid implicit reference semantics
        result = dict(existing_disabled)
        result[agent_key] = list(dict.fromkeys(merged_list))  # Deduplicate while preserving order
        return result
    
    elif isinstance(existing_disabled, (list, tuple)):
        # Flat list format: merge all tools into a single deduplicated list (stable order)
        sorted_defaults = sorted(default_disabled_tools)  # Sort for deterministic ordering
        return list(dict.fromkeys(list(existing_disabled) + sorted_defaults))
    
    else:
        # Fallback: existing_disabled is None or unexpected type — use defaults only (sorted)
        return sorted(default_disabled_tools)

