"""
Agent Factory — Unified tool registration and agent loading.

All agents are loaded from their soul.md files as standard Agent instances
(capable of spawning sub-agents). The "main orchestrator" is just one more
agent with its own soul.md, not a special class. Tool availability is
controlled via the disabled_tools policy, not by which loader function was used.
"""

from agent_cascade.log import logger
from agent_cascade.tools.code_interpreter import CodeInterpreter
from agent_cascade.tools.web_extractor import WebExtractor
from agent_cascade.tools.custom import (
    ReadFile, ViewImage, WriteFile, EditFile, ListDir, Grep,
    DeleteFile, CopyFile, ReIndent, ListAgents, ShellCmd,
    ReadLogs, Calculate, CodeMap, ForgetLast, SyntaxCheck, ScanSkills,
    ProposeSkill,
)
from agent_cascade.tools.custom.compression_tools import CompressContext
from agent_cascade.tools.custom import DDGSearch, SystemInfo as _SystemInfo
from agent_cascade.soul_loader import create_agent_from_soul
from agent_cascade.settings import DEFAULT_WORKSPACE


def register_standard_tools(agent, agent_pool, agent_name: str):
    """
    Register the standard tool suite on an agent instance.

    Tool availability is driven by AVAILABLE_TOOLS in dna.py — edit that list to
    add/remove tools system-wide. Per-agent enable/disable is handled via the UI
    disabled_tools settings (applied at runtime by _get_active_functions_from_template).

    This is the SINGLE place where tool instances are created and wired
    to their agent_pool / agent_name.

    Args:
        agent: The agent to register tools on.
        agent_pool: The AgentPool instance (for file ops and approvals).
        agent_name: The role name (e.g. 'orchestrator', 'coder').
    """
    from agent_cascade.tools._agent_instance_proxy import _AgentInstanceFunctionProxy
    from agent_cascade.prompts.dna import AVAILABLE_TOOLS

    # ── Tool factory: maps tool name → (instance, needs_pool, needs_name) ──────
    # needs_pool  = set agent_pool attribute on the tool instance
    # needs_name  = set agent_name attribute on the tool instance
    tools_to_register = {}

    for tool_name in AVAILABLE_TOOLS:
        if tool_name == 'call_agent':
            tools_to_register[tool_name] = (_AgentInstanceFunctionProxy(tool_name), False, False)
        elif tool_name == 'list_agents':
            tools_to_register[tool_name] = (ListAgents(agent_pool=agent_pool), False, False)
        elif tool_name == 'dismiss_agent':
            tools_to_register[tool_name] = (_AgentInstanceFunctionProxy(tool_name), False, False)
        elif tool_name == 'read_file':
            t = ReadFile()
            tools_to_register[tool_name] = (t, True, False)
        elif tool_name == 'view_image':
            t = ViewImage()
            tools_to_register[tool_name] = (t, True, False)
        elif tool_name == 'list_dir':
            t = ListDir()
            tools_to_register[tool_name] = (t, True, False)
        elif tool_name == 'grep':
            t = Grep()
            tools_to_register[tool_name] = (t, True, False)
        elif tool_name == 'write_file':
            t = WriteFile()
            tools_to_register[tool_name] = (t, True, True)
        elif tool_name == 'edit_file':
            t = EditFile()
            tools_to_register[tool_name] = (t, True, True)
        elif tool_name == 're_indent':
            t = ReIndent()
            tools_to_register[tool_name] = (t, True, True)
        elif tool_name == 'delete_file':
            t = DeleteFile()
            tools_to_register[tool_name] = (t, True, True)
        elif tool_name == 'copy_file':
            t = CopyFile()
            tools_to_register[tool_name] = (t, True, True)
        elif tool_name == 'compress_context':
            t = CompressContext()
            tools_to_register[tool_name] = (t, True, True)
        elif tool_name == 'shell_cmd':
            t = ShellCmd()
            tools_to_register[tool_name] = (t, True, True)
        elif tool_name == 'read_logs':
            t = ReadLogs()
            tools_to_register[tool_name] = (t, True, False)
        elif tool_name == 'code_map':
            t = CodeMap()
            tools_to_register[tool_name] = (t, True, False)
        elif tool_name == 'forget_last':
            t = ForgetLast()
            tools_to_register[tool_name] = (t, True, True)
        elif tool_name == 'code_interpreter':
            # Code interpreter needs special setup with operation_manager
            try:
                om = agent_pool.operation_manager
                if om is not None:
                    code_cfg = {'work_dir': str(om.base_dir)}
                    if hasattr(om, 'extra_work_folders_ro') and om.extra_work_folders_ro:
                        code_cfg['extra_work_folders_ro'] = [str(p) for p in om.extra_work_folders_ro]
                    if hasattr(om, 'extra_work_folders_rw') and om.extra_work_folders_rw:
                        code_cfg['extra_work_folders_rw'] = [str(p) for p in om.extra_work_folders_rw]
                    if not code_cfg.get('extra_work_folders_ro') and not code_cfg.get('extra_work_folders_rw'):
                        logger.debug("CodeInterpreter: No extra work folders configured (RO/RW)")
                    t = CodeInterpreter(cfg=code_cfg)
                    t._operation_manager = om
                    tools_to_register[tool_name] = (t, False, False)
                else:
                    logger.warning("Skipping CodeInterpreter for agent %s: operation_manager is None", agent_name)
            except Exception as e:
                logger.warning(f"Failed to load CodeInterpreter for agent {agent_name}: {e}")
        elif tool_name == 'ddg_search':
            tools_to_register[tool_name] = (DDGSearch(), False, False)
        elif tool_name == 'web_extractor':
            tools_to_register[tool_name] = (WebExtractor(cfg={'work_dir': DEFAULT_WORKSPACE}), False, False)
        elif tool_name == 'system_info':
            tools_to_register[tool_name] = (_SystemInfo(agent_pool=agent_pool), False, False)
        elif tool_name == 'calculate':
            tools_to_register[tool_name] = (Calculate(), False, False)
        elif tool_name == 'syntax_check':
            t = SyntaxCheck()
            tools_to_register[tool_name] = (t, True, False)
        elif tool_name == 'scan_skills':
            t = ScanSkills(agent_pool=agent_pool)
            tools_to_register[tool_name] = (t, False, False)
        elif tool_name == 'propose_skill':
            t = ProposeSkill(agent_pool=agent_pool)
            tools_to_register[tool_name] = (t, False, False)
        else:
            logger.debug("Unknown tool '%s' in AVAILABLE_TOOLS — skipping", tool_name)

    # Wire up all registered tools into the agent's function_map
    for name, (instance, needs_pool, needs_name) in tools_to_register.items():
        if instance is not None:
            if needs_pool and hasattr(instance, 'agent_pool'):
                instance.agent_pool = agent_pool
            if needs_name and hasattr(instance, 'agent_name'):
                instance.agent_name = agent_name
            agent.function_map[name] = instance

    # ── User approval system notice ──
    agent.system_message += """
    
User Approval System:
- All mutating operations (file write, edit, delete, move, copy) require explicit user approval.
- When you call a tool like write_file or edit_file, the user will see a prompt and can approve or reject.
- If rejected, you'll receive the user's reason. Adjust your approach accordingly.
- Read operations (read_file, list_dir, grep, view_image) are free access.

Workspace & Path Reference:
- ALL file tool paths (read_file, write_file, edit_file, list_dir, grep, etc.) are RELATIVE to the workspace root.
  Example: to read "src/main.py" within your workspace, use path "src/main.py" (NOT an absolute host path).
- shell_cmd executes commands with the workspace directory as the working directory.
- code_interpreter runs Python inside a Docker container where the workspace is mounted at "/workspace/".
  So a file at "src/main.py" (used by host tools) is available at "/workspace/src/main.py" inside Docker.
  The container's working directory is /workspace, so relative paths like "src/main.py" also work.
  Additional mounted folders (if configured) appear as "/extra_rw_N" (writable) and
  "/extra_ro_N" (read-only). A path mapping file "path_mapping_{kernel_id}.json" in the
  work_dir lists all mounts with their host paths and access modes — read it to discover available folders.
"""


