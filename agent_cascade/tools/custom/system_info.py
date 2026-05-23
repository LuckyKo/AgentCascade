import sys
import os
import platform
import datetime
import logging
from typing import Dict, Any
from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.utils.utils import get_history_stats

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA

logger = logging.getLogger(__name__)

@register_tool('system_info', allow_overwrite=True)
class SystemInfo(BaseTool):
    """Tool to get the current system information including OS, time, date, cwd, python version, and session stats."""
    
    name = 'system_info'
    description = TOOL_METADATA['system_info']['description']
    parameters = {
        'type': 'object',
        'properties': {},
        'required': [],
    }
    
    def __init__(self, agent_pool=None, agent_name=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool
        self.agent_name = agent_name

    def _is_path_allowed(self, abs_path: str, allowed_prefixes: set) -> bool:
        """Check if a path is within an allowed directory (mirrors code_interpreter._is_path_allowed)."""
        for prefix in allowed_prefixes:
            try:
                if os.path.commonpath([abs_path, prefix]) == prefix:
                    return True
            except ValueError:
                # Different drive letters on Windows
                continue
        return False

    def call(self, params: str, **kwargs) -> str:
        # Just return the information as a string
        now = datetime.datetime.now()
        os_info = f"{platform.system()} {platform.release()} ({platform.version()})"
        py_version = sys.version
        cwd = os.getcwd()
        try:
            cwd_contents = os.listdir(cwd)
            # Limit the output if there are too many files
            if len(cwd_contents) > 20:
                cwd_contents = cwd_contents[:20] + [f"... and {len(cwd_contents) - 20} more"]
            cwd_str = ", ".join(cwd_contents)
        except Exception as e:
            cwd_str = f"Error reading directory: {str(e)}"
            
        # Determine current history and agent name
        agent_name = kwargs.get('agent_instance_name') or self.agent_name or 'orchestrator'
        history = kwargs.get('messages')
        agent_obj = kwargs.get('agent_obj')
        
        # Fallback to agent pool if messages not in kwargs
        if not history and hasattr(self, 'agent_pool') and self.agent_pool:
            history = self.agent_pool.get_conversation(agent_name)
        
        history = history or []
        stats_str = f"Current Agent ({agent_name}) History Length: {len(history)} messages"
        
        # Max context detection
        max_context = "Unknown"
        if agent_obj:
            if hasattr(agent_obj, '_get_max_tokens'):
                max_context = agent_obj._get_max_tokens()
            elif hasattr(agent_obj, 'llm') and hasattr(agent_obj.llm, 'cfg'):
                from agent_cascade.settings import DEFAULT_MAX_INPUT_TOKENS
                cfg = agent_obj.llm.cfg
                max_context = cfg.get('generate_cfg', {}).get('max_input_tokens') or cfg.get('max_input_tokens') or DEFAULT_MAX_INPUT_TOKENS
        
        # Use centralized history stats (tokens & words)
        try:
            stats = get_history_stats(history)
            total_tokens = stats['tokens']
            total_words = stats['words']
            stats_str += f"\nCurrent Agent Usage: ~{total_tokens} tokens, ~{total_words} words (Max Context: {max_context})"
        except Exception as e:
            logger.warning(f"Failed to calculate stats for {agent_name}: {e}")
            
        if hasattr(self, 'agent_pool') and self.agent_pool:
            stats_str = f"Number of running sessions: {len(self.agent_pool.instance_conversations)}\n" + stats_str
        else:
            stats_str = "Agent Pool not connected.\n" + stats_str

        # Tools information
        tools_str = ""
        if agent_obj:
            all_tools = sorted(agent_obj.function_map.keys())
            active_tools_schemas = agent_obj._get_active_functions()
            active_tools = sorted([t['name'] for t in active_tools_schemas])
            tools_str = f"Available Tools: {', '.join(all_tools)}\n"
            tools_str += f"Enabled Tools: {', '.join(active_tools)}\n"
        
        # Resolve Model and API base
        model = "Unknown"
        api_base = "Unknown"
        if agent_obj and hasattr(agent_obj, 'llm') and agent_obj.llm:
            model = getattr(agent_obj.llm, 'model', "Unknown")
            if hasattr(agent_obj.llm, 'cfg'):
                cfg = agent_obj.llm.cfg
                api_base = cfg.get('api_base') or cfg.get('base_url') or cfg.get('model_server') or "Unknown"

        # Workspace and Folders information (with Docker container mount paths)
        default_ws = DEFAULT_WORKSPACE
        ro_folders = []
        rw_folders = []
        has_docker_context = False
        if hasattr(self, 'agent_pool') and self.agent_pool and self.agent_pool.operation_manager:
            om = self.agent_pool.operation_manager
            default_ws = str(om.base_dir)
            ro_folders = [str(p) for p in om.extra_work_folders_ro]
            rw_folders = [str(p) for p in om.extra_work_folders_rw]
            has_docker_context = True

        # Build folder info with Docker mount mappings (mirrors code_interpreter.py validation logic)
        # Only claim Docker paths when operation_manager is available (Docker context exists)
        if has_docker_context:
            folders_info = f"Default Workspace (RW): {default_ws} → /workspace (Docker)\n"
            
            # Filter and mount RW folders exactly like code_interpreter.py (lines 648-660)
            allowed_prefixes = {os.path.realpath(default_ws)} if default_ws else set()
            for fp in [*rw_folders, *ro_folders]:  # include both RW and RO (mirrors code_interpreter.py:644)
                allowed_prefixes.add(os.path.realpath(fp))
            
            rw_idx = 0
            if rw_folders:
                folders_info += "Additional RW Folders:\n"
                for folder in rw_folders:
                    abs_path = os.path.realpath(folder)
                    # Skip if path doesn't exist as a directory
                    if not os.path.isdir(abs_path):
                        continue
                    # Skip if path is outside allowed directories (security check)
                    if not self._is_path_allowed(abs_path, allowed_prefixes):
                        continue
                    docker_path = f"/workspace/extra_rw_{rw_idx}"
                    folders_info += f"  - {folder} → {docker_path} (Docker)\n"
                    rw_idx += 1
            
            # Filter and mount RO folders exactly like code_interpreter.py (lines 662-674)
            ro_idx = 0
            if ro_folders:
                folders_info += "Additional RO Folders:\n"
                for folder in ro_folders:
                    abs_path = os.path.realpath(folder)
                    # Skip if path doesn't exist as a directory
                    if not os.path.isdir(abs_path):
                        continue
                    # Skip if path is outside allowed directories (security check)
                    if not self._is_path_allowed(abs_path, allowed_prefixes):
                        continue
                    docker_path = f"/workspace/extra_ro_{ro_idx}"
                    folders_info += f"  - {folder} → {docker_path} (Docker, read-only)\n"
                    ro_idx += 1
        else:
            # No Docker context — don't claim Docker mount paths
            folders_info = f"Default Workspace (RW): {default_ws} → /workspace (no Docker context)\n"

        info = (
            f"--- System Information ---\n"
            f"OS: {os_info}\n"
            f"Current Time: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Python Version: {py_version}\n"
            f"API Endpoint: {api_base}\n"
            f"Model Used: {model}\n"
            f"\n--- Workspace & Permissions ---\n"
            f"{folders_info}"
            f"\n--- Session Stats ---\n"
            f"{stats_str}\n"
            f"\n--- Tool Policy ---\n"
            f"{tools_str}"
        )
        return info
