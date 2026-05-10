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

        # Workspace and Folders information
        default_ws = DEFAULT_WORKSPACE
        ro_folders = []
        rw_folders = []
        if hasattr(self, 'agent_pool') and self.agent_pool and self.agent_pool.operation_manager:
            om = self.agent_pool.operation_manager
            default_ws = str(om.base_dir)
            ro_folders = [str(p) for p in om.extra_work_folders_ro]
            rw_folders = [str(p) for p in om.extra_work_folders_rw]

        folders_info = f"Default Workspace (RW): {default_ws}\n"
        if rw_folders:
            folders_info += f"Additional RW Folders: {', '.join(rw_folders)}\n"
        if ro_folders:
            folders_info += f"Additional RO Folders: {', '.join(ro_folders)}\n"

        info = (
            f"--- System Information ---\n"
            f"OS: {os_info}\n"
            f"Current Time: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Python Version: {py_version}\n"
            f"API Endpoint: {api_base}\n"
            f"Model Used: {model}\n"
            f"--- Workspace & Permissions ---\n"
            f"{folders_info}"
            f"--- Session Stats ---\n"
            f"{stats_str}\n"
            f"--- Tool Policy ---\n"
            f"{tools_str}"
        )
        return info