def load_agent(agent_pool, agent_name: str, llm_cfg: dict = None):
    """
    Load any agent (including the orchestrator) from its soul.md.
    
    Every agent is a standard Agent instance — fully capable of spawning and
    managing sub-agents. The "main orchestrator" is just another agent
    whose soul.md gives it a supervisor personality.
    
    Args:
        agent_pool: The AgentPool instance.
        agent_name: The agent's role name (e.g. 'orchestrator', 'coder').
        llm_cfg: LLM config used when APIRouter is not active.
        
    Returns:
        Fully configured Agent instance with tools registered.
    """
    import copy

    # Ensure each agent gets its own distinct LLM instance config
    # to avoid state bleed across parallel threads.
    if agent_pool.api_router is not None:
        agent_llm_cfg = agent_pool.api_router.get_llm_config(agent_name)
    else:
        agent_llm_cfg = llm_cfg or agent_pool.llm_cfg
    
    # Validate that we have a usable LLM config before proceeding
    if agent_llm_cfg is None:
        raise ValueError(
            f"No LLM configuration available for agent '{agent_name}'. "
            "Pass llm_cfg to load_agent() or provide it when constructing AgentPool."
        )
        
    # Deepcopy to prevent shared references in the LLM object tree
    agent_llm_cfg = copy.deepcopy(agent_llm_cfg)

    soul_path = agent_pool.agents_dir / f'{agent_name}_soul.md'

    if soul_path.exists():
        agent, config = create_agent_from_soul(
            agent_llm_cfg,
            str(soul_path),
            role_name=agent_name,
        )
    else:
        # Fallback: no soul.md — create with a generic prompt
        from agent_cascade.agents import Assistant

        config = {}
        system_prompt = _default_agent_prompt(agent_pool, agent_name)
        agent = Assistant(
            llm=agent_llm_cfg,
            name=agent_name.replace('_', ' ').title(),
            description=f"{agent_name.replace('_', ' ').title()} agent",
            system_message=system_prompt,
            function_list=[],
        )
        agent.agent_type = agent_name.replace('_', ' ').title()  # Mirrors main branch — needed for disabled_tools lookup
        agent.agent_configs = {agent_name: config}
        agent.base_system_message = system_prompt

    # ── Full tool suite (same for every agent) ──
    register_standard_tools(agent, agent_pool, agent_name)

    return agent


