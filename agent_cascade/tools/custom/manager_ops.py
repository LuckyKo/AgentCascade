import json
import copy
from typing import List, Optional, Dict, Any
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.llm.schema import Message, ROLE, ASSISTANT, USER


@register_tool('call_agent', allow_overwrite=True)
class CallAgent(BaseTool):
    """Tool to call other specialized agents for help."""

    name = 'call_agent'
    description = TOOL_METADATA['call_agent']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'agent_class': {
                'type': 'string',
                'description': TOOL_METADATA['call_agent']['parameters']['agent_class']
            },
            'instance_name': {
                'type': 'string',
                'description': TOOL_METADATA['call_agent']['parameters']['instance_name']
            },
            'task': {
                'type': 'string',
                'description': TOOL_METADATA['call_agent']['parameters']['task']
            },
            'context': {
                'type': 'string',
                'description': TOOL_METADATA['call_agent']['parameters']['context']
            },
            'parallel_launch': {
                'type': 'boolean',
                'description': TOOL_METADATA['call_agent']['parameters']['parallel_launch']
            },
            'log_file': {
                'type': 'string',
                'description': TOOL_METADATA['call_agent']['parameters']['log_file']
            }
        },
        'required': ['agent_class', 'instance_name', 'task'],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        agent_class = params['agent_class']
        instance_name = params['instance_name']
        task = params['task']
        context = params.get('context', '')

        # Resolve template
        agent = self.agent_pool.get_agent(agent_class)
        if not agent:
            return f"Error: Agent class '{agent_class}' not found. Available: {self.agent_pool.list_agents()}"

        # Prepare sub-agent logger
        caller = kwargs.get('agent_obj')
        caller_name = getattr(caller, 'name', 'Supervisor') if caller else 'Tool'
        
        logger_inst = self.agent_pool.get_logger(
            instance_name, 
            agent_class,
            base_metadata={'supervisor': caller_name}
        )
        
        # Isolation Safeguard: If this instance exists but was a DIFFERENT class, 
        # clear its history to avoid confusing stacking/context merging.
        existing_class = self.agent_pool.instance_classes.get(instance_name)
        if existing_class and existing_class != agent_class:
            logger_inst.info(f"Re-assigning instance '{instance_name}' from {existing_class} to {agent_class}. Clearing history.")
            self.agent_pool.clear_conversation(instance_name)
            # Re-get logger after clear
            logger_inst = self.agent_pool.get_logger(instance_name, agent_class)

        # Register instance class
        self.agent_pool.instance_classes[instance_name] = agent_class
        
        # Identify the caller (supervisor)
        caller = kwargs.get('agent_obj')
        caller_name = getattr(caller, 'name', 'Unknown')
        caller_class = caller.__class__.__name__ if caller else 'Tool'

        # Prepare sub-agent system message with identity and memory info
        metadata_prompt = f"""
[IDENTITY]
You are a specialized agent instance.
- Instance Name: {instance_name}
- Agent Class: {agent_class}
- Supervisor: {caller_name} ({caller_class})
- Working Dir: {logger_inst.data['metadata'].get('working_dir', 'Unknown')}
"""
        # Add extra paths if they exist
        extra_ro = logger_inst.data['metadata'].get('extra_paths_ro', [])
        extra_rw = logger_inst.data['metadata'].get('extra_paths_rw', [])
        if extra_ro:
            metadata_prompt += f"- Extra Paths (Read-Only): {', '.join(extra_ro)}\n"
        if extra_rw:
            metadata_prompt += f"- Extra Paths (Read-Write): {', '.join(extra_rw)}\n"
        # Ensure metadata is in sub-agent's prompt
        orig_sys = getattr(agent, 'system_message', "")
        if metadata_prompt not in orig_sys:
            agent.system_message = metadata_prompt + "\n" + orig_sys

        messages = self.agent_pool.instance_conversations.get(instance_name)
        if messages is None:
            # Initialize with agent's soul (SYSTEM message)
            messages = [Message(role=SYSTEM, content=agent.system_message)]
            self.agent_pool.instance_conversations[instance_name] = messages
            # Sync to persistent log immediately
            logger_inst.update_history(messages)
        elif not logger_inst.data["history"]:
            # Sync if memory exists but log doesn't
            logger_inst.update_history(messages)
        
        msg_text = (
            f"Context: {context}\n\nTask: {task}\n\nPlease help with this task."
            if context else task
        )
        user_msg = {ROLE: USER, 'content': msg_text}
        messages.append(user_msg)
        
        # Record user message in persistent log
        logger_inst.log_message(user_msg)

        max_internal_retries = 3
        internal_retries = 0
        while internal_retries <= max_internal_retries:
            try:
                response = []
                for resp in agent.run(messages=messages):
                    response = resp
                    
                    # Check for tool call events in the run
                    if resp and (resp[-1].get(ROLE) == FUNCTION or resp[-1].get('function_call')):
                        logger_inst.update_history(messages + resp)
                        # Check for loop
                        from agent_cascade.loop_detection import detect_loop
                        loop_info = detect_loop(messages + response)
                        if loop_info:
                            loop_reason, pop_count = loop_info
                            from agent_cascade.loop_detection import LoopDetectedError
                            logger.warning(f"Loop detected for sub-agent {instance_name}: {loop_reason}")
                            raise LoopDetectedError(loop_reason, agent_name=instance_name, pop_count=pop_count, turn_pop_count=len(response), resp_snapshot=list(response))

                if response:
                    messages.extend(response)
                    
                    # Final log sync for the session turn
                    logger_inst.update_history(messages)

                    # Accumulate refined text output
                    from agent_cascade.compression.helpers import extract_sub_agent_feedback
                    result_str = extract_sub_agent_feedback(response, instance_name)
                    
                    return f"[{instance_name}'s output]:\n{result_str}"
                
                return f"[{instance_name}]: No response generated"
            except Exception as e:
                from agent_cascade.loop_detection import LoopDetectedError
                if isinstance(e, LoopDetectedError):
                    internal_retries += 1
                    if internal_retries > max_internal_retries:
                        logger.warning(f"Sub-agent {instance_name} hit internal retry limit for loop: {e.reason}")
                        raise e  # Finally kick back to main if it keeps failing
                    
                    logger.warning(f"Sub-agent {instance_name} detected loop: {e.reason}. Performing internal retry ({internal_retries}/{max_internal_retries}).")
                    
                    # Telemetry: Record the loop event
                    try:
                        if hasattr(self.agent_pool, 'telemetry'):
                            self.agent_pool.telemetry.record_loop_detected(
                                instance_name, 
                                e.reason, 
                                auto_rolled_back=True, 
                                pop_count=e.pop_count
                            )
                    except Exception:
                        pass
                    
                    # Partial Turn Commitment: Save progress before the loop started
                    if hasattr(e, 'resp_snapshot') and e.resp_snapshot:
                        if e.pop_count < len(e.resp_snapshot):
                            keep_count = len(e.resp_snapshot) - e.pop_count
                            if keep_count > 0:
                                good_msgs = e.resp_snapshot[:keep_count]
                                messages.extend(good_msgs)
                                logger.info(f"Sub-agent {instance_name} partial recovery: Committed {keep_count} messages to history.")
                        else:
                            pool_pop = e.pop_count - len(e.resp_snapshot)
                            if pool_pop > 0:
                                self.agent_pool.surgical_rollback(instance_name, pool_pop)
                    elif e.pop_count > 0:
                        # Fallback for old errors or missing snapshots
                        if e.pop_count > e.turn_pop_count:
                            pool_pop = e.pop_count - e.turn_pop_count
                            self.agent_pool.surgical_rollback(instance_name, pool_pop)
                    
                    # 2. Inject hint to sub-agent
                    sub_hint = f"[SYSTEM]: Your last actions resulted in a repetitive loop ({e.reason}). Please try a different approach to solve the task."
                    messages.append({ROLE: USER, 'content': sub_hint})
                    
                    # 3. Sync log
                    logger_inst.update_history(messages)
                    
                    # 4. Refresh pointer for next attempt
                    continue
                    
                return f"Error calling agent {instance_name} ({agent_class}): {str(e)}"


