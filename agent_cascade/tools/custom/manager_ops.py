from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA


@register_tool('list_agents', allow_overwrite=True)
class ListAgents(BaseTool):
    """Tool to list all available agent classes and their active instances."""

    name = 'list_agents'
    description = TOOL_METADATA['list_agents']['description']
    parameters = {
        'type': 'object',
        'properties': {},
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        if not self.agent_pool:
            return "Error: No agent pool available."

        from agent_cascade.utils.utils import get_history_stats
        import datetime

        lines = ["# Agent Management Inventory\n"]
        lines.append("Use this list to monitor context usage and status of your workers. "
                     "To delegate a new task, use `call_agent`. To free up resources, use `dismiss_agent`.\n")
        
        # 1. Available Agent Templates
        lines.append("## 1. Agent Templates (Available Classes)")
        for agent_name in self.agent_pool.list_agents():
            info = self.agent_pool.get_agent_info(agent_name)
            tagline = (info.get('tagline') or 'No tagline available.') if info else 'No template info available.'
            # description = info.get('description', '') if info else ''
            tools = info.get('tools', []) if info else []
            tools_str = f" [Capabilities: {', '.join(tools)}]" if tools else ""
            
            lines.append(f"- **{agent_name}**: {tagline}{tools_str}")
            # if description:
            #     # Add truncated background for context
            #     short_desc = description.strip().split('\n')[0]
            #     if len(short_desc) > 200: short_desc = short_desc[:197] + "..."
            #     lines.append(f"  _{short_desc}_")
        lines.append("")

        # 2. Active & Persistent Instances
        lines.append("## 2. Active Instances (Sessions)")
        
        # Get all known instances from classes or conversations
        all_instances = sorted(list(set(self.agent_pool.instance_classes.keys()) | 
                                    set(self.agent_pool.instance_conversations.keys())))
        
        if not all_instances:
            lines.append("- No active or persistent instances.")
        else:
            for inst_name in all_instances:
                cls_name = self.agent_pool.instance_classes.get(inst_name, "Unknown")
                inst_obj = self.agent_pool.instances.get(inst_name)
                is_executing = inst_obj.is_running if inst_obj else False
                status_emoji = "🟢" if is_executing else "⚪"
                status_text = "ACTIVE" if is_executing else "IDLE"
                
                # Context Metrics
                msgs = self.agent_pool.get_conversation(inst_name)
                # We slice history to show exactly what the LLM is currently working with
                active_msgs = self.agent_pool.slice_history_for_llm(msgs) if self.agent_pool else msgs
                stats = get_history_stats(active_msgs)
                
                # Metadata & Traceability
                logger_inst = self.agent_pool.instance_loggers.get(inst_name)
                log_path = logger_inst.log_path if logger_inst and hasattr(logger_inst, 'log_path') else "N/A"
                
                last_active = "Unknown"
                if logger_inst and hasattr(logger_inst, 'data'):
                    ts_str = logger_inst.data['metadata'].get('last_update')
                    if ts_str:
                        try:
                            dt = datetime.datetime.fromisoformat(ts_str)
                            last_active = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except:
                            last_active = ts_str

                summary = self.agent_pool.instance_summaries.get(inst_name, "None")
                if len(summary) > 150:
                    summary = summary[:147] + "..."

                lines.append(f"### {status_emoji} Instance: `{inst_name}`")
                lines.append(f"  - **Status**: {status_text} | **Class**: {cls_name}")
                lines.append(f"  - **Context Usage**: {stats['tokens']} tokens / {stats['words']} words")
                lines.append(f"  - **Last Activity**: {last_active}")
                lines.append(f"  - **Summary**: {summary}")
                lines.append(f"  - **Log Path**: `{log_path}`")
                lines.append("")
        
        return "\n".join(lines)