# ── Convenience wrappers ────────────────────────────────────────────────────────

def load_orchestrator_agent(agent_pool, llm_cfg: dict):
    """Load the orchestrator agent. Delegates to load_agent('orchestrator')."""
    return load_agent(agent_pool, 'orchestrator', llm_cfg)


def load_agent_template(agent_pool, agent_name: str, llm_cfg: dict):
    """Load an agent template. Delegates to load_agent()."""
    return load_agent(agent_pool, agent_name, llm_cfg)


def _default_agent_prompt(agent_pool, agent_name: str) -> str:
    """Fallback system prompt when no soul.md exists."""
    prompt = f"""You are {agent_name.replace('_', ' ').title()}, an AI assistant that can coordinate with specialized agent instances.

Available sub-agents:
"""
    for name in agent_pool.list_agents():
        info = agent_pool.get_agent_info(name)
        if info:
            prompt += f"\n- **{info['name']}**: {info['tagline']}"

    prompt += """

Tools:
- call_agent: Delegate tasks to a specialized sub-agent
- dismiss_agent: Clear a sub-agent's conversation context
- list_agents: Show available sub-agents

Example of delegating a task:
{"name": "call_agent", "arguments": {"agent_class": "coder", "instance_name": "worker1", "task": "Write a script"}}
"""
    return prompt