@register_tool('dismiss_agent', allow_overwrite=True)
class DismissAgent(BaseTool):
    """Clear a sub-agent instance's conversation history (manager tool)."""

    name = 'dismiss_agent'
    description = TOOL_METADATA['dismiss_agent']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'instance_name': {
                'type': 'string',
                'description': TOOL_METADATA['dismiss_agent']['parameters']['instance_name']
            },
            'all_idle': {
                'type': 'boolean',
                'description': TOOL_METADATA['dismiss_agent']['parameters']['all_idle']
            }
        },
        'required': [],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        instance_name = params.get('instance_name')
        all_idle = params.get('all_idle', False)

        if all_idle:
            active_set = set(self.agent_pool.active_stack)
            all_instances = list(set(self.agent_pool.instance_classes.keys()) | 
                                 set(self.agent_pool.instance_conversations.keys()))
            
            dismissed = []
            # Capture log paths BEFORE clearing (clear_conversation removes the logger)
            for inst in all_instances:
                if inst not in active_set and inst != 'Maine':
                    # Get log path before it gets cleared
                    log_path = None
                    logger_inst = self.agent_pool.instance_loggers.get(inst)
                    if logger_inst:
                        log_path = getattr(logger_inst, 'log_path', None)
                    elif inst in self.agent_pool.instance_conversations:
                        from agent_cascade.log import logger
                        logger.warning(f"Log path not found for instance '{inst}' — may affect resurrection")
                    
                    self.agent_pool.clear_conversation(inst)
                    if hasattr(self.agent_pool, 'operation_manager') and self.agent_pool.operation_manager:
                        self.agent_pool.operation_manager.cleanup_backups(inst)
                    dismissed.append({'agent': inst, 'log_path': log_path})
                    
                    # Fire real-time callback for UI tab removal
                    if hasattr(self.agent_pool, '_fire_on_dismissed'):
                        self.agent_pool._fire_on_dismissed(inst, log_path)
            
            if not dismissed:
                return json.dumps({"status": "no_idle_agents", "message": "No idle agents found to dismiss."})
            
            return json.dumps({
                "status": "dismissed_all_idle",
                "agents": dismissed,
                "count": len(dismissed),
                "message": f"Successfully dismissed {len(dismissed)} idle agents: {', '.join(d['agent'] for d in dismissed)}"
            })

        if not instance_name:
            return json.dumps({"status": "error", "message": "Please provide 'instance_name' or set 'all_idle' to true."})

        if instance_name not in self.agent_pool.instance_conversations:
            return json.dumps({
                "status": "not_found",
                "agent": instance_name,
                "message": f"Instance '{instance_name}' not found."
            })

        # Capture log path BEFORE clearing (clear_conversation removes the logger)
        log_path = None
        logger_inst = self.agent_pool.instance_loggers.get(instance_name)
        if logger_inst:
            log_path = getattr(logger_inst, 'log_path', None)
        else:
            from agent_cascade.log import logger
            logger.warning(f"Log path not found for instance '{instance_name}' — may affect resurrection")

        self.agent_pool.clear_conversation(instance_name)
        if hasattr(self.agent_pool, 'operation_manager') and self.agent_pool.operation_manager:
            self.agent_pool.operation_manager.cleanup_backups(instance_name)
        
        # Fire real-time callback for UI tab removal (triggers WebSocket broadcast)
        if hasattr(self.agent_pool, '_fire_on_dismissed'):
            self.agent_pool._fire_on_dismissed(instance_name, log_path)

        return json.dumps({
            "status": "dismissed",
            "agent": instance_name,
            "log_path": log_path,
            "message": f"Agent instance '{instance_name}' dismissed — conversation context cleared and backups removed."
        })


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
        active_set = set(self.agent_pool.active_stack)
        
        # Get all known instances from classes or conversations
        all_instances = sorted(list(set(self.agent_pool.instance_classes.keys()) | 
                                    set(self.agent_pool.instance_conversations.keys())))
        
        if not all_instances:
            lines.append("- No active or persistent instances.")
        else:
            for inst in all_instances:
                cls_name = self.agent_pool.instance_classes.get(inst, "Unknown")
                is_active = inst in active_set
                status_emoji = "🟢" if is_active else "⚪"
                status_text = "ACTIVE" if is_active else "IDLE"
                
                # Context Metrics
                msgs = self.agent_pool.get_conversation(inst)
                # We slice history to show exactly what the LLM is currently working with
                active_msgs = self.agent_pool.slice_history_for_llm(msgs) if self.agent_pool else msgs
                stats = get_history_stats(active_msgs)
                
                # Metadata & Traceability
                logger_inst = self.agent_pool.instance_loggers.get(inst)
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

                summary = self.agent_pool.instance_summaries.get(inst, "None")
                if len(summary) > 150:
                    summary = summary[:147] + "..."

                lines.append(f"### {status_emoji} Instance: `{inst}`")
                lines.append(f"  - **Status**: {status_text} | **Class**: {cls_name}")
                lines.append(f"  - **Context Usage**: {stats['tokens']} tokens / {stats['words']} words")
                lines.append(f"  - **Last Activity**: {last_active}")
                lines.append(f"  - **Summary**: {summary}")
                lines.append(f"  - **Log Path**: `{log_path}`")
                lines.append("")
        
        return "\n".join(lines)



